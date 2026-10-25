"""T2.1/T2.2: multi-procedure extraction + (1)-style parenthetical bullets."""

import sys

sys.path.insert(0, "src")

from ai_memory.consolidator import extract, _paren_items


SESS_WRAP = (
    "Session 2026-10-23 wrap: (1) opencode.json WARN-spam root-caused to invalid "
    "experimental.policies entries - removed the block and verified zero "
    "diagnostics. (2) Harness triage input hygiene: squash_lines template-collapse "
    "shipped with max_lines 40 knob. (3) Bake-off closed: gemma-4-E2B Q8 retained "
    "after decisive margin over LFM2.5. (4) Llama watchdog schedule verified "
    "end-to-end twice with restart step live."
)


def test_paren_items_four_numbered():
    items = _paren_items(SESS_WRAP)
    assert [n for n, _ in items] == [1, 2, 3, 4]


def test_extract_returns_multiple_procs_for_wrap():
    procs = extract(SESS_WRAP)["procedures"]
    assert len(procs) >= 4, f"wrap should mine >=4 procs, got {len(procs)}"
    assert any("(part 1)" in t for t, _ in procs)
    assert any("(part 4)" in t for t, _ in procs)


def test_paren_items_rejects_no_sequence():
    assert _paren_items("totally normal prose with no numbered splits") == []


def test_paren_items_rejects_start_at_two():
    t = (
        "notes (2) something long enough to count as an item body here "
        "(3) another long body that is clearly above twenty characters"
    )
    assert _paren_items(t) == []


def test_paren_items_rejects_short_bodies():
    t = "(1) tiny (2) bits (3) small (4) short bits here"
    assert _paren_items(t) == []


def test_extract_normal_prose_single_proc():
    t = "Fixed the watchdog failure with a systemctl --user restart step on all units"
    out = extract(t)
    assert len(out["procedures"]) == 1


def test_explicit_proc_still_short_circuits():
    out = extract("proc: restart llama -> run systemctl --user restart llamacpp")
    assert out["procedures"][0][0] == "restart llama"
    assert len(out["procedures"]) == 1


def test_proc_cap_six():
    t = "Session double wrap: " + " ".join(
        f"({i}) item number {i} carefully padded to exceed twenty characters long"
        for i in range(1, 9)
    )
    out = extract(t)
    # cap is 6; num 8 sequence still parses so 8 candidates -> trimmed
    assert len(out["procedures"]) <= 6
