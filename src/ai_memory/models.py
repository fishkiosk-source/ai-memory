"""Dataclasses for memory rows."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Episode:
    id: str
    ts: float
    actor: str
    text: str
    harness: str
    importance: float
    processed: int = 0


@dataclass
class Fact:
    id: str
    entity: str
    key: str
    value: str
    confidence: float
    importance: float
    updated: float
    source_episodes: list[str] = field(default_factory=list)


@dataclass
class RecallHit:
    ref_id: str
    store: str  # episode | fact | procedure
    text: str
    score: float
    ts: float
    meta: dict
