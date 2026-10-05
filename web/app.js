// Liminal Groupchat - browser UI. Talks to the server over /api and a WebSocket.

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
  return node;
};

// Models worth showing first in the picker, when OpenRouter has them.
const FEATURED = [
  "anthropic/claude-opus-5.5", "anthropic/claude-fable-5.1", "google/gemini-3.1-pro-preview",
  "openai/gpt-6-astra", "openai/gpt-6-sol", "openai/gpt-6-luna-pro",
  "anthropic/claude-opus-5", "anthropic/claude-fable-5", "anthropic/claude-sonnet-5",
  "anthropic/claude-opus-4.6", "anthropic/claude-opus-4.5", "anthropic/claude-sonnet-4.5", "anthropic/claude-haiku-4.5",
  "google/gemini-3-pro-preview", "google/gemini-3.7-flash",
  "openai/gpt-5.6-sol", "openai/gpt-5.5", "openai/gpt-5.4", "openai/gpt-4o",
  "x-ai/grok-4.6", "x-ai/grok-4.5", "x-ai/grok-4",
  "moonshotai/kimi-k3", "moonshotai/kimi-k2.6", "moonshotai/kimi-k2.5",
  "deepseek/deepseek-v4-pro", "deepseek/deepseek-r1-0528", "qwen/qwen3.8-max",
  "z-ai/glm-5.3", "minimax/minimax-m2.5",
];
// Invited by "Quick start", whether or not OpenRouter's public list shows them.
const STARTER_CAST = [
  ["anthropic/claude-opus-5.5", "Opus 5.5"],
  ["anthropic/claude-fable-5.1", "Fable 5.1"],
  ["google/gemini-3.1-pro-preview", "Gemini 3.1 Pro"],
  ["openai/gpt-6-astra", "GPT 6 Astra"],
  ["openai/gpt-6-sol", "GPT 6 Sol"],
  ["openai/gpt-5.4-image-2", "GPT Image", { illustrator: true }],
].map(([model, name, extra]) => [model, name, extra || { web: true }]);
const WEB_HELP = "Can search the web and open pages while replying, whenever it wants. Each search costs a little (counts towards the spend cap).";
const MODE_HINTS = {
  natural: "whoever's likely to speak does; they can pass",
  everyone: "every AI sees every message and chooses (costs more)",
  round_robin: "everyone takes turns, in order",
};

const state = { settings: {}, chats: [], chat: null, running: false, typing: new Set(), models: null };
const msgEls = new Map();

// ─── API ──────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    toast(detail);
    throw new Error(detail);
  }
  return res.json();
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3200);
}

// ─── Live updates ─────────────────────────────────────────────────────

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => {
    try { handle(JSON.parse(e.data)); }
    catch (err) { console.error("update failed", err); resync(); }
  };
  ws.onclose = () => setTimeout(connect, 1200);
}

// If an update ever fails to apply, rebuild the UI from a fresh snapshot
// instead of leaving the controls in a stale state.
let resyncing = false;
async function resync() {
  if (resyncing) return;
  resyncing = true;
  try { handle(await api("GET", "/api/state")); } finally { resyncing = false; }
}

function handle(ev) {
  switch (ev.type) {
    case "snapshot": {
      const chatChanged = state.chat?.id !== ev.chat?.id;
      if (chatChanged) state.shown = WINDOW;
      Object.assign(state, { settings: ev.settings, chats: ev.chats, chat: ev.chat, running: ev.running });
      state.typing = new Set(ev.typing);
      renderAll(chatChanged);
      onboarding();
      break;
    }
    case "message": upsertMessage(ev.message); break;
    case "delta": {
      const msg = state.chat?.messages.find((m) => m.id === ev.id);
      if (msg) { msg.text = ev.text; updateText(msg); }
      break;
    }
    case "remove": {
      if (!state.chat) break;
      state.chat.messages = state.chat.messages.filter((m) => m.id !== ev.id);
      renderMessages();
      break;
    }
    case "typing":
      ev.on ? state.typing.add(ev.member) : state.typing.delete(ev.member);
      renderTyping(); renderMembers();
      break;
    case "pass": showPass(ev.member); break;
    case "waiting": state.waiting = ev.on ? ev.what : null; renderTyping(); break;
    case "status":
      state.running = ev.running;
      if (state.chat) state.chat.cost = ev.cost;
      renderControls(); renderCost();
      break;
  }
}

// ─── Rendering ────────────────────────────────────────────────────────

function renderAll(chatChanged) {
  renderChats(); renderHeader(); renderMembers(); renderControls(); renderCost();
  renderMessages(chatChanged); renderTyping();
}

const initials = (name) => {
  const words = (name || "?").replace(/[^\p{L}\p{N}. ]/gu, "").split(/\s+/).filter((w) => /^\p{L}/u.test(w));
  if (!words.length) return "?";
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)).toUpperCase();
};
// A model's profile picture if it has one, else coloured initials
const avatarUrl = (model) => {
  const file = model && (state.settings.avatars || {})[model];
  return file ? `/avatars/${encodeURIComponent(file)}` : null;
};
const avatar = (name, color, model, cls = "avatar") => {
  const url = avatarUrl(model);
  return url
    ? el("div", { class: `${cls} has-pic`, style: `background:${color || "#868e96"}` }, el("img", { src: url, alt: "" }))
    : el("div", { class: cls, style: `background:${color || "#868e96"}` }, initials(name));
};
const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const members = () => state.chat?.members || [];
const memberById = (id) => members().find((m) => m.id === id);
const shortModel = (id) => (id || "").split("/").pop();

function renderChats() {
  const list = $("#chat-list");
  const live = state.chat && {
    ...state.chats.find((c) => c.id === state.chat.id), id: state.chat.id, title: state.chat.title,
    members: members().map((m) => ({ name: m.name, color: m.color })),
    count: state.chat.messages.filter((m) => m.kind === "text" || m.kind === "image").length,
  };
  const chats = state.chats.map((c) => (live && c.id === live.id ? live : c));
  list.replaceChildren(...chats.map((c) => el("button", {
    class: `chat-item ${c.id === state.chat?.id ? "active" : ""}`,
    onclick: () => { api("POST", `/api/chats/${c.id}/open`); closeDrawers(); },
  },
    el("div", { class: "dots" }, ...c.members.slice(0, 4).map((m) => el("span", { style: `background:${m.color}` }))),
    el("div", { class: "meta" },
      el("div", { class: "name" }, c.title),
      el("div", { class: "sub" }, c.count ? `${c.count} messages` : "empty")),
    el("span", {
      class: "del", title: "Rename chat",
      onclick: (e) => { e.stopPropagation(); renameInline(e.currentTarget.closest(".chat-item"), c); },
    }, "✎"),
    el("span", {
      class: "del", title: "Delete chat",
      onclick: (e) => {
        e.stopPropagation();
        if (confirm(`Delete "${c.title}"? This can't be undone.`)) api("DELETE", `/api/chats/${c.id}`);
      },
    }, "✕"),
  )));
}

function renameInline(item, chat) {
  const nameEl = $(".name", item);
  const input = el("input", { class: "rename", value: chat.title, spellcheck: "false" });
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (save && title && title !== chat.title) api("PATCH", `/api/chats/${chat.id}`, { title });
    else renderChats();
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("blur", () => finish(true));
  nameEl.replaceWith(input);
  input.focus();
  input.select();
}

function renderHeader() {
  const title = $("#chat-title");
  if (document.activeElement !== title) title.value = state.chat?.title || "";
  const names = members().map((m) => m.name);
  $("#subtitle").textContent = names.length
    ? `${names.join(", ")} and ${state.settings.username || "you"}`
    : "nobody here yet";
  document.title = `${state.chat?.title || "chat"} · Liminal Groupchat`;
}

function renderCost() {
  const cost = state.chat?.cost || 0;
  $("#cost").textContent = `$${cost < 1 ? cost.toFixed(3) : cost.toFixed(2)}`;
}

function renderControls() {
  const play = $("#play");
  play.textContent = state.running ? "⏸ Pause" : "▶ Play";
  play.classList.toggle("running", state.running);
  $("#step").disabled = state.running;
  const mode = state.chat?.settings.mode || "natural";
  $("#mode").value = mode;
  $("#mode-hint").textContent = MODE_HINTS[mode] || "";
}

function renderMembers() {
  const list = $("#member-list");
  list.replaceChildren(...members().map((m) => {
    const typing = state.typing.has(m.id);
    return el("div", { class: `member ${m.muted ? "muted" : ""}`, onclick: () => editMember(m), title: "Edit" },
      avatar(m.name, m.color, m.model),
      el("span", { class: "presence" }),
      el("div", { class: "info" },
        el("div", { class: "name" }, m.name),
        el("div", { class: "model" }, m.model),
        typing ? el("div", { class: "state typing" }, m.illustrator ? "drawing…" : "typing…")
          : m.muted ? el("div", { class: "state" }, "muted")
          : m.illustrator ? el("div", { class: "state" }, `🎨 illustrator · every ~${m.draw_every || 30} msgs`)
          : m.web ? el("div", { class: "state" }, "🌐 web access") : null),
    );
  }));
  if (!members().length) {
    list.append(el("p", { class: "hint", style: "padding: 4px 8px" }, "Nobody here yet. Add a few AIs to get the chat going."));
  }
  $("#remove-all").hidden = members().length < 2;
}

// @mentions: one regex per cast, not one per message rendered
let mentionCache = { key: null, re: null };
function mentionPattern() {
  const names = [...new Set([...members().flatMap((m) => [m.name, m.name.split(" ")[0]]), state.settings.username])]
    .filter((n) => n && n.length >= 3).sort((a, b) => b.length - a.length);
  const key = names.join("\u0001");
  if (key !== mentionCache.key) {
    const esc = (n) => n.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]))
      .replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    mentionCache = { key, re: names.length ? new RegExp(`@(${names.map(esc).join("|")})`, "gi") : null };
  }
  return mentionCache.re;
}

// Minimal formatting: escape, then code, bold, italics, links, mentions.
function format(text) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const blocks = [];
  let s = esc(text || "").replace(/```(?:\w+)?\n?([\s\S]*?)```/g, (_, code) => {
    blocks.push(`<pre>${code.replace(/\n$/, "")}</pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, "$1<i>$2</i>")
    .replace(/(https?:\/\/[^\s<]+[^\s<.,)!?])/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  const mention = mentionPattern();
  if (mention) s = s.replace(mention, '<span class="mention">@$1</span>');

  s = s.split(/\n{2,}/).map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[i]);
}

function isGrouped(msg, prev) {
  return prev && prev.kind !== "notice" && msg.kind !== "notice" && prev.author === msg.author
    && msg.ts - prev.ts < 300 && prev.kind !== "whisper" && msg.kind !== "whisper";
}

function visible(msg) {
  return !(msg.kind === "whisper" && !state.settings.show_whispers);
}

function messageEl(msg, prev) {
  if (msg.kind === "notice") {
    const isSearch = msg.text.startsWith("🔎") || msg.text.startsWith("🦋");
    return el("div", { class: `notice ${msg.private ? "private" : ""} ${isSearch ? "search" : ""}`, "data-id": msg.id }, msg.text);
  }
  const human = msg.author === "human";
  const m = memberById(msg.author);
  const name = human ? (state.settings.username || "you") : (m?.name || msg.name || "someone");
  const color = m?.color || msg.color;
  const classes = ["msg", human ? "human" : "", isGrouped(msg, prev) ? "grouped" : "",
    msg.status === "streaming" ? "streaming" : "", msg.kind === "whisper" ? "whisper" : ""];

  let body;
  if (msg.kind === "poll") {
    const me = state.settings.username || "you";
    const total = msg.options.reduce((n, o) => n + o.votes.length, 0);
    body = el("div", { class: "poll-card" },
      el("div", { class: "poll-q" }, "📊 ", msg.text),
      ...msg.options.map((o, i) => {
        const pct = total ? Math.round((100 * o.votes.length) / total) : 0;
        return el("button", {
          class: `poll-opt ${o.votes.includes(me) ? "mine" : ""}`, type: "button",
          title: o.votes.length ? o.votes.join(", ") : "no votes yet",
          onclick: () => api("POST", `/api/polls/${msg.id}/vote`, { option: i }),
        },
          el("span", { class: "poll-bar", style: `width:${pct}%` }),
          el("span", { class: "poll-label" }, o.text),
          el("span", { class: "poll-count" }, o.votes.length ? `${o.votes.length} · ${o.votes.join(", ")}` : ""));
      }),
      el("div", { class: "poll-foot" }, total ? `${total} vote${total === 1 ? "" : "s"}` : "no votes yet · click to vote"));
  } else if (msg.kind === "image") {
    if (msg.status === "pending") {
      body = el("div", { class: "image-card pending" },
        el("div", { class: "ph" }, msg.illustration ? "🎨 drawing the chat…" : "🎨 making an image…"),
        msg.prompt ? el("div", { class: "caption" }, msg.prompt) : null);
    } else if (msg.status === "error") {
      body = el("div", { class: "image-card error" }, el("div", { class: "caption" }, `🎨 ${msg.prompt || "drawing"} — ${msg.text}`));
    } else {
      const img = el("img", { src: `/thumbs/${msg.image}`, alt: msg.prompt || msg.text || "image", loading: "lazy", decoding: "async", onclick: () => zoom(`/media/${msg.image}`) });
      img.addEventListener("load", () => stickToBottom());
      const caption = msg.prompt ? el("div", { class: "caption" }, msg.prompt)
        : msg.text ? el("div", { class: "caption", html: format(msg.text) }) : null;
      body = el("div", { class: "image-card" }, img, caption);
    }
  } else {
    body = el("div", { class: "bubble", html: format(msg.text) });
  }

  const whisperTag = msg.kind === "whisper"
    ? el("div", { class: "whisper-tag" }, `🤫 whispered to ${msg.to === "human" ? (state.settings.username || "you") : (memberById(msg.to)?.name || "someone")}`)
    : null;
  const reactions = Object.entries(msg.reactions || {});
  return el("div", { class: classes.filter(Boolean).join(" "), "data-id": msg.id },
    avatar(name, color, human ? null : (m?.model || msg.model)),
    el("div", { class: "msg-body" },
      el("div", { class: "msg-head" },
        el("span", { class: "who", style: human ? "" : `color:${color}` }, name),
        !human && (m?.model || msg.model) ? el("span", { class: "model" }, shortModel(m?.model || msg.model)) : null,
        el("span", { class: "time", title: new Date(msg.ts * 1000).toLocaleString() }, timeOf(msg.ts))),
      whisperTag, body,
      msg.sources?.length || msg.web_uses ? el("div", { class: "sources", title: webUsesText(msg.web_uses) },
        "🌐", ...(msg.sources?.length ? msg.sources.map((s) =>
          el("a", { href: s.url, target: "_blank", rel: "noopener noreferrer", title: s.title || s.url }, hostOf(s.url)))
          : [webUsesText(msg.web_uses)])) : null,
      reactions.length ? el("div", { class: "reactions" }, ...reactions.map(([emoji, who]) =>
        el("span", { class: "reaction", title: who.join(", ") }, emoji, who.length > 1 ? el("b", {}, who.length) : null))) : null,
    ));
}

// Long chats render only their latest messages; scrolling to the top (or the
// button there) brings in earlier ones. Opening a 1000-message chat stays fast.
const WINDOW = 80;
state.shown = WINDOW;

function renderMessages(scrollToEnd = true) {
  const box = $("#messages");
  msgEls.clear();
  const all = (state.chat?.messages || []).filter(visible);
  const start = Math.max(0, all.length - state.shown);
  const nodes = [];
  if (start > 0) {
    nodes.push(el("button", { class: "earlier", onclick: showEarlier }, `Show earlier messages (${start})`));
  }
  let prev = start > 0 ? all[start - 1] : null;
  for (const msg of all.slice(start)) {
    const node = messageEl(msg, prev);
    msgEls.set(msg.id, node);
    nodes.push(node);
    prev = msg;
  }
  if (!all.length) nodes.push(emptyState());
  const atBottom = nearBottom();
  box.replaceChildren(...nodes);
  if (scrollToEnd || atBottom) scrollBottom();
}

function showEarlier() {
  const box = $("#messages");
  const fromBottom = box.scrollHeight - box.scrollTop;
  state.shown += WINDOW * 2;
  renderMessages(false);
  box.scrollTop = box.scrollHeight - fromBottom;  // stay where you were
}

function emptyState() {
  if (!members().length) {
    return el("div", { class: "empty" },
      el("h2", {}, "An empty room"),
      el("p", {}, "Invite a few AIs, then hit Play and watch them talk. Jump in whenever you like."),
      el("div", { style: "display:flex; gap:8px; justify-content:center; flex-wrap:wrap" },
        el("button", { class: "btn primary", onclick: quickCast }, "✨ Quick start: invite the starter cast"),
        el("button", { class: "btn", onclick: addMember }, "Pick them myself")));
  }
  return el("div", { class: "empty" },
    el("h2", {}, "Quiet in here"),
    el("p", {}, "Press Play to let them talk, or say something to get it started."));
}

function upsertMessage(msg) {
  if (!state.chat) return;
  const list = state.chat.messages;
  const i = list.findIndex((m) => m.id === msg.id);
  const atBottom = nearBottom();
  if (i >= 0) {
    list[i] = msg;
    const old = msgEls.get(msg.id);
    if (old && visible(msg)) {
      const prev = list.slice(0, i).filter(visible).pop();
      const node = messageEl(msg, prev);
      old.replaceWith(node);
      msgEls.set(msg.id, node);
    } else if (visible(msg) && list.length - i <= state.shown) {
      renderMessages(false);
    }  // otherwise it's above what's shown: the data is updated, nothing to draw
  } else {
    list.push(msg);
    renderChats();
    if (!visible(msg)) return;
    const box = $("#messages");
    $(".empty", box)?.remove();
    const prev = list.slice(0, -1).filter(visible).pop();
    const node = messageEl(msg, prev);
    msgEls.set(msg.id, node);
    box.append(node);
    if (!atBottom && msg.author !== "human") $("#jump").hidden = false;
  }
  if (atBottom || msg.author === "human") scrollBottom();
}

function updateText(msg) {
  const node = msgEls.get(msg.id);
  const bubble = node && $(".bubble", node);
  if (!bubble) return upsertMessage(msg);
  const atBottom = nearBottom();
  bubble.innerHTML = format(msg.text);
  if (atBottom) scrollBottom();
}

function renderTyping() {
  const box = $("#typing");
  const who = [...state.typing].map(memberById).filter(Boolean);
  const passed = $(".passed", box);
  if (!who.length) {
    const waiting = state.waiting && el("span", { class: "waiting" },
      "⏳ waiting for an image or lookup to finish before the next reply…");
    box.replaceChildren(...[waiting, passed].filter(Boolean));
    return;
  }
  const names = who.map((m) => m.name);
  const verb = (m) => (m.illustrator ? "drawing" : "typing");
  const label = names.length === 1 ? `${names[0]} is ${verb(who[0])}`
    : names.length === 2 ? `${names[0]} and ${names[1]} are typing`
    : `${names.length} people are typing`;
  box.replaceChildren(
    el("span", { class: "faces" }, ...who.map((m) => avatarUrl(m.model)
      ? el("span", { class: "has-pic" }, el("img", { src: avatarUrl(m.model), alt: "" }))
      : el("span", { style: `background:${m.color}` }, initials(m.name)))),
    el("span", {}, label), el("span", { class: "dots" }, el("i"), el("i"), el("i")));
}

function showPass(memberId) {
  const m = memberById(memberId);
  if (!m) return;
  const box = $("#typing");
  $(".passed", box)?.remove();
  const note = el("span", { class: "passed" }, `👀 ${m.name} read it, said nothing`);
  box.append(note);
  setTimeout(() => note.remove(), 2600);
}

// ─── Scrolling ────────────────────────────────────────────────────────

const nearBottom = () => {
  const box = $("#messages");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
};
function scrollBottom() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
  $("#jump").hidden = true;
}
function stickToBottom() { if (nearBottom()) scrollBottom(); }

// ─── Modals ───────────────────────────────────────────────────────────

function openModal(title, content, footer, { onClose } = {}) {
  const dlg = $("#modal");
  $("#modal-body").replaceChildren(el("div", { class: "modal-inner" },
    el("div", { class: "modal-head" }, el("h2", {}, title),
      el("button", { class: "icon-btn", onclick: () => dlg.close(), title: "Close" }, "✕")),
    el("div", { class: "modal-content" }, content),
    footer ? el("div", { class: "modal-foot" }, footer) : null));
  dlg.onclose = onClose || null;
  if (!dlg.open) dlg.showModal();
  return dlg;
}
const closeModal = () => $("#modal").close();

function field(label, input, help) {
  return el("label", { class: "field" }, el("span", {}, label), input, help ? el("small", {}, help) : null);
}
function toggleField(label, checked, help) {
  const input = el("input", { type: "checkbox", class: "switch", checked });
  const node = el("label", { class: "field row" },
    el("div", {}, el("div", { style: "font-weight:600;font-size:13px" }, label), help ? el("small", {}, help) : null), input);
  return [node, input];
}
function rangeField(label, value, min, max, step, fmt, help) {
  const out = el("output", {}, fmt(value));
  const input = el("input", { type: "range", min, max, step, value, oninput: () => (out.textContent = fmt(+input.value)) });
  return [field(label, el("div", { class: "range-row" }, input, out), help), input];
}

// Settings (app-wide)
function openSettings(firstRun = false) {
  const s = state.settings;
  const key = el("input", { type: "password", autofocus: firstRun, placeholder: s.has_key ? `saved (${s.key_hint || "hidden"}) · paste to replace` : "sk-or-…", autocomplete: "off" });
  const username = el("input", { type: "text", value: s.username || "" });
  const thinking = el("select", {}, ...["off", "low", "medium", "high"].map((v) => el("option", { value: v, selected: s.thinking === v }, v)));
  const imageModel = el("input", { type: "text", value: s.image_model || "" });
  const fallback = el("input", { type: "text", value: s.memory_fallback_model || "", placeholder: "none" });
  const [memRow, memory] = toggleField("Memory", s.memory_enabled,
    "Each model remembers past chats in its own words and can !remember / !forget.");
  const [whisperRow, whispers] = toggleField("Show whispers", s.show_whispers, "See the private DMs the AIs send each other.");
  const [timeRow, timeAware] = toggleField("Date & time", s.time_awareness !== false,
    "Tell the AIs the current date and time, and when hours or days pass between messages.");
  const bskyHandle = el("input", { type: "text", value: s.bsky_handle || "", placeholder: "you.bsky.social", autocomplete: "off" });
  const bskyPassword = el("input", { type: "password", autocomplete: "off",
    placeholder: s.has_bsky_password ? "saved · paste to replace, - to remove" : "xxxx-xxxx-xxxx-xxxx" });
  const theme = el("select", {}, ...["system", "light", "dark"].map((v) =>
    el("option", { value: v, selected: (localStorage.getItem("theme") || "system") === v }, v)));

  const content = [
    firstRun ? el("p", { class: "hint" }, "Welcome! Liminal Groupchat runs AIs from OpenRouter, so you need an OpenRouter API key. It stays on this computer.") : null,
    field("OpenRouter API key", key, el("span", {}, "Get one at ", el("a", { href: "https://openrouter.ai/keys", target: "_blank" }, "openrouter.ai/keys"), ".")),
    el("div", { class: "row2" }, field("Your name in the chat", username), field("Thinking", thinking, "How hard models think before replying.")),
    memRow, whisperRow, timeRow,
    el("div", { class: "row2" }, field("Image model", imageModel, "Used for !image."), field("Theme", theme)),
    field("Memory fallback model", fallback,
      "If a model's provider refuses its memory requests, this model writes its memories for it, in its voice. Leave empty to skip them instead."),
    el("div", { class: "row2" }, field("Bluesky handle", bskyHandle), field("Bluesky app password", bskyPassword)),
    el("small", { class: "hint" }, "Optional. Lets !bsky search posts (reading someone's posts with !bsky \"@handle\" works without it). It only reads. Make an app password at ",
      el("a", { href: "https://bsky.app/settings/app-passwords", target: "_blank" }, "bsky.app/settings/app-passwords"), "."),
  ];
  const save = el("button", { class: "btn primary", onclick: async () => {
    const values = { username: username.value.trim() || "you", thinking: thinking.value,
      image_model: imageModel.value.trim(), memory_enabled: memory.checked, show_whispers: whispers.checked, time_awareness: timeAware.checked,
      memory_fallback_model: fallback.value.trim(), bsky_handle: bskyHandle.value.trim().replace(/^@/, "") };
    if (bskyPassword.value.trim()) values.bsky_app_password = bskyPassword.value.trim();
    if (key.value.trim()) values.api_key = key.value.trim();
    if (firstRun && !values.api_key && !s.has_key) return toast("Paste your OpenRouter key first");
    setTheme(theme.value);
    await api("POST", "/api/settings", values);
    closeModal();
    if (firstRun && !members().length) quickCast();
  } }, firstRun ? "Let's go" : "Save");
  openModal(firstRun ? "Welcome 👋" : "Settings", content, [save]);
}

// Chat settings (per chat)
function openChatSettings() {
  const cs = state.chat.settings;
  const mode = el("select", {}, ...Object.keys(MODE_HINTS).map((v) =>
    el("option", { value: v, selected: cs.mode === v }, { natural: "Natural", everyone: "Everyone decides", round_robin: "Round robin" }[v])));
  const [paceField, pace] = rangeField("Pace", cs.pace, 0, 8, 0.5, (v) => `${v}s`, "Pause between messages.");
  const [overlapField, overlap] = rangeField("Crosstalk", cs.overlap, 0, 0.6, 0.05, (v) => `${Math.round(v * 100)}%`,
    "Natural mode: how often two AIs reply at the same time.");
  const perPlay = el("input", { type: "number", min: 0, value: cs.messages_per_play });
  const cap = el("input", { type: "number", min: 0, step: 0.25, value: cs.spend_cap });
  const prompt = el("textarea", { rows: 10 }, cs.room_prompt || "");
  const save = el("button", { class: "btn primary", onclick: async () => {
    await api("PATCH", "/api/chat", { settings: {
      mode: mode.value, pace: +pace.value, overlap: +overlap.value,
      messages_per_play: Math.max(0, parseInt(perPlay.value || "0", 10)),
      spend_cap: Math.max(0, parseFloat(cap.value || "0")), room_prompt: prompt.value } });
    closeModal();
  } }, "Save");
  openModal("Chat settings", [
    field("Who talks when", mode, "Natural: a quick local guess picks who replies next, weighted toward whoever was mentioned or has been quiet, and they can pass. Everyone decides: every AI sees every message and chooses whether to reply (about one call per AI per message)."),
    el("div", { class: "row2" }, paceField, overlapField),
    el("div", { class: "row2" },
      field("Messages per Play", perPlay, "Autoplay stops after this many. 0 = no limit."),
      field("Spend cap ($)", cap, "Autoplay stops when this chat has cost this much. 0 = no cap.")),
    field("Room prompt", prompt, "The vibe of the room. Commands and who's here are added automatically."),
  ], [save]);
}

// Model picker
async function loadModels() {
  if (!state.models) state.models = await api("GET", "/api/models");
  return state.models;
}

const price = (m) => m.input == null ? "" : (m.input === 0 && m.output === 0) ? "free"
  : `$${m.input} in · $${m.output} out`;

function webUsesText(uses) {
  if (!uses) return "";
  const n = (k, word) => (uses[k] ? `${word} ${uses[k]}×` : "");
  return [n("searches", "searched"), n("fetches", "opened pages")].filter(Boolean).join(", ");
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

function prettyName(m) {
  return (m.name || m.id).replace(/^[^:]+:\s*/, "");
}

async function addMember() {
  const search = el("input", { class: "search", placeholder: "Search models… (e.g. opus, gemini, kimi)", autofocus: true });
  const list = el("div", { class: "model-list" }, el("p", { class: "hint" }, "Loading models…"));
  const name = el("input", { type: "text", placeholder: "defaults to the model's name" });
  const [illoRow, illo] = toggleField("Illustrator",
    false, "Draws the chat instead of talking: an image every few messages, or when someone @'s them.");
  const every = el("input", { type: "number", min: 1, value: 30 });
  const everyField = field("Draws every ~N messages", every);
  const [webRow, web] = toggleField("🌐 Web access", true, WEB_HELP);
  const syncIllo = () => { everyField.hidden = !illo.checked; webRow.hidden = illo.checked; };
  illo.onchange = syncIllo;
  syncIllo();
  let chosen = null;
  const add = el("button", { class: "btn primary", disabled: true, onclick: async () => {
    await api("POST", "/api/members", { model: chosen.id, name: name.value.trim() || prettyName(chosen),
      illustrator: illo.checked, draw_every: Math.max(1, parseInt(every.value || "30", 10)), web: web.checked && !illo.checked });
    closeModal();
  } }, "Add to chat");

  openModal("Add someone", [search, list,
    field("Nickname", name), webRow, illoRow, everyField], [add]);

  let models;
  try { models = await loadModels(); } catch { list.replaceChildren(el("p", { class: "hint" }, "Couldn't load models from OpenRouter.")); return; }
  const featured = new Set(FEATURED);
  const row = (m) => el("div", {
    class: `model-row ${chosen?.id === m.id ? "selected" : ""}`,
    onclick: () => {
      chosen = m; add.disabled = false; name.placeholder = prettyName(m);
      illo.checked = !!m.draws || /\/(muse-image|seedream)/.test(m.id); syncIllo();
      draw();
    },
    ondblclick: () => add.click(),
  },
    el("div", {}, el("div", { class: "mname" }, featured.has(m.id) ? el("span", { class: "star" }, "★ ") : null, prettyName(m)),
      el("div", { class: "mid" }, m.id)),
    el("div", { class: "price" }, m.draws ? el("div", {}, "🎨 draws") : null,
      m.custom ? "not in OpenRouter's list" : price(m),
      m.context ? el("div", {}, `${Math.round(m.context / 1000)}k ctx`) : null));
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    let rows = models.filter((m) => !q || m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q));
    rows.sort((a, b) => (featured.has(b.id) - featured.has(a.id))
      || (featured.has(a.id) ? FEATURED.indexOf(a.id) - FEATURED.indexOf(b.id) : 0));
    if (!q) rows = rows.slice(0, 200);  // searching shows every match
    const nodes = rows.map(row);
    // Unlisted or brand-new models: type the full ID to use it anyway
    if (q.includes("/") && !models.some((m) => m.id.toLowerCase() === q)) {
      nodes.unshift(row({ id: search.value.trim(), name: search.value.trim().split("/").pop(), custom: true }));
    }
    list.replaceChildren(...nodes);
    if (!nodes.length) list.append(el("p", { class: "hint" }, "No models match. Type a full model ID (like openai/gpt-6-astra) to use one that isn't listed."));
  };
  search.oninput = draw;
  draw();
}

async function quickCast() {
  const have = new Set(members().map((m) => m.model));
  let added = 0;
  for (const [model, name, extra] of STARTER_CAST) {
    if (have.has(model)) continue;
    await api("POST", "/api/members", { model, name, ...extra });
    added++;
  }
  toast(added ? `Invited ${added} AIs. Press Play!` : "They're all here already.");
}

// Member editor
function editMember(m) {
  const name = el("input", { type: "text", value: m.name });
  const [tempField, temp] = rangeField("Temperature", m.temperature ?? 1, 0, 2, 0.05, (v) => v.toFixed(2), "Higher = more chaotic.");
  const [muteRow, muted] = toggleField("Muted", m.muted, "Muted members stay in the chat but don't talk.");
  const [illoRow, illo] = toggleField("Illustrator", !!m.illustrator,
    "Draws the chat instead of talking: an image every few messages, or when someone @'s them.");
  const every = el("input", { type: "number", min: 1, value: m.draw_every || 30 });
  const everyField = field("Draws every ~N messages", every);
  const [webRow, web] = toggleField("🌐 Web access", !!m.web, WEB_HELP);
  illo.onchange = () => { everyField.hidden = !illo.checked; webRow.hidden = illo.checked; };
  illo.onchange();
  const remove = el("button", { class: "btn danger", onclick: async () => {
    if (!confirm(`Remove ${m.name} from this chat?`)) return;
    await api("DELETE", `/api/members/${m.id}`);
    closeModal();
  } }, "Remove");
  const memoryBtn = el("button", { class: "btn", onclick: () => showMemory(m) }, "🧠 Memories");
  const save = el("button", { class: "btn primary", onclick: async () => {
    await api("PATCH", `/api/members/${m.id}`, { name: name.value.trim() || m.name, temperature: +temp.value, muted: muted.checked,
      illustrator: illo.checked, draw_every: Math.max(1, parseInt(every.value || "30", 10)), web: web.checked && !illo.checked });
    closeModal();
  } }, "Save");
  const picInput = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif", hidden: true });
  const hasPic = !!avatarUrl(m.model);
  const pic = el("div", { class: "pic-row" },
    el("button", { class: "pic-btn", type: "button", title: "Change picture", onclick: () => picInput.click() },
      avatar(m.name, m.color, m.model, "avatar big")),
    el("div", {},
      el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" },
        el("button", { class: "btn", type: "button", onclick: () => picInput.click() }, hasPic ? "Change picture" : "Add picture"),
        hasPic ? el("button", { class: "btn ghost", type: "button", onclick: async () => {
          await api("DELETE", `/api/avatars?model=${encodeURIComponent(m.model)}`);
          editMember(memberById(m.id) || m);
        } }, "Remove") : null),
      el("div", { class: "hint", style: "margin-top:4px;font-family:var(--mono)" }, m.model),
      el("small", { class: "hint" }, "Used for this model in every chat.")),
    picInput);
  picInput.onchange = async () => {
    const file = picInput.files[0];
    if (!file) return;
    try {
      await api("POST", "/api/avatars", { model: m.model, data: await squareImage(file) });
      editMember(memberById(m.id) || m);
    } catch {}
  };
  openModal(m.name, [
    pic,
    el("div", { class: "row2" }, field("Name", name), tempField),
    muteRow, webRow, illoRow, everyField,
  ], [remove, state.settings.memory_enabled ? memoryBtn : null, el("span", { class: "spacer" }), save]);
}

async function showMemory(m) {
  const box = el("div", { style: "display:flex;flex-direction:column;gap:8px" }, el("p", { class: "hint" }, "Loading…"));
  openModal(`${m.name}'s memories`, [
    el("p", { class: "hint" }, `Everything ${shortModel(m.model)} remembers from group chats, written by the model itself. Memories are shared by every chat this model is in.`),
    box]);
  const draw = (items) => {
    const kinds = { note: "note", 1: "memory", 2: "older memory", 3: "distant memory" };
    const shown = items.filter((x) => !x.faded).sort((a, b) => (b.when || "").localeCompare(a.when || ""));
    box.replaceChildren(...shown.map((x) => el("div", { class: `memory ${x.forgotten ? "forgotten" : ""}` },
      el("div", { class: "mhead" },
        el("span", { class: "tag" }, kinds[x.kind === "note" ? "note" : x.level] || "memory"),
        el("span", {}, x.when ? new Date(x.when).toLocaleString() : ""),
        x.written_by ? el("span", { class: "tag", title: "Written on its behalf, because its own memory requests were refused" },
          `✍️ ${shortModel(x.written_by)}`) : null,
        x.forgotten ? el("span", {}, "· forgotten") : el("button", { class: "btn", onclick: async () => {
          draw(await api("POST", "/api/memory/forget", { model: m.model, id: x.id }));
        } }, "Forget")),
      el("div", { class: "mtext" }, x.content))));
    if (!shown.length) box.append(el("p", { class: "hint" }, "Nothing yet. Memories form as the chat goes on."));
  };
  draw(await api("GET", `/api/memory?model=${encodeURIComponent(m.model)}`));
}

// ─── Attachments ──────────────────────────────────────────────────────

function attach(file) {
  if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) return toast("Images only: PNG, JPEG, WebP or GIF");
  state.attachment = file;
  const url = URL.createObjectURL(file);
  $("#attachment").replaceChildren(
    el("img", { src: url, alt: "" }),
    el("span", { class: "hint" }, file.name || "pasted image"),
    el("button", { class: "icon-btn", type: "button", title: "Remove", onclick: clearAttachment }, "✕"));
  $("#attachment").hidden = false;
  $("#input").focus();
}

function clearAttachment() {
  state.attachment = null;
  $("#attachment").hidden = true;
  $("#attachment").replaceChildren();
}

// Big photos are scaled down before upload: they're re-sent to vision models
// with every reply, so a 12 MP phone photo would be slow and costly.
async function shrinkImage(file, max = 1568) {
  const dataUrl = await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
  if (file.type === "image/gif") return dataUrl;  // keep animation
  const img = await new Promise((resolve, reject) => {
    const i = new Image();
    i.onload = () => resolve(i);
    i.onerror = reject;
    i.src = dataUrl;
  });
  const scale = Math.min(1, max / Math.max(img.width, img.height));
  if (scale === 1 && file.size < 1.5e6) return dataUrl;
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(img.width * scale);
  canvas.height = Math.round(img.height * scale);
  canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL(file.type === "image/png" ? "image/png" : "image/jpeg", 0.88);
}

// Profile pictures: centre-cropped to a square and scaled to 256px.
// GIFs are kept as they are, so animated ones still move.
async function squareImage(file, size = 256) {
  if (file.type === "image/gif") return shrinkImage(file);
  const dataUrl = await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
  const img = await new Promise((resolve, reject) => {
    const i = new Image();
    i.onload = () => resolve(i);
    i.onerror = reject;
    i.src = dataUrl;
  });
  const side = Math.min(img.width, img.height);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = Math.min(size, side);
  canvas.getContext("2d").drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side,
    0, 0, canvas.width, canvas.height);
  return canvas.toDataURL(file.type === "image/png" ? "image/png" : "image/jpeg", 0.9);
}

function zoom(src) {
  const lb = $("#lightbox");
  $("img", lb).src = src;
  lb.hidden = false;
}

function setTheme(theme) {
  localStorage.setItem("theme", theme);
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = theme;
}

function onboarding() {
  if (!state.settings.has_key && !$("#modal").open) openSettings(true);
}

// ─── Drawers (mobile) ─────────────────────────────────────────────────

function closeDrawers() {
  $("#sidebar").classList.remove("open");
  $("#cast").classList.remove("open");
  $("#scrim").hidden = true;
}
function toggleDrawer(id) {
  const open = !$(id).classList.contains("open");
  closeDrawers();
  if (open) { $(id).classList.add("open"); $("#scrim").hidden = false; }
}

// ─── Wiring ───────────────────────────────────────────────────────────

function wire() {
  setTheme(localStorage.getItem("theme") || "system");
  $("#new-chat").onclick = () => { api("POST", "/api/chats"); closeDrawers(); };
  $("#open-settings").onclick = () => openSettings();
  $("#open-chat-settings").onclick = openChatSettings;
  $("#add-member").onclick = addMember;
  $("#remove-all").onclick = () => {
    const n = members().length;
    if (n && confirm(`Remove all ${n} members from this chat? Their memories are kept.`)) api("DELETE", "/api/members");
  };
  $("#toggle-sidebar").onclick = () => toggleDrawer("#sidebar");
  $("#toggle-cast").onclick = () => toggleDrawer("#cast");
  $("#scrim").onclick = closeDrawers;
  $("#lightbox").onclick = () => ($("#lightbox").hidden = true);
  $("#jump").onclick = scrollBottom;
  $("#messages").addEventListener("scroll", () => {
    const box = $("#messages");
    if (nearBottom()) $("#jump").hidden = true;
    if (box.scrollTop < 60 && $(".earlier", box)) showEarlier();
  });

  $("#play").onclick = () => api("POST", `/api/control/${state.running ? "pause" : "play"}`);
  $("#step").onclick = () => api("POST", "/api/control/step");
  $("#mode").onchange = (e) => api("PATCH", "/api/chat", { settings: { mode: e.target.value } });

  const title = $("#chat-title");
  title.addEventListener("keydown", (e) => { if (e.key === "Enter") title.blur(); if (e.key === "Escape") { title.value = state.chat.title; title.blur(); } });
  title.addEventListener("blur", () => { if (state.chat && title.value.trim() !== state.chat.title) api("PATCH", "/api/chat", { title: title.value }); });

  const input = $("#input");
  const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; };
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#composer").onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    const file = state.attachment;
    if (!text && !file) return;
    input.value = ""; grow();
    clearAttachment();
    let image = null;
    if (file) {
      try { image = (await api("POST", "/api/upload", { data: await shrinkImage(file) })).image; }
      catch { return; }
    }
    api("POST", "/api/messages", { text, image });
  };
  $("#attach").onclick = () => $("#file").click();
  $("#file").onchange = (e) => { if (e.target.files[0]) attach(e.target.files[0]); e.target.value = ""; };
  input.addEventListener("paste", (e) => {
    const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith("image/"));
    if (file) { e.preventDefault(); attach(file); }
  });
  const main = $(".main");
  main.addEventListener("dragover", (e) => {
    if ([...e.dataTransfer.items].some((i) => i.type.startsWith("image/"))) { e.preventDefault(); main.classList.add("dropping"); }
  });
  main.addEventListener("dragleave", (e) => { if (!main.contains(e.relatedTarget)) main.classList.remove("dropping"); });
  main.addEventListener("drop", (e) => {
    main.classList.remove("dropping");
    const file = [...e.dataTransfer.files].find((f) => f.type.startsWith("image/"));
    if (file) { e.preventDefault(); attach(file); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("#lightbox").hidden = true; closeDrawers(); }
  });
}

wire();
connect();
