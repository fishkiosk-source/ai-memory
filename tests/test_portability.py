"""v0.4 eval: namespace isolation + export/import roundtrip + backup."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory


def test_namespace_isolation():
    a = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash",
               namespace="work")
    a.store("Deploy freezes on Fridays, no exceptions")
    b = Memory(db_path=a.db_path, embed_provider="hash", namespace="personal")
    b.store("Deploy sourdough starter on Fridays for pizza night")
    got_a = a.recall("deploy fridays", k=5)
    got_b = b.recall("deploy fridays", k=5)
    assert any("freezes" in h["text"].lower() for h in got_a), got_a
    assert all("freezes" not in h["text"].lower() for h in got_b), got_b
    assert any("sourdough" in h["text"].lower() for h in got_b), got_b


def test_consolidate_stays_in_namespace():
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash",
               namespace="work")
    m.store("My editor is helix")
    m.consolidate()
    assert m.recall("which editor?", k=3), "own ns must see distilled fact"
    other = Memory(db_path=m.db_path, embed_provider="hash", namespace="personal")
    assert not any("helix" in h["text"].lower()
                   for h in other.recall("which editor?", k=5)), "ns leak"


def test_export_import_roundtrip():
    src = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    src.store("User prefers dark mode")
    src.store("Portugal", kind="fact", entity="user", key="vacation")
    src.store("Run onboard.sh first", kind="procedure", key="onboard")
    assert src.stats()["episodes"] == 1
    path = tempfile.mktemp(suffix=".jsonl")
    res = src.export(path)
    assert res["episodes"] == 1 and res["facts"] == 1 and res["procedures"] == 1, res
    assert json.loads(open(path).readline())["format"] == "ai-memory-export"

    dst = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    got = dst.import_(path)
    assert got["episodes"] == 1 and got["facts"] == 1, got
    assert dst.stats()["episodes"] == 1 and dst.stats()["facts"] == 1
    assert any("dark mode" in h["text"].lower()
               for h in dst.recall("dark mode", k=3))
    os.remove(path)


def test_backup_opens():
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    m.store("remember this for backup test")
    bak = m.backup()
    try:
        m2 = Memory(db_path=bak, embed_provider="hash")
        assert m2.stats()["episodes"] == 1, m2.stats()
    finally:
        for suf in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(bak + suf)
            except OSError:
                pass


if __name__ == "__main__":
    for fn in (test_namespace_isolation, test_consolidate_stays_in_namespace,
               test_export_import_roundtrip, test_backup_opens):
        fn()
        print(f"OK {fn.__name__}")
