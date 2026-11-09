"""Tests for the security layer: CORS policy, API-key auth, and warnings.

Covers:
- Default CORS is same-origin only (no ACAO header for cross-origin)
- Configured CORS origins are echoed back correctly
- API key auth: 401 without key, 200 with correct key, exempt paths open
- Startup warnings when server is wide open
- SDK client passes the API key through
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from shatterling.purslane.security import SecurityConfig
from shatterling.purslane.server import start_supervisor


# ---------------------------------------------------------------------------
# Unit tests for SecurityConfig (no server needed)
# ---------------------------------------------------------------------------


class TestSecurityConfig:
    def test_default_is_same_origin_only(self):
        sec = SecurityConfig()
        # No origin header -> no ACAO (same-origin / non-browser)
        assert sec.cors_allowed_origin(None) is None
        # Cross-origin with empty cors_origins -> blocked
        assert sec.cors_allowed_origin("https://evil.com") is None
        # Auth disabled by default
        assert not sec.is_auth_enabled()
        assert sec.check_auth(None)  # passes when auth off

    def test_cors_specific_origins(self):
        sec = SecurityConfig(cors_origins=("https://trusted.example.com",))
        assert (
            sec.cors_allowed_origin("https://trusted.example.com")
            == "https://trusted.example.com"
        )
        assert sec.cors_allowed_origin("https://evil.com") is None
        # No origin (same-origin request) -> no ACAO needed
        assert sec.cors_allowed_origin(None) is None

    def test_cors_wildcard(self):
        sec = SecurityConfig(cors_origins=("*",))
        assert sec.cors_allowed_origin("https://anything.com") == "*"
        assert sec.cors_allowed_origin(None) is None  # still no ACAO for same-origin

    def test_cors_allow_credentials(self):
        sec_specific = SecurityConfig(cors_origins=("https://a.com",))
        assert sec_specific.cors_allow_credentials() is True
        sec_wildcard = SecurityConfig(cors_origins=("*",))
        assert sec_wildcard.cors_allow_credentials() is False
        sec_none = SecurityConfig()
        assert sec_none.cors_allow_credentials() is False

    def test_auth_enabled_rejects_no_key(self):
        sec = SecurityConfig(api_key="s3cret-key")
        assert sec.is_auth_enabled()
        assert not sec.check_auth(None)
        assert not sec.check_auth("")
        assert not sec.check_auth("Bearer wrong-key")
        assert not sec.check_auth("Basic s3cret-key")  # wrong scheme
        assert sec.check_auth("Bearer s3cret-key")
        assert sec.check_auth("bearer s3cret-key")  # case-insensitive scheme

    def test_auth_exempt_paths(self):
        sec = SecurityConfig(api_key="s3cret")
        assert sec.is_exempt("/health")
        assert sec.is_exempt("/v1/health")
        assert sec.is_exempt("/version")
        assert sec.is_exempt("/")  # static
        assert sec.is_exempt("/index.html")
        assert not sec.is_exempt("/v1/clm/sessions")
        assert not sec.is_exempt("/v1/chat/completions")
        assert not sec.is_exempt("/mcp")

    def test_startup_warnings_no_auth_non_loopback(self):
        sec = SecurityConfig(api_key="", bind_host="0.0.0.0")
        warnings = sec.startup_warnings()
        assert any("non-loopback" in w for w in warnings)

    def test_startup_warnings_cors_wildcard_no_auth(self):
        sec = SecurityConfig(api_key="", cors_origins=("*",), bind_host="127.0.0.1")
        warnings = sec.startup_warnings()
        assert any("CSRF" in w for w in warnings)

    def test_startup_warnings_no_issues_loopback(self):
        sec = SecurityConfig(api_key="mykey", bind_host="127.0.0.1")
        warnings = sec.startup_warnings()
        assert warnings == []

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("CLM_MEM_API_KEY", "env-key-123")
        monkeypatch.setenv("CLM_MEM_CORS_ORIGINS", "https://a.com, https://b.com")
        sec = SecurityConfig.from_env(bind_host="10.0.0.1")
        assert sec.api_key == "env-key-123"
        assert sec.cors_origins == ("https://a.com", "https://b.com")
        assert sec.bind_host == "10.0.0.1"

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("CLM_MEM_API_KEY", "env-key")
        sec = SecurityConfig.from_env(api_key="explicit", bind_host="127.0.0.1")
        assert sec.api_key == "explicit"


# ---------------------------------------------------------------------------
# Integration tests: live server with security config
# ---------------------------------------------------------------------------


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


class TestServerAuth:
    """Integration: server with API key enabled."""

    @pytest.fixture()
    def secured(self, tmp_path):
        sup = start_supervisor(
            public_port=0,
            served_model="mock-model",
            backend_mode="mock",
            systemone_mode="none",
            harness_kwargs={
                "workdir": str(tmp_path),
                "max_context_tokens": 600,
            },
            timeout=30,
            api_key="test-secret-key-12345",
        )
        yield sup
        sup.shutdown()

    def test_health_is_exempt(self, secured):
        st, body = http_call(secured, "/health")
        assert st == 200
        assert body["status"] == "ok"

    def test_version_is_exempt(self, secured):
        st, _ = http_call(secured, "/version")
        assert st == 200

    def test_static_index_is_exempt(self, secured):
        # / should serve the studio shell without auth
        req = urllib.request.Request(f"{secured.base_url}/")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 200

    def test_api_requires_key(self, secured):
        st, body = http_call(secured, "/v1/clm/sessions")
        assert st == 401
        assert "authentication required" in body["error"]["message"]

    def test_api_accepts_correct_key(self, secured):
        headers = {"Authorization": "Bearer test-secret-key-12345"}
        st, body = http_call(secured, "/v1/clm/sessions", headers=headers)
        assert st == 200
        assert "sessions" in body

    def test_api_rejects_wrong_key(self, secured):
        headers = {"Authorization": "Bearer wrong-key"}
        st, body = http_call(secured, "/v1/clm/sessions", headers=headers)
        assert st == 401

    def test_api_rejects_no_scheme(self, secured):
        # Just the raw key without Bearer prefix
        headers = {"Authorization": "test-secret-key-12345"}
        st, body = http_call(secured, "/v1/clm/sessions", headers=headers)
        assert st == 401

    def test_post_requires_key(self, secured):
        st, body = http_call(
            secured,
            "/v1/clm/sessions",
            method="POST",
            body={"session": "ws:test"},
        )
        assert st == 401

    def test_post_with_key_works(self, secured):
        headers = {"Authorization": "Bearer test-secret-key-12345"}
        st, body = http_call(
            secured,
            "/v1/clm/sessions",
            method="POST",
            body={"session": "ws:test"},
            headers=headers,
        )
        assert st in (200, 201)

    def test_mcp_requires_key(self, secured):
        st, body = http_call(
            secured,
            "/mcp",
            method="POST",
            body={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )
        assert st == 401

    def test_delete_requires_key(self, secured):
        # First create a session with auth, then try delete without
        headers = {"Authorization": "Bearer test-secret-key-12345"}
        http_call(
            secured,
            "/v1/clm/sessions",
            method="POST",
            body={"session": "ws:todelete"},
            headers=headers,
        )
        st, _ = http_call(
            secured,
            "/v1/clm/sessions/ws%3Atodelete",
            method="DELETE",
        )
        assert st == 401


class TestServerCORS:
    """Integration: CORS headers on the live server."""

    @pytest.fixture()
    def no_cors(self, tmp_path):
        """Default: same-origin only (no ACAO for cross-origin)."""
        sup = start_supervisor(
            public_port=0,
            served_model="mock-model",
            backend_mode="mock",
            systemone_mode="none",
            harness_kwargs={
                "workdir": str(tmp_path),
                "max_context_tokens": 600,
            },
            timeout=30,
        )
        yield sup
        sup.shutdown()

    @pytest.fixture()
    def cors_configured(self, tmp_path):
        """Specific origins allowed."""
        sup = start_supervisor(
            public_port=0,
            served_model="mock-model",
            backend_mode="mock",
            systemone_mode="none",
            harness_kwargs={
                "workdir": str(tmp_path),
                "max_context_tokens": 600,
            },
            timeout=30,
            cors_origins=("https://studio.example.com", "http://localhost:3000"),
        )
        yield sup
        sup.shutdown()

    def test_default_no_acao_for_cross_origin(self, no_cors):
        """Without cors_origins, cross-origin requests get no ACAO header."""
        req = urllib.request.Request(f"{no_cors.base_url}/health")
        req.add_header("Origin", "https://evil.com")
        resp = urllib.request.urlopen(req, timeout=10)
        acao = resp.headers.get("Access-Control-Allow-Origin")
        assert acao is None  # blocked by browser

    def test_default_no_acao_for_same_origin(self, no_cors):
        """Same-origin (no Origin header) also gets no ACAO -- not needed."""
        req = urllib.request.Request(f"{no_cors.base_url}/health")
        resp = urllib.request.urlopen(req, timeout=10)
        acao = resp.headers.get("Access-Control-Allow-Origin")
        assert acao is None

    def test_configured_origin_echoed(self, cors_configured):
        """Allowed origin is echoed back in ACAO."""
        req = urllib.request.Request(f"{cors_configured.base_url}/health")
        req.add_header("Origin", "https://studio.example.com")
        resp = urllib.request.urlopen(req, timeout=10)
        acao = resp.headers.get("Access-Control-Allow-Origin")
        assert acao == "https://studio.example.com"

    def test_configured_origin_rejected(self, cors_configured):
        """Non-allowed origin gets no ACAO."""
        req = urllib.request.Request(f"{cors_configured.base_url}/health")
        req.add_header("Origin", "https://evil.com")
        resp = urllib.request.urlopen(req, timeout=10)
        acao = resp.headers.get("Access-Control-Allow-Origin")
        assert acao is None

    def test_preflight_options(self, cors_configured):
        """OPTIONS preflight gets 204 with CORS headers."""
        req = urllib.request.Request(
            f"{cors_configured.base_url}/v1/clm/sessions", method="OPTIONS"
        )
        req.add_header("Origin", "https://studio.example.com")
        req.add_header("Access-Control-Request-Method", "POST")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 204
        acao = resp.headers.get("Access-Control-Allow-Origin")
        assert acao == "https://studio.example.com"

    def test_allow_credentials_header(self, cors_configured):
        """Specific origins get Access-Control-Allow-Credentials: true."""
        req = urllib.request.Request(f"{cors_configured.base_url}/health")
        req.add_header("Origin", "https://studio.example.com")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.headers.get("Access-Control-Allow-Credentials") == "true"


class TestServerWarnings:
    """Startup warnings are emitted for dangerous configs."""

    def test_warning_non_loopback_no_auth(self, tmp_path, capsys):
        # We can't easily bind to 0.0.0.0 in tests (port conflicts), so we
        # test the warning logic directly via SecurityConfig.
        sec = SecurityConfig(api_key="", bind_host="0.0.0.0")
        warnings = sec.startup_warnings()
        assert len(warnings) >= 1
        assert "non-loopback" in warnings[0]

    def test_warning_cors_star_no_auth(self, tmp_path):
        sec = SecurityConfig(api_key="", cors_origins=("*",), bind_host="127.0.0.1")
        warnings = sec.startup_warnings()
        assert any("CSRF" in w for w in warnings)

    def test_no_warning_safe_config(self, tmp_path):
        sec = SecurityConfig(api_key="abc123", bind_host="127.0.0.1")
        warnings = sec.startup_warnings()
        assert warnings == []


class TestSDKAuth:
    """The SDK client passes the API key to authenticated servers."""

    @pytest.fixture()
    def secured(self, tmp_path):
        sup = start_supervisor(
            public_port=0,
            served_model="mock-model",
            backend_mode="mock",
            systemone_mode="none",
            harness_kwargs={
                "workdir": str(tmp_path),
                "max_context_tokens": 600,
            },
            timeout=30,
            api_key="sdk-test-key-999",
        )
        yield sup
        sup.shutdown()

    def test_connect_with_api_key(self, secured):
        from shatterling.clients.sdk import connect

        h = connect(
            secured.public_base,
            workspace="sdktest",
            api_key="sdk-test-key-999",
        )
        resp = h.open()
        assert resp["session_id"] == "ws:sdktest"

    def test_connect_without_key_fails(self, secured):
        from shatterling.clients.sdk import connect

        h = connect(secured.public_base, workspace="ws:nokey", api_key="")
        with pytest.raises(RuntimeError, match="401|authentication"):
            h.open()

    def test_connect_env_var(self, secured, monkeypatch):
        from shatterling.clients.sdk import connect

        monkeypatch.setenv("CLM_MEM_API_KEY", "sdk-test-key-999")
        h = connect(secured.public_base, workspace="envtest")
        resp = h.open()
        assert resp["session_id"] == "ws:envtest"

    def test_full_turn_with_auth(self, secured):
        from shatterling.clients.sdk import connect

        h = connect(
            secured.public_base,
            workspace="fullturn",
            api_key="sdk-test-key-999",
        )
        reply = h.step("hello world")
        assert isinstance(reply, str)
        assert len(reply) > 0
