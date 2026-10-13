"""Inline tag parsing + the two prompts that make self-management work.

The paper shows off-the-shelf models will happily author their own structured
context markers (``[[CTX_TURN]]``, ``@STATE`` ...) zero-shot. We lean on the
same behaviour: the model wraps durable content in ``<longterm>`` /
``<shortterm>`` while decoding, and a second cheap call re-labels anything it
missed. No fine-tuning involved.
"""
from __future__ import annotations
import re
from typing import List, Tuple
from .blocks import LONG, SHORT, TRANSIENT

TAG_RE = re.compile(r"<(longterm|shortterm)>(.*?)</\1>", re.S | re.I)


def split_tagged(text: str) -> List[Tuple[str, str]]:
    """Split decoded text into ``(tag, content)`` chunks.

    ``'a <longterm>b</longterm> c'`` ->
    ``[(transient,'a'), (longterm,'b'), (transient,'c')]``
    """
    out: List[Tuple[str, str]] = []
    pos = 0
    for m in TAG_RE.finditer(text):
        if text[pos:m.start()].strip():
            out.append((TRANSIENT, text[pos:m.start()].strip()))
        out.append((LONG if m.group(1).lower() == "longterm" else SHORT,
                    m.group(2).strip()))
        pos = m.end()
    if text[pos:].strip():
        out.append((TRANSIENT, text[pos:].strip()))
    return out or [(TRANSIENT, text.strip())]


def strip_tags(text: str) -> str:
    """User-facing rendering: remove the memory markup."""
    return TAG_RE.sub(lambda m: m.group(2).strip(), text).strip()


MEMORY_PREAMBLE = """\
You are managing your own context (Context-Language-Model mode). The \
conversation history you see may have been pruned: older chatter was deleted, \
some content is summarized memory, and items marked [offloaded ... -> \
archive.jsonl] can be retrieved on request. While you reply, wrap anything \
worth keeping in markers:
<longterm>facts/decisions/IDs/specs that must survive a long time</longterm>
<shortterm>working state needed for the next while</shortterm>
Everything else may be deleted when the context window fills. Keep unmarked \
prose for your normal user-facing reply, and never tag information you have \
not actually been given."""

CLASSIFIER_SYSTEM = """\
You are a memory router for an LLM that manages its own context \
(Context-Language-Model style). Label each conversation block with exactly one \
tag:
- longterm: durable facts, decisions, requirements, identifiers, credentials,
  specs, results likely needed much later.
- shortterm: working state -- intermediate reasoning, recent tool/task output
  useful for a while but not forever.
- transient: pleasantries, filler, redundant restatement, small talk.
Reply ONLY with JSON: {"labels": [{"id": <int>, "tag": \
"longterm|shortterm|transient"}, ...]}"""
