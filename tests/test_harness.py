"""Tests for the eviction engine and tag parsing (no GPU / server needed)."""
import json
import pytest
from clm_mem import (HarnessConfig, LONG, MemoryHarness, SHORT, StubChat,
                     TRANSIENT, split_tagged)
from clm_mem.tagging import strip_tags


def make_harness(tmp_path, **kw) -> MemoryHarness:
    cfg = HarnessConfig(max_context_tokens=kw.pop("budget", 400),
                        shortterm_keep_pct=kw.pop("keep", 0.5),
                        keep_recent_turns=kw.pop("recent", 1),
                        workdir=str(tmp_path),
                        memory_preamble=kw.pop("preamble", False),
                        **kw)
    return MemoryHarness(StubChat(), cfg)


# -- tag parsing -----------------------------------------------------------
def test_split_tagged_basic():
    parts = split_tagged(
        "hi <longterm>key is K1</longterm> and "
        "<shortterm>todo: diff 3</shortterm> bye")
    assert parts == [("transient", "hi"), ("longterm", "key is K1"),
                     ("transient", "and"), ("shortterm", "todo: diff 3"),
                     ("transient", "bye")]


def test_split_tagged_none():
    assert split_tagged("no tags here") == [("transient", "no tags here")]


def test_strip_tags():
    assert strip_tags("a <longterm>b</longterm>") == "a b"


# -- config ------------------------------------------------------------------
def test_keep_pct_range_rejected():
    for bad in (-0.1, 1.0, 2.0):
        with pytest.raises(ValueError):
            HarnessConfig(shortterm_keep_pct=bad)


# -- phase 1: transient deletion ------------------------------------------------
def test_phase1_deletes_transient_keeps_memory(tmp_path):
    h = make_harness(tmp_path, budget=400, recent=1)
    big = "filler text that is quite long and eats up the token budget " * 5
    for i in range(6):                       # push over budget with chatter
        h.turn += 1
        h.add("user", f"{big} {i}")
    h.add("assistant", "PORT=8080", tag=LONG)   # add() stores raw; tags are stripped by step()
    assert h.over_budget()
    log = h.evict()
    assert any("phase1" in l for l in log)
    assert not [b for b in h.blocks if b.tag == TRANSIENT and b.turn < h.turn]
    assert any(b.content == "PORT=8080" for b in h.blocks)   # longterm survives


# -- phase 2: shortterm FIFO with keep-pct ------------------------------------
def test_phase2_fifo_rounds(tmp_path):
    h = make_harness(tmp_path, budget=300, keep=0.5)
    big = "x" * 400                          # ~101 tokens per block
    ids = []
    for i in range(6):
        h.turn += 1
        ids.append(h.add("user", f"note {i} {big}", tag=SHORT).id)
    log = h.evict()
    assert any("phase2" in l for l in log)
    live = [b.id for b in h.blocks if b.tag == SHORT]
    # oldest-first eviction: whatever survives must be the newest ids
    assert live == sorted(live, reverse=True)[: len(live)] or live == sorted(live)
    assert not set(ids[:2]) & set(live)      # oldest two are gone
    assert ids[-1] in live                   # newest survives


def test_phase2_higher_keep_deletes_fewer(tmp_path):
    def survivors(keep):
        h = make_harness(tmp_path / f"k{keep}", budget=300, keep=keep)
        big = "x" * 400
        for i in range(6):
            h.turn += 1
            h.add("user", f"note {i} {big}", tag=SHORT)
        h.evict()
        return len([b for b in h.blocks if b.tag == SHORT])
    assert survivors(0.75) >= survivors(0.25)


# -- phase 3: longterm offload ---------------------------------------------------
def test_phase3_offloads_longterm_with_pointer(tmp_path):
    h = make_harness(tmp_path, budget=250, keep=0.5)
    big = "y" * 500
    for i in range(5):
        h.turn += 1
        h.add("user", f"durable {i} {big}", tag=LONG)
    log = h.evict()
    assert any("phase3" in l for l in log)
    pointers = [b for b in h.blocks if "offloaded longterm" in b.content]
    assert pointers and all(b.tag == SHORT for b in pointers)
    archived = [json.loads(l) for l in
                open(h.archive_path, encoding="utf-8")]
    assert any("durable 0" in r["content"] for r in archived)
    hits = h.recall("durable 0")
    assert hits and h.reinject(hits[0]["id"])


# -- end-to-end -------------------------------------------------------------------
def test_end_to_end_stays_under_budget(tmp_path):
    h = make_harness(tmp_path, budget=600, keep=0.5, preamble=True)
    for i in range(24):
        h.step(f"message {i}: remember codename Nova-{i}, port 80{i}")
        assert h.tokens() <= h.cfg.max_context_tokens
    # a pinned system block always survives
    assert h.blocks[0].pinned and h.blocks[0].tag == LONG
    # files mirrored as in the paper's context-as-a-file
    assert "CTX_TURN" in open(h.context_md_path, encoding="utf-8").read()
    json.loads(open(h.context_jsonl_path, encoding="utf-8")
               .readline())  # valid JSONL
