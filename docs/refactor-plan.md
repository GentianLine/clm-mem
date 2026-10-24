# clm-mem refactor: options, ideas, and plan

Status: discussion draft for the owner. Nothing in this file is implemented.
Branches referenced below exist in the sandbox repo; see `git log` for details.

## 1. What we have today (as-is map)

One package, one process (`clm-mem serve`), four front doors into one
`MemoryHarness`:

| Surface | Entry | Notes |
|---|---|---|
| CLI chat | `clm-mem chat`, `clm-mem demo` | local `MemoryHarness`, or `--connect` -> `RemoteHarness` |
| OpenAI-compat proxy | `POST /v1/chat/completions` (+ native `/completion`, `/props`, `/metrics`, `/version`, `/health`, decision aliases `/v1/decisions`, `/v1/systemone`, `/v1/classifier`) | session header (`X-Cllm-Mem-Session`) selects harness mode, absence = passthrough |
| Studio REST | `/v1/clm/*` | workspaces, sessions, snapshot/status/archive/reinject/settings/reset/chat-open, tools/activity/approvals/sandbox |
| MCP server | `POST /mcp` (JSON-RPC) | `clm_chat`, `clm_status`, ... mirrors the same library |

Files and rough sizes: `server.py` 1394, `cli.py` ~1000, `harness.py` 964,
`llm.py` 500, `mcp.py` 401, `static/app.js` 1051. The supervisor
(`server.py`) owns process spawning (`processes.py`), the HTTP handler, and
session state; the harness holds blocks + eviction + mirror files + tools.

### 2. Where it got complicated (why the current shape hurts)

1. **The memory layer is invisible.** It rides inside a chat-completions call:
   session id via header/body field, only the *latest* user message becomes a
   turn, reply reconstructed as a completion object, SSE faked from a
   non-streaming step (`_sse_reply`). Clients that don't send the header get
   pure passthrough - so "is this server remembered?" is an undocumented
   side effect of a header.
2. **The harness API surface is inconsistent across transports.** CLI chat,
   `RemoteHarness`, MCP, and the web studio each hand-implement parts of the
   same library; `RemoteHarness.step` even re-primes config with an extra
   snapshot call per turn. The studio's own vocabulary (`chat-open`, seeds)
   exists in REST but nowhere else.
3. **Sessions are implicit everywhere.** A session is created by any request
   that mentions an id (even a tools toggle before a first message). There is
   no place to hang creation-time options (model, budget, template) or return
   creation errors.
4. **Two levels of "conversation" with no shared contract.** Workspace =
   harness = session (`ws:<name>`); chat = transcript slot inside it
   (`chat-001`). The spike branch (`spike/chat-isolation-seed`) adds more
   per-chat semantics (seeded chats, reheat scopes, activity filters) on top
   of this split without changing the transport, which works but deepens the
   implicitness.
5. **The proxy's own routing is hand-rolled** (`do_GET`/`do_POST` +
   `_clm_get`/`_clm_post` if-chains): `if` after `return`, 404s emitted from
   six places, no route table to test or reuse.
6. **No layering story for the Python library.** Everything is exported from
   `__init__`; a "harness-only" consumer must import the server module to get
   `Supervisor`. There's no documented split between engine / serving /
   clients.
7. **Streaming doesn't exist end-to-end.** The backend LLM call is
   non-streaming (`OpenAIChat.chat`), the harness step is atomic, and SSE is
   a post-hoc replay of the finished string. A real streaming story touches
   every layer and needs to be designed, not patched.
8. **Unfinished spike work.** `spike/chat-isolation-seed` carries uncommitted
   changes (reheat access channels: `last_seen`, `reheat_scope=chat`) plus a
   failing test (`test_phase3_ranks_by_value_when_decay_on`). The failure is
   semantic, not cosmetic: the test marks the *newer* block stale by setting
   `last_access = turn - 20` (turn is 1, so last_access = -19), but the new
   `eff_importance` freshness rule takes `max(last_access, last_seen)` and
   `add()` stamps `last_seen=turn`, so the "stale" block reads as perfectly
   fresh and phase-3 ranking ties/keeps it. The test's intent (weakest
   effective importance offloads first) must be re-expressed under the two
   access channels — e.g. set both `last_access` *and* `last_seen` stale, or
   decide freshness never participates in phase-3 ranking. Whatever direction
   we take, that branch needs to land or be explicitly deferred.

## 3. Goals (restated as design constraints)

- **G1 - Simple harness layer.** Runnable as an API from the command line;
  wraps LLMs and SystemOne models; provides a memory layer around the
  completion API.
- **G2 - All-in-one option.** One command spins up a full backup API,
  including the llama.cpp endpoints (proxy + spawned backends + studio).
- **G3 - Independence.** `harness`, `chat`, and `web studio` stay independent
  enough that the harness can run just as an API and as a Python library.

## 4. The change under consideration, analyzed

> Instead of wrapping the completion API transparently, present all the
> backend APIs and add a new API for starting a new memory session, adding to
> the conversation, and streaming responses back out. These endpoints should
> be similar to the completion endpoint but differ when useful.

This is the **explicit sessions** option (Option B below). Before choosing it,
note what it costs and buys:

- It makes the memory contract discoverable: session lifecycle verbs
  (`open`, `turn`, `stream`) instead of a header side effect.
- "Differ when useful" is the key phrase: keep OpenAI-compatible request
  bodies (messages array, model, temperature...) so clients can port with small
  changes, but let responses carry harness truth (event chips, counts,
  budget) and let streaming be native SSE rather than a replay.
- The cost: every existing client (Unsloth Studio integration, openai-python
  users, MCP, the web studio itself) sits on today's implicit contract.
  Option B without a compatibility story breaks them all at once.

## 5. Options

### Option A - Keep transparent wrapping; tighten only

Keep `/v1/chat/completions` + session header as the single memory entry.
Add: route table refactor, streaming (maybe), shared client SDK, and make the
studio REST official by documenting it. No new verbs.

- **Pros:** zero client migration; least work; everything already works.
- **Cons:** the implicit contract stays the product's spine - session
  creation, chat slots, and memory behavior remain undocumented side effects;
  streaming remains awkward (a completion call that streams *and* creates a
  session atomically); no place for creation-time options; G1/G3 barely
  addressed.
- **Risk:** low short-term, high long-term. We'd keep accreting around the
  same invisible seam - which is exactly how we got here.

### Option B - Explicit memory-session API (the proposed change)

Present backend APIs honestly and add first-class session verbs:

```
POST /v1/clm/sessions                     open a memory session (options body)
POST /v1/clm/sessions/<id>/turns          add to conversation, get reply (SSE-capable)
DELETE /v1/clm/sessions/<id>              close/drop it
...                                       + the existing introspection verbs
```

Bodies mirror chat-completions where useful (`messages`/`content`, `model`,
temperature) and differ where useful (`stream`, `chat` slot, `options` on
open). Passthrough/native routes stay exactly as they are - "present all the
backend APIs" means `/completion`, `/props`, `/metrics`, decision aliases
remain first-class, not hidden behind a default.

- **Pros:** matches the stated goal almost word for word; sessions become
  testable units with explicit creation errors; streaming designed once at
  the right layer; studio/CLI/MCP all become clients of one contract;
  passthrough becomes *a mode*, not an accident.
- **Cons:** breaking unless we keep a compatibility window; every client and
  the web studio must move (mitigated by vendoring `RemoteHarness` as the
  SDK that already speaks the new verbs).
- **Risk:** medium - but bounded if we ship it behind explicit modes plus a
  compat shim rather than deleting the old path overnight.

### Option C - Hybrid: both, with a declared mode per server

Option B's API surface, but `serve` carries an explicit
`--memory-entry={explicit|compat|both}` (default `both` for one release).
In `both`, the legacy header path keeps working and is logged/deprecated;
native clients see passthrough in all modes. The web studio immediately
migrates to the new verbs (it's same-origin, we control it). One release
later the default flips; compat stays available via flag.

- **Pros:** satisfies B's goals without a big-bang break; gives the
  harness-only story (G1) a real entry (`clm-mem api` - see section 7) while keeping
  G2's all-in-one intact; lets us *measure* adoption before deleting.
- **Cons:** two doors open during transition (mitigated: compat door is a
  thin adapter into the same session code, not a second implementation).
- **Risk:** lowest that still achieves the goal. This is what I recommend.

### Option D - Split packages (`clm-mem-core` + `clm-mem-server`)

Extract engine into its own distribution for pip-only consumers.

- **Pros:** purity; smaller install for library users.
- **Cons:** packaging churn, version coupling, no benefit the namespace split
  (below) doesn't give us for free. Revisit only if someone actually needs
  `pip install clm-mem-core` without the server.

**Recommendation: Option C**, sequenced so B's surface is built first and A's
compat path is a *thin adapter* into it, not a parallel implementation.

## 6. Layering & naming (the refactor skeleton)

Goal: `clm_mem` stays one installed package; modules form a strict downward
dependency graph with no cycles:

```
clm_mem/
  engine/            # pure library - NO http.server, NO CLI imports
    blocks.py tokens.py config.py tagging.py
    llm.py           # OpenAIChat, StubChat, SystemOneClient (transport clients)
    harness.py       # MemoryHarness; step() gets an optional stream= hook
    tools/           # manager.py sandbox.py spec.py web.py
  serving/           # the supervisor + HTTP machinery
    processes.py     # unchanged role
    supervisor.py    # Supervisor, ProxyConfig, start_supervisor (from server.py)
    routes.py        # route table: one place mapping path+method -> handler
    http_handlers.py # the BaseHTTPRequestHandler subclass, thin
  clients/           # SDKs *of* a running server (all speak the same verbs)
    remote.py        # RemoteHarness - extended with open/create/stream
    sdk.py           # shared request plumbing (auth headers, errors) used by
                     # remote.py + mcp.py adapters
  cli.py             # CLI on top of engine + clients only
  mcp.py             # MCP JSON-RPC adapter over the same route handlers
  static/            # web studio SPA - a client of routes via HTTP only
```

Rules enforced by import-linter-style grep tests (`tests/test_layers.py`):

1. `engine/` imports nothing from `serving/`, `clients/`, `cli`, or `mcp`.
2. `serving/` may import `engine`; never the reverse.
3. `clients/` imports stdlib + engine types only (no serving).
4. `cli.py` and `mcp.py` import engine + clients + serving; `static/` speaks
   HTTP only.
5. No module other than `serving/supervisor.py` touches `ProcessManager`.

Public API (`__init__.py`) becomes explicit tiers:
`from clm_mem.engine import MemoryHarness, HarnessConfig` for harness-only
users; `from clm_mem.serving import start_supervisor` for embedders;
`from clm_mem.clients import RemoteHarness` (or `clm_mem.connect(url)`)
for anyone talking to a running server. Today's flat names keep working via
aliases for one release, then drop.

## 7. The new session API surface (detailed proposal)

### 7.1 Resource model

Three nouns, matching what the studio already implies:

- **workspace** - `ws:<name>`; on-disk sandbox; == harness == legacy "session".
- **chat** - `chat-001`; transcript slot inside a workspace.
- **turn** - one user message + its assistant reply + event chips; the verb
  that replaces "send a completion with a session header".

Legacy plain sessions (bare ids, no `ws:` prefix) remain supported as
anonymous workspaces for exactly one release (compat mode).

### 7.2 Endpoint table

```
# backend passthrough - unchanged, first-class
POST /v1/chat/completions        (no session header == plain llama.cpp; compat mode only)
POST /completion   GET /props /metrics /specs /version /health /v1/models
POST /v1/decisions | /v1/systemone | /v1/classifier      (verbatim relay - unchanged)

# memory sessions - NEW verbs, chat-completions-shaped bodies
POST   /v1/clm/workspaces                      {name}            create sandbox
GET    /v1/clm/workspaces                               list + stats
DELETE /v1/clm/workspaces/<name>                        delete (files included)

POST   /v1/clm/sessions                         {workspace?, model?, options{...}}
        -> 201 {session_id, workspace, model, budget, created: true|false}
        Idempotent-ish: reopening an existing id returns it with created:false.
        Options (all optional): max_context_tokens, shortterm_keep_pct,
        classify_mode, decay/supersede/reheat_scope, tools_enabled...
        -> maps straight onto HarnessConfig; creation-time only.

POST   /v1/clm/sessions/<id>/turns            {content | messages, chat?, model?,
                                                stream?, temperature?, ...}
        -> 200 {reply, session_id, chat, counts, tokens, budget, archive,
                events[], usage{...}}            (non-stream)
        -> SSE when stream:true: role-delta, content-deltas, clm-mem event
           chunks (`event: clm` with chip payloads), final `data:[DONE]`
GET    /v1/clm/sessions/<id>/turns?chat=<c>   transcript of one chat slot (replay)

DELETE /v1/clm/sessions/<id>                            close/drop   (unchanged)
POST   /v1/clm/sessions/<id>/reset            {chats?}  retire live window    (unchanged)
POST   /v1/clm/sessions/<id>/chat-open        {chat, seed?}                (unchanged)

# introspection - unchanged paths
GET    /v1/clm/sessions                          GET .../status | snapshot[?full=1]
GET    .../archive?q=                            POST .../reinject {block_id}
POST   .../settings {...}                        tools/activity/approvals/sandbox (unchanged)
```

Notes on "similar but different":
- `/turns` accepts either `content` (string) or the full OpenAI `messages`
  array; if messages, it uses the last user message - so an openai-python
  client can be pointed at it with two lines changed.
- Responses embed what completion responses embed *plus* harness truth
  (counts/events/budget), replacing today's ad-hoc `clm_mem` extension key
  with a documented one.
- `/turns` **does not** create sessions. Explicit 409/410 when the session
  is unknown/closed - errors become teachable, and the CLI/studio call
  `POST /v1/clm/sessions` first.

### 7.3 Modes & compatibility

`serve --memory-entry=both|explicit|compat`:
- `both` (default for one release): new verbs live; legacy header path lives
  via a **thin adapter**: `_chat_completions` with a session header becomes
  *get-or-create + turns* called through the same handler functions.
- `explicit`: header path gone; `/v1/chat/completions` is pure passthrough,
  period. This is the end state the proposal implies.
- `compat`: new verbs hidden - for anyone pinning an old deployment.

Deprecation telemetry: legacy hits log once per session id (`legacy memory
entry used; migrate to POST /v1/clm/sessions/<id>/turns`) and appear in
`/health` as `legacy_sessions: n` so the flip is evidence-based.

### 7.4 Streaming, designed once

- **Phase S1 (structure):** `/turns` streams SSE from day one of the new
  verbs, even though it's still a replay (`role`, deltas, `clm` events,
  `[DONE]`) - but now the replay happens in *one* shared helper used by
  compat adapter and new endpoint alike.
- **Phase S2 (true streaming):** `OpenAIChat.chat(stream=True)` -> generator
  of deltas; `MemoryHarness.step(..., stream=callback)` invokes it per chunk;
  inline-tag parsing (`split_tagged`) becomes incremental: emit visible text
  outside tags immediately, buffer at `<` until tag identity is known,
  suppress the markup. SystemOne post-turn router pass and eviction stay
  post-completion (they already are); `clm` event chunks carry chips when
  they fire. This lands as a separate milestone; nothing in the refactor
  blocks it.

## 8. Ideas (worth keeping, roughly ranked)

1. **A shared request SDK used by all four transports.** Today `remote.py`
   hand-rolls urllib calls, app.js hand-rolls fetch, mcp.py hand-rolls JSON-RPC
   dispatch. One `clients/sdk.py` (base url, auth headers, error mapping,
   SSE reader) makes the studio's JS mirror exactly what RemoteHarness can
   do - which is the point: *one contract, many languages of client*.
2. **Route table.** Replace if-chains with a declarative table in
   `serving/routes.py` (method, path pattern, handler, auth mode). Testable:
   assert every documented route exists and nothing undocumented does.
3. **Session as an object, not a dict entry.** `Supervisor.sessions[sid]`
   becomes a `Session` with metadata (created_at, last_used, chat slots,
   config template). Enables `PATCH /v1/clm/sessions/<id>` later
   (rename/options), idle-TTL eviction, and honest reset semantics.
4. **Chat-open seeds are turns.** The spike's seeded chats should flow
   through the same code path as real turns (`harness.remember(...)` stays,
   but `/turns` with `role:user` + `seed:true` could unify them). Avoid two
   write paths into the window.
5. **CLI as a client of its own API.** `clm-mem api` (name bikeshedding
   welcome) = harness server without backend spawning: requires
   `--backend-url` or `--use-mock`, no UI, no native passthrough routes
   advertised. That's G1's literal deliverable: "run as an api from the
   command line." The all-in-one `clm-mem serve` (G2) stays exactly
   today's shape plus the new verbs.
6. **MCP becomes a thin mapping** onto route handlers (it mostly is already)
   and gains `open_session` / `turn` tools mirroring the new verbs, so an MCP
   agent can create sessions explicitly.
7. **Persist session templates.** Workspaces already persist files; adding
   `workspace.json` with last-used options means a reopened workspace keeps
   its budget/keep-pct instead of defaults - cheap, high quality-of-life.
8. **Deprecate the implicit bare-id session** after compat ends: every
   harness lives in a named workspace. Removes the weird "session vs
   ws:session" duality from health output and tests.
9. **`clm-mem chat --connect` rides RemoteHarness with explicit open** -
   one-time `POST /v1/clm/sessions` at connect, then `/turns`. CLI UX
   unchanged; internals unified.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Breaking Unsloth Studio / openai-python users | compat mode default for one release; studio migrates same-origin immediately; telemetry before flip |
| Two parallel implementations during transition | compat path is *an adapter into* the new handlers, never a second code path |
| Spike branch conflicts with refactor | land spike work on `main` first (fix the failing test), then refactor atop it |
| Streaming rewrite tempts scope creep | S1/S2 split; S1 ships with the session API, S2 is its own milestone |
| Layer rules rot | import-check tests in CI (`tests/test_layers.py`) |
| Session creation changing semantics (options at open vs per-request) | keep per-request overrides for model/temperature as today; only *config-shaping* knobs move to open-time |

## 10. Milestones (what an agent executes)

Each milestone = one feature branch off `main`, small commits, black-formatted,
tests green at every step (`.venv/bin/python -m pytest -q`). AGENTS.md governs
the rest.

### M0 - Land the spike (`spike/chat-isolation-seed` -> `main`)
- Resolve the failing `test_phase3_ranks_by_value_when_decay_on`: decide
  whether freshness (`last_seen`) participates in phase-3 value ranking or
  only in decay demotion; make test and code agree deliberately.
- Commit reheat access channels + tests; squash per AGENTS.md when merging.
- Deliverable: main green with chat-isolation semantics.

### M1 - Layering (`refactor/layers`)
- Create `engine/`, `serving/`, `clients/` packages by *moving* modules
  (git mv), update imports, keep flat aliases in `__init__.py`.
- Split `server.py`: supervisor/config/process wiring -> `serving/supervisor.py`;
  handler + route logic stays (still if-chains at this stage).
- Add `tests/test_layers.py` enforcing the import rules (section 6).
- No behavior change; all existing tests pass unchanged.

### M2 - Route table (`refactor/routes`)
- Declarative routes in `serving/routes.py`; handler methods become plain
  functions `(sup, req) -> reply`. Same paths, same bodies - pure structure.
- One test asserts the full documented route set == implemented route set.

### M3 - Session API (`feat/session-api`)
- Implement `POST /v1/clm/sessions`, `/turns` (non-stream + SSE replay via
  shared helper), transcript GET; `Session` object with metadata.
- Compat adapter: header path calls the same handlers.
- `clients/remote.py`: add `open()`, `turn()`; keep `step()` as sugar over
  them (RemoteHarness becomes the reference SDK).
- MCP: add `clm_open_session`, `clm_turn` tools.
- `--memory-entry` flag; default `both`; telemetry in `/health`.

### M4 - Clients migrate (`feat/clients-migrate`)
- Web studio app.js: send via `/turns` (with SSE), open session explicitly,
  chat-open/reset unchanged. Keep UI behavior identical (scroll tests exist).
- CLI chat --connect uses open+turns; demo unaffected.
- README rewrite of the "Client contract" section: documented verbs, modes,
  migration table for header users.

### M5 - Flip & clean (`refactor/flip-defaults`, one release later)
- Default `--memory-entry=explicit`; legacy adapter behind flag; remove bare-id
  implicit sessions when compat ends.
- Update Unsloth-facing docs (the README's openai-python example).

### M6 - True streaming (`feat/streaming`, milestone S2)
- `OpenAIChat.chat(stream=True)`; incremental tag-splitting in harness;
  `/turns` streams real deltas + event chips live.
- Tests: mock backend streams chunk-by-chunk; assert tag markup never leaks
  into visible deltas and inline classification still applies.

### M7 - Harness-only API (`feat/api-command`) *(or fold into M3)*
- `clm-mem api` subcommand: session verbs only, backend required/mock,
  no studio/no native routes; documents G1.
- Optional: workspace.json templates (idea section 8.7).

## 11. Test & docs plan per milestone

- Every new endpoint gets a `tests/test_server.py` case using the mock
  backends (`--use-mock` equivalents via fixtures already in repo).
- Compat adapter tests: header path produces byte-identical responses to
  what today's `_chat_completions` produced (golden-ish assertions on shape).
- Layer tests (section 6) run always.
- README: rewrite sections "Drop-in llama.cpp server", "Web studio +
  workspaces + MCP", and "Client contract" when M3/M4 land; add a
  `Migration` section listing header -> verb mappings.
- Keep `docs/refactor-plan.md` (this file) updated with status checkboxes as
  milestones complete - it is the handoff document for follow-on agents.

## 12. Open questions for the owner

1. **Session == workspace?** Today a session *is* a workspace harness
   (`ws:<name>`). The proposal keeps that. Alternative: sessions become
   short-lived objects *inside* a workspace (many concurrent chats with
   independent live windows). This is a real semantic fork - the spike's
   chat isolation hints at wanting it. Which way?
2. **Streaming priority.** Is S2 (true streaming) required for v0.4, or is
   the replayed SSE acceptable while clients migrate? Affects M6 ordering.
3. **Compat window length** - one release then flip to `explicit` default,
   or keep `both` longer? Unsloth Studio integration is the main consumer
   to check before flipping.
4. Are bare-id legacy sessions (no `ws:` prefix) worth keeping past compat?
5. Does the CLI chat's local mode (no server, direct harness) need any of
   this, or does it stay pure-engine and untouched? (Assumed: untouched.)
6. Naming bikeshed: `/turns` vs `/messages` vs `/say`; `clm-mem api` vs
   `clm-mem serve --library`. Not blocking; pick when M3 starts.
