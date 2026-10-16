"""Temporal clock: humanize buckets, stale, pre-bump last_checked, activity."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.clock import InternalClock
from ai_memory.sdk import Memory


def test_humanize_buckets():
    c = InternalClock()
    now = time.time()
    assert c.humanize(now - 30, now) == "just now"
    assert c.humanize(now - 180, now) == "3 minutes ago"
    assert c.humanize(now - 5 * 3600, now) == "5 hours ago"
    assert c.humanize(now - 3 * 86400, now) == "3 days ago"
    assert c.humanize(now + 100, now) == "in the future"


def test_stale_logic():
    c = InternalClock(stale_after_days=7)
    now = time.time()
    assert c.summarize_hit(now - 10 * 86400, now - 10 * 86400, 0, now)["stale"] is True
    assert c.summarize_hit(now - 60, now - 60, 0, now)["stale"] is False


def test_recall_logs_access_without_mutating_content():
    # v0.9 contract: recall logs one access row per returned hit (frequency
    # signal) but never mutates row content: no clock annotation in text,
    # no importance/uses bumps.
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    eid = m.store("Remember: user prefers dark mode", namespace="clk")
    imp0 = m.con.execute(
        "SELECT importance FROM episodes WHERE id=?", (eid,)
    ).fetchone()[0]
    before = m.stats()["accesses"]
    h1 = m.recall("dark mode", namespace="clk")
    assert h1 and "_clock" not in h1[0]["text"], h1
    assert m.stats()["accesses"] == before + len(h1), m.stats()
    imp1 = m.con.execute(
        "SELECT importance FROM episodes WHERE id=?", (eid,)
    ).fetchone()[0]
    assert imp1 == imp0, (imp0, imp1)
    h2 = m.recall("dark mode", namespace="clk")
    assert m.stats()["accesses"] == before + len(h1) + len(h2)
    # frequency boost: same top hit scores higher the second time
    assert h2[0]["ref_id"] == h1[0]["ref_id"], (h1, h2)
    assert h2[0]["score"] >= h1[0]["score"], (h1[0], h2[0])


def test_temporal_summary():
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    m.store("Remember: temporal probe", namespace="clk")
    hits = m.recall("temporal probe", namespace="clk")
    t = m.temporal()
    assert "now" in t and "today" in t and "activity" in t, t
    assert t["today"]["episodes_added_today"] >= 1
    # v0.9: recall logs its hits, so activity reflects them
    assert t["activity"]["accesses"] == len(hits) >= 1, t


if __name__ == "__main__":
    for fn in (
        test_humanize_buckets,
        test_stale_logic,
        test_recall_logs_access_without_mutating_content,
        test_temporal_summary,
    ):
        fn()
        print(f"OK {fn.__name__}")
