"""v0.2 eval: exact + paraphrase/fuzzy + procedural. Run: uv run pytest tests/test_eval.py -q"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_memory.sdk import Memory

PROVIDER = os.environ.get("AI_MEMORY_EMBED", "hash")

SEED = [
    ("episode", "User's dog is named Biscuit, a golden retriever", {}),
    ("episode", "Decided: deploy on Fridays is banned after the outage", {}),
    ("episode", "User prefers dark mode in all apps", {}),
    ("fact", "Portugal", {"entity": "user", "key": "vacation_2026"}),
    ("fact", "all-MiniLM-L6-v2 with sqlite-vec", {"entity": "project", "key": "embedder"}),
    ("procedure", " inviting", {}),  # placeholder replaced below
]

CASES = [
    # (query, must_contain, kind)
    ("what is the dog's name?", "biscuit", "fuzzy"),
    ("golden retriever dog", "biscuit", "fuzzy"),
    ("can we deploy on friday?", "fridays is banned", "fuzzy"),
    ("dark mode", "dark mode", "exact"),
    ("what theme does user like?", "dark mode", "fuzzy"),
    ("where vacation 2026?", "portugal", "fuzzy"),
    ("which embedder project uses?", "sqlite-vec", "fuzzy"),
    ("how to onboard new contributor?", "onboard", "procedural"),
]


def build(provider: str = PROVIDER) -> Memory:
    tmp = tempfile.mktemp(suffix=".db")
    m = Memory(db_path=tmp, embed_provider=provider)
    m.store("User's dog is named Biscuit, a golden retriever")
    m.store("Decided: deploy on Fridays is banned after the outage")
    m.store("User prefers dark mode in all apps")
    m.store("Portugal", kind="fact", entity="user", key="vacation_2026")
    m.store("all-MiniLM-L6-v2 with sqlite-vec", kind="fact",
            entity="project", key="embedder")
    m.store("Run onboard.sh, then copy .env.example to .env, then uv sync",
            kind="procedure", key="onboard new contributor")
    return m


def score(m: Memory, k: int = 3) -> dict:
    passed, rows = 0, []
    for q, want, kind in CASES:
        hits = m.recall(q, k=k)
        blob = " | ".join(h["text"] for h in hits).lower()
        ok = want.lower() in blob
        passed += ok
        rows.append((ok, kind, q, want, hits[0]["text"][:80] if hits else "<none>"))
    return {"passed": passed, "total": len(CASES), "rows": rows,
            "recall_at_3": round(passed / len(CASES), 3)}


def test_eval_recall():
    m = build()
    r = score(m)
    for ok, kind, q, want, top in r["rows"]:
        print(f"{'PASS' if ok else 'FAIL'} [{kind}] q={q!r} want={want!r} top={top!r}")
    print(f"recall@3 = {r['recall_at_3']} ({r['passed']}/{r['total']}) provider={PROVIDER}")
    # hash fallback must pass exact; ST/real embedder must pass >=6/8
    min_pass = 6 if PROVIDER != "hash" else 4
    assert r["passed"] >= min_pass, r


if __name__ == "__main__":
    m = build()
    r = score(m)
    for ok, kind, q, want, top in r["rows"]:
        print(f"{'PASS' if ok else 'FAIL'} [{kind}] q={q!r} want={want!r} top={top!r}")
    print(f"recall@3 = {r['recall_at_3']} provider={PROVIDER} stats={m.stats()}")
    raise SystemExit(0 if r["passed"] >= 4 else 1)
