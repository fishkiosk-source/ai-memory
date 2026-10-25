"""Embedder selection: env honored, non-hash failures fail closed (v0.9.2)."""

import sys

sys.path.insert(0, "src")

import pytest

from ai_memory import sdk
from ai_memory.embeddings import HashEmbedder


def test_env_provider_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_MEMORY_EMBED", "hash")
    m = sdk.Memory(db_path=str(tmp_path / "a.db"))
    assert m.embed_provider == "hash"
    assert isinstance(m.embedder, HashEmbedder)


def test_failed_st_does_not_fall_back_to_hash(tmp_path, monkeypatch):
    # a transient ST load failure used to become HashEmbedder, which wrote
    # incomparable vectors into the ST index (cosine ~0 -> dead rows)
    def boom(provider, dim):
        raise RuntimeError("hf hub down")

    monkeypatch.setattr(sdk, "get_embedder", boom)
    m = sdk.Memory(db_path=str(tmp_path / "b.db"), embed_provider="st")
    assert m.embedder is None
    assert "hf hub down" in (m.embed_error or "")
    assert m._vec("anything") is None
    assert m.vec_enabled is False

    # writes still work, just without vectors -> backfill can fill them later
    rid = m.store("episode without vector", kind="episode")
    assert (
        m.con.execute(
            "SELECT COUNT(*) FROM vec_episodes WHERE id=?", (rid,)
        ).fetchone()[0]
        == 0
    )
    assert (
        m.con.execute("SELECT COUNT(*) FROM episodes WHERE id=?", (rid,)).fetchone()[0]
        == 1
    )


def test_hash_provider_still_falls_back_on_error(tmp_path, monkeypatch):
    def boom(provider, dim):
        raise RuntimeError("nope")

    monkeypatch.setattr(sdk, "get_embedder", boom)
    m = sdk.Memory(db_path=str(tmp_path / "c.db"), embed_provider="hash")
    assert isinstance(m.embedder, HashEmbedder)
