"""Memory tags and the Block: the unit of live context."""

from __future__ import annotations

from dataclasses import dataclass

LONG = "longterm"
SHORT = "shortterm"
TRANSIENT = "transient"
VALID_TAGS = frozenset({LONG, SHORT, TRANSIENT})


@dataclass
class Block:
    """One unit of context: a message plus its memory classification.

    Analogous to the paper's ``[[CTX_TURN n role=... ]]`` serialized turns;
    ``turn`` is the FIFO key used by short-term eviction.
    """

    id: int
    role: str  # system | user | assistant
    content: str
    tag: str = TRANSIENT  # longterm | shortterm | transient
    turn: int = 0  # conversation turn when created (FIFO order)
    pinned: bool = False  # e.g. system prompt -- never evicted
    importance: float = 0.0  # base value, assigned from tag on add()
    last_access: int = -1  # turn of last reheat; creation turn if unset
