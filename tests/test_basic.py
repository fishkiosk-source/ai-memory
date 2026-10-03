"""Offline smoke test: store -> recall -> consolidate -> forget. stdlib only."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory


def run():
    tmp = tempfile.mktemp(suffix=".db")
    m = Memory(db_path=tmp)
    eid = m.store("Remember: user prefers dark mode", harness="opencode")
    assert eid.startswith("ep_"), eid
    hits = m.recall("dark mode")
    assert hits and any("dark" in h["text"].lower() for h in hits), hits
    res = m.consolidate()
    assert res["processed"] >= 1, res
    st = m.stats()
    assert st["episodes"] >= 1 and st["facts"] >= 1, st
    n = m.forget(query="dark mode")
    assert n >= 1, n
    os.path.exists(tmp) and os.remove(tmp)
    for suf in ("-journal", "-wal", "-shm"):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass
    print("OK store->recall->consolidate->forget", st, res)
    return 0


def test_smoke():
    assert run() == 0


if __name__ == "__main__":
    raise SystemExit(run())
