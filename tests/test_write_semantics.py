"""T1.2/T1.3/T2.4: write-path semantic conflict resolution + merge."""

import sys

sys.path.insert(0, "src")

from ai_memory import db as DB
from ai_memory import recall as R
from ai_memory import store as S
from ai_memory.embeddings import HashEmbedder


def _setup(tmp_path):
    con = DB.connect(str(tmp_path / "t.db"), dim=64)
    emb = HashEmbedder(64)
    return con, emb


def test_cross_key_collision_auto_supersedes(tmp_path):
    con, emb = _setup(tmp_path)
    old_id = S.store_fact(
        con,
        "user",
        "model-table-old",
        "FINAL measured table strict accuracy comparison",
        vec=emb.embed(
            "user model-table FINAL measured table strict accuracy comparison"
        ),
    )
    new_id = S.store_fact(
        con,
        "user",
        "model-scorecard",
        "FINAL measured table strict accuracy comparison",
        vec=emb.embed(
            "user model-scorecard FINAL measured table strict accuracy comparison"
        ),
    )
    old = con.execute(
        "SELECT status,superseded_by FROM facts WHERE id=?", (old_id,)
    ).fetchone()
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new_id
    # T1.3: dead slots removed
    assert (
        con.execute("SELECT COUNT(*) FROM facts_fts WHERE id=?", (old_id,)).fetchone()[
            0
        ]
        == 0
    )
    assert (
        con.execute("SELECT COUNT(*) FROM vec_facts WHERE id=?", (old_id,)).fetchone()[
            0
        ]
        == 0
    )
    # recall shows only the new one
    hits = R.recall(
        con, "FINAL measured table", 5, qvec=emb.embed("FINAL measured table strict")
    )
    assert [h.ref_id for h in hits if h.store == "fact"] == [new_id]


def test_confident_old_fact_not_superseded(tmp_path):
    con, emb = _setup(tmp_path)
    old_id = S.store_fact(
        con,
        "user",
        "editor",
        "user editor configured editor nvim",
        vec=emb.embed("user configured editor nvim"),
        confidence=1.0,
    )
    S.store_fact(
        con,
        "user",
        "defaults",
        "user configured editor nvim",
        vec=emb.embed("user configured editor nvim"),
        confidence=0.6,
    )
    old = con.execute("SELECT status FROM facts WHERE id=?", (old_id,)).fetchone()
    assert old["status"] == "active"  # weak new + strong old → collision tolerated


def test_no_vec_keeps_both(tmp_path):
    con, emp = _setup(tmp_path)  # noqa: F841
    a = S.store_fact(con, "user", "k1", "identical text stored offline without vectors")
    b = S.store_fact(con, "user", "k2", "identical text stored offline without vectors")
    assert (
        con.execute(
            "SELECT COUNT(*) FROM facts WHERE status='active' AND id IN (?,?)", (a, b)
        ).fetchone()[0]
        == 2
    )  # offline-first: no vec → no semantic call, plain insert


def test_same_key_supersede_cleans_slots(tmp_path):
    con, emb = _setup(tmp_path)
    old_id = S.store_fact(
        con, "user", "editor", "editor is nvim", vec=emb.embed("user editor is nvim")
    )
    S.store_fact(
        con, "user", "editor", "editor is helix", vec=emb.embed("user editor is helix")
    )
    assert (
        con.execute("SELECT status FROM facts WHERE id=?", (old_id,)).fetchone()[
            "status"
        ]
        == "superseded"
    )
    assert (
        con.execute("SELECT COUNT(*) FROM facts_fts WHERE id=?", (old_id,)).fetchone()[
            0
        ]
        == 0
    )
    assert (
        con.execute("SELECT COUNT(*) FROM vec_facts WHERE id=?", (old_id,)).fetchone()[
            0
        ]
        == 0
    )


def test_procedure_semantic_merge(tmp_path):
    con, emb = _setup(tmp_path)
    p1 = S.store_procedure(
        con,
        trigger="restart the llama server",
        steps="systemctl --user restart llamacpp then check /health",
        vec=emb.embed(
            "restart the llama server systemctl restart llamacpp check health"
        ),
        namespace="default",
    )
    p2 = S.store_procedure(
        con,
        trigger="llama server restart how to",
        steps="systemctl --user restart llamacpp then check /health again",
        vec=emb.embed(
            "llama server restart how to systemctl restart llamacpp check health"
        ),
        namespace="default",
    )
    assert p1 == p2
    assert con.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 1
    assert (
        con.execute("SELECT uses FROM procedures WHERE id=?", (p1,)).fetchone()["uses"]
        == 2
    )


def test_procedure_unrelated_no_merge(tmp_path):
    con, emb = _setup(tmp_path)
    S.store_procedure(
        con,
        trigger="rotate api keys",
        steps="vault policy update every 90 days",
        vec=emb.embed("rotate api keys vault policy update"),
    )
    S.store_procedure(
        con,
        trigger="print labels",
        steps="lpr printer queue with label stock",
        vec=emb.embed("print labels printer queue stock"),
    )
    assert con.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 2


def test_supersede_sdk_method(tmp_path):
    con, emb = _setup(tmp_path)
    old_id = S.store_fact(
        con,
        "user",
        "measured",
        "fact A measured final",
        vec=emb.embed("fact A measured final"),
    )
    new_id = S.store_fact(
        con,
        "user",
        "final",
        "fact A measured newer",
        vec=emb.embed("fact A measured newer"),
    )
    # bypass auto-collision (sims below threshold on purpose): use SDK verdict
    mem = sys.modules["ai_memory.sdk"].Memory
    from ai_memory.sdk import Memory as M

    m = M(db_path=str(tmp_path / "t.db"), embed_provider="hash", dim=64)
    out = m.supersede_fact(old_id, new_id)
    assert out["superseded"] == old_id and out["by"] == new_id
    assert (
        con.execute("SELECT status FROM facts WHERE id=?", (old_id,)).fetchone()[
            "status"
        ]
        == "superseded"
    )
    assert (
        con.execute("SELECT COUNT(*) FROM vec_facts WHERE id=?", (old_id,)).fetchone()[
            0
        ]
        == 0
    )
    with_m = M(db_path=str(tmp_path / "t.db"), embed_provider="hash", dim=64)
    try:
        with_m.supersede_fact("nope", new_id)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_namespace_isolation_in_collision(tmp_path):
    con, emb = _setup(tmp_path)
    old_id = S.store_fact(
        con,
        "user",
        "k1",
        "shared phrase alpha beta gamma delta epsilon",
        vec=emb.embed("user k1 shared phrase alpha beta gamma delta epsilon"),
        namespace="ns1",
    )
    S.store_fact(
        con,
        "user",
        "k2",
        "shared phrase alpha beta gamma delta epsilon",
        vec=emb.embed("user k2 shared phrase alpha beta gamma delta epsilon"),
        namespace="ns2",
    )
    # other-namespace same text must NOT destroy ns1 row
    assert (
        con.execute("SELECT status FROM facts WHERE id=?", (old_id,)).fetchone()[
            "status"
        ]
        == "active"
    )
