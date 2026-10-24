/* clm-mem studio — vanilla SPA hitting the serve REST + MCP surface.
   Context is shown as hints (badges, chips, collapsed previews), never raw. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  ws: null,            // active workspace name
  seenSeqs: new Set(), // event seqs already rendered in chat chips
  events: [],          // timeline (deduped by seq)
  tagFilter: "",
  sending: false,
  lastSeq: 0,          // highest tool-activity seq rendered as a card
  cards: {},           // seq -> card element (live updates while sending)
  fullCache: null,     // promise cache for expanded tool details
  approvals: {},       // approval id -> pending card
  chats: [],           // transcript keys per project ("chat-001", …)
  chat: "chat-001",    // active chat slot within the workspace session
  drafts: {},          // per-chat composer draft (never shared between chats)
};

/* ---------- plumbing ---------- */
async function req(method, path, body, headers = {}) {
  const opts = { method, headers: { "Content-Type": "application/json", ...headers } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const ct = r.headers.get("Content-Type") || "";
  let data = null;
  if (ct.includes("json")) data = await r.json();
  else data = await r.text().catch(() => null);
  if (!r.ok) {
    const msg = (data && data.error && data.error.message) || `${r.status}`;
    throw new Error(msg);
  }
  return data;
}
function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = ""), err ? 4000 : 2000);
}
const sid = () => `ws:${state.ws}`;
/* One chat is a transcript slot inside a workspace session: the server keeps
   one memory harness per project, but each chat owns its own messages,
   activity lane, approvals and (see draftKey) composer draft. */
const CHAT_RE = /^chat-\d{3}$/;
const chatId = () => state.chat;
function setChatFor(s) {
  const m = new Map();
  for (const c of s.split("/")) if (CHAT_RE.test(c)) m.set(c, true);
  return m;
}
const draftKey = () => `${sid()}/${state.chat}`;
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- scrolling ------------------------------------------------------
   A panel scrolls itself and never an ancestor. The DOM "scroll into view"
   helper walks the whole ancestor chain (it is banned here by a test), which
   would drag the side panels along with the transcript. */
const atBottom = (el, slack = 90) =>
  el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
function scrollToBottom(el, smooth = false) {
  if (!el) return;
  el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}
/* rebuild a list without yanking the reader's position: background refreshes
   must not send someone back to the top of a panel they are reading. For
   timelines that grow at the head (`prepend`), anchor to the distance from
   the bottom so visible content stays put; readers parked at the very top
   stay parked and watch the new entries land. */
function keepScroll(el, fn, prepend = false) {
  if (!el) return fn();
  const top = el.scrollTop;
  const fromBottom = el.scrollHeight - top;
  fn();
  el.scrollTop = prepend && top > 0 ? el.scrollHeight - fromBottom : top;
}

// bring `el` to the top of its own scroll container (offset-aware, no chaining)
function revealIn(container, el, smooth = true) {
  if (!container || !el) return;
  const max = container.scrollHeight - container.clientHeight;
  if (max <= 0) return;
  const dy = el.getBoundingClientRect().top - container.getBoundingClientRect().top;
  if (Math.abs(dy) < 1) return;
  const top = Math.max(0, Math.min(max, container.scrollTop + dy - 8));
  container.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
}

/* ---------- narrow screens: one panel per view ---------- */
const VIEWS = ["side", "chat", "insp"];
function setView(view) {
  if (!VIEWS.includes(view)) return;
  for (const v of VIEWS) document.body.classList.remove(`view-${v}`);
  document.body.classList.add(`view-${view}`);
  document.querySelectorAll("#viewBtns button").forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  if (view === "insp") { paintContext(); loadSandbox(); }
  if (view === "side") loadWorkspaces(false);
  if (view === "chat") {
    const t = $("input");
    // re-attaching the field must not resurrect another chat's draft
    if (t.dataset.key !== draftKey()) { t.value = ""; autoGrow(); }
    t.focus();
  }
}

/* ---------- header pills ---------- */
async function loadHealth() {
  try {
    const h = await req("GET", "/health");
    const pills = $("pills");
    pills.innerHTML = "";
    const mk = (label, cls, title) => {
      const el = document.createElement("button");
      el.className = "pill " + cls; el.textContent = label; el.title = title || "";
      return pills.appendChild(el);
    };
    mk(`● ${h.model}`, "ok", `${Object.keys(h.processes || {}).length} backend process(es) running`);
    const mcp = `${location.origin}/mcp`;
    const p = mk("⚡ MCP " + mcp, "", "click to copy — add to any MCP client");
    p.onclick = () => { navigator.clipboard?.writeText(mcp); toast("MCP endpoint copied"); };
    $("mcpLabel").textContent = "/mcp";
    $("mcpLabel").onclick = () => { navigator.clipboard?.writeText(mcp); toast("copied"); };
  } catch (e) {
    toast("server unreachable: " + e.message, true);
  }
}

/* ---------- workspaces (projects) ---------- */
async function loadWorkspaces(selectFirst = false) {
  const ul0 = $("wsList");
  if (ul0) ul0.classList.add("busy");
  let d;
  try { d = await req("GET", "/v1/clm/workspaces"); } finally {
    setTimeout(() => { const u = $("wsList"); if (u) u.classList.remove("busy"); }, 250);
  }
  if (selectFirst && !d.workspaces.length) {
    await req("POST", "/v1/clm/workspaces", { name: "default" });
    d = await req("GET", "/v1/clm/workspaces");
  }
  const cur = d.workspaces.find((w) => w.name === state.ws);
  state.chats = ((cur && cur.chats) || []).filter((c) => CHAT_RE.test(c));
  renderWorkspaces(d.workspaces);
  if (selectFirst && d.workspaces.length) {
    selectWorkspace(d.workspaces[0].name, true);
  }
}
function renderWorkspaces(list) {
  const ul = $("wsList");
  keepScroll(ul, () => {
    ul.innerHTML = "";
    for (const w of list) {
      const li = document.createElement("li");
      li.className = "ws-item" + (w.name === state.ws ? " active" : "");
      li.dataset.ws = w.name;
      const pct = w.active ? Math.min(100, Math.round((100 * w.tokens) / w.budget)) : 0;
      const frac = w.active ? Math.min(1, w.tokens / (w.budget || 1)) : 0;
      const chats = (w.chats || []).slice().sort();
      li.innerHTML = `
        <div class="ws-name"><span class="t"><span class="ws-icon"></span>${esc(w.name)}</span>
          <button class="del" title="delete project">✕</button></div>
        ${(w.chats && w.chats.length) ? `<ul class="chat-list"></ul>` : ""}
        <div class="ws-meter"><i style="width:${pct}%"
          class="${frac === 0 ? "zero" : ""}${pct > 90 ? " over" : ""}"></i></div>
        ${w.active ? `<div class="ws-meta">
          <span><i class="dot long"></i>${w.counts.longterm}</span>
          <span><i class="dot short"></i>${w.counts.shortterm}</span>
          <span><i class="dot transient"></i>${w.counts.transient}</span>
          <span>${w.tokens} tok</span><span>${w.archive} archived</span></div>` : `<div class="ws-meta">idle</div>`}`;
      li.querySelector(".del").onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`Delete project "${w.name}" and all its memory files?`)) return;
        try {
          await req("DELETE", `/v1/clm/workspaces/${encodeURIComponent(w.name)}`);
          if (state.ws === w.name) { state.ws = null; $("messages").innerHTML = ""; }
          toast(`project ${w.name} deleted`);
          loadWorkspaces(false);
        } catch (e2) { toast(e2.message, true); }
      };
      const cl = li.querySelector(".chat-list");
      if (cl) {
        cl.onclick = (ev2) => ev2.stopPropagation();
        for (const c of chats) {
          const ci = document.createElement("li");
          ci.className = "chat-item" + (w.name === state.ws && c === state.chat ? " active" : "");
          ci.textContent = `✦ ${c}`;
          ci.tabIndex = 0;
          ci.setAttribute("role", "button");
          const pickChat = async () => {
            if (w.name !== state.ws) { selectWorkspace(w.name, true); setView("chat"); }
            await openChat(w.name, c);
          };
          ci.onclick = pickChat;
          ci.onkeydown = (ev2) => {
            if (ev2.key === "Enter" || ev2.key === " ") { ev2.preventDefault(); pickChat(); }
          };
          cl.appendChild(ci);
        }
      }
      // per-project dyson ring: how much shell is built (fraction of budget)
      const iconSlot = li.querySelector(".ws-icon");
      if (window.dyson && iconSlot) {
        iconSlot.appendChild(frac >= 1 ? dyson.done(16) : dyson.progress(16, frac));
      }
      const pick = () => { selectWorkspace(w.name, true); setView("chat"); };
      li.onclick = pick;
      // keyboard: rows are buttons for a11y; picking one leaves the drawer
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      if (w.name === state.ws) li.setAttribute("aria-current", "true");
      li.onkeydown = (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); }
      };
      ul.appendChild(li);
    }
  });
}

/* ---------- chats: open / new / transcript restore ---------- */
function clearChatView() {
  state.seenSeqs.clear();
  Object.values(state.cards).forEach((c) => c.remove());
  state.cards = {}; state.approvals = {};
  state.fullCache = null; state.lastSeq = 0;
  state.events = [];   // the timeline belongs to the chat being viewed
  $("messages").innerHTML = "";
}
function captureDraft() {
  if (!state.ws) return;
  const t = $("input");
  // only fields currently holding *this* chat's draft may stash it
  if (t.dataset.key !== draftKey()) return;
  state.drafts[draftKey()] = t.value;
}
/* open one chat slot: show only its turns, keep every other chat's text,
   drafts and approvals where they are */
async function openChat(name, key) {
  if (!name || !CHAT_RE.test(key)) return;
  const wsSwitch = name !== state.ws;
  captureDraft();
  if (wsSwitch) await selectWorkspaceInternal(name, true);
  if (state.ws !== name) return;
  state.chat = key;
  clearChatView();
  try {
    // register the slot server-side so an empty chat still lists itself
    await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/chat-open`,
      { chat: key });
    const snap = await req("GET", `/v1/clm/sessions/${encodeURIComponent(sid())}/snapshot` + chatQs());
    restoreTranscript(snap);
    renderActivity((snap.activity || []).filter(
      (a) => setChatFor(state.chat).has(a.chat)).slice().reverse());
    state.lastSeq = Math.max(0, ...(snap.activity || [])
      .filter((a) => setChatFor(state.chat).has(a.chat)).map((a) => a.seq));
    for (const ev of (snap.events || []).slice().reverse()) rememberEvent(ev, true);
  } catch (e) { /* no session yet */ }
  const t = $("input");
  t.value = state.drafts[draftKey()] || "";
  t.dataset.key = draftKey();
  autoGrow();
  $("sidLabel").textContent = `${sid()}/${state.chat}`;
  scrollToBottom($("messages"));
  renderChatsInList();
}
function renderChatsInList() {
  document.querySelectorAll(".chat-item.active").forEach((el) => el.classList.remove("active"));
  if (!state.ws) return;
  const sel = `.ws-item[data-ws="${CSS.escape(state.ws)}"] .chat-item`;
  document.querySelectorAll(sel).forEach((el) => {
    if (el.textContent.replace(/^✦ /, "") === state.chat) el.classList.add("active");
  });
}

/* ---------- workspace selection & transcript restore ---------- */
async function selectWorkspace(name, refreshPanels = true) {
  captureDraft();
  await selectWorkspaceInternal(name, refreshPanels);
  // land on the newest chat so a fresh visit continues where the project left off
  const chats = state.chats.filter((c) => CHAT_RE.test(c));
  if (chats.length) {
    const last = chats.sort()[chats.length - 1];
    if (last !== "chat-001") await openChat(state.ws, last);
  }
}
async function selectWorkspaceInternal(name, refreshPanels = true) {
  state.ws = name;
  state.chat = "chat-001";
  clearChatView();
  try {
    const snap = await req("GET", `/v1/clm/sessions/${encodeURIComponent(sid())}/snapshot` + chatQs());
    restoreTranscript(snap);
    // recent agent activity is part of the restored transcript
    const only = setChatFor(state.chat);
    renderActivity((snap.activity || [])
      .filter((a) => only.has(a.chat)).slice().reverse());
    state.lastSeq = Math.max(0, ...(snap.activity || [])
      .filter((a) => only.has(a.chat)).map((a) => a.seq));
    if (refreshPanels) { paintContext(snap); primeTuning(snap); }
    // seed the events timeline from server history (chips stay chat-side only)
    for (const ev of (snap.events || []).slice().reverse()) rememberEvent(ev, true);
  } catch (e) {
    // no session yet — fine, first message will create it
    addSystem(`project "${esc(name)}" ready — memory budget will apply from your serve settings`);
  }
  const t = $("input");
  t.value = state.drafts[draftKey()] || "";
  t.dataset.key = draftKey();
  autoGrow();
  // the restored transcript opens on its newest line
  scrollToBottom($("messages"));
  loadTools(); loadSandbox();
  loadWorkspaces(false);
}

function restoreTranscript(snap) {
  const only = setChatFor(state.chat);
  const byTurn = new Map();
  for (const b of snap.blocks) {
    if (b.role === "system") continue;
    // chat isolation: turns belonging to other chats stay in the harness
    // memory but never render inside this transcript view (the server tags
    // every block with its chat id; legacy blocks arrive as chat-001)
    if (!only.has(b.chat || "chat-001")) continue;
    const t = byTurn.get(b.turn) || { user: [], ai: [] };
    if (b.role === "user") t.user.push(b);
    else if (b.role === "assistant") t.ai.push(b);
    byTurn.set(b.turn, t);
  }
  // replay without per-message scrolling — selectWorkspace lands on the
  // newest line once, which keeps long restores cheap (one reflow, not n)
  for (const [turn, t] of [...byTurn.entries()].sort((a, b) => a[0] - b[0])) {
    if (t.user.length)
      addMessage("user", joinBlocks(t.user), tagChips(t.user), { stick: false });
    if (t.ai.length)
      addMessage("ai", joinBlocks(t.ai), tagChips(t.ai), { stick: false });
  }
}
const joinBlocks = (bs) => bs.map((b) => b.content).join("\n\n");
function tagChips(blocks) {
  const counts = {};
  for (const b of blocks) counts[b.tag] = (counts[b.tag] || 0) + 1;
  return Object.entries(counts).map(([tag, n]) => ({ cls: tagShort(tag), label: `${n}× ${tag}` }));
}
const tagShort = (t) => t === "longterm" ? "long" : t === "shortterm" ? "short" : "transient";

/* ---------- messages ---------- */
function addMessage(who, text, chips = [], opts = {}) {
  // follow the newest line only when the reader was already down there —
  // a background event must not yank someone scrolling through history.
  const m = $("messages");
  const stick = opts.stick === undefined ? atBottom(m) : opts.stick;
  const wrap = document.createElement("div");
  wrap.className = "msg " + (who === "user" ? "user" : "ai");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  if (chips.length) {
    const meta = document.createElement("div");
    meta.className = "meta";
    for (const c of chips) {
      const s = document.createElement("span");
      s.className = "chip " + c.cls; s.textContent = c.label;
      meta.appendChild(s);
    }
    wrap.appendChild(meta);
  }
  m.appendChild(wrap);
  if (stick) scrollToBottom(m, !!opts.smooth);
  return wrap;
}
function addSystem(text) {
  const el = document.createElement("div");
  el.className = "system-line"; el.innerHTML = text;
  $("messages").appendChild(el);
}

/* ---------- chat send ---------- */
async function send() {
  const input = $("input");
  // the field always belongs to one chat; sending must never burn another
  // chat's draft (see dataset.key, kept in sync by openChat/setView)
  if (input.dataset.key !== draftKey()) return;
  const wsAt = state.ws, chatAt = state.chat;
  const text = input.value.trim();
  if (!text || state.sending) return;
  delete state.drafts[draftKey()];
  input.value = ""; autoGrow();
  state.sending = true; $("sendBtn").disabled = true;
  addMessage("user", text, [], { stick: true, smooth: true });
  // live activity lane: tool cards appear here as the turn runs
  const actBox = document.createElement("div");
  actBox.className = "activity-turn";
  $("messages").appendChild(actBox);
  const typing = document.createElement("div");
  typing.className = "msg ai";
  typing.appendChild(window.dyson ? dyson.spinner(26) : Object.assign(
    document.createElement("div"), { className: "typing", innerHTML: `<i></i><i></i><i></i>` }));
  $("messages").appendChild(typing);
  scrollToBottom($("messages"));
  $("statusLine").textContent = "thinking…";
  const timer = setInterval(() => {
    pollActivity(actBox);
    pollApprovals();
  }, 800);
  try {
    const resp = await req("POST", "/v1/chat/completions", {
      model: (state.model || null), messages: [{ role: "user", content: text }],
    }, { "X-Cllm-Mem-Chat": chatId(), "X-Cllm-Mem-Session": sid() });
    clearInterval(timer);
    await pollActivity(actBox);          // catch the final state of each card
    if (!actBox.childNodes.length) actBox.remove();   // nothing happened
    if (window.dyson) {   // panels fuse as the answer lands, then it fades out
      const d = dyson.done(26); d.classList.add("flash");
      typing.replaceChildren(d);
      setTimeout(() => typing.remove(), 450);
    } else typing.remove();
    if (state.ws !== wsAt || state.chat !== chatAt) {
      // the reader switched chats mid-turn; the answer lives in harness
      // memory and will restore with its own transcript view when visited
    } else {
    const reply = resp.choices[0].message.content;
    const chips = [];
    for (const ev of (resp.clm_mem?.events || [])) {
      // tool events render as cards in the activity lane, not as chips
      if (ev.kind.startsWith("tool")) continue;
      if (state.seenSeqs.has(ev.seq)) continue;
      state.seenSeqs.add(ev.seq);
      state.events.unshift(ev);
      chips.push({ cls: chipClass(ev.kind), label: evIcon(ev.kind) + " " + ev.text });
    }
    addMessage("ai", reply, chips, { stick: true, smooth: true });
    paintContextFromResp(resp.clm_mem);
    paintContext();               // refresh block list with the new turn
    renderEvents();
    loadWorkspaces(false);
    }
  } catch (e) {
    clearInterval(timer);
    typing.remove();
    if (!actBox.childNodes.length) actBox.remove();
    if (state.ws === wsAt && state.chat === chatAt)
      addSystem(`<span style="color:#f85149">backend error: ${esc(e.message)}</span>`);
  } finally {
    // a slow turn must not disable the composer of an unrelated chat
    const mine = state.ws === wsAt && state.chat === chatAt;
    if (mine) {
      state.sending = false; $("sendBtn").disabled = false;
      $("statusLine").textContent = "ready";
      $("input").focus();
    }
    loadTools(); loadSandbox();
  }
}
const evIcon = (kind) => kind.startsWith("evict") ? "🧹" : kind.startsWith("backstop") ? "🛡"
  : kind.startsWith("maintain") ? "♻️" : kind === "classify" ? "🏷" : kind === "warn" ? "⚠️"
  : kind.startsWith("tool-call") ? "⚡" : kind.startsWith("tool-") ? "⚠️" : "·";

/* ---------- agent tools: composer toggles, live cards, approvals ---------- */
const toolIcon = (t) => ({ web_search: "🌐", web_fetch: "🔗", run_command: "💻",
  run_python: "🐍", write_file: "✏️", read_file: "📄", list_files: "📁" }[t] || "⚡");

async function loadTools() {
  if (!state.ws) return;
  let d;
  try { d = await req("GET", `/v1/clm/sessions/${encodeURIComponent(sid())}/tools`); }
  catch (e) { return; }
  const cats = d.enabled_categories || {};
  $("toolWeb").checked = cats.web === "on";
  $("toolExec").checked = cats.exec === "on";
  $("toolFs").checked = cats.fs === "on";
  $("policySel").value = d.approval_mode || "auto";
  const sb = d.sandbox || {};
  $("toolStatus").textContent = d.active
    ? `${(d.enabled_tools || []).length} tools · ${sb.isolation || ""}`
    : "tools off";
  $("toolStatus").className = "tb-status" + (d.active ? " on" : "");
}

async function setTool(target, on) {
  if (!state.ws) return toast("pick a project first", true);
  try {
    const d = await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/tools`,
      { target, on });
    await loadTools();
    toast(`${target} ${d.enabled_categories[target] || ""}`.trim());
  } catch (e) { toast(e.message, true); }
}

async function setPolicy(mode) {
  if (!state.ws) return;
  try {
    await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/tools`,
      { approval_mode: mode });
    toast(`approval policy = ${mode}`);
  } catch (e) { toast(e.message, true); }
}

function toolCard(entry) {
  const wrap = document.createElement("div");
  wrap.className = "tool-card " + (entry.status || "running");
  wrap.dataset.seq = entry.seq;
  const head = document.createElement("button");
  head.className = "tc-head";
  head.innerHTML = `<span class="tc-icon">${toolIcon(entry.tool)}</span>
    <span class="tc-sum">${esc(entry.summary || entry.tool)}</span>
    <span class="tc-state">${entry.status === "running"
      ? "running…" : entry.status === "ok" ? "done"
      : entry.status === "denied" ? "denied" : "error"}</span>`;
  const body = document.createElement("pre");
  body.className = "tc-body"; body.hidden = true;
  head.onclick = async () => {
    if (!body.hidden) { body.hidden = true; return; }
    body.hidden = false;
    if (body.textContent) return;
    const full = await fullActivity();
    const e = full.find((x) => x.seq === entry.seq);
    body.textContent = ((e && e.detail) || "(no output)").slice(0, 6000);
  };
  wrap.appendChild(head); wrap.appendChild(body);
  return wrap;
}

const chatQs = () => `?chats=${encodeURIComponent(state.chat)}`;
async function fullActivity() {
  if (!state.fullCache) {
    state.fullCache = req("GET",
      `/v1/clm/sessions/${encodeURIComponent(sid())}/activity?limit=60&full=1` +
      `&chats=${encodeURIComponent(state.chat)}`)
      .then((d) => d.activity || [])
      .catch(() => []);
  }
  return state.fullCache;
}

function renderActivity(entries, container) {
  const host = container || $("messages");
  const scroller = $("messages");
  const pinned = state.sending || atBottom(scroller);
  let added = false;
  for (const a of entries) {
    if (a.seq <= state.lastSeq && state.cards[a.seq]) continue;
    if (state.cards[a.seq]) {   // live update of a running card
      const c = state.cards[a.seq];
      c.className = "tool-card " + a.status;
      c.querySelector(".tc-sum").textContent = a.summary || a.tool;
      c.querySelector(".tc-state").textContent = a.status === "running"
        ? "running…" : a.status === "ok" ? "done"
        : a.status === "denied" ? "denied" : "error";
      continue;
    }
    if (a.seq <= state.lastSeq) continue;
    const card = toolCard(a);
    host.appendChild(card);
    state.cards[a.seq] = card;
    state.lastSeq = Math.max(state.lastSeq, a.seq);
    added = true;
  }
  if (added && pinned) scrollToBottom(scroller);
}

async function pollActivity(container) {
  if (!state.ws) return;
  try {
    const d = await req("GET",
      `/v1/clm/sessions/${encodeURIComponent(sid())}/activity?limit=12` + chatQs());
    // only entries from the chat being read may land in this transcript
    renderActivity((d.activity || []).filter(
      (a) => setChatFor(state.chat).has(a.chat)), container);
  } catch (e) { /* transient */ }
}

async function pollApprovals() {
  if (!state.ws) return;
  let d;
  try { d = await req("GET", `/v1/clm/sessions/${encodeURIComponent(sid())}/approvals` + chatQs()); }
  catch (e) { return; }
  const mine = setChatFor(state.chat);
  const host = $("messages");
  const pinned = atBottom(host);
  let added = false;
  for (const p of (d.pending || [])) {
    if (state.approvals[p.id] || !mine.has(p.chat)) continue;
    const card = document.createElement("div");
    card.className = "msg approval";
    card.innerHTML = `<div class="bubble">
      <b>⚠ ${esc(p.tool)}</b> wants to run<br><code>${esc(JSON.stringify(p.args))}</code>
      <div class="meta"><span>waiting ${p.age_s}s</span></div>
      <div class="row"><button class="ok">approve</button>
        <button class="cancel">deny</button></div></div>`;
    const [yes, no] = card.querySelectorAll("button");
    yes.onclick = () => decideApproval(p.id, true, card);
    no.onclick = () => decideApproval(p.id, false, card);
    host.appendChild(card);
    state.approvals[p.id] = card;
    added = true;
  }
  if (added && pinned) scrollToBottom(host);
}

async function decideApproval(id, ok, card) {
  try {
    await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/approvals`,
      { id, approve: ok, by: "web" });
    card.remove();
    toast(`${ok ? "approved" : "denied"} ${id}`);
  } catch (e) { toast(e.message, true); }
}

/* ---------- sandbox panel ---------- */
async function loadSandbox() {
  if (!state.ws) return;
  let d;
  try { d = await req("GET",
    `/v1/clm/sessions/${encodeURIComponent(sid())}/sandbox?path=.`); }
  catch (e) { $("fileList").innerHTML = `<li class="empty">${esc(e.message)}</li>`; return; }
  const sb = d.sandbox || {};
  $("sbIso").textContent = sb.isolation || "—";
  $("sbUsage").textContent = `${sb.files || 0} files · ${sb.bytes || 0} B`
    + ` / ${sb.quota_mb} MB`;
  const ul = $("fileList");
  keepScroll(ul, () => {
    ul.innerHTML = "";
    if (!d.files.length) {
      ul.innerHTML = `<li class="empty">Empty sandbox.</li>`; return;
    }
    for (const f of d.files) {
      const li = document.createElement("li");
      li.className = "arch-hit file-item";
      li.innerHTML = `<div class="a-head"><span>${f.bytes} B</span>
        <code>${esc(f.path)}</code><button>view</button></div>`;
      li.querySelector("button").onclick = async () => {
        try {
          const v = await req("GET",
            `/v1/clm/sessions/${encodeURIComponent(sid())}/sandbox/file?path=${encodeURIComponent(f.path)}&max=8000`);
          const view = $("fileView");
          view.textContent = `// ${v.path} (${v.chars} chars)\n${v.text}`;
          view.hidden = false;
          revealIn($("tab-sandbox"), view);
        } catch (e2) { toast(e2.message, true); }
      };
      ul.appendChild(li);
    }
  });
}

async function runTerminal(command) {
  if (!state.ws || !command.trim()) return;
  const log = $("termLog");
  log.textContent += `$ ${command}\n`;
  log.scrollTop = log.scrollHeight;
  let d;
  try {
    d = await req("POST",
      `/v1/clm/sessions/${encodeURIComponent(sid())}/sandbox/exec`, { command });
  } catch (e) { d = { ok: false, detail: e.message }; }
  log.textContent += `${(d.detail || "").trim() || "(no output)"} ` +
    `[${d.ok ? "exit ok" : "failed"}]\n`;
  if (log.textContent.length > 40000)
    log.textContent = log.textContent.slice(-20000);
  log.scrollTop = log.scrollHeight;
  loadSandbox();
}

function termLine(text) {
  const log = $("termLog");
  log.textContent += text + "\n";
  log.scrollTop = log.scrollHeight;
}
const chipClassForTool = (kind) => kind === "tool-call" ? "classify"
  : kind === "tool-error" ? "warn" : "classify";
const chipClass = (kind) => kind.startsWith("evict") || kind.startsWith("maintain")
  ? "evict" : kind.startsWith("backstop") ? "guard"
  : kind === "warn" ? "warn" : "classify";

/* ---------- inspector: context tab ---------- */
function paintContextFromResp(clm) {
  setBudget(clm.tokens, clm.budget, clm.counts);
}
async function paintContext() {
  if (!state.ws) return;
  let snap;
  try {
    snap = await req("GET", `/v1/clm/sessions/${encodeURIComponent(sid())}/snapshot` + chatQs());
  } catch (e) { return; }
  setBudget(snap.tokens, snap.budget, snap.counts);
  const ul = $("blockList");
  keepScroll(ul, () => {
    ul.innerHTML = "";
    const filtered = state.tagFilter ? snap.blocks.filter((b) => b.tag === state.tagFilter) : snap.blocks;
    if (!filtered.length) {
      ul.innerHTML = `<li class="empty">nothing here yet</li>`; return;
    }
    for (const b of filtered) {
      const li = document.createElement("li");
      li.className = "block " + tagShort(b.tag);
      li.innerHTML = `
        <div class="b-head">
          <span class="b-tag">${b.tag[0]}</span>
          <span>#${b.id} · ${esc(b.role)} · turn ${b.turn}</span>
          <span>${b.tokens} tok</span>
          ${b.importance !== undefined ? `<span class="imp${b.tag === "longterm" && b.importance < 0.45 ? " low" : ""}" title="effective importance (decays without access)">⏳ ${b.importance}</span>` : ""}
          ${b.pinned ? `<span class="pin" title="pinned (never evicted)">📌</span>` : ""}
        </div>
        <div class="b-text">${esc(b.content)}${b.truncated ? "…" : ""}</div>
        ${b.truncated ? `<button class="expand-btn">show full block</button>` : ""}`;
      const btn = li.querySelector(".expand-btn");
      if (btn) btn.onclick = async () => {
        try {
          const d = await req("GET", `/v1/clm/sessions/${encodeURIComponent(sid())}/block/${b.id}`);
          const txt = li.querySelector(".b-text");
          txt.textContent = d.block.content;
          txt.classList.add("expanded"); btn.remove();
        } catch (e2) { toast(e2.message, true); }
      };
      ul.appendChild(li);
    }
  });
}
function setBudget(tokens, budget, counts) {
  const pct = Math.min(100, Math.round((100 * tokens) / budget));
  const fill = $("budgetFill");
  fill.style.width = pct + "%";
  fill.className = (pct > 90 ? "over" : pct > 70 ? "warn" : "") + (pct === 0 ? " zero" : "");
  $("budgetNums").textContent = `${tokens} / ${budget} tokens`;
  $("cLong").textContent = counts.longterm;
  $("cShort").textContent = counts.shortterm;
  $("cTrans").textContent = counts.transient;
}

/* ---------- inspector: archive tab ---------- */
async function searchArchive() {
  const q = $("searchInput").value.trim();
  const ul = $("archiveList");
  if (!q || !state.ws) { ul.innerHTML = `<li class="empty">Type to search the archive.</li>`; return; }
  let d;
  try {
    d = await req("GET", `/v1/clm/sessions/${encodeURIComponent(sid())}/archive?q=${encodeURIComponent(q)}&limit=30`);
  } catch (e) { toast(e.message, true); return; }
  keepScroll(ul, () => {
    ul.innerHTML = "";
    if (!d.hits.length) {
      ul.innerHTML = `<li class="empty">no archived memory matches</li>`; return;
    }
    for (const h of d.hits.slice().reverse()) {
      const li = document.createElement("li");
      li.className = "arch-hit";
      const shown = h.content.length > 200 ? h.content.slice(0, 200) + "…" : h.content;
      li.innerHTML = `
        <div class="a-head"><span>#${h.id}</span><span>${h.tag}</span>
          <span>turn ${h.turn}</span>
          <button>↩ reinject</button></div>
        <div>${esc(shown)}</div>`;
      li.querySelector("button").onclick = async () => {
        try {
          await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/reinject`, { block_id: h.id });
          toast(`#${h.id} re-injected as shortterm`);
          paintContext(); loadWorkspaces(false);
        } catch (e2) { toast(e2.message, true); }
      };
      ul.appendChild(li);
    }
  });
}

/* ---------- inspector: events tab ---------- */
function rememberEvent(ev, toTimeline = true) {
  if (state.seenSeqs.has(ev.seq)) return;
  state.seenSeqs.add(ev.seq);
  // timeline entries belong to the chat they happened in; events from other
  // chats stay out of this panel (they live in harness memory)
  if (toTimeline && setChatFor(state.chat).has(ev.chat)) state.events.unshift(ev);
}
function renderEvents() {
  const ul = $("eventList");
  keepScroll(ul, () => {
    ul.innerHTML = "";
    if (!state.events.length) {
      ul.innerHTML = `<li class="empty">No context events yet.</li>`; return;
    }
    for (const ev of state.events.slice(0, 60)) {
      const li = document.createElement("li");
      li.className = "event";
      li.innerHTML = `<span class="e-kind">${evIcon(ev.kind)} ${ev.kind} · #${ev.seq}</span>${esc(ev.text)}`;
      ul.appendChild(li);
    }
  }, true);
}

/* ---------- inspector: tuning tab ---------- */
function primeTuning(snap) {
  $("keepSlider").value = Math.round((snap.shortterm_keep_pct || 0.5) * 100);
  $("keepOut").textContent = Math.round((snap.shortterm_keep_pct || 0.5) * 100) + "%";
  $("decayOn").checked = !!snap.decay_enabled;
  $("supOn").checked = !!snap.supersede_enabled;
  const inp = $("budgetInput");
  inp.value = snap.budget;
  // the llama.cpp window is fixed when the backend loads; the harness budget
  // follows it and can never exceed it, so cap the control at that value.
  const bctx = snap.backend_ctx || null;
  inp.max = bctx || "";
  $("ctxOut").textContent = bctx
    ? `llama.cpp window: ${bctx} (budget follows)` : `backend window: n/a`;
}
async function applySetting(body) {
  if (!state.ws) return;
  try {
    const d = await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/settings`, body);
    if (d && d.note) { toast(d.note, true); primeTuningSnapshotBudget(d); }
    return d;
  } catch (e) { toast(e.message, true); }
}
function primeTuningSnapshotBudget(d) {
  // reflect a server-side clamp immediately (budget -> backend ctx)
  const v = d.changed && d.changed.max_context_tokens;
  if (v !== undefined) $("budgetInput").value = v;
}

/* ---------- wiring ---------- */
function autoGrow() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(180, t.scrollHeight) + "px";
}

document.addEventListener("DOMContentLoaded", () => {
  // composer
  $("composer").addEventListener("submit", async (e) => { e.preventDefault(); await send(); });
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("input").addEventListener("input", () => {
    autoGrow();
    // live autosave: the draft belongs to the chat that owns the field —
    // switching chats or projects never carries text across
    if ($("input").dataset.key === draftKey())
      state.drafts[draftKey()] = $("input").value;
  });

  // header
  $("refreshBtn").onclick = () => { loadHealth(); loadWorkspaces(false); paintContext(); };
  // narrow-screen panel switcher (three columns do not fit side by side)
  document.querySelectorAll("#viewBtns button").forEach((b) => {
    b.onclick = () => setView(b.dataset.view);
  });

  // workspace add
  $("wsAddBtn").onclick = () => {
    const f = $("wsAddForm");
    f.hidden = !f.hidden;
    $("wsAddInput").focus();
  };
  document.querySelector("#wsAddForm .cancel").onclick =
    () => { $("wsAddForm").hidden = true; };
  $("wsAddForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("wsAddInput").value.trim();
    if (!name) return;
    try {
      await req("POST", "/v1/clm/workspaces", { name });
      $("wsAddInput").value = ""; $("wsAddForm").hidden = true;
      toast(`project "${name}" created`);
      await loadWorkspaces(false); selectWorkspace(name, true); setView("chat");
    } catch (err) { toast(err.message, true); }
  });

  $("newChatBtn").onclick = async () => {
    if (!state.ws || state.sending) return;
    const ws = state.ws;
    captureDraft();   // the chat we leave keeps its draft in state.drafts
    try {
      const next = `chat-${String(state.chats.length + 1).padStart(3, "0")}`;
      await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/reset`,
        { chats: [next] });
      state.events = [];
      clearChatView();
      // a brand-new chat never inherits the previous one's draft
      delete state.drafts[`ws:${ws}/${next}`];
      state.chat = next;
      // register the fresh slot so it lists itself before its first message
      await req("POST", `/v1/clm/sessions/${encodeURIComponent(sid())}/chat-open`,
        { chat: next });
      const t = $("input");
      t.value = state.drafts[draftKey()] || "";
      t.dataset.key = draftKey();
      autoGrow();
      $("sidLabel").textContent = `${sid()}/${state.chat}`;
      $("blockList").innerHTML = ""; renderEvents();
      setView("chat");
      toast(`new chat ${next} in ${ws}`);
      loadWorkspaces(false);
    } catch (e) { toast(e.message, true); }
  };

  // tabs
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#tabs button").forEach((x) => {
        x.classList.remove("on");
        x.setAttribute("aria-selected", "false");
      });
      b.classList.add("on");
      b.setAttribute("aria-selected", "true");
      document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
      $(`tab-${b.dataset.tab}`).hidden = false;
      if (b.dataset.tab === "events") renderEvents();
      if (b.dataset.tab === "sandbox") loadSandbox();
    };
  });

  // tag filter
  document.querySelectorAll("#tagFilter button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#tagFilter button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      state.tagFilter = b.dataset.tag;
      paintContext();
    };
  });

  // archive search (live, debounced)
  let st; $("searchInput").addEventListener("input", () => {
    clearTimeout(st); st = setTimeout(searchArchive, 350);
  });
  $("searchForm").addEventListener("submit", (e) => { e.preventDefault(); searchArchive(); });

  // tuning controls
  $("keepSlider").addEventListener("change", async () => {
    const v = +$("keepSlider").value / 100;
    $("keepOut").textContent = Math.round(v * 100) + "%";
    await applySetting({ shortterm_keep_pct: v });
    toast(`shortterm keep = ${Math.round(v * 100)}%`);
  });
  $("budgetInput").addEventListener("change", async () => {
    await applySetting({ max_context_tokens: +$("budgetInput").value });
    toast(`budget = ${$("budgetInput").value} tokens`); paintContext();
  });
  $("thrSlider").addEventListener("change", async () => {
    const v = +$("thrSlider").value / 100;
    $("thrOut").textContent = v.toFixed(2);
    await applySetting({ systemone_evict_threshold: v });
    toast(`evict threshold = ${v.toFixed(2)}`);
  });
  $("decayOn").addEventListener("change", async (e) => {
    await applySetting({ decay_enabled: e.target.checked });
    toast(`decay & reheating ${e.target.checked ? "on" : "off"}`);
    paintContext();
  });
  $("supOn").addEventListener("change", async (e) => {
    await applySetting({ supersede_enabled: e.target.checked });
    toast(`supersession ${e.target.checked ? "on" : "off"}`);
  });

  // agent toggles in the composer (per-project, applied live) ---------------
  $("toolWeb").addEventListener("change", (e) => setTool("web", e.target.checked));
  $("toolExec").addEventListener("change", (e) => setTool("exec", e.target.checked));
  $("toolFs").addEventListener("change", (e) => setTool("fs", e.target.checked));
  $("policySel").addEventListener("change", (e) => setPolicy(e.target.value));

  // sandbox panel: terminal + files ------------------------------------------ 
  $("termForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = $("termInput");
    if (!v.value.trim()) return;
    const cmd = v.value; v.value = "";
    await runTerminal(cmd);
  });
  $("sbRefreshBtn").onclick = () => loadSandbox();
  $("sbResetBtn").onclick = async () => {
    if (!confirm("Delete every file in this sandbox?")) return;
    try {
      await req("DELETE", `/v1/clm/sessions/${encodeURIComponent(sid())}/sandbox`);
      $("termLog").textContent = ""; toast("sandbox reset"); loadSandbox();
    } catch (e2) { toast(e2.message, true); }
  };

  // hints toggle: hide/show chips in transcript
  $("hintsOn").addEventListener("change", (e) => {
    document.querySelectorAll(".msg .meta, .system-line").forEach((el) => {
      el.style.display = e.target.checked ? "" : "none";
    });
  });

  // boot: splash (animated dyson loader) holds until the server answers, then
  // swaps to the "done" pose and fades away.
  const killSplash = () => {
    const sp = $("splash");
    if (!sp) return;
    const img = sp.querySelector("img");
    if (img) { img.src = "/dyson-done.svg"; img.classList.add("flash"); }
    setTimeout(() => sp.classList.add("gone"), 300);
  };
  state.model = null;
  loadHealth().then(() => { killSplash(); loadWorkspaces(true); });
  // snappy background refresh while the tab is visible
  setInterval(() => { if (!document.hidden) { paintContext(); loadWorkspaces(false); } }, 4000);
});
