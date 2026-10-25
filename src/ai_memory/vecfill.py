"""Fill missing vector rows (shared by SDK, CLI and the consolidation worker).

Keeping this in one place matters: the "which rows count as missing" rule has to
match between the writer that skips vectors (offline / hash-disabled) and the
repair path. Facts are special-cased — superseded rows must never get a vector
back (T1.3), because a live vector slot for a dead fact resurrects it in recall.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from .db import vec_delete, vec_insert

VecFn = Callable[[str], "list[float] | None"]

JOBS = (
    ("episodes", "vec_episodes", lambda r: r["text"]),
    ("facts", "vec_facts", lambda r: f"{r['entity']} {r['key']} {r['value']}"),
    (
        "procedures",
        "vec_procedures",
        lambda r: (
            f"{r['trigger'] if 'trigger' in r.keys() else ''} "
            f"{r['steps'] if 'steps' in r.keys() else ''}"
        ),
    ),
)


def backfill_vecs(con: sqlite3.Connection, vec_fn: VecFn) -> dict:
    """Recompute vectors for rows missing from the vec tables. Returns counts."""
    out: dict = {}
    for base, vec_tbl, text_fn in JOBS:
        try:
            is_fact = base == "facts"
            missing = [
                r[0]
                for r in con.execute(
                    f"SELECT id FROM {base} WHERE id NOT IN "
                    f"(SELECT id FROM {vec_tbl})"
                    + (" AND (status IS NULL OR status='active')" if is_fact else "")
                ).fetchall()
            ]
            if is_fact:
                evicted = 0
                for r in con.execute("SELECT id FROM vec_facts").fetchall():
                    row = con.execute(
                        "SELECT status FROM facts WHERE id=?", (r["id"],)
                    ).fetchone()
                    if row and (row["status"] or "active") != "active":
                        vec_delete(con, "vec_facts", r["id"])
                        evicted += 1
                out.setdefault("facts_evicted", evicted)
        except Exception:
            missing = []
        done, failed = 0, 0
        for mid in missing:
            row = con.execute(f"SELECT * FROM {base} WHERE id=?", (mid,)).fetchone()
            if row is None:
                continue
            try:
                vec = vec_fn(text_fn(row))
            except Exception:
                vec = None
            if vec is None:
                failed += 1
                continue
            try:
                vec_insert(con, vec_tbl, mid, vec)
                done += 1
            except Exception:
                failed += 1
        out[base] = {"missing": len(missing), "filled": done, "failed": failed}
    try:
        con.commit()
    except Exception:
        pass
    return out
