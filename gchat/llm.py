"""OpenRouter calls: streamed chat replies, one-shot completions, images, models."""

import base64
import json
import os
import time
import uuid

import httpx

from . import settings

# Overridable for testing against a local fake
API = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


class LLMError(Exception):
    pass


class RefusalError(LLMError):
    """The provider refused the request (content filter / refusal)."""


def _headers():
    return {
        "Authorization": f"Bearer {settings.api_key()}",
        "HTTP-Referer": "https://github.com/liminalbardo/liminal_groupchat",
        "X-Title": "Liminal Groupchat",
        "Content-Type": "application/json",
    }


def _reasoning():
    level = settings.get("thinking")
    if level == "off":
        return {"enabled": False, "exclude": True}
    return {"effort": level if level in ("low", "medium", "high") else "low", "exclude": True}


def _error_text(status, body):
    try:
        message = json.loads(body).get("error", {}).get("message")
    except (ValueError, AttributeError):
        message = None
    hint = ""
    if status == 401:
        hint = " (the key was refused for this request - run tools/check_images.py for details)"
    return f"{status}: {message or body[:300]}{hint}"


# Web access: OpenRouter runs these for the model mid-reply (search, then open
# pages), and bills the searches into the request's cost. Capped per reply.
WEB_TOOLS = [
    {"type": "openrouter:web_search", "parameters": {"max_results": 5, "max_uses": 3}},
    {"type": "openrouter:web_fetch", "parameters": {"max_uses": 3, "max_content_tokens": 6000}},
]


def _add_sources(sources, annotations):
    for a in annotations or []:
        cite = (a.get("url_citation") or a) if isinstance(a, dict) else {}
        url = cite.get("url")
        if url and url.startswith(("http://", "https://")) and all(s["url"] != url for s in sources):
            sources.append({"url": url, "title": (cite.get("title") or "").strip()[:120]})


async def stream_chat(model, messages, temperature=1.0, max_tokens=4000, on_delta=None,
                      web=False, sources=None):
    """Stream one reply. Calls `await on_delta(full_text_so_far)` as text arrives.

    With `web`, the model can search the web and open pages while it writes;
    pages it cites are appended to `sources` as {"url", "title"}.
    Returns (text, cost_in_usd).
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "reasoning": _reasoning(),
        "usage": {"include": True},
    }
    if web:
        payload["tools"] = WEB_TOOLS
    if sources is None:
        sources = []
    text, cost = "", 0.0
    timeout = httpx.Timeout(180, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{API}/chat/completions",
                                 headers=_headers(), json=payload) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")
                raise LLMError(_error_text(response.status_code, body))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"].get("message", chunk["error"]))[:300])
                usage = chunk.get("usage") or {}
                if usage.get("cost") is not None:
                    cost = float(usage["cost"])
                for choice in chunk.get("choices") or []:
                    _add_sources(sources, (choice.get("delta") or {}).get("annotations"))
                    _add_sources(sources, (choice.get("message") or {}).get("annotations"))
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        text += delta
                        if on_delta:
                            await on_delta(text)
    return text, cost


def _text_of(content):
    """Message content as text; some providers send a list of parts."""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def complete_sync(model, messages, max_tokens=4000, on_cost=None):
    """One blocking completion, for memory formation on background threads.

    Raises LLMError saying why when there's no usable reply. If thinking ate
    the whole token budget, retries once with more room.
    """
    for budget in (max_tokens, max_tokens * 4):
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": budget,
            "reasoning": {"effort": "low", "exclude": True},
            "usage": {"include": True},
        }
        try:
            response = httpx.post(f"{API}/chat/completions", headers=_headers(),
                                  json=payload, timeout=300)
        except httpx.HTTPError as e:
            raise LLMError(f"request failed: {e}")
        if response.status_code != 200:
            raise LLMError(_error_text(response.status_code, response.text))
        try:
            data = response.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected reply: {response.text[:200]}")
        cost = (data.get("usage") or {}).get("cost")
        if on_cost and cost is not None:
            on_cost(float(cost))
        text = _text_of((choice.get("message") or {}).get("content")).strip()
        if text:
            return text
        reason = choice.get("finish_reason") or choice.get("native_finish_reason")
        if reason != "length":
            refusal = (choice.get("message") or {}).get("refusal")
            error = RefusalError if (refusal or reason == "content_filter") else LLMError
            raise error(f"empty reply (finish reason: {reason}{', refusal: ' + refusal if refusal else ''})")
    raise LLMError("empty reply: it used its whole token budget thinking")


# Models served on OpenRouter's /images endpoint instead of chat completions
IMAGES_ENDPOINT_MODELS = ("meta/muse-image", "bytedance-seed/seedream")


def uses_images_endpoint(model):
    return any(model.startswith(prefix) for prefix in IMAGES_ENDPOINT_MODELS)


def is_image_model(model):
    """True for models that draw rather than talk (so they join as illustrators)."""
    if uses_images_endpoint(model):
        return True
    known = next((m for m in _models_cache["models"] if m["id"] == model), None)
    return bool(known and known.get("draws"))


_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _save_image(raw, ext):
    os.makedirs(settings.MEDIA_DIR, exist_ok=True)
    name = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    with open(os.path.join(settings.MEDIA_DIR, name), "wb") as f:
        f.write(raw)
    return name


async def generate_image(prompt, model=None):
    """Generate an image. Returns (filename in MEDIA_DIR, cost, caption).

    The caption is any text the model sent back with the image (a revised
    prompt, or what a reasoning image model says it drew), else "".
    """
    model = model or settings.get("image_model")
    if uses_images_endpoint(model):
        return await _generate_via_images_endpoint(prompt, model)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
        "usage": {"include": True},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as client:
        response = await client.post(f"{API}/chat/completions", headers=_headers(), json=payload)
    if response.status_code != 200:
        raise LLMError(_error_text(response.status_code, response.text))
    data = response.json()
    try:
        message = data["choices"][0]["message"]
        url = (message.get("images") or [])[0]["image_url"]["url"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"{model} returned no image")
    if not url.startswith("data:image"):
        raise LLMError("unexpected image format")
    header, b64 = url.split(",", 1)
    ext = _EXT.get(header[5:].split(";")[0], ".jpg")
    caption = _text_of(message.get("content")).strip()
    return (_save_image(base64.b64decode(b64), ext),
            float((data.get("usage") or {}).get("cost") or 0.0), caption)


async def _generate_via_images_endpoint(prompt, model):
    """OpenRouter's /images endpoint (Muse, Seedream, ...): {model, prompt} in,
    {data: [{b64_json | url, media_type, revised_prompt}]} out."""
    payload = {"model": model, "prompt": prompt}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as client:
        response = await client.post(f"{API}/images", headers=_headers(), json=payload)
        if response.status_code != 200:
            raise LLMError(_error_text(response.status_code, response.text))
        data = response.json()
        images = data.get("data") or []
        if not images:
            raise LLMError(f"{model} returned no image")
        image = images[0]
        if image.get("b64_json"):
            raw = base64.b64decode(image["b64_json"])
            ext = _EXT.get(image.get("media_type", ""), ".png")
        elif image.get("url", "").startswith("data:image"):
            header, b64 = image["url"].split(",", 1)
            raw, ext = base64.b64decode(b64), _EXT.get(header[5:].split(";")[0], ".png")
        elif image.get("url"):
            fetched = await client.get(image["url"], timeout=60)
            fetched.raise_for_status()
            raw = fetched.content
            ext = _EXT.get(fetched.headers.get("content-type", "").split(";")[0], ".png")
        else:
            raise LLMError(f"no image data in the reply (keys: {list(image)})")
    caption = (image.get("revised_prompt") or data.get("text") or "").strip()
    return _save_image(raw, ext), float((data.get("usage") or {}).get("cost") or 0.0), caption


_models_cache = {"at": 0.0, "models": []}


async def supports_images(model):
    """True/False if OpenRouter says whether model takes images, None if unknown."""
    try:
        models = await list_models()
    except Exception:
        return None
    found = next((m for m in models if m["id"] == model), None)
    return found["vision"] if found else None


async def list_models():
    """Text-capable models on OpenRouter, cached for an hour."""
    if _models_cache["models"] and time.time() - _models_cache["at"] < 3600:
        return _models_cache["models"]
    # After a failure, don't retry for a while: every reply asks for this list
    if time.time() - _models_cache.get("failed_at", 0) < 300:
        raise LLMError("OpenRouter's model list is unavailable (retrying in a few minutes)")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{API}/models")
        response.raise_for_status()
    except httpx.HTTPError:
        _models_cache["failed_at"] = time.time()
        raise
    models = []
    for m in response.json().get("data", []):
        arch = m.get("architecture") or {}
        outputs = arch.get("output_modalities") or ["text"]
        if "text" not in outputs and "image" not in outputs:
            continue
        pricing = m.get("pricing") or {}

        def per_million(key):
            try:
                return round(float(pricing.get(key) or 0) * 1_000_000, 3)
            except (TypeError, ValueError):
                return None

        models.append({
            "vision": "image" in (arch.get("input_modalities") or []),
            # Draws rather than talks: joins the chat as an illustrator
            "draws": ("image" in outputs and "text" not in outputs) or uses_images_endpoint(m["id"]),
            "id": m["id"],
            "name": m.get("name") or m["id"],
            "context": m.get("context_length"),
            "input": per_million("prompt"),
            "output": per_million("completion"),
            "created": m.get("created") or 0,
        })
    models.sort(key=lambda m: -m["created"])
    _models_cache.update(at=time.time(), models=models)
    return models
