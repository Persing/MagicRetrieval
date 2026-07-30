"""Reports must not read as complete while contradicting their own data.

This is a nastier defect class than a slow path or a wrong number, because it is silent by
construction: nothing raises, every figure present is correct, and the file looks finished. Two
instances shipped before these guards existed —

  * T4's arms table iterated the full-universe aggregate. Arm D has no full-universe number by
    construction, so its row vanished, directly beneath prose saying D appears in the clean-parse
    column.
  * T2's scope paragraph keyed off "is anything missing" rather than *what* is missing, so
    `findings/t2_leakage_partial_cd.md` states "2c/2d ... were not run" immediately above a table
    of four 2c/2d PASS rows, tags every decision rule "(unresolved — needs 2d)" when 2d is exactly
    what ran, and closes with an "Interim read (2a/2b only)" section on a file holding no 2a or 2b
    data.

Every assertion here is made against the RENDERED TEXT, not the intermediate dicts. Both bugs had
perfectly correct intermediate data; the defect lived in the rendering, which is the only artifact
anyone reads.
"""

import pytest

from mr import t0_coverage, t2_leakage


# ── T2: the scope paragraph and decision tags must follow what actually ran ──

def _rung(arm, corpus, lift=0.15):
    return {
        "arm": arm, "corpus": corpus, "n_decks": 100, "n_cards_vocab": 500,
        "n_positives_mined_total": 10, "n_positives_mined": 10, "n_positives_strict": 10,
        "n_positives_trained": 8, "n_negatives": 3, "n_holdout_positives": 2,
        "n_extra_positives_2d": 0, "n_extra_negatives_2d": 0,
        "curated": {"n_pairs_in_vocab": 40, "mean_substitute_cosine": 0.81,
                    "mean_random_cluster_cosine": 0.66, "mean_random_stratum_cosine": 0.70,
                    "lift_vs_cluster": lift, "lift_vs_cluster_ci": [lift - 0.03, lift + 0.03],
                    "lift_vs_stratum": lift, "lift_vs_stratum_ci": [lift - 0.03, lift + 0.03]},
        "mined_holdout": {"n_pairs_in_vocab": 900, "mean_substitute_cosine": 0.7,
                          "mean_random_cluster_cosine": 0.67, "mean_random_stratum_cosine": 0.6,
                          "lift_vs_cluster": 0.02, "lift_vs_cluster_ci": [0.01, 0.03],
                          "lift_vs_stratum": 0.05, "lift_vs_stratum_ci": [0.04, 0.06]},
        "verdict": "PASS", "narrow_effect": True,
    }


CD_RUNGS = [_rung("2c", "casual"), _rung("2d", "casual"),
            _rung("2c", "cedh"), _rung("2d", "cedh")]


def _render(rungs):
    return t2_leakage.render(rungs, t2_leakage._build_harness(rungs))


def test_scope_does_not_claim_the_arms_that_ran_were_not_run():
    """The exact sentence that shipped: '2c/2d ... were not run', above four 2c/2d result rows."""
    md = _render(CD_RUNGS)
    assert "were not run" not in md
    assert "stopped deliberately after 2b" not in md
    # and it must name what is genuinely absent
    assert "2a/precon" in md and "2b/casual" in md and "2b/cedh" in md


def test_decision_rules_are_tagged_unresolved_only_for_absent_arms():
    md = _render(CD_RUNGS)
    assert "(unresolved — needs 2d)" not in md, "2d ran; tagging its rule unresolved is false"
    assert "(unresolved — needs 2c)" not in md, "2c ran; tagging its rule unresolved is false"


def test_interim_read_section_only_appears_on_an_actual_2a_2b_run():
    assert "Interim read" not in _render(CD_RUNGS)
    ab = [_rung("2a", "precon"), _rung("2b", "casual"), _rung("2b", "cedh")]
    assert "Interim read" in _render(ab)


def test_unresolved_tags_do_appear_when_the_arm_really_is_absent():
    ab = [_rung("2a", "precon"), _rung("2b", "casual"), _rung("2b", "cedh")]
    md = _render(ab)
    assert "(unresolved — needs 2d)" in md and "(unresolved — needs 2c)" in md


def test_complete_ladder_emits_no_scope_caveat_at_all():
    full = ([_rung("2a", "precon")]
            + [_rung(a, c) for a in ("2b", "2c", "2d") for c in ("casual", "cedh")])
    md = _render(full)
    assert "Scope of this run" not in md
    assert "unresolved" not in md
    assert "Interim read" not in md


def test_every_rung_that_ran_has_a_row():
    md = _render(CD_RUNGS)
    for r in CD_RUNGS:
        assert f"| {r['arm']} | {r['corpus']} |" in md


def test_t2_raises_rather_than_render_a_missing_rung():
    with pytest.raises(t2_leakage.ReportIncomplete, match="2c/cedh"):
        t2_leakage.assert_rungs_rendered(CD_RUNGS, "| 2c | casual |\n| 2d | casual |\n| 2d | cedh |")


# ── T0: structurally safe, guarded anyway ────────────────────────────────────

def test_t0_raises_rather_than_render_a_missing_corpus():
    with pytest.raises(t0_coverage.ReportIncomplete, match="cedh"):
        t0_coverage.assert_corpora_rendered({"casual": {}, "cedh": {}}, "| casual | 1 |")


def test_t0_guard_passes_when_every_corpus_is_present():
    t0_coverage.assert_corpora_rendered({"casual": {}, "cedh": {}}, "| casual | 1 |\n| cedh | 2 |")
