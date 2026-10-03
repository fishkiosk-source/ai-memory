"""v0.3 eval: consolidation distills episodes -> facts/procedures, ignores noise."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory


def build() -> Memory:
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    m.store("Remember: user prefers dark mode")
    m.store("My editor is helix")
    m.store("Decided: deploy on Fridays is banned after the outage")
    m.store("Error: sqlite locked on concurrent write, fixed with WAL mode + single writer")
    m.store("To onboard a contributor, steps: run onboard.sh, copy .env.example, uv sync")
    m.store("hello")  # noise: must not become a fact
    return m


def test_consolidate_distills():
    m = build()
    res = m.consolidate()
    assert res["processed"] == 6, res
    assert res["facts_made"] >= 4, res
    assert res["procedures_made"] >= 1, res
    st = m.stats()
    assert st["facts"] >= 4 and st["procedures"] >= 1, st
    # structured attr fact usable via recall
    hits = m.recall("which editor?", k=3)
    assert any("helix" in h["text"].lower() for h in hits), hits


def test_consolidate_merges():
    m = build()
    m.consolidate()
    n_before = m.stats()["facts"]
    m.store("My editor is helix")
    m.store("My editor is zed")  # same (entity,key): value update, no new row
    m.consolidate()
    rows = m.con.execute("SELECT value FROM facts WHERE entity='user' AND key='editor'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "zed", rows
    assert m.stats()["facts"] == n_before, (n_before, m.stats())


def test_vacuum_drops_old_trivia():
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    old = time.time() - 100 * 86400
    m.con.execute(
        "INSERT INTO episodes(id,ts,actor,text,harness,importance,processed) VALUES(?,?,?,?,?,?,1)",
        ("ep_old", old, "user", "hey", "opencode", 0.01))
    m.con.execute("INSERT INTO episodes_fts(id,text) VALUES(?,?)", ("ep_old", "hey"))
    m.con.commit()
    n = m.vacuum(older_than_days=90)
    assert n == 1, n
    assert m.con.execute("SELECT COUNT(*) FROM episodes WHERE id='ep_old'").fetchone()[0] == 0


if __name__ == "__main__":
    for fn in (test_consolidate_distills, test_consolidate_merges, test_vacuum_drops_old_trivia):
        fn()
        print(f"OK {fn.__name__}")
