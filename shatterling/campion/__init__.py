"""Campion -- the chat interface. Impulsive, adventurous.

Named after Campion from *House of Suns*: bold and quick-moving. This is where
users actually see what's happening in real time -- live tok/s readouts, eviction
chips, debug panels that make the ledger visible and playable. It is deliberately
thin: pure static assets (HTML/JS/CSS) served by purslane at its root URL; no own
backend process needed.

Design theory: a good interface should be fast enough to feel alive without ever
being so impulsive it breaks invariants -- the debug panels show what's actually
happening, not just that something happened.
"""

__version__ = "0.4.0"
