"""Regression: SDK must be usable from multiple threads (MCP worker threads)."""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory


def test_threaded_store_recall():
    m = Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")
    errors: list = []

    def worker(n: int):
        try:
            for i in range(10):
                m.store(f"thread {n} fact number {i} about pineapples")
            m.recall("pineapples", k=3)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert m.stats()["episodes"] == 40, m.stats()


if __name__ == "__main__":
    test_threaded_store_recall()
    print("OK test_threaded_store_recall")
