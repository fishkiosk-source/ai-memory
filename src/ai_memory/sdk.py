"""Thin in-process SDK — same API as MCP tools."""

from __future__ import annotations
import json
import sqlite3
import threading

from .config import Config
from .db import connect, has_vec, vec_counts, vec_delete
from .embeddings import get_embedder, DIM_DEFAULT
from . import store as S, recall as R, consolidator as C


class Memory:
    def __init__(
        self,
        db_path: str | None = None,
        embed_provider: str | None = None,
        dim: int = DIM_DEFAULT,
        namespace: str | None = None,
    ):
        cfg = Config()
        self.db_path = db_path or cfg.db_path
        self.dim = dim
        # None means "whatever AI_MEMORY_EMBED says": hardcoding "hash" here made
        # SDK/CLI callers silently write hash vectors into an ST database, and
        # cross-embedder cosines are ~0, so those rows never match semantically.
        self.embed_provider = embed_provider or cfg.embed_provider
        self.namespace = namespace or cfg.namespace
        self._lock = threading.RLock()
        self.con: sqlite3.Connection = connect(self.db_path, dim=dim)
        # A failed embedder must NOT silently become hash: hash vectors are not
        # comparable with ST ones (cosine ~0), so writing them into an ST index
        # poisons recall instead of degrading it. Non-hash providers fail closed
        # (no vectors, text-only rows) and backfill can fill them later.
        self.embed_error: str | None = None
        try:
            self.embedder = get_embedder(self.embed_provider, dim)
        except Exception as e:  # noqa: BLE001 - provider load is env-dependent
            self.embed_error = f"{type(e).__name__}: {e}"
            if (self.embed_provider or "hash").lower() == "hash":
                from .embeddings import HashEmbedder

                self.embedder = HashEmbedder(dim)
            else:
                self.embedder = None

    @property
    def vec_enabled(self) -> bool:
        return has_vec(self.con) and self.embedder is not None

    def _vec(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            v = self.embedder.embed(text)
            if v is not None and len(v) == self.dim:
                return v
            return None
        except Exception:
            return None

    # --- tools (mirror MCP) ---
    def store(
        self,
        text: str,
        kind: str = "episode",
        actor: str = "user",
        harness: str = "opencode",
        entity: str = "user",
        key: str = "note",
        importance: float | None = None,
        namespace: str | None = None,
        session_id: str | None = None,
    ) -> str:
        with self._lock:
            return self._store(
                text,
                kind,
                actor,
                harness,
                entity,
                key,
                importance,
                namespace,
                session_id,
            )

    def _store(
        self,
        text: str,
        kind: str = "episode",
        actor: str = "user",
        harness: str = "opencode",
        entity: str = "user",
        key: str = "note",
        importance: float | None = None,
        namespace: str | None = None,
        session_id: str | None = None,
    ) -> str:
        ns = namespace or self.namespace
        if kind == "episode":
            import re as _re

            pm = _re.match(
                r"\s*(?:proc|procedure)\s*:\s*(.+?)\s*->\s*(.+)",
                text,
                _re.S,
            )
            if pm and len(pm.group(1).strip()) > 1 and len(pm.group(2).strip()) > 1:
                return S.store_procedure(
                    self.con,
                    trigger=pm.group(1).strip()[:120],
                    steps=pm.group(2).strip()[:800],
                    namespace=ns,
                    vec=self._vec(f"{pm.group(1)} {pm.group(2)}"),
                )
        if kind == "fact":
            return S.store_fact(
                self.con,
                entity,
                key,
                text,
                importance=importance,
                vec=self._vec(f"{entity} {key} {text}"),
                namespace=ns,
            )
        if kind == "procedure":
            return S.store_procedure(
                self.con,
                trigger=key,
                steps=text,
                namespace=ns,
                vec=self._vec(f"{key} {text}"),
            )
        sess = session_id or ns
        S.push_working(self.con, sess, actor, text, namespace=ns)
        return S.store_episode(
            self.con,
            text,
            actor,
            harness,
            importance,
            vec=self._vec(text),
            namespace=ns,
        )

    def recall(
        self,
        query: str,
        k: int = 5,
        namespace: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict]:
        """Hybrid FTS + sqlite-vec + importance + recency + frequency rerank.

        Logs one access row per returned hit so frequently-needed rows rank
        higher over time (frequency saturates at ~20 accesses).
        """
        import time as _time

        with self._lock:
            ns = namespace or self.namespace
            qvec = self._vec(query)
            hits = R.recall(
                self.con,
                query,
                k,
                qvec=qvec,
                namespace=ns,
                since=since,
                until=until,
            )
            out = [dict(h.__dict__) for h in hits]
            if out:
                try:
                    now = _time.time()
                    self.con.executemany(
                        "INSERT INTO access_log(ref_id,store,accessed_at)"
                        " VALUES(?,?,?)",
                        [(h["ref_id"], h["store"], now) for h in out],
                    )
                    self.con.commit()
                except Exception:
                    pass
            return out

    def temporal(self, hours: int = 24) -> dict:
        from .clock import InternalClock

        with self._lock:
            clock = InternalClock()
            return {
                "now": clock.now(),
                "today": clock.today_summary(self.con),
                "activity": clock.graph_activity(self.con, hours=hours),
            }

    def recent(self, limit: int = 5, namespace: str | None = None) -> list[dict]:
        with self._lock:
            return R.recent_working(self.con, namespace or self.namespace, limit=limit)

    def forget(self, ref_id: str | None = None, query: str | None = None) -> int:
        """Soft-delete into trash (30d grace, see restore/vacuum)."""
        with self._lock:
            return self._forget(ref_id, query)

    def _forget(self, ref_id: str | None = None, query: str | None = None) -> int:
        from . import trash as T

        ns = self.namespace or "default"
        n = 0
        ids = []
        if ref_id:
            ids.append(ref_id)
        if query:
            like = f"%{query}%"
            ids += [
                r[0]
                for r in self.con.execute(
                    "SELECT id FROM episodes WHERE text LIKE ? AND ns=?",
                    (like, ns),
                ).fetchall()
            ]
            ids += [
                r[0]
                for r in self.con.execute(
                    "SELECT id FROM facts WHERE (entity LIKE ? OR key LIKE ? OR value LIKE ?) AND ns=?",
                    (like, like, like, ns),
                ).fetchall()
            ]
            ids += [
                r[0]
                for r in self.con.execute(
                    "SELECT id FROM procedures WHERE (trigger LIKE ? OR steps LIKE ?) AND ns=?",
                    (like, like, ns),
                ).fetchall()
            ]
        for eid in ids:
            for tbl in ("episodes", "facts", "procedures"):
                row = self.con.execute(
                    f"SELECT * FROM {tbl} WHERE id=? AND ns=?", (eid, ns)
                ).fetchone()
                if row:
                    T.stash(self.con, tbl, row)
                    self.con.execute(f"DELETE FROM {tbl} WHERE id=?", (eid,))
                    n += 1
            self.con.execute("DELETE FROM episodes_fts WHERE id=?", (eid,))
            self.con.execute("DELETE FROM facts_fts WHERE id=?", (eid,))
            vec_delete(self.con, "vec_episodes", eid)
            vec_delete(self.con, "vec_facts", eid)
            vec_delete(self.con, "vec_procedures", eid)
        self.con.commit()
        return n

    def restore(self, ref_id: str | None = None, query: str | None = None) -> int:
        """Bring back trashed rows (searches trash by id or text)."""
        from . import trash as T
        from .db import vec_insert

        ns = self.namespace or "default"
        with self._lock:
            rows = T.find(self.con, ref_id, query)
            # scope to this instance's namespace so restore never crosses namespaces
            rows = [
                t for t in rows if (json.loads(t["row"]).get("ns") or "default") == ns
            ]
            n = 0
            for t in rows:
                row = json.loads(t["row"])
                cols = ", ".join(row.keys())
                qs = ", ".join("?" for _ in row)
                self.con.execute(
                    f"INSERT OR REPLACE INTO {t['tbl']}({cols}) VALUES({qs})",
                    tuple(row.values()),
                )
                if t["tbl"] == "episodes":
                    self.con.execute(
                        "INSERT OR REPLACE INTO episodes_fts(id,text) VALUES(?,?)",
                        (row["id"], row["text"]),
                    )
                    vec_insert(
                        self.con, "vec_episodes", row["id"], self._vec(row["text"])
                    )
                elif t["tbl"] == "facts":
                    self.con.execute(
                        "INSERT OR REPLACE INTO facts_fts(id,entity,key,value) VALUES(?,?,?,?)",
                        (row["id"], row["entity"], row["key"], row["value"]),
                    )
                    vec_insert(
                        self.con,
                        "vec_facts",
                        row["id"],
                        self._vec(f"{row['entity']} {row['key']} {row['value']}"),
                    )
                elif t["tbl"] == "procedures":
                    vec_insert(
                        self.con,
                        "vec_procedures",
                        row["id"],
                        self._vec(f"{row.get('trigger', '')} {row.get('steps', '')}"),
                    )
                T.remove(self.con, t["id"])
                n += 1
            self.con.commit()
            return n

    def consolidate(
        self,
        limit: int = 50,
        vacuum: bool = False,
        llm: str | None = None,
        max_llm: int = 20,
        reprocess: bool = False,
    ) -> dict:
        with self._lock:
            out = C.run_once(
                self.con,
                limit,
                vacuum=vacuum,
                llm=llm,
                max_llm=max_llm,
                reprocess=reprocess,
                embed_fn=self._vec,
            )
        return out

    def vacuum(self, older_than_days: int = 90, trash_days: int = 30) -> int:
        with self._lock:
            n = C.vacuum_old(self.con, older_than_days=older_than_days)
            from . import trash as T
            from . import store as S

            n += T.purge(self.con, older_than_days=trash_days)
            try:
                n += S.prune_working(self.con, older_than_days=7)
            except Exception:
                pass
            try:  # drop access rows for deleted refs + very old touches
                import time as _time

                cur = self.con.execute(
                    "DELETE FROM access_log WHERE ref_id NOT IN "
                    "(SELECT id FROM episodes UNION SELECT id FROM facts "
                    "UNION SELECT id FROM procedures)"
                )
                n += cur.rowcount
                cur = self.con.execute(
                    "DELETE FROM access_log WHERE accessed_at<?",
                    (_time.time() - 90 * 86400,),
                )
                n += cur.rowcount
                self.con.commit()
            except Exception:
                pass
            return n

    def maintenance(self) -> dict:
        from . import maintenance as M

        with self._lock:
            return M.run_maintenance(self.con)

    def export(self, path: str) -> dict:
        from . import portability as P

        with self._lock:
            return P.export_db(self.con, path)

    def import_(self, path: str) -> dict:
        from . import portability as P

        with self._lock:
            return P.import_db(self.con, path, embed_fn=self._vec)

    def backup(self, dest: str | None = None, keep: int | None = None) -> str:
        from .portability import backup_db

        with self._lock:
            self.con.commit()
            return backup_db(self.db_path, dest, keep=keep)

    def backfill_vecs(self) -> dict:
        """Recompute vectors for rows missing from vec tables. Returns counts."""
        from .db import vec_insert

        with self._lock:
            out = {}
            jobs = (
                ("episodes", "vec_episodes", lambda r: r["text"]),
                (
                    "facts",
                    "vec_facts",
                    lambda r: f"{r['entity']} {r['key']} {r['value']}",
                ),
                (
                    "procedures",
                    "vec_procedures",
                    lambda r: (
                        f"{r['trigger'] if 'trigger' in r.keys() else ''} {r['steps'] if 'steps' in r.keys() else ''}"
                    ),
                ),
            )
            for base, vec_tbl, text_fn in jobs:
                try:
                    is_fact = base == "facts"
                    # facts: fill only active rows, evict vec slots of non-active
                    missing = [
                        r[0]
                        for r in self.con.execute(
                            f"SELECT id FROM {base} WHERE id NOT IN "
                            f"(SELECT id FROM {vec_tbl})"
                            + (
                                " AND (status IS NULL OR status='active')"
                                if is_fact
                                else ""
                            )
                        ).fetchall()
                    ]
                    if is_fact:
                        evicted = 0
                        for r in self.con.execute(
                            "SELECT id FROM vec_facts"
                        ).fetchall():
                            row = self.con.execute(
                                "SELECT status FROM facts WHERE id=?", (r["id"],)
                            ).fetchone()
                            if row and (row["status"] or "active") != "active":
                                vec_delete(self.con, "vec_facts", r["id"])
                                evicted += 1
                        out.setdefault("facts_evicted", evicted)
                except Exception:
                    missing = []
                done, failed = 0, 0
                for mid in missing:
                    row = self.con.execute(
                        f"SELECT * FROM {base} WHERE id=?", (mid,)
                    ).fetchone()
                    if row is None:
                        continue
                    try:
                        vec = self._vec(text_fn(row))
                    except Exception:
                        vec = None
                    if vec is None:
                        failed += 1
                        continue
                    try:
                        vec_insert(self.con, vec_tbl, mid, vec)
                        done += 1
                    except Exception:
                        failed += 1
                out[base] = {"missing": len(missing), "filled": done, "failed": failed}
            try:
                self.con.commit()
            except Exception:
                pass
            return out

    def stats(self) -> dict:
        with self._lock:
            q = lambda s: self.con.execute(s).fetchone()[0]
            d = {
                "episodes": q("SELECT COUNT(*) FROM episodes"),
                "facts": q("SELECT COUNT(*) FROM facts"),
                "procedures": q("SELECT COUNT(*) FROM procedures"),
                "working": q("SELECT COUNT(*) FROM working"),
                "accesses": q("SELECT COUNT(*) FROM access_log"),
                "unprocessed": q("SELECT COUNT(*) FROM episodes WHERE processed=0"),
                "db": self.db_path,
                "embedder": type(self.embedder).__name__,
                "dim": self.dim,
            }
            d.update(vec_counts(self.con))
            return d

    def doctor_facts(
        self, namespace: str | None = None, sim_threshold: float = 0.85
    ) -> dict:
        """Read-only fact audit (see doctor.py)."""
        from .doctor import fact_audit

        with self._lock:
            return fact_audit(
                self.con,
                embed_fn=self._vec,
                namespace=namespace,
                sim_threshold=sim_threshold,
            )

    def supersede_fact(self, old_ref_id: str, new_ref_id: str) -> dict:
        """Owner verdict: mark old fact superseded by new, drop dead slots."""
        with self._lock:
            from .store import _is_active, _supersede_row

            for rtid in (old_ref_id, new_ref_id):
                r = self.con.execute(
                    "SELECT id,status FROM facts WHERE id=?", (rtid,)
                ).fetchone()
                if r is None:
                    raise ValueError(f"fact {rtid} not found")
                if rtid == old_ref_id and not _is_active(r["status"]):
                    raise ValueError(f"fact {rtid} already not active")
                if rtid == new_ref_id and not _is_active(r["status"]):
                    raise ValueError(f"replacement {rtid} must be active")
            if old_ref_id == new_ref_id:
                raise ValueError("cannot supersede a fact with itself")
            _supersede_row(self.con, old_ref_id, new_ref_id)
            self.con.commit()
            return {"superseded": old_ref_id, "by": new_ref_id}
