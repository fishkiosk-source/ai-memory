"""Temporal context for ai-memory. Adapted from asha-memory's InternalClock.

Stdlib only. Gives recall results a sense of time without changing the
store/recall contract:

- now() / humanize() — snapshots + "3 days ago" phrases
- summarize_hit() — per-hit card: added / last_checked / access_count / stale
- access_log — pre-bump read so hits don't all claim "just now"
- today_summary() / graph_activity() — daily + sliding-window counts

ai-memory mapping (no nodes/edges here):
  created = episodes.ts | facts.updated (fallback) | procedures.updated
  accesses = access_log(ref_id, store, accessed_at)
  queries are not logged separately; access_log volume is the activity signal.
"""

from __future__ import annotations
import sqlite3
import time
from datetime import datetime
from typing import Any


class InternalClock:
    def __init__(self, enabled: bool = True, stale_after_days: int = 7):
        self.enabled = enabled
        self.stale_after_days = max(1, stale_after_days)

    def now(self) -> dict[str, Any]:
        t = time.time()
        dt = datetime.fromtimestamp(t)
        return {
            "epoch": int(t),
            "iso": dt.isoformat(timespec="seconds"),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M:%S"),
            "weekday": dt.strftime("%A"),
        }

    def _day_start_epoch(self, now_epoch: float | None = None) -> float:
        t = now_epoch if now_epoch is not None else time.time()
        dt = datetime.fromtimestamp(t)
        return datetime(dt.year, dt.month, dt.day).timestamp()

    def humanize(self, epoch: float, now_epoch: float | None = None) -> str:
        now = now_epoch if now_epoch is not None else time.time()
        diff = now - epoch
        if diff < 0:
            return "in the future"
        if diff < 60:
            return "just now"
        minutes = diff / 60.0
        if minutes < 60:
            return self._unit(int(minutes), "minute")
        hours = minutes / 60.0
        if hours < 24:
            return self._unit(int(hours), "hour")
        days = hours / 24.0
        if days < 7:
            return self._unit(int(days), "day")
        weeks = days / 7.0
        if weeks < 4.345:
            return self._unit(round(weeks), "week")
        months = days / 30.44
        if months < 12:
            return self._unit(round(months), "month")
        years = days / 365.25
        return self._unit(round(years), "year")

    @staticmethod
    def _unit(count: int, unit: str) -> str:
        if count <= 0:
            count = 1
        label = unit if count == 1 else unit + "s"
        return f"{count} {label} ago"

    def _is_stale(self, created: float | None, last: float | None, now: float) -> bool:
        threshold = self.stale_after_days * 86400.0
        if last is not None:
            return (now - last) > threshold
        if created is not None:
            return (now - created) > threshold
        return False

    def summarize_hit(
        self,
        created: float | None,
        last_accessed: float | None,
        access_count: int = 0,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        now = now_epoch if now_epoch is not None else time.time()
        last = last_accessed if last_accessed is not None else created
        return {
            "added": self.humanize(created, now) if created else None,
            "added_at": created,
            "last_checked": self.humanize(last, now) if last else None,
            "last_checked_at": last,
            "access_count": access_count,
            "stale": self._is_stale(created, last, now),
        }

    # --- DB-aware helpers (ai-memory schema) ---

    @staticmethod
    def _count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
        try:
            return con.execute(sql, params).fetchone()[0]
        except Exception:
            return 0

    def last_accessed_before(
        self, con: sqlite3.Connection, ref_id: str, before_epoch: float
    ) -> float | None:
        try:
            row = con.execute(
                "SELECT MAX(accessed_at) AS la FROM access_log WHERE ref_id=? AND accessed_at<?",
                (ref_id, before_epoch),
            ).fetchone()
            if row and row["la"] is not None:
                return float(row["la"])
        except Exception:
            pass
        return None

    def access_count(self, con: sqlite3.Connection, ref_id: str) -> int:
        return self._count(
            con, "SELECT COUNT(*) FROM access_log WHERE ref_id=?", (ref_id,)
        )

    def log_access(
        self, con: sqlite3.Connection, ref_id: str, store: str, ts: float | None = None
    ) -> None:
        try:
            con.execute(
                "INSERT INTO access_log(ref_id, store, accessed_at) VALUES(?,?,?)",
                (ref_id, store, ts or time.time()),
            )
        except Exception:
            pass

    def today_summary(self, con: sqlite3.Connection) -> dict[str, Any]:
        day_start = self._day_start_epoch()
        out = self.now()
        out["day_start_epoch"] = int(day_start)
        out["episodes_added_today"] = self._count(
            con, "SELECT COUNT(*) FROM episodes WHERE ts>=?", (day_start,)
        )
        out["facts_updated_today"] = self._count(
            con, "SELECT COUNT(*) FROM facts WHERE updated>=?", (day_start,)
        )
        out["procedures_updated_today"] = self._count(
            con, "SELECT COUNT(*) FROM procedures WHERE updated>=?", (day_start,)
        )
        out["rows_accessed_today"] = self._count(
            con,
            "SELECT COUNT(DISTINCT ref_id) FROM access_log WHERE accessed_at>=?",
            (day_start,),
        )
        out["accesses_today"] = self._count(
            con, "SELECT COUNT(*) FROM access_log WHERE accessed_at>=?", (day_start,)
        )
        return out

    def graph_activity(
        self, con: sqlite3.Connection, hours: int = 24
    ) -> dict[str, Any]:
        since = time.time() - hours * 3600.0
        return {
            "since_hours": hours,
            "since_epoch": int(since),
            "episodes_added": self._count(
                con, "SELECT COUNT(*) FROM episodes WHERE ts>=?", (since,)
            ),
            "facts_updated": self._count(
                con, "SELECT COUNT(*) FROM facts WHERE updated>=?", (since,)
            ),
            "procedures_updated": self._count(
                con, "SELECT COUNT(*) FROM procedures WHERE updated>=?", (since,)
            ),
            "rows_accessed": self._count(
                con,
                "SELECT COUNT(DISTINCT ref_id) FROM access_log WHERE accessed_at>=?",
                (since,),
            ),
            "accesses": self._count(
                con, "SELECT COUNT(*) FROM access_log WHERE accessed_at>=?", (since,)
            ),
        }
