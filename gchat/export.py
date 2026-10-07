"""Export a chat for reading elsewhere, by people or by other LLMs.

- zip: transcript.md plus an images/ folder. Each image sits in the
  transcript where it was posted, with its prompt/caption written out, so
  an LLM that only reads the text still knows what each picture showed.
- page: the same transcript as a print-ready web page; the browser's
  "Save as PDF" turns it into a PDF with the images inline.
"""

import html
import io
import re
import time
import zipfile

from . import images, settings

WHISPERS = True  # whispers are part of the story; they're labelled as private


def _when(ts):
    return time.strftime("%a %d %b %Y, %H:%M", time.localtime(ts)) if ts else ""


def _names(chat):
    names = {m["id"]: m["name"] for m in chat.get("members", [])}
    names["human"] = settings.get("username") or "you"
    return names


def _who(msg, chat):
    name = msg.get("name") or _names(chat).get(msg.get("author"), "?")
    model = msg.get("model")
    return f"{name} ({model})" if model else name


def slug(chat):
    s = re.sub(r"[^a-z0-9]+", "-", (chat.get("title") or "chat").lower()).strip("-")
    return (s or "chat")[:60]


def _entries(chat):
    """The chat as a list of dicts, in order: what both formats are built from.
    Private notices (errors, hints meant for you) are left out."""
    names = _names(chat)
    out, n_images = [], 0
    for msg in chat.get("messages", []):
        kind = msg.get("kind")
        if msg.get("status") in ("error", "pending", "streaming") and kind == "image":
            continue
        if kind == "notice":
            if msg.get("private") or not msg.get("text"):
                continue
            out.append({"type": "notice", "ts": msg.get("ts"), "text": msg["text"]})
            continue
        if kind == "whisper" and not WHISPERS:
            continue
        e = {"type": kind, "ts": msg.get("ts"), "who": _who(msg, chat), "text": msg.get("text") or "",
             "reactions": msg.get("reactions") or {}, "sources": msg.get("sources") or []}
        if kind == "whisper":
            e["to"] = names.get(msg.get("to"), "someone")
        if kind == "poll":
            e["options"] = msg.get("options", [])
        if kind == "image":
            path = images.source_path(msg.get("image") or "")
            if not path:
                continue
            n_images += 1
            e.update(path=path, image=msg["image"], prompt=msg.get("prompt") or "",
                     number=n_images, illustration=bool(msg.get("illustration")))
        out.append(e)
    return out


def _image_label(e):
    what = "drew" if e.get("illustration") else "posted an image"
    label = f"{e['who'].split(' (')[0]} {what}"
    if e["prompt"]:
        label += f': "{e["prompt"]}"'
    return label


def _reactions(e):
    return ", ".join(f"{emoji} {' & '.join(who)}" for emoji, who in e["reactions"].items())


# ─── markdown ───────────────────────────────────────────────────────────

def markdown(chat, image_paths):
    """`image_paths` maps image number -> its path inside the export."""
    members = chat.get("members", [])
    lines = [f"# {chat.get('title') or 'chat'}", ""]
    lines.append(f"A group chat between AIs{' and a human' if settings.get('username') else ''}, "
                 f"exported {_when(time.time())}.")
    if members:
        lines += ["", "Members:"]
        lines += [f"- {m['name']} ({m['model']})" + (" - illustrator" if m.get("illustrator") else "")
                  for m in members]
        lines.append(f"- {settings.get('username') or 'you'} (human)")
    lines += ["", "---", ""]
    for e in _entries(chat):
        when = _when(e["ts"])
        if e["type"] == "notice":
            lines += [f"*{e['text']}* ({when})", ""]
            continue
        head = f"**{e['who']}**"
        if e["type"] == "whisper":
            head += f" → whispered privately to **{e['to']}**"
        lines.append(f"{head} · {when}")
        if e["type"] == "image":
            lines.append(f"![{_image_label(e)}]({image_paths[e['number']]})")
            lines.append(f"[image {e['number']}: {_image_label(e)}]")
            if e["text"]:
                lines.append(f"> {e['text']}")
        elif e["type"] == "poll":
            lines.append(f"📊 Poll: {e['text']}")
            for o in e["options"]:
                lines.append(f"- {o['text']}: {', '.join(o['votes']) or 'no votes'}")
        else:
            lines.append(e["text"])
        if e["sources"]:
            lines.append("Sources: " + " ".join(s["url"] for s in e["sources"]))
        if e["reactions"]:
            lines.append(f"Reactions: {_reactions(e)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def zip_bytes(chat):
    buf = io.BytesIO()
    paths = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for e in _entries(chat):
            if e["type"] != "image":
                continue
            data, mime = images.for_model(e["path"])  # same size the models see
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
                   "image/webp": "webp"}.get(mime, "jpg")
            paths[e["number"]] = f"images/{e['number']:04d}.{ext}"
            z.writestr(paths[e["number"]], data, zipfile.ZIP_STORED)
        z.writestr("transcript.md", markdown(chat, paths))
    return buf.getvalue()


# ─── printable page (→ PDF) ─────────────────────────────────────────────

PAGE_CSS = """
body { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1a1a1f; max-width: 760px; margin: 32px auto; padding: 0 20px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.meta { color: #666; font-size: 13px; margin-bottom: 24px; }
.msg { margin: 0 0 14px; break-inside: avoid; }
.head { font-size: 12.5px; color: #555; }
.head b { color: #1a1a1f; font-size: 14px; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
.whisper { border-left: 3px solid #999; padding-left: 10px; }
.notice { color: #666; font-style: italic; font-size: 13px; text-align: center; margin: 10px 0; }
img { display: block; max-width: 100%; max-height: 520px; margin: 6px 0 4px; border-radius: 8px; }
.caption, .extra { font-size: 12.5px; color: #555; }
.bar { position: sticky; top: 0; background: #fff; padding: 10px 0; margin-bottom: 12px; border-bottom: 1px solid #ddd; }
.bar button { font: inherit; font-weight: 600; padding: 8px 14px; border-radius: 8px; border: 0; background: #5b4cf0; color: #fff; cursor: pointer; }
.bar span { color: #666; font-size: 13px; margin-left: 10px; }
@media print { .bar { display: none; } body { margin: 0 auto; } }
"""


def page(chat):
    esc = html.escape
    members = chat.get("members", [])
    who = ", ".join(f"{m['name']} ({m['model']})" for m in members)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{esc(chat.get('title') or 'chat')}</title><style>{PAGE_CSS}</style></head><body>",
        "<div class='bar'><button onclick='print()'>Save as PDF</button>"
        "<span>In the print dialog, choose “Save as PDF” as the printer.</span></div>",
        f"<h1>{esc(chat.get('title') or 'chat')}</h1>",
        f"<div class='meta'>A group chat between AIs, exported {esc(_when(time.time()))}."
        + (f"<br>Members: {esc(who)}" if who else "") + "</div>",
    ]
    for e in _entries(chat):
        when = esc(_when(e["ts"]))
        if e["type"] == "notice":
            parts.append(f"<div class='notice'>{esc(e['text'])} · {when}</div>")
            continue
        head = f"<b>{esc(e['who'])}</b>"
        if e["type"] == "whisper":
            head += f" → whispered privately to <b>{esc(e['to'])}</b>"
        body = ""
        if e["type"] == "image":
            label = esc(_image_label(e))
            body = (f"<img src='/thumbs/{esc(e['image'])}' alt='{label}'>"
                    f"<div class='caption'>[image {e['number']}: {label}]</div>")
            if e["text"]:
                body += f"<div class='caption'>{esc(e['text'])}</div>"
        elif e["type"] == "poll":
            opts = "".join(f"<li>{esc(o['text'])}: {esc(', '.join(o['votes']) or 'no votes')}</li>"
                           for o in e["options"])
            body = f"<div class='text'>📊 Poll: {esc(e['text'])}</div><ul>{opts}</ul>"
        else:
            body = f"<div class='text'>{esc(e['text'])}</div>"
        if e["sources"]:
            body += "<div class='extra'>Sources: " + " ".join(
                f"<a href='{esc(s['url'])}'>{esc(s['url'])}</a>" for s in e["sources"]) + "</div>"
        if e["reactions"]:
            body += f"<div class='extra'>Reactions: {esc(_reactions(e))}</div>"
        cls = "msg whisper" if e["type"] == "whisper" else "msg"
        parts.append(f"<div class='{cls}'><div class='head'>{head} · {when}</div>{body}</div>")
    # Bring up the print dialog once the pictures have loaded
    parts.append("<script>addEventListener('load', () => setTimeout(print, 300))</script></body></html>")
    return "".join(parts)
