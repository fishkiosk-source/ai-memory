"""Shared config + paths."""
from __future__ import annotations
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    db_path: str = os.environ.get("AI_MEMORY_DB", "memory.db")
    session_id: str = os.environ.get("AI_MEMORY_SESSION", "default")
    embed_dim: int = 384
    embed_provider: str = os.environ.get("AI_MEMORY_EMBED", "hash")  # hash | sentence-transformers | ollama
    namespace: str = os.environ.get("AI_MEMORY_NS", "default")
