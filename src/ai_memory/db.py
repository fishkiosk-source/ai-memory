"""SQLite schema + FTS5 + sqlite-vec (optional/best-effort)."""

from __future__ import annotations
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS episodes(
  id TEXT PRIMARY KEY, ts REAL, actor TEXT, text TEXT,
  harness TEXT, importance REAL, processed INT DEFAULT 0, ns TEXT DEFAULT 'default');
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, entity TEXT, key TEXT, value TEXT,
  confidence REAL, importance REAL, updated REAL, source_episodes TEXT, ns TEXT DEFAULT 'default');
CREATE TABLE IF NOT EXISTS procedures(
  id TEXT PRIMARY KEY, trigger TEXT, steps TEXT,
  uses INT DEFAULT 1, success REAL DEFAULT 0.5, updated REAL, ns TEXT DEFAULT 'default');
CREATE TABLE IF NOT EXISTS working(
  session_id TEXT, ts REAL, role TEXT, text TEXT, ns TEXT DEFAULT 'default');
CREATE TABLE IF NOT EXISTS access_log(
  ref_id TEXT, store TEXT, accessed_at REAL);
CREATE INDEX IF NOT EXISTS idx_access_ref ON access_log(ref_id, accessed_at);
CREATE INDEX IF NOT EXISTS idx_access_ts ON access_log(accessed_at);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(id UNINDEXED, text);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(id UNINDEXED, entity, key, value);
CREATE INDEX IF NOT EXISTS idx_ep_ts ON episodes(ts);
CREATE INDEX IF NOT EXISTS idx_facts_ent ON facts(entity, key);
"""

VEC_TABLES = ("vec_episodes", "vec_facts", "vec_procedures")

# sqlite3.Connection is a C type without __dict__ — track vec state here.
_STATE: dict[int, dict] = {}


def _migrate(con: sqlite3.Connection) -> None:
    """Add ns columns to pre-v0.4 databases; NULL/""/missing -> 'default'."""
    for tbl in ("episodes", "facts", "procedures", "working"):
        cols = [r["name"] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if "ns" not in cols:
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN ns TEXT DEFAULT 'default'")
        con.execute(f"UPDATE {tbl} SET ns='default' WHERE ns IS NULL OR ns=''")
    for idx, tbl in (("idx_ep_ns", "episodes"), ("idx_facts_ns", "facts")):
        try:
            con.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {tbl}(ns)")
        except Exception:
            pass
    con.commit()


def connect(db_path: str, dim: int = 384) -> sqlite3.Connection:
    # check_same_thread=False: MCP servers (FastMCP) call tools from worker
    # threads; callers serialize access via Memory._lock.
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    _STATE[id(con)] = {"dim": dim, "has_vec": False}
    try:
        import sqlite_vec  # type: ignore

        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodes USING vec0(id TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_facts USING vec0(id TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_procedures USING vec0(id TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        _STATE[id(con)]["has_vec"] = True
    except Exception:
        pass
    return con


def has_vec(con: sqlite3.Connection) -> bool:
    return bool(_STATE.get(id(con), {}).get("has_vec", False))


def vec_dim(con: sqlite3.Connection) -> int:
    return int(_STATE.get(id(con), {}).get("dim", 384))


def vec_insert(
    con: sqlite3.Connection, table: str, ref_id: str, vec: list[float] | None
) -> None:
    if vec is None or not has_vec(con):
        return
    if table not in VEC_TABLES:
        raise ValueError(f"unknown vec table {table}")
    if len(vec) != vec_dim(con):
        return  # dim mismatch: skip rather than corrupt index
    try:
        from sqlite_vec import serialize_float32  # type: ignore

        con.execute(
            f"INSERT OR REPLACE INTO {table}(id, embedding) VALUES(?, ?)",
            (ref_id, serialize_float32(vec)),
        )
    except Exception:
        pass


def vec_delete(con: sqlite3.Connection, table: str, ref_id: str) -> None:
    if not has_vec(con):
        return
    try:
        con.execute(f"DELETE FROM {table} WHERE id=?", (ref_id,))
    except Exception:
        pass


def vec_search(
    con: sqlite3.Connection, table: str, qvec: list[float], limit: int = 50
) -> list[tuple[str, float]]:
    """Return [(id, distance)] ordered by cosine distance asc. Empty if vec unavailable."""
    if not has_vec(con) or qvec is None or len(qvec) != vec_dim(con):
        return []
    try:
        from sqlite_vec import serialize_float32  # type: ignore

        rows = con.execute(
            f"SELECT id, vec_distance_cosine(embedding, ?) AS d FROM {table} "
            f"ORDER BY d LIMIT {int(limit)}",
            (serialize_float32(qvec),),
        ).fetchall()
        return [(r["id"], float(r["d"])) for r in rows]
    except Exception:
        return []


def vec_counts(con: sqlite3.Connection) -> dict:
    if not has_vec(con):
        return {"vec": False}
    out = {"vec": True}
    for t in VEC_TABLES:
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = 0
    return out
