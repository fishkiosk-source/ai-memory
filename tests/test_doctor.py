"""T1.1: doctor fact_audit tests."""

import sys

sys.path.insert(0, "src")

from ai_memory import db as DB
from ai_memory import doctor
from ai_memory import store as S
from ai_memory.embeddings import HashEmbedder


def _setup(tmp_path):
    con = DB.connect(str(tmp_path / "t.db"), dim=64)
    emb = HashEmbedder(64)
    return con, emb


def test_doctor_flags_cross_key_conflict(tmp_path):
    con, emb = _setup(tmp_path)
    S.store_fact(
        con,
        "user",
        "dial_radius",
        "prefers radius 40 for dial",
        vec=emb.embed("user dial_radius prefers radius 40 for dial"),
    )
    S.store_fact(
        con,
        "user",
        "dial_size",
        "prefers radius 40 for dial",
        vec=None,  # offline path: semantic check can't fire, doctor still spots pair
    )
    out = doctor.fact_audit(con, embed_fn=emb.embed)
    assert len(out["conflicts"]) == 1
    assert out["conflicts"][0]["a"]["key"] != out["conflicts"][0]["b"]["key"]
    rep = doctor.audit_report(out)
    assert "conflict" in rep.lower()


def test_doctor_flags_same_key_dupes(tmp_path):
    con, emb = _setup(tmp_path)
    fid = S.store_fact(
        con, "user", "editor", "editor is nvim", vec=emb.embed("user editor is nvim")
    )
    S.store_fact(
        con, "user", "editor", "editor is helix", vec=emb.embed("user editor is helix")
    )
    # force a same-key double-active to prove detection (write path guards it;
    # audit must catch invariant breaks from imports/restore)
    con.execute("UPDATE facts SET status='active' WHERE id=?", (fid,))
    con.commit()
    out = doctor.fact_audit(con, embed_fn=emb.embed)
    assert len(out["same_key_dupes"]) == 1


def test_doctor_dead_slots_from_supersede(tmp_path):
    con, emb = _setup(tmp_path)
    S.store_fact(
        con, "user", "editor", "editor is nvim", vec=emb.embed("user editor is nvim")
    )
    S.store_fact(
        con, "user", "editor", "editor is helix", vec=emb.embed("user editor is helix")
    )
    # T1.3: supersede now CLEANS old fts/vec slots; doctor must see zero dead
    out = doctor.fact_audit(con, embed_fn=emb.embed)
    assert out["superseded"] == 1
    assert out["dead_fts_slots"] == []
    assert out["dead_vec_slots"] == []
    # doctor still DETECTS manually-created dead slots (ghost index rows)
    con.execute(
        "INSERT INTO facts_fts(id,entity,key,value) VALUES('ghost','u','k','v')"
    )
    con.commit()
    out2 = doctor.fact_audit(con, embed_fn=emb.embed)
    assert "ghost" in out2["dead_fts_slots"]


def test_doctor_missing_vec_rows(tmp_path):
    con, emb = _setup(tmp_path)
    S.store_fact(con, "user", "note", "fact with no vector", vec=None)
    out = doctor.fact_audit(con, embed_fn=emb.embed)
    assert len(out["missing_vec_rows"]) == 1


def test_doctor_namespace_filter(tmp_path):
    con, emb = _setup(tmp_path)
    S.store_fact(
        con,
        "user",
        "ns_a",
        "fact in namespace one",
        vec=emb.embed("fact in namespace one"),
        namespace="one",
    )
    S.store_fact(
        con,
        "user",
        "ns_b",
        "fact in namespace two",
        vec=emb.embed("fact in namespace two"),
        namespace="two",
    )
    out = doctor.fact_audit(con, embed_fn=emb.embed, namespace="one")
    assert out["active_facts"] == 1


def test_doctor_clean_db_no_conflicts(tmp_path):
    con, emb = _setup(tmp_path)
    S.store_fact(
        con, "user", "editor", "editor is nvim", vec=emb.embed("user editor is nvim")
    )
    S.store_fact(
        con, "user", "shell", "shell is zsh", vec=emb.embed("user shell is zsh")
    )
    out = doctor.fact_audit(con, embed_fn=emb.embed)
    assert out["conflicts"] == []
    assert out["same_key_dupes"] == []
