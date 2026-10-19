"""clm-mem: a zero-training long/short-term memory harness for open chat models.

Inspired by "Context Language Models" (Shao et al., arXiv:2609.37725): the live
token window is treated as an editable artifact that the SAME model helps manage
-- no retraining, just prompting plus a deterministic eviction engine.

Quick start:
    from clm_mem import MemoryHarness, HarnessConfig, OpenAIChat

    h = MemoryHarness(OpenAIChat("http://localhost:8000/v1", "Qwen/Qwen3-8B-Instruct"),
                      HarnessConfig(max_context_tokens=8192, shortterm_keep_pct=0.5))
    print(h.step("Remember: the deploy port is 8080."))
"""
from .blocks import LONG, SHORT, TRANSIENT, VALID_TAGS, Block
from .config import HarnessConfig
from .harness import MemoryHarness
from .llm import OpenAIChat, StubChat
from .remote import RemoteHarness
from .server import ProxyConfig, Supervisor, start_supervisor
from .tagging import split_tagged
from .tokens import TokenCounter

__version__ = "0.2.0"
__all__ = [
    "Block", "HarnessConfig", "MemoryHarness", "OpenAIChat", "StubChat",
    "ProxyConfig", "Supervisor", "start_supervisor", "RemoteHarness",
    "TokenCounter", "split_tagged",
    "LONG", "SHORT", "TRANSIENT", "VALID_TAGS", "__version__",
]
