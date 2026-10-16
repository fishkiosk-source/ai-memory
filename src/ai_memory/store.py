"""Write path: store episodes / facts / procedures / working (+vec index)."""

from __future__ import annotations
import json
import random
import sqlite3
import time

from . import salience
from .db import vec_insert, vec_delete  # noqa: F401 (re-export for sdk.forget)


def _nid(prefix: str = "mem") -> str:
    return f"{prefix}_{int(time.time() * 1000):x}_{random.randint(0, 0xFFFF):04x}"


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
        con.execute(
            "UPDATE facts SET status='superseded', superseded_by=? WHERE id=? AND status='active'",
            (mid, row["id"]),
        )
        con.execute(
            "INSERT INTO facts_fts(id,entity,key,value) VALUES(?,?,?,?)",
            (mid, entity, key, value),
        )
    else:
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
    if row:  # dedupe: same trigger+ns updates steps, reinforces uses
        con.execute(
            "UPDATE procedures SET steps=?, updated=?, uses=COALESCE(uses,1)+1 WHERE id=?",
            (steps, time.time(), row["id"]),
        )
        if vec is not None:
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
