"""shatterling -- memory harness and context ledger for open chat models.

Named for the shatterlings of Alastair Reynolds' *House of Suns*: each clone of
Abigail Gentian keeps her own ledger, and every ledger stays coherent. Nothing
silently forgets because something else carefully remembered it elsewhere.

Subpackages (named after Gentleman Line members):

* :mod:`shatterling.purslane` -- the serve/API daemon. Measured, analytical,
  cautious: it enforces the contract, never destroys data silently, and keeps
  reliable expectations about its own load.
* :mod:`shatterling.campion` -- the chat interface (static assets served by
  purslane). Impulsive, adventurous: live tok/s, eviction chips, debug panels.

Quick start (engine only, no server):
    from shatterling.engine import MemoryHarness, HarnessConfig, OpenAIChat
    h = MemoryHarness(OpenAIChat("http://localhost:8000/v1", "Qwen3-8B"),
                      HarnessConfig(max_context_tokens=8192))
    print(h.step("Remember: the deploy port is 8080."))

Quick start (full daemon):
    purslane serve --use-mock --port 8080
"""

from shatterling.engine import (
    Block, HarnessConfig, MemoryHarness,
    OpenAIChat, StubChat, SystemOneClient, MockSystemOne,
    LONG, SHORT, TRANSIENT, VALID_TAGS,
    split_tagged, strip_tags, TokenCounter,
)
from shatterling.purslane import ProxyConfig, Supervisor, start_supervisor
from shatterling.clients import RemoteHarness, connect

__version__ = "0.4.0"

__all__ = [
    # engine
    "Block", "LONG", "SHORT", "TRANSIENT", "VALID_TAGS",
    "HarnessConfig", "MemoryHarness",
    "OpenAIChat", "StubChat", "SystemOneClient", "MockSystemOne",
    "split_tagged", "strip_tags", "TokenCounter",
    # purslane
    "ProxyConfig", "Supervisor", "start_supervisor",
    # clients
    "RemoteHarness", "connect",
]
