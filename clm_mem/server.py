"""`clm-mem serve`: a llama.cpp-compatible front door + web studio.

To any OpenAI/llama.cpp client this is just *the* server: one host:port with
``/v1/chat/completions``, ``/v1/models``, ``/completion``, ``/props``,
``/metrics``, ``/version``, ``/health``. Behind it the supervisor spins up
2-3 background processes (proxy + chat backend + optional System One
decision server) and wires clm-mem's memory harness into every request that
carries an ``X-Cllm-Mem-Session`` header, so clients like Unsloth Studio get
long/short-term memory without knowing they have it.

Additionally, `serve` can host a built-in single-page web UI (the "studio":
multi-project sidebar, chat pane with memory hints instead of raw context,
live context inspector, archive search, MCP toolbox) at the same host:port,
together with a REST surface (`/v1/clm/*`) and an MCP server endpoint
(`/mcp`) that expose the very same library features `clm-mem chat` offers.

Nothing is downloaded or compiled here -- real backends are only ever exec'd
from binaries you point at; the bundled mocks cover testing.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote
from .config import HarnessConfig
from .harness import MemoryHarness
from .llm import OpenAIChat
from .processes import ProcessManager, free_port, render_flags

VERSION = "0.3.0"
SESSION_HEADER = "X-Cllm-Mem-Session"
NATIVE_GET_PATHS = ("/props", "/metrics", "/specs")
NATIVE_POST_PATHS = ("/completion", "/embedding")

# Workspace sessions are named ``ws:<name>`` so the colon keeps them in one
# URL path segment while tagging which project sandbox a harness lives in.
WS_PREFIX = "ws:"
WS_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


def normalize_root(url: str) -> str:
    """http://host:8080/v1/ -> http://host:8080 (root, not OpenAI base)."""
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[: -3].rstrip("/")
    return url


@dataclass
class ProxyConfig:
    backend_root: Optional[str] = None      # e.g. http://127.0.0.1:8081
    systemone_root: Optional[str] = None    # e.g. http://127.0.0.1:8010
    systemone_model: str = ""               # model id sent to the decision API
    systemone_api_key: str = ""             # bearer for hosted decision APIs
    served_model: str = "clm-mem-model"
    harness_kwargs: dict = field(default_factory=dict)
    chat_temperature: float = 0.6
    chat_max_tokens: int = 1024
    request_timeout: float = 300.0
    ui_enabled: bool = True                 # serve the bundled web studio at /
    backend_ctx: Optional[int] = None       # live llama.cpp window (from /props)
    systemone_alias: str = ""               # cached decision endpoint path


class ProxyHandler(BaseHTTPRequestHandler):
    """llama.cpp-shaped HTTP surface; ``self.server.supervisor`` carries state."""
    server_version = f"clm-mem-proxy/{VERSION}"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):   # pragma: no cover - noise control
        import os
        if os.environ.get("CLM_MEM_VERBOSE"):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    @property
    def supervisor(self):
        return self.server.supervisor       # type: ignore[attr-defined]

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         f"Content-Type, Authorization, {SESSION_HEADER}")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, DELETE, OPTIONS")

    def send_json(self, obj, status: int = 200):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, status: int, msg: str):
        self.send_json({"error": {"message": msg, "type": "proxy",
                                  "code": status}}, status)

    def read_body_bytes(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- forwarding --------------------------------------------------------
    def _respond_payload(self, status: int, payload: bytes,
                         content_type: str):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _start_sse(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _forward(self, method: str, url: str, body: bytes | None = None,
                 stream: bool = False, probe: bool = False):
        """Proxy one request to a backend. With ``probe=True`` the response is
        buffered and NOT sent to the client; True means "this alias exists"
        (any HTTP status other than 404/405), False means try another path."""
        req = urllib.request.Request(
            url, data=body if method.upper() != "GET" else None,
            headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req,
                                          timeout=self.supervisor.config.request_timeout)
        except urllib.error.HTTPError as e:
            if probe and e.code in (404, 405):
                return False
            self._respond_payload(e.code, e.read(),
                                  e.headers.get("Content-Type", "application/json"))
            return True
        except Exception as exc:       # backend down / connection refused
            if probe:
                return False
            self.send_error_json(502, f"backend unreachable at {url}: {exc}")
            return True
        if probe:
            payload = resp.read()
            status = getattr(resp, "status", 200) or 200
            ok = status not in (404, 405)
            if ok:
                self._respond_payload(status, payload,
                                      resp.headers.get("Content-Type",
                                                       "application/json"))
            resp.close()
            return ok
        ctype = resp.headers.get("Content-Type", "")
        if stream and ctype.startswith("text/event-stream"):
            self._start_sse()
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except Exception:          # pragma: no cover - client went away
                pass
            finally:
                resp.close()
        else:
            payload = resp.read()
            status = getattr(resp, "status", 200) or 200
            resp.close()
            self._respond_payload(status, payload, ctype)

    # -- sessions ----------------------------------------------------------
    def _session_id(self, body: dict) -> Optional[str]:
        sid = self.headers.get(SESSION_HEADER) or body.get("session_id")
        return str(sid) if sid else None

    def _harness_for(self, sid: str, model: str):
        return self.supervisor.harness_for(sid, model)

    def _sse_reply(self, sid: str, model: str, reply: str):
        self._start_sse()
        created = int(time.time())

        def chunk(delta, finish=None):
            obj = {"id": f"clm-{created}", "object": "chat.completion.chunk",
                   "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta,
                                **({"finish_reason": finish} if finish else {})}]}
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        import re
        pieces = re.findall(r"\S+\s?|\s+", reply) or ([reply] if reply else [])
        for piece in pieces:
            chunk({"content": piece})
        chunk({}, finish="stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # -- methods -------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # -- static web studio ---------------------------------------------------
    def _serve_static(self, name: str):
        fname = os.path.basename(name)         # no traversal
        fp = os.path.join(STATIC_DIR, fname)
        if not os.path.isfile(fp):
            self.send_error_json(404, f"no static asset {fname!r}")
            return
        ctype = MIME_TYPES.get(os.path.splitext(fname)[1],
                               "application/octet-stream")
        with open(fp, "rb") as f:
            payload = f.read()
        self._respond_payload(200, payload, ctype)

    def _static_route(self, path: str) -> bool:
        """Handle UI asset paths; False when the studio is disabled."""
        ui_paths = ("", "/", "/index.html", "/style.css", "/app.js",
                    "/favicon.svg")
        if path not in ui_paths:
            return False
        cfg = self.supervisor.config
        if not cfg.ui_enabled:
            self.send_error_json(404, "web studio disabled (--no-ui)")
            return True
        name = "index.html" if path in ("", "/", "/index.html") \
            else path.lstrip("/")
        self._serve_static(name)
        return True

    # -- MCP server endpoint -------------------------------------------------
    def _mcp(self):
        raw = self.read_body_bytes()
        try:
            msg = json.loads(raw.decode() or "{}")
        except Exception:
            return self.send_error_json(400, "invalid JSON-RPC body")
        from . import mcp as mcp_mod
        resp = mcp_mod.handle(self.supervisor, msg)
        if resp is None:              # notification: accepted, no reply body
            self._respond_payload(202, b"", "application/json")
            return
        self.send_json(resp)

    def do_GET(self):
        sup = self.supervisor
        cfg = sup.config
        path = unquote(self.path.split("?")[0])
        if self._static_route(path):
            return
        if path in ("/health", "/v1/health"):
            with sup.lock:
                sids = list(sup.sessions)
            backends = {h.name: f"{h.host}:{h.port}"
                        for h in sup.manager.handles if h.alive()}
            self.send_json({"status": "ok", "version": VERSION,
                            "model": cfg.served_model,
                            "sessions": sids, "processes": backends,
                            "ui": cfg.ui_enabled,
                            "workspace_root": sup.workspace_root})
        elif path in ("/mcp", "/v1/mcp"):
            self.send_json({"server": "clm-mem", "version": VERSION,
                            "transport": "streamable-http",
                            "hint": "POST JSON-RPC here "
                                    "(initialize / tools/list / tools/call)"})
        elif path == "/version":
            self.send_json({"version": VERSION, "built_at": 0,
                            "llama.cpp": {"version": f"clm-mem-{VERSION}"}})
        elif path == "/v1/models":
            ids = [cfg.served_model]
            if cfg.backend_root:
                try:
                    with urllib.request.urlopen(
                            f"{cfg.backend_root}/v1/models", timeout=5) as r:
                        data = json.loads(r.read().decode()).get("data", [])
                    for m in data:
                        mid = m.get("id")
                        if mid and mid not in ids:
                            ids.append(mid)
                except Exception:
                    pass        # offline is fine; served_model still listed
            self.send_json({"object": "list",
                            "data": [{"id": i, "object": "model",
                                      "owned_by": "clm-mem"} for i in ids]})
        elif path.startswith("/v1/clm/"):
            self._clm_get(path)
        elif path in NATIVE_GET_PATHS and cfg.backend_root:
            self._forward("GET", f"{cfg.backend_root}{path}")
        else:
            self.send_error_json(404, f"no route for {path}")

    def do_DELETE(self):
        sup = self.supervisor
        path = unquote(self.path.split("?")[0])
        if path.startswith("/v1/clm/sessions/"):
            sid = path[len("/v1/clm/sessions/"):].rstrip("/")
            existed = sup.drop_session(sid)
            if not existed:
                self.send_error_json(404, f"unknown session {sid!r}")
            else:
                self.send_json({"deleted": sid})
        elif path.startswith("/v1/clm/workspaces/"):
            name = path[len("/v1/clm/workspaces/"):].rstrip("/")
            try:
                ok = sup.delete_workspace(name)
            except ValueError as exc:
                self.send_error_json(400, str(exc))
                return
            if not ok:
                self.send_error_json(404, f"unknown workspace {name!r}")
            else:
                self.send_json({"deleted": name})
        else:
            self.send_error_json(404, f"no route for {path}")

    def do_POST(self):
        sup = self.supervisor
        cfg = sup.config
        path = unquote(self.path.split("?")[0])
        if path == "/v1/chat/completions":
            return self._chat_completions()
        if path in ("/mcp", "/v1/mcp"):
            return self._mcp()
        if path in ("/v1/systemone", "/v1/classifier", "/v1/decisions"):
            target = cfg.systemone_root or cfg.backend_root
            if not target:
                self.send_error_json(503, "systemone is not configured")
                return
            body = self.read_body_bytes()
            # the decision server may only expose one of its aliases (the
            # llama.cpp fork serves /v1/systemone, Featherless both); probe
            # each path and relay the first response that is not a 404/405.
            aliases = [cfg.systemone_alias] if cfg.systemone_alias else []
            aliases += [p for p in ("/v1/systemone", "/v1/classifier",
                                    "/v1/decisions")
                        if p not in aliases and p != path]
            aliases.insert(aliases.index(path) if path in aliases else 0, path)
            for alias in aliases:
                if self._forward("POST", f"{target}{alias}", body, probe=True):
                    cfg.systemone_alias = alias      # sticky for later calls
                    return
            self.send_error_json(503, "no decision endpoint answered")
        if path.startswith("/v1/clm/"):
            return self._clm_post(path)
        if path in NATIVE_POST_PATHS and cfg.backend_root:
            body = self.read_body_bytes()
            self._forward("POST", f"{cfg.backend_root}{path}", body)
            return
        self.send_error_json(404, f"no route for {path}")

    # -- clm-mem introspection / studio API -----------------------------------
    def _read_json_body(self):
        raw = self.read_body_bytes()
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def _clm_get(self, path):
        sup = self.supervisor
        parts = [p for p in path.split("/") if p]
        qd = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        if len(parts) < 3 or parts[2] not in ("sessions", "workspaces"):
            return self.send_error_json(404, f"no route for {path}")
        if parts[2] == "workspaces":
            if len(parts) == 3:
                return self.send_json({"workspaces": sup.list_workspaces()})
            return self.send_error_json(404, f"no route for {path}")
        if len(parts) == 3:                     # /v1/clm/sessions
            with sup.lock:
                sids = list(sup.sessions)
            return self.send_json({"sessions": sids})
        sid = parts[3]
        action = parts[4] if len(parts) > 4 else "snapshot"
        h = sup.get_session(sid)
        if action == "status":
            if h is None:
                return self.send_error_json(404, f"unknown session {sid!r}")
            return self.send_json({"session_id": sid, "status": h.status(),
                                   "turns": h.turn, "tokens": h.tokens(),
                                   "counts": h.counts()})
        if action == "snapshot":
            if h is None:
                return self.send_error_json(404, f"unknown session {sid!r}")
            full = qd.get("full", ["0"])[0].lower() in ("1", "true", "yes")
            snap = h.snapshot(include_full=full)
            snap["session_id"] = sid
            return self.send_json(snap)
        if action == "archive":
            if h is None:
                return self.send_error_json(404, f"unknown session {sid!r}")
            query = qd.get("q", [""])[0]
            try:
                limit = int(qd.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            hits = h.recall(query, limit=limit) if query else []
            return self.send_json({"query": query, "hits": hits,
                                   "archive_size": h.archive_size()})
        if action == "block" and len(parts) == 6 and parts[5].isdigit():
            if h is None:
                return self.send_error_json(404, f"unknown session {sid!r}")
            b = h.block_by_id(int(parts[5]))
            if b is None:
                return self.send_error_json(
                    404, f"block {parts[5]} not in live window")
            import dataclasses
            out = dataclasses.asdict(b)
            out["tokens"] = h.counter.count_block(b)
            return self.send_json({"session_id": sid, "block": out})
        return self.send_error_json(404, f"no route for {path}")

    def _clm_post(self, path):
        sup = self.supervisor
        parts = [p for p in path.split("/") if p]
        body = self._read_json_body()
        if len(parts) == 3 and parts[2] == "workspaces":
            name = str(body.get("name", "")).strip()
            try:
                ws = sup.create_workspace(name)
            except ValueError as exc:
                return self.send_error_json(400, str(exc))
            return self.send_json(ws)
        if parts[2] != "sessions" or len(parts) < 5:
            return self.send_error_json(404, f"no route for {path}")
        sid = parts[3]
        action = parts[4]
        if action == "reset":
            existed = sup.drop_session(sid)
            return self.send_json({"reset": sid, "existed": existed})
        h = sup.get_session(sid)
        if h is None:
            return self.send_error_json(404, f"unknown session {sid!r}")
        if action == "reinject":
            try:
                bid = int(body.get("block_id"))
            except (TypeError, ValueError):
                return self.send_error_json(400, "block_id must be an int")
            ok = h.reinject(bid)
            if not ok:
                return self.send_error_json(
                    404, f"block {bid} not in archive for {sid!r}")
            return self.send_json({"reinstated": True, "session_id": sid,
                                   "status": h.status()})
        if action == "settings":
            changed: Dict[str, object] = {}
            pct = body.get("shortterm_keep_pct")
            if pct is not None:
                try:
                    pct = float(pct)
                    assert 0.0 <= pct < 1.0
                except (TypeError, ValueError, AssertionError):
                    return self.send_error_json(
                        400, "shortterm_keep_pct must be in [0, 1)")
                h.cfg.shortterm_keep_pct = pct
                changed["shortterm_keep_pct"] = pct
            budget = body.get("max_context_tokens")
            if budget is not None:
                try:
                    budget = int(budget)
                    assert budget >= 64
                except (TypeError, ValueError, AssertionError):
                    return self.send_error_json(
                        400, "max_context_tokens must be an int >= 64")
                h.cfg.max_context_tokens = budget
                changed["max_context_tokens"] = budget
            thr = body.get("systemone_evict_threshold")
            if thr is not None:
                try:
                    thr = float(thr)
                    assert 0.0 <= thr <= 1.0
                except (TypeError, ValueError, AssertionError):
                    return self.send_error_json(
                        400, "systemone_evict_threshold must be in [0, 1]")
                h.cfg.systemone_evict_threshold = thr
                changed["systemone_evict_threshold"] = thr
            return self.send_json({"session_id": sid,
                                   "changed": changed, "status": h.status()})
        return self.send_error_json(404, f"no route for {path}")

    # -- chat completions: passthrough or memory-managed ---------------------
    def _chat_completions(self):
        sup = self.supervisor
        cfg = sup.config
        raw = self.read_body_bytes()
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            self.send_error_json(400, "invalid JSON body")
            return
        sid = self._session_id(body)
        model = body.get("model") or cfg.served_model

        if not sid:                       # plain llama.cpp client: passthrough
            if not cfg.backend_root:
                self.send_error_json(503, "no backend configured")
                return
            self._forward("POST", f"{cfg.backend_root}/v1/chat/completions",
                          raw, stream=bool(body.get("stream")))
            return

        msgs = body.get("messages") or []
        user_text = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):   # multimodal-ish parts
                    c = " ".join(p.get("text", "") for p in c
                                 if isinstance(p, dict))
                user_text = str(c)
                break
        if not user_text:
            self.send_error_json(400, "session requests need a user message")
            return
        try:
            harness = self._harness_for(sid, model)
        except Exception as exc:
            self.send_error_json(503, f"cannot create session: {exc}")
            return
        try:
            reply = harness.step(user_text)
        except Exception as exc:
            self.send_error_json(502, f"backend error during step: {exc}")
            return
        if body.get("stream"):
            self._sse_reply(sid, model, reply)
            return
        payload = {"id": "clm-mem-session", "object": "chat.completion",
                   "created": int(time.time()), "model": model,
                   "choices": [{"index": 0,
                                "message": {"role": "assistant",
                                            "content": reply},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": harness.tokens(),
                             "completion_tokens": max(1, len(reply) // 4),
                             "total_tokens": harness.tokens()
                             + max(1, len(reply) // 4)},
                   "clm_mem": {"session_id": sid,
                               "status": harness.status(),
                               "counts": harness.counts(),
                               "tokens": harness.tokens(),
                               "budget": harness.cfg.max_context_tokens,
                               "archive": harness.archive_size(),
                               "events": harness.events[-8:]}}
        self.send_json(payload)


class Supervisor:
    """The proxy process + whatever background backends it launched."""

    def __init__(self, server: ThreadingHTTPServer,
                 manager: ProcessManager, config: ProxyConfig):
        self.server = server
        self.manager = manager
        self.config = config
        self.sessions: Dict[str, MemoryHarness] = {}
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=server.serve_forever,
                                       daemon=True, name="clm-mem-proxy")

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        host = "127.0.0.1" if host in ("0.0.0.0", "*") else host
        return f"http://{host}:{port}"

    @property
    def public_base(self) -> str:          # OpenAI-style base for clients
        return f"{self.base_url}/v1"

    @property
    def ui_url(self) -> Optional[str]:     # web studio entry point (if enabled)
        return self.base_url if self.config.ui_enabled else None

    # -- sessions ---------------------------------------------------------
    def harness_for(self, sid: str, model: str = ""):
        """Get-or-create the :class:`MemoryHarness` behind a session id.

        ``ws:<name>`` sessions get their own project sandbox under
        ``<workdir>/workspaces/<name>`` so multiple projects never share
        context/archive files. Thread-safe (double-checked creation).
        """
        with self.lock:
            h = self.sessions.get(sid)
            if h is not None:
                return h
        cfg = self.config
        kwargs = dict(cfg.harness_kwargs or {})
        if sid.startswith(WS_PREFIX):
            kwargs["workdir"] = self.ws_dir(sid[len(WS_PREFIX):])
            os.makedirs(kwargs["workdir"], exist_ok=True)
        llm = OpenAIChat(base_url=f"{cfg.backend_root}/v1",
                         model=model or cfg.served_model, api_key="EMPTY",
                         temperature=cfg.chat_temperature,
                         max_tokens=cfg.chat_max_tokens)
        systemone = None
        if cfg.systemone_root:
            from .llm import SystemOneClient
            systemone = SystemOneClient(cfg.systemone_root,
                                       model=cfg.systemone_model,
                                       api_key=cfg.systemone_api_key)
        h = MemoryHarness(llm, HarnessConfig(**kwargs), systemone=systemone)
        with self.lock:
            existing = self.sessions.get(sid)      # another thread raced us
            if existing is not None:
                return existing
            self.sessions[sid] = h
            return h

    def get_session(self, sid: str):
        with self.lock:
            return self.sessions.get(sid)

    def drop_session(self, sid: str) -> bool:
        with self.lock:
            return self.sessions.pop(sid, None) is not None

    # -- workspaces (project sandboxes) ----------------------------------------
    @property
    def workspace_root(self) -> str:
        base = self.config.harness_kwargs.get("workdir") or "."
        return os.path.join(base, "workspaces")

    def ws_dir(self, name: str) -> str:
        if not WS_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid workspace name {name!r}")
        d = os.path.join(self.workspace_root, name)
        # containment check against traversal even with the regex guard
        root = os.path.realpath(self.workspace_root)
        rp = os.path.realpath(d)
        if os.path.commonpath([root, rp]) != root:
            raise ValueError(f"invalid workspace path {name!r}")
        return d

    def list_workspaces(self) -> List[dict]:
        root = self.workspace_root
        names = sorted(os.listdir(root)) if os.path.isdir(root) else []
        out = []
        for n in names:
            if not WS_NAME_RE.fullmatch(n):
                continue
            sid = f"{WS_PREFIX}{n}"
            h = self.get_session(sid)
            item: dict = {"name": n, "active": h is not None}
            if h is not None:
                item.update({"turn": h.turn, "tokens": h.tokens(),
                             "budget": h.cfg.max_context_tokens,
                             "counts": h.counts(),
                             "archive": h.archive_size()})
            out.append(item)
        return out

    def create_workspace(self, name: str) -> dict:
        os.makedirs(self.ws_dir(name), exist_ok=True)
        return {"name": name}

    def delete_workspace(self, name: str) -> bool:
        d = self.ws_dir(name)
        self.drop_session(f"{WS_PREFIX}{name}")
        if os.path.isdir(d):
            shutil.rmtree(d)          # sandbox teardown drops the files too
            return True
        return False

    def start(self, ready_timeout: float = 30.0) -> "Supervisor":
        self.thread.start()
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base_url}/health",
                                            timeout=1.0) as resp:
                    resp.read()
                    if resp.status < 500:
                        return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError(
            f"proxy did not become ready on {self.base_url} "
            f"within {ready_timeout}s")

    def wait(self) -> None:               # pragma: no cover - CLI convenience
        self.thread.join()

    def shutdown(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        try:
            self.server.server_close()
        except Exception:
            pass
        with self.lock:
            self.sessions.clear()
        self.manager.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def start_supervisor(*, public_host: str = "127.0.0.1", public_port: int = 0,
                     served_model: str = "clm-mem-model",
                     backend_mode: str = "mock", backend_cmd=None,
                     backend_flags=("--host", "{host}", "--port", "{port}"),
                     backend_url: Optional[str] = None,
                     systemone_mode: str = "mock", systemone_cmd=None,
                     systemone_flags=("--host", "{host}", "--port", "{port}"),
                     systemone_url: Optional[str] = None,
                     systemone_model: str = "", systemone_api_key: str = "",
                     harness_kwargs: Optional[dict] = None,
                     chat_temperature: float = 0.6, chat_max_tokens: int = 1024,
                     log_dir: Optional[str] = None, timeout: float = 90.0,
                     ui: bool = True
                     ) -> Supervisor:
    """Bring up the whole stack and return a running :class:`Supervisor`.

    Modes for backend/systemone are: ``mock`` (bundled fake), ``spawn``
    (exec ``*_cmd`` with host/port flags rendered in), ``external`` (already
    running; give its root URL) or ``none``. ``systemone_mode='passthrough'``
    means the backend itself answers ``/v1/systemone`` (SystemOne fork of
    llama.cpp server).
    """
    manager = ProcessManager(log_dir=log_dir)
    host = "127.0.0.1"                      # internal backends stay loopback

    def spawn(kind: str, cmd, flags):
        port = free_port(host)
        argv = list(cmd) + render_flags(flags, host, port)
        h = manager.start(kind, argv, host=host, port=port,
                          ready_paths=("/health", "/v1/models"),
                          timeout=timeout)
        return h.root_url

    backend_root: Optional[str] = None
    if backend_mode == "mock":
        backend_root = spawn("backend-mock",
                             manager.mock_argv("llm"), ("--host", "{host}", "--port", "{port}"))
    elif backend_mode == "spawn":
        if not backend_cmd:
            raise ValueError("backend_mode='spawn' needs backend_cmd")
        backend_root = spawn("backend", backend_cmd, backend_flags)
    elif backend_mode == "external":
        if not backend_url:
            raise ValueError("backend_mode='external' needs backend_url")
        backend_root = normalize_root(backend_url)
    elif backend_mode == "none":
        backend_root = None
    else:
        raise ValueError(f"unknown backend_mode {backend_mode!r}")

    systemone_root: Optional[str] = None
    if systemone_mode == "mock":
        systemone_root = spawn("systemone-mock",
                               manager.mock_argv("systemone"), ("--host", "{host}", "--port", "{port}"))
    elif systemone_mode == "spawn":
        if not systemone_cmd:
            raise ValueError("systemone_mode='spawn' needs systemone_cmd")
        systemone_root = spawn("systemone", systemone_cmd, systemone_flags)
    elif systemone_mode == "external":
        if not systemone_url:
            raise ValueError("systemone_mode='external' needs systemone_url")
        systemone_root = normalize_root(systemone_url)
    elif systemone_mode == "passthrough":
        systemone_root = backend_root          # fork serves it itself
    elif systemone_mode in ("none", None):
        systemone_root = None
    else:
        raise ValueError(f"unknown systemone_mode {systemone_mode!r}")

    config = ProxyConfig(
        backend_root=backend_root, systemone_root=systemone_root,
        systemone_model=systemone_model, systemone_api_key=systemone_api_key,
        served_model=served_model,
        harness_kwargs=dict(harness_kwargs or {}),
        chat_temperature=chat_temperature, chat_max_tokens=chat_max_tokens,
        ui_enabled=bool(ui))

    try:
        server = ThreadingHTTPServer((public_host, public_port), ProxyHandler)
    except Exception:
        manager.shutdown()
        raise
    server.daemon_threads = True
    sup = Supervisor(server, manager, config)
    server.supervisor = sup                 # type: ignore[attr-defined]
    try:
        sup.start()
    except Exception:
        manager.shutdown()
        server.server_close()
        raise
    return sup
