"""The chat engine: state, turn-taking, AI replies, commands and memory.

Everything here runs on the server's asyncio loop, except memory formation,
which runs on background threads (see identity_memory) and only reports cost
back through the loop.
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import random
import re
import time

from . import commands, images, llm, settings, store
from .identity_memory import IdentityMemory, MemoryFileError
from .prompts import ILLUSTRATOR, TITLE, build_system_prompt

MEMORY_EVERY = 6            # AI messages between memory-formation checks
UNREMEMBERED_TOKENS = 24000  # context cap per reply when memory is off
SAVE_DELAY = 0.5            # seconds: saves are batched, then written off the event loop
TITLE_AFTER = 10            # messages before an untitled chat gets named
QUIET_ROUNDS = 4            # rounds of nobody speaking before autoplay stops
IMAGE_WAIT = 120            # seconds to wait for pending images before the next reply
REPLY_TIMEOUT = 240         # seconds before a reply that never finishes is given up on
CONTEXT_IMAGES = 3          # most recent images sent as pictures to models that can see


def _ts():
    return time.time()


def _tokens(text):
    return len(text) // 4


class Engine:
    def __init__(self):
        self.chat = None
        self.running = False
        self.task = None
        self._save_error = None
        # One writer thread, so saves land in order and a slow disk (or a file
        # Dropbox is holding) never stalls the chat
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="save")
        self._dirty = None           # the chat waiting to be saved
        self._save_handle = None
        self._memory_problems = set()
        self._memory_refused = set()  # models whose memory requests are refused
        self.limit = 0               # messages this Play/Step should post (0 = no limit)
        self.posted = 0
        self.typing = set()
        self.passed = set()          # members who passed since the last message
        self.listeners = set()
        self.loop = None
        self.since_memory = 0
        self.tool_tasks = set()      # images and searches still running
        self.no_vision = set()       # models that turned out not to take images
        self._image_data = {}        # filename -> data URL, so files are read once
        self.memory = IdentityMemory(complete_fn=self._memory_complete)
        self.memory.set_scenario(settings.MEMORY_SCENARIO)

    # ─── events ─────────────────────────────────────────────────────────

    def subscribe(self):
        queue = asyncio.Queue()
        self.listeners.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.listeners.discard(queue)

    def emit(self, event):
        for queue in list(self.listeners):
            queue.put_nowait(event)

    def snapshot(self):
        return {
            "type": "snapshot",
            "settings": settings.public(),
            "chats": store.list_chats(),
            "chat": {**self.chat, "messages": [self._message_for_ui(m) for m in self.chat["messages"]]} if self.chat else None,
            "running": self.running,
            "typing": sorted(self.typing),
        }

    def emit_snapshot(self):
        self.emit(self.snapshot())

    def _emit_message(self, msg):
        self.emit({"type": "message", "message": self._message_for_ui(msg)})

    @staticmethod
    def _message_for_ui(msg):
        if msg.get("kind") == "image" and msg.get("image"):
            size = images.dimensions(msg["image"])
            if size:
                return {**msg, "image_width": size[0], "image_height": size[1]}
        return msg

    def _emit_status(self):
        self.emit({"type": "status", "running": self.running,
                   "cost": round(self.chat["cost"], 6) if self.chat else 0})

    # ─── chats ──────────────────────────────────────────────────────────

    def start(self):
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None  # no loop yet (tests); set again when playback starts
        chat_id = settings.get("current_chat")
        self.chat = self._load(chat_id) if chat_id else None
        if not self.chat:
            chats = store.list_chats()
            self.chat = self._load(chats[0]["id"]) if chats else None
        if not self.chat:
            self.new_chat()

    def _save(self):
        """Save the open chat soon. Saves within SAVE_DELAY are batched into
        one, and written on the writer thread. Without a running event loop
        (tests, startup) it saves straight away."""
        if not self.chat:
            return
        if self._dirty is not None and self._dirty is not self.chat:
            self.flush()  # a different chat was waiting: write it first
        self._dirty = self.chat
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self.flush(wait=True)
        if self._save_handle is None:
            self._save_handle = loop.call_later(SAVE_DELAY, self.flush)

    def flush(self, wait=False):
        """Write the pending save now (still on the writer thread)."""
        if self._save_handle:
            self._save_handle.cancel()
            self._save_handle = None
        chat, self._dirty = self._dirty, None
        if chat is None:
            return
        text = store.serialize(chat)
        future = self._writer.submit(self._write, chat["id"], text)
        if wait:
            future.result()

    def _write(self, chat_id, text):
        """On the writer thread. A failed save is reported, never fatal: the
        chat carries on in memory and the next save tries again."""
        try:
            store.write(chat_id, text)
            error = None
        except OSError as e:
            error = str(e)
        self._call_on_loop(self._save_result, error)

    def _load(self, chat_id):
        """Read a chat from disk after any saves still queued for it."""
        return self._writer.submit(store.load, chat_id).result()

    @staticmethod
    def _write_quietly(chat_id, text):
        try:
            store.write(chat_id, text)
        except OSError as e:
            print(f"[Save] couldn't save chat {chat_id}: {e}")

    def _call_on_loop(self, fn, *args):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(fn, *args)
        else:
            fn(*args)

    def _save_result(self, error):
        if error and self._save_error != error and self.chat:  # say it once, not every message
            self._notice(f"couldn't save this chat to disk (will keep trying): {error}", private=True)
        self._save_error = error

    def shutdown(self):
        """Stop playback and wait for every pending save to reach disk."""
        self.pause()
        self.flush()
        writer, self._writer = self._writer, ThreadPoolExecutor(max_workers=1, thread_name_prefix="save")
        writer.shutdown(wait=True)

    def _set_current(self, chat):
        self.chat = chat
        self.passed.clear()
        self.typing.clear()
        settings.update({"current_chat": chat["id"]}, persist=False)
        self._writer.submit(self._save_settings)

    @staticmethod
    def _save_settings():
        try:
            settings.save()
        except OSError as e:
            print(f"[Settings] couldn't save: {e}")

    def new_chat(self):
        self.pause()
        chat = store.new_chat()
        # A new chat starts with an empty room, but keeps the chat settings
        # (mode, pace, room prompt, spend cap) from the one you were in
        if self.chat:
            chat["settings"] = dict(self.chat["settings"])
        self.flush()
        self._set_current(chat)
        self._save()
        self.emit_snapshot()

    def open_chat(self, chat_id):
        chat = self._load(chat_id)
        if not chat:
            return False
        self.pause()
        self.flush()
        self._set_current(chat)
        self.emit_snapshot()
        return True

    def _delete_file(self, chat_id):
        """Deletes go through the writer thread too, so a save still queued
        for this chat can't bring the file back afterwards."""
        if self._dirty is not None and self._dirty["id"] == chat_id:
            self._dirty = None
            if self._save_handle:
                self._save_handle.cancel()
                self._save_handle = None
        self._writer.submit(store.delete, chat_id).result()

    def delete_chat(self, chat_id):
        if self.chat and self.chat["id"] == chat_id:
            self.pause()
            self._delete_file(chat_id)
            remaining = store.list_chats()
            if remaining:
                self._set_current(self._load(remaining[0]["id"]))
            else:
                self.chat = None
                self.new_chat()
                return
        else:
            self._delete_file(chat_id)
        self.emit_snapshot()

    def rename_chat(self, chat_id, title):
        """Rename any chat, open or not."""
        title = (title or "").strip()[:80] or "untitled"
        if self.chat and self.chat["id"] == chat_id:
            return self.update_chat(title=title)
        chat = self._load(chat_id)
        if not chat:
            return
        chat["title"], chat["titled"] = title, True
        self._writer.submit(self._write_quietly, chat_id, store.serialize(chat)).result()
        self.emit_snapshot()

    def update_chat(self, title=None, chat_settings=None):
        if title is not None:
            self.chat["title"] = title.strip()[:80] or "untitled"
            self.chat["titled"] = True  # you named it: don't auto-title over it
        if chat_settings:
            for key, value in chat_settings.items():
                if key in store.DEFAULT_CHAT_SETTINGS:
                    self.chat["settings"][key] = value
        self._save()
        self.emit_snapshot()

    # ─── members ────────────────────────────────────────────────────────

    def member(self, member_id):
        return next((m for m in self.chat["members"] if m["id"] == member_id), None)

    def add_member(self, model, name, temperature=1.0, illustrator=None, draw_every=30):
        name = (name or model.split("/")[-1]).strip()[:40]
        if illustrator is None:
            illustrator = llm.is_image_model(model)
        member = store.new_member(self.chat, model, name, temperature, illustrator, draw_every)
        self.chat["members"].append(member)
        self._notice(f"{name} joined the chat")
        self._save()
        self.emit_snapshot()
        return member

    def update_member(self, member_id, values):
        member = self.member(member_id)
        if not member:
            return
        for key in ("name", "temperature", "muted", "model", "color", "illustrator", "draw_every"):
            if key in values:
                member[key] = values[key]
        self._save()
        self.emit_snapshot()

    def remove_all_members(self):
        if not self.chat or not self.chat["members"]:
            return
        self.pause()
        self.chat["members"] = []
        self._notice("everyone left the chat")
        self._save()
        self.emit_snapshot()

    def remove_member(self, member_id):
        member = self.member(member_id)
        if not member:
            return
        self.chat["members"].remove(member)
        self._notice(f"{member['name']} left the chat")
        self._save()
        self.emit_snapshot()

    # ─── messages ───────────────────────────────────────────────────────

    def _new_message(self, kind, author, text="", **extra):
        msg = {"id": store.new_id(), "ts": _ts(), "kind": kind, "author": author,
               "text": text, "status": "done", "reactions": {}}
        if author == "human":
            msg.update(name=settings.get("username") or "you")
        elif author != "system":
            m = self.member(author)
            if m:
                msg.update(name=m["name"], color=m["color"], model=m["model"])
        msg.update(extra)
        self.chat["messages"].append(msg)
        return msg

    def _notice(self, text, private=False):
        msg = self._new_message("notice", "system", text, private=private)
        self._emit_message(msg)
        return msg

    def _remove_message(self, msg):
        if msg in self.chat["messages"]:
            self.chat["messages"].remove(msg)
            self.emit({"type": "remove", "id": msg["id"]})

    def _human(self):
        return {"id": "human", "name": settings.get("username") or "you", "model": None}

    async def human_message(self, text, image=None):
        """You said something: text, an uploaded image, and/or commands."""
        cleaned, cmds = commands.parse((text or "").strip())
        if image:
            msg = self._new_message("image", "human", cleaned, image=image)
            self._emit_message(msg)
        elif cleaned:
            self._emit_message(self._new_message("text", "human", cleaned))
        for cmd in cmds:
            await self._run_command(self._human(), cmd)
        if not (image or cleaned or cmds):
            return
        self.passed.clear()
        self._save()
        if not self.running and self.chat["members"]:
            self.step()  # somebody answers when you speak

    @staticmethod
    def _decode_image(data_url, max_bytes=10 * 1024 * 1024):
        """data:image/...;base64,... -> (extension, bytes). Images only."""
        header, _, b64 = (data_url or "").partition(",")
        mime = header[5:].split(";")[0] if header.startswith("data:") else ""
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "image/gif": ".gif"}.get(mime)
        if not ext:
            raise ValueError("that isn't a PNG, JPEG, WebP or GIF image")
        raw = base64.b64decode(b64, validate=False)
        if len(raw) > max_bytes:
            raise ValueError(f"images must be under {max_bytes // (1024 * 1024)} MB")
        return ext, raw

    def set_avatar(self, model, data_url):
        """Give a model a profile picture, used in every chat it's in."""
        ext, raw = self._decode_image(data_url, max_bytes=3 * 1024 * 1024)
        os.makedirs(settings.AVATARS_DIR, exist_ok=True)
        # A new name each time, so browsers don't keep showing the old picture
        name = f"{re.sub(r'[^a-z0-9._-]+', '_', model.lower())}_{int(time.time())}{ext}"
        with open(os.path.join(settings.AVATARS_DIR, name), "wb") as f:
            f.write(raw)
        self.remove_avatar(model, announce=False)
        avatars = dict(settings.get("avatars") or {})
        avatars[model] = name
        settings.update({"avatars": avatars})
        self.emit_snapshot()
        return name

    def remove_avatar(self, model, announce=True):
        avatars = dict(settings.get("avatars") or {})
        old = avatars.pop(model, None)
        if old and os.path.basename(old) == old:  # only ever a file in AVATARS_DIR
            try:
                os.remove(os.path.join(settings.AVATARS_DIR, old))
            except OSError:
                pass
            settings.update({"avatars": avatars})
        if announce:
            self.emit_snapshot()

    def save_upload(self, data_url):
        """Store an image you uploaded. Returns its filename in MEDIA_DIR."""
        ext, raw = self._decode_image(data_url)
        os.makedirs(settings.MEDIA_DIR, exist_ok=True)
        name = f"upload_{time.strftime('%Y%m%d_%H%M%S')}_{store.new_id()[:6]}{ext}"
        with open(os.path.join(settings.MEDIA_DIR, name), "wb") as f:
            f.write(raw)
        return name

    # ─── playback ───────────────────────────────────────────────────────
    #
    # There is only ever one playback loop. Play and Step set how many more
    # messages it should post; Pause asks it to stop at the next checkpoint.
    # Replies already being written when you pause still finish (they're paid
    # for), and pressing Play again just lets the same loop carry on.

    def play(self):
        if self.chat:
            self._go(self.chat["settings"].get("messages_per_play") or 0)

    def step(self):
        if self.chat and not self.running:
            self._go(1)

    def pause(self):
        if self.running:
            self.running = False
            if self.chat:
                self._emit_status()

    def _go(self, limit):
        self.loop = asyncio.get_running_loop()
        self.limit, self.posted = limit, 0
        self.running = True
        self._emit_status()
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        chat = self.chat
        try:
            await self._play(chat)
        except Exception as e:  # keep the server alive whatever happens
            self._notice(f"something broke: {e}", private=True)
        finally:
            if self.chat is chat:
                if self.running and self.limit and self.posted >= self.limit:
                    self.running = False
                self._form_memories(final=not self.running)
                self._save()
            if self.running and self.chat is not None and self.chat is not chat:
                self.running = False
            self._emit_status()
            # Play was pressed again while this loop was winding down
            if self.running:
                self.task = asyncio.create_task(self._run())

    async def _play(self, chat):
        quiet, force = 0, False
        while self.running and self.chat is chat:
            cap = chat["settings"].get("spend_cap") or 0
            if cap and chat["cost"] >= cap:
                self._notice(f"paused: this chat reached its ${cap:.2f} spend cap "
                             f"(change it in chat settings)", private=True)
                break
            if not await self._wait_for_images(chat):
                break
            eligible = [m for m in chat["members"] if not m["muted"]]
            if not eligible:
                self._notice("add someone to the chat first", private=True)
                break
            speakers, allow_pass = self._choose(eligible, force)
            results = await asyncio.gather(*(self._speak(m, allow_pass) for m in speakers),
                                           return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    self._notice(f"a reply failed: {r}", private=True)
            spoke = sum(1 for r in results if r is True)
            self.posted += spoke
            if spoke:
                quiet, force = 0, False
                self.since_memory += spoke
                if self.since_memory >= MEMORY_EVERY:
                    self._form_memories(final=False)
                self._maybe_title(chat)
            else:
                quiet += 1
                force = True
                if quiet >= QUIET_ROUNDS:
                    self._notice("the chat went quiet", private=True)
                    break
            if self.limit and self.posted >= self.limit:
                break
            if spoke and self.running:
                pace = float(chat["settings"].get("pace") or 0)
                await asyncio.sleep(max(0.0, pace * random.uniform(0.6, 1.4)))
        self.running = False

    async def _wait_for_images(self, chat):
        """Hold the next reply until posted images have rendered and searches
        have returned, so whoever speaks next can see them. Returns False if
        paused meanwhile."""
        deadline = time.monotonic() + IMAGE_WAIT
        announced = False
        while any(not t.done() for t in self.tool_tasks) and time.monotonic() < deadline:
            if not self.running or self.chat is not chat:
                return False
            if not announced:
                self.emit({"type": "waiting", "what": "image", "on": True})
                announced = True
            await asyncio.sleep(0.25)
        if announced:
            self.emit({"type": "waiting", "what": "image", "on": False})
        return self.running and self.chat is chat

    # ─── who speaks ─────────────────────────────────────────────────────

    def _public(self):
        return [m for m in self.chat["messages"] if m["kind"] in ("text", "image", "poll")]

    def _mentions(self, text, member):
        text = text.lower()
        names = {member["name"].lower()}
        first = member["name"].split()[0].lower()
        if len(first) >= 4:
            names.add(first)
        return any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", text) for n in names)

    def _weights(self, eligible):
        talk = self._public()
        last = talk[-1] if talk else None
        weights = {}
        for m in eligible:
            since = next((i for i, msg in enumerate(reversed(talk)) if msg["author"] == m["id"]),
                         len(talk))
            w = 1 + 0.35 * min(since, 6)
            if last and last["author"] == m["id"]:
                w *= 0.12          # double-texting happens, rarely
            elif len(talk) > 1 and talk[-2]["author"] == m["id"]:
                w *= 0.6
            if last and last["author"] != m["id"] and self._mentions(last.get("text", ""), m):
                w *= 5             # you got @'d
            weights[m["id"]] = w
        return weights

    def _due_illustrators(self, illustrators):
        """Illustrators draw every ~draw_every messages, or straight away when @'d."""
        talk = self._public()
        last = talk[-1] if talk else None
        due = []
        for m in illustrators:
            since = next((i for i, msg in enumerate(reversed(talk)) if msg["author"] == m["id"]),
                         len(talk))
            asked = last and last["author"] != m["id"] and self._mentions(last.get("text", ""), m)
            if asked or since >= max(1, int(m.get("draw_every") or 30)):
                due.append(m)
        return due

    def _choose(self, eligible, force):
        """Pick who replies next. Returns (members, whether they may pass).

        Talkers take turns by the chat's mode. Illustrators aren't in that
        rotation: any that are due draw alongside whoever talks.
        """
        talkers = [m for m in eligible if not m.get("illustrator")]
        drawing = self._due_illustrators([m for m in eligible if m.get("illustrator")])
        if not talkers:
            return drawing or [m for m in eligible if m.get("illustrator")][:1], False
        picked, allow_pass = self._choose_talkers(talkers, force)
        return picked + drawing, allow_pass

    def _choose_talkers(self, eligible, force):
        mode = self.chat["settings"].get("mode", "natural")
        talk = self._public()
        last_author = talk[-1]["author"] if talk else None

        if mode == "round_robin":
            ids = [m["id"] for m in eligible]
            last_ai = next((msg["author"] for msg in reversed(talk) if msg["author"] in ids), None)
            nxt = (ids.index(last_ai) + 1) % len(ids) if last_ai else 0
            return [eligible[nxt]], False

        weights = self._weights(eligible)
        if mode == "everyone" and not force:
            return [m for m in eligible if m["id"] != last_author] or eligible, True

        candidates = [m for m in eligible if m["id"] not in self.passed] or eligible
        first = self._weighted_pick(candidates, weights)
        picked = [first]
        overlap = float(self.chat["settings"].get("overlap") or 0)
        if not force and len(candidates) >= 3 and random.random() < overlap:
            rest = [m for m in candidates if m is not first and m["id"] != last_author]
            if rest:
                picked.append(self._weighted_pick(rest, weights))
        return picked, not force

    @staticmethod
    def _weighted_pick(members, weights):
        return random.choices(members, [weights[m["id"]] for m in members])[0]

    # ─── one AI reply ───────────────────────────────────────────────────

    def _memory_on(self):
        return bool(settings.get("memory_enabled"))

    def _to_memory_format(self, messages):
        """Messages in the shape identity_memory (from the backrooms) expects."""
        conv = []
        for msg in messages:
            if msg["kind"] == "notice" and (msg.get("private") or msg["author"] != "system"):
                continue
            if msg.get("status") in ("streaming", "error") or msg["kind"] == "notice" and not msg.get("text"):
                continue
            if msg["kind"] == "poll":
                text = f"[started a poll: {msg.get('text', '')} - options: " + " / ".join(
                    o["text"] for o in msg.get("options", [])) + "]"
            elif msg["kind"] == "image":
                if msg.get("prompt"):
                    text = f"[posted an image: {msg['prompt']}]"
                elif msg.get("illustration"):
                    text = "[drew a picture of the chat]" + (f" {msg['text']}" if msg.get("text") else "")
                else:  # an image you uploaded, maybe with a caption
                    text = "[posted an image]" + (f" {msg['text']}" if msg.get("text") else "")
            else:
                text = msg.get("text", "")
            if msg["author"] == "human":
                d = {"role": "user", "_user_name": msg.get("name", "human"), "content": text}
            elif msg["author"] == "system":
                d = {"role": "user", "_user_name": "system", "content": text}
            else:
                d = {"role": "assistant", "ai_name": msg["author"],
                     "model": msg.get("name", "someone"), "content": text}
            if msg["kind"] == "whisper":
                d.update(_type="whisper", _whisper_to=msg.get("to", ""))
            if msg["kind"] == "notice":
                d["_type"] = "notice"
            d["_msg"] = msg
            conv.append(d)
        return conv

    def _image_url(self, filename):
        """A data URL for an image, sized for models (cached: made once)."""
        if filename not in self._image_data:
            path = images.source_path(filename)
            if not path:
                return None
            try:
                raw, mime = images.for_model(path)
            except OSError:
                return None
            self._image_data[filename] = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        return self._image_data[filename]

    @staticmethod
    def _merge(a, b):
        """Join two message contents, either of which may be a list of parts."""
        if isinstance(a, str) and isinstance(b, str):
            return a + "\n\n" + b
        parts = lambda c: c if isinstance(c, list) else [{"type": "text", "text": c}]
        return parts(a) + parts(b)

    def _render(self, member, live, images=True):
        """Turn memory-format messages into API messages from member's seat.

        With `images`, the latest few images others posted go in as pictures,
        not just their prompts.
        """
        names = {m["id"]: m["name"] for m in self.chat["members"]}
        names["human"] = settings.get("username") or "human"
        shown = set()
        if images:
            recent = [d["_msg"]["id"] for d in live
                      if (d.get("_msg") or {}).get("kind") == "image"
                      and d["_msg"].get("status") == "done" and d["_msg"].get("image")
                      and d["_msg"].get("author") != member["id"]]
            shown = set(recent[-CONTEXT_IMAGES:])
        out = []
        time_aware = bool(settings.get("time_awareness"))
        prev_ts = None
        for d in live:
            msg = d.get("_msg") or {}
            text = d["content"]
            # Commands show in the history the way they were written, like in
            # the backrooms: seeing each other use !image is what makes the
            # models keep using it.
            if msg.get("kind") == "image" and msg.get("prompt"):
                text = f'!image "{msg["prompt"]}"'
            if msg.get("kind") == "poll":
                text = self._poll_text(msg)
            gap = self._time_gap(prev_ts, msg.get("ts")) if time_aware else ""
            if msg.get("ts"):
                prev_ts = msg["ts"]
            if msg.get("kind") == "whisper":
                if msg.get("to") == member["id"]:
                    role, text = "user", f"[{msg.get('name', '?')} → you, privately]: {text}"
                elif msg.get("author") == member["id"]:
                    role, text = "assistant", f'!whisper "{names.get(msg.get("to"), "?")}" "{text}"'
                else:
                    continue
            elif d.get("ai_name") == member["id"] and d["role"] == "assistant":
                role = "assistant"
            elif msg.get("author") == "system":
                role, text = "user", f"[system]: {text}"
            else:
                speaker = d.get("model") or d.get("_user_name") or "someone"
                role, text = "user", f"[{speaker}]: {text}"
            if gap:
                if role == "user":
                    text = f"{gap}\n{text}"
                elif out and out[-1]["role"] == "user":
                    out[-1]["content"] = self._merge(out[-1]["content"], gap)
                else:
                    out.append({"role": "user", "content": gap})
            reactions = msg.get("reactions") or {}
            if reactions:
                text += "  (reactions: " + ", ".join(
                    f"{emoji} {' & '.join(who)}" for emoji, who in reactions.items()) + ")"
            content = text
            url = self._image_url(msg["image"]) if msg.get("id") in shown else None
            if url:
                content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}]
            if out and out[-1]["role"] == role:
                out[-1]["content"] = self._merge(out[-1]["content"], content)
            else:
                out.append({"role": role, "content": content})
        return out

    def _build_messages(self, member, allow_pass, images=True):
        others = [m for m in self.chat["members"] if m["id"] != member["id"]]
        system = build_system_prompt(member, others, settings.get("username"),
                                     self.chat["settings"].get("room_prompt"),
                                     self._memory_on(), allow_pass)
        conv = self._to_memory_format(self.chat["messages"])
        prefix = []
        if self._memory_on():
            try:
                prefix, conv = self.memory.context_for_turn(member["id"], member["model"], conv)
            except Exception as e:
                print(f"[Memory] skipped for {member['name']}: {e}")
        else:
            kept, used = [], 0
            for d in reversed(conv):
                used += _tokens(d["content"])
                if used > UNREMEMBERED_TOKENS and kept:
                    break
                kept.append(d)
            conv = list(reversed(kept))
        messages = [{"role": "system", "content": system}] + prefix + self._render(member, conv, images)
        if messages[-1]["role"] != "user":
            nudge = "(the chat's quiet. say something" + (", or pass)" if allow_pass else ")")
            messages.append({"role": "user", "content": nudge})
        if settings.get("time_awareness"):
            # At the end, not in the system prompt: a clock up top would change
            # every minute and break prompt caching for the whole context
            messages[-1]["content"] = self._merge(messages[-1]["content"], f"[now: {self._now_label()}]")
        return messages

    @staticmethod
    def _now_label(when=None):
        t = datetime.fromtimestamp(when) if when else datetime.now()
        hour = t.strftime("%I").lstrip("0") or "12"
        return f"{t.strftime('%A')} {t.day} {t.strftime('%B %Y')}, {hour}:{t.strftime('%M')}{t.strftime('%p').lower()}"

    @staticmethod
    def _time_gap(prev_ts, ts, minimum=30 * 60):
        """A marker when real time passed between messages: "[— 3 hours later —]",
        or the new date when the day changed."""
        if not prev_ts or not ts or ts - prev_ts < minimum:
            return ""
        a, b = datetime.fromtimestamp(prev_ts), datetime.fromtimestamp(ts)
        if a.date() != b.date():
            return f"[— {b.strftime('%A')} {b.day} {b.strftime('%B')} —]"
        minutes = int((ts - prev_ts) // 60)
        span = f"{minutes // 60} hour{'s' if minutes >= 120 else ''}" if minutes >= 60 else f"{minutes} minutes"
        return f"[— {span} later —]"

    @staticmethod
    def _poll_text(msg):
        options = msg.get("options", [])
        tallies = "; ".join(f"{o['text']}: {', '.join(o['votes']) or 'nobody'}" for o in options)
        quoted = " ".join(f'"{o["text"]}"' for o in options)
        return f'!poll "{msg.get("text", "")}" {quoted}  (votes so far: {tallies})'

    def _set_typing(self, member_id, on):
        (self.typing.add if on else self.typing.discard)(member_id)
        self.emit({"type": "typing", "member": member_id, "on": on})

    async def _draw(self, member):
        """An illustrator's turn: read the recent chat and draw one picture."""
        chat = self.chat
        lines, used = [], 0
        for msg in reversed(self._public()):
            if msg["kind"] == "image":
                line = f"[{msg.get('name', '?')} posted an image" + (
                    f": {msg.get('prompt') or msg.get('text')}]" if (msg.get("prompt") or msg.get("text")) else "]")
            else:
                line = f"[{msg.get('name', '?')}]: {msg.get('text', '')}"
            used += len(line)
            if used > 6000 or len(lines) >= 30:
                break
            lines.append(line)
        prompt = ILLUSTRATOR.format(name=member["name"],
                                    room=(chat["settings"].get("room_prompt") or "").strip(),
                                    transcript="\n".join(reversed(lines)) or "(nothing yet)")
        msg = self._new_message("image", member["id"], "", status="pending", illustration=True)
        self._emit_message(msg)
        self._set_typing(member["id"], True)
        try:
            name, cost, caption = await asyncio.wait_for(
                llm.generate_image(prompt, model=member["model"]), REPLY_TIMEOUT)
            msg.update(image=name, text=caption[:500], status="done")
            if self.chat is chat:
                self._add_cost(cost)
            ok = True
        except Exception as e:
            reason = "took too long" if isinstance(e, asyncio.TimeoutError) else e
            msg.update(status="error", text=f"couldn't draw: {reason}")
            ok = False
        finally:
            self._set_typing(member["id"], False)
        if self.chat is chat:
            self._emit_message(msg)
            self._save()
        return ok

    async def _speak(self, member, allow_pass):
        """Let one member reply. Returns True if something visible was posted."""
        if member.get("illustrator"):
            return await self._draw(member)
        chat = self.chat
        names = [member["name"]]
        images = member["model"] not in self.no_vision and await llm.supports_images(member["model"]) is not False
        messages = self._build_messages(member, allow_pass, images)
        has_images = any(isinstance(m["content"], list) for m in messages)
        msg = None
        self._set_typing(member["id"], True)

        async def on_delta(text):
            nonlocal msg
            if self.chat is not chat:
                return
            shown = commands.strip_name_prefix(text, names)
            if msg is None:
                if commands.could_be_pass(shown):
                    return  # hold back until we know it isn't a pass
                msg = self._new_message("text", member["id"], shown, status="streaming")
                self._emit_message(msg)
            else:
                msg["text"] = shown
                self.emit({"type": "delta", "id": msg["id"], "text": shown})

        async def call(messages):
            return await asyncio.wait_for(
                llm.stream_chat(member["model"], messages,
                                temperature=float(member.get("temperature", 1.0)),
                                on_delta=on_delta),
                REPLY_TIMEOUT)

        try:
            try:
                text, cost = await call(messages)
            except llm.LLMError as e:
                if not has_images or msg is not None or not re.search(
                        r"image|vision|modalit", str(e), re.IGNORECASE):
                    raise
                # Some models won't take images: remember that, resend as text
                print(f"[Images] {member['model']} refused images ({e}); retrying without")
                self.no_vision.add(member["model"])
                text, cost = await call(self._build_messages(member, allow_pass, images=False))
        except Exception as e:
            if msg:
                self._remove_message(msg)
            reason = "took too long" if isinstance(e, asyncio.TimeoutError) else e
            self._notice(f"{member['name']} couldn't reply: {reason}", private=True)
            return False
        finally:
            self._set_typing(member["id"], False)

        if self.chat is not chat:
            return False
        self._add_cost(cost)
        text = commands.strip_name_prefix(text, names).strip()
        if commands.is_pass(text) or not text:
            if msg:
                self._remove_message(msg)
            self.passed.add(member["id"])
            self.emit({"type": "pass", "member": member["id"]})
            return False

        cleaned, cmds = commands.parse(text)
        posted = False
        if cleaned:
            if msg is None:
                msg = self._new_message("text", member["id"], cleaned)
            msg.update(text=cleaned, status="done", cost=round(cost, 6))
            self._emit_message(msg)
            posted = True
        elif msg:
            self._remove_message(msg)
        for cmd in cmds:
            posted = await self._run_command(member, cmd) or posted
        if posted:
            self.passed.clear()
        self._save()
        return posted

    def _add_cost(self, cost):
        if self.chat and cost:
            self.chat["cost"] = self.chat.get("cost", 0.0) + cost
            self._emit_status()

    # ─── commands ───────────────────────────────────────────────────────

    def _find_member(self, name):
        if not name:
            return None
        name = name.strip().lstrip("@").lower()
        members = self.chat["members"]
        for test in (lambda m: m["name"].lower() == name,
                     lambda m: m["name"].lower().startswith(name),
                     lambda m: name in m["name"].lower() or name in m["model"].lower()):
            found = [m for m in members if test(m)]
            if found:
                return found[0]
        return None

    async def _run_command(self, member, cmd):
        """Run one command. Returns True if it put something visible in the chat."""
        a = cmd.args
        if cmd.action == "image" and a.get("text"):
            msg = self._new_message("image", member["id"], "", prompt=a["text"], status="pending")
            self._emit_message(msg)
            task = asyncio.create_task(self._make_image(msg))
            self.tool_tasks.add(task)
            task.add_done_callback(self.tool_tasks.discard)
            return True
        if cmd.action == "poll" and a.get("question"):
            if len(a.get("options") or []) < 2:
                self._notice(f"{member['name']}'s poll needs at least two options", private=True)
                return False
            msg = self._new_message("poll", member["id"], a["question"][:200], status="open",
                                    options=[{"text": o[:80], "votes": []} for o in a["options"]])
            self._emit_message(msg)
            return True
        if cmd.action == "vote" and a.get("choice"):
            return self._vote(member["name"], a["choice"])
        if cmd.action == "react" and a.get("emoji"):
            return self._react(member, a["emoji"], a.get("to"))
        if cmd.action == "whisper" and a.get("text"):
            target = self._find_member(a.get("to"))
            username = settings.get("username") or "human"
            if target and target["id"] == member["id"]:
                return False  # whispering to yourself is just thinking
            if target:
                to = target["id"]
            elif a.get("to", "").lower().lstrip("@") == username.lower():
                to = "human"
            else:
                self._notice(f"{member['name']} tried to whisper to \"{a.get('to')}\", "
                             f"who isn't here", private=True)
                return False
            if to == member["id"]:
                return False
            msg = self._new_message("whisper", member["id"], a["text"], to=to)
            self._emit_message(msg)
            return True
        if cmd.action in ("search", "bsky") and a.get("text"):
            lookup = self._search if cmd.action == "search" else self._bluesky
            task = asyncio.create_task(lookup(member, a["text"]))
            self.tool_tasks.add(task)
            task.add_done_callback(self.tool_tasks.discard)
            return False
        if cmd.action in ("remember", "forget"):
            if not member.get("model"):
                self._notice(f"!{cmd.action} is for the AIs - they keep their own memories",
                             private=True)
                return False
            if not self._memory_on():
                self._notice(f"{member['name']} tried to !{cmd.action}, but memory is off",
                             private=True)
                return False
            try:
                return await self._memory_command(member, cmd.action, a["text"])
            except MemoryFileError as e:
                self._notice(f"🧠 {member['name']}'s !{cmd.action} wasn't saved: {e}", private=True)
                return False
        return False

    async def _memory_command(self, member, action, text):
        if action == "remember":
            faded = await asyncio.to_thread(self.memory.remember, member["model"], member["id"], text)
            note = f" ({faded})" if faded else ""
            self._notice(f"🧠 {member['name']} will remember: {text}{note}", private=True)
        else:
            gone = await asyncio.to_thread(self.memory.forget, member["model"], text)
            self._notice(f"🫥 {member['name']} forgot: {gone}" if gone else
                         f"{member['name']} tried to forget \"{text}\" but had no such memory",
                         private=True)
        return False

    def _vote(self, voter, choice, poll_id=None):
        """Vote in a poll (the latest open one unless poll_id). One vote each:
        voting again moves it. Returns False - a vote isn't a message."""
        polls = [m for m in self.chat["messages"] if m["kind"] == "poll" and m.get("status") == "open"]
        poll = next((p for p in reversed(polls) if poll_id in (None, p["id"])), None)
        if not poll:
            if voter != (settings.get("username") or "you"):
                self._notice(f"{voter} tried to vote, but there's no open poll", private=True)
            return False
        options = poll["options"]
        choice = str(choice).strip()
        pick = None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            pick = options[int(choice) - 1]
        else:
            low = choice.lower()
            pick = (next((o for o in options if o["text"].lower() == low), None)
                    or next((o for o in options if o["text"].lower().startswith(low)), None)
                    or next((o for o in options if low in o["text"].lower()), None))
        if not pick:
            self._notice(f"{voter} voted \"{choice}\", which isn't an option", private=True)
            return False
        for o in options:
            if voter in o["votes"]:
                o["votes"].remove(voter)
        pick["votes"].append(voter)
        self._emit_message(poll)
        self._save()
        return False

    def human_vote(self, poll_id, index):
        """You clicked an option on a poll card."""
        self._vote(settings.get("username") or "you", str(int(index) + 1), poll_id)

    def _react(self, member, emoji, to_name):
        target_member = self._find_member(to_name) if to_name else None
        for msg in reversed(self._public()):
            if msg["author"] == member["id"]:
                continue
            if target_member and msg["author"] != target_member["id"]:
                continue
            who = msg.setdefault("reactions", {}).setdefault(emoji, [])
            if member["name"] not in who:
                who.append(member["name"])
            self._emit_message(msg)
            return False  # a reaction isn't a message
        return False

    async def _make_image(self, msg):
        chat = self.chat
        try:
            name, cost, _ = await llm.generate_image(msg["prompt"])
            msg.update(image=name, status="done")
            if self.chat is chat:
                self._add_cost(cost)
        except Exception as e:
            msg.update(status="error", text=f"image failed: {e}")
        if self.chat is chat:
            self._emit_message(msg)
            self._save()
        else:  # you switched chats while it was drawing: save that chat quietly
            text = store.serialize(chat)
            self._writer.submit(self._write_quietly, chat["id"], text)

    def _maybe_title(self, chat):
        """Once an untitled chat gets going, one of its members names it."""
        if chat.get("titled") or chat["title"] not in ("new chat", "", "untitled"):
            return
        talk = [m for m in chat["messages"] if m["kind"] in ("text", "image", "poll")]
        talkers = [m for m in chat["members"] if not m.get("illustrator")]
        if len(talk) < TITLE_AFTER or not talkers:
            return
        chat["titled"] = True  # one try per chat
        asyncio.create_task(self._auto_title(chat, random.choice(talkers), talk[-30:]))

    async def _auto_title(self, chat, member, talk):
        transcript = "\n".join(
            f"[{m.get('name', '?')}]: " + (m.get("text") or (f"(image: {m['prompt']})" if m.get("prompt") else "(image)"))
            for m in talk)
        prompt = [{"role": "user", "content": TITLE.format(transcript=transcript[-6000:])}]
        record = lambda cost: self._call_on_loop(self._add_cost, cost)
        try:
            title = await asyncio.to_thread(llm.complete_sync, member["model"], prompt, 400, record)
        except llm.LLMError as e:
            print(f"[Title] {member['name']} couldn't name the chat: {e}")
            return
        title = title.strip().splitlines()[0].strip(' "\'*#.').strip()[:60]
        if not title or chat["title"] not in ("new chat", "", "untitled"):
            return  # you renamed it in the meantime
        chat["title"] = title
        if self.chat is chat:
            self._save()
        else:
            self._writer.submit(self._write_quietly, chat["id"], store.serialize(chat))
        self.emit_snapshot()

    async def _search(self, member, query):
        from .search import web_search
        results = await asyncio.to_thread(web_search, query)
        text = f"🔎 {member['name']} searched \"{query}\"\n{results}"
        self._notice(text)
        self._save()

    async def _bluesky(self, member, query):
        from .bluesky import bluesky
        results = await asyncio.to_thread(bluesky, query)
        what = f"read {query}'s posts" if query.startswith("@") else f"searched \"{query}\""
        self._notice(f"🦋 {member['name']} {what} on Bluesky\n{results}")
        self._save()

    # ─── memory ─────────────────────────────────────────────────────────

    def _memory_complete(self, model_id, messages):
        """Model call used to form memories (runs on a background thread).

        If the model's provider refuses memory requests, stop asking it for
        the rest of the session and let the fallback model write its
        memories instead, in its voice. Returns (text, who wrote it).
        """
        def record(cost):
            if self.loop:
                self.loop.call_soon_threadsafe(self._add_cost, cost)

        def report(reason):
            print(f"[Memory] {model_id}: {reason}")
            if self.loop:
                self.loop.call_soon_threadsafe(self._memory_problem, model_id, reason)

        if model_id not in self._memory_refused:
            try:
                return llm.complete_sync(model_id, messages, on_cost=record), model_id
            except llm.RefusalError as e:
                self._memory_refused.add(model_id)
                report(f"couldn't form a memory: {e}")
            except llm.LLMError as e:
                report(f"couldn't form a memory: {e}")
                return None

        fallback = (settings.get("memory_fallback_model") or "").strip()
        if not fallback or fallback == model_id:
            return None
        name = self._name_for(model_id)
        ghost = [dict(m) for m in messages]
        if ghost and ghost[0]["role"] == "system":
            ghost[0]["content"] = (f"(You're writing this memory on behalf of {name}, in their "
                                   f"voice, because they can't write it themselves.)\n\n"
                                   + ghost[0]["content"])
        try:
            text = llm.complete_sync(fallback, ghost, on_cost=record)
        except llm.LLMError as e:
            report(f"the fallback {fallback} couldn't write its memory either: {e}")
            return None
        if self.loop:
            self.loop.call_soon_threadsafe(self._memory_problem, model_id,
                                           f"is refused memory requests, so {fallback} is writing "
                                           f"its memories for it")
        return text, fallback

    def _name_for(self, model_id):
        members = self.chat["members"] if self.chat else []
        return next((m["name"] for m in members if m["model"] == model_id), model_id)

    def _memory_problem(self, model_id, reason):
        """Tell you, once per model and kind of problem, that memories aren't forming."""
        key = (model_id, reason[:60])
        if key in self._memory_problems or not self.chat:
            return
        self._memory_problems.add(key)
        self._notice(f"🧠 {self._name_for(model_id)} {reason}", private=True)

    def _form_memories(self, final):
        self.since_memory = 0
        if not self._memory_on() or not self.chat:
            return
        conv = self._to_memory_format(self.chat["messages"])
        slots = [(m["id"], m["model"]) for m in self.chat["members"] if not m.get("illustrator")]
        self.memory.after_round(conv, slots, final)

    def memories(self, model):
        data = self.memory.store(model, self.memory.scope).load()
        return [m for m in data["memories"] if not m.get("merged_into")]

    def forget_memory(self, model, memory_id):
        store_ = self.memory.store(model, self.memory.scope)
        from .identity_memory import _io_lock
        with _io_lock:
            data = store_.load(strict=True)
            for m in data["memories"]:
                if m.get("id") == memory_id:
                    m["forgotten"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            store_.save(data)
