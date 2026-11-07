"""Per-request and per-session timing capture for the purslane daemon.

Design goals (decision D4):
* The debug/tok/s panel in campion renders **served data** — a timings sidecar
  attached to each turn response, plus SSE frames. No separate polling backend
  is needed; same-origin means no CORS.
* TTFB = time from request received → first byte flushed (for non-streaming:
  total latency). For streaming: time until the first SSE frame hits the wire.
* Token throughput (tok/s) computed server-side when `usage` data is available
  in the harness reply; falls back to a character-count heuristic otherwise.

The :class:`TimingRecorder` object is created at request start, ticked as the
response progresses, and finalized before the response closes. Its ``payload()``
may be:
* Injected into the non-streaming JSON under key ``"_timings"``, or
* Emitted as a final SSE event (type: ``clm-mem-timings``) just before [DONE].

Session-level aggregation lives in :class:`SessionMetrics` so the debug panel can
show rolling tok/s across turns without re-parsing history.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Per-request timing
# ---------------------------------------------------------------------------

@dataclass
class TimingRecorder:
    """Measures latency for a single HTTP request/turn.

    Lifecycle::

        rec = TimingRecorder()
        # ... do work ...
        rec.mark_first_byte()   # when first byte is written (or SSE frame flushed)
        # ... more work / streaming ...
        payload = rec.finalize(tokens=142, model="Qwen3-8B")
    """

    t_start: float = field(default_factory=time.monotonic)
    t_first_byte: Optional[float] = None
    t_end: Optional[float] = None
    tokens_generated: int = 0
    input_tokens: int = 0
    model: str = ""
    stream: bool = False
    # optional sub-phase timing (harness internals)
    phases: Dict[str, float] = field(default_factory=dict)  # phase -> seconds

    def mark_first_byte(self) -> None:
        if self.t_first_byte is None:
            self.t_first_byte = time.monotonic()

    def mark_end(self) -> None:
        self.t_end = time.monotonic()
        if self.t_first_byte is None:
            # non-streaming: first byte == last byte (single flush)
            self.mark_first_byte()

    def record_phase(self, name: str, seconds: float) -> None:
        """Record a named sub-phase duration in seconds."""
        self.phases[name] = round(seconds * 1000.0, 2)  # store as ms

    @property
    def ttfb_ms(self) -> Optional[float]:
        if self.t_first_byte is None:
            return None
        return round((self.t_first_byte - self.t_start) * 1000.0, 2)

    @property
    def total_ms(self) -> float:
        end = self.t_end or time.monotonic()
        return round((end - self.t_start) * 1000.0, 2)

    @property
    def generation_time_s(self) -> Optional[float]:
        """Time from first byte to last (i.e., actual token streaming window)."""
        if not self.stream or self.t_first_byte is None:
            return None
        end = self.t_end or time.monotonic()
        g = end - self.t_first_byte
        return max(g, 1e-6)  # avoid div-by-zero for instant responses

    @property
    def tok_per_s(self) -> Optional[float]:
        if not self.tokens_generated:
            return None
        gen_time = self.generation_time_s or (self.total_ms / 1000.0)
        return round(self.tokens_generated / max(gen_time, 1e-6), 2)

    def payload(self) -> Dict[str, object]:
        """JSON-safe dict for the ``_timings`` sidecar key."""
        d: Dict[str, object] = {
            "ttfb_ms": self.ttfb_ms,
            "total_ms": self.total_ms,
            "stream": self.stream,
            "model": self.model or None,
        }
        if self.tokens_generated:
            d["tokens_out"] = self.tokens_generated
        if self.input_tokens:
            d["tokens_in"] = self.input_tokens
        tps = self.tok_per_s
        if tps is not None:
            d["tok_per_s"] = tps
        if self.phases:
            d["phases_ms"] = dict(self.phases)
        return d

    def sse_event_line(self) -> str:
        """Render as an SSE data line (for streaming responses)."""
        import json
        body = {"type": "clm-mem-timings", **self.payload()}
        return f'data: {json.dumps(body, separators=(",", ":"))}\n\n'


# ---------------------------------------------------------------------------
# Per-session rolling metrics (for the debug panel's tok/s display)
# ---------------------------------------------------------------------------

@dataclass
class SessionMetrics:
    """Rolling timing stats for one session, updated after each turn.

    Stored by purslane keyed on ``session_id``. The campion debug panel can
    poll this (or it's included in the SSE stream) to show a live tok/s gauge.
    """

    # exponential moving average of tok_per_s across turns (alpha=0.3)
    ema_tok_per_s: Optional[float] = None
    total_turns: int = 0
    total_tokens_out: int = 0
    last_ttfb_ms: Optional[float] = None
    last_total_ms: float = 0.0
    # ring buffer of the most recent N per-turn payloads (for sparkline)
    history: List[Dict[str, object]] = field(default_factory=list)
    _history_max: int = 50

    def record(self, rec: TimingRecorder) -> None:
        """Ingest a finalized recorder after each turn."""
        self.total_turns += 1
        if rec.tokens_generated:
            self.total_tokens_out += rec.tokens_generated
        tps = rec.tok_per_s
        if tps is not None and tps > 0:
            alpha = 0.3
            if self.ema_tok_per_s is None:
                self.ema_tok_per_s = tps
            else:
                self.ema_tok_per_s = round(alpha * tps + (1 - alpha) * self.ema_tok_per_s, 2)
        self.last_ttfb_ms = rec.ttfb_ms
        self.last_total_ms = rec.total_ms
        p = rec.payload()
        self.history.append(p)
        if len(self.history) > self._history_max:
            del self.history[: -self._history_max]

    def payload(self) -> Dict[str, object]:
        """JSON for the debug panel: rolling stats + recent history."""
        return {
            "turns": self.total_turns,
            "tokens_out_total": self.total_tokens_out,
            "ema_tok_per_s": self.ema_tok_per_s,
            "last_ttfb_ms": self.last_ttfb_ms,
            "last_total_ms": round(self.last_total_ms, 2),
            "history": list(reversed(self.history[-10:])),  # most recent first
        }
