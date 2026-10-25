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


def test_off_provider_writes_text_only(tmp_path):
    # plugin path: AI_MEMORY_EMBED=off must be a fast, honest write (no vector)
    m = sdk.Memory(db_path=str(tmp_path / "off.db"), embed_provider="off")
    assert m.embedder is None
    assert m.vec_enabled is False
    rid = m.store("plugin captured this without vectors", kind="episode")
    assert m.con.execute("SELECT COUNT(*) FROM vec_episodes WHERE id=?", (rid,)).fetchone()[0] == 0
    # recall still works (FTS path), no qvec needed
    hits = m.recall("plugin captured vectors", k=3)
    assert [h["ref_id"] for h in hits] == [rid]


def test_worker_backfill_fills_text_only_rows(tmp_path):
    from ai_memory import vecfill

    off = sdk.Memory(db_path=str(tmp_path / "bf.db"), embed_provider="off")
    rid = off.store("row written by a hook with embeddings off", kind="episode")
    assert off.con.execute("SELECT COUNT(*) FROM vec_episodes WHERE id=?", (rid,)).fetchone()[0] == 0

    from ai_memory.embeddings import HashEmbedder

    emb = HashEmbedder(384)
    out = vecfill.backfill_vecs(off.con, emb.embed)
    assert out["episodes"]["filled"] == 1
    assert off.con.execute("SELECT COUNT(*) FROM vec_episodes WHERE id=?", (rid,)).fetchone()[0] == 1
