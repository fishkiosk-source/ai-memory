"""Amygdala: rule-based salience scorer (LLM upgrade in v0.3)."""
from __future__ import annotations

HIGH = ("remember", "always", "never", "prefer", "my name is", "important")
MID = ("decided", "decision", "error", "fixed", "likes", "prefers", "todo")
LOW = ("hello", "hi", "thanks", "ok", "hey :)")


def score(text: str, kind: str = "episode", hint: float | None = None) -> float:
    if hint is not None:
        return max(0.0, min(1.0, hint))
    t = text.lower()
    if any(k in t for k in HIGH):
        return 0.9
    if any(k in t for k in MID):
        return 0.65
    if any(k in t for k in LOW) or len(t.split()) < 3:
        return 0.15
    return 0.45 if kind == "episode" else 0.6
