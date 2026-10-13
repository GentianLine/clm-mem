"""Command-line interface: `clm-mem chat` and `clm-mem demo`."""
from __future__ import annotations
import argparse
import sys
import textwrap
from typing import List, Optional
from .blocks import LONG, SHORT
from .config import HarnessConfig
from .harness import MemoryHarness
from .llm import OpenAIChat, StubChat

HELP = ("commands:  /status   /long   /short   /recall <text>   "
        "/reinject <block-id>   /keep <pct>   /quit")


def chat_loop(h: MemoryHarness) -> None:
    print(HELP)
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/status":
            print(h.status())
            continue
        if text.startswith("/keep"):
            try:
                h.cfg.shortterm_keep_pct = float(text.split()[1])
            except (IndexError, ValueError):
                print(f"  current shortterm_keep_pct = "
                      f"{h.cfg.shortterm_keep_pct:.0%}  "
                      f"(usage: /keep 0.75)")
                continue
            print(f"  shortterm_keep_pct = {h.cfg.shortterm_keep_pct:.0%}")
            continue
        if text in ("/long", "/short"):
            want = LONG if text == "/long" else SHORT
            for b in h.blocks:
                if b.tag == want:
                    print(f"  #{b.id} t{b.turn}: "
                          f"{textwrap.shorten(b.content, 120)}")
            continue
        if text.startswith("/recall "):
            hits = h.recall(text[len("/recall "):])
            for rec in hits:
                print(f"  archive #{rec['id']} ({rec['tag']}, turn "
                      f"{rec['turn']}): {textwrap.shorten(rec['content'], 140)}")
            if not hits:
                print("  (no archive hits)")
            continue
        if text.startswith("/reinject "):
            try:
                bid = int(text.split()[1])
            except (IndexError, ValueError):
                print("  usage: /reinject <block-id>")
                continue
            print("  re-injected as shortterm" if h.reinject(bid)
                  else "  id not in archive")
            continue
        reply = h.step(text)
        print(f"ai> {reply}")
        print(f"    ({h.status()})")


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--base-url", default="http://localhost:8000/v1",
                     help="OpenAI-compatible endpoint (vLLM/Ollama/...)")
    sub.add_argument("--model", default="Qwen/Qwen3-8B-Instruct")
    sub.add_argument("--api-key", default="EMPTY")
    sub.add_argument("--temperature", type=float, default=0.6)
    sub.add_argument("--max-tokens", type=int, default=1024,
                     help="generation cap per reply")
    sub.add_argument("--max-context-tokens", type=int, default=8192,
                     help="live-window token budget for the harness")
    sub.add_argument("--shortterm-keep-pct", type=float, default=0.5,
                     help="fraction of shortterm KEPT per FIFO eviction round "
                          "(changeable mid-session with /keep <pct>)")
    sub.add_argument("--keep-recent-turns", type=int, default=2)
    sub.add_argument("--classify-mode", choices=["inline", "post", "both"],
                     default="both")
    sub.add_argument("--tokenizer", default="",
                     help="HF tokenizer id for exact token counts (optional)")
    sub.add_argument("--workdir", default=".",
                     help="where context.md / context.jsonl / archive.jsonl live")


def _cfg(a: argparse.Namespace) -> HarnessConfig:
    return HarnessConfig(
        max_context_tokens=a.max_context_tokens,
        shortterm_keep_pct=a.shortterm_keep_pct,
        keep_recent_turns=a.keep_recent_turns,
        classify_mode=a.classify_mode,
        workdir=a.workdir,
        tokenizer_name=a.tokenizer or a.model,
    )


def cmd_chat(a: argparse.Namespace) -> None:
    llm = OpenAIChat(base_url=a.base_url, model=a.model, api_key=a.api_key,
                     temperature=a.temperature, max_tokens=a.max_tokens)
    h = MemoryHarness(llm, _cfg(a))
    print(f"clm-mem | {h.status()}\nbackend: {a.base_url} -> {a.model}")
    chat_loop(h)


def cmd_demo(a: argparse.Namespace) -> None:
    """Offline simulation with StubChat -- exercises all three eviction phases."""
    cfg = _cfg(a)
    cfg.max_context_tokens = min(cfg.max_context_tokens, 2048)
    h = MemoryHarness(StubChat(), cfg)
    filler = ("Here is a longish status update with plenty of incidental detail "
              "that eats tokens while we work. " * 3)
    msgs = [f"Remember: project codename Nova-{i}, port 80{i}. {filler}"
            if i % 3 == 0 else f"Quick temp note {i}: {filler}"
            for i in range(24)]
    for m in msgs:
        h.step(m)
        print(h.status())
    print("\n-- final live context --")
    for b in h.blocks:
        print(f"  #{b.id:<3} {b.tag:<9} t{b.turn:<2} "
              f"{textwrap.shorten(b.content, 90)}")
    hits = h.recall("Nova")
    sample = textwrap.shorten(hits[-1]["content"], 80) if hits else ""
    print(f"\n/recall 'Nova' -> {len(hits)} archived hit(s); e.g. {sample}")


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(
        prog="clm-mem",
        description="Zero-training long/short-term memory harness for open "
                    "chat models (inspired by Context Language Models, "
                    "arXiv:2609.37725)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("chat", help="interactive chat with memory management")
    sd = sub.add_parser("demo", help="offline eviction simulation (no server needed)")
    for s in (sc, sd):
        _add_common(s)
    a = p.parse_args(argv)
    try:
        if a.cmd == "chat":
            cmd_chat(a)
        else:
            cmd_demo(a)
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
