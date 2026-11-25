"""Parity test: every route in the declarative table is implemented by the server.

Walks ROUTES, substitutes concrete values for parameters, sends the request,
and asserts the dispatcher recognized it (i.e., no "no route for" 404).
Handler-level 404s (unknown session, missing block) are acceptable — they
prove the route was matched and dispatched to the right handler.

Conditional routes (native passthrough) that require a real backend are
skipped when no backend_root is configured.
"""

import json
import urllib.error
import urllib.request
import pytest
from shatterling.purslane.routes import ROUTES, documented_route_count
from shatterling.purslane.server import start_supervisor


@pytest.fixture()
def sup(tmp_path):
    s = start_supervisor(
        public_port=0,
        served_model="mock-model",
        backend_mode="mock",
        systemone_mode="mock",
        harness_kwargs={
            "workdir": str(tmp_path),
            "max_context_tokens": 600,
            "memory_preamble": True,
        },
        timeout=30,
    )
    yield s
    s.shutdown()


def _http(sup, method, path, body=None):
    """Minimal HTTP call returning (status_code, parsed_body_or_str)."""
    url = f"{sup.base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read().decode()
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def _is_dispatcher_404(status: int, body) -> bool:
    """True when the 404 came from the route dispatcher (not a handler)."""
    if status != 404:
        return False
    text = str(body)
    return "no route for" in text


def _concrete_path(pattern: str) -> str:
    """Replace <param> placeholders with concrete test values."""
    replacements = {
        "sid": "ws:parity",
        "sub": "exec",
        "name": "parity-ws",
        "id": "1",
    }
    for key, val in replacements.items():
        pattern = pattern.replace(f"<{key}>", val)
    return pattern


def _body_for(route) -> dict | None:
    """Provide a minimal valid body for POST routes that need one."""
    if route.method != "POST":
        return None
    p = route.pattern
    if "/turns" in p:
        return {"content": "parity test"}
    if "/reset" in p:
        return {}
    if "/chat-open" in p:
        return {"chat": "chat-001"}
    if "/tools" in p:
        return {"tool": "run_command", "enabled": True}
    if "/approvals" in p:
        return {"id": 1, "approve": True, "by": "test"}
    if "/sandbox" in p:
        return {"command": "echo ok"}
    if "/search" in p:
        return {"query": "test"}
    if "/toolcall" in p:
        return {"tool": "run_command", "args": {"command": "echo hi"}}
    if "/reinject" in p:
        return {"block_id": 1}
    if "/settings" in p:
        return {"keep_pct": 0.5}
    if p.endswith("/workspaces"):
        return {"name": "parity-ws"}
    if p.endswith("/sessions"):
        return {"session": "ws:parity"}
    return {}


def test_all_documented_routes_are_implemented(sup):
    """Every non-conditional route must be recognized by the dispatcher."""
    # Open a session so session-scoped routes have something to hit.
    _http(sup, "POST", "/v1/clm/sessions", {"session": "ws:parity"})

    failures = []
    for route in ROUTES:
        # Skip native passthrough: requires a real backend_root which the
        # mock setup doesn't provide (the dispatcher correctly 404s without one).
        if route.category == "native_passthrough":
            continue
        # Skip static routes: served before auth, handled by _static_route()
        # which checks file existence on disk (not via match()).
        if route.category == "static":
            continue

        path = _concrete_path(route.pattern)
        body = _body_for(route)
        st, resp_body = _http(sup, route.method, path, body)
        if _is_dispatcher_404(st, resp_body):
            failures.append(f"{route.method} {route.pattern} → dispatcher 404")

    assert not failures, (
        f"{len(failures)} documented route(s) not recognized by dispatcher:\n"
        + "\n".join(f"  {f}" for f in failures)
    )


def test_route_table_completeness():
    """Sanity: the route table covers all expected categories and is non-trivial."""
    count = documented_route_count()
    assert count >= 30, f"expected at least 30 routes, got {count}"

    categories = {r.category for r in ROUTES}
    expected_cats = {
        "meta",
        "native_passthrough",
        "decision_proxy",
        "session_mgmt",
        "turns",
        "agent_surface",
        "static",
        "mcp",
    }
    missing = expected_cats - categories
    assert not missing, f"missing route categories: {missing}"


def test_match_function_consistency():
    """match() finds every route in the table when given a concrete path."""
    from shatterling.purslane.routes import match

    for route in ROUTES:
        path = _concrete_path(route.pattern)
        m = match(route.method, path)
        assert m is not None, (
            f"match() returned None for {route.method} {route.pattern} "
            f"(concrete: {path})"
        )
        assert m.route.pattern == route.pattern, (
            f"match({route.method}, {path!r}) → {m.route.pattern!r}, "
            f"expected {route.pattern!r}"
        )
