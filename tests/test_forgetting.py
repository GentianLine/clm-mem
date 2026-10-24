"""Tests for the two forgetting strategies (decay & reheating, supersede)."""

import json
from clm_mem import HarnessConfig, LONG, MemoryHarness, SHORT, StubChat, TRANSIENT
from clm_mem.harness import BASE_IMPORTANCE


def make_harness(tmp_path, **kw) -> MemoryHarness:
    cfg = HarnessConfig(
        max_context_tokens=kw.pop("budget", 400),
        shortterm_keep_pct=kw.pop("keep", 0.5),
        keep_recent_turns=kw.pop("recent", 1),
        workdir=str(tmp_path / str(kw.pop("name", "d"))),
        memory_preamble=kw.pop("preamble", False),
        **kw,
    )
    return MemoryHarness(StubChat(), cfg)


def archived(h) -> list:
    if not h.archive_size():
        return []
    return [json.loads(l) for l in open(h.archive_path, encoding="utf-8")]


# -- strategy 1: decay & reheating --------------------------------------------
def test_decay_demotes_stale_longterm(tmp_path):
    h = make_harness(tmp_path, budget=2000, decay_enabled=True, name="dem")
    b = h.add("assistant", "Remember: project codename Nova-1.", tag=LONG)
    h.turn += 12  # ~1.5 half-lives without access
    log = h.evict()
    assert b.tag == SHORT  # demoted out of the long tier
    assert any(e["kind"] == "maintain-decay" for e in log)
    assert h.archive_size() == 0  # demotion never destroys


def test_reheat_prevents_demotion(tmp_path):
    h = make_harness(tmp_path, budget=2000, decay_enabled=True, name="reh")
    b = h.add("assistant", "Remember: project codename Nova-1.", tag=LONG)
    h.turn += 3  # created at t=0, now t=3
    n = h.reheat("did we settle on nova-1 for the build?")
    assert n == 1
    h.turn += 7  # last access t=3 -> gap 7 < demote
    log = h.evict()
    eff = h.eff_importance(b)
    assert b.tag == LONG and eff >= h.cfg.demote_threshold
    assert not [e for e in log if e["kind"] == "maintain-decay"]


def test_reheat_scope_global_hot_everywhere(tmp_path):
    """Default: a fact touched in ANY chat warms up project-wide."""
    h = make_harness(
        tmp_path, budget=2000, decay_enabled=True, name="rglob"
    )  # reheat_scope defaults to 'global'
    b = h.add("assistant", "Remember: project codename Nova-1.", tag=LONG)
    b.importance = 0.5  # cool it so the global lift is observable
    h.turn += 5
    n = h.reheat("what is our codename? nova-1 confirmed")
    assert n == 1 and b.last_access == h.turn
    assert b.importance > 0.6, b.importance


def test_reheat_scope_chat_own_lifts_foreign_fresh(tmp_path):
    """Chat-scoped: own chat lifts, other chats only keep the fact fresh.

    Global hot stays global *hot* -- a lift inside the owning chat raises
    importance and holds everywhere (freshness = max of both channels).
    A cross-chat touch under 'chat' scope refreshes freshness but never
    lifts value: it can stop decay, not resurrect.
    """
    h = make_harness(
        tmp_path,
        budget=2000,
        decay_enabled=True,
        reheat_scope="chat",
        name="rchat",
    )
    # chat-001 (slot 0) owns a longterm fact; cool its stored value first so
    # lifts are observable (importance caps at 1.0 when created fresh)
    h.slot = 0
    b = h.add("assistant", "Remember: project codename Nova-1.", tag=LONG)
    b.importance = 0.5
    h.turn += 24  # both freshness channels seeded at turn 0: eff ~0.35
    cooled = h.eff_importance(b)
    assert cooled < 0.36, cooled

    # chat-002 (slot 1) mentions the identifier: freshness only, no lift
    h.slot = 1
    n = h.reheat("did nova-1 ship in chat two?")
    assert n == 0 and b.importance == 0.5
    assert b.last_seen == h.turn and b.last_access < h.turn
    # freshness holds: the decay penalty lifts, but the value never rises
    eff_fresh = h.eff_importance(b)
    assert abs(eff_fresh - 0.5) < 1e-9, eff_fresh

    # own-chat access lifts: stored importance itself rises and holds
    h.slot = 0
    n = h.reheat("nova-1 revisited in chat one")
    assert n == 1 and b.last_access == h.turn
    assert b.importance > 0.5 + 0.14, b.importance
    eff_own = h.eff_importance(b)
    assert eff_own > eff_fresh, (eff_own, eff_fresh)


def test_reheat_scope_chat_cannot_resurrect(tmp_path):
    """A cold fact stays cold: cross-chat touches don't lift it back."""
    h = make_harness(
        tmp_path,
        budget=2000,
        decay_enabled=True,
        reheat_scope="chat",
        name="rcold",
    )
    h.slot = 0
    b = h.add("assistant", "Remember: project codename Nova-1.", tag=LONG)
    imp0 = b.importance
    # demote it into staleness without any access (freshness channels must
    # both go stale: last_access and last_seen were seeded at creation)
    h.turn += 80
    log = h.evict()
    assert b.tag == SHORT  # decay demoted it out of long tier
    assert not [e for e in log if e["kind"] == "maintain-reheat"]

    # another chat touches the identifier repeatedly: stays demoted, no lift
    h.slot = 1
    for _ in range(3):
        n = h.reheat("nova-1 nova-1 nova-1")
        assert n == 0
    assert b.importance <= imp0 + 1e-9
    assert b.tag == SHORT  # cross-chat touches cannot resurrect


def test_reheat_scope_fresh_global_hot_fresh_never_lifts(tmp_path):
    """'fresh': global hot & fresh, no reheat -- not even in the current chat.

    Every touch (own or foreign) refreshes BOTH freshness channels so the
    fact never goes stale while any chat uses it, but access alone never
    raises importance anywhere: the current session gets no lift either.
    """
    h = make_harness(
        tmp_path,
        budget=2000,
        decay_enabled=True,
        reheat_scope="fresh",
        name="rfresh",
    )
    h.slot = 0
    b = h.add("assistant", "Remember: project codename Nova-1.", tag=LONG)
    b.importance = 0.5  # cool it so any lift would be observable
    h.turn += 24  # both channels seeded at turn 0: eff ~0.35 (stale)
    cooled = h.eff_importance(b)
    assert cooled < 0.36, cooled

    # foreign chat touches: freshness lifts globally, value never rises
    h.slot = 1
    n = h.reheat("did nova-1 ship in chat two?")
    assert n == 1
    assert b.importance == 0.5  # no reheat for anyone
    assert b.last_access == h.turn and b.last_seen == h.turn  # both channels
    eff_fresh = h.eff_importance(b)
    assert abs(eff_fresh - 0.5) < 1e-9, eff_fresh  # decay fully held off

    # own chat touches, a few turns later: same rule -- no reheat for the
    # current session either, only freshness moves
    h.turn += 3
    h.slot = 0
    n = h.reheat("nova-1 revisited in chat one")
    assert n == 1 and b.importance == 0.5
    assert max(b.last_access, b.last_seen) == h.turn

    # a demoted fact stays demoted however often it is touched: fresh mode
    # can stop the decay from advancing but cannot resurrect value.
    h.turn += 80  # long silence -> decay demotes on next maintenance
    log = h.evict()
    assert b.tag == SHORT
    imp_after_demote = b.importance  # retag re-seeded it to the short base
    n = h.reheat("nova-1 nova-1")
    assert n == 1  # the touch still registers as fresh
    assert b.last_seen == h.turn and b.last_access == h.turn
    assert b.importance == imp_after_demote  # access never lifts a notch
    assert b.tag == SHORT  # freshness holds; resurrection is not on offer


def test_decay_off_by_default(tmp_path):
    h = make_harness(tmp_path, budget=2000, name="off")
    b = h.add("assistant", "Remember: project codename Nova-1.", tag=LONG)
    h.turn += 40  # ancient by any curve
    log = h.evict()
    assert b.tag == LONG  # untouched without the toggle
    assert not [e for e in log if e["kind"].startswith("maintain")]


def test_phase3_ranks_by_value_when_decay_on(tmp_path):
    """With decay on, phase 3 offloads the weakest long fact first -- even if
    it is the *newest* by id (strict FIFO would keep that one)."""
    h = make_harness(
        tmp_path,
        budget=300,
        decay_enabled=True,
        decay_half_life=40,
        demote_threshold=0.05,
        name="rank",
    )
    big = "y" * 500
    old = h.add("user", f"durable old {big}", tag=LONG)  # id low, fresh-ish
    h.turn += 1
    new = h.add("user", f"durable new {big}", tag=LONG)  # id high...
    # stale on BOTH freshness channels (eff_importance takes the best of
    # access and seen; one warm channel would hold the fact alive)
    new.last_access = h.turn - 20
    new.last_seen = h.turn - 20
    assert old.id < new.id
    log = h.evict()
    assert any(e["kind"] == "evict-phase3" for e in log)
    arch = archived(h)
    long_offloads = [r for r in arch if r["tag"] == LONG or "durable" in r["content"]]
    assert long_offloads[0]["id"] == new.id  # weakest eff importance first


# -- strategy 2: supersede / dedup ---------------------------------------------
def test_supersede_update_demotes_old(tmp_path):
    h = make_harness(tmp_path, budget=2000, supersede_enabled=True, name="sup")
    old = h.add("user", "Remember: project codename Nova-1.", tag=LONG)
    h.turn += 1
    new = h.add("assistant", "codename Nova-2 is current now.", tag=LONG)
    log = h.evict()
    assert any(e["kind"] == "maintain-supersede" for e in log)
    assert old.tag == SHORT and new.tag == LONG
    assert old.content.startswith(f"[superseded by #{new.id}]")


def test_supersede_dedup_drops_duplicate_write(tmp_path):
    h = make_harness(tmp_path, budget=2000, supersede_enabled=True, name="dup")
    old = h.add("user", "Remember: project codename Nova-1.", tag=LONG)
    h.turn += 1
    dup = h.add("assistant", "as discussed, codename Nova-1 confirmed.", tag=LONG)
    log = h.evict()
    assert any(e["kind"] == "maintain-dedup" for e in log)
    live_ids = {b.id for b in h.blocks}
    assert dup.id not in live_ids  # copy never admitted...
    assert old.tag == LONG  # ...original survives
    arch = archived(h)  # but is still recoverable
    assert any(r["id"] == dup.id for r in arch)
    assert h.recall("Nova-1 confirmed")


def test_partial_slot_match_is_update_not_duplicate(tmp_path):
    """Same codename but a *changed* port is an update, not a duplicate:
    the new fact must survive and the old one must be superseded."""
    h = make_harness(tmp_path, budget=2000, supersede_enabled=True, name="part")
    old = h.add("user", "Remember: codename Nova-1 port 801.", tag=LONG)
    h.turn += 1
    new = h.add("assistant", "codename Nova-1 port is now 999.", tag=LONG)
    log = h.evict()
    assert not [e for e in log if e["kind"] == "maintain-dedup"]
    assert any(e["kind"] == "maintain-supersede" for e in log)
    assert new.id in {b.id for b in h.blocks}  # update admitted
    assert old.tag == SHORT  # and old superseded


def test_supersede_off_by_default(tmp_path):
    h = make_harness(tmp_path, budget=2000, name="soff")
    old = h.add("user", "Remember: project codename Nova-1.", tag=LONG)
    h.turn += 1
    new = h.add("assistant", "codename Nova-2 is current now.", tag=LONG)
    log = h.evict()
    assert old.tag == LONG and new.tag == LONG  # both kept, no adjudication
    assert not [e for e in log if e["kind"].startswith("maintain")]


def test_supersede_ignores_unrelated_slots(tmp_path):
    h = make_harness(tmp_path, budget=2000, supersede_enabled=True, name="mix")
    a = h.add("user", "Remember: project codename Nova-1.", tag=LONG)
    h.turn += 1
    b = h.add("user", "The port is 8012 and the api_key sk-42 works.", tag=LONG)
    h.evict()
    assert a.tag == LONG and b.tag == LONG  # no shared subject slots


# -- combined behaviour ---------------------------------------------------------
def test_toggles_combine_under_pressure(tmp_path):
    """Decay + supersede together: stale facts demote, updates overwrite,
    duplicates drop, budget invariant still holds end-to-end via step()."""
    h = make_harness(
        tmp_path, budget=900, decay_enabled=True, supersede_enabled=True, name="combo"
    )
    for i in range(30):
        msg = (
            f"Remember: project codename Nova-{i}, port {80 + i}."
            if i % 5 == 1
            else f"Quick temp note {i}: filler chatter " * 8
        )
        h.step(msg)
    assert not h.over_budget()
    kinds = {e["kind"] for e in h.events}
    assert {"maintain-supersede", "maintain-decay"} <= kinds
    # nothing destroyed: every long fact is live or archived
    import re as _re

    NUM = _re.compile(r"codename Nova-(\d+)")

    def nums(text: str) -> set:
        return {int(m) for m in NUM.findall(text)}

    live_facts, arch_facts = set(), set()
    for b in h.blocks:
        live_facts |= nums(b.content)
    for r in archived(h):
        arch_facts |= nums(r["content"])
    assert live_facts | arch_facts, "facts must survive somewhere"
    # supersession keeps exactly one current codename per slot in the long tier
    cur = {
        int(_re.search(r"codename Nova-(\d+)", b.content).group(1))
        for b in h.blocks
        if b.tag == LONG
        and "codename Nova-" in b.content
        and not b.content.startswith("[")
    }
    assert len(cur) <= 1
