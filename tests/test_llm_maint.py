"""v0.6 eval: LLM hook (mocked, offline) + DB maintenance."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ai_memory.llm as LLM
from ai_memory.sdk import Memory

CANNED = ('{"facts": [{"entity": "project", "key": "staging_port", '
          '"value": "staging runs on port 8080", "confidence": 0.8}], '
          '"procedures": []}')


def _mem() -> Memory:
    return Memory(db_path=tempfile.mktemp(suffix=".db"), embed_provider="hash")


def test_llm_off_by_default(monkeypatch):
    monkeypatch.setattr(LLM, "complete", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("LLM must not be called when off")))
    m = _mem()
    m.store("The staging server runs on port 8080 and restarts nightly")
    res = m.consolidate()
    assert res["llm_calls"] == 0, res
    assert m.stats()["facts"] == 0, m.stats()


def test_llm_distills_rule_miss(monkeypatch):
    monkeypatch.setattr(LLM, "complete", lambda *a, **k: CANNED)
    m = _mem()
    m.store("The staging server runs on port 8080 and restarts nightly")
    res = m.consolidate(llm="ollama")
    assert res["llm_calls"] == 1 and res["llm_facts"] >= 1, res
    hits = m.recall("staging port", k=3)
    assert any("8080" in h["text"] for h in hits), hits


def test_llm_malformed_is_safe(monkeypatch):
    monkeypatch.setattr(LLM, "complete", lambda *a, **k: "not json {{{")
    m = _mem()
    m.store("The staging server runs on port 8080 and restarts nightly")
    res = m.consolidate(llm="openai")  # must not raise
    assert res["llm_calls"] == 1 and m.stats()["facts"] == 0, (res, m.stats())


def test_llm_skips_trivia(monkeypatch):
    calls: list = []
    monkeypatch.setattr(LLM, "complete",
                        lambda *a, **k: calls.append(1) or CANNED)
    m = _mem()
    m.store("hello", importance=0.1)  # below 0.4 threshold
    res = m.consolidate(llm="ollama")
    assert calls == [] and res["llm_calls"] == 0, res


def test_maintenance():
    m = _mem()
    m.store("Remember: maintenance keeps the DB fast")
    m.store("Temporary episode to churn")
    m.consolidate()
    # create orphans the way crashes do: raw deletes bypassing forget()
    m.con.execute("DELETE FROM episodes")
    m.con.commit()
    res = m.maintenance()
    assert res["integrity"] == "ok", res
    assert res["orphans_removed"] >= 1, res
    assert res["freelist_after"] == 0, res
    assert set(res) >= {"integrity", "orphans_removed", "bytes_before",
                        "bytes_after", "bytes_reclaimed"}, res


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
