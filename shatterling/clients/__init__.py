"""Client SDK for talking to a running purslane daemon from Python code.

RemoteHarness is the thin HTTP client that tokamak (and any other consumer) uses
in served mode: it mirrors what app.js does in the browser, but over urllib so no
extra dependency is needed. For local use without a server, prefer MemoryHarness
from shatterling.engine directly.
"""

from .sdk import RemoteConfig, RemoteHarness, connect

__all__ = ["RemoteConfig", "RemoteHarness", "connect"]
