"""v0.9 recall: time-range filters, frequency boost, namespace isolation."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory

PROVIDER = os.environ.get("AI_MEMORY_EMBED", "hash")


def _mem(tmp_path) -> Memory:
    return Memory(db_path=str(tmp_path / "r09.db"), embed_provider=PROVIDER)


def test_range_filter(tmp_path):
    m = _mem(tmp_path)
    now = time.time()
    old_ts, new_ts = now - 60 * 86400, now - 1 * 86400
    for ts, txt in (
        (old_ts, "rangeprobe ancient decision alpha"),
        (new_ts, "rangeprobe recent decision beta"),
    ):
        eid = m.store(f"{txt} marker")
        m.con.execute("UPDATE episodes SET ts=? WHERE id=?", (ts, eid))
    m.con.commit()

    all_hits = [h["text"] for h in m.recall("rangeprobe decision", k=5)]
    assert any("alpha" in t for t in all_hits), all_hits
    assert any("beta" in t for t in all_hits), all_hits

    week = [
        h["text"] for h in m.recall("rangeprobe decision", k=5, since=now - 7 * 86400)
    ]
    assert all("alpha" not in t for t in week), week
    assert any("beta" in t for t in week), week

    past = [
        h["text"] for h in m.recall("rangeprobe decision", k=5, until=now - 30 * 86400)
    ]
    assert any("alpha" in t for t in past), past
    assert all("beta" not in t for t in past), past


def test_frequency_boost_and_logging(tmp_path):
    m = _mem(tmp_path)
    m.store("freqprobe the lighthouse keeper's log")
    first = m.recall("freqprobe lighthouse", k=3)
    assert first, "no hits at all"
    s0 = first[0]["score"]
    for _ in range(25):
        m.recall("freqprobe lighthouse", k=3)
    last = m.recall("freqprobe lighthouse", k=3)
    assert last[0]["score"] > s0, (s0, last[0]["score"])
    n = m.con.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    assert n >= 25, n  # every recall logs its hits


def test_namespace_isolation(tmp_path):
    m = _mem(tmp_path)
    m.store("Quinoa is the codeword", namespace="probe")
    default_hits = [h["text"] for h in m.recall("codeword", k=5)]
    assert all("Quinoa" not in t for t in default_hits), default_hits
    scoped = [h["text"] for h in m.recall("codeword", k=5, namespace="probe")]
    assert any("Quinoa" in t for t in scoped), scoped


def test_maintenance_prunes_access_log(tmp_path):
    m = _mem(tmp_path)
    eid = m.store("pruneprobe episode")
    ancient = time.time() - 100 * 86400
    m.con.execute(
        "INSERT INTO access_log(ref_id,store,accessed_at) VALUES(?,?,?)",
        (eid, "episode", ancient),
    )
    m.con.execute(
        "INSERT INTO access_log(ref_id,store,accessed_at) VALUES(?,?,?)",
        ("no-such-id", "episode", time.time()),
    )
    m.con.commit()
    out = m.maintenance()
    left = m.con.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    assert left == 0, (out, left)


def test_fts_rank_uses_relevance_not_insertion_order(tmp_path):
    # v0.9.2: FTS query lacked ORDER BY bm25(), so rank 0 was the OLDEST row and
    # a brand-new exact match landed last in the FTS component (0.056 vs 1.0).
    from ai_memory import db as DB
    from ai_memory import recall as R
    from ai_memory import store as S

    con = DB.connect(str(tmp_path / "fts.db"), dim=64)
    weak = S.store_fact(con, "project", "notes", "mcp server mentioned in passing")
    strong = S.store_fact(
        con,
        "project",
        "mcp.reload",
        "mcp reload script for server code editing after changes",
    )
    hits = R.recall(con, "mcp reload script server code editing", 5)
    assert hits[0].ref_id == strong
    assert {h.ref_id for h in hits} == {weak, strong}
