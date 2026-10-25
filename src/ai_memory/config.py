"""Shared config + paths."""

from __future__ import annotations
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # read env at instantiation, not at import: tests and long-lived processes
    # flip AI_MEMORY_* between calls
    db_path: str = field(
        default_factory=lambda: os.environ.get("AI_MEMORY_DB", "memory.db")
    )
    session_id: str = field(
        default_factory=lambda: os.environ.get("AI_MEMORY_SESSION", "default")
    )
    embed_dim: int = 384
    embed_provider: str = field(
        default_factory=lambda: os.environ.get("AI_MEMORY_EMBED", "hash")
    )
    namespace: str = field(
        default_factory=lambda: os.environ.get("AI_MEMORY_NS", "default")
    )
