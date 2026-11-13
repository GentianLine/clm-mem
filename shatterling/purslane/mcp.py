"""MCP endpoint logic implemented as a thin HTTP client of `/v1/clm/*`.

Implements the streamable-HTTP transport subset that matters for tool clients:
JSON-RPC over POST /mcp answering ``initialize``, ``tools/list`` and
``tools/call``. The exposed tools mirror exactly what the web studio and CLI
can do -- but per split-plan decision D6 every capability executes by hitting
the very same REST verbs any other client uses, through a :class:`HttpApi`,
instead of being handed the live ``Supervisor`` object.
Consequences:

* parity tests covering ``/v1/clm/*`` cover this module for free;
* it works against *remote* daemons (construct :class:`HttpApi` with any base
  URL plus an Authorization header if one is required);
* when served by purslane itself at /mcp, calls loop back over HTTP and are
  answered on their own threads -- both paths run identical code.

This module imports nothing else from shatterling: the layering stays honest.
The wire identity ``name`` remains "clm-mem" (stable across the rename).
"""

from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import quote

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "clm-mem"  # wire identity: stable across the shatterling rename
SERVER_TITLE = "clm-mem memory harness"
_TIMEOUT = 60.0

_WS_DEFAULT = "default"


class ApiError(Exception):
    """A daemon call answered non-2xx; carries the server's message through."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def _err_msg(payload: Dict[str, Any]) -> str:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("message", "")) or json.dumps(err)
    return str(payload)


def _parse(raw: bytes | str) -> Dict[str, Any]:
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    try:
        out = json.loads(text) if text.strip() else {}
    except Exception:
        return {"_raw": text}
    return out if isinstance(out, dict) else {"_json": out}


class HttpApi:
    """Minimal JSON-over-HTTP client for one purslane daemon root URL.

    :param base_url: e.g. ``http://127.0.0.1:8080`` (no path suffix).
    :param headers: optional extra request headers -- typically an
        ``Authorization`` value copied from the inbound MCP call so a guarded
        daemon stays honest on both hops.

    Legacy ``X-Cllm-Mem-*`` spellings are never forwarded; they belong to an
    expired contract and would earn their 410 over here too.
    """

    def __init__(
        self, base_url: str = "", headers: Optional[Dict[str, str]] = None
    ):
        self.base = (base_url or "").rstrip("/")
        self.headers = {
            k: v
            for k, v in dict(headers or {}).items()
            if not k.lower().startswith(("x-cllm-mem", "content-type"))
        }
        self._version: Optional[str] = None

    def version(self) -> str:
        """Daemon-reported build version (``GET /version``, cached per api)."""
        if self._version is None:
            try:
                _, payload = self.request("GET", "/version")
                v = payload.get("version") or payload.get("_raw")
                self._version = str(v) if v else "unknown"
            except Exception:
                self._version = "unknown"
        return self._version

    def request(
        self,
        method: str = "GET",
        path: str = "/",
        body: Optional[Dict[str, Any]] = None,
    ) -> tuple[int, Dict[str, Any]]:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if isinstance(body, dict) else (
            b"{}" if method.upper() != "GET" else None
        )
        req = urllib.request.Request(
            url,
            data=data,
            headers=dict(self.headers),
            method=method.upper(),
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # daemon
                raw = (r.read() or b"")
                status = int(r.status)
        except urllib.error.HTTPError as exc:  # non-2xx still carries a JSON body
            try:
                raw = exc.read() or b""
            except Exception:
                raw = b"{}"
            status = int(exc.code)
        return (status or 0), _parse(raw)

    def ok(
        self,
        method: str = "GET",
        path: str = "/v1/clm/workspaces",
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """:meth:`request` that raises :class:`ApiError` on non-2xx."""
        status, payload = self.request(method, path, body)
        if not 200 <= status < 300:
            raise ApiError(status, _err_msg(payload))
        return payload

    def ensure_open(self, sid: str) -> None:
        """Mirror the old ``harness_for`` lazy-create through the open verb."""
        self.ok("POST", "/v1/clm/sessions", {"session": sid})



def _sid(args: Dict[str, Any]) -> str:
    ws = str(args.get("workspace") or _WS_DEFAULT).strip()
    return f"ws:{ws}"


def _obj(props: Dict[str, Any], required: List[str]) -> Dict:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
_WS = {
    "type": "string",
    "description": "workspace/project sandbox name (default: 'default')",
}

TOOLS: List[Dict] = [
    {
        "name": "clm_open_session",
        "description": (
            "Open (or reopen) a memory branch for a workspace. A branch is "
            "a session == workspace == its own harness + sandbox; this "
            "creates it explicitly before the first clm_chat turn, and "
            "accepts creation-time options like max_context_tokens."
        ),
        "inputSchema": _obj(
            {
                "workspace": _WS,
                "model": _STR,
                "options": {
                    "type": "object",
                    "description": (
                        "open-time harness knobs (creation-time only): "
                        "max_context_tokens, shortterm_keep_pct, "
                        "classify_mode, decay/supersede toggles, ..."
                    ),
                },
            },
            [],
        ),
    },
    {
        "name": "clm_chat",
        "description": (
            "Send a message to the clm-mem memory-managed chat for a "
            "workspace. The harness classifies context as longterm / "
            "shortterm / transient and enforces the token budget. Opens "
            "the branch implicitly if it was never opened."
        ),
        "inputSchema": _obj({"message": _STR, "workspace": _WS}, ["message"]),
    },
    {
        "name": "clm_status",
        "description": (
            "Inspect a workspace session: turn count, live tokens vs "
            "budget, per-tier block counts, archive size."
        ),
        "inputSchema": _obj({"workspace": _WS}, []),
    },
    {
        "name": "clm_blocks",
        "description": (
            "List the live context blocks of a workspace session with "
            "their memory tags and token counts (previews unless "
            "full=true)."
        ),
        "inputSchema": _obj(
            {
                "workspace": _WS,
                "tag": {
                    "type": "string",
                    "enum": ["longterm", "shortterm", "transient"],
                },
                "full": {"type": "boolean"},
            },
            [],
        ),
    },
    {
        "name": "clm_recall",
        "description": (
            "Substring-search the eviction archive of a workspace -- "
            "memory that was dropped/offloaded is still retrievable."
        ),
        "inputSchema": _obj(
            {
                "query": _STR,
                "workspace": _WS,
                "limit": {"type": "integer", "default": 50},
            },
            ["query"],
        ),
    },
    {
        "name": "clm_reinject",
        "description": (
            "Pull an archived block back into the live context of a "
            "workspace as shortterm."
        ),
        "inputSchema": _obj(
            {"block_id": {"type": "integer"}, "workspace": _WS}, ["block_id"]
        ),
    },
    {
        "name": "clm_set_keep_pct",
        "description": (
            "Tune the memory knob (fraction of shortterm kept per "
            "FIFO eviction round) live, in [0, 1)."
        ),
        "inputSchema": _obj({"pct": {"type": "number"}, "workspace": _WS}, ["pct"]),
    },
    {
        "name": "clm_set_forget",
        "description": (
            "Enable/disable the forgetting strategies live: decay "
            "(forgetting-curve demotion of stale longterm plus "
            "value-ranked offload) and supersede (updates overwrite "
            "older longterm, duplicates dropped at write time). Also sets "
            "reheat scope: 'global' (a touch in any chat lifts a fact's "
            "importance everywhere), 'chat' (only the fact's own chat "
            "lifts; other chats merely keep it from going stale) or "
            "'fresh' (touches anywhere keep facts fresh but never lift "
            "importance -- global hot & fresh, no reheat). "
            "Omitted flags leave the current state untouched."
        ),
        "inputSchema": _obj(
            {
                "decay": {"type": "boolean"},
                "supersede": {"type": "boolean"},
                "reheat_scope": {
                    "type": "string",
                    "enum": ["global", "chat", "fresh"],
                },
                "workspace": _WS,
            },
            [],
        ),
    },
    {
        "name": "clm_workspaces",
        "description": (
            "List project workspaces (sandboxes) with their active " "session stats."
        ),
        "inputSchema": _obj({}, []),
    },
    # -- agent tools: identical to the web studio toggles and CLI slash cmds --
    {
        "name": "clm_tools",
        "description": (
            "Inspect a workspace session's agent tools: which are "
            "enabled (web / exec / fs categories), the approval "
            "policy, sandbox isolation level and recent activity."
        ),
        "inputSchema": _obj(
            {"workspace": _WS, "activity": {"type": "integer", "default": 10}}, []
        ),
    },
    {
        "name": "clm_tool_on",
        "description": (
            "Enable/disable agent tools live: target is a category "
            "(web, exec, fs) or a tool name (all toggles every one). "
            "Also accepts approval_mode=auto|ask|deny."
        ),
        "inputSchema": _obj(
            {
                "target": _STR,
                "on": {"type": "boolean"},
                "approval_mode": {"type": "string", "enum": ["auto", "ask", "deny"]},
                "workspace": _WS,
            },
            ["target"],
        ),
    },
    {
        "name": "clm_tool_call",
        "description": (
            "Run one clm-mem tool directly (web_search, web_fetch, "
            "run_command, run_python, write_file, read_file, "
            "list_files). Subject to the session's approval policy: mutating "
            "tools require approval when the mode is 'ask'/'deny'; only "
            "read-only tools always run."
        ),
        "inputSchema": _obj(
            {
                "tool": _STR,
                "args": {"type": "object", "description": "tool arguments"},
                "workspace": _WS,
            },
            ["tool"],
        ),
    },
    {
        "name": "clm_sandbox_exec",
        "description": (
            "Run a shell command inside a workspace's sandbox "
            "(bounded process; cwd is the sandbox root). This is a model-"
            "initiated call, so it goes through the session's approval "
            "policy like any other exec tool (not an approval bypass)."
        ),
        "inputSchema": _obj(
            {
                "command": _STR,
                "cwd": _STR,
                "timeout": {"type": "number"},
                "workspace": _WS,
            },
            ["command"],
        ),
    },
    {
        "name": "clm_web_search",
        "description": (
            "Search the web with the session's configured provider "
            "(stub/ddg/brave/tavily/exa) without a model turn."
        ),
        "inputSchema": _obj(
            {
                "query": _STR,
                "max_results": {"type": "integer", "default": 5},
                "workspace": _WS,
            },
            ["query"],
        ),
    },
]


def _text(msg: str, is_error: bool = False) -> Dict:
    return {"content": [{"type": "text", "text": msg}], "isError": is_error}


def _err(rid, code: int, msg: str) -> Dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def _exec_text(payload: Dict[str, Any]) -> str:
    summary = str(payload.get("summary", "") or "")
    detail = str(payload.get("detail", "") or "")[:4000]
    return f"{summary}\n{detail}".strip()


def _call(
    api: HttpApi,
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    status, payload = api.request(method, path, body)
    if not 200 <= status < 300:
        raise ApiError(status, _err_msg(payload))
    return payload


def _strip_extra(payload: Dict[str, Any]) -> Dict[str, Any]:
    drop = ("session_id", "backend_ctx")
    out = dict(payload)
    for k in drop:
        out.pop(k, None)
    return out


def _agent_result(api: HttpApi, sid: str, name: str, args: Dict[str, Any]) -> Dict:
    ep = f"/v1/clm/sessions/{quote(sid)}"
    if name == "clm_tools":
        tail = int(args.get("activity", 10) or 10)
        state = api.ok("GET", ep + "/tools")
        act = api.ok("GET", f"{ep}/activity?limit={tail}")
        merged: Dict[str, Any] = {}
        for k, v in state.items():
            if k != "session_id":
                merged[k] = v
        merged["activity"] = act.get("activity", [])
        return _text(json.dumps(merged))
    if name == "clm_tool_on":
        if args.get("approval_mode"):
            api.request(
                "POST",
                ep + "/tools",
                {"approval_mode": str(args["approval_mode"])},
            )  # old code ignored this result too
        st, p = api.request(
            "POST",
            ep + "/tools",
            {
                "target": str(args.get("target", "all")),
                "on": args.get("on"),
            },
        )
        if not 200 <= st < 300:
            raise ApiError(st, _err_msg(p))
        out = {k: v for k, v in p.items() if k != "session_id" and k != "changed"}
        return _text(json.dumps(out))
    if name == "clm_tool_call":
        body: Dict[str, Any] = {
            "tool": str(args.get("tool", "")).strip(),
            "args": dict(args.get("args") or {}),
            "origin": "mcp",
        }
        p = _call(api, "POST", ep + "/toolcall", body)
        return _text(_exec_text(p), is_error=not bool(p.get("ok")))
    if name == "clm_sandbox_exec":
        body = {
            "command": str(args.get("command", "")).strip(),
            "cwd": str(args.get("cwd", "")),
            "timeout": args.get("timeout"),
        }
        p = _call(api, "POST", ep + "/sandbox/exec", body)
        return _text(_exec_text(p), is_error=not bool(p.get("ok")))
    if name == "clm_web_search":
        body = {
            "query": str(args.get("query", "")).strip(),
            "max_results": int(args.get("max_results", 5)),
        }
        p = _call(api, "POST", ep + "/search", body)
        return _text(_exec_text(p), is_error=not bool(p.get("ok")))
    return _text(f"unknown agent tool {name!r}", is_error=True)


def _tool_result(api: HttpApi, name: str, args: Dict[str, Any]) -> Dict:
    try:
        if name == "clm_workspaces":
            payload = api.ok("GET", "/v1/clm/workspaces")
            return _text(json.dumps(payload))

        sid = _sid(args)
        ep = f"/v1/clm/sessions/{quote(sid)}"
        if name in (
            "clm_tools",
            "clm_tool_on",
            "clm_tool_call",
            "clm_sandbox_exec",
            "clm_web_search",
        ):
            return _agent_result(api, sid, name, args)

        if name == "clm_open_session":
            opts = dict(args.get("options") or {})
            body: Dict[str, Any] = {
                "session": sid,
                "model": str(args.get("model", "")),
                "options": opts,
            }
            st, p = api.request("POST", "/v1/clm/sessions", body)
            if not 200 <= st < 300:
                return _text(_err_msg(p), is_error=True)
            filtered: Dict[str, Any] = {}
            for k in ("session_id", "created", "budget", "status"):
                if k in p:
                    filtered[k] = p[k]
            return _text(json.dumps(filtered))

        # The old code used ``harness_for`` lazy-create; mirror it via the open verb.
        api.ensure_open(sid)

        if name == "clm_chat":
            st, p = api.request(
                "POST",
                ep + "/turns",
                {"content": str(args.get("message", ""))},
            )
            if not 200 <= st < 300:
                return _text(_err_msg(p), is_error=True)
            choices = p.get("choices") or [{}]
            reply = (choices[0].get("message") or {}).get("content", "")
            clm_mem = p.get("clm_mem") or {}
            status = str(clm_mem.get("status", ""))
            return _text(f"{reply}\n\n[context: {status}]")
        if name == "clm_status":
            snap = api.ok("GET", ep + "/snapshot")
            return _text(json.dumps(_strip_extra(snap)))
        if name == "clm_blocks":
            fullq = "1" if bool(args.get("full")) else "0"
            st, snap = api.request(
                "GET", ep + f"/snapshot?full={fullq}"
            )  # ensure_open already guarantees presence
            if not 200 <= st < 300:
                return _text(_err_msg(snap), is_error=True)
            snap = _strip_extra(snap)
            tag = args.get("tag")
            if tag:
                snap["blocks"] = [b for b in snap.get("blocks", []) if b.get("tag") == tag]
            return _text(json.dumps(snap))
        if name == "clm_recall":
            q = quote(str(args.get("query", "")))
            limit = int(args.get("limit", 50))
            p = api.ok("GET", f"{ep}/archive?q={q}&limit={limit}")
            return _text(json.dumps({"hits": p.get("hits")}))
        if name == "clm_reinject":
            st, p = api.request(
                "POST",
                ep + "/reinject",
                {"block_id": int(args.get("block_id", -1))},
            )
            if 200 <= st < 300 and p.get("reinstated"):
                return _text("re-injected as shortterm")
            return _text(_err_msg(p), is_error=True)
        if name == "clm_set_forget":
            out: List[str] = []
            body2: Dict[str, Any] = {}
            if args.get("decay") is not None:
                val = bool(args["decay"])
                body2["decay_enabled"] = val
                out.append(f"decay_enabled={val}")
            if args.get("supersede") is not None:
                val = bool(args["supersede"])
                body2["supersede_enabled"] = val
                out.append(f"supersede_enabled={val}")
            scope_raw = args.get("reheat_scope")
            if scope_raw is not None:
                scope = str(scope_raw).lower()
                if scope not in ("global", "chat", "fresh"):
                    return _text(
                        "reheat_scope must be 'global', 'chat' or 'fresh'",
                        is_error=True,
                    )
                body2["reheat_scope"] = scope
                out.append(f"reheat_scope={scope}")
            if not body2:
                # omitted flags read back current state (old cfg introspection)
                snap = api.ok("GET", ep + "/snapshot")
                return _text(
                    f"decay_enabled={snap.get('decay_enabled')} "
                    f"supersede_enabled={snap.get('supersede_enabled')}"
                )
            st, p = api.request("POST", ep + "/settings", body2)
            if not 200 <= st < 300:
                return _text(_err_msg(p), is_error=True)
            return _text("; ".join(out))
        if name == "clm_set_keep_pct":
            try:
                pct = float(args.get("pct", -1.0))
            except (TypeError, ValueError):
                return _text("pct must be in [0, 1)", is_error=True)   
            if not 0.0 <= pct < 1.0:
                return _text("pct must be in [0, 1)", is_error=True)
            st, p = api.request(
                "POST", ep + "/settings", {"shortterm_keep_pct": pct}
            )
            if not 200 <= st < 300:
                return _text(_err_msg(p), is_error=True)
            return _text(f"shortterm_keep_pct = {pct}")
        return _text(f"unknown tool {name!r}", is_error=True)
    except Exception as exc:  # MCP tools report failures via isError
        return _text(f"{name} failed: {exc}", is_error=True)


def handle(msg: Dict[str, Any], api: HttpApi) -> Optional[Dict]:
    """Handle one JSON-RPC message against daemon `api`; None means accepted."""
    rid = msg.get("id")
    method = str(msg.get("method", ""))

    if method == "initialize":
        server_info: Dict[str, Any] = {
            "name": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": api.version(),
        }
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": server_info,
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None  # notification -> 202 no body
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = str(params.get("name", ""))
        args: Dict[str, Any] = dict(params.get("arguments") or {})
        known = {t["name"] for t in TOOLS}
        if name not in known:
            return _err(rid, -32602, f"unknown tool {name!r}")
        result = _tool_result(api, name, args)
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    if method.startswith("notifications/"):
        return None
    return _err(rid, -32601, f"unsupported method {method!r}")