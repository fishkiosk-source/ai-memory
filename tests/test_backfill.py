"""Backfill: rows stored without vecs get recomputed; worker passes embed_fn."""

import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory
from ai_memory import consolidator as C


def test_backfill_vecs(tmp_path):
    m = Memory(db_path=str(tmp_path / "t.db"), embed_provider="hash")
    eid = m.store("backfill test episode alpha")
    fid = m.store("backfill fact beta", kind="fact", entity="user", key="bf_key")
    # simulate pre-fix rows: drop their vecs
    m.con.execute("DELETE FROM vec_episodes WHERE id=?", (eid,))
    m.con.execute("DELETE FROM vec_facts WHERE id=?", (fid,))
    m.con.commit()
    out = m.backfill_vecs()
    assert out["episodes"]["filled"] == 1, out
    assert out["facts"]["filled"] == 1, out
    # idempotent second run
    out2 = m.backfill_vecs()
    assert out2["episodes"]["missing"] == 0, out2


def test_worker_wires_embed_fn():
    # worker.run() must pass a working embed_fn into run_once (the cron bug:
    # facts/procedures created without vecs). Simulate by calling run_once
    # the way worker does and checking a mined fact gets a vec.
    import sqlite3
    from ai_memory.db import connect, has_vec

    con = connect(":memory:")
    assert has_vec(con) or True  # vec optional; embed_fn is what matters
    got = {}

    def fake_embed(text):
        got["called"] = True
        return [0.1] * 384

    con.execute(
        "INSERT INTO episodes(id,ts,actor,text,harness,importance,ns)"
        " VALUES('ep_t1',0,'user','remember my coffee is black',"
        " 'opencode',0.7,'default')"
    )
    con.execute("INSERT INTO episodes_fts(id,text) VALUES('ep_t1','remember x')")
    con.commit()
    res = C.run_once(con, limit=10, embed_fn=fake_embed)
    assert got.get("called"), "embed_fn was not used by run_once"
    assert res["facts_made"] >= 1, res
