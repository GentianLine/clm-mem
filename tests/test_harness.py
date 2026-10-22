"""Tests for the eviction engine and tag parsing (no GPU / server needed)."""

import json
import pytest
from clm_mem import (
    HarnessConfig,
    LONG,
    MemoryHarness,
    SHORT,
    StubChat,
    TRANSIENT,
    split_tagged,
)
from clm_mem.tagging import strip_tags


def make_harness(tmp_path, **kw) -> MemoryHarness:
    cfg = HarnessConfig(
        max_context_tokens=kw.pop("budget", 400),
        shortterm_keep_pct=kw.pop("keep", 0.5),
        keep_recent_turns=kw.pop("recent", 1),
        workdir=str(tmp_path),
        memory_preamble=kw.pop("preamble", False),
        **kw,
    )
    return MemoryHarness(StubChat(), cfg)


# -- tag parsing -----------------------------------------------------------
def test_split_tagged_basic():
    parts = split_tagged(
        "hi <longterm>key is K1</longterm> and "
        "<shortterm>todo: diff 3</shortterm> bye"
    )
    assert parts == [
        ("transient", "hi"),
        ("longterm", "key is K1"),
        ("transient", "and"),
        ("shortterm", "todo: diff 3"),
        ("transient", "bye"),
    ]


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
    for i in range(6):  # push over budget with chatter
        h.turn += 1
        h.add("user", f"{big} {i}")
    h.add(
        "assistant", "PORT=8080", tag=LONG
    )  # add() stores raw; tags are stripped by step()
    assert h.over_budget()
    log = h.evict()
    assert any(l["kind"] == "evict-phase1" for l in log)
    assert not [b for b in h.blocks if b.tag == TRANSIENT and b.turn < h.turn]
    assert any(b.content == "PORT=8080" for b in h.blocks)  # longterm survives


# -- phase 2: shortterm FIFO with keep-pct ------------------------------------
def test_phase2_fifo_rounds(tmp_path):
    h = make_harness(tmp_path, budget=300, keep=0.5)
    big = "x" * 400  # ~101 tokens per block
    ids = []
    for i in range(6):
        h.turn += 1
        ids.append(h.add("user", f"note {i} {big}", tag=SHORT).id)
    log = h.evict()
    assert any(l["kind"] == "evict-phase2" for l in log)
    live = [b.id for b in h.blocks if b.tag == SHORT]
    # oldest-first eviction: whatever survives must be the newest ids
    assert live == sorted(live, reverse=True)[: len(live)] or live == sorted(live)
    assert not set(ids[:2]) & set(live)  # oldest two are gone
    assert ids[-1] in live  # newest survives


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
    assert any(l["kind"] == "evict-phase3" for l in log)
    pointers = [b for b in h.blocks if "offloaded longterm" in b.content]
    assert pointers and all(b.tag == SHORT for b in pointers)
    archived = [json.loads(l) for l in open(h.archive_path, encoding="utf-8")]
    assert any("durable 0" in r["content"] for r in archived)
    hits = h.recall("durable 0")
    assert hits and h.reinject(hits[0]["id"])


# -- end-to-end -------------------------------------------------------------------
# --- SystemOne integration tests -------------------------------------------

from clm_mem.llm import MockSystemOne
from clm_mem.blocks import LONG, SHORT, TRANSIENT


def test_mock_systemone_classify_longterm():
    """MockSystemOne should classify factual content as longterm."""
    m = MockSystemOne()
    assert m.classify_memory("Remember: API key is KEY-123") == LONG
    assert m.classify_memory("The project codename is Aurora") == LONG


def test_mock_systemone_classify_transient():
    """MockSystemOne should classify short filler as transient."""
    m = MockSystemOne()
    assert m.classify_memory("How are you?") == TRANSIENT
    assert m.classify_memory("Thanks!") == TRANSIENT


def test_mock_systemone_classify_shortterm():
    """MockSystemOne should classify working state as shortterm."""
    m = MockSystemOne()
    assert (
        m.classify_memory(
            "I need to review the diff for PR #45 and check the build status"
        )
        == SHORT
    )


def test_mock_systemone_should_evict():
    """MockSystemOne should approve eviction of filler, reject facts."""
    m = MockSystemOne()
    assert m.should_evict("Hi there!") is True  # short filler
    assert m.should_evict("Remember: password is secret123") is False  # has fact


def test_systemone_preclassification_in_step(tmp_path):
    """User inputs should be pre-classified before being sent to LLM."""
    from clm_mem.harness import MemoryHarness
    from clm_mem.llm import StubChat

    h = MemoryHarness(StubChat(), systemone=MockSystemOne())
    h.cfg.workdir = str(tmp_path)

    # Longterm fact should be classified as longterm immediately
    h.step("Remember: project codename is Nova-1")
    user_blocks = [b for b in h.blocks if b.role == "user" and b.turn == 1]
    assert len(user_blocks) == 1
    assert user_blocks[0].tag == LONG

    # Check events show pre-classification happened
    assert any(
        e["kind"] == "classify" and "pre-classified user msg -> longterm" in e["text"]
        for e in h.events
    )


def test_systemone_preclassification_transient(tmp_path):
    """Small talk should be pre-classified as transient."""
    from clm_mem.harness import MemoryHarness
    from clm_mem.llm import StubChat

    h = MemoryHarness(StubChat(), systemone=MockSystemOne())
    h.cfg.workdir = str(tmp_path)

    h.step("How are you today?")
    user_blocks = [b for b in h.blocks if b.role == "user" and b.turn == 1]
    assert len(user_blocks) == 1
    assert user_blocks[0].tag == TRANSIENT


def test_batched_eviction_backstop(tmp_path):
    """Eviction backstops should batch SystemOne calls."""
    from clm_mem.harness import MemoryHarness
    from clm_mem.llm import StubChat, MockSystemOne

    h = MemoryHarness(StubChat(), systemone=MockSystemOne())
    h.cfg.workdir = str(tmp_path)
    h.cfg.max_context_tokens = 300  # force eviction quickly

    # Add several turns to trigger eviction
    for i in range(5):
        if i % 2 == 0:
            h.step(f"Remember: fact number {i}")
        else:
            h.step(f"Quick note {i}: filler")

    # Check that backstop events were logged
    backstop_events = [e for e in h.events if "backstop" in e["kind"]]
    assert len(backstop_events) > 0, "Expected eviction backstop events"


def test_phase3_backstop_keeps_longterm(tmp_path):
    """Phase 3 should consult SystemOne before offloading longterm memory."""
    from clm_mem.harness import MemoryHarness
    from clm_mem.llm import StubChat, MockSystemOne

    h = MemoryHarness(StubChat(), systemone=MockSystemOne())
    h.cfg.workdir = str(tmp_path)
    h.cfg.max_context_tokens = 200  # very small to force phase 3

    # Add longterm facts that should trigger phase 3 eviction
    for i in range(6):
        h.step(f"Remember: important fact {i} about port 80{i}")

    # Check that phase 3 backstop events exist
    phase3_events = [e for e in h.events if e["kind"] == "backstop-phase3"]
    assert len(phase3_events) > 0, "Expected phase 3 backstop events"


def test_evict_threshold_config():
    """systemone_evict_threshold should be configurable."""
    from clm_mem.config import HarnessConfig

    cfg = HarnessConfig(systemone_evict_threshold=0.9)
    assert cfg.systemone_evict_threshold == 0.9


def test_end_to_end_stays_under_budget(tmp_path):
    h = make_harness(tmp_path, budget=600, keep=0.5, preamble=True)
    for i in range(24):
        h.step(f"message {i}: remember codename Nova-{i}, port 80{i}")
        assert h.tokens() <= h.cfg.max_context_tokens
    # a pinned system block always survives
    assert h.blocks[0].pinned and h.blocks[0].tag == LONG
    # files mirrored as in the paper's context-as-a-file
    assert "CTX_TURN" in open(h.context_md_path, encoding="utf-8").read()
    json.loads(open(h.context_jsonl_path, encoding="utf-8").readline())  # valid JSONL
