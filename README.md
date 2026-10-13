# clm-mem

A **zero-training memory harness** for open chat models (Qwen and friends).
The same model that reads and writes text also classifies its own context as
**long-term** / **short-term** memory while decoding; a small deterministic
eviction engine then enforces the token budget.

Design inspired by *Context Language Models* (Shao et al., arXiv:2609.37725):
context is treated as an editable artifact mirrored to disk, and context
management is delegated to the model via prompting — **no fine-tuning**,
works off-the-shelf with any OpenAI-compatible server (vLLM, Ollama,
llama.cpp, LM Studio).

## How it works

Every message lives in the live window as a `Block` tagged
`longterm`, `shortterm`, or `transient`. Tagging happens two ways, using the
generated model itself:

1. **Inline ("classify as it decodes")** — a system preamble instructs the
   model to wrap durable content in `<longterm>…</longterm>` and working
   state in `<shortterm>…</shortterm>`. The harness splits each reply on
   those markers into separately-tagged blocks and strips the markup from
   what the user sees.
2. **Post-turn router pass** — a second cheap call to the *same* model
   labels this turn's untagged blocks as JSON (`longterm / shortterm /
   transient`). `classify_mode="both"` (default) uses both paths.

When the window exceeds `max_context_tokens`, eviction runs in phases:

| Phase | Condition | Action |
|---|---|---|
| **1** | over budget | delete every `transient` block (except the last `keep_recent_turns` turns and pinned system blocks) |
| **2** | window is now only long+short memory | evict **shortterm FIFO in rounds**: each round keeps the newest `shortterm_keep_pct` fraction, deletes the oldest; repeats while still over budget |
| **3** | long-term alone overflows | offload oldest long-term blocks to `archive.jsonl`, leave an evictable pointer stub (retrievable via `/recall`) |

Long-term memory is never silently destroyed — it is only ever offloaded to
the archive, fixing the "can offload but can't evict" failure mode the paper's
ContextBench exposes in tool-based baselines.

Each turn the live window is mirrored to `context.md` (human-readable,
paper-style `[[CTX_TURN n role=… tag=…]]` markers) and `context.jsonl`
(machine-readable), like the paper's context-as-a-file.

## Install

All dependencies are declared in **`pyproject.toml`** (the single source of
truth). Create a virtual environment and install the package into it:

```bash
cd clm-mem
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"     # package + pytest (recommended for hacking)
pip install -e .            # runtime only (needs: openai client)
pip install -e ".[tokens]"  # + exact token counting via the model's HF tokenizer
```

For a byte-for-byte reproducible environment, use the frozen lockfile that
pairs with this checkout (deps pinned to the exact versions tested):

```bash
pip install -r requirements.lock.txt   # pinned versions (also at workspace root)
pip install -e ./clm-mem               # then the package itself, editable
```

Deactivate / clean up any time with `deactivate` and by deleting `.venv/`.

Python ≥ 3.10.

## Serve a model

Any OpenAI-compatible endpoint works. Examples with Qwen:

```bash
# vLLM
vllm serve Qwen/Qwen3-8B-Instruct --max-model-len 16384

# Ollama (openai-compatible at :11434/v1)
ollama pull qwen3:8b
```

## Run the chat harness

```bash
clm-mem chat \
  --base-url http://localhost:8000/v1 \
  --model Qwen/Qwen3-8B-Instruct \
  --max-context-tokens 8192 \
  --shortterm-keep-pct 0.5
```

Ollama variant:

```bash
clm-mem chat --base-url http://localhost:11434/v1 --model qwen3:8b \
  --max-context-tokens 6144 --shortterm-keep-pct 0.75
```

In-session commands:

```
/status                # tokens used, block counts per memory tier
/long    /short        # list live long-term / short-term blocks
/recall <text>         # substring search the eviction archive
/reinject <block-id>   # pull an archived block back into context (as shortterm)
/keep <pct>            # change shortterm_keep_pct mid-session, e.g. /keep 0.75
/quit
```

## Demo (no GPU, no server)

Exercises the full pipeline — inline tagging, router pass, all three eviction
phases — with a deterministic stub model:

```bash
clm-mem demo --max-context-tokens 900 --shortterm-keep-pct 0.5
```

You should see phases fire like:

```
  [evict] phase1: deleted 4 transient block(s)
  [evict] phase2: FIFO-evicted 3 of 5 shortterm (kept newest 50%)
  [evict] phase3: offloaded longterm #17 to archive, left pointer
```

## Key options

| Flag | Default | Meaning |
|---|---|---|
| `--max-context-tokens` | 8192 | live-window budget (keep below your server's `--max-model-len`) |
| `--shortterm-keep-pct` | 0.5 | **the memory knob**: fraction of short-term kept per FIFO round (`/keep` to change live) |
| `--keep-recent-turns` | 2 | trailing turns exempt from phase-1 transient deletion |
| `--classify-mode` | both | `inline`, `post`, or `both` tagging paths |
| `--tokenizer` | = model id | HF tokenizer for exact counts (heuristic ~4 chars/token if unavailable) |
| `--workdir` | `.` | where `context.md`, `context.jsonl`, `archive.jsonl` live |

## Use as a library

```python
from clm_mem import MemoryHarness, HarnessConfig, OpenAIChat

h = MemoryHarness(
    OpenAIChat("http://localhost:8000/v1", "Qwen/Qwen3-8B-Instruct"),
    HarnessConfig(max_context_tokens=8192, shortterm_keep_pct=0.5),
)

print(h.step("Remember: the staging deploy port is 8080."))
h.cfg.shortterm_keep_pct = 0.75          # tune the knob at runtime
print(h.status())                         # tokens/blocks per tier
print(h.recall("8080"))                   # search evicted/offloaded memory
```

## Tests

With the venv active (`source .venv/bin/activate`):

```bash
python -m pytest tests/ -q     # 9 tests, no GPU/server needed
```

## Limitations vs. the paper

- The eviction policy here is **harness-defined**; the paper's headline result
  comes from letting the model *edit* the context file itself (e.g. via a
  Bash tool). The `context.md` mirror is there if you want to bolt that on.
- Tag quality rides on the base model's zero-shot judgment; small models
  occasionally forget the markers — the post-turn router pass is the backstop.
- Phase-3 offload keeps pointer stubs in-window, so a pathological long-term-
  heavy session still needs `--max-context-tokens` headroom for the preamble.

## License

MIT
