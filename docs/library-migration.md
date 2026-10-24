# Migrating a library onto explicit sessions (clm-mem 0.3 -> session-api)

Audience: whoever owns an integration **library** that talks to a running
`clm-mem serve` — both the ones already in this repo (`clm_mem.remote.RemoteHarness`,
the web studio's `app.js`, the MCP adapter, `cli.py --connect`) and the external
ones named in the plan (Unsloth Studio integration, openai-python users, any agent
runtime that rides `/v1/chat/completions` with a session header).

The plan covers **two halves**: how the library's *web/HTTP* usage must change,
and how its *Python* usage must change. They are the same contract seen from two
angles — a Python library consumer today is either (a) vendoring `RemoteHarness`,
(b) hand-rolling `requests`/`urllib` against the REST surface, or (c) importing
the engine directly (`MemoryHarness`) and never touching HTTP. Only (c) is
unaffected by this migration; (a) and (b) both ride the new verbs.

---

## 1. Where the branch is

Everything below lives on **`spike/session-api`** (HEAD `6f86c3f`), not on `main`.

```
main                       old contract: header-driven memory, implicit sessions
spike/chat-isolation-seed  merged into spike/session-api at 085e691 (D1 landed)
spike/session-api          THE branch — M3 shipped on-branch, M4 folded in, green (115 tests)
```

Key facts about the branch:

* **It is a breaking change with no compat window.** The original plan (§7.3 of
  `docs/refactor-plan.md`) promised a `--memory-entry=both` transition mode and a
  thin legacy adapter. That was cancelled by owner directive ("be brave"): M3
  shipped without it, M5 is marked vaporware. Header-driven memory is **gone**.
* Legacy headers (`X-Cllm-Mem-Session`, `X-Cllm-Mem-Chat`) are not honored
  anywhere — including on `/v1/clm/*` routes. Sending them earns a **410 Gone**
  whose body is the migration hint ("open a branch with POST /v1/clm/sessions and
  converse via POST .../turns"). That shim exists only for detection/teaching; it
  will be deleted once no downstream sends headers (M5 cleanup).
* `/v1/chat/completions` is now **pure passthrough** to the backend — plain
  llama.cpp/OpenAI semantics, never remembered, regardless of headers. A library
  that previously got memory "for free" by adding a header will get *unremembered*
  completions plus a 410 if it still sends the header.
* Sessions are **branches**: `session == workspace == ws:<name>`, each owning its
  harness (live window + archive) and sandbox. Bare ids (`"alice"`) no longer name
  a session; open-time validation requires `ws:<name>` with the same regex used
  for workspaces (`WS_NAME_RE`). Percent-encode the colon in URL paths
  (`/v1/clm/sessions/ws%3Aalice/turns`) or leave it bare — both route identically.
* `MemoryHarness.step()` gained a `since_event_seq` parameter and now returns
  `{"reply", "events"}` **when that argument is passed**, still a bare string
  when omitted (engine-level change; relevant to engine consumers who want the
  event chips without an HTTP round trip).

Pin against this branch until it lands on `main`: install editable or track the
branch, and treat anything documented in `README.md` ("Client contract") plus
`clm_mem/remote.py` as the reference implementation. The SDK is now the contract:
whatever `RemoteHarness` can express, hand-rolled HTTP should be able to express,
and vice versa — if your library can do something the SDK cannot (or vice versa),
that is a bug in one of them, not a second dialect.

---

## 2. The new contract at a glance

| Concept | Old (`main`) | New (`spike/session-api`) |
|---|---|---|
| Start memory | side effect: first request that mentions an id creates it | `POST /v1/clm/sessions` — explicit, returns 201/200 + `created` flag |
| Send a message | `POST /v1/chat/completions` + session header | `POST /v1/clm/sessions/<id>/turns` |
| Session id | any string (`"alice"`), bare ids allowed | branch id `ws:<name>` only; workspace name validated |
| Chat slot | `X-Cllm-Mem-Chat` header | `"chat": "chat-001"` field in the turn body |
| Unknown session on send | created implicitly, no error | **409** — turns never create sessions |
| Streaming | SSE faked post-hoc from a finished string (`_sse_reply`) | SSE on `/turns` via shared helper: role chunk, content deltas, `event: clm-mem-event` chip chunks, `[DONE]` |
| Harness truth in replies | ad-hoc `clm_mem` extension key | documented `clm_mem` block (status/counts/tokens/budget/archive/events/chats) |
| Legacy headers | the contract | **410 Gone** with migration hint |

Three nouns: **branch** (`ws:<name>`, one harness + sandbox), **chat**
(`chat-001`, a transcript slot inside a branch — partitions visibility, not
memory), **turn** (one user message + reply + event chips).

---

## 3. Web/HTTP migration for library authors

### 3.1 What to stop doing

1. **Stop sending `X-Cllm-Mem-Session` / `X-Cllm-Mem-Chat` anywhere.** They are
   rejected with 410 even on routes where you might think they'd be harmless. If
   your library currently sends them, delete the header code entirely — do not
   "keep it for compatibility", there is nothing to be compatible with.
2. **Stop treating `/v1/chat/completions` as the memory door.** It is now plain
   passthrough: fine for stateless completions (and it stays first-class, SSE
   included), but it will never remember anything. A library that wants memory
   must speak session verbs; a library that wants plain llama.cpp should use it
   and forget clm-mem exists.
3. **Stop relying on implicit session creation.** Nothing creates state by
   mentioning an id anymore. Every memory-capable library flow starts with an
   explicit open.

### 3.2 What to do instead

Minimal lifecycle, two requests:

```http
POST /v1/clm/sessions                     {"session": "ws:mylib"}      -> 201 {created:true,...} / 200 {created:false,...}
POST /v1/clm/sessions/ws%3Amylib/turns    {"content": "..."}           -> reply + clm_mem truth
```

Body shapes worth knowing:

* **Open** (`POST /v1/clm/sessions`): `session` (or `workspace`, a synonym during
  the naming transition) is required; `model` optional; harness-shaping options
  go in `options{...}` — `max_context_tokens`, `shortterm_keep_pct`,
  `classify_mode`, decay/supersede/reheat toggles, `tools_enabled`. These are
  **creation-time only**: a reopen (`created:false`) never reshapes an existing
  branch. Put them on the open call even when reopening is idempotent — it costs
  nothing and keeps options honored on first creation. The response embeds
  budget/status/counts/chats, so a library can validate its assumptions (e.g.
  "is my budget what I asked for?") right there without a second snapshot call.
* **Turn** (`POST .../turns`): accept either `content` (string) or the OpenAI
  `messages` array — if you pass `messages`, the last user message becomes the
  turn, so an openai-python-shaped client ports with two lines changed. Chat slot
  is a body field (`chat: "chat-001"`), not a header. Generation knobs stay
  per-request (`temperature`, `max_tokens`). `"stream": true` switches to SSE.
* **Errors are teachable now**: 409 unknown session (open first), 400 missing
  user text / invalid chat id, 502 backend failure. Map them to library-level
  exceptions deliberately; in particular a "session not open" error in your SDK
  should suggest calling open(), not silently retrying with creation — the old
  implicit path hid exactly this class of bug.

### 3.3 Streaming advice

The SSE contract on `/turns` (phase S1): an assistant role chunk, then content
deltas as `chat.completion.chunk` objects, then **one `event: clm-mem-event`
chunk per memory event chip** (eviction, offload, re-injection, tool activity —
whatever the turn produced), then `data: [DONE]`. Two things matter for
libraries:

* Parse the `clm-mem-event` events — they are the honest replacement for parsing
  log lines or polling status after the reply. A chat-completions client that
  ignores unknown SSE event types will silently drop memory behavior; make your
  library surface them (that is what "differs when useful" means).
* The deltas are currently a **replay** of a finished completion (S1), not true
  streaming (S2, milestone M6). Do not build logic that assumes you receive the
  reply before the server finishes computing it — nothing breaks if S2 lands, but
  code written against "first token arrives mid-compute" is speculative. Consume
  it as a transport convenience (progressive render), not as an algorithmic
  guarantee.

### 3.4 Ids, encoding, slots

* Branch ids are `ws:<name>` with `<name>` matching
  `[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}`. Generate ids from workspace-safe strings;
  do not assume arbitrary client strings name sessions. If your library previously
  let users pick any session id ("alice"), migrate it to `ws:alice` — and be
  aware a legacy bare id no longer maps anywhere (the compat adapter that would
  have done that was cancelled).
* Percent-encode the colon in URL paths (`ws%3Amylib`) or leave it bare; both
  work, but pick **one** style library-wide so cache/proxy behavior stays stable.
  The SDK quotes it; matching the SDK is the cheap answer.
* Chat slots: `chat-open` registers a slot (optionally seeded with memories);
  turns carry `chat`; transcript replay is
  `GET /v1/clm/sessions/<id>/turns?chat=<c>`. A library that wants multiple
  concurrent conversations on one branch uses slots; isolation between projects
  means separate branches, never slots.

---

## 4. Python migration for library authors

Three flavors of Python consumer; pick your row.

### 4.1 Engine consumers (`MemoryHarness` in-process)

Unaffected by the transport migration — no HTTP, no headers. Two notes anyway:

* If you want this turn's event chips without polling `h.events`, pass
  `since_event_seq=<last seq you saw>` to `step()` and get `{"reply", "events"}`;
  omit it and the bare-string return stands (the old signature is a subset of the
  new one, so existing code keeps compiling).
* The layering plan (§6 of `docs/refactor-plan.md`) will move engine modules under
  `clm_mem/engine/` with flat aliases kept for one release. If your library imports
  `from clm_mem import MemoryHarness`, that keeps working; if it reaches into
  `clm_mem.server` to get harness machinery, stop — the planned split makes that
  an embedding concern (`serving/supervisor.py`), not an engine one.

### 4.2 SDK consumers (`RemoteHarness`)

This is where most library migration happens, and on this branch it is already
half-done: `clm_mem/remote.py` grew `open()` / `turn()`, and `step()` became sugar
that opens once then turns. The old `main` version of the same class rode
`/v1/chat/completions` with a session header — so upgrading the package *is* the
migration for anyone who vendored or pinned it.

Advice for using the new SDK **differently** than old code used the old one:

* **Call `open()` explicitly at connection/init time** instead of letting
  `step()` lazily open on first message. That is what "the SDK speaks the same
  verbs as the studio" buys you: creation-time options (`max_context_tokens`,
  keep-pct, classify mode...) actually reach the server, and creation failures
  surface at connect time where your library can fail fast with a useful error —
  not mid-conversation. `step()`'s lazy open exists for convenience; prefer the
  explicit call in library code so "session not open" becomes an impossible state
  rather than a runtime surprise (the studio does exactly this: one
  `openSession()` per workspace visit).
* **Prefer `turn()` over `step()`** when you need harness truth. `step()` returns
  only the reply string and re-primes config knobs with an extra snapshot call;
  `turn()` returns the full server payload — reply under `choices`, memory state
  under `clm_mem` (counts, budget, archive size, this turn's event chips). If
  your library was doing `step(...)` then `snapshot()` to learn what happened,
  that second round trip disappears: read `resp["clm_mem"]`.
* **Config knobs:** keep using the `RemoteConfig` attribute-write magic
  (`h.cfg.shortterm_keep_pct = 0.7`) — it pushes to `/settings` and is unchanged.
  But know the boundary: knobs that *shape* the harness (budget, policy toggles)
  belong at open time for new branches; runtime knob writes still work on live
  sessions via settings. Don't fight the split by trying to set budget per turn.
* **Errors:** `turn()` raises `RuntimeError` with the server's code/body — a 409
  there means you skipped `open()`. Catch and translate into your library's own
  exception hierarchy; do not swallow it into an implicit open, which would
  recreate the old "sessions appear from nowhere" bug class.
* **Sessions:** `RemoteHarness(base_url, session="ws:name")` — pass branch ids.
  The `connect(url, workspace=...)` helper builds `ws:<name>` for you and is the
  nicer door for libraries whose config already speaks "project name". Default
  session is `ws:cli`; if your library previously defaulted to a bare `"cli"`-style
  id, update it.

### 4.3 Hand-rolled HTTP in Python (`requests`/`httpx` users who skipped the SDK)

Same migration as §3 with one extra rule: **vendor or import `RemoteHarness`
instead of reimplementing it.** The refactor's end state makes the SDK the
reference client — every studio capability is expressible through it, and a
parallel hand-rolled client drifts from that on day one. If you must stay
hand-rolled (dependency weight, async event loop), mirror exactly what
`remote.py` does: same paths, same bodies, same percent-encoding, same error
mapping — then your code is reviewable against the SDK in tests rather than
against vibes.

A concrete before/after for a typical library function:

```python
# BEFORE (main): header memory
resp = requests.post(
    f"{base}/v1/chat/completions",
    headers={"X-Cllm-Mem-Session": session},          # -> 410 on the new branch
    json={"messages": [{"role": "user", "content": text}]},
)

# AFTER (spike/session-api): explicit verbs
base = "http://host:8080/v1"
root = base[: -len("/v1")]
requests.post(f"{root}/v1/clm/sessions", json={"session": f"ws:{name}"})
resp = requests.post(
    f"{root}/v1/clm/sessions/ws%3A{name}/turns",
    json={"content": text, "chat": chat_id},
)
reply = resp.json()["choices"][0]["message"]["content"]   # truth in resp.json()["clm_mem"]
```

...or simply:

```python
from clm_mem import RemoteHarness            # or connect(url, workspace=name)
h = RemoteHarness(base_url, session=f"ws:{name}")
h.open(options={"max_context_tokens": 8192})     # once, at init — not lazily
r = h.turn(text, chat=chat_id)                  # full payload, no extra snapshot call
```

---

## 5. Advice: how the library should use this differently going forward

The point of the refactor was that clients stop treating memory as an accident.
For a library specifically, that means:

1. **Sessions are long-lived resources you manage, not request decorations.**
   Open at connection setup (constructor, `connect()`, first-use-with-lock), keep
   the branch id in your config, close/drop deliberately (`DELETE
   /v1/clm/sessions/<id>`) when your library's lifecycle ends. The old shape — a
   header on every request creating state invisibly — made it impossible to know
   what your library left behind on a server. Branches are visible in `/health`
   and `GET /v1/clm/sessions`; be a good citizen and clean up.
2. **Options belong at open time, generation knobs belong per turn.** Budget and
   policy shape the branch (and persist into its files); temperature does not. If
   your library was previously setting budget-ish things via `/settings` mid-use,
   that still works but is a smell for new branches — set them once at open.
3. **Consume harness truth from replies, not side channels.** The `clm_mem` block
   (and SSE `clm-mem-event` chips) replaces the pattern of "send message, then
   poll status/snapshot" that header-era clients needed. One call, honest state.
4. **One namespace: branch ids, validated.** Names are workspace-safe; two levels
   exist for a reason — chats partition visibility *inside* a branch, branches
   isolate memory *between* projects. A library supporting "multiple users" should
   decide explicitly which level it maps onto and document that choice; do not
   invent a third session-like noun.
5. **Isolation is structural, don't fight it.** A turn in one branch never sees or
   reheats another's blocks; archive search and re-injection are per-branch. If
   your library wants cross-session behavior (shared base memory, merging), that
   is deferred design — express it as explicit application-level copy (search
   archive of A, reinject into B) rather than assuming broadcast semantics.
6. **If you want plain completions, say so.** Use `/v1/chat/completions` for what
   it now is: passthrough to the backend with no memory. If a library needs both
   modes (stateless calls plus a remembered conversation), that's two code paths
   against one contract — much better than "whichever endpoint happens to have a
   header attached."
7. **Watch for M5.** When the 410 shim is deleted, legacy-header senders get a
   plain error instead of a teaching hint. Libraries should migrate headers off
   *now*, not when the hint stops being friendly. And if your library integrates
   with Unsloth Studio specifically, that integration is the consumer named in
   the plan for deletion-gating — coordinate its migration before M5 lands.

---

## 6. Per-consumer checklists

### `clm_mem.remote.RemoteHarness` (in-repo SDK)
- [x] Already migrated on this branch (`open`/`turn`, header-free). Downstream
      libraries get the fix by upgrading against the branch.
- [ ] External consumers pinned to an old wheel: update dependency to track
      `spike/session-api` (or wait for it to land on `main`), then apply §4.2.

### Web studio (`clm_mem/static/app.js`) — reference client
- [x] Migrated: `openSession()` before turns, explicit slots, no headers. Copy its
      patterns when in doubt; it is the same-origin proof that the contract works.

### MCP adapter (`clm_mem/mcp.py`)
- [x] `clm_open_session` added; `clm_chat` opens implicitly (convenience for agent
      loops). MCP clients of your library should call `clm_open_session` when they
      need creation-time options, exactly like HTTP libraries do.

### CLI (`clm-mem chat --connect`)
- [x] Rides `RemoteHarness` open+turns; UX unchanged.

### Unsloth Studio integration (external, named in the plan)
- [ ] Replace header injection with session open + turns; map its "projects" onto
      branches (`ws:<project>`), its chat tabs onto slots within a branch.
- [ ] Decide whether to stream (`stream: true`, parse `clm-mem-event` chips) or
      stay non-stream; either is fine, but surface memory events if you stream.
- [ ] Remove legacy headers entirely; test that the 410 never appears in logs —
      its presence means some code path still thinks headers exist.

### openai-python / generic OpenAI clients pointed at `base_url`
- [ ] If they never sent session headers: nothing to do — passthrough is theirs.
- [ ] If they want memory: two-line port (point at `/turns`, pass `content` or
      `messages`), plus an open call before the first turn.

---

## 7. Test plan for a migrating library

1. **410 detector.** Run your library's suite against the new server with headers
   still enabled in *your* code; assert no request ever receives a 410 after you
   remove them — before/after is the cleanest proof you found every path.
2. **Unknown-session test.** Turn on an unopened branch -> expect 409 (or your SDK
   equivalent). This is the new error class; if your library swallows it, fix that.
3. **Idempotent open.** Open twice; second returns `created:false` with same state.
   Libraries may call open defensively per connection — must be harmless.
4. **Reopen preserves memory.** Turn, drop session (or restart server), reopen,
   snapshot shows the archive/window intact per reset semantics. Creation-time
   options on the reopen are ignored (`created:false`) — assert your library
   doesn't depend on them applying.
5. **Chat slots.** Two chats on one branch: transcripts partition (`GET
   .../turns?chat=`), memory shared within the branch, and seeded chat-open works.
6. **Branch isolation.** Turn in A; B's snapshot/archive shows nothing of it;
   search of B finds none of A's content.
7. **SSE shape.** Stream a turn: role chunk first, content deltas concatenate to
   the non-stream reply, chips appear as `clm-mem-event` events, `[DONE]` last.
8. **Legacy bare ids die.** Open with `"alice"` -> 400 (`ws:` prefix required). If
   your library generates ids from user input, sanitize into workspace-safe names.

---

## 8. Suggested sequencing for the owner of such a library

1. Read `README.md` *Client contract* + `clm_mem/remote.py` on this branch (they
   are the spec).
2. Remove header code; make open explicit at connection setup (§4.2, §3.2).
3. Switch sends to `/turns`; read harness truth from replies instead of polling.
4. Map ids onto `ws:<name>` branches + chat slots (§3.4).
5. Add the error/slot/isolation tests (§7).
6. If streaming matters, adopt SSE with chip parsing; treat deltas as transport
   convenience until S2.
7. Coordinate with Unsloth Studio integration before M5 deletes the 410 shim.

Nothing in this plan requires server changes — the branch is complete and green
for it. What remains on the repo side (M6 true streaming, M7 `clm-mem api`, layer
split) is independent; a library migrating today is compatible with all of it.
