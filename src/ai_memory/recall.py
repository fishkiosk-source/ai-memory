"""Read path: hybrid FTS + sqlite-vec, recency*importance rerank.

Final per architecture.md:
  score = 0.5*vec_sim + 0.3*fts_norm + 0.15*importance + 0.05*recency
  vec_sim = 1 - cosine_distance (sqlite-vec), clipped to [0,1]
  fts_norm = 1/(1+rank) over FTS hit order; 0 when no FTS hit
"""
from __future__ import annotations
import json
import math
import sqlite3
import time

from .db import vec_search
from .models import RecallHit

W_VEC, W_FTS, W_IMP, W_REC = 0.5, 0.3, 0.15, 0.05


def _recency(ts: float, lam: float = 0.01) -> float:
    age_days = max(0.0, (time.time() - ts) / 86400)
    return math.exp(-lam * age_days)


STOP = {"what", "does", "user", "like", "likes", "the", "a", "an", "is", "are",
        "do", "how", "why", "when", "where", "which", "who", "whom", "can",
        "could", "would", "should", "my", "your", "their", "them", "they",
        "our", "with", "for", "and", "all", "we", "you", "that", "this"}


def _sanitize_match(query: str) -> str | None:
    # FTS5 MATCH chokes on punctuation; keep alnum/space/*/" only.
    # Drop stopwords: "user"/"like"/"what" otherwise match every user fact.
    q = "".join(c if c.isalnum() or c in ' *"' else " " for c in query).strip()
    toks = [t.lower() for t in q.split() if len(t) > 1][:10]
    toks = [t for t in toks if t not in STOP]
    if not toks:
        return None
    return " OR ".join(toks)


def recall(con: sqlite3.Connection, query: str, k: int = 5,
           qvec: list[float] | None = None,
           namespace: str | None = None) -> list[RecallHit]:
    fts_rank: dict[tuple[str, str], float] = {}  # (store, id) -> fts_norm
    vec_sim: dict[tuple[str, str], float] = {}

    # 1. FTS candidates (rank-normalized)
    mq = _sanitize_match(query)
    if mq:
        for tbl, store in (("episodes_fts", "episode"), ("facts_fts", "fact")):
            try:
                rows = con.execute(
                    f"SELECT id FROM {tbl} WHERE {tbl} MATCH ? LIMIT 50", (mq,)
                ).fetchall()
                for rank, r in enumerate(rows):
                    key = (store, r["id"])
                    fts_rank[key] = max(fts_rank.get(key, 0.0), 1.0 / (1.0 + rank))
            except Exception:
                pass
    if not fts_rank:  # LIKE fallback for odd queries
        like = f"%{query}%"
        try:
            for r in con.execute(
                "SELECT id FROM episodes WHERE text LIKE ? LIMIT 20", (like,)).fetchall():
                fts_rank.setdefault(("episode", r["id"]), 0.4)
            for r in con.execute(
                "SELECT id FROM facts WHERE entity LIKE ? OR key LIKE ? OR value LIKE ? LIMIT 20",
                (like, like, like)).fetchall():
                fts_rank.setdefault(("fact", r["id"]), 0.4)
        except Exception:
            pass

    # 2. vector candidates via sqlite-vec (true cosine distance, not brute force)
    if qvec is not None:
        for tbl, store in (("vec_episodes", "episode"), ("vec_facts", "fact")):
            for ref_id, dist in vec_search(con, tbl, qvec, limit=50):
                sim = max(0.0, min(1.0, 1.0 - dist))
                if sim > 0.05:
                    key = (store, ref_id)
                    vec_sim[key] = max(vec_sim.get(key, 0.0), sim)

    # 3. procedures by trigger tokens (full-phrase LIKE misses paraphrases)
    try:
        toks = [t.strip('?.,!"\'').lower() for t in query.split()]
        toks = [t for t in toks if len(t) > 3][:6]
        seen: set[str] = set()
        for tok in toks:
            q = "SELECT id, trigger FROM procedures WHERE lower(trigger) LIKE ?"
            args: tuple = (f"%{tok}%",)
            if namespace is not None:
                q += " AND ns=?"
                args += (namespace,)
            q += " LIMIT 10"
            for r in con.execute(q, args).fetchall():
                if r["id"] not in seen:
                    seen.add(r["id"])
                    # more shared tokens -> higher base
                    overlap = sum(1 for t in toks if t in (r["trigger"] or "").lower())
                    fts_rank[("procedure", r["id"])] = max(
                        fts_rank.get(("procedure", r["id"]), 0.0),
                        min(0.8, 0.4 + 0.15 * overlap))
    except Exception:
        pass

    # 4. rerank union
    keys = set(fts_rank) | set(vec_sim)
    hits: list[RecallHit] = []
    for store, ref_id in keys:
        row = _fetch(con, store, ref_id, namespace)
        if row is None:
            continue
        text, ts, imp, meta = row
        score = (W_VEC * vec_sim.get((store, ref_id), 0.0)
                 + W_FTS * fts_rank.get((store, ref_id), 0.0)
                 + W_IMP * imp + W_REC * _recency(ts))
        hits.append(RecallHit(ref_id, store, text, round(score, 4), ts, meta))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


def _ns_ok(row_ns: str | None, namespace: str | None) -> bool:
    if namespace is None:
        return True
    return (row_ns or "default") == namespace


def _fetch(con: sqlite3.Connection, store: str, ref_id: str,
           namespace: str | None = None):
    if store == "episode":
        r = con.execute("SELECT * FROM episodes WHERE id=?", (ref_id,)).fetchone()
        if not r or not _ns_ok(r["ns"] if "ns" in r.keys() else None, namespace):
            return None
        return (r["text"], r["ts"], float(r["importance"] or 0),
                {"actor": r["actor"], "harness": r["harness"]})
    if store == "fact":
        r = con.execute("SELECT * FROM facts WHERE id=?", (ref_id,)).fetchone()
        if not r or not _ns_ok(r["ns"] if "ns" in r.keys() else None, namespace):
            return None
        try:
            src = json.loads(r["source_episodes"] or "[]")
        except Exception:
            src = []
        return (f"{r['entity']}.{r['key']} = {r['value']}", r["updated"],
                float(r["importance"] or 0), {"entity": r["entity"], "sources": src})
    r = con.execute("SELECT * FROM procedures WHERE id=?", (ref_id,)).fetchone()
    if not r or not _ns_ok(r["ns"] if "ns" in r.keys() else None, namespace):
        return None
    return (f"{r['trigger']} → {r['steps']}", r["updated"], 0.5, {})
