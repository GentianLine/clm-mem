"""Security layer: CORS policy, API-key auth, and startup warnings.

Defaults are secure-by-default:
- CORS: same-origin only (no Access-Control-Allow-Origin header for cross-origin)
- Auth: disabled by default on loopback; strongly warned when non-loopback

The :class:`SecurityConfig` is attached to the supervisor's ProxyConfig and
checked on every request before routing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import List, Optional, Set

log = logging.getLogger("clm-mem.security")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Paths that never require authentication (health checks, static assets).
AUTH_EXEMPT_PATHS: frozenset = frozenset(
    {
        "/health",
        "/v1/health",
        "/version",
        "//",  # empty path after split
    }
)

#: Static asset paths (web studio) -- exempt from auth when the UI is enabled.
STATIC_PATHS: frozenset = frozenset(
    {
        "/",
        "/index.html",
        "/style.css",
        "/app.js",
        "/favicon.svg",
        "/dyson.svg",
        "/dyson-done.svg",
        "/dyson.js",
    }
)


def _is_loopback(host: str) -> bool:
    """True for 127.0.0.1, ::1, localhost, or empty (bind to all loopback)."""
    return host in ("127.0.0.1", "::1", "localhost", "")


@dataclass
class SecurityConfig:
    """Security settings for the purslane proxy.

    Attributes
    ----------
    api_key : str
        Bearer token required on all ``/v1/*`` and ``/mcp`` routes when non-empty.
        Generate with ``secrets.token_urlsafe(32)`` or pass your own.
    cors_origins : tuple
        Allowed Origin values for cross-origin requests. Empty tuple means
        same-origin only (the default -- no ACAO header is sent for foreign
        origins). Use ``("*",)`` to allow all (NOT recommended).
    bind_host : str
        The address the server binds to; used for startup warnings.
    """

    api_key: str = ""
    cors_origins: tuple = ()  # empty = same-origin only
    bind_host: str = "127.0.0.1"
    #: Send Access-Control-Allow-Credentials: true for matched origins.
    #: Off by default -- this API authenticates with a Bearer header (not
    #: cookies), so credentialed cross-origin requests are never needed and
    #: the header is an unnecessary CSRF/credential-leak surface. Set True
    #: only if a browser client genuinely requires cookie credentials.
    allow_credentials: bool = False

    @classmethod
    def from_env(cls, **overrides) -> "SecurityConfig":
        """Build from environment variables + explicit overrides.

        Recognised env vars:
        - ``CLM_MEM_API_KEY``: bearer token for API auth
        - ``CLM_MEM_CORS_ORIGINS``: comma-separated list of allowed origins
        """
        api_key = overrides.pop("api_key", os.environ.get("CLM_MEM_API_KEY", ""))
        cors_raw = overrides.pop(
            "cors_origins", os.environ.get("CLM_MEM_CORS_ORIGINS", "")
        )
        cors_origins: tuple = (
            tuple(o.strip() for o in cors_raw.split(",") if o.strip())
            if cors_raw
            else ()
        )
        bind_host = overrides.pop("bind_host", "127.0.0.1")
        allow_creds_raw = overrides.pop(
            "allow_credentials",
            os.environ.get("CLM_MEM_CORS_ALLOW_CREDENTIALS", ""),
        )
        allow_credentials = (
            str(allow_creds_raw).strip().lower() in ("1", "true", "yes", "on")
            if allow_creds_raw is not None
            else False
        )
        return cls(
            api_key=api_key,
            cors_origins=cors_origins,
            bind_host=bind_host,
            allow_credentials=allow_credentials,
        )

    # -- auth helpers -------------------------------------------------------

    def is_auth_enabled(self) -> bool:
        return bool(self.api_key)

    def check_auth(self, authorization_header: Optional[str]) -> bool:
        """Return True when the Authorization header carries our key.

        Accepts ``Bearer <key>`` (case-insensitive scheme). When auth is
        disabled this always returns True.
        """
        if not self.is_auth_enabled():
            return True
        if not authorization_header:
            return False
        # Strip the scheme; accept both "Bearer x" and "bearer x"
        parts = authorization_header.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        provided = parts[1]
        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(provided.encode(), self.api_key.encode())

    def is_exempt(self, path: str) -> bool:
        """True when the path does not require authentication."""
        clean = path.split("?")[0].rstrip("/") or "/"
        if clean in AUTH_EXEMPT_PATHS:
            return True
        if clean in STATIC_PATHS:
            return True
        # /v1/clm/* and /mcp always require auth when enabled
        return False

    # -- CORS helpers -------------------------------------------------------

    def cors_allowed_origin(self, origin: Optional[str]) -> Optional[str]:
        """Return the value for Access-Control-Allow-Origin, or None.

        - No Origin header (same-origin / non-browser): no ACAO needed.
        - Origin in cors_origins: echo it back.
        - ``*`` in cors_origins: return ``*``.
        - Otherwise: None (browser will block the cross-origin read).
        """
        if not origin:
            return None  # same-origin or non-browser request
        if not self.cors_origins:
            return None  # same-origin only policy
        if "*" in self.cors_origins:
            return "*"
        if origin in self.cors_origins:
            return origin
        return None

    def cors_allow_credentials(self) -> bool:
        """True when we should send Access-Control-Allow-Credentials: true.

        Opt-in via the ``cors_allow_credentials`` flag (default False). Never
        combined with a wildcard origin, and only sent for a specific matched
        origin -- the dangerous ACAO+credentials combo is otherwise impossible.
        """
        if not self.allow_credentials:
            return False
        return bool(self.cors_origins) and "*" not in self.cors_origins

    # -- startup warnings ---------------------------------------------------

    def startup_errors(self) -> List[str]:
        """Fatal configuration problems that should refuse to start.

        A non-loopback bind without an API key exposes the full RCE/file/
        web-fetch surface to the network with no authentication -- a warning
        is not a control, so this is an error (see audit C-1).
        """
        errors: List[str] = []
        if not self.is_auth_enabled() and not _is_loopback(self.bind_host):
            errors.append(
                f"refusing to bind {self.bind_host} without authentication: "
                "the proxy exposes sandbox exec, file write/delete and web "
                "fetch over HTTP. Set --api-key / CLM_MEM_API_KEY to enable "
                "authentication, or bind to 127.0.0.1 for local-only use."
            )
        return errors

    def startup_warnings(self, ui_enabled: bool = True) -> List[str]:
        """Return a list of warning strings for the server banner."""
        warnings: List[str] = []
        non_loopback = not _is_loopback(self.bind_host)

        if self.is_auth_enabled() and non_loopback:
            warnings.append(
                f"SECURITY: bound to {self.bind_host} with authentication "
                "enabled. Serve over TLS (or a TLS-terminating reverse proxy) "
                "so the Bearer key is not sent in cleartext."
            )

        if not self.is_auth_enabled() and self.cors_origins == ("*",):
            warnings.append(
                "SECURITY: CORS is set to allow ALL origins (*) with NO API "
                "key. Any website loaded in a browser can make authenticated "
                "requests to this server (CSRF). Set --api-key or restrict "
                "--cors-origins to specific trusted domains."
            )

        if not self.is_auth_enabled() and non_loopback and not self.cors_origins:
            # Same-origin only + no auth + non-loopback: still a risk if the
            # server is on a LAN IP (other machines can curl it directly).
            warnings.append(
                f"SECURITY: bound to {self.bind_host} without authentication. "
                "Direct HTTP clients (curl, scripts) on this network have "
                "unrestricted access. Set --api-key for any non-local deployment."
            )

        if self.is_auth_enabled() and not self.cors_origins and ui_enabled:
            # Auth is on but CORS is same-origin only: the web studio works
            # fine (same origin), but note it for operators who expect remote UI.
            pass  # no warning needed -- this is the safe default

        return warnings
