"""Regression tests for the paired security-review fixes (2026-11-09).

Covers: web-fetch scheme + SSRF guard (C-3/C-4), request-body cap (L-1),
canonical session-id validation at the router edge (M-3), and MCP calls
routed through the approval broker (M-2). All offline.
"""

import json
import urllib.error
import urllib.request

import pytest

from shatterling.purslane.security import SecurityConfig
from shatterling.purslane.server import start_supervisor
from shatterling.tools import ToolCall, Sandbox
from shatterling.tools import web as _web
from shatterling.engine.harness import MemoryHarness
from shatterling.engine.llm import StubChat
from shatterling.engine.config import HarnessConfig


# ---------------------------------------------------------------------------
# C-3 / C-4: scheme restriction + resolution-based SSRF guard
# ---------------------------------------------------------------------------


def test_web_fetch_rejects_non_http_schemes():
    for url in ("file:///etc/passwd", "gopher://127.0.0.1:6379/_INFO", "data:text/plain,hi"):
        with pytest.raises(_web.WebError):
            _web.fetch_page(url)


def test_web_fetch_blocks_metadata_and_private():
    # Decimal form of 169.254.169.254 (the audit's bypass case) and the raw
    # metadata IP must both be refused by the resolution-based guard.
    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://2130706433/latest/meta-data/",  # decimal = 169.254.169.254
    ):
        with pytest.raises(_web.WebError):
            _web._validate_url(url)


def test_web_fetch_allows_public_host(monkeypatch):
    monkeypatch.setattr(
        _web.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))]
    )
    # must not raise
    _web._validate_url("http://example.com/x")


def test_web_fetch_ipv4_in_ipv6_metadata(monkeypatch):
    # ::ffff:169.254.169.254 is the IPv4-in-IPv6 bypass from the audit.
    monkeypatch.setattr(
        _web.socket,
        "getaddrinfo",
        lambda *a, **k: [(10, 1, 6, "", ("::ffff:a9fe:a9fe", 80))],
    )
    with pytest.raises(_web.WebError):
        _web._validate_url("http://[::ffff:169.254.169.254]/latest/meta-data/")


# ---------------------------------------------------------------------------
# L-1: request body cap
# ---------------------------------------------------------------------------


def test_body_cap_rejects_oversized():
    # Patch the class attribute directly (the env var is read at import time,
    # so monkeypatch.setenv would be reverted on test exit -- before shutdown's
    # own health check runs -- and break teardown). Restore it in finally.
    from shatterling.purslane.server import ProxyHandler

    original = ProxyHandler.MAX_BODY_BYTES
    ProxyHandler.MAX_BODY_BYTES = 16
    sup = start_supervisor(
        public_port=0,
        served_model="mock-model",
        backend_mode="mock",
        systemone_mode="none",
        harness_kwargs={"max_context_tokens": 600},
        timeout=30,
    )
    try:
        req = urllib.request.Request(
            f"{sup.base_url}/v1/clm/sessions",
            data=b"x" * 200,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            status = 200
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 413
    finally:
        ProxyHandler.MAX_BODY_BYTES = original
        sup.shutdown()


def http_call(sup, path, method="GET", body=None, headers=None):
    url = f"{sup.base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    raw = resp.read().decode()
    try:
        return resp.status, json.loads(raw)
    except Exception:
        return resp.status, raw


@pytest.fixture()
def sup(tmp_path):
    s = start_supervisor(
        public_port=0,
        served_model="mock-model",
        backend_mode="mock",
        systemone_mode="mock",
        timeout=30,
        harness_kwargs={
            "workdir": str(tmp_path),
            "max_context_tokens": 800,
            "tools_enabled": True,
            "search_provider": "stub",
            "sandbox_mode": "jail",
        },
    )
    yield s
    s.shutdown()


# ---------------------------------------------------------------------------
# M-3: canonical session-id validation at the router edge
# ---------------------------------------------------------------------------


def test_sid_validation_rejects_traversal(sup):
    st, _ = http_call(sup, "/v1/clm/sessions/..%2F..%2Fetc/snapshot")
    assert st == 400
    st, _ = http_call(sup, "/v1/clm/sessions/%3Cscript%3E/status")
    assert st == 400
    # a well-formed branch id still works (404 unknown session, not 400)
    st, _ = http_call(sup, "/v1/clm/sessions/ws%3Aghost/status")
    assert st in (404, 200)


# ---------------------------------------------------------------------------
# M-2: MCP model-initiated calls go through the approval broker
# ---------------------------------------------------------------------------


def test_mcp_origin_respects_approval_policy(tmp_path):
    h = MemoryHarness(
        StubChat(),
        HarnessConfig(workdir=str(tmp_path), tools_enabled=True, approval_mode="ask"),
    )
    res = h.tools.execute(
        ToolCall(id="m", name="run_command", args={"command": "echo hi"}, origin="mcp")
    )
    assert not res.ok
    assert "denied" in (res.error or res.summary).lower()
    # a genuinely human call still bypasses approval
    ok = h.tools.execute(
        ToolCall(id="u", name="run_command", args={"command": "echo hi"}, origin="user")
    )
    assert ok.ok
