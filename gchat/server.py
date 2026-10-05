"""HTTP + WebSocket server. The browser UI lives in web/."""

import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import images, llm, settings
from .engine import Engine

WEB_DIR = os.path.join(settings.APP_DIR, "web")

engine = Engine()


@asynccontextmanager
async def lifespan(app):
    settings.load()
    os.makedirs(settings.MEDIA_DIR, exist_ok=True)
    engine.start()
    yield
    engine.shutdown()  # writes any pending save


app = FastAPI(lifespan=lifespan)

# Settings the browser may change. Paths and internal state stay server-side.
EDITABLE_SETTINGS = {"api_key", "username", "image_model", "thinking", "memory_enabled",
                     "show_whispers", "memory_fallback_model", "time_awareness",
                     "bsky_handle", "bsky_app_password"}

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


class OnlyThisApp:
    """Refuse requests from other websites open in the same browser.

    The server has no login: it trusts whoever can reach it, which by
    default is only this computer. But a web page you visit can still send
    requests to localhost. So:
    - requests whose Origin isn't this app are refused. That covers
      cross-site form posts (e.g. one that would press Play and spend your
      credits) and cross-site WebSockets (which could read your chats).
    - when listening on loopback only, the Host must be a loopback name,
      which blocks DNS-rebinding tricks.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        origin = headers.get("origin", "")
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        ok = True
        if os.environ.get("GROUPCHAT_LAN") != "1" and hostname.lower() not in LOOPBACK_HOSTS:
            ok = False
        if origin and origin != "null" and urlsplit(origin).netloc.lower() != host.lower():
            ok = False
        if origin == "null":
            ok = False
        if ok:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"Forbidden: not from this app"})


app.add_middleware(OnlyThisApp)


@app.middleware("http")
async def cache_headers(request, call_next):
    """UI files: re-checked on every load, so a `git pull` and restart shows
    the new version. Images: their file names never change (a new picture
    gets a new name), so browsers keep them rather than asking again."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    elif path.startswith(("/media/", "/avatars/", "/thumbs/")) and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
os.makedirs(settings.MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
app.mount("/media", StaticFiles(directory=settings.MEDIA_DIR), name="media")
os.makedirs(settings.AVATARS_DIR, exist_ok=True)
app.mount("/avatars", StaticFiles(directory=settings.AVATARS_DIR), name="avatars")


@app.get("/thumbs/{name}")
async def thumb(name: str):
    """A chat-sized copy of an image, made on first request and kept."""
    path = await asyncio.to_thread(images.thumbnail, name)
    if not path:
        raise HTTPException(404, "no such image")
    return FileResponse(path)


@app.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.websocket("/ws")
async def events(ws: WebSocket):
    await ws.accept()
    queue = engine.subscribe()
    try:
        await ws.send_json(engine.snapshot())
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        engine.unsubscribe(queue)


# ─── settings & models ──────────────────────────────────────────────────

@app.get("/api/state")
async def current_state():
    return engine.snapshot()


@app.post("/api/settings")
async def update_settings(values: dict = Body(...)):
    values = {k: v for k, v in values.items() if k in EDITABLE_SETTINGS}
    for secret in settings.SECRETS:
        if secret in values and not values[secret]:
            values.pop(secret)  # blank means "keep the current one"
    if values.get("bsky_app_password") == "-":
        values["bsky_app_password"] = ""  # "-" forgets it
    settings.update(values)
    engine.emit_snapshot()
    return settings.public()


@app.get("/api/models")
async def models():
    try:
        return await llm.list_models()
    except Exception as e:
        raise HTTPException(502, f"couldn't reach OpenRouter: {e}")


# ─── chats ──────────────────────────────────────────────────────────────

@app.post("/api/chats")
async def new_chat():
    engine.new_chat()
    return {"id": engine.chat["id"]}


@app.post("/api/chats/{chat_id}/open")
async def open_chat(chat_id: str):
    if not engine.open_chat(chat_id):
        raise HTTPException(404, "no such chat")
    return {"ok": True}


@app.patch("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, values: dict = Body(...)):
    engine.rename_chat(chat_id, values.get("title", ""))
    return {"ok": True}


@app.patch("/api/chat")
async def update_chat(values: dict = Body(...)):
    engine.update_chat(values.get("title"), values.get("settings"))
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    engine.delete_chat(chat_id)
    return {"ok": True}


@app.post("/api/messages")
async def post_message(values: dict = Body(...)):
    await engine.human_message(values.get("text", ""), values.get("image"))
    return {"ok": True}


@app.post("/api/upload")
async def upload(values: dict = Body(...)):
    try:
        return {"image": engine.save_upload(values.get("data", ""))}
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/polls/{poll_id}/vote")
async def vote(poll_id: str, values: dict = Body(...)):
    engine.human_vote(poll_id, int(values.get("option", 0)))
    return {"ok": True}


@app.post("/api/control/{action}")
async def control(action: str):
    if action not in ("play", "pause", "step"):
        raise HTTPException(400, "unknown action")
    getattr(engine, action)()
    return {"running": engine.running}


# ─── members ────────────────────────────────────────────────────────────

@app.post("/api/members")
async def add_member(values: dict = Body(...)):
    if not values.get("model"):
        raise HTTPException(400, "pick a model")
    member = engine.add_member(values["model"], values.get("name"),
                               float(values.get("temperature", 1.0)),
                               values.get("illustrator"), int(values.get("draw_every") or 30),
                               bool(values.get("web")))
    return member


@app.patch("/api/members/{member_id}")
async def update_member(member_id: str, values: dict = Body(...)):
    engine.update_member(member_id, values)
    return {"ok": True}


@app.delete("/api/members")
async def remove_all_members():
    engine.remove_all_members()
    return {"ok": True}


@app.delete("/api/members/{member_id}")
async def remove_member(member_id: str):
    engine.remove_member(member_id)
    return {"ok": True}


# ─── profile pictures ──────────────────────────────────────────────────

@app.post("/api/avatars")
async def set_avatar(values: dict = Body(...)):
    if not values.get("model"):
        raise HTTPException(400, "which model?")
    try:
        return {"avatar": engine.set_avatar(values["model"], values.get("data", ""))}
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.delete("/api/avatars")
async def remove_avatar(model: str):
    engine.remove_avatar(model)
    return {"ok": True}


# ─── memory ─────────────────────────────────────────────────────────────

@app.get("/api/memory")
async def memories(model: str):
    return engine.memories(model)


@app.post("/api/memory/forget")
async def forget(values: dict = Body(...)):
    from .identity_memory import MemoryFileError
    try:
        engine.forget_memory(values["model"], values["id"])
    except MemoryFileError as e:
        raise HTTPException(503, str(e))
    return engine.memories(values["model"])
