"""Harness configuration. All knobs live here; ``shortterm_keep_pct`` is the
variable that controls how much short-term memory survives each FIFO round."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HarnessConfig:
    # --- budget ---------------------------------------------------------
    max_context_tokens: int = 8192      # live-window token budget

    # --- eviction policy --------------------------------------------------
    shortterm_keep_pct: float = 0.5     # fraction of shortterm KEPT per FIFO round
    keep_recent_turns: int = 2          # coherence window exempt from phase-1 eviction
    offload_overflow: bool = True       # phase 3: longterm -> archive + pointer stub

    # --- classification (same model, no retraining) ------------------------
    classify_mode: str = "both"         # inline | post | both
    classifier_max_block_chars: int = 400

    # --- files / serving ----------------------------------------------------
    workdir: str = "."                  # context.md / context.jsonl / archive.jsonl live here
    tokenizer_name: str = ""            # HF tokenizer id for exact counts (optional)
    memory_preamble: bool = True        # inject the CLM memory system prompt on first step

    def __post_init__(self) -> None:
        if not 0.0 <= self.shortterm_keep_pct < 1.0:
            raise ValueError("shortterm_keep_pct must be in [0, 1)")
        if self.classify_mode not in ("inline", "post", "both"):
            raise ValueError("classify_mode must be inline | post | both")
        if self.max_context_tokens < 64:
            raise ValueError("max_context_tokens too small to hold anything useful")
