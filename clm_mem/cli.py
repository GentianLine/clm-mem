"""Command-line interface: `clm-mem chat` and `clm-mem demo`."""
from __future__ import annotations
import argparse
import sys
import textwrap
from typing import List, Optional
from .blocks import LONG, SHORT
from .config import HarnessConfig
from .harness import MemoryHarness
from .llm import OpenAIChat, StubChat, SystemOneClient

HELP = ("commands:  /status   /long   /short   /transient   /recall <text>   "
        "/reinject <block-id>   /keep <pct>   /quit")


def chat_loop(h: MemoryHarness) -> None:
    print(HELP, flush=True)
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
        if text in ("/long", "/short", "/transient") or text.startswith(
                ("/long ", "/short ", "/transient ")):
            from .blocks import TRANSIENT
            want = {"/long": LONG, "/short": SHORT,
                    "/transient": TRANSIENT}[text.split()[0]]
            hits = [b for b in h.blocks if b.tag == want]
            for b in hits:
                print(f"  #{b.id} t{b.turn}: "
                      f"{textwrap.shorten(b.content, 120)}")
            if not hits:
                print(f"  (no live {want} blocks)")
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
        print("  ...working", flush=True)
        try:
            reply = h.step(text)
        except Exception as e:      # a backend hiccup must not kill the session
            print(f"ai> ERROR backend: {e}")
            continue
        print(f"ai> {reply}")
        print(f"    ({h.status()})", flush=True)


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
    sub.add_argument("--systemone-url", default="http://127.0.0.1:8010",
                     help="System One Lite API URL for typed decisions "
                          "(pre-classification + eviction backstop). "
                          "Set to 'none' to disable.")
    sub.add_argument("--evict-threshold", type=float, default=0.7,
                     help="SystemOne noul confidence needed to approve eviction "
                          "(higher = more conservative, keep more memory)")


def _cfg(a: argparse.Namespace) -> HarnessConfig:
    return HarnessConfig(
        max_context_tokens=a.max_context_tokens,
        shortterm_keep_pct=a.shortterm_keep_pct,
        keep_recent_turns=a.keep_recent_turns,
        classify_mode=a.classify_mode,
        workdir=a.workdir,
        tokenizer_name=a.tokenizer or a.model,
        systemone_evict_threshold=a.evict_threshold,
    )


def cmd_chat(a: argparse.Namespace) -> None:
    llm = OpenAIChat(base_url=a.base_url, model=a.model, api_key=a.api_key,
                     temperature=a.temperature, max_tokens=a.max_tokens)

    # Wire up SystemOne for typed decisions if configured
    systemone = None
    if a.systemone_url and a.systemone_url.lower() != "none":
        try:
            import urllib.request
            req = urllib.request.Request(a.systemone_url + "/v1/systemone",
                                         data=b'{"state":"test","questions":{"q":{"type":"noul","instructions":"test"}}}',
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            systemone = SystemOneClient(a.systemone_url)
            print(f"SystemOne decision API: {a.systemone_url} (OK)")
        except Exception as e:
            print(f"WARNING: SystemOne API unreachable at {a.systemone_url}: {e}")
            print("         Pre-classification and eviction backstop will be skipped.")

    h = MemoryHarness(llm, _cfg(a), systemone=systemone)
    print(f"clm-mem | {h.status()}\nbackend: {a.base_url} -> {a.model}")
    chat_loop(h)


def cmd_demo(a: argparse.Namespace) -> None:
    """Offline simulation with StubChat -- exercises all three eviction phases."""
    cfg = _cfg(a)
    cfg.max_context_tokens = min(cfg.max_context_tokens, 2048)

    # Use mock SystemOne to exercise typed decision code paths offline
    from .llm import MockSystemOne
    h = MemoryHarness(StubChat(), cfg, systemone=MockSystemOne())
    print("demo mode: using mock SystemOne (no HTTP calls)")
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
