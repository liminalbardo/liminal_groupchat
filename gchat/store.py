"""Saved chats: one JSON file per chat in data/chats/."""

import json
import os
import re
import time
import uuid

from . import settings
from .fsutil import write_text
from .prompts import DEFAULT_ROOM_PROMPT

PALETTE = ["#e8590c", "#1c7ed6", "#2f9e44", "#9c36b5", "#e03131",
           "#0c8599", "#f08c00", "#5f3dc4", "#c2255c", "#495057"]

DEFAULT_CHAT_SETTINGS = {
    "mode": "natural",          # natural | everyone | round_robin
    "pace": 1.5,                # seconds between messages
    "overlap": 0.2,             # natural mode: chance two AIs reply at once
    "messages_per_play": 20,    # autoplay stops after this many (0 = no limit)
    "spend_cap": 1.0,           # USD per chat; autoplay stops there (0 = no cap)
    "room_prompt": DEFAULT_ROOM_PROMPT,
}


def new_id():
    return uuid.uuid4().hex[:10]


def new_chat(title="new chat"):
    now = time.time()
    return {
        "id": new_id(),
        "title": title,
        "created": now,
        "updated": now,
        "members": [],
        "messages": [],
        "cost": 0.0,
        "settings": dict(DEFAULT_CHAT_SETTINGS),
    }


def new_member(chat, model, name, temperature=1.0, illustrator=False, draw_every=30, web=False):
    used = {m["color"] for m in chat["members"]}
    color = next((c for c in PALETTE if c not in used), PALETTE[len(chat["members"]) % len(PALETTE)])
    return {
        "id": new_id(),
        "model": model,
        "name": name,
        "temperature": temperature,
        "color": color,
        "muted": False,
        # Illustrators draw the chat instead of talking, every ~draw_every messages
        "illustrator": bool(illustrator),
        "draw_every": int(draw_every),
        # Can search the web and open pages while replying (paid per search)
        "web": bool(web),
    }


def _path(chat_id):
    # Chat IDs come from URLs: never let one reach outside the chats folder
    if not isinstance(chat_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", chat_id):
        raise ValueError(f"not a chat id: {chat_id!r}")
    return os.path.join(settings.CHATS_DIR, f"{chat_id}.json")


def serialize(chat):
    """The chat as JSON text. Cheap enough for the event loop; the slow part,
    writing it to disk, can then happen on another thread."""
    chat["updated"] = time.time()
    return json.dumps(chat, ensure_ascii=False, separators=(",", ":"))


def write(chat_id, text):
    write_text(_path(chat_id), text)


def save(chat):
    write(chat["id"], serialize(chat))


def load(chat_id):
    try:
        with open(_path(chat_id), "r", encoding="utf-8") as f:
            chat = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(chat, dict) or not isinstance(chat.get("messages"), list):
        return None
    chat.setdefault("settings", {})
    for key, value in DEFAULT_CHAT_SETTINGS.items():
        chat["settings"].setdefault(key, value)
    for msg in chat.get("messages", []):
        if msg.get("status") in ("streaming", "pending"):
            msg["status"] = "done"  # interrupted by a restart
    return chat


def delete(chat_id):
    try:
        os.remove(_path(chat_id))
    except (OSError, ValueError):
        pass


_summaries = {}  # file name -> ((mtime, size), summary): only changed files are re-read


def _summary(chat):
    return {
        "id": chat["id"],
        "title": chat["title"],
        "updated": chat.get("updated", 0),
        "members": [{"name": m["name"], "color": m["color"]} for m in chat["members"]],
        "count": sum(1 for m in chat["messages"] if m["kind"] in ("text", "image", "poll")),
    }


def list_chats():
    os.makedirs(settings.CHATS_DIR, exist_ok=True)
    chats, seen = [], set()
    for name in os.listdir(settings.CHATS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            st = os.stat(os.path.join(settings.CHATS_DIR, name))
        except OSError:
            continue
        seen.add(name)
        stamp = (st.st_mtime_ns, st.st_size)
        cached = _summaries.get(name)
        if not cached or cached[0] != stamp:
            chat = load(name[:-5])
            if not chat:
                continue
            cached = (stamp, _summary(chat))
            _summaries[name] = cached
        chats.append(cached[1])
    for gone in set(_summaries) - seen:
        del _summaries[gone]
    chats.sort(key=lambda c: -c["updated"])
    return chats
