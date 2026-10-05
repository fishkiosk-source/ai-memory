"""Prefrontal cortex: distill episodes -> facts/procedures, dedupe, decay, vacuum.

Rule-based extractors (offline). An LLM hook can replace `extract()` in future
without changing the consolidate/vacuum/loop contract.
"""

from __future__ import annotations
import re
import sqlite3
import time

# --- extractors -------------------------------------------------------------

REMEMBER_RE = re.compile(
    r"(?:remember|prefer|my \w+ is|i like|i dislike)\s*[:\-]?\s*(.+)", re.I
)
ATTR_RE = re.compile(r"my\s+(\w[\w\- ]{1,30}?)\s+is\s+(.+)", re.I)
DECIDED_RE = re.compile(r"(?:decided|decision)\s*[:\-]\s*(.+)", re.I)
FIX_RE = re.compile(
    r"(?:error|bug|failure)[^.]*?(?:fixed|fix|resolved|solved)[^.]*", re.I
)
RUNBOOK_RE = re.compile(
    r"(?:steps?|procedure|runbook|to\s+.+?,\s*(?:run|do|execute))\s*:\s*(.{20,400})",
    re.I,
)
# Looser procedural signals (v0.8.1): numbered lists, first/then chains,
# how-to imperatives, shell sequences. Prod DB had 31 episodes / 0 procedures
# because RUNBOOK_RE alone almost never fires on real session summaries.
NUMBERED_LIST_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+\S.{8,200}"
    r"(?:\n\s*(?:\d+[.)]|[-*])\s+\S.{8,200}){1,}",
)
FIRST_THEN_RE = re.compile(
    r"\bfirst\b.{5,120}?\bthen\b.{5,200}",
    re.I | re.S,
)
HOWTO_RE = re.compile(
    r"how\s+to\s+(.{4,80}?)[.:\n]\s*(.{20,400})",
    re.I | re.S,
)
SHELL_SEQ_RE = re.compile(
    r"\brun\b\s+`?[\w./~$-][^\n]{4,140}?(?:&&|;|\bthen\b|\bfollowed by\b)[^\n]{4,200}",
    re.I,
)


def _slug(text: str, n: int = 6) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", text.lower()).split()
    return "_".join(words[:n]) or "note"


def extract(text: str) -> dict:
    """Return {'facts': [(entity,key,value,conf)], 'procedures': [(trigger,steps)]}."""
    facts, procs = [], []
    t = text.strip()
    if len(t) < 4:
        return {"facts": facts, "procedures": procs}

    m = ATTR_RE.search(t)  # highest quality: my X is Y
    if m and len(m.group(2).strip()) > 1:
        facts.append(
            (
                "user",
                m.group(1).strip().lower().replace(" ", "_")[:40],
                m.group(2).strip()[:280],
                0.85,
            )
        )
    elif (m := REMEMBER_RE.search(t)) and len(m.group(1).strip()) > 2:
        facts.append(("user", _slug(m.group(1)), m.group(1).strip()[:280], 0.7))

    if (m := DECIDED_RE.search(t)) and len(m.group(1).strip()) > 2:
        facts.append(("decisions", _slug(m.group(1)), m.group(1).strip()[:280], 0.75))

    if FIX_RE.search(t):
        facts.append(("learnings", _slug(t), t[:280], 0.65))

    if m := RUNBOOK_RE.search(t):
        trigger = t[: m.start()].strip()[-80:] or _slug(t).replace("_", " ")
        procs.append((trigger, m.group(1).strip()))
    if not procs:
        m = NUMBERED_LIST_RE.search(t)
        if m:
            trigger = t[: m.start()].strip()[-80:] or _slug(t).replace("_", " ")
            procs.append((trigger or "numbered steps", m.group(0).strip()[:400]))
        elif m := FIRST_THEN_RE.search(t):
            trigger = t[: m.start()].strip()[-80:] or _slug(t).replace("_", " ")
            procs.append((trigger or "first-then chain", m.group(0).strip()[:400]))
        elif m := HOWTO_RE.search(t):
            procs.append(
                (f"how to {m.group(1).strip()}"[:120], m.group(2).strip()[:400])
            )
        elif m := SHELL_SEQ_RE.search(t):
            trigger = t[: m.start()].strip()[-80:] or _slug(t).replace("_", " ")
            procs.append((trigger or "shell sequence", m.group(0).strip()[:400]))
    return {"facts": facts, "procedures": procs}


# --- pipeline ---------------------------------------------------------------


def run_once(
    con: sqlite3.Connection,
    limit: int = 50,
    vacuum: bool = False,
    llm: str | None = None,
    max_llm: int = 20,
) -> dict:
    import os as _os
    from . import store as S

    provider = llm or _os.environ.get("AI_MEMORY_LLM", "off")
    llm_on = provider not in (None, "", "off", "false", "0")
    llm_calls, llm_facts = 0, 0

    rows = con.execute(
        "SELECT * FROM episodes WHERE processed=0 ORDER BY ts LIMIT ?", (limit,)
    ).fetchall()
    facts_made, procs_made, processed = 0, 0, 0
    for r in rows:
        processed += 1
        out = extract(r["text"])
        if (
            not out["facts"]
            and not out["procedures"]
            and llm_on
            and llm_calls < max_llm
            and (r["importance"] or 0) >= 0.4
        ):
            from . import llm as LLM

            out = LLM.extract_with_llm(r["text"], provider=provider)
            llm_calls += 1
            llm_facts += len(out["facts"]) + len(out["procedures"])
        ns = r["ns"] if "ns" in r.keys() and r["ns"] else "default"
        for entity, key, value, conf in out["facts"]:
            S.store_fact(
                con,
                entity,
                key,
                value,
                confidence=conf,
                source_episodes=[r["id"]],
                namespace=ns,
            )
            facts_made += 1
        for trigger, steps in out["procedures"]:
            S.store_procedure(con, trigger=trigger, steps=steps, namespace=ns)
            procs_made += 1
        con.execute("UPDATE episodes SET processed=1 WHERE id=?", (r["id"],))
    # passive decay of trivia never promoted
    con.execute(
        "UPDATE episodes SET importance = importance*0.99 WHERE importance < 0.2"
    )
    con.commit()
    res = {
        "processed": processed,
        "facts_made": facts_made,
        "procedures_made": procs_made,
        "llm_calls": llm_calls,
        "llm_facts": llm_facts,
    }
    if vacuum:
        res["vacuumed"] = vacuum_old(con)
    return res


def consolidate(con: sqlite3.Connection, limit: int = 50) -> dict:
    """Backwards-compatible entry (v0.1/v0.2 API)."""
    return run_once(con, limit)


def vacuum_old(
    con: sqlite3.Connection, min_importance: float = 0.05, older_than_days: int = 90
) -> int:
    cutoff = time.time() - older_than_days * 86400
    cur = con.execute(
        "DELETE FROM episodes WHERE importance<? AND ts<? AND processed=1",
        (min_importance, cutoff),
    )
    n = cur.rowcount
    con.execute("DELETE FROM episodes_fts WHERE id NOT IN (SELECT id FROM episodes)")
    try:  # keep vec index consistent when available
        con.execute(
            "DELETE FROM vec_episodes WHERE id NOT IN (SELECT id FROM episodes)"
        )
        con.execute("DELETE FROM vec_facts WHERE id NOT IN (SELECT id FROM facts)")
        con.execute(
            "DELETE FROM vec_procedures WHERE id NOT IN (SELECT id FROM procedures)"
        )
    except Exception:
        pass
    con.commit()
    return n


def vacuum(
    con: sqlite3.Connection, min_importance: float = 0.05, older_than_days: int = 90
) -> int:
    return vacuum_old(con, min_importance, older_than_days)
