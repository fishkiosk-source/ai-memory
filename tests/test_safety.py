"""v0.8 eval: forget is undoable (trash), auto-purges after 30d, backups pruned."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory


def _mem():
    return Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")


def test_forget_restore_roundtrip():
    m = _mem()
    m.store("Remember: safety net test phrase")
    assert m.forget(query="safety net") == 1
    assert not any("safety net" in h["text"].lower()
                   for h in m.recall("safety net", k=5)), "trashed must be unrecallable"
    assert m.restore(query="safety net") == 1
    assert any("safety net" in h["text"].lower()
               for h in m.recall("safety net", k=5)), "restored must recall"


def test_forget_fact_restore():
    m = _mem()
    m.store("helix", kind="fact", entity="user", key="editor")
    assert m.forget(query="helix") == 1
    assert m.stats()["facts"] == 0
    assert m.restore(ref_id=None, query="helix") == 1
    assert m.stats()["facts"] == 1
    assert any("helix" in h["text"].lower() for h in m.recall("editor", k=3))


def test_trash_purges_after_30d():
    m = _mem()
    m.store("ephemeral oops")
    assert m.forget(query="ephemeral") == 1
    # age the trash row past the grace window
    m.con.execute("UPDATE trash SET deleted_at=?", (time.time() - 31 * 86400,))
    m.con.commit()
    assert m.vacuum(trash_days=30) >= 1
    assert m.restore(query="ephemeral") == 0, "purged trash must stay gone"


def test_backup_prune_keep():
    m = _mem()
    m.store("backup prune check")
    made = [m.backup(keep=2) for _ in range(4)]
    import glob

    left = glob.glob(f"{m.db_path}.*.bak")
    assert len(left) == 2, left
    newest = Memory(db_path=max(left, key=os.path.getmtime), embed_provider="hash")
    assert newest.stats()["episodes"] == 1, "newest backup must hold the data"
    for p in made:
        for suf in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(p + suf)
            except OSError:
                pass


if __name__ == "__main__":
    for fn in (test_forget_restore_roundtrip, test_forget_fact_restore,
               test_trash_purges_after_30d, test_backup_prune_keep):
        fn()
        print(f"OK {fn.__name__}")
