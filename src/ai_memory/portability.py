"""Portability: JSONL export/import (vectors recomputed) + file backup."""
from __future__ import annotations
import json
import os
import sqlite3
import time

FORMAT = "ai-memory-export"
VERSION = 1
TABLES = ("episodes", "facts", "procedures")


def export_db(con: sqlite3.Connection, path: str) -> dict:
    counts = {}
    with open(path, "w") as f:
        f.write(json.dumps({"format": FORMAT, "version": VERSION,
                            "exported_at": time.time()}) + "\n")
        for tbl in TABLES:
            try:
                cols = [r["name"] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
            except Exception:
                continue
            n = 0
            for r in con.execute(f"SELECT * FROM {tbl}").fetchall():
                f.write(json.dumps({"table": tbl,
                                    "row": {c: r[c] for c in cols}}) + "\n")
                n += 1
            counts[tbl] = n
    return {"path": path, "bytes": os.path.getsize(path), **counts}


def import_db(con: sqlite3.Connection, path: str, embed_fn=None) -> dict:
    from .db import vec_insert

    counts = {t: 0 for t in TABLES}
    with open(path) as f:
        header = json.loads(f.readline())
        assert header.get("format") == FORMAT, f"not an ai-memory export: {path}"
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            tbl, row = obj["table"], obj["row"]
            if tbl not in TABLES:
                continue
            cols = ", ".join(row.keys())
            qs = ", ".join("?" for _ in row)
            con.execute(f"INSERT OR REPLACE INTO {tbl}({cols}) VALUES({qs})",
                        tuple(row.values()))
            if tbl == "episodes":
                con.execute("INSERT OR REPLACE INTO episodes_fts(id,text) VALUES(?,?)",
                            (row["id"], row["text"]))
                if embed_fn:
                    vec_insert(con, "vec_episodes", row["id"], embed_fn(row["text"]))
            elif tbl == "facts":
                con.execute(
                    "INSERT OR REPLACE INTO facts_fts(id,entity,key,value) VALUES(?,?,?,?)",
                    (row["id"], row["entity"], row["key"], row["value"]))
                if embed_fn:
                    vec_insert(con, "vec_facts", row["id"],
                               embed_fn(f"{row['entity']} {row['key']} {row['value']}"))
            counts[tbl] += 1
    con.commit()
    return {"path": path, **counts}


def backup_db(db_path: str, dest: str | None = None, keep: int | None = None) -> str:
    if dest is None:
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1e6) % 1000000:06d}"
        dest = f"{db_path}.{stamp}.bak"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if keep is not None:
        prune_backups(db_path, keep)
    return dest


def prune_backups(db_path: str, keep: int = 14) -> int:
    """Keep newest `keep` *.bak files beside db_path, delete the rest."""
    import glob

    cands = sorted(glob.glob(f"{db_path}.*.bak"), key=os.path.getmtime)
    doomed = cands[: max(0, len(cands) - keep)]
    for p in doomed:
        try:
            os.remove(p)
        except OSError:
            pass
    return len(doomed)
