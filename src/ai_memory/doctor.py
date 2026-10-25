"""Read-only diagnostics: fact audit, dead-slot report (T1.1, see docs/plan.md).

Never mutates. Reuses the SDK embedder so thresholds are comparable across
runners: same FactSet must audit identically under hash vs ST.
"""

from __future__ import annotations
import sqlite3

from .embeddings import cosine


def _is_active(status: str | None) -> bool:
    return (status or "active") == "active"


def _dead_slot(status: str | None) -> bool:
    """Index entry is dead when its base row is gone OR not active."""
    return status is None or not _is_active(status)


def fact_audit(
    con: sqlite3.Connection,
    embed_fn=None,
    namespace: str | None = None,
    sim_threshold: float = 0.85,
) -> dict:
    """Audit facts. Returns conflicts, dead slots, summary counts.

    conflicts: pairs of active facts, same entity+ns, semantic sim >=
    sim_threshold, different key. Cross-key collisions are the blind spot;
    same-key actives shouldn't exist at all (write path guarantees one per key)
    and get reported as `same_key_dupes` if they do.
    """
    active: list[dict] = []
    superseded_rows: list[dict] = []
    for r in con.execute(
        "SELECT id,entity,key,value,ns,status,updated FROM facts ORDER BY entity,ns"
    ).fetchall():
        ns = r["ns"] or "default"
        if namespace is not None and ns != namespace:
            continue
        rec = {
            "id": r["id"],
            "entity": r["entity"],
            "key": r["key"],
            "value": r["value"],
            "ns": ns,
            "updated": r["updated"],
        }
        if _is_active(r["status"]):
            active.append(rec)
        else:
            superseded_rows.append(rec)

    # embed once, pairwise-compare (prod is ~51 facts, brute force fine)
    if embed_fn is not None:
        for rec in active:
            try:
                rec["_vec"] = embed_fn(f"{rec['entity']} {rec['key']} {rec['value']}")
            except Exception:
                rec["_vec"] = None

    conflicts: list[dict] = []
    same_key_dupes: list[dict] = []
    for i, a in enumerate(active):
        for b in active[i + 1 :]:
            if a["ns"] != b["ns"] or a["entity"] != b["entity"]:
                continue
            if a["key"] == b["key"]:
                same_key_dupes.append(
                    {"a": _clean(a), "b": _clean(b), "reason": "two active same key"}
                )
                continue
            sim = None
            if a.get("_vec") and b.get("_vec"):
                sim = cosine(a["_vec"], b["_vec"])
            if sim is not None and sim >= sim_threshold:
                conflicts.append(
                    {
                        "a": _clean(a),
                        "b": _clean(b),
                        "sim": round(sim, 4),
                    }
                )

    # dead slots: non-active (or missing) fact rows still holding index entries
    fact_status = {
        r["id"]: r["status"]
        for r in con.execute("SELECT id,status FROM facts").fetchall()
    }
    dead_fts = [
        r["id"]
        for r in con.execute("SELECT id FROM facts_fts").fetchall()
        if _dead_slot(fact_status.get(r["id"]))
    ]
    dead_vec: list[str] = []
    missing_vec: list[str] = []
    try:
        from .db import has_vec

        if has_vec(con):
            for r in con.execute("SELECT id FROM vec_facts").fetchall():
                if _dead_slot(fact_status.get(r["id"])):
                    dead_vec.append(r["id"])
            vec_ids = {
                r["id"] for r in con.execute("SELECT id FROM vec_facts").fetchall()
            }
            # only active rows: backfill_vecs deliberately skips superseded
            # facts (T1.3), so counting them would be a permanent false alarm
            missing_vec = [
                r["id"]
                for r in con.execute("SELECT id,status FROM facts").fetchall()
                if r["id"] not in vec_ids and _is_active(r["status"])
            ]
    except Exception:
        pass

    return {
        "namespace": namespace or "all",
        "active_facts": len(active),
        "superseded": len(superseded_rows),
        "conflicts": conflicts,
        "same_key_dupes": same_key_dupes,
        "dead_fts_slots": dead_fts,
        "dead_vec_slots": dead_vec,
        "missing_vec_rows": missing_vec,
        "sim_threshold": sim_threshold,
    }


def _clean(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "_vec"}


def audit_report(a: dict) -> str:
    """Human-readable summary of a fact_audit dict."""
    lines = [
        f"facts audit [{a['namespace']}]: active={a['active_facts']} "
        f"superseded={a['superseded']} conflicts={len(a['conflicts'])} "
        f"same_key_dupes={len(a['same_key_dupes'])} "
        f"dead_fts={len(a['dead_fts_slots'])} dead_vec={len(a['dead_vec_slots'])}"
    ]
    for c in a["conflicts"]:
        lines.append(
            f"  conflict sim={c['sim']}\n"
            f"    A {c['a']['entity']}.{c['a']['key']} = {c['a']['value'][:120]}\n"
            f"    B {c['b']['entity']}.{c['b']['key']} = {c['b']['value'][:120]}"
        )
    for d in a["same_key_dupes"]:
        lines.append(f"  same-key: {d['a']['entity']}.{d['a']['key']} ({d['reason']})")
    for slot, ids in (
        ("dead_fts", a["dead_fts_slots"]),
        ("dead_vec", a["dead_vec_slots"]),
    ):
        if ids:
            lines.append(
                f"  {slot}: {', '.join(ids[:10])}{', …' if len(ids) > 10 else ''}"
            )
    if a["missing_vec_rows"]:
        lines.append(
            f"  missing vec rows: {len(a['missing_vec_rows'])} — run backfill-vecs"
        )
    return "\n".join(lines)
