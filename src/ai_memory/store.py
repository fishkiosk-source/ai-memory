"""Write path: store episodes / facts / procedures / working (+vec index)."""

from __future__ import annotations
import json
import random
import sqlite3
import time

from . import salience
from .db import has_vec, vec_delete, vec_insert, vec_search  # noqa: F401 (re-export)


def _nid(prefix: str = "mem") -> str:
    return f"{prefix}_{int(time.time() * 1000):x}_{random.randint(0, 0xFFFF):04x}"


# Semantic thresholds (T1.2/T2.4, plan.md). Need vecs; hash embedder works too,
# it just splits differently — thresholds are on cosine sim of stored vecs.
SEMANTIC_SUPERSEDE_SIM = 0.88  # same entity+ns, different key: new fact wins
SEMANTIC_CONF_WINDOW = 0.10  # new confidence must be >= old - window to win
PROC_MERGE_SIM = 0.90  # procedure near-duplicate trigger+steps → merge


def _is_active(status: str | None) -> bool:
    return (status or "active") == "active"


# Working memory is short-lived (session context). Drop trivia older than this so
# the table doesn't grow without bound across sessions.
WORKING_TTL_DAYS = 7


def store_episode(
    con: sqlite3.Connection,
    text: str,
    actor: str = "user",
    harness: str = "opencode",
    importance: float | None = None,
    vec: list[float] | None = None,
    namespace: str = "default",
) -> str:
    imp = salience.score(text, "episode", importance)
    mid = _nid("ep")
    ts = time.time()
    con.execute(
        "INSERT INTO episodes(id,ts,actor,text,harness,importance,ns) VALUES(?,?,?,?,?,?,?)",
        (mid, ts, actor, text, harness, imp, namespace),
    )
    con.execute("INSERT INTO episodes_fts(id,text) VALUES(?,?)", (mid, text))
    vec_insert(con, "vec_episodes", mid, vec)
    con.commit()
    return mid


def _nearest_other_fact(
    con: sqlite3.Connection,
    qvec: list[float] | None,
    entity: str,
    key: str,
    namespace: str,
) -> tuple[sqlite3.Row | None, float]:
    """Nearest ACTIVE fact with same entity+ns but a DIFFERENT key.

    Same-key rows are handled by the caller (reinforce/supersede path); this is
    the cross-key collision check. Vec-unavailable or no qvec → (None, 0.0).
    """
    if not qvec or not has_vec(con):
        return None, 0.0
    try:
        for ref_id, dist in vec_search(con, "vec_facts", qvec, limit=8):
            row = con.execute("SELECT * FROM facts WHERE id=?", (ref_id,)).fetchone()
            if row is None or not _is_active(row["status"]):
                continue
            if (row["ns"] or "default") != namespace or row["entity"] != entity:
                continue
            if row["key"] == key:
                continue
            return row, max(0.0, 1.0 - dist)
    except Exception:
        pass
    return None, 0.0


def _supersede_row(con: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Mark old row superseded and drop its index slots (T1.3 hygiene)."""
    con.execute(
        "UPDATE facts SET status='superseded', superseded_by=? WHERE id=?",
        (new_id, old_id),
    )
    con.execute("DELETE FROM facts_fts WHERE id=?", (old_id,))
    vec_delete(con, "vec_facts", old_id)


def store_fact(
    con: sqlite3.Connection,
    entity: str,
    key: str,
    value: str,
    confidence: float = 0.8,
    importance: float | None = None,
    source_episodes: list[str] | None = None,
    vec: list[float] | None = None,
    namespace: str = "default",
) -> str:
    source_episodes = source_episodes or []
    imp = salience.score(f"{entity} {key} {value}", "fact", importance)
    ts = time.time()
    row = con.execute(
        "SELECT id,confidence,source_episodes,value,status FROM facts WHERE entity=? AND key=? AND ns=? AND status='active'",
        (entity, key, namespace),
    ).fetchone()
    mid = _nid("fact")
    dup_row = None
    if row is None:
        # semantic collision check (T1.2): near-duplicate of an existing fact
        # under a different key. Auto-supersede only when confidence is
        # comparable; otherwise both stay active (doctor reports the pair).
        cand, sim = _nearest_other_fact(con, vec, entity, key, namespace)
        if (
            cand is not None
            and sim >= SEMANTIC_SUPERSEDE_SIM
            and confidence >= float(cand["confidence"] or 0.8) - SEMANTIC_CONF_WINDOW
        ):
            dup_row = cand
    if row:  # merge/invalidate = cortical consolidation (per-namespace)
        old_src = json.loads(row["source_episodes"] or "[]")
        merged = sorted(set(old_src) | set(source_episodes))
        old_value = row["value"]
        old_status = row["status"] or "active"
        if old_value == value and old_status == "active":
            # same key + same value: reinforce in place, no new row
            conf = min(1.0, max(confidence, row["confidence"]) + 0.05)
            con.execute(
                "UPDATE facts SET confidence=?,importance=?,updated=?,source_episodes=? WHERE id=?",
                (conf, max(imp, 0.5), ts, json.dumps(merged), row["id"]),
            )
            if vec is not None:
                vec_insert(con, "vec_facts", row["id"], vec)
            con.commit()
            return row["id"]
        # same key + new value: invalidate old active fact, insert new active row
        _supersede_row(con, row["id"], mid)
        con.execute(
            "INSERT INTO facts_fts(id,entity,key,value) VALUES(?,?,?,?)",
            (mid, entity, key, value),
        )
    else:
        if dup_row is not None:
            _supersede_row(con, dup_row["id"], mid)
        con.execute(
            "INSERT INTO facts_fts(id,entity,key,value) VALUES(?,?,?,?)",
            (mid, entity, key, value),
        )
    con.execute(
        "INSERT INTO facts(id,entity,key,value,confidence,importance,updated,source_episodes,ns,status,superseded_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            mid,
            entity,
            key,
            value,
            confidence,
            imp,
            ts,
            json.dumps(source_episodes),
            namespace,
            "active",
            None,
        ),
    )
    vec_insert(con, "vec_facts", mid, vec)
    con.commit()
    return mid


def store_procedure(
    con: sqlite3.Connection,
    trigger: str,
    steps: str,
    namespace: str = "default",
    vec: list[float] | None = None,
) -> str:
    trigger = (trigger or "").strip()[:120] or "procedure"
    steps = (steps or "").strip()[:800]
    row = con.execute(
        "SELECT id, uses FROM procedures WHERE trigger=? AND ns=?",
        (trigger, namespace),
    ).fetchone()
    merged = False
    if row is None and vec and has_vec(con):
        # semantic dedupe (T2.4): near-identical procedure under a different
        # trigger phrasing merges into the existing row instead of multiplying
        try:
            for ref_id, dist in vec_search(con, "vec_procedures", vec, limit=5):
                prow = con.execute(
                    "SELECT id, ns FROM procedures WHERE id=?", (ref_id,)
                ).fetchone()
                if prow is None or (prow["ns"] or "default") != namespace:
                    continue
                if 1.0 - dist >= PROC_MERGE_SIM:
                    row = prow
                    merged = True
                    break
        except Exception:
            pass
    if row:  # exact or semantic dedupe: update steps, reinforce uses
        con.execute(
            "UPDATE procedures SET steps=?, updated=?, uses=COALESCE(uses,1)+1 WHERE id=?",
            (steps, time.time(), row["id"]),
        )
        if vec is not None or merged:
            vec_insert(con, "vec_procedures", row["id"], vec)
        con.commit()
        return row["id"]
    mid = _nid("proc")
    con.execute(
        "INSERT INTO procedures(id,trigger,steps,updated,ns) VALUES(?,?,?,?,?)",
        (mid, trigger, steps, time.time(), namespace),
    )
    vec_insert(con, "vec_procedures", mid, vec)
    con.commit()
    return mid


def push_working(
    con: sqlite3.Connection,
    session_id: str,
    role: str,
    text: str,
    limit: int = 50,
    namespace: str = "default",
) -> None:
    con.execute(
        "INSERT INTO working(session_id,ts,role,text,ns) VALUES(?,?,?,?,?)",
        (session_id, time.time(), role, text, namespace),
    )
    con.execute(
        "DELETE FROM working WHERE session_id=? AND ns=? AND rowid NOT IN "
        "(SELECT rowid FROM working WHERE session_id=? AND ns=? ORDER BY ts DESC LIMIT ?)",
        (session_id, namespace, session_id, namespace, limit),
    )
    # bound working memory across sessions: drop trivia older than the TTL
    cutoff = time.time() - WORKING_TTL_DAYS * 86400
    con.execute("DELETE FROM working WHERE ts<?", (cutoff,))
    con.commit()


def prune_working(con: sqlite3.Connection, older_than_days: int = 7) -> int:
    cutoff = time.time() - older_than_days * 86400
    cur = con.execute("DELETE FROM working WHERE ts<?", (cutoff,))
    con.commit()
    return cur.rowcount
