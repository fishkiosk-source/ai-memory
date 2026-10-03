"""Trash: forget moves rows here first; restore within 30d, auto-purge after."""
from __future__ import annotations
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS trash(
  id TEXT PRIMARY KEY, tbl TEXT, row TEXT, deleted_at REAL);
CREATE INDEX IF NOT EXISTS idx_trash_ts ON trash(deleted_at);
"""


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def stash(con: sqlite3.Connection, tbl: str, row: sqlite3.Row) -> None:
    ensure(con)
    con.execute("INSERT OR REPLACE INTO trash(id,tbl,row,deleted_at) VALUES(?,?,?,?)",
                (row["id"], tbl, json.dumps(dict(row)), time.time()))


def find(con: sqlite3.Connection, ref_id: str | None = None,
         query: str | None = None) -> list[sqlite3.Row]:
    ensure(con)
    if ref_id:
        return con.execute("SELECT * FROM trash WHERE id=?", (ref_id,)).fetchall()
    if query:
        return con.execute("SELECT * FROM trash WHERE row LIKE ?",
                           (f"%{query}%",)).fetchall()
    return []


def remove(con: sqlite3.Connection, ref_id: str) -> None:
    ensure(con)
    con.execute("DELETE FROM trash WHERE id=?", (ref_id,))


def purge(con: sqlite3.Connection, older_than_days: int = 30) -> int:
    ensure(con)
    cur = con.execute("DELETE FROM trash WHERE deleted_at < ?",
                      (time.time() - older_than_days * 86400,))
    con.commit()
    return cur.rowcount
