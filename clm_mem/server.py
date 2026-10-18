"""`clm-mem serve`: a llama.cpp-compatible front door.

To any OpenAI/llama.cpp client this is just *the* server: one host:port with
``/v1/chat/completions``, ``/v1/models``, ``/completion``, ``/props``,
``/metrics``, ``/version``, ``/health``. Behind it the supervisor spins up
2-3 background processes (proxy + chat backend + optional System One
decision server) and wires clm-mem's memory harness into every request that
carries an ``X-Cllm-Mem-Session`` header, so clients like Unsloth Studio get
long/short-term memory without knowing they have it.

Nothing is downloaded or compiled here -- real backends are only ever exec'd
from binaries you point at; the bundled mocks cover testing.
"""
from __future__ import annotations
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import unquote
from .config import HarnessConfig
from .harness import MemoryHarness
from .llm import OpenAIChat
from .processes import ProcessManager, free_port, render_flags

VERSION = "0.1.0"
SESSION_HEADER = "X-Cllm-Mem-Session"
NATIVE_GET_PATHS = ("/props", "/metrics", "/specs")
NATIVE_POST_PATHS = ("/completion", "/embedding")


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
    served_model: str = "clm-mem-model"
    harness_kwargs: dict = field(default_factory=dict)
    chat_temperature: float = 0.6
    chat_max_tokens: int = 1024
    request_timeout: float = 300.0


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
                 stream: bool = False):
        req = urllib.request.Request(
            url, data=body if method.upper() != "GET" else None,
            headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req,
                                          timeout=self.supervisor.config.request_timeout)
        except urllib.error.HTTPError as e:
            self._respond_payload(e.code, e.read(),
                                  e.headers.get("Content-Type", "application/json"))
            return
        except Exception as exc:       # backend down / connection refused
            self.send_error_json(502, f"backend unreachable at {url}: {exc}")
            return
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
        sup = self.supervisor
        with sup.lock:
            h = sup.sessions.get(sid)
            if h is not None:
                return h
        cfg = sup.config
        llm = OpenAIChat(base_url=f"{cfg.backend_root}/v1", model=model,
                         api_key="EMPTY",
                         temperature=cfg.chat_temperature,
                         max_tokens=cfg.chat_max_tokens)
        systemone = None
        if cfg.systemone_root:
            from .llm import SystemOneClient
            systemone = SystemOneClient(cfg.systemone_root)
        h = MemoryHarness(llm, HarnessConfig(**(cfg.harness_kwargs or {})),
                          systemone=systemone)
        with sup.lock:
            # re-check: another thread may have raced us to create it
            existing = sup.sessions.get(sid)
            if existing is not None:
                return existing
            sup.sessions[sid] = h
            return h

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

    def do_GET(self):
        sup = self.supervisor
        cfg = sup.config
        path = self.path.split("?")[0]
        if path in ("/health", "/v1/health"):
            with sup.lock:
                sids = list(sup.sessions)
            backends = {h.name: f"{h.host}:{h.port}"
                        for h in sup.manager.handles if h.alive()}
            self.send_json({"status": "ok", "version": VERSION,
                            "model": cfg.served_model,
                            "sessions": sids, "processes": backends})
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
        elif path.startswith("/v1/clm/sessions"):
            parts = [p for p in unquote(path).split("/") if p]
            with sup.lock:
                sids = list(sup.sessions)
            if len(parts) == 3:
                self.send_json({"sessions": sids})
            elif len(parts) == 5 and parts[4] == "status":
                sid = parts[3]
                h = sup.sessions.get(sid)
                if h is None:
                    self.send_error_json(404, f"unknown session {sid!r}")
                else:
                    self.send_json({"session_id": sid, "status": h.status(),
                                    "turns": h.turn,
                                    "tokens": h.tokens(),
                                    "counts": h.counts()})
            else:
                self.send_error_json(404, f"no route for {path}")
        elif path in NATIVE_GET_PATHS and cfg.backend_root:
            self._forward("GET", f"{cfg.backend_root}{path}")
        else:
            self.send_error_json(404, f"no route for {path}")

    def do_DELETE(self):
        sup = self.supervisor
        path = self.path.split("?")[0]
        if path.startswith("/v1/clm/sessions/"):
            sid = unquote(path[len("/v1/clm/sessions/"):].rstrip("/"))
            with sup.lock:
                h = sup.sessions.pop(sid, None)
            if h is None:
                self.send_error_json(404, f"unknown session {sid!r}")
            else:
                self.send_json({"deleted": sid})
        else:
            self.send_error_json(404, f"no route for {path}")

    def do_POST(self):
        sup = self.supervisor
        cfg = sup.config
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            return self._chat_completions()
        if path == "/v1/systemone":
            target = cfg.systemone_root or cfg.backend_root
            if not target:
                self.send_error_json(503, "systemone is not configured")
                return
            body = self.read_body_bytes()
            self._forward("POST", f"{target}{path}", body)
            return
        if path.startswith("/v1/clm/sessions/") and path.endswith("/reset"):
            sid = unquote(path[len("/v1/clm/sessions/"):-len("/reset")])
            with sup.lock:
                h = sup.sessions.pop(sid, None)
            self.send_json({"reset": sid, "existed": h is not None})
            return
        if path in NATIVE_POST_PATHS and cfg.backend_root:
            body = self.read_body_bytes()
            self._forward("POST", f"{cfg.backend_root}{path}", body)
            return
        self.send_error_json(404, f"no route for {path}")

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
                               "status": harness.status()}}
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
                     harness_kwargs: Optional[dict] = None,
                     chat_temperature: float = 0.6, chat_max_tokens: int = 1024,
                     log_dir: Optional[str] = None, timeout: float = 90.0
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
        served_model=served_model,
        harness_kwargs=dict(harness_kwargs or {}),
        chat_temperature=chat_temperature, chat_max_tokens=chat_max_tokens)

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
