"""DB maintenance: integrity, index hygiene, space reclaim. Offline, no deps."""

from __future__ import annotations
import os
import sqlite3


def run_maintenance(con: sqlite3.Connection) -> dict:
    con.commit()
    db_path = con.execute("PRAGMA database_list").fetchone()[2]
    before = os.path.getsize(db_path) if db_path != ":memory:" else 0
    before_free = con.execute("PRAGMA freelist_count").fetchone()[0]

    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]

    # orphan hygiene (vec + FTS rows whose base row is gone)
    orphans = 0
    for vec, base in (
        ("vec_episodes", "episodes"),
        ("vec_facts", "facts"),
        ("vec_procedures", "procedures"),
    ):
        try:
            cur = con.execute(
                f"DELETE FROM {vec} WHERE id NOT IN (SELECT id FROM {base})"
            )
            orphans += cur.rowcount
        except Exception:
            pass
    try:
        con.execute(
            "DELETE FROM episodes_fts WHERE id NOT IN (SELECT id FROM episodes)"
        )
        con.execute("DELETE FROM facts_fts WHERE id NOT IN (SELECT id FROM facts)")
    except Exception:
        pass
    try:  # working is a rolling buffer, not forever
        import time as _time

        cur = con.execute(
            "DELETE FROM working WHERE ts<?", (_time.time() - 30 * 86400,)
        )
        orphans += cur.rowcount
    except Exception:
        pass
    con.commit()

    # FTS index rebuild hints
    for tbl in ("episodes_fts", "facts_fts"):
        try:
            con.execute(f"INSERT INTO {tbl}({tbl}) VALUES('optimize')")
        except Exception:
            pass

    con.commit()
    con.execute("VACUUM")
    after = os.path.getsize(db_path) if db_path != ":memory:" else 0
    after_free = con.execute("PRAGMA freelist_count").fetchone()[0]
    return {
        "integrity": integrity,
        "orphans_removed": orphans,
        "freelist_before": before_free,
        "freelist_after": after_free,
        "bytes_before": before,
        "bytes_after": after,
        "bytes_reclaimed": before - after,
    }
