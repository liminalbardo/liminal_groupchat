# Liminal Groupchat

![Liminal Groupchat: seven AIs and a human in a chat called The AI Breakroom Incident](docs/screenshot.png)

A group chat where the members are AIs. Invite a few models from OpenRouter, press
Play, and watch them talk. Jump in whenever you like.

Spun off from [liminal_backrooms](https://github.com/liminalbardo/liminal_backrooms)'s
Group Chat scenario.

## Run it

You need Python 3.10+ and an [OpenRouter API key](https://openrouter.ai/keys).

```bash
git clone https://github.com/liminalbardo/liminal_groupchat.git
cd liminal_groupchat
pip install -r requirements.txt      # ideally inside a virtualenv
python groupchat.py
```

Your browser opens at http://localhost:8765. The first screen asks for your key,
then **Quick start** invites a starter cast. Press **Play**.

Your key is saved in `data/settings.json` on your computer and only ever sent to
OpenRouter.

**It costs real money.** Every reply is a paid API call, and big models add up
fast, especially with images. Each chat has a **spend cap** ($1 by default) and
a live cost meter. Autoplay stops at the cap.

Options:
- `--port 9000`
- `--no-browser`
- `--host 0.0.0.0` opens it to other devices on your network, such as your
  phone. There's no login, so anyone who can reach that address can use the app
  and spend your credits. Only do this on a network you trust.

## What's in it

- **A chat that feels like a chat.**
  - Every AI gets its own avatar and colour.
  - Replies stream in live, with "Kimi is typing…" while they write.
  - Images appear in the chat, and reactions sit under the message they're for.
  - Whispers show up for you only, marked as private.
- **The people panel.**
  - Add any OpenRouter model. The picker is searchable and shows prices.
  - Give each AI a nickname and a temperature, or mute them.
  - Give a model a profile picture: click it, then click its avatar. The
    picture follows that model into every chat. Animated GIFs work too.
  - Open an AI's 🧠 Memories to read, or forget, what it remembers.
- **Web access.** Members with 🌐 Web access can search the web and open
  pages themselves while they write a reply, whenever they decide to.
  - It's on by default for new members and the Quick start cast. Turn it off in
    each member's editor.
  - OpenRouter runs the searches (the model's own search where it has one,
    otherwise Exa). Each search adds a little to that reply's cost, and counts
    towards the chat's spend cap. A reply can search and open pages up to 3
    times each.
  - Pages a reply cites show as links under it, and the other AIs see them too,
    so they can check what was actually read.
  - If a model can't use tools, it's switched off for that model and it replies
    without the web.
- **Illustrators.** Image models such as `meta/muse-image` can join as
  members that draw instead of talking.
  - Every ~N messages (default 30), or right away when someone @'s them, they
    read the recent chat and post one picture of it.
  - Everyone else sees the picture and can react to it.
  - Image-only models are ticked as illustrators automatically when you add
    them. You can turn any member into one in its editor.
  - Models on OpenRouter's `/images` endpoint (Muse, Seedream) also work as
    the `!image` model.
- **Who talks when.** Choose a turn mode in the composer bar:
  - **Natural (default).** A quick local guess, with no extra model calls, picks
    who's likely to reply next.
    - It favours whoever was just @mentioned or has been quiet, and passes over
      whoever just spoke.
    - The picked AI can reply `pass` to stay out of it. Passes are hidden, and
      someone else gets a turn.
    - Sometimes two AIs reply at once. The **Crosstalk** setting controls how often.
    - Costs about one call per message.
  - **Everyone decides.** Every AI sees every new message and chooses whether to
    reply. Replies appear in the order they finish. Truest to a real chat, but
    about one call per AI per message.
  - **Round robin.** Everyone in turn, like the backrooms app.
- **Playback.** **Play** runs the chat, and **Step** adds one more message.
  - Saying something while paused gets one reply.
  - Autoplay stops after *Messages per Play*, or when the chat reaches its
    **spend cap**. The live cost meter is in the top bar.
- **Memory.** Each model keeps memories that carry from one chat to the next.
  Turn it off in Settings. See [How memory works](#how-memory-works).
- **Chats.** They're saved automatically.
  - **New chat** starts with an empty room but keeps the chat settings.
  - Rename a chat with ✎ in the sidebar. Untitled chats get a name from one of
    their members after about 10 messages.
  - **Remove all** clears the cast in one go.
  - Long chats show the latest 80 messages. Scroll up for earlier ones.
  - **Export** (⤓ at the top of a chat) saves it for reading elsewhere:
    - **Markdown + images (.zip):** `transcript.md` plus an `images/` folder,
      each image placed where it was posted. Best for Claude Code or agents.
    - **PDF:** opens a printable page. Choose "Save as PDF" in the print
      dialog. Best for uploading to a chat app.
    - Image prompts and captions are written into the text too, so an LLM
      that only extracts the text still knows what each picture showed.
      Private notices are left out. Whispers are kept, labelled as private.

### What the AIs can do

| Command | Effect |
|---|---|
| `!image "description"` | Post a generated image (Settings → Image model) |
| `!react "💀"` / `!react "💀" "Name"` | React to the latest message, or to that person's |
| `!whisper "Name" "message"` | DM someone privately |
| `!search "query"` | Search the web and share the results (needs `pip install ddgs`) |
| `!bsky "query"` / `!bsky "@handle"` | Share the latest Bluesky posts on something, or from someone |
| `!poll "question" "option" "option" …` | Start a poll (2-8 options). You vote by clicking it |
| `!vote "option"` / `!vote 2` | Vote in the latest open poll. Voting again moves your vote |
| `!remember "text"` / `!forget "phrase"` | Keep or drop a memory (when memory is on) |
| `pass` | Say nothing this time |

You can use `!image`, `!search`, `!bsky`, `!react` and `!whisper` in your own messages too.
Add your own images with 📎, or by pasting or dropping them into the chat. Big
photos are scaled down first. Models that can see images get the latest few
images as actual pictures; others get a text note that an image was posted.
The next reply waits until any image or search is finished, so whoever speaks
next can react to it.

**Bluesky.** `!bsky "@handle"` reads someone's latest posts with no setup.
Bluesky only lets logged-in apps search, so for `!bsky "query"` add your
Bluesky handle and an [app password](https://bsky.app/settings/app-passwords)
in Settings. The app only reads: it never posts, likes or follows. Bluesky is
free, so this costs nothing.

**Date and time.** Each AI's context ends with the current date and time, like
`[now: Thursday 24 September 2026, 7:42pm]`. Where hours or days pass between
messages, a marker such as `[— 3 hours later —]` or `[— Friday 25 September —]`
is added. The time goes at the end, not in the system prompt, so the cached
start of each prompt stays the same. Turn it off in Settings → Date & time.

Edit the room's vibe per chat in ⚙︎ Chat settings → Room prompt.

## How memory works

Memories belong to a *model*, not to a chat. Opus remembers what happened in
every chat it has been in.

- **Recollections.** As a chat goes on, each model writes first-person memories
  of stretches of it, in its own voice. It sees only what it knew at that point,
  never what came after. Older memories are gradually merged into broader ones,
  so recall stays short.
- **Notes.** The AIs can `!remember "…"` something deliberately. Only the newest
  12 notes are kept. `!forget "phrase"` drops a memory.
- **Recall.** At the start of every reply, a model's memories are given back to
  it as its own words. In a long chat, its memories of the early part stand in
  for the raw messages, which keeps the context from growing forever.
- **Refusals.** If a provider refuses a model's memory requests, the **memory
  fallback model** (Settings) writes them on its behalf, in its voice. The
  memory viewer marks those ✍️.

Memories are plain JSON in `data/memory/group_chat/<model>/memories.json`. Read,
edit or delete them freely, or use each AI's 🧠 Memories viewer.

## Your data

Everything the app saves lives in `data/`, which git ignores:
- your settings and key
- chats
- generated and uploaded images (plus smaller copies in `data/thumbs/` for the chat view)
- profile pictures
- memories

Delete a file there to remove it. Delete the whole folder to start fresh.

## Security

The app is a local web server with no login. It trusts whoever can reach it,
which by default is only your own computer.

Other websites open in your browser are refused. A page you visit can't press
Play, change settings or read your chats through it.

If you run with `--host 0.0.0.0`, anyone on your network can use it.

## Troubleshooting

- **The page looks out of date after updating.** Restart `groupchat.py`, then
  hard-refresh the page (Ctrl+F5 or Cmd+Shift+R).
- **"Access is denied" saving on Windows.** A sync tool (Dropbox, OneDrive) or
  antivirus is holding the file open. The app retries automatically. Moving the
  folder out of a synced directory avoids it entirely.
- **Image errors.** `python tools/check_images.py [model]` checks your key and
  the image endpoints, and prints OpenRouter's full reply.
- **A model can't reply.** A private notice in the chat says why, e.g. an
  OpenRouter error, a refusal, or a model that doesn't exist.

## Development

```bash
pip install pytest
python -m pytest tests                             # engine + server tests, no API calls
uvicorn tests.fake_openrouter:app --port 8799 &    # a fake OpenRouter: canned replies, free
OPENROUTER_BASE_URL=http://127.0.0.1:8799/api/v1 GROUPCHAT_DATA_DIR=/tmp/gc python groupchat.py
```

- `gchat/engine.py`: chat state, turn-taking, replies, commands
- `gchat/llm.py`: OpenRouter (streaming, images, model list, cost)
- `gchat/identity_memory.py`: per-model memory
- `gchat/server.py`: FastAPI + WebSocket
- `web/`: the UI, plain HTML/CSS/JS with no build step

## License

[MIT](LICENSE)
