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
        embed_provider: str = "hash",
        dim: int = DIM_DEFAULT,
        namespace: str | None = None,
    ):
        cfg = Config()
        self.db_path = db_path or cfg.db_path
        self.dim = dim
        self.embed_provider = embed_provider
        self.namespace = namespace or cfg.namespace
        self._lock = threading.RLock()
        self.con: sqlite3.Connection = connect(self.db_path, dim=dim)
        try:
            self.embedder = get_embedder(embed_provider, dim)
        except Exception:
            from .embeddings import HashEmbedder

            self.embedder = HashEmbedder(dim)

    @property
    def vec_enabled(self) -> bool:
        return has_vec(self.con) and self.embedder is not None

    def _vec(self, text: str) -> list[float] | None:
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
        self, query: str, k: int = 5, namespace: str | None = None
    ) -> list[dict]:
        import time as _time

        from .clock import InternalClock

        clock = InternalClock()
        with self._lock:
            ns = namespace or self.namespace
            t0 = _time.time()
            qvec = self._vec(query)
            hits = R.recall(self.con, query, k, qvec=qvec, namespace=ns)
            out = []
            for h in hits:
                d = dict(h.__dict__)
                try:  # pre-bump read: last access BEFORE this query
                    last = clock.last_accessed_before(self.con, h.ref_id, t0)
                    cnt = clock.access_count(self.con, h.ref_id)
                    d["_clock"] = clock.summarize_hit(h.ts, last, cnt + 1, now_epoch=t0)
                except Exception:
                    pass
                out.append(d)
            try:  # learning loop: reinforce what was useful + access log
                for h in hits:
                    clock.log_access(self.con, h.ref_id, h.store, t0)
                    if h.store == "episode":
                        self.con.execute(
                            "UPDATE episodes SET importance=min(1.0, COALESCE(importance,0.5)+0.05) WHERE id=?",
                            (h.ref_id,),
                        )
                    elif h.store == "fact":
                        self.con.execute(
                            "UPDATE facts SET importance=min(1.0, COALESCE(importance,0.5)+0.05) WHERE id=?",
                            (h.ref_id,),
                        )
                    elif h.store == "procedure":
                        self.con.execute(
                            "UPDATE procedures SET uses=COALESCE(uses,1)+1 WHERE id=?",
                            (h.ref_id,),
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

        n = 0
        ids = [ref_id] if ref_id else []
        if query:
            like = f"%{query}%"
            ids += [
                r[0]
                for r in self.con.execute(
                    "SELECT id FROM episodes WHERE text LIKE ?", (like,)
                ).fetchall()
            ]
            ids += [
                r[0]
                for r in self.con.execute(
                    "SELECT id FROM facts WHERE entity LIKE ? OR key LIKE ? OR value LIKE ?",
                    (like, like, like),
                ).fetchall()
            ]
            ids += [
                r[0]
                for r in self.con.execute(
                    "SELECT id FROM procedures WHERE trigger LIKE ? OR steps LIKE ?",
                    (like, like),
                ).fetchall()
            ]
        for eid in ids:
            for tbl in ("episodes", "facts", "procedures"):
                row = self.con.execute(
                    f"SELECT * FROM {tbl} WHERE id=?", (eid,)
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

        with self._lock:
            rows = T.find(self.con, ref_id, query)
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
    ) -> dict:
        with self._lock:
            out = C.run_once(self.con, limit, vacuum=vacuum, llm=llm, max_llm=max_llm)
        # embed newly created facts/procedures so vec index stays warm
        try:
            rows = self.con.execute(
                "SELECT id, entity, key, value FROM facts ORDER BY updated DESC LIMIT ?",
                (out.get("facts_made", 0),),
            ).fetchall()
            from .db import vec_insert

            for r in rows:
                if (
                    self.con.execute(
                        "SELECT COUNT(*) FROM vec_facts WHERE id=?", (r["id"],)
                    ).fetchone()[0]
                    == 0
                ):
                    vec_insert(
                        self.con,
                        "vec_facts",
                        r["id"],
                        self._vec(f"{r['entity']} {r['key']} {r['value']}"),
                    )
            prows = self.con.execute(
                "SELECT id, trigger, steps FROM procedures ORDER BY updated DESC LIMIT ?",
                (out.get("procedures_made", 0),),
            ).fetchall()
            for r in prows:
                if (
                    self.con.execute(
                        "SELECT COUNT(*) FROM vec_procedures WHERE id=?", (r["id"],)
                    ).fetchone()[0]
                    == 0
                ):
                    vec_insert(
                        self.con,
                        "vec_procedures",
                        r["id"],
                        self._vec(f"{r['trigger']} {r['steps']}"),
                    )
            self.con.commit()
        except Exception:
            pass
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
