"""LLM clients. Anything with a ``chat(messages) -> str`` method works;
OpenAIChat targets any OpenAI-compatible server, StubChat powers the offline
demo and the test-suite without a GPU.

SystemOneClient wraps the System One Lite decision API (http://127.0.0.1:8010)
for typed questions -- used for memory classification and eviction backstops."""
from __future__ import annotations
import json
import os
import re
from typing import Any, Dict, List, Optional
from .blocks import LONG, SHORT, TRANSIENT


class OpenAIChat:
    """Thin wrapper over any OpenAI-compatible endpoint (vLLM, Ollama,
    llama.cpp server, LM Studio, ...)."""

    def __init__(self, base_url: str = "http://localhost:8000/v1",
                 model: str = "Qwen/Qwen3-8B-Instruct",
                 api_key: str = "EMPTY",
                 temperature: float = 0.6, max_tokens: int = 1024):
        from openai import OpenAI   # lazy: keeps `demo` runnable without the dep
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat(self, messages: List[Dict[str, str]],
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
        )
        return resp.choices[0].message.content or ""


class StubChat:
    """Deterministic fake LLM: emits inline memory tags and answers the
    memory-router classification prompt with keyword heuristics."""

    _FACTS = [
        "<longterm>User's project codename is {i}-Aurora; deploy port is 80{i}.</longterm> Noted.",
        "Sure -- working through it now. Step A done, step B pending (call {i}).",
        "Remember: final decision v{i} ships Friday with schema rev {i}."
        "<shortterm>open item: review diff {i}</shortterm>",
        "Ha, good one! Anyway, continuing.",
        "<longterm>API key stored as KEY_{i}; table name is users_v{i}.</longterm> Saved it.",
    ]
    _LONG_KW = ("remember", "decision", "password", "api key", "codename",
                "deadline", "spec", "final", "port", "schema")

    def chat(self, messages: List[Dict[str, str]],
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None) -> str:
        first = messages[0]["content"] if messages else ""
        last = messages[-1]["content"] if messages else ""
        if "memory router" in first:
            return self._classify(last)
        i = last.count("\n") + len(last) % 97
        return self._FACTS[i % len(self._FACTS)].format(i=i)

    @classmethod
    def _classify(cls, prompt: str) -> str:
        labels = []
        for m in re.finditer(r"id=(\d+) role=(\w+) text=(.*)", prompt):
            bid, _role, txt = m.groups()
            low = txt.lower()
            tag = LONG if any(k in low for k in cls._LONG_KW) else (
                TRANSIENT if len(txt) < 60 else SHORT)
            labels.append({"id": int(bid), "tag": tag})
        return json.dumps({"labels": labels})


#: Both the llama.cpp SystemOne fork and the Featherless classifier service
#: answer this endpoint family; ``/v1/classifier`` is the canonical hosted
#: path (also served as /v1/systemone and /v1/decisions), while local fork
#: builds only expose /v1/systemone. We try them in that order and cache.
SYSTEMONE_PATHS = ("/v1/systemone", "/v1/classifier")

#: Both APIs reject bodies without a non-empty ``model`` field, so the client
#: always sends one; override via --systemone-model / the studio settings.
DEFAULT_SYSTEMONE_MODEL = "systemone"


class SystemOneClient:
    """HTTP client for the System One Lite decision API.

    Sends typed questions (choice, score, noul) and returns calibrated
    answers. Used for memory classification and eviction backstops.

    Works against both the local llama.cpp SystemOne fork and the hosted
    Featherless classifier endpoint. The request body always carries a
    ``model`` field (required by both servers), plus optional bearer auth.

    Default base URL is http://127.0.0.1:8010 (local System One Lite server).
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8010",
                 model: str = "", api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model or DEFAULT_SYSTEMONE_MODEL
        self.api_key = api_key or os.environ.get("FEATHERLESS_API_KEY", "")
        # cached endpoint path that worked (see SYSTEMONE_PATHS)
        self._path: Optional[str] = None

    def system_one(self, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate a state against typed questions.

        Args:
            state: The input text or JSON object to evaluate.
            questions: Dict of question_id -> question spec. Each spec has
                'type' (choice/score/noul), 'instructions', and type-specific
                fields ('criteria' for choice, etc.).

        Returns:
            Response dict with 'answers' keyed by question_id.
        """
        import urllib.request
        import urllib.error

        paths = ([self._path] if self._path else []) + [
            p for p in SYSTEMONE_PATHS if p != self._path]
        last_exc: Optional[Exception] = None
        for path in paths:
            body = json.dumps(
                {"model": self.model, "state": state,
                 "questions": questions}).encode()
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(
                f"{self.base_url}{path}", data=body,
                headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    out = json.loads(resp.read().decode())
                    self._path = path          # sticky: skip probing next time
                    return out
            except urllib.error.HTTPError as e:
                # 404/405 usually means this server does not expose that route
                # -- try the alias before giving up. (Featherless also uses
                # 404 for "model not enabled for /classifier", so keep the
                # error body around; it ends up in the final message.)
                if e.code in (404, 405):
                    try:
                        detail = e.read().decode(errors="replace")[:400]
                    except Exception:
                        detail = ""
                    last_exc = RuntimeError(
                        f"SystemOne API {e.code} at {path}: {detail}")
                    continue
                detail = ""
                try:
                    detail = e.read().decode(errors="replace")[:400]
                except Exception:
                    pass
                raise RuntimeError(
                    f"SystemOne API error {e.code} at {path}: {detail}") from e
            except urllib.error.URLError as e:
                last_exc = e
        raise RuntimeError(f"SystemOne API error: {last_exc}") from last_exc

    def classify_memory(self, text: str) -> str:
        """Classify a conversation block's memory duration.

        Returns one of 'longterm', 'shortterm', or 'transient'.
        """
        questions = {
            "duration": {
                "type": "choice",
                "instructions": (
                    "What memory duration should this conversation block have? "
                    "Choose based on whether it contains durable facts/decisions "
                    "that must survive long-term, working state needed for a while, "
                    "or transient chatter that can be forgotten."
                ),
                "criteria": {
                    LONG: "Durable facts, decisions, IDs, specs, credentials, requirements likely needed much later",
                    SHORT: "Working state, intermediate reasoning, recent task output useful for a while but not forever",
                    TRANSIENT: "Pleasantries, filler, redundant restatement, small talk, ephemeral chatter"
                }
            }
        }
        resp = self.system_one({"text": text}, questions)
        return resp["answers"]["duration"]["choice"]

    def should_evict(self, block_content: str) -> bool:
        """Backstop check: is this block safe to evict from live memory?

        Returns True if safe to drop, False if it should be kept.
        """
        questions = {
            "safe_to_drop": {
                "type": "noul",
                "instructions": (
                    "Is this conversation block safe to remove from the live "
                    "context window? Answer yes only if it's transient chatter, "
                    "redundant information, or content that won't be needed again. "
                    "Answer no if it contains facts, decisions, working state, or "
                    "anything that might be referenced later."
                )
            }
        }
        resp = self.system_one({"block": block_content}, questions)
        # noul >= 0.7 means reasonably confident it's safe to drop
        return resp["answers"]["safe_to_drop"]["noul"] >= 0.7


class MockSystemOne:
    """Offline mock of SystemOneClient for testing and demo mode.

    Uses keyword heuristics instead of an actual model to simulate typed
    decisions. No HTTP calls required.
    """

    _LONG_KW = ("remember", "decision", "password", "api key", "codename",
                "deadline", "spec", "final", "port", "schema")

    def __init__(self):
        pass

    def system_one(self, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate typed decisions using keyword heuristics."""
        answers = {}
        for qid, spec in questions.items():
            if spec["type"] == "choice":
                # Extract text from state
                if isinstance(state, dict):
                    text = " ".join(str(v) for v in state.values())
                else:
                    text = str(state)
                low = text.lower()
                if any(k in low for k in self._LONG_KW):
                    answers[qid] = {"type": "choice", "choice": LONG,
                                    "confidence": 0.85}
                elif len(text) < 60:
                    answers[qid] = {"type": "choice", "choice": TRANSIENT,
                                    "confidence": 0.75}
                else:
                    answers[qid] = {"type": "choice", "choice": SHORT,
                                    "confidence": 0.80}
            elif spec["type"] == "noul":
                # Simulate: short/filler content is safe to drop
                if isinstance(state, dict):
                    text = " ".join(str(v) for v in state.values())
                else:
                    text = str(state)
                low = text.lower()
                has_facts = any(k in low for k in self._LONG_KW)
                is_short = len(text) < 60
                # High noul (yes, safe to drop) for short filler without facts
                if is_short and not has_facts:
                    answers[qid] = {"type": "noul", "noul": 0.85}
                elif has_facts:
                    answers[qid] = {"type": "noul", "noul": 0.25}
                else:
                    answers[qid] = {"type": "noul", "noul": 0.60}
            else:
                answers[qid] = {"type": spec["type"]}
        return {"answers": answers, "model": "mock-systemone"}

    def classify_memory(self, text: str) -> str:
        """Mock classification using keyword heuristics."""
        low = text.lower()
        if any(k in low for k in self._LONG_KW):
            return LONG
        elif len(text) < 60:
            return TRANSIENT
        else:
            return SHORT

    def should_evict(self, block_content: str) -> bool:
        """Mock eviction decision using keyword heuristics."""
        low = block_content.lower()
        has_facts = any(k in low for k in self._LONG_KW)
        is_short = len(block_content) < 60
        return (is_short and not has_facts) or not has_facts
