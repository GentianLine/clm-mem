"""The memory harness: context-as-a-file mirror + three-phase eviction engine."""
from __future__ import annotations
import dataclasses
import json
import math
import os
from typing import Dict, List, Optional
from .blocks import LONG, SHORT, TRANSIENT, VALID_TAGS, Block
from .config import HarnessConfig
from .tagging import CLASSIFIER_SYSTEM, MEMORY_PREAMBLE, TAG_RE, split_tagged, strip_tags
from .tokens import TokenCounter


class MemoryHarness:
    """Wraps any chat backend and keeps its context inside a token budget.

    Lifecycle of one :meth:`step`:
      1. append the user message,
      2. evict to make room BEFORE generating,
      3. call the model on the current live context,
      4. split the reply on inline ``<longterm>/<shortterm>`` tags ("classify
         as it decodes"), optionally re-label with a post-turn router pass,
      5. evict again, then mirror the window to ``context.md`` / ``context.jsonl``.
    """

    def __init__(self, llm, cfg: Optional[HarnessConfig] = None):
        self.llm = llm
        self.cfg = cfg or HarnessConfig()
        self.counter = TokenCounter(self.cfg.tokenizer_name)
        self.blocks: List[Block] = []
        self.turn = 0
        self.events: List[str] = []
        self._next_id = 0
        if self.cfg.memory_preamble:
            self.add("system", MEMORY_PREAMBLE, tag=LONG, pinned=True)

    # -- paths ------------------------------------------------------------
    @property
    def archive_path(self) -> str:
        return os.path.join(self.cfg.workdir, "archive.jsonl")

    @property
    def context_md_path(self) -> str:
        return os.path.join(self.cfg.workdir, "context.md")

    @property
    def context_jsonl_path(self) -> str:
        return os.path.join(self.cfg.workdir, "context.jsonl")

    # -- basic ops ----------------------------------------------------------
    def add(self, role: str, content: str, tag: str = TRANSIENT,
            pinned: bool = False) -> Block:
        b = Block(id=self._next_id, role=role, content=content.strip(),
                  tag=tag, turn=self.turn, pinned=pinned)
        self._next_id += 1
        self.blocks.append(b)
        return b

    def tokens(self) -> int:
        return sum(self.counter.count_block(b) for b in self.blocks)

    def over_budget(self) -> bool:
        return self.tokens() > self.cfg.max_context_tokens

    # -- classification (same model, no retraining) -------------------------
    def classify_new(self, force: bool = False) -> None:
        """Post-turn router pass: relabel this turn's untagged blocks.

        In ``inline``/``both`` mode the pass is skipped when the model already
        used inline markers for this turn -- otherwise a full extra LLM call
        would run every single turn (the user message block is never tagged
        inline, so an ungated check always finds candidates).
        """
        if self.cfg.classify_mode not in ("post", "both"):
            return
        if not force and any(b.tag in (LONG, SHORT) for b in self.blocks
                             if b.turn == self.turn and b.role == "assistant"):
            return  # inline tagging succeeded this turn; nothing to re-route
        cands = [b for b in self.blocks
                 if not b.pinned and b.turn == self.turn and b.tag == TRANSIENT]
        if not cands:
            return
        lines = "\n".join(
            f"id={b.id} role={b.role} text="
            f"{b.content[:self.cfg.classifier_max_block_chars]}"
            for b in cands)
        try:
            raw = self.llm.chat(
                [{"role": "system", "content": CLASSIFIER_SYSTEM},
                 {"role": "user", "content": f"Conversation blocks:\n{lines}"}],
                temperature=0.0, max_tokens=self.cfg.classifier_max_tokens)
            data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except (ValueError, KeyError):
            self.events.append("classify: unparseable router response; kept transient")
            return
        by_id = {b.id: b for b in self.blocks}
        for lab in data.get("labels", []):
            bid, tag = lab.get("id"), str(lab.get("tag", "")).lower()
            if bid in by_id and tag in VALID_TAGS and not by_id[bid].pinned:
                by_id[bid].tag = tag

    # -- eviction engine ------------------------------------------------------
    def evict(self) -> List[str]:
        """Run the staged policy until under budget. Returns event-log lines.

        Phase 1: delete all transient blocks (outside the recent-turns
                 coherence window; pinned blocks are exempt).
        Phase 2: context is now only long+short -> FIFO rounds on shortterm,
                 each round keeping the newest ``shortterm_keep_pct`` fraction.
        Phase 3: longterm alone overflows -> offload oldest to archive.jsonl
                 and leave an evictable pointer stub.
        """
        log: List[str] = []
        if not self.over_budget():
            return log

        # Phase 1 -- delete everything that is neither longterm nor shortterm.
        min_turn = max(0, self.turn - self.cfg.keep_recent_turns + 1)
        doomed = [b for b in self.blocks
                  if b.tag == TRANSIENT and not b.pinned and b.turn < min_turn]
        if doomed:
            self._drop(doomed)
            log.append(f"phase1: deleted {len(doomed)} transient block(s)")

        # Phase 2 -- shortterm FIFO, keeping `shortterm_keep_pct` per round.
        while self.over_budget():
            shorts = sorted((b for b in self.blocks if b.tag == SHORT),
                            key=lambda b: b.id)
            if not shorts:
                break
            keep_n = math.floor(len(shorts) * self.cfg.shortterm_keep_pct)
            drop = shorts[: len(shorts) - keep_n] or shorts[:1]  # guarantee progress
            self._drop(drop)
            log.append(f"phase2: FIFO-evicted {len(drop)} of {len(shorts)} "
                       f"shortterm (kept newest "
                       f"{self.cfg.shortterm_keep_pct:.0%})")

        # Phase 3 -- longterm overflow: offload oldest, leave a pointer.
        if self.over_budget() and self.cfg.offload_overflow:
            longs = sorted((b for b in self.blocks
                            if b.tag == LONG and not b.pinned), key=lambda b: b.id)
            while self.over_budget() and longs:
                b = longs.pop(0)
                self._archive([b])
                b.content = f"[offloaded longterm #{b.id} -> archive.jsonl]"
                b.tag = SHORT   # the pointer itself is disposable later
                log.append(f"phase3: offloaded longterm #{b.id} to archive, "
                           f"left pointer")

        if self.over_budget():
            log.append("WARN: still over budget after all phases "
                       "(pinned + longterm alone exceed the window)")
        self.events.extend(log)
        return log

    def _drop(self, doomed: List[Block]) -> None:
        self._archive(doomed)   # evict from live window, keep a copy on disk
        ids = {id(b) for b in doomed}
        self.blocks = [b for b in self.blocks if id(b) not in ids]

    def _archive(self, blocks: List[Block]) -> None:
        os.makedirs(self.cfg.workdir, exist_ok=True)
        with open(self.archive_path, "a", encoding="utf-8") as f:
            for b in blocks:
                f.write(json.dumps({"id": b.id, "turn": b.turn, "role": b.role,
                                    "tag": b.tag, "content": b.content}) + "\n")

    # -- recall: offload & retrieval (ContextBench KV-store style) --------------
    def recall(self, query: str, limit: int = 50) -> List[dict]:
        """Substring search over the eviction archive."""
        if not os.path.exists(self.archive_path):
            return []
        hits: List[dict] = []
        with open(self.archive_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if query.lower() in rec["content"].lower():
                    hits.append(rec)
                    if len(hits) >= limit:
                        break
        return hits

    def reinject(self, block_id: int) -> bool:
        """Pull an archived block back into the live window as shortterm."""
        if not os.path.exists(self.archive_path):
            return False
        with open(self.archive_path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("id") == block_id:
                    self.add(rec["role"],
                             f"[recalled #{block_id}] {rec['content']}", tag=SHORT)
                    return True
        return False

    # -- context-as-a-file mirror ------------------------------------------------
    def serialize_messages(self) -> List[Dict[str, str]]:
        """The live window rendered as chat messages for the backend."""
        return [{"role": b.role, "content": b.content} for b in self.blocks]

    def mirror_files(self) -> None:
        os.makedirs(self.cfg.workdir, exist_ok=True)
        with open(self.context_md_path, "w", encoding="utf-8") as f:
            f.write(f"<!-- live context mirror | turn {self.turn} | "
                    f"{self.tokens()}/{self.cfg.max_context_tokens} tokens -->\n")
            for b in self.blocks:
                f.write(f"[[CTX_TURN {b.turn} id={b.id} role={b.role} "
                        f"tag={b.tag}{' PINNED' if b.pinned else ''}]]\n"
                        f"{b.content}\n\n")
        with open(self.context_jsonl_path, "w", encoding="utf-8") as f:
            for b in self.blocks:
                f.write(json.dumps(dataclasses.asdict(b)) + "\n")

    # -- top-level: one conversational turn ----------------------------------------
    def step(self, user_text: str) -> str:
        """Send a user message; returns the (tag-stripped) assistant reply.

        Side effects: eviction may run, files are mirrored, ``self.events`` grows.
        """
        self.turn += 1
        self.add("user", user_text)
        self.evict()                                  # room BEFORE generating
        raw = self.llm.chat(self.serialize_messages())
        if TAG_RE.search(raw) and self.cfg.classify_mode in ("inline", "both"):
            parts = split_tagged(raw)
            if any(tag != TRANSIENT for tag, _ in parts):
                # The model honoured the inline contract: mark its untagged
                # user message short-term instead of waiting for a router call.
                for b in self.blocks:
                    if (b.turn == self.turn and b.role == "user"
                            and b.tag == TRANSIENT and not b.pinned):
                        b.tag = SHORT
            for tag, chunk in parts:                 # classify as it decodes
                self.add("assistant", chunk, tag=tag)
        else:
            self.add("assistant", raw)
        self.classify_new()                           # post-turn router pass
        log = self.evict()                            # enforce budget after growth
        self.mirror_files()
        for line in log:
            print(f"  [evict] {line}")
        return "\n".join(strip_tags(b.content) for b in self.blocks
                         if b.role == "assistant" and b.turn == self.turn)

    # -- introspection ---------------------------------------------------------------
    def counts(self) -> Dict[str, int]:
        out = {LONG: 0, SHORT: 0, TRANSIENT: 0}
        for b in self.blocks:
            out[b.tag] += 1
        return out

    def status(self) -> str:
        n = self.counts()
        return (f"turn={self.turn} tokens={self.tokens()}/"
                f"{self.cfg.max_context_tokens} blocks={len(self.blocks)} "
                f"(long={n[LONG]} short={n[SHORT]} transient={n[TRANSIENT]})")
