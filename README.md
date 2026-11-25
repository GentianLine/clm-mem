# clm-mem

A **zero-training memory harness** for open chat models (Qwen and friends).
The same model that reads and writes text also classifies its own context as
**long-term** / **short-term** memory while decoding; a small deterministic
eviction engine then enforces the token budget.

Even though the eviction engine is deterministic the model itself is deciding
what to keep and how important it is as its creating context instead of after its grown.
So the model is in control but the hardware has more reliable expectations about its load
and no new LV cache strategies are required.

Design inspired by *Context Language Models* (Shao et al., arXiv:2609.37725):
context is treated as an editable artifact mirrored to disk, and context
management is delegated to the model via prompting  **no fine-tuning**,
works off-the-shelf with any OpenAI-compatible server (vLLM, Ollama,
llama.cpp, LM Studio).

One of CLMs core problems is that the method in the paper  invalidates the kvcache each request.
This means novel improvements had/have to be made to KV cache management for the paper to be effective
but  any improvements made would help this solution as well.
By compacting only when the context grows to a maximum size its reducing the amount KV churn.

This also means models can be improved by training within this harness and 
then having the trained model use the harness again until it performs worse than without it.


## Current status (2026-11-24, `main` @ `deea886`)

**Version 0.4.0 — feature-complete core, mid-refactor.** The package was
renamed `clm_mem` → **`shatterling`** and split into subpackages (the rename
is done; the docs below still use `clm-mem`, which remains the CLI name):

| Subpackage | Role |
|---|---|
| `shatterling.engine` | pure memory engine: blocks, tagging, token counting, harness + 3-phase eviction, LLM clients |
| `shatterling.purslane` | the `serve` daemon: llama.cpp-shaped proxy, session/branch API, studio REST, MCP, security layer |
| `shatterling.campion` | the web studio (static assets served by purslane) |
| `shatterling.clients` | `RemoteHarness` SDK + `connect()` helper |
| `shatterling.tools` | agent tools (web / exec / fs), sandbox, tool manager |

Two console scripts are installed: **`purslane`** (current name) and
**`clm-mem`** (deprecated alias, removed in 0.5). Python imports use the new
name (`from shatterling import ...`); the old `clm_mem` top-level package no
longer exists in this repo.

State of the world:

- **Tests: 162 passing** (`.venv/bin/python -m pytest tests/ -q`, ~6 min, mock
  backends only — no GPU, weights, or network needed).
- **End-to-end verified:** `serve --use-mock` brings up the full topology
  (proxy + mock backend + mock SystemOne), opens branches, runs turns with
  harness truth in the reply, and serves the studio + MCP at the same URL.
- **Security audit done** (`SECURITY_AUDIT.md`, 2026-11-09) and remediated in
  `deea886`: SSRF/scheme guards on web tools, request-body cap, no upstream
  auth-header relay by default, session/workspace id validation,
  per-session `run_python` scratch paths, sandbox caveats surfaced in UI.
  Binding non-loopback without an API key now **refuses to start** (was: warn).
- **Explicit session API is the contract** (milestones M3/M4): memory lives
  behind `POST /v1/clm/sessions` + `.../turns`; legacy headers get a 410.
- **Milestone status** (plan in `docs/refactor-plan.md`):

| Milestone | Status |
|---|---|
| M0 chat-isolation spike, phase-3 ranking semantics | done (test re-expressed on both freshness channels) |
| M1 layering (`engine/` `purslane/` `clients/`) | done (shatterling split) |
| M2 declarative route table + timing recorder | **stub only** — `purslane/routes.py` and `purslane/metrics.py` exist but are not wired into the server or tests yet |
| M3 explicit session API (breaking, no compat window) | done |
| M4 clients migrate (studio / CLI / MCP / SDK) | done |
| M5 flip & clean | vaporware by design; only the 410 legacy-header shim remains to delete once consumers migrate |
| M6 true streaming (real SSE deltas) | not started — `/turns` SSE is still a post-hoc replay of the finished reply |
| M7 harness-only `clm-mem api` subcommand | not started |

Open items worth knowing before you hack: see the **Roadmap** section at the
bottom. Unmerged local branch: `fix/search-ddg-lite-fallback` (one commit —
ddg search results fix).

## How it works

Every message lives in the live window as a `Block` tagged
`longterm`, `shortterm`, or `transient`. Tagging happens two ways, using the
generated model itself:

1. **Inline ("classify as it decodes")**  a system preamble instructs the
   model to wrap durable content in `<longterm>…</longterm>` and working
   state in `<shortterm>…</shortterm>`. The harness splits each reply on
   those markers into separately-tagged blocks and strips the markup from
   what the user sees.
2. **Post-turn router pass**   a second cheap call to the *same* model
   labels this turn's untagged blocks as JSON (`longterm / shortterm /
   transient`). `classify_mode="both"` (default) uses both paths.

When the window exceeds `max_context_tokens`, eviction runs in phases:

| Phase | Condition | Action |
|---|---|---|
| **1** | over budget | delete every `transient` block (except the last `keep_recent_turns` turns and pinned system blocks) |
| **2** | window is now only long+short memory | evict **shortterm FIFO in rounds**: each round keeps the newest `shortterm_keep_pct` fraction, deletes the oldest; repeats while still over budget |
| **3** | long-term alone overflows | offload oldest long-term blocks to `archive.jsonl`, leave an evictable pointer stub (retrievable via `/recall`) |

Long-term memory is never silently destroyed   it is only ever offloaded to
the archive, fixing the "can offload but can't evict" failure mode the paper's
ContextBench exposes in tool-based baselines.

Each turn the live window is mirrored to `context.md` (human-readable,
paper-style `[[CTX_TURN n role=… tag=…]]` markers) and `context.jsonl`
(machine-readable), like the paper's context-as-a-file.

## Install

All dependencies are declared in **`pyproject.toml`** (the single source of
truth). Create a virtual environment and install the package into it:

```bash
cd clm-mem
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"     # package + pytest (recommended for hacking)
pip install -e .            # runtime only (needs: openai client)
pip install -e ".[tokens]"  # + exact token counting via the model's HF tokenizer
```

For a byte-for-byte reproducible environment, use the frozen lockfile that
pairs with this checkout (deps pinned to the exact versions tested):

```bash
pip install -r requirements.lock.txt   # pinned versions (also at workspace root)
pip install -e ./clm-mem               # then the package itself, editable
```

Deactivate / clean up any time with `deactivate` and by deleting `.venv/`.

Python ≥ 3.10.

## Serve a model

Any OpenAI-compatible endpoint works. Examples with Qwen:

```bash
# vLLM
vllm serve Qwen/Qwen3-8B-Instruct --max-model-len 16384

# Ollama (openai-compatible at :11434/v1)
ollama pull qwen3:8b
```

## Drop-in llama.cpp server: `clm-mem serve`

`clm-mem serve` turns the harness into a **single llama.cpp-shaped endpoint**:
to any OpenAI/llama.cpp client (Unsloth Studio, openai-python, text-gen-webui,
an LLM agent runtime...) it looks like one ordinary server at one host:port.
Behind it, `serve` brings up 2-3 background processes; memory lives behind
explicit session verbs (`POST /v1/clm/sessions`, `.../turns`) rather than
chat-completions headers -- see *Client contract* below:

```
client ──http──> clm-mem proxy :8080          <- what clients talk to
                    ├── llama-server / Unsloth / vLLM / Ollama backend :auto
                    └── System One decision server (fork or mock)   :auto
```

Nothing is downloaded or compiled by `serve`  it only execs binaries or URLs
you point it at. With `--use-mock` it runs the bundled fake backends so the
whole stack works with no GPU and no weights.

```bash
# smoke-test the whole topology (mock backend + mock SystemOne):
clm-mem serve --use-mock --port 8080 --served-model mock-model \
    --max-context-tokens 600 --workdir /tmp/clm-serve

# real llama.cpp behind it:
clm-mem serve --port 8080 --llama-server ./build/bin/llama-server \
    --model-file models/Qwen3-8B-IQ2_K.gguf --ctx-size 16384 \
    --systemone-passthrough          # fork build answers the decision route itself

# already-running server + separate SystemOne API:
# NOTE: --systemone-url is the COMPLETE decision endpoint URL and is used
# exactly as given -- nothing is ever appended to it. Each provider exposes
# the decision endpoint under a different URL structure (/v1/decisions,
# /v1/systemone, /v1/classifier) but the request and response are identical
# everywhere, so point the flag at whichever your server answers.
clm-mem serve --backend-url http://localhost:8000/v1 \
    --systemone-url http://localhost:8010/v1/decisions

# hosted chat backend (Featherless, OpenRouter, ...): --backend-api-key is the
# bearer token the proxy sends on every LLM call it fronts -- including the
# harness's own calls and context-window discovery. Local servers ignore it;
# when unset, any Authorization header clients send is relayed instead:
clm-mem serve --port 8080 \
    --backend-url https://openrouter.ai/api/v1 \
    --backend-api-key "$OPENROUTER_API_KEY"

# pass the *entire* backend command line, options included (no flag guessing):
clm-mem serve --port 8080 \
    --backend-cmd "./build/bin/llama-server -m models/qwen.gguf --n-gpu-layers 99 --ctx-size 16384" \
    --systemone-cmd "./build/bin/server-decision -m systemone.gguf"

# hosted Featherless decision API instead of a local SystemOne server:
clm-mem serve --backend-url http://localhost:8000/v1 \
    --systemone-url https://api.featherless.ai/v1/classifier \
    --systemone-model featherless-ai/Qwen3.6-35B-A3B-classifier
    # auth via $FEATHERLESS_API_KEY or --systemone-api-key
```

When `--backend-cmd` (or `--systemone-cmd`) contains `{host}` / `{port}`
placeholders they are expanded in place; otherwise the server is launched with
`--host <ip> --port <port>` appended. The harness budget follows the backend:
if `--max-context-tokens` is not given, `serve` discovers the live llama.cpp
context window (`/props -> default_generation_settings.n_ctx`) and sets the
clm-mem budget to exactly that value, so the two windows never drift apart.

`serve` prints a machine-readable READY line (JSON) once everything is up, including the web-studio and MCP endpoints:

```json
{"ready": true, "base_url": "http://127.0.0.1:8080/v1", "root_url": "http://127.0.0.1:8080",
 "model": "mock-model", "backend": "http://127.0.0.1:...", "systemone": "http://127.0.0.1:...",
 "ui_url": "http://127.0.0.1:8080", "mcp_url": "http://127.0.0.1:8080/mcp",
 "processes": {"backend-mock": "...", "systemone-mock": "..."}}
```

**Client contract (memory is explicit).** A memory session is a **branch**:
it owns its harness (live window + archive) and sandbox, addressed as
`ws:<name>`. You *open* a branch, then you *turn* on it -- memory is never a
header side effect of `/v1/chat/completions`, which stays pure passthrough
(exactly plain llama.cpp for any OpenAI client pointed at `base_url` with
`api_key="EMPTY"`). The retired `X-Cllm-Mem-Session` / `X-Cllm-Mem-Chat`
headers are rejected with a **410** that points at the verbs to use:

```bash
curl -X POST http://localhost:8080/v1/clm/sessions \
     -H 'Content-Type: application/json' \
     -d '{"session": "ws:unsloth-user"}'          # -> 201 {created: true, budget...}

curl -X POST http://localhost:8080/v1/clm/sessions/ws%3Aunsloth-user/turns \
     -H 'Content-Type: application/json' \
     -d '{"content": "Remember: the deploy port is 8080."}'   # -> memory-managed reply
```

```python
from shatterling import RemoteHarness
h = RemoteHarness("http://localhost:8080/v1", session="ws:unsloth-user")
h.open()                              # POST /v1/clm/sessions (idempotent)
h.turn("Remember: the deploy port is 8080.")   # POST .../turns
```

The turns endpoint mirrors chat-completions where useful (`content` or a
full `messages` array, `model`, `temperature`, `stream`) and differs where
correct: it **never creates sessions** (409 on an unknown branch), it names
its chat slot in the reply, and its answer carries harness truth -- counts,
budget, archive size and this turn's event chips under `clm_mem`. With
`"stream": true` it answers SSE: role delta, content deltas, one
`event: clm-mem-event` chunk per memory chip, then `[DONE]`.
Open-time options (`max_context_tokens`, `shortterm_keep_pct`,
classify/decay/supersede/tools knobs...) ride on the open call and build the
branch's config; generation knobs stay per-request.

Supported surface (llama.cpp + OpenAI union): `/v1/chat/completions` pure
passthrough (incl. SSE streaming), `/v1/models`, `/v1/decisions` (accepted
incoming as the provider alias spellings `/v1/systemone` and
`/v1/classifier` too, same contract; the proxy relays to `--systemone-url`
verbatim -- that flag is the complete endpoint URL and nothing is appended
to it), native `/completion`, `/props`, `/metrics`, `/version`, `/health`,
CORS preflight. Memory surface: `POST /v1/clm/sessions` (open), `POST
/v1/clm/sessions/<id>/turns` (+ `GET` transcript replay), introspection
routes `/v1/clm/sessions`, `/v1/clm/sessions/<id>/status`, `.../reset`, and
`DELETE /v1/clm/sessions/<id>` — plus the full studio API below.

Session ids are branch ids: `ws:<name>`. Percent-encode the colon in URL
paths (`ws%3Aname`) or leave it bare -- both spellings route identically.

## Security

### Defaults (secure-by-default)

| Setting | Default | Rationale |
|---------|---------|----------|
| Bind address | `127.0.0.1` | Local-only; not reachable from the network |
| CORS | Same-origin only | No `Access-Control-Allow-Origin` header for cross-origin requests, preventing CSRF from arbitrary web pages |
| API auth | Disabled (on loopback) | Safe when bound to localhost; warned when non-loopback |

### API key authentication

Enable bearer-token auth on all `/v1/*` and `/mcp` routes:

```bash
# Generate a key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Start with auth
clm-mem serve --use-mock --api-key YOUR_KEY_HERE

# Or via environment variable
export CLM_MEM_API_KEY=YOUR_KEY_HERE
clm-mem serve --use-mock
```

Clients send the key as a standard Bearer header:

```bash
curl -H "Authorization: Bearer YOUR_KEY" http://127.0.0.1:8080/v1/clm/sessions
```

**Exempt paths** (always accessible without auth): `/health`, `/v1/health`,
`/version`, and static assets (the web studio shell). This lets monitoring
probes and the browser load the UI before the JS authenticates API calls.

### CORS policy

By default, **no cross-origin requests are answered** -- the server sends no
`Access-Control-Allow-Origin` header for foreign origins, so browsers block
any cross-site reads. This prevents CSRF attacks when auth is disabled.

To allow specific origins (e.g., a remote studio or dashboard):

```bash
clm-mem serve --use-mock \
  --api-key YOUR_KEY \
  --cors-origins "https://studio.example.com,http://localhost:3000"
```

Or via environment: `CLM_MEM_CORS_ORIGINS=https://studio.example.com`

> **Warning:** `--cors-origins '*'` combined with no API key allows any
> website in a user's browser to make requests to your server. The server
> prints a prominent startup warning for this combination.

### Startup warnings

The server prints warnings to stderr when it detects dangerous configurations:

- Bound to a non-loopback address without an API key
- CORS set to `*` with no authentication (CSRF risk)
- Non-loopback bind without auth (LAN exposure)

### TLS / reverse-proxy deployment

The server is plain HTTP and does **not** terminate TLS. For any deployment
reachable beyond localhost, put a TLS-terminating reverse proxy in front of it
and enable `--api-key`. Full topologies, nginx/Caddy configs (including the
Server-Sent-Events buffering gotcha for `/turns`), mTLS notes, and a
deployment checklist live in [`docs/deployment.md`](docs/deployment.md).

### Attaching clients (CLI / SDK / tokamak)

The Python SDK and CLI automatically pick up the key from the environment:

```python
from shatterling.clients.sdk import connect

# Key comes from CLM_MEM_API_KEY env var, or pass explicitly
h = connect("http://127.0.0.1:8080/v1", workspace="myproject")
# or
h = connect("http://127.0.0.1:8080/v1", workspace="myproject", api_key="..." )
h.step("hello")
```

CLI:
```bash
export CLM_MEM_API_KEY=YOUR_KEY
clm-mem chat --connect http://127.0.0.1:8080/v1 --workspace myproject
```

**For tokamak or other projects:** set `CLM_MEM_API_KEY` in the environment
before starting `clm-mem serve`, and the same variable in any client process.
No code changes needed -- the SDK reads it automatically. For programmatic
integration, pass `api_key=` to `connect()` or construct `RemoteHarness`
directly with the key.

## Web studio + workspaces + MCP

By default `serve` also hosts a single-page web studio at its root URL
(`http://127.0.0.1:8080/`; disable with `--no-ui`). It is built for people
juggling several projects:

* **Projects sidebar** — each workspace is an isolated sandbox (its own
  `context.md` / `archive.jsonl` under `<workdir>/workspaces/<name>`), with a
  live token meter and per-tier block dots. Create/delete from the UI or via
  `POST`/`DELETE /v1/clm/workspaces`.
* **Chat with context hints, not raw dumps** — long-term / short-term /
  transient appear as colored badges on your messages, eviction phases fire
  as compact chips under a reply (`🧹 phase2 · FIFO-evicted …`, `🛡 backstop ·
  kept #4`), and the context panel shows collapsed previews + token counts
  instead of full text ("show full block" fetches one block on demand).
* **Archive search** — type in the Archive tab to substring-search evicted
  memory; one click re-injects any hit back into the live window.
* **Tuning tab** — `shortterm_keep_pct`, budget and SystemOne threshold are
  changed live per project (`POST .../settings`).
* **Events timeline** — every classifier decision and eviction phase, with
  its kind badge and sequence number.

Session ids drive the studio: anything named `ws:<project>` gets that
project's sandbox; plain ids still behave like classic single sessions.

Studio API (same-origin, JSON):

```
GET    /v1/clm/workspaces                    list projects + live stats
POST   /v1/clm/workspaces        {name}     create a sandbox
DELETE /v1/clm/workspaces/<name>            delete it (files included)
POST   /v1/clm/sessions       {session,model?,options?}
                                             open a branch (410-free zone:
                                             this is where memory begins)
POST   /v1/clm/sessions/<id>/turns {content|messages, chat?, stream?}
                                             the conversation verb (+SSE)
GET    /v1/clm/sessions/<id>/turns?chat=…   transcript replay for one chat
GET    /v1/clm/sessions                    list live session ids
GET    /v1/clm/sessions/<id>/snapshot[?full=1]   full context view
GET    /v1/clm/sessions/<id>/archive?q=…   search the eviction archive
POST   /v1/clm/sessions/<id>/reinject {block_id}
POST   /v1/clm/sessions/<id>/settings {shortterm_keep_pct, max_context_tokens,
                                          systemone_evict_threshold}
GET    /v1/clm/sessions/<id>/tools          agent tool state (+ toggle)
POST   /v1/clm/sessions/<id>/tools {target,on} or {approval_mode}
GET    /v1/clm/sessions/<id>/activity?limit=&full=   tool activity log
GET    /v1/clm/sessions/<id>/approvals      pending + resolved approvals
POST   /v1/clm/sessions/<id>/approvals {id,approve}  decide one
POST   /v1/clm/sessions/<id>/search {query}         direct web search
POST   /v1/clm/sessions/<id>/toolcall {tool,args}   run any tool directly
GET    /v1/clm/sessions/<id>/sandbox[?path=]        sandbox info + file list
GET    /v1/clm/sessions/<id>/sandbox/file?path=     read one sandbox file
POST   /v1/clm/sessions/<id>/sandbox/exec {command} human terminal command
POST   /v1/clm/sessions/<id>/sandbox/write {path,content}
DELETE /v1/clm/sessions/<id>/sandbox              reset (wipe) the sandbox
```

**MCP toolbox.** `serve` is also an MCP server (streamable HTTP JSON-RPC at
`/mcp`): `initialize`, `tools/list`, `tools/call` with tools
`clm_open_session`, `clm_chat`,
`clm_status`, `clm_blocks`, `clm_recall`, `clm_reinject`,
`clm_set_keep_pct`, `clm_workspaces`, plus the agent surface — `clm_tools`,
`clm_tool_on`, `clm_tool_call`, `clm_sandbox_exec`, `clm_web_search`. Point
any MCP client at the URL and it gets the identical per-workspace memory and
the identical tools the web studio toggles control — same library, same
harnesses, no extra process.

## Agent mode: sessions that also act

Memory alone decides what a session keeps; `--tools` lets the same session
also *do* things — web search, sandboxed commands and file work. The point
of building it next to the memory harness is that **tool output is just more
context**: executed calls enter the live window as short-term blocks and obey
the same budget and eviction rules as chat, so an agent's long scratchpad is
forgettable working state instead of a context bomb.

Seven tools in three categories:

| Category | Tools | Notes |
|---|---|---|
| **web** | `web_search`, `web_fetch` | read-only; providers: `ddg` (no key), `brave`/`tavily`/`exa` (`$…_API_KEY`), `stub` (offline deterministic), `off` |
| **exec** | `run_command`, `run_python` | mutating; run inside the session sandbox |
| **fs** | `write_file`, `read_file`, `list_files` | read tools are read-only; paths are jail-contained |

**Sandbox levels** (`--sandbox-mode`): every workspace gets its own sandbox
under `<workdir>/workspaces/<name>/sandbox`. `jail` (default) is a *limited
process*: subprocess pinned to the sandbox dir with a scrubbed environment
(host secrets like API keys never leak in), wall-clock timeout, CPU/address-
space/file-size/process rlimits and output caps — strong enough for research
and demos, honest about not being a security boundary against a hostile
script. `docker` runs each command in a real container (`--network none`, memory/cpu/volume limits) when the docker CLI is available, falling back to
the jail with an explanatory note; the UI/CLI always show which level
actually ran. `off` removes exec/fs tools entirely.

**Approval policies** (`--ask` / `--approval-mode`): `auto` (default) runs
mutating tools inside the sandbox without asking; `ask` pauses them until a
human decides — inline prompt in a local CLI, live approval card in the web
studio, or REST/MCP from anywhere; `deny` never runs them. Read-only tools
never need approval, and human-initiated calls (composer buttons, slash
commands) are approved by definition.

```bash
clm-mem serve --use-mock --tools --search-provider stub   # offline demo
clm-mem serve --backend-url http://localhost:8000/v1 --tools \
    --sandbox-mode docker --ask                            # real model, containers
```

**Web studio UX (Unsloth-Studio-shaped).** The composer shows a tool bar —
toggles for 🌐 web / 💻 run / 📁 files plus an approval-policy select; when
the model uses tools mid-turn, **live activity cards** appear in the
transcript (spinner while running, ✓/✗/denied states, click to expand full
output), and under `ask` an **approval card** with approve/deny buttons
appears right where the request happened. A new **Sandbox tab** gives each
project a mini terminal (`$ …` log), file list with viewer, isolation level,
disk usage vs quota, and a reset button. Tool calls never print raw context
at you: summaries stay collapsed, details expand on demand.

**CLI UX (the same language at the prompt).** `clm-mem chat` gains an agent
cast of slash commands, identical in meaning to the studio's toggles/cards:

```
/tools                       state per category + policy + sandbox isolation
/tools on|off web|exec|fs|all|<tool>      toggles (same as the composer bar)
/tools auto|ask|deny         approval policy
/web <query>                 direct web search (results laid out, not dumped)
/fetch <url>                 fetch a page
/run <command>  /py <code>   sandbox exec / python snippet
/files [path]  /cat <path>  /rm <path>     file work
/sandbox [info|reset]        isolation level, usage vs quota, wipe
/activity [n]  /tool <seq>   activity log; expand one entry's output
/approvals   /approve <id>   /deny <id>    resolve pending requests
```

After a model turn that used tools the CLI prints a `⚡ tools used this
turn:` summary (one line per call, deduped across sessions). Under `ask`, a
local session prompts inline (`allow? [y/n/a]` — `a` switches the rest of the
session to auto); an *attached* session under `ask` resolves through the
studio cards or a second terminal running `/approvals` + `/approve <id>`.
The bundled mock backend answers `tools=[...]` requests with deterministic
tool-calls planned from wording ("search …", "run command …", "python …"),
so the whole agent loop — search → fetch → exec → files — is exercisable
offline end to end, in serve, web studio, CLI and MCP alike.

## Attach a CLI chat to a running server

`chat` and `serve` are two ends of the same stick — attach the prompt to a
running server (web studio stays usable meanwhile):

```bash
clm-mem serve --use-mock --port 8080                     # or with real backends
clm-mem chat --connect http://127.0.0.1:8080/v1 --workspace myproj
```

All in-session commands (`/status`, `/long`, `/recall`, `/reinject`, `/keep`)
then talk to the server's REST API — the same ones the studio's UI buttons
call — and `RemoteHarness` (in the library) mirrors live config knobs:
setting `h.cfg.shortterm_keep_pct = 0.8` pushes it to the running session.

## Run the chat harness

```bash
clm-mem chat \
  --base-url http://localhost:8000/v1 \
  --model Qwen/Qwen3-8B-Instruct \
  --max-context-tokens 8192 \
  --shortterm-keep-pct 0.5
```

Ollama variant:

```bash
clm-mem chat --base-url http://localhost:11434/v1 --model qwen3:8b \
  --max-context-tokens 6144 --shortterm-keep-pct 0.75
```

In-session commands:

```
/status                # tokens used, block counts per memory tier
/long    /short        # list live long-term / short-term blocks
/recall <text>         # substring search the eviction archive
/reinject <block-id>   # pull an archived block back into context (as shortterm)
/keep <pct>            # change shortterm_keep_pct mid-session, e.g. /keep 0.75
/quit
```

## Demo (no GPU, no server)

Exercises the full pipeline   inline tagging, router pass, all three eviction
phases   with a deterministic stub model:

```bash
clm-mem demo --max-context-tokens 900 --shortterm-keep-pct 0.5
```

You should see phases fire like:

```
  [evict] phase1: deleted 4 transient block(s)
  [evict] phase2: FIFO-evicted 3 of 5 shortterm (kept newest 50%)
  [evict] phase3: offloaded longterm #17 to archive, left pointer
```

## Key options

| Flag | Default | Meaning |
|---|---|---|
| `--max-context-tokens` | 8192 | live-window budget (keep below your server's `--max-model-len`) |
| `--shortterm-keep-pct` | 0.5 | **the memory knob**: fraction of short-term kept per FIFO round (`/keep` to change live) |
| `--keep-recent-turns` | 2 | trailing turns exempt from phase-1 transient deletion |
| `--classify-mode` | both | `inline`, `post`, or `both` tagging paths |
| `--tokenizer` | = model id | HF tokenizer for exact counts (heuristic ~4 chars/token if unavailable) |
| `--workdir` | `.` | where `context.md`, `context.jsonl`, `archive.jsonl` live |
| `--api-key` (`chat`/`demo`) | `EMPTY` | bearer token for the chat backend API; hosted services require one, local llama.cpp/vLLM/Ollama ignore it (`--backend-api-key` is an accepted alias) |
| `--backend-api-key` (`serve`) | unset | bearer token the proxy sends to the chat backend on every LLM call; when unset, clients' own `Authorization` headers are relayed |

## Use as a library

```python
from shatterling import MemoryHarness, HarnessConfig, OpenAIChat

h = MemoryHarness(
    OpenAIChat("http://localhost:8000/v1", "Qwen/Qwen3-8B-Instruct"),
    HarnessConfig(max_context_tokens=8192, shortterm_keep_pct=0.5),
)

print(h.step("Remember: the staging deploy port is 8080."))
h.cfg.shortterm_keep_pct = 0.75          # tune the knob at runtime
print(h.status())                         # tokens/blocks per tier
print(h.recall("8080"))                   # search evicted/offloaded memory
```

## Tests

With the venv active (`source .venv/bin/activate`):

```bash
python -m pytest tests/ -q     # harness + serve/proxy tests, no GPU/server needed
```

## Limitations vs. the paper

- The eviction policy here is **harness-defined**; the paper's headline result
  comes from letting the model *edit* the context file itself (e.g. via a
  Bash tool). The `context.md` mirror is there if you want to bolt that on.
- Tag quality rides on the base model's zero-shot judgment; small models
  occasionally forget the markers   the post-turn router pass is the backstop.
- Phase-3 offload keeps pointer stubs in-window, so a pathological long-term-
  heavy session still needs `--max-context-tokens` headroom for the preamble.

## Roadmap / TODO

Ordered roughly by value; each item is small enough for one feature branch
(see `AGENTS.md` for the workflow).

**Ship what's half-built (M2 finish)**
- [x] Wire the route table: make `ProxyHandler` dispatch through
      `purslane/routes.py::match()` instead of if-chains, and add the promised
      parity test asserting *documented routes == implemented routes*.
      Until then `routes.py` is dead documentation that can drift silently.
- [x] Wire `TimingRecorder`/`SessionMetrics` (`purslane/metrics.py`) into
      `/turns` responses (the `_timings` sidecar + final SSE frame the module
      docstring already describes) — or delete the module. It is currently
      imported by nothing.

**M6 — true streaming**
- [ ] `OpenAIChat.chat(stream=True)` with incremental tag-splitting in the
      harness, so `/turns` streams real deltas and event chips live instead of
      replaying a finished string. Test that `<longterm>`/`<shortterm>` markup
      never leaks into visible deltas.

**M7 — harness-only API (documents G1)**
- [x] `clm-mem api` subcommand: session verbs only, backend required or mock,
      no studio / native passthrough routes. Lets the engine run as a bare
      memory API without the full proxy surface.

**Security follow-ups (post-audit)**
- [x] TLS story: documented in `docs/deployment.md` — reverse-proxy + TLS/mTLS
      setup for non-loopback deployments (the server warns but does not terminate TLS).
- [ ] Per-route capability split: let a memory-only client get no exec/file/
      web-fetch routes at all (audit C-1 remediation left this as a
      "consider").
- [x] Rate limiting on the proxy: per-client fixed-window limiter, `/health`
      exempt, 429 + Retry-After; `--rate-limit` / `CLM_MEM_RATE_LIMIT` (0 disables).

**Housekeeping**
- [ ] Merge or explicitly drop `fix/search-ddg-lite-fallback` (ddg search fix,
      one commit ahead of main).
- [ ] Delete the 410 legacy-header shim once no downstream consumer sends
      `X-Cllm-Mem-*` headers (M5; Unsloth Studio integration is the one to
      check).
- [x] Update `AGENTS.md` "Project Orientation" — refreshed to the subpackage
      (`shatterling/{engine,purslane,campion,clients}`) layout.
- [x] Refresh `docs/library-migration.md` — re-pointed at `main` (0.4.0) +
      `shatterling/clients/sdk.py` (was the merged `spike/session-api` branch).
- [x] Add a `CHANGELOG.md` (0.4.0's breaking rename + subpackage split).
- [x] Clean repo root: agent scratch-file patterns gitignored (`_*` scratch).

**0.5 planning**
- [ ] Remove the deprecated `clm-mem` console-script alias (keep `purslane`).
- [ ] Decide distribution name: publish to PyPI as `shatterling`? Rename the
      repo / CLI accordingly, or keep `clm-mem` as the public brand and treat
      `shatterling` as the Python package.
- [ ] Re-run the security audit after M2/M6 land (new dispatch path + new
      streaming code are exactly where regressions hide).

## License

MIT
