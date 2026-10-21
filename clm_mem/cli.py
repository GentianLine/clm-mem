"""Command-line interface: clm-mem chat / demo / serve."""
from __future__ import annotations
import argparse
import sys
import textwrap
from typing import List, Optional
from .blocks import LONG, SHORT
from .config import DEFAULT_CTX_BUDGET, HarnessConfig
from .harness import MemoryHarness
from .llm import OpenAIChat, StubChat, SystemOneClient

HELP = ("commands:  /status   /long   /short   /transient   /recall <text>   "
        "/reinject <block-id>   /keep <pct>   /decay [on|off]   "
        "/supersede [on|off]   /quit")


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
        if text.split(maxsplit=1)[0] in ("/decay", "/supersede"):
            attr = {"/decay": "decay_enabled",
                    "/supersede": "supersede_enabled"}[text.split()[0]]
            parts = text.split()
            if len(parts) == 1:      # bare command: toggle
                setattr(h.cfg, attr, not getattr(h.cfg, attr))
            elif parts[1].lower() in ("on", "true", "1", "yes"):
                setattr(h.cfg, attr, True)
            elif parts[1].lower() in ("off", "false", "0", "no"):
                setattr(h.cfg, attr, False)
            else:
                print("  usage: /decay [on|off] or /supersede [on|off]")
                continue
            print(f"  {attr} = {getattr(h.cfg, attr)}")
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
            hits = h.live_blocks(want)
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


def _add_harness_flags(sub: argparse.ArgumentParser) -> None:
    """Harness knobs shared by chat / demo / serve (no endpoint args)."""
    sub.add_argument("--temperature", type=float, default=0.6)
    sub.add_argument("--max-tokens", type=int, default=1024,
                     help="generation cap per reply")
    sub.add_argument("--max-context-tokens", type=int, default=None,
                     help="live-window token budget for the harness; when "
                          "unset it follows the backend's own context window "
                          "(read from /props) so llama.cpp and clm-mem never "
                          "drift apart")
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
    sub.add_argument("--evict-threshold", type=float, default=None,
                     help="SystemOne noul confidence needed to approve eviction "
                          "(higher = more conservative, keep more memory). "
                          "Default 0.7, or 0.55 under --use-mock so the "
                          "bundled mock decisions visibly evict")
    # --- forgetting strategies (toggles; default off) -----------------------
    sub.add_argument("--decay", dest="decay", action="store_true",
                     default=False,
                     help="enable decay & reheating: long-term facts lose value "
                          "on a forgetting curve unless accessed, demote to "
                          "short-term below the floor, and phase-3 offload "
                          "ranks by value instead of age")
    sub.add_argument("--no-decay", dest="decay", action="store_false",
                     help="disable decay & reheating (default)")
    sub.add_argument("--decay-half-life", type=int, default=8,
                     help="turns for importance to halve without access")
    sub.add_argument("--demote-threshold", type=float, default=0.45,
                     help="effective importance below which a long-term block "
                          "becomes short-term (working state again)")
    sub.add_argument("--supersede", dest="supersede", action="store_true",
                     default=False,
                     help="enable supersession: a new long-term fact that "
                          "shares a subject slot (codename/port/id/...) with "
                          "an older one supersedes it; exact duplicates are "
                          "dropped at write time instead of admitted twice")
    sub.add_argument("--no-supersede", dest="supersede", action="store_false",
                     help="disable supersession (default)")


def _add_common(sub: argparse.ArgumentParser) -> None:
    """Backend endpoint args + harness flags (chat / demo)."""
    sub.add_argument("--base-url", default="http://localhost:8000/v1",
                     help="OpenAI-compatible endpoint (vLLM/Ollama/...)")
    sub.add_argument("--model", default="Qwen/Qwen3-8B-Instruct")
    sub.add_argument("--api-key", "--backend-api-key",
                     help="bearer token for the chat backend's API (any "
                          "OpenAI-compatible server; local llama.cpp/vLLM/"
                          "Ollama do not check it, hosted services like "
                          "Featherless or OpenRouter require one)")
    _add_harness_flags(sub)
    sub.add_argument("--systemone-url", default="http://127.0.0.1:8010",
                     help="System One Lite API URL for typed decisions "
                          "(pre-classification + eviction backstop). "
                          "Set to 'none' to disable.")
    sub.add_argument("--systemone-model", default="",
                     help="model id sent to the decision API (required; both "
                          "the llama.cpp fork and Featherless reject bodies "
                          "without it). e.g. featherless-ai/"
                          "Qwen3.6-35B-A3B-classifier")
    sub.add_argument("--systemone-api-key", default="",
                     help="bearer token for the decision API (defaults to "
                          "$FEATHERLESS_API_KEY)")
    sub.add_argument("--connect", default="",
                     help="attach to a running `clm-mem serve` at this base "
                          "URL (e.g. http://127.0.0.1:8080/v1) instead of "
                          "talking to a backend directly")
    sub.add_argument("--workspace", default="",
                     help="workspace/project for --connect sessions "
                          "(session becomes ws:<name>)")
    sub.add_argument("--session", default="",
                     help="explicit session id for --connect "
                          "(overrides ws:<workspace> naming)")


def _cfg(a: argparse.Namespace,
         fallback_model: Optional[str] = None) -> HarnessConfig:
    fb = fallback_model if fallback_model is not None \
        else getattr(a, "model", "")
    thr = a.evict_threshold if a.evict_threshold is not None else (
        0.55 if getattr(a, "use_mock", False) else 0.7)
    return HarnessConfig(
        max_context_tokens=a.max_context_tokens or DEFAULT_CTX_BUDGET,
        shortterm_keep_pct=a.shortterm_keep_pct,
        keep_recent_turns=a.keep_recent_turns,
        classify_mode=a.classify_mode,
        workdir=a.workdir,
        tokenizer_name=a.tokenizer or fb,
        systemone_evict_threshold=thr,
        decay_enabled=a.decay,
        decay_half_life=a.decay_half_life,
        demote_threshold=a.demote_threshold,
        supersede_enabled=a.supersede,
    )


def cmd_chat(a: argparse.Namespace) -> None:
    if getattr(a, "connect", ""):
        # Attach this prompt to a running `clm-mem serve` (same library the
        # web studio uses; sessions are shared via X-Cllm-Mem-Session).
        from .remote import connect as open_remote
        h = open_remote(a.connect,
                        session=getattr(a, "session", "") or None,
                        workspace=getattr(a, "workspace", "") or None)
        try:
            status = h.status()
        except Exception as exc:
            sys.exit(f"error: cannot attach to {a.connect}: {exc}")
        ui = h.ui_url()
        h.prime()
        print(f"clm-mem | attached to serve @ {a.connect} "
              f"(session {h.session})")
        if ui:
            print(f"web studio: {ui}")
        print(status)
        chat_loop(h)
        return

    if a.max_context_tokens is None:
        # follow the served window: llama.cpp fixes its context at load time,
        # so mirror it instead of letting the two budgets drift.
        from .server import discover_backend_ctx, normalize_root
        ctx = discover_backend_ctx(normalize_root(a.base_url),
                                    api_key=a.api_key)
        a.max_context_tokens = ctx or DEFAULT_CTX_BUDGET
        print(f"context window: {a.max_context_tokens} tokens "
              f"(from backend)" if ctx else
              f"context window: {a.max_context_tokens} tokens (default)")

    llm = OpenAIChat(base_url=a.base_url, model=a.model, api_key=a.api_key,
                     temperature=a.temperature, max_tokens=a.max_tokens)

    # Wire up SystemOne for typed decisions if configured. The probe goes
    # through the same client (and therefore the same endpoint aliases and
    # required "model" field) the harness will use later -- a raw urllib ping
    # with no model always fails against both the fork and Featherless.
    systemone = None
    if a.systemone_url and a.systemone_url.lower() != "none":
        systemone = SystemOneClient(a.systemone_url,
                                    model=a.systemone_model,
                                    api_key=getattr(a, "systemone_api_key", ""))
        try:
            probe = systemone.system_one(
                "test",
                {"q": {"type": "noul", "instructions": "is this a test?"}})
            if "answers" not in probe:
                raise RuntimeError(f"unexpected reply: {probe}")
            print(f"SystemOne decision API: {a.systemone_url} (OK, "
                  f"model={systemone.model}, endpoint={systemone._path})")
        except Exception as e:
            print(f"WARNING: SystemOne API unreachable at {a.systemone_url}: {e}")
            print("         Pre-classification and eviction backstop will be skipped.")
            systemone = None

    h = MemoryHarness(llm, _cfg(a), systemone=systemone)
    print(f"clm-mem | {h.status()}\nbackend: {a.base_url} -> {a.model}")
    chat_loop(h)


def cmd_demo(a: argparse.Namespace) -> None:
    """Offline simulation with StubChat -- exercises all three eviction phases."""
    a.use_mock = True                    # mock-aware default evict threshold
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


def _add_serve_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--host", dest="public_host", default="127.0.0.1",
                     help="address the public llama.cpp-compatible endpoint binds")
    sub.add_argument("--port", dest="public_port", type=int, default=8080)
    sub.add_argument("--served-model", default="clm-mem-model",
                     help="model id advertised to clients")
    sub.add_argument("--use-mock", action="store_true",
                     help="run the bundled mock backend + mock SystemOne "
                          "(no GPU, no binaries needed)")
    sub.add_argument("--llama-server", dest="llama_server_bin", default="",
                     help="llama-server binary to spawn behind the proxy "
                          "(simple form: just the path; see --backend-cmd for "
                          "passing the full command with options)")
    sub.add_argument("--model-file", dest="model_path", default="",
                     help="GGUF/weights path passed to llama-server --model")
    sub.add_argument("--ctx-size", type=int, default=0,
                     help="context size passed to llama-server (optional)")
    sub.add_argument("--backend-cmd", default="",
                     help="complete backend command line including all options, "
                          "e.g. \"llama-server -m qwen.gguf --n-gpu-layers 99 "
                          "--ctx-size 16384\". Use {host} / {port} placeholders "
                          "if the server needs them spelled your way; otherwise "
                          "--host/--port are appended. Replaces "
                          "--llama-server/--model-file/--ctx-size when given.")
    sub.add_argument("--backend-url", default="",
                     help="root (or /v1) URL of an already-running server "
                          "to proxy instead of spawning one")
    sub.add_argument("--backend-api-key", default="",
                     help="bearer token sent to the chat backend on every LLM "
                          "call it fronts (needed for hosted OpenAI-compatible "
                          "backends, e.g. --backend-url https://api.featherless.ai "
                          "--backend-api-key KEY). Local servers ignore it; "
                          "when unset the proxy relays any Authorization "
                          "header clients send")
    sub.add_argument("--systemone-binary", dest="systemone_bin", default="",
                     help="SystemOne fork/server binary to spawn behind the proxy")
    sub.add_argument("--systemone-cmd", default="",
                     help="complete SystemOne server command line including all "
                          "options (placeholders {host}/{port} supported); "
                          "replaces --systemone-binary when given.")
    sub.add_argument("--systemone-url", default="",
                     help="URL of an already-running System One API "
                          "(local fork or https://api.featherless.ai)")
    sub.add_argument("--systemone-model", default="",
                     help="model id sent to the decision API; required by both "
                          "the llama.cpp fork and Featherless (default: a bare "
                          "name the fork accepts, set the real id for hosted)")
    sub.add_argument("--systemone-api-key", default="",
                     help="bearer token for the decision API (defaults to "
                          "$FEATHERLESS_API_KEY)")
    sub.add_argument("--systemone-passthrough", action="store_true",
                     help="the backend itself answers /v1/systemone "
                          "(SystemOne fork build)")
    sub.add_argument("--log-dir", default="",
                     help="where backend process logs go (default: tmpdir)")
    sub.add_argument("--json", action="store_true",
                     help="print only the machine-readable READY line")
    sub.add_argument("--no-ui", dest="ui", action="store_false",
                     help="do not host the bundled web studio at the root URL")
    _add_harness_flags(sub)


def cmd_serve(a: argparse.Namespace) -> None:
    """One llama.cpp-shaped endpoint in front, 2-3 processes behind."""
    import dataclasses as _dc
    import json as _json
    import time as _time
    from .server import start_supervisor

    harness_kwargs = _dc.asdict(_cfg(a, ""))
    if a.max_context_tokens is None:
        # not pinned on the CLI -> supervisor sets it from the backend's ctx
        harness_kwargs["max_context_tokens"] = None

    if a.use_mock:
        backend_mode = "mock"
    elif a.backend_url:
        backend_mode = "external"
    elif a.llama_server_bin or getattr(a, "backend_cmd", ""):
        backend_mode = "spawn"
    else:
        sys.exit("error: pick a backend: --use-mock, --backend-url URL, "
                 "--llama-server PATH or --backend-cmd 'FULL COMMAND'")

    backend_cmd = None
    if getattr(a, "backend_cmd", ""):
        # full command line including options, passed through verbatim
        import shlex
        backend_mode = "spawn"
        backend_cmd = shlex.split(a.backend_cmd)
    elif backend_mode == "spawn":
        backend_cmd = [a.llama_server_bin]
        if a.model_path:
            backend_cmd += ["--model", a.model_path]
        if a.ctx_size:
            backend_cmd += ["--ctx-size", str(a.ctx_size)]

    sys_cmd = None
    if a.systemone_passthrough:
        sys_mode = "passthrough"
    elif getattr(a, "systemone_cmd", ""):
        import shlex
        sys_mode, sys_cmd = "spawn", shlex.split(a.systemone_cmd)
    elif a.use_mock:
        sys_mode = "mock"
    elif a.systemone_bin:
        sys_mode, sys_cmd = "spawn", [a.systemone_bin]
    elif a.systemone_url:
        sys_mode = "external"
    else:
        sys_mode = "none"

    sup = start_supervisor(
        public_host=a.public_host, public_port=a.public_port,
        served_model=a.served_model,
        backend_mode=backend_mode, backend_cmd=backend_cmd,
        backend_url=a.backend_url or None,
        backend_api_key=a.backend_api_key,
        systemone_mode=sys_mode,
        systemone_cmd=sys_cmd if sys_mode == "spawn" else None,
        systemone_url=a.systemone_url or None,
        systemone_model=getattr(a, "systemone_model", ""),
        systemone_api_key=getattr(a, "systemone_api_key", ""),
        harness_kwargs=harness_kwargs,
        chat_temperature=a.temperature, chat_max_tokens=a.max_tokens,
        log_dir=a.log_dir or None, ui=a.ui)

    procs = {h.name: f"{h.host}:{h.port}"
             for h in sup.manager.handles if h.alive()}
    ready = {"ready": True, "base_url": sup.public_base,
             "root_url": sup.base_url, "model": a.served_model,
             "backend": sup.config.backend_root,
             "systemone": sup.config.systemone_root,
             "backend_ctx": sup.config.backend_ctx,
             "budget": sup.config.harness_kwargs.get("max_context_tokens"),
             "ui_url": sup.ui_url, "mcp_url": f"{sup.base_url}/mcp"
             if sup.ui_url else None,
             "processes": procs}
    if not a.json:
        print(f"clm-mem serve | llama.cpp-compatible drop-in at {sup.public_base}")
        print(f"  advertised model : {a.served_model}")
        print(f"  chat backend     : {sup.config.backend_root or '(none)'}")
        print(f"  context budget   : "
              f"{sup.config.harness_kwargs.get('max_context_tokens')} tokens "
              f"(llama.cpp window: {sup.config.backend_ctx or 'unknown'})")
        print(f"  systemone api    : {sup.config.systemone_root or '(none)'}")
        if sup.ui_url:
            print(f"  web studio       : {sup.ui_url}  (MCP tools: {sup.base_url}/mcp)")
        print("  memory sessions  : send header "
              f"X-Cllm-Mem-Session: <id> on chat requests")
    print(_json.dumps(ready), flush=True)
    try:
        while True:
            _time.sleep(3600)
    except KeyboardInterrupt:                          # pragma: no cover
        print("\nshutting down...", flush=True)
    finally:
        sup.shutdown()


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(
        prog="clm-mem",
        description="Zero-training long/short-term memory harness for open "
                    "chat models (inspired by Context Language Models, "
                    "arXiv:2609.37725)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("chat", help="interactive chat with memory management")
    sd = sub.add_parser("demo", help="offline eviction simulation (no server needed)")
    ss = sub.add_parser("serve", help="llama.cpp-compatible drop-in server "
                                      "(proxy + backend + optional SystemOne)")
    for s in (sc, sd):
        _add_common(s)
    _add_serve_flags(ss)
    a = p.parse_args(argv)
    try:
        if a.cmd == "chat":
            cmd_chat(a)
        elif a.cmd == "serve":
            cmd_serve(a)
        else:
            cmd_demo(a)
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
