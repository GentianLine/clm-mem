"""Token counting: HuggingFace tokenizer when available, heuristic otherwise."""

from __future__ import annotations
import math
from .blocks import Block


class TokenCounter:
    """Counts tokens for eviction decisions.

    Uses the served model's HF tokenizer when ``tokenizer_name`` resolves
    locally (exact); otherwise falls back to a ~3 chars/token estimate.
    Qwen-family BPE runs ~3.3-3.5 chars/token on prose, so /3 slightly
    OVER-counts -- deliberately: under-counting (the old /4 heuristic) let the
    live window exceed the server's real context limit and made requests
    error out mid-session.
    """

    def __init__(self, tokenizer_name: str = ""):
        self._tok = None
        if tokenizer_name:
            try:
                from transformers import AutoTokenizer  # optional dependency

                self._tok = AutoTokenizer.from_pretrained(
                    tokenizer_name, trust_remote_code=True
                )
            except Exception:
                self._tok = None

    @property
    def exact(self) -> bool:
        return self._tok is not None

    def count(self, text: str) -> int:
        if self._tok is not None:
            return len(self._tok.encode(text, disallowed_special=()))
        return max(1, math.ceil(len(text) / 3))

    def count_block(self, b: Block) -> int:
        return self.count(b.content) + 4  # chatml-ish per-message overhead
