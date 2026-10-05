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


def test_recall_attaches_clock_and_logs_access():
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    eid = m.store("Remember: user prefers dark mode", namespace="clk")
    h1 = m.recall("dark mode", namespace="clk")
    assert h1 and "_clock" in h1[0], h1
    assert h1[0]["_clock"]["access_count"] >= 1
    assert h1[0]["_clock"]["added"] is not None
    # second recall: last_checked reflects the FIRST access, not "just now" bug
    time.sleep(0.05)
    h2 = m.recall("dark mode", namespace="clk")
    assert h2[0]["_clock"]["access_count"] >= 2, h2
    assert m.stats()["accesses"] >= 2


def test_temporal_summary():
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    m.store("Remember: temporal probe", namespace="clk")
    m.recall("temporal probe", namespace="clk")
    t = m.temporal()
    assert "now" in t and "today" in t and "activity" in t, t
    assert t["today"]["episodes_added_today"] >= 1
    assert t["activity"]["accesses"] >= 1


if __name__ == "__main__":
    for fn in (
        test_humanize_buckets,
        test_stale_logic,
        test_recall_attaches_clock_and_logs_access,
        test_temporal_summary,
    ):
        fn()
        print(f"OK {fn.__name__}")
