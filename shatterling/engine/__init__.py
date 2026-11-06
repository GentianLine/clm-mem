"""The shatterling's ledger: pure context-memory engine.

No HTTP, no threads, no subprocess, no CLI. This layer is the deterministic
core -- blocks, tags, budget, three-phase eviction, archive, recall. It is
unit-testable with StubChat and runs in a notebook cell.

Design theory (House of Suns): a shatterling's ledger must be coherent on its
own. Every clone can audit her own memory without asking another process.
"""

from .blocks import LONG, SHORT, TRANSIENT, VALID_TAGS, Block
from .config import HarnessConfig
from .harness import MemoryHarness
from .llm import OpenAIChat, StubChat, SystemOneClient, MockSystemOne
from .tagging import split_tagged, strip_tags
from .tokens import TokenCounter

__all__ = [
    "Block", "LONG", "SHORT", "TRANSIENT", "VALID_TAGS",
    "HarnessConfig", "MemoryHarness",
    "OpenAIChat", "StubChat", "SystemOneClient", "MockSystemOne",
    "split_tagged", "strip_tags",
    "TokenCounter",
]
