"""Purslane -- the serve/API daemon. Measured, analytical, cautious.

Named after Purslane from *House of Suns*: a careful, deliberate character who
keeps reliable expectations about what will happen and enforces contracts
precisely. That is exactly what this layer does: it owns the HTTP surface,
manages subprocess lifetimes (tokamak / mock backends), serves static assets,
and never silently drops data -- every request either succeeds with honest
status codes or fails loudly.

Design theory: a cautious gatekeeper keeps reliable expectations about its own
load; if something will break it says so before the fact rather than after.
"""

from .server import ProxyConfig, Supervisor, start_supervisor

__all__ = ["ProxyConfig", "Supervisor", "start_supervisor"]
