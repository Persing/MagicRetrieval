"""The conclusion renderer and its two guards.

This document is the one that gets cited and pasted, so the failure that matters is not a crash —
it is a conclusion that reads as settled while the ladder underneath it is not, or that asserts a
closed question without showing the figure that closed it. Both guards therefore assert on the
**rendered text**, and so do these tests.
"""

import json

import numpy as np
import pytest

from mr import t4_conclusion as C


# ── fixtures ──────────────────────────────────────────────────────────────────

def _universe(agg, cold, high, n_seeds_pop=0.18):
    def m(v, n):
        return {"n": n, "mrr": v / 5, "recall_at_10": v / 3, "recall_at_50": v}
    return {
        "n_queries": 40457, "n_decks": 555, "n_near_zero": 881,
        "near_zero_underpowered": True,
        "n_dropped_no_text": 0, "n_dropped_target_ineligible": 0,
        "n_commander_fallback_decks": 0,
        "overall": {"centroid": m(agg, 40457), "max_sim": m(agg, 40457),
                    "popularity": m(n_seeds_pop, 40457), "random": m(0.0016, 40457)},
        "coldstart_gated": {"centroid": m(cold, 5964), "max_sim": m(cold, 5964),
                            "popularity": m(0.0, 5964), "random": m(0.0038, 5964)},
        "strata": {"play_bucket": {
            "high": {"centroid": m(high, 11283), "max_sim": m(high, 11283),
                     "popularity": m(0.6481, 11283), "random": m(0.0016, 11283)},
            "low": {"centroid": m(cold, 5083), "max_sim": m(cold, 5083),
                    "popularity": m(0.0, 5083), "random": m(0.0038, 5083)},
        }},
    }


def _rep(is_final=True, n_seeds=5, seeds=(42, 43, 44, 45, 46)):
    """A merged ladder payload shaped like the real one, with the dose-response built in."""
    profile = {"A": (0.0608, 0.1415, 0.0200), "B": (0.0584, 0.1386, 0.0210),
               "B_type": (0.0616, 0.1382, 0.0300), "B+": (0.0624, 0.1335, 0.0348),
               "C": (0.0535, 0.1278, 0.0150)}
    records = []
    for arm, (agg, cold, high) in profile.items():
        for i, s in enumerate(seeds):
            jitter = 0.0001 * i
            records.append({"arm": arm, "seed": s, "corpus": "casual",
                            "full": _universe(agg + jitter, cold + jitter, high + jitter),
                            "clean_universe": _universe(agg, cold, high)})
    return {
        "records": records, "is_final": is_final, "n_seeds": n_seeds, "frozen_n_seeds": 5,
        "verdicts": {"C - B+": {"question": "does CDL justify continued development?",
                                "gate": 0.02, "gap": -0.0089, "pooled_sd": 0.0007,
                                "verdict": "BELOW_GATE"}},
        "layer0_meta": {"corpus": "casual", "n_train_decks": 3142, "n_test_decks": 555,
                        "n_pool": 30958, "n_clean_in_pool": 10628},
    }


def _sources(is_final=True, n_seeds=5):
    def sd(mean, sd_):
        return {"mean": mean, "sd": sd_, "n_seeds": 3}
    return {
        "t4_representation": _rep(is_final, n_seeds),
        "t4_staple_reread": {"clean_reread": {
            "all_clean": {"paired": sd(-0.0216, 0.0018)},
            "clean_non_staple": {"paired": sd(-0.0144, 0.0043)},
        }},
        "t4_diagnostics": {
            "d1_partition": {
                "n_cdl": 10628, "n_fallback": 20330,
                "arms": {a: {"geometry": {"dialect_gap": sd(g, 0.0001), "across": sd(0.526, 0.003)}}
                         for a, g in (("A", 0.00059), ("B", 0.00088), ("B_type", 0.00085),
                                      ("B+", 0.00118), ("C", 0.00309))},
            },
            "d2_displacement": {"flip_decomposition": {
                "42": {"clean_share_of_displaced": 0.503, "clean_share_of_Bplus_top50": 0.399}}},
        },
        "t4_matched_data": {
            "runs": {"42": {"B+_matched": {"recall_at_50": 0.0742},
                            "B+_random": {"recall_at_50": 0.0809}}},
            "reference": {"D": {"recall_at_50": 0.0315, "n_examples": 8702},
                          "B+_full": {"recall_at_50": 0.0937, "n_examples": 234593}},
        },
    }


# ── the INTERIM guard ─────────────────────────────────────────────────────────

def test_interim_ladder_renders_its_banner_and_passes(tmp_path):
    md, _ = C.render(_sources(is_final=False, n_seeds=3))
    assert "INTERIM — 3 of 5" in md
    C.assert_interim_declared(_sources(is_final=False, n_seeds=3)["t4_representation"], md)


def test_conclusion_off_an_incomplete_ladder_without_a_banner_raises(tmp_path):
    """The guard has to fire on the rendered text. A conclusion drawn from an n=3 grid that reads
    as settled is the failure — nobody opens `t4_representation.md` to discover it was provisional."""
    rep = _sources(is_final=False, n_seeds=3)["t4_representation"]
    with pytest.raises(C.ConclusionIncomplete, match="3 of 5"):
        C.assert_interim_declared(rep, "# T4 — conclusion\n\nCDL is settled.\n")


def test_final_ladder_needs_no_banner(tmp_path):
    md, _ = C.render(_sources(is_final=True, n_seeds=5))
    assert "INTERIM" not in md
    C.assert_interim_declared(_sources()["t4_representation"], md)


# ── the closed-question guard ─────────────────────────────────────────────────

def test_every_closed_question_appears_with_its_number(tmp_path):
    md, payload = C.render(_sources())
    cdl = payload["cdl_case"]
    for value in (f"{cdl['c_minus_bplus']['gap']:+.4f}",
                  f"{cdl['non_staple']['mean']:+.4f}",
                  f"{cdl['dialect_gap_C']:.4f}",
                  f"{cdl['encoding_effect']:+.4f}"):
        assert value in md, f"{value} asserted in the payload but absent from the rendered text"


def test_a_claim_without_its_figure_raises(tmp_path):
    """The specific regression: prose keeps the conclusion, the table loses the number."""
    with pytest.raises(C.ConclusionIncomplete, match="not a staple artifact"):
        C.assert_closed_questions(
            "CDL makes retrieval worse. `C - B+` = -0.0089.",
            {"stop-CDL": "-0.0089", "not a staple artifact": "-0.0144"})


# ── the numbers themselves ────────────────────────────────────────────────────

def test_dose_response_is_monotonic_in_the_ladder_order(tmp_path):
    """The finding is the ordering, so the table must be built in the pre-declared ladder order and
    not sorted on the result."""
    rows = C.dose_response(_rep())
    assert [r["arm"] for r in rows] == ["A", "B", "B_type", "B+", "C"]
    colds = [r["coldstart"]["mean"] for r in rows]
    assert colds == sorted(colds, reverse=True), "cold-start must fall along the ladder"


def test_gaps_against_A_are_paired_over_shared_seeds(tmp_path):
    """Arms share the split, queries and seeds, so the difference is taken per seed. With a ragged
    grid the pairing must use only the seeds both arms actually ran."""
    rep = _rep(seeds=(42, 43, 44))
    rep["records"] = [r for r in rep["records"] if not (r["arm"] == "C" and r["seed"] == 44)]
    rows = {r["arm"]: r for r in C.dose_response(rep)}
    assert rows["C"]["vs_A"]["n_seeds"] == 2, "must pair only on seeds C actually has"
    assert rows["B"]["vs_A"]["n_seeds"] == 3


def test_highplay_sign_flips_against_cold_start(tmp_path):
    """The mechanism claim rests on the sign flip, so it is asserted rather than narrated."""
    rep = _rep()
    flip = C.highplay_flip(rep)
    cold = {r["arm"]: r for r in C.dose_response(rep)}["B+"]["vs_A"]["mean"]
    assert flip["mean"] > 0 > cold


def test_baselines_are_read_not_averaged_across_arms(tmp_path):
    """Popularity and random are arm-independent by construction. Averaging them over arms would
    imply they vary and would silently paper over it if they ever did."""
    b = C.baselines(_rep())
    assert b["cold_pop"] == 0.0
    assert b["agg_pop"] == pytest.approx(0.18)


def test_missing_input_names_the_command_that_produces_it(tmp_path):
    (tmp_path / "t4_representation.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"mr\.t4_matched_data"):
        C.load(tmp_path)


def test_end_to_end_write(tmp_path):
    for name, payload in _sources(is_final=True, n_seeds=5).items():
        (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    env = C.main(tmp_path)
    md = (tmp_path / "t4_conclusion.md").read_text(encoding="utf-8")
    assert (tmp_path / "t4_conclusion.json").exists()
    assert env["ladder_is_final"] is True
    assert "Recorded, not solved." in md, "the standing confound header must be reproduced"
    assert "INTERIM" not in md
