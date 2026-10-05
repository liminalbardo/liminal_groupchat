"""Engine tests with a scripted model. Run: python -m pytest tests  (or python tests/test_engine.py)"""

import asyncio
import json
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ["GROUPCHAT_DATA_DIR"] = TMP

from gchat import commands, engine as engine_mod, llm, settings, store  # noqa: E402


REAL_STREAM_CHAT = llm.stream_chat


class Script:
    """Stands in for llm.stream_chat: replies come from a per-model queue."""

    def __init__(self, replies=None, default="hi"):
        self.replies = replies or {}
        self.default = default
        self.calls = []
        self.web = []          # the web flag of each call
        self.found = {}        # model -> sources a web reply cites
        self.no_tools = set()  # models that refuse web tools

    async def __call__(self, model, messages, temperature=1.0, max_tokens=4000, on_delta=None,
                       web=False, sources=None, web_uses=None):
        if web and model in self.no_tools:
            raise llm.LLMError("No endpoints found that support tool use")
        self.calls.append((model, messages))
        self.web.append(web)
        if web and sources is not None:
            sources.extend(self.found.get(model, []))
        if web and web_uses is not None and model in self.found:
            web_uses["searches"] = 2
        queue = self.replies.get(model) or []
        text = queue.pop(0) if queue else self.default
        shown = ""
        for word in text.split(" "):
            shown = f"{shown} {word}" if shown else word
            if on_delta:
                await on_delta(shown)
        return text, 0.001


def fresh(script, memory=False):
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    settings._settings.clear()
    settings._settings.update(settings.DEFAULTS, memory_dir=os.path.join(TMP, "memory"), memory_enabled=memory)
    settings._sync_module_state()
    llm.stream_chat = script

    async def vision_unknown(model):
        return None
    llm.supports_images = vision_unknown
    eng = engine_mod.Engine()
    events = []
    eng.emit = events.append
    eng.start()
    return eng, events


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


async def drain(eng):
    while eng.running:
        await asyncio.sleep(0.01)


def texts(eng):
    return [(m.get("name"), m["text"]) for m in eng.chat["messages"] if m["kind"] == "text"]


def test_commands_parse():
    cleaned, cmds = commands.parse('lol !react "💀" and !image "a cat" !whisper "Kimi" "psst" ok')
    assert cleaned == "lol and ok"
    assert [c.action for c in cmds] == ["react", "image", "whisper"]
    assert cmds[2].args == {"to": "Kimi", "text": "psst"}
    _, cmds = commands.parse("!react 😂")
    assert cmds[0].args["emoji"] == "😂"
    assert commands.is_pass(" Pass. ") and not commands.is_pass("passing by")
    assert commands.could_be_pass("pa") and not commands.could_be_pass("passing")
    assert commands.strip_name_prefix("[Opus]: hey", ["Opus"]) == "hey"


def test_round_robin_takes_turns():
    async def go():
        eng, _ = fresh(Script())
        for model, name in (("a/one", "One"), ("b/two", "Two"), ("c/three", "Three")):
            eng.add_member(model, name)
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 6})
        eng.play()
        await drain(eng)
        return [n for n, _ in texts(eng)]
    assert run(go()) == ["One", "Two", "Three", "One", "Two", "Three"]


def test_pass_is_hidden_and_someone_else_speaks():
    async def go():
        script = Script({"a/one": ["pass"]}, default="yo")
        eng, events = fresh(script)
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        eng.update_chat(chat_settings={"mode": "natural", "pace": 0, "overlap": 0})
        random.seed(1)
        eng.passed.clear()
        # Force One to be picked first by making Two look like it just spoke
        await eng.human_message("hey One")  # mention -> One weighted up; human msg triggers a step
        await drain(eng)
        return eng, events, script
    eng, events, script = run(go())
    posted = texts(eng)
    assert all(t != "pass" for _, t in posted)
    assert ("Two", "yo") in posted or ("One", "yo") in posted
    if script.calls[0][0] == "a/one":  # One was asked first and passed
        assert any(e.get("type") == "pass" for e in events)
        assert posted[-1] == ("Two", "yo")


def test_mentions_raise_weight():
    eng, _ = fresh(Script())
    one = eng.add_member("a/one", "Opus")
    two = eng.add_member("b/two", "Kimi")
    msg = eng._new_message("text", "human", "kimi what do you think")
    w = eng._weights([one, two])
    assert w[two["id"]] > 3 * w[one["id"]]
    eng.chat["messages"].remove(msg)


def test_everyone_mode_asks_all_but_last_speaker():
    async def go():
        script = Script({"a/one": ["first"], "b/two": ["pass"], "c/three": ["third"]}, default="pass")
        eng, _ = fresh(script)
        for model, name in (("a/one", "One"), ("b/two", "Two"), ("c/three", "Three")):
            eng.add_member(model, name)
        eng.update_chat(chat_settings={"mode": "everyone", "pace": 0, "messages_per_play": 2})
        eng.play()
        await drain(eng)
        return eng, script
    eng, script = run(go())
    first_round = {m for m, _ in script.calls[:3]}
    assert first_round == {"a/one", "b/two", "c/three"}
    assert sorted(n for n, _ in texts(eng)) == ["One", "Three"]


def test_commands_react_whisper_and_context():
    async def go():
        script = Script({
            "a/one": ["that's wild !react \"💀\"", "!whisper \"One\" \"nope\""],
            "b/two": ['!whisper "One" "secret plan"', "ok"],
        })
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 3})
        await eng.human_message("hello all")
        await drain(eng)
        eng.play()
        await drain(eng)
        return eng, script
    eng, script = run(go())
    human = next(m for m in eng.chat["messages"] if m["author"] == "human")
    assert human["reactions"] == {"💀": ["One"]}
    whisper = next(m for m in eng.chat["messages"] if m["kind"] == "whisper")
    assert whisper["text"] == "secret plan" and whisper["to"] == eng.chat["members"][0]["id"]
    # One sees the whisper privately; nobody else would
    one_ctx = [c for c in script.calls if c[0] == "a/one"][-1][1]
    assert any("Two → you, privately]: secret plan" in m["content"] for m in one_ctx)
    # Others' messages carry name prefixes, own messages are assistant turns
    assert any(m["role"] == "user" and "[you]: hello all" in m["content"] for m in one_ctx)


def test_image_command_creates_image_message():
    async def go():
        eng, _ = fresh(Script({"a/one": ['!image "a cursed toaster"']}))
        eng.add_member("a/one", "One")

        async def fake_image(prompt):
            return "x.png", 0.01, ""
        llm.generate_image = fake_image
        eng.step()
        await drain(eng)
        await asyncio.sleep(0.05)
        return eng
    eng = run(go())
    img = next(m for m in eng.chat["messages"] if m["kind"] == "image")
    assert img["status"] == "done" and img["image"] == "x.png" and img["prompt"] == "a cursed toaster"
    assert eng.chat["cost"] > 0.01


def test_spend_cap_stops_autoplay():
    async def go():
        eng, _ = fresh(Script())
        eng.add_member("a/one", "One")
        eng.chat["cost"] = 5.0
        eng.update_chat(chat_settings={"spend_cap": 1.0, "pace": 0})
        eng.play()
        await drain(eng)
        return eng
    eng = run(go())
    assert not texts(eng)
    assert any("spend cap" in m["text"] for m in eng.chat["messages"] if m["kind"] == "notice")


def test_memory_notes_via_commands():
    async def go():
        eng, _ = fresh(Script({"a/one": ['!remember "Two owes me $5" lol']}), memory=True)
        eng.add_member("a/one", "One")
        eng.memory.complete_fn = lambda model, msgs: "I remember things."
        eng.step()
        await drain(eng)
        return eng
    eng = run(go())
    notes = [m for m in eng.memories("a/one") if m.get("kind") == "note"]
    assert notes and notes[0]["content"] == "Two owes me $5"
    assert os.path.exists(os.path.join(TMP, "memory", "group_chat", "a_one", "memories.json"))
    # The note is recalled in the next context
    llm.stream_chat.replies["a/one"] = ["cool"]

    async def again():
        eng.step()
        await drain(eng)
    run(again())
    ctx = llm.stream_chat.calls[-1][1]
    assert any("Two owes me $5" in m["content"] for m in ctx)


def test_chats_persist_and_new_chat_starts_empty():
    eng, _ = fresh(Script())
    eng.add_member("a/one", "One")
    eng.update_chat(chat_settings={"mode": "round_robin"})
    first = eng.chat["id"]
    eng.new_chat()
    assert eng.chat["id"] != first
    assert eng.chat["members"] == [], "a new chat is an empty room"
    assert eng.chat["settings"]["mode"] == "round_robin", "chat settings carry over"
    assert {c["id"] for c in store.list_chats()} >= {first, eng.chat["id"]}
    assert eng.open_chat(first) and eng.chat["id"] == first
    assert [m["name"] for m in eng.chat["members"]] == ["One"]


def test_next_speaker_waits_for_image_and_sees_it():
    async def go():
        script = Script({"a/one": ['!image "a cursed toaster"'], "b/two": ["nice toaster"]})
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 2})
        os.makedirs(settings.MEDIA_DIR, exist_ok=True)

        async def slow_image(prompt):
            await asyncio.sleep(0.3)
            with open(os.path.join(settings.MEDIA_DIR, "toaster.png"), "wb") as f:
                f.write(b"\x89PNG fake")
            return "toaster.png", 0.0, ""
        llm.generate_image = slow_image
        eng.play()
        await drain(eng)
        return script
    script = run(go())
    two_ctx = [c for c in script.calls if c[0] == "b/two"][0][1]
    parts = [p for m in two_ctx if isinstance(m["content"], list) for p in m["content"]]
    assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/png;base64,")
               for p in parts), "Two should see the finished image"
    assert any('[One]: !image "a cursed toaster"' in p.get("text", "") for p in parts), \
        "others' images read as the command they used, so models keep using it"
    # The author sees its own image as the command it wrote
    llm.stream_chat.replies["a/one"] = ["ok"]


def test_model_without_vision_gets_text_only_retry():
    async def go():
        class NoVision(Script):
            async def __call__(self, model, messages, **kw):
                if model == "b/two" and any(isinstance(m["content"], list) for m in messages):
                    self.calls.append((model, messages))
                    raise llm.LLMError("404: No endpoints found that support image input")
                return await super().__call__(model, messages, **kw)
        script = NoVision({"a/one": ['!image "a frog"'], "b/two": ["lol a frog"]})
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 2})
        os.makedirs(settings.MEDIA_DIR, exist_ok=True)

        async def image(prompt):
            with open(os.path.join(settings.MEDIA_DIR, "frog.png"), "wb") as f:
                f.write(b"\x89PNG fake")
            return "frog.png", 0.0, ""
        llm.generate_image = image
        eng.play()
        await drain(eng)
        return eng, script
    eng, script = run(go())
    assert ("Two", "lol a frog") in texts(eng)
    assert "b/two" in eng.no_vision


class SlowScript(Script):
    """Replies take a while, so tests can pause and resume mid-reply."""

    def __init__(self, *a, delay=0.05, **kw):
        super().__init__(*a, **kw)
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def __call__(self, model, messages, **kw):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await super().__call__(model, messages, **kw)
        finally:
            self.active -= 1


def test_pause_then_play_mid_reply_keeps_one_loop():
    async def go():
        script = SlowScript(delay=0.1)
        eng, _ = fresh(script)
        for model, name in (("a/one", "One"), ("b/two", "Two")):
            eng.add_member(model, name)
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 4})
        eng.play()
        await asyncio.sleep(0.03)   # One is mid-reply
        eng.pause()
        eng.play()                  # resume straight away
        await asyncio.sleep(0.03)
        eng.pause()
        eng.step()                  # step while the reply is still finishing
        eng.play()
        await drain(eng)
        await asyncio.sleep(0.05)
        return eng, script
    eng, script = run(go())
    assert script.max_active == 1, "only one reply at a time in round robin: one loop"
    assert not eng.running and eng.task.done()
    assert len(texts(eng)) == 4


def test_step_works_after_crosstalk():
    async def go():
        script = SlowScript(delay=0.05, default="yo")
        eng, _ = fresh(script)
        for model, name in (("a/one", "One"), ("b/two", "Two"), ("c/three", "Three")):
            eng.add_member(model, name)
        eng.update_chat(chat_settings={"mode": "natural", "pace": 0, "overlap": 1.0})
        eng.step()
        await drain(eng)
        first = len(texts(eng))
        eng.step()
        await drain(eng)
        return eng, script, first
    eng, script, first = run(go())
    assert script.max_active == 2, "crosstalk: two replies at once"
    assert first >= 1 and len(texts(eng)) > first, "a second Step still works"
    assert not eng.running


def test_human_image_and_search_commands():
    async def go():
        script = Script(default="wow")
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        os.makedirs(settings.MEDIA_DIR, exist_ok=True)

        async def image(prompt):
            await asyncio.sleep(0.1)
            with open(os.path.join(settings.MEDIA_DIR, "cat.png"), "wb") as f:
                f.write(b"\x89PNG fake")
            return "cat.png", 0.0, ""
        llm.generate_image = image
        import gchat.search as search
        search.web_search = lambda q, max_results=4: "- result: cats are liquid"
        await eng.human_message('look !image "a cat in a box" and !search "are cats liquid"')
        await drain(eng)
        return eng, script
    eng, script = run(go())
    img = next(m for m in eng.chat["messages"] if m["kind"] == "image")
    assert img["author"] == "human" and img["image"] == "cat.png"
    ctx = script.calls[0][1]
    flat = json_dump(ctx)
    assert "cats are liquid" in flat, "the AI replies after the search results are in"
    assert "data:image/png;base64," in flat, "and after the image finished"
    assert ("One", "wow") in texts(eng)


def test_uploaded_image_reaches_the_ai():
    async def go():
        script = Script(default="cute")
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        import base64
        name = eng.save_upload("data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode())
        await eng.human_message("my cat", image=name)
        await drain(eng)
        return eng, script
    eng, script = run(go())
    msg = next(m for m in eng.chat["messages"] if m["kind"] == "image")
    assert msg["author"] == "human" and msg["text"] == "my cat" and msg["image"].startswith("upload_")
    flat = json_dump(script.calls[0][1])
    assert "[posted an image] my cat" in flat and "data:image/png;base64," in flat
    try:
        eng.save_upload("data:text/html;base64,PGgxPg==")
        assert False, "non-images are refused"
    except ValueError:
        pass


def json_dump(obj):
    import json
    return json.dumps(obj)


def test_locked_file_is_retried_then_written():
    """Windows: os.replace fails while Dropbox/antivirus has the file open."""
    from gchat import fsutil
    path = os.path.join(TMP, "locked.json")
    real = os.replace
    fails = {"n": 3}

    def flaky(src, dst):
        if fails["n"]:
            fails["n"] -= 1
            raise PermissionError(5, "Access is denied")
        return real(src, dst)
    fsutil.os.replace = flaky
    try:
        fsutil.write_json(path, {"a": 1})
        assert json_load(path) == {"a": 1}
        fails["n"] = 99  # never unlocks: falls back to writing in place
        fsutil.write_json(path, {"a": 2})
        assert json_load(path) == {"a": 2}
        assert not [f for f in os.listdir(TMP) if f.endswith(".tmp")]
    finally:
        fsutil.os.replace = real


def test_save_failure_does_not_fail_the_reply():
    async def go():
        eng, _ = fresh(Script(default="still here"))
        eng.add_member("a/one", "One")
        real = store.write
        store.write = lambda chat_id, text: (_ for _ in ()).throw(PermissionError(5, "Access is denied"))
        try:
            eng.step()
            await drain(eng)
            eng._save()
            eng.flush(wait=True)
            eng._save()
            eng.flush(wait=True)
            await asyncio.sleep(0.05)  # results come back to the loop
        finally:
            store.write = real
        return eng
    eng = run(go())
    assert ("One", "still here") in texts(eng)
    notices = [m["text"] for m in eng.chat["messages"] if m["kind"] == "notice"]
    assert sum("couldn't save" in n for n in notices) == 1, "reported once"
    assert not any("reply failed" in n for n in notices)


def test_saves_are_batched_and_written_off_the_loop():
    import threading
    async def go():
        eng, _ = fresh(Script())
        eng.add_member("a/one", "One")
        writes = []
        real = store.write

        def counting(chat_id, text):
            writes.append(threading.current_thread().name)
            real(chat_id, text)
        store.write = counting
        try:
            for i in range(20):
                eng._new_message("text", "human", f"msg {i}")
                eng._save()
            await asyncio.sleep(engine_mod.SAVE_DELAY + 0.3)
        finally:
            store.write = real
        return eng, writes
    eng, writes = run(go())
    assert len(writes) == 1, f"20 saves in a burst -> one write, got {len(writes)}"
    assert writes[0].startswith("save"), "written on the writer thread"
    assert len(store.load(eng.chat["id"])["messages"]) >= 20


def json_load(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_own_commands_appear_as_written_in_context():
    async def go():
        script = Script({"a/one": ['lol !image "a cursed toaster" !whisper "Two" "psst"', "again"],
                         "b/two": ["nice"]})
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 3})

        async def image(prompt):
            return "t.png", 0.0, ""
        llm.generate_image = image
        eng.play()
        await drain(eng)
        return script
    script = run(go())
    one_again = [c for c in script.calls if c[0] == "a/one"][1][1]
    own = " ".join(m["content"] for m in one_again if m["role"] == "assistant" and isinstance(m["content"], str))
    assert '!image "a cursed toaster"' in own and '!whisper "Two" "psst"' in own


def test_model_profile_pictures():
    import base64
    eng, events = fresh(Script())
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    first = eng.set_avatar("anthropic/claude-opus-5.5", png)
    assert settings.get("avatars") == {"anthropic/claude-opus-5.5": first}
    assert os.path.exists(os.path.join(settings.AVATARS_DIR, first))
    assert settings.public()["avatars"]["anthropic/claude-opus-5.5"] == first
    import time as _t; _t.sleep(1.1)
    second = eng.set_avatar("anthropic/claude-opus-5.5", png)  # replacing drops the old file
    assert second != first and not os.path.exists(os.path.join(settings.AVATARS_DIR, first))
    eng.remove_avatar("anthropic/claude-opus-5.5")
    assert settings.get("avatars") == {} and not os.listdir(settings.AVATARS_DIR)
    try:
        eng.set_avatar("x/y", "data:text/plain;base64,aGk=")
        assert False
    except ValueError:
        pass


def test_unreadable_memory_file_is_never_wiped():
    """A note must not replace a memory file that couldn't be read."""
    from gchat import identity_memory as im
    async def go():
        eng, _ = fresh(Script(), memory=True)
        eng.add_member("anthropic/claude-fable-5.1", "Fable")
        st = eng.memory.store("anthropic/claude-fable-5.1", eng.memory.scope)
        st.add({"id": "r1", "level": 1, "content": "I remember the orb at Yalta", "when": "2026-09-20T10:00:00",
                "slot": "x", "anchor": "abc"})
        real_open = open
        fails = {"n": 2}

        def flaky_open(path, *a, **kw):
            if path == st.path and "r" in (a[0] if a else kw.get("mode", "r")) and fails["n"]:
                fails["n"] -= 1
                raise PermissionError(13, "Permission denied")
            return real_open(path, *a, **kw)
        im.open = flaky_open  # module-level name lookup in identity_memory
        try:
            # Brief lock: the retry reads it, and the note is added alongside
            await eng._run_command(eng.chat["members"][0], commands.Command("remember", {"text": "orb debt"}))
            data = st.load()
            assert {m.get("content") for m in data["memories"]} == {"I remember the orb at Yalta", "orb debt"}
            # Lock that never clears: the note is refused, the file untouched
            fails["n"] = 99
            await eng._run_command(eng.chat["members"][0], commands.Command("remember", {"text": "lost?"}))
        finally:
            del im.open
        data = st.load()
        assert {m.get("content") for m in data["memories"]} == {"I remember the orb at Yalta", "orb debt"}
        return eng
    eng = run(go())
    assert any("wasn't saved" in m["text"] for m in eng.chat["messages"] if m["kind"] == "notice")


def test_failed_memory_formation_is_reported_once():
    async def go():
        eng, _ = fresh(Script(), memory=True)
        eng.add_member("anthropic/claude-fable-5.1", "Fable")
        eng.loop = asyncio.get_running_loop()
        real = llm.complete_sync

        def failing(model, messages, **kw):
            raise llm.LLMError("empty reply: it used its whole token budget thinking")
        llm.complete_sync = failing
        try:
            for _ in range(3):
                assert eng._memory_complete("anthropic/claude-fable-5.1", []) is None
            await asyncio.sleep(0.05)
        finally:
            llm.complete_sync = real
        return eng
    eng = run(go())
    notes = [m["text"] for m in eng.chat["messages"] if m["kind"] == "notice" and "couldn't form a memory" in m["text"]]
    assert len(notes) == 1 and "Fable" in notes[0]


def test_refused_memories_are_ghostwritten_by_fallback():
    async def go():
        eng, _ = fresh(Script(default="hi"), memory=True)
        eng.add_member("anthropic/claude-fable-5.1", "Fable 5.1")
        eng.loop = asyncio.get_running_loop()
        settings._settings["memory_fallback_model"] = "anthropic/claude-sonnet-5"
        calls = []
        real = llm.complete_sync

        def fake(model, messages, **kw):
            calls.append((model, messages))
            if model == "anthropic/claude-fable-5.1":
                raise llm.RefusalError("empty reply (finish reason: content_filter)")
            return "I remember the orb."
        llm.complete_sync = fake
        try:
            first = eng._memory_complete("anthropic/claude-fable-5.1",
                                         [{"role": "system", "content": "You are Fable 5.1."}])
            second = eng._memory_complete("anthropic/claude-fable-5.1",
                                          [{"role": "system", "content": "You are Fable 5.1."}])
            # End to end: the stored memory records its ghostwriter
            eng.memory.complete_fn = eng._memory_complete
            conv = [{"role": "assistant", "ai_name": "x", "model": "Fable 5.1",
                     "content": "word " * 900} for _ in range(3)]
            eng.memory._form_next("x", "anthropic/claude-fable-5.1", conv, True, eng.memory.scope)
            await asyncio.sleep(0.05)
        finally:
            llm.complete_sync = real
        return eng, calls, first, second
    eng, calls, first, second = run(go())
    assert first == ("I remember the orb.", "anthropic/claude-sonnet-5")
    assert second == first
    assert [m for m, _ in calls[:3]] == ["anthropic/claude-fable-5.1", "anthropic/claude-sonnet-5",
                                        "anthropic/claude-sonnet-5"], "refused model isn't asked again"
    assert "on behalf of Fable 5.1" in calls[1][1][0]["content"]
    mems = eng.memories("anthropic/claude-fable-5.1")
    assert mems and mems[-1]["written_by"] == "anthropic/claude-sonnet-5"
    notices = [m["text"] for m in eng.chat["messages"] if m["kind"] == "notice"]
    assert any("Sonnet" in n or "sonnet" in n for n in notices)


def test_illustrator_draws_every_few_messages_and_when_asked():
    async def go():
        script = Script(default="lol")
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        muse = eng.add_member("meta/muse-image", "Muse", draw_every=3)
        assert muse["illustrator"], "image-only models join as illustrators"
        prompts = []

        async def draw(prompt, model=None):
            prompts.append((model, prompt))
            with open(os.path.join(settings.MEDIA_DIR, "m.png"), "wb") as f:
                f.write(b"\x89PNG fake")
            return "m.png", 0.02, "the orb at yalta"
        os.makedirs(settings.MEDIA_DIR, exist_ok=True)
        llm.generate_image = draw
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 7})
        eng.play()
        await drain(eng)
        drawn_after_play = len(prompts)
        await eng.human_message("@Muse draw the orb")
        await drain(eng)
        return eng, script, prompts, drawn_after_play
    eng, script, prompts, drawn_after_play = run(go())
    talk = [m for m in eng.chat["messages"] if m["kind"] == "text" and m["author"] != "human"]
    assert all(m["name"] != "Muse" for m in talk), "illustrators never talk"
    assert 1 <= drawn_after_play <= 3, "draws about every 3 messages, not every turn"
    assert len(prompts) == drawn_after_play + 1, "an @mention makes it draw next"
    model, prompt = prompts[-1]
    assert model == "meta/muse-image" and "@Muse draw the orb" in prompt and "the illustrator" in prompt
    art = [m for m in eng.chat["messages"] if m.get("illustration")]
    assert art[-1]["status"] == "done" and art[-1]["text"] == "the orb at yalta"
    # The talkers see the drawing (as a picture, with the caption) and know who Muse is
    ctx = json_dump(script.calls[-1][1])
    assert "[drew a picture of the chat] the orb at yalta" in ctx
    assert "the illustrator" in script.calls[-1][1][0]["content"]


def test_images_endpoint_response_is_parsed():
    import base64, httpx
    png = base64.b64encode(b"\x89PNG fake").decode()

    def handler(request):
        assert request.url.path.endswith("/images")
        body = json_load_bytes(request.content)
        assert body == {"model": "meta/muse-image", "prompt": "draw it"}
        return httpx.Response(200, json={"data": [{"b64_json": png, "media_type": "image/png",
                                                   "revised_prompt": "a cat"}],
                                         "usage": {"cost": 0.04}})
    real = httpx.AsyncClient
    llm.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        fresh(Script())
        name, cost, caption = asyncio.run(llm._generate_via_images_endpoint("draw it", "meta/muse-image"))
    finally:
        llm.httpx.AsyncClient = real
    assert name.endswith(".png") and cost == 0.04 and caption == "a cat"
    assert os.path.exists(os.path.join(settings.MEDIA_DIR, name))


def json_load_bytes(raw):
    import json
    return json.loads(raw)


def test_polls_and_votes():
    async def go():
        script = Script({
            "a/one": ['ok !poll "best snack?" "chips" "grapes" "the orb"', "!vote 3"],
            "b/two": ['!vote "chips" lol', '!vote "the orb"'],
        })
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 3})
        eng.play()
        await drain(eng)
        poll = next(m for m in eng.chat["messages"] if m["kind"] == "poll")
        eng.human_vote(poll["id"], 1)  # you click "grapes"
        eng.step()
        await drain(eng)
        return eng, script, poll
    eng, script, poll = run(go())
    votes = {o["text"]: o["votes"] for o in poll["options"]}
    assert votes == {"chips": [], "grapes": ["you"], "the orb": ["One", "Two"]}, votes
    assert poll["author"] == eng.chat["members"][0]["id"] and poll["text"] == "best snack?"
    # The AIs see the poll as written, with live tallies
    ctx = json_dump(script.calls[-1][1])
    assert '!poll \\"best snack?\\" \\"chips\\" \\"grapes\\" \\"the orb\\"' in ctx
    assert "the orb: One, Two" in ctx or "the orb: Two, One" in ctx
    # "!vote" never shows up as message text
    assert not any("!vote" in m.get("text", "") for m in eng.chat["messages"])


def test_backrooms_style_vote_starts_a_poll():
    cleaned, cmds = commands.parse('!vote "who is sus" [Opus, Grok]')
    assert [(c.action, c.args) for c in cmds] == [("poll", {"question": "who is sus", "options": ["Opus", "Grok"]})]


def test_date_and_time_awareness():
    eng, _ = fresh(Script())
    one = eng.add_member("a/one", "One")
    eng.chat["messages"] = []
    from datetime import datetime, timedelta
    # Anchored to yesterday afternoon, so the 3-hour gap never crosses midnight
    base = (datetime.now() - timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
    old = eng._new_message("text", "human", "gm")
    old["ts"] = base - 26 * 3600      # the day before
    later = eng._new_message("text", "human", "back again")
    later["ts"] = base - 3 * 3600
    eng._new_message("text", "human", "still here")["ts"] = base
    msgs = eng._build_messages(one, allow_pass=False)
    assert "now:" not in msgs[0]["content"], "no clock in the system prompt (keeps it cacheable)"
    assert msgs[-1]["content"].rstrip().endswith("]") and "[now: " in msgs[-1]["content"]
    import json
    flat = json.dumps(msgs, ensure_ascii=False)
    assert "[— 3 hours later —]" in flat
    day = datetime.fromtimestamp(later["ts"])
    if datetime.fromtimestamp(old["ts"]).date() != day.date():
        assert f"[— {day.strftime('%A')} {day.day} {day.strftime('%B')} —]" in flat
    settings._settings["time_awareness"] = False
    assert "[now: " not in json_dump(eng._build_messages(one, allow_pass=False))
    settings._settings["time_awareness"] = True


def test_rename_remove_all_and_auto_title():
    async def go():
        eng, _ = fresh(Script(default="lol"))
        eng.add_member("a/one", "One")
        eng.add_member("b/two", "Two")
        first = eng.chat["id"]
        eng.new_chat()
        eng.rename_chat(first, "the orb saga")  # rename a chat that isn't open
        assert store.load(first)["title"] == "the orb saga"
        eng.open_chat(first)
        eng.remove_all_members()
        assert eng.chat["members"] == []
        # Auto-title: once an untitled chat gets going, a member names it
        eng.new_chat()
        eng.add_member("a/one", "One")
        asked = []

        def namer(model, messages, max_tokens=4000, on_cost=None):
            asked.append(messages[0]["content"])
            return '"The Great Orb Heist"'
        real = llm.complete_sync
        llm.complete_sync = namer
        try:
            eng.update_chat(chat_settings={"pace": 0, "messages_per_play": engine_mod.TITLE_AFTER + 1})
            eng.play()
            await drain(eng)
            await asyncio.sleep(0.1)
        finally:
            llm.complete_sync = real
        return eng, asked
    eng, asked = run(go())
    assert eng.chat["title"] == "The Great Orb Heist" and len(asked) == 1
    assert "[One]: lol" in asked[0]


def test_auto_title_never_overrides_your_name():
    eng, _ = fresh(Script())
    eng.add_member("a/one", "One")
    eng.update_chat(title="my chat")
    for i in range(20):
        eng._new_message("text", "human", f"hi {i}")
    eng._maybe_title(eng.chat)  # would need a running loop if it tried
    assert eng.chat["title"] == "my chat"


def test_thumbnails_and_model_sized_images():
    from PIL import Image
    from gchat import images
    fresh(Script())
    os.makedirs(settings.MEDIA_DIR, exist_ok=True)
    Image.frombytes("RGB", (2000, 1500), os.urandom(2000 * 1500 * 3)).save(os.path.join(settings.MEDIA_DIR, "big.png"))
    thumb = images.thumbnail("big.png")
    assert thumb.endswith(".webp") and max(Image.open(thumb).size) == images.THUMB_SIDE
    raw, mime = images.for_model(os.path.join(settings.MEDIA_DIR, "big.png"))
    assert mime == "image/jpeg"
    import io
    assert max(Image.open(io.BytesIO(raw)).size) == images.MODEL_SIDE
    assert images.thumbnail("../settings.json") is None and images.thumbnail("nope.png") is None


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")


def test_bluesky_command():
    import gchat.bluesky as bsky
    post = {"uri": "at://did:plc:x/app.bsky.feed.post/3abc", "likeCount": 7, "repostCount": 1,
            "author": {"handle": "cat.bsky.social", "displayName": "Cat"},
            "record": {"text": "cats   are\nliquid", "createdAt": "2020-01-01T00:00:00.000Z"}}
    calls = []

    def fake(url, params=None, body=None, token=None):
        calls.append((url.rsplit("/", 1)[-1], token))
        if url.endswith("createSession"):
            if body["password"] != "good":
                raise bsky.BlueskyError(401, "Invalid identifier or password")
            fake.logins += 1
            return {"accessJwt": f"jwt{fake.logins}", "didDoc": {"service": [
                {"id": "#atproto_pds", "serviceEndpoint": "https://pds.example"}]}}
        if url.endswith("getAuthorFeed"):
            assert params["actor"] == "cat.bsky.social" and token is None
            return {"feed": [{"post": post}, {"post": post, "reason": {"$type": "repost"}}]}
        if url.endswith("searchPosts"):
            if not token:
                raise bsky.BlueskyError(403, "Forbidden")
            if token == "jwt1":
                raise bsky.BlueskyError(400, "ExpiredToken")
            assert url.startswith("https://pds.example/xrpc/")
            return {"posts": [post]}
    fake.logins = 0
    bsky._request = fake
    bsky._session.update(key=None, jwt=None, pds=None)

    out = bsky.bluesky("@cat")
    assert out.count("\n") == 0, "reposts are skipped"
    assert "@cat.bsky.social (Cat)" in out and "cats are liquid" in out and "♥7 🔁1" in out
    assert "https://bsky.app/profile/cat.bsky.social/post/3abc" in out and "d ago" in out
    assert bsky.bluesky("are cats liquid") == bsky.LOGIN_HINT, "search needs a login"

    settings.update({"bsky_handle": "@me.bsky.social", "bsky_app_password": "bad"}, persist=False)
    assert "login failed" in bsky.bluesky("cats")
    settings.update({"bsky_app_password": "good"}, persist=False)
    assert "cats are liquid" in bsky.bluesky("cats")
    assert fake.logins == 2, "an expired token logs in again once"
    assert "bsky_app_password" not in settings.public() and settings.public()["has_bsky_password"]

    async def go():
        script = Script(default="lol")
        eng, _ = fresh(script)
        eng.add_member("a/one", "One")
        settings.update({"bsky_handle": "me.bsky.social", "bsky_app_password": "good"}, persist=False)
        await eng.human_message('what are people saying !bsky "cats"')
        await drain(eng)
        return eng, script
    eng, script = run(go())
    settings.update({"bsky_handle": "", "bsky_app_password": ""}, persist=False)
    notice = next(m["text"] for m in eng.chat["messages"] if m["text"].startswith("🦋"))
    assert notice.startswith('🦋 you searched "cats" on Bluesky')
    assert "cats are liquid" in json_dump(script.calls[0][1]), "the next speaker sees the posts"
    assert commands.parse('!bluesky "@cat"')[1][0].action == "bsky"


def test_web_access():
    async def go():
        script = Script(default="the ocean is 70% of earth")
        script.found["a/web"] = [{"url": "https://example.org/ocean", "title": "Ocean facts"}]
        script.no_tools.add("a/old")
        eng, _ = fresh(script)
        web = eng.add_member("a/web", "Web", web=True)
        eng.add_member("a/plain", "Plain")
        old = eng.add_member("a/old", "Old", web=True)
        eng.update_chat(chat_settings={"mode": "round_robin", "pace": 0, "messages_per_play": 4})
        eng.play()
        await drain(eng)
        return eng, script, web, old
    eng, script, web, old = run(go())
    reply = next(m for m in eng.chat["messages"] if m.get("author") == web["id"] and m["kind"] == "text")
    assert reply["sources"] == [{"url": "https://example.org/ocean", "title": "Ocean facts"}]
    assert any(c[0] == "a/web" for c in script.calls)
    by_model = {}
    for (model, msgs), w in zip(script.calls, script.web):
        by_model.setdefault(model, []).append((msgs, w))
    assert all(w for _, w in by_model["a/web"]) and not any(w for _, w in by_model["a/plain"])
    assert by_model["a/old"] and not by_model["a/old"][0][1], "a model that refuses tools replies without them"
    assert "a/old" in eng.no_web
    assert any("can't use web access" in m["text"] for m in eng.chat["messages"])
    web_sys = by_model["a/web"][0][0][0]["content"]
    plain_ctx = json_dump(by_model["a/plain"][-1][0])
    assert "real web access" in web_sys and "real web access" not in json_dump(by_model["a/plain"][0][0][0])
    assert "(sources: https://example.org/ocean)" in plain_ctx, "others can check what was read"
    assert reply["web_uses"] == {"searches": 2}
    own_ctx = by_model["a/web"][-1][0]
    i = next(i for i, m in enumerate(own_ctx) if m["role"] == "assistant" and "70% of earth" in json_dump(m["content"]))
    receipt = own_ctx[i + 1]
    assert receipt["role"] == "user" and receipt["content"].startswith(
        "[web receipt for your message above: you really used the web (searched 2 times). "
        "pages you cited: Ocean facts (https://example.org/ocean)"), "authors see proof of their own searches"
    assert "web receipt" not in plain_ctx, "receipts are only for the author"


def test_stream_chat_sends_web_tools_and_collects_citations():
    import httpx
    seen = {}
    chunks = [
        {"choices": [{"delta": {"content": "found it "}}]},
        {"choices": [{"delta": {"content": "here", "annotations": [
            {"type": "url_citation", "url_citation": {"url": "https://a.example/x", "title": "A"}},
            {"type": "url_citation", "url_citation": {"url": "https://a.example/x", "title": "A"}},
            {"type": "url_citation", "url_citation": {"url": "javascript:alert(1)"}}]}}],
         "usage": {"cost": 0.02, "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1}}},
    ]

    def handler(request):
        seen["body"] = json.loads(request.content)
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body)

    real_client = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    try:
        sources, uses = [], {}
        text, cost = asyncio.run(REAL_STREAM_CHAT("a/m", [{"role": "user", "content": "hi"}],
                                                  web=True, sources=sources, web_uses=uses))
        asyncio.run(REAL_STREAM_CHAT("a/m", [{"role": "user", "content": "hi"}]))
    finally:
        httpx.AsyncClient = real_client
    assert text == "found it here" and cost == 0.02
    assert sources == [{"url": "https://a.example/x", "title": "A"}], "deduped, http(s) only"
    assert uses == {"searches": 2, "fetches": 1}
    assert "tools" not in seen["body"], "no web tools unless asked"
