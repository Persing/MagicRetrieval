"""Partial merging, aggregation and the gate wiring.

A 30-run grid is written one partial at a time so a crash costs one run rather than the grid.
That makes the merge step load-bearing: a silently overwritten or double-counted run would move
a mean without moving anything visible.
"""

import json

import numpy as np
import pytest

from mr import t4_representation as T, thresholds


def _rec(arm, seed, recall, corpus="casual", clean_recall=None):
    def universe(r):
        return {
            "n_queries": 100,
            "overall": {s: {"n": 100, "mrr": 0.1, "recall_at_10": r / 2, "recall_at_50": r}
                        for s in ("centroid", "max_sim", "popularity", "random")},
            "strata": {}, "coldstart_gated": {}, "n_near_zero": 10,
        }
    return {"arm": arm, "seed": seed, "corpus": corpus,
            "full": universe(recall),
            "clean_universe": universe(clean_recall if clean_recall is not None else recall),
            "layer0_meta": {"corpus": corpus, "n_train_decks": 100, "n_test_decks": 20,
                            "n_queries": 100, "n_pool": 1000, "n_clean_in_pool": 300}}


def _write(tmp_path, recs):
    for r in recs:
        T.partial_path(r["corpus"], r["arm"], r["seed"], tmp_path).write_text(
            json.dumps(r), encoding="utf-8")


def test_duplicate_run_raises_rather_than_overwriting(tmp_path):
    """Two runs of the same (corpus, arm, seed) must be an error. Silently keeping the last one
    would let a re-run with different settings replace a recorded result invisibly."""
    _write(tmp_path, [_rec("A", 42, 0.5)])
    # a second file carrying the same key, under a name that does not collide on disk
    (tmp_path / "t4_partial_casual_A_s42_copy.json").write_text(
        json.dumps(_rec("A", 42, 0.9)), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate run"):
        T.merge_partials(tmp_path)


def test_merge_with_no_partials_raises(tmp_path):
    with pytest.raises(ValueError, match="no t4_partial"):
        T.merge_partials(tmp_path)


def test_aggregate_reports_mean_and_sample_sd(tmp_path):
    recs = [_rec("A", s, r) for s, r in zip((42, 43, 44), (0.40, 0.50, 0.60))]
    agg = T.aggregate(recs)
    assert agg["A"]["n_seeds"] == 3
    assert agg["A"]["mean"] == pytest.approx(0.50)
    assert agg["A"]["sd"] == pytest.approx(np.std([0.4, 0.5, 0.6], ddof=1))


def test_arm_D_is_absent_from_the_full_universe_aggregate(tmp_path):
    """D has no text for cards CDL cannot parse, so a full-pool number for it would compare a
    model that knows a third of the corpus against models that know all of it."""
    d = _rec("D", 42, 0.5)
    d["full"] = None
    agg_full = T.aggregate([d], universe="full")
    agg_clean = T.aggregate([d], universe="clean_universe")
    assert "D" not in agg_full
    assert "D" in agg_clean


def test_null_rule_vetoes_a_gap_that_clears_its_gate(tmp_path):
    """C beats B+ by +0.03 — past the +0.02 gate — but the seeds are noisier than the gap."""
    recs = ([_rec("C", s, r) for s, r in zip((42, 43, 44), (0.50, 0.58, 0.42))]
            + [_rec("B+", s, r) for s, r in zip((42, 43, 44), (0.47, 0.55, 0.39))])
    v = T.verdicts(T.aggregate(recs))["C - B+"]
    assert v["gap"] == pytest.approx(0.03)
    assert v["verdict"] == "NULL"


def test_clean_gap_passes_its_gate(tmp_path):
    recs = ([_rec("C", s, r) for s, r in zip((42, 43, 44), (0.530, 0.531, 0.529))]
            + [_rec("B+", s, r) for s, r in zip((42, 43, 44), (0.500, 0.501, 0.499))])
    v = T.verdicts(T.aggregate(recs))["C - B+"]
    assert v["verdict"] == "PASS"


def test_paired_gap_is_reported_but_is_not_the_verdict(tmp_path):
    """All arms share seeds, split and queries, so the paired difference is strictly more
    informative than the pooled-sd rule. It was not pre-committed, so it is reported alongside
    and must never move the verdict — same footing as T2's `narrow_effect`."""
    recs = ([_rec("C", s, r) for s, r in zip((42, 43, 44), (0.50, 0.58, 0.42))]
            + [_rec("B+", s, r) for s, r in zip((42, 43, 44), (0.47, 0.55, 0.39))])
    v = T.verdicts(T.aggregate(recs))["C - B+"]
    assert v["paired_mean_gap"] == pytest.approx(0.03)
    assert v["paired_sd"] == pytest.approx(0.0, abs=1e-9)   # perfectly consistent across seeds
    assert v["verdict"] == "NULL", "the paired annotation must not override the frozen rule"


def test_ungated_pair_reports_real_gap(tmp_path):
    recs = ([_rec("B+", s, r) for s, r in zip((42, 43), (0.60, 0.601))]
            + [_rec("B_type", s, r) for s, r in zip((42, 43), (0.50, 0.501))])
    assert T.verdicts(T.aggregate(recs))["B+ - B_type"]["verdict"] == "REAL_GAP"


def test_stratified_table_puts_arms_side_by_side(tmp_path):
    """The finding the spec cares about is arms tying on the aggregate and separating inside a
    stratum. A per-arm table would make that comparison something the reader does by hand."""
    def with_strata(arm, cold, hot):
        r = _rec(arm, 42, 0.5)
        def m(v, n):
            return {s: {"n": n, "mrr": 0.1, "recall_at_10": v / 2, "recall_at_50": v}
                    for s in ("centroid", "max_sim", "popularity", "random")}
        r["full"]["strata"] = {"play_bucket": {"low": m(cold, 500), "high": m(hot, 900)}}
        return r

    recs = [with_strata("A", 0.10, 0.80), with_strata("B+", 0.30, 0.80)]
    md = T._render_strata(recs, k=50)
    assert "### by play_bucket" in md
    header = next(ln for ln in md.splitlines() if ln.startswith("| stratum"))
    assert "A" in header and "B+" in header and "popularity" in header
    low = next(ln for ln in md.splitlines() if ln.startswith("| low"))
    assert "0.1000" in low and "0.3000" in low, "both arms must appear on the same stratum row"


def test_stratified_table_survives_no_full_results(tmp_path):
    d = _rec("D", 42, 0.5)
    d["full"] = None
    assert "No full-universe results" in T._render_strata([d], k=50)


def test_arm_D_still_appears_in_the_report(tmp_path):
    """D has no full-universe number, so a table driven off the full-universe aggregate drops its
    row entirely — directly under prose saying D appears in the clean-parse column. A missing row
    and an inapplicable cell look the same to a reader; only one of them is honest."""
    d = _rec("D", 42, 0.5, clean_recall=0.61)
    d["full"] = None
    _write(tmp_path, [_rec("A", 42, 0.5), d])
    T.merge_partials(tmp_path)
    md = (tmp_path / "t4_representation.md").read_text(encoding="utf-8")
    drow = next(ln for ln in md.splitlines() if ln.startswith("| D |"))
    assert "0.6100" in drow, "D's clean-universe number must be shown"
    assert "—" in drow, "D's full-universe cell must be marked inapplicable, not blank"


def test_report_refuses_to_render_with_an_arm_missing(tmp_path):
    """The guard must fire on the rendered TEXT, not on the aggregate dict.

    The arm-D bug passed every check on the intermediate data — the clean-universe aggregate had
    D in it and every number was correct. What was wrong was the table, which is the only artifact
    anyone reads. So this test asserts the failure mode directly: a row-set that omits an arm which
    ran must raise, regardless of what the aggregates contain.
    """
    recs = [_rec("A", 42, 0.5), _rec("C", 42, 0.5)]
    with pytest.raises(T.ReportIncomplete, match=r"\['C'\]"):
        T.assert_arms_rendered(recs, "| A | 1 | 0.5000 | 0.0000 | 0.5000 |")


def test_report_guard_ignores_skipped_arms(tmp_path):
    """An arm that produced no result is reported in its own section, not the arms table, so it
    must not trip the guard."""
    d = _rec("D", 42, 0.5)
    d["skipped"] = "no training pair has text under this arm"
    T.assert_arms_rendered([_rec("A", 42, 0.5), d], "| A | 1 | 0.5000 | 0.0000 | 0.5000 |")


def test_full_merge_passes_the_arm_guard(tmp_path):
    """End-to-end: a real merge of all six arms, including D with no full-universe number, must
    render every one of them and therefore not raise."""
    recs = [_rec(a, 42, 0.5) for a in ("A", "B", "B_type", "B+", "C")]
    d = _rec("D", 42, 0.5, clean_recall=0.61)
    d["full"] = None
    _write(tmp_path, recs + [d])
    T.merge_partials(tmp_path)
    md = (tmp_path / "t4_representation.md").read_text(encoding="utf-8")
    for arm in ("A", "B", "B_type", "B+", "C", "D"):
        assert any(ln.startswith(f"| {arm} |") for ln in md.splitlines()), f"{arm} missing"


def test_merge_writes_both_findings_files(tmp_path):
    _write(tmp_path, [_rec(a, 42, 0.5) for a in ("A", "B", "B_type", "B+", "C")])
    payload = T.merge_partials(tmp_path, smoke=True)
    assert (tmp_path / "t4_representation.json").exists()
    md = (tmp_path / "t4_representation.md").read_text(encoding="utf-8")
    assert "SMOKE RUN" in md, "a smoke run must say so in the report, not just in the JSON"
    assert payload["thresholds"]["t4"]["gate_c_minus_bplus"] == thresholds.T4_GATE_C_MINUS_BPLUS


def test_interim_run_declares_itself_non_final(tmp_path):
    """A 3-of-5-seed pass must not render as a finished result. n=5 is frozen so that n is not
    chosen after seeing results; an interim artifact that looks identical to a final one is how
    that discipline quietly gets lost."""
    _write(tmp_path, [_rec(a, s, 0.5) for a in ("A", "B") for s in (42, 43, 44)])
    payload = T.merge_partials(tmp_path)
    md = (tmp_path / "t4_representation.md").read_text(encoding="utf-8")
    assert "INTERIM" in md and "not final" in md
    assert payload["is_final"] is False
    assert payload["n_seeds"] == 3 and payload["frozen_n_seeds"] == 5


def test_full_seed_run_is_marked_final(tmp_path):
    _write(tmp_path, [_rec(a, s, 0.5) for a in ("A", "B") for s in (42, 43, 44, 45, 46)])
    payload = T.merge_partials(tmp_path)
    md = (tmp_path / "t4_representation.md").read_text(encoding="utf-8")
    assert "INTERIM" not in md
    assert payload["is_final"] is True
