"""Declarative route table for the purslane daemon.

Every HTTP path served by ``ProxyHandler`` is listed here as a ``(method,
pattern) -> handler_name`` entry.  The pattern uses named parameters in angle
brackets (e.g. ``/v1/clm/sessions/<sid>/turns``).

This table serves two purposes:

* **Documentation** — one authoritative list of every route, its method and
  what it does.
* **Test oracle** — the parity test walks this list against a live (mock)
  daemon to assert no documented path is missing or unimplemented.  When the
  server handler is eventually refactored from if-chains to dispatching via
  ``match()``, the table becomes the single source of truth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    """A single route entry.

    Attributes
    ----------
    method : str
        HTTP verb (``GET``, ``POST``, …).
    pattern : str
        URL path template.  Named parameters use angle brackets, e.g.
        ``/v1/clm/sessions/<sid>/turns``.
    handler : str
        Human-readable name of the method on ProxyHandler that serves this
        route (e.g. ``"_clm_get"``, ``"do_POST->_forward"``).
    description : str
        One-line purpose, shown in docs / test output.
    category : str
        Logical group: ``meta``, ``native_passthrough``, ``decision_proxy``,
        ``session_mgmt``, ``turns``, ``agent_surface``, ``static``, ``mcp``,
        ``legacy_reject``.
    """

    method: str
    pattern: str
    handler: str
    description: str = ""
    category: str = "meta"


class Match:
    """Result of matching a concrete path against the route table."""

    __slots__ = ("route", "params")

    def __init__(self, route: Route, params: Dict[str, str]):
        self.route = route
        self.params = params  # e.g. {"sid": "ws:alice"}

    def __repr__(self) -> str:
        return f"Match({self.route.method} {self.route.pattern}, {self.params})"


# ---------------------------------------------------------------------------
# Route table (mirrors server.py as of v0.4.0)
# ---------------------------------------------------------------------------

ROUTES: List[Route] = [
    # -- Meta / infrastructure ---------------------------------------------
    Route("GET", "/health",
          "do_GET->inline", 'Health check + daemon status', "meta"),
    Route("GET", "/v1/health",
          "do_GET->inline", 'Alias for /health', "meta"),
    Route("GET", "/version",
          "do_GET->inline", 'Version info (clm-mem + llama.cpp shim)', "meta"),
    Route("GET", "/v1/models",
          "do_GET->inline", 'Model list; merges served_model with backend', "meta"),

    # -- Native passthrough to upstream llama-server ------------------------
    Route("GET", "/props",
          "do_POST/GET->_forward", 'llama.cpp /props (window size, etc.)', "native_passthrough"),
    Route("GET", "/metrics",
          "do_POST/GET->_forward", 'Prometheus metrics passthrough', "native_passthrough"),
    Route("GET", "/specs",
          "do_POST/GET->_forward", 'llama.cpp /specs (grammar specs)', "native_passthrough"),
    Route("POST", "/completion",
          "do_POST->_forward", 'Raw completion passthrough', "native_passthrough"),
    Route("POST", "/embedding",
          "do_POST->_forward", 'Embedding passthrough', "native_passthrough"),

    # -- OpenAI-compatible (pure proxy, no memory) --------------------------
    Route("POST", "/v1/chat/completions",
          "do_POST->_forward", 'OpenAI chat completions; pure passthrough, never touches harness', "decision_proxy"),

    # -- Decision / SystemOne aliases ---------------------------------------
    Route("POST", "/v1/decisions",
          "do_POST->systemone_proxy", 'Decision API (alias 1)', "decision_proxy"),
    Route("POST", "/v1/systemone",
          "do_POST->systemone_proxy", 'SystemOne endpoint (native path, alias 2)', "decision_proxy"),
    Route("POST", "/v1/classifier",
          "do_POST->systemone_proxy", 'Classifier API (alias 3)', "decision_proxy"),

    # -- MCP ------------------------------------------------------------------
    Route("GET", "/mcp",
          "do_GET->inline", 'MCP transport info banner', "mcp"),
    Route("POST", "/mcp",
          "do_POST->_handle_mcp", 'MCP JSON-RPC (initialize / tools/list / tools/call)', "mcp"),

    # -- CLM session management ---------------------------------------------
    Route("GET",  "/v1/clm/workspaces",
          "_clm_get->list_workspaces", 'List all workspaces', "session_mgmt"),
    Route("POST", "/v1/clm/workspaces",
          "_clm_post->create_workspace", 'Create a new workspace (branch)', "session_mgmt"),

    Route("GET",  "/v1/clm/sessions",
          "_clm_get->list_sessions", 'List open session ids', "session_mgmt"),
    Route("POST", "/v1/clm/sessions",
          "_clm_post->open_session", 'Open (or re-open) a memory branch; 201 on create, 200 on reuse', "session_mgmt"),

    # -- CLM session sub-resources ------------------------------------------
    Route("GET", "/v1/clm/sessions/<sid>/status",
          "_clm_get->status", 'Session status + turn count + token counts', "turns"),
    Route("GET", "/v1/clm/sessions/<sid>/snapshot",
          "_clm_get->snapshot", 'Full harness snapshot (live blocks, archive size, chats)', "turns"),
    Route("GET", "/v1/clm/sessions/<sid>/turns",
          "_clm_get->replay_turns", 'Replay transcript for one or all chat slots', "turns"),

    # -- CLM turn (the hot path) ---------------------------------------------
    Route("POST", "/v1/clm/sessions/<sid>/turns",
          "_clm_post->_turns", 'Add a user message; returns assistant reply + events. SSE when stream=true', "turns"),

    # -- CLM agent surface ---------------------------------------------------
    Route("GET",  "/v1/clm/sessions/<sid>/tools",
          "_clm_get->agent_surface_read", 'List available tools and their enabled state', "agent_surface"),
    Route("POST", "/v1/clm/sessions/<sid>/tools",
          "_clm_post->_clm_tools_post", 'Enable/disable a tool by name', "agent_surface"),

    Route("GET",  "/v1/clm/sessions/<sid>/activity",
          "_clm_get->agent_surface_read", 'Recent tool-call activity log', "agent_surface"),
    Route("POST", "/v1/clm/sessions/<sid>/toolcall",
          "_clm_post->_clm_tools_post", 'Execute a tool call (with approval if required)', "agent_surface"),

    Route("GET",  "/v1/clm/sessions/<sid>/approvals",
          "_clm_get->agent_surface_read", 'Pending / recent approvals', "agent_surface"),

    Route("POST", "/v1/clm/sessions/<sid>/sandbox",
          "_clm_post->_clm_tools_post", 'Execute code in the sandbox (jail/docker)', "agent_surface"),

    Route("POST", "/v1/clm/sessions/<sid>/search",
          "_clm_post->_clm_tools_post", 'Web search via tool', "agent_surface"),

    # -- CLM memory ops ------------------------------------------------------
    Route("GET",  "/v1/clm/sessions/<sid>/archive",
          "_clm_get->recall", 'Search archive by query; returns hits + reinject candidates', "turns"),
    Route("POST", "/v1/clm/sessions/<sid>/reinject",
          "_clm_post->reinject", 'Re-inject an archived block back into the live window', "turns"),

    # -- CLM chat-slot ops ---------------------------------------------------
    Route("GET",  "/v1/clm/sessions/<sid>/block/<id>",
          "_clm_get->get_block", 'Fetch a single block by id (live window)', "turns"),

    # -- CLM settings / lifecycle --------------------------------------------
    Route("POST", "/v1/clm/sessions/<sid>/settings",
          "_clm_post->push_settings", 'Push runtime settings (keep_pct, forget thresholds, etc.)', "session_mgmt"),
    Route("DELETE", "/v1/clm/sessions/<sid>",
          "do_DELETE->drop_session", 'Close and drop a session (archive retained on disk)', "session_mgmt"),

    # -- Static assets (campion) ---------------------------------------------
    Route("GET", "/",
          "_static_route", 'Served index.html', "static"),
    Route("GET", "/index.html",
          "_static_route", 'Studio SPA shell', "static"),
    Route("GET", "/app.js",
          "_static_route", 'Studio JS bundle (chat UI + debug panel)', "static"),
    Route("GET", "/style.css",
          "_static_route", 'Studio CSS', "static"),
    Route("GET", "/dyson.svg",
          "_static_route", 'Decorative asset: Dyson sphere (loading state)', "static"),
    Route("GET", "/dyson-done.svg",
          "_static_route", 'Decorative asset: Dyson sphere (complete)', "static"),
    Route("GET", "/dyson.js",
          "_static_route", 'Dyson animation logic', "static"),
    Route("GET", "/favicon.svg",
          "_static_route", 'Favicon', "static"),
]

# ---------------------------------------------------------------------------
# Pattern matching (used by tests and future dispatch refactor)
# ---------------------------------------------------------------------------

# Pre-compiled regex per route, built lazily.
_PATTERN_CACHE: Dict[str, re.Pattern] = {}
_PARAM_NAMES_RE = re.compile(r"<([^>]+)>")


def _compile(pattern: str) -> Tuple[re.Pattern, List[str]]:
    """Compile a URL pattern into (regex, param_names)."""
    if pattern in _PATTERN_CACHE:
        return _PATTERN_CACHE[pattern], []  # type: ignore[misc]

    parts = []
    names: List[str] = []
    for seg in pattern.split("/"):
        m = re.fullmatch(r"<([^>]+)>", seg)
        if m:
            names.append(m.group(1))
            parts.append(f"(?P<{m.group(1)}>[^/]+)")
        else:
            # escape regex metachars in literal segments
            parts.append(re.escape(seg))
    rx = re.compile("^" + "/".join(parts) + "$")
    _PATTERN_CACHE[pattern] = rx  # type: ignore[assignment]
    return rx, names


def match(method: str, path: str) -> Optional[Match]:
    """Find the first route matching *method* and concrete URL *path*.

    Returns a :class:`Match` with captured params, or ``None`` if no route
    matches.  Query strings are ignored (caller should strip them).

    Example::

        >>> m = match("GET", "/v1/clm/sessions/ws:alice/turns")
        >>> m.route.handler
        '_clm_get->replay_turns'
        >>> m.params['sid']
        'ws:alice'
    """
    # strip query string if present (caller should, but be defensive)
    path = path.split("?", 1)[0]
    for route in ROUTES:
        if route.method != method.upper():
            continue
        rx, _ = _compile(route.pattern)
        mobj = rx.match(path)
        if mobj:
            return Match(route, {k: v for k, v in mobj.groupdict().items()})
    return None


def all_routes_for_path(pattern_fragment: str) -> List[Route]:
    """Return every route whose pattern contains *pattern_fragment*.

    Useful for test assertions like ``all_routes_for_path("/v1/clm/sessions")``.
    """
    return [r for r in ROUTES if pattern_fragment in r.pattern]


def documented_route_count() -> int:
    """Total number of routes registered (for completeness tests)."""
    return len(ROUTES)
