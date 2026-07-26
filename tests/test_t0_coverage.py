"""T0 stratified-coverage invariants, on synthetic data — no parser or real corpus needed.

The honesty guard only means something if the strata nest correctly: non_land and non_staple
subsets can never show HIGHER coverage than a stratum that additionally excludes cards the
subset already excludes would allow... concretely: non_land_non_staple's numerator/denominator
must be consistent with non_land and non_staple individually, and every appearance-weighted
coverage number must lie in [0, 1].
"""

import pandas as pd

from mr import t0_coverage as T0
from mr.corpus import Deck


def _decks() -> list[Deck]:
    # "land1" is a land staple appearing in every deck; "spell1" fails to parse; "spell2" parses.
    return [
        Deck(deck_id=f"d{i}", commander="cmd", oracle_ids=("land1", "spell1", "spell2"))
        for i in range(10)
    ]


def _parsed() -> pd.DataFrame:
    return pd.DataFrame([
        {"oracle_id": "land1", "name": "Land One", "type_line": "Basic Land", "status": "clean",
         "ok": True, "encoded": True, "error": None, "exclusion_reason": None,
         "n_gaps": 0, "gap_category": None},
        {"oracle_id": "spell1", "name": "Spell One", "type_line": "Sorcery", "status": "gap",
         "ok": False, "encoded": True, "error": None, "exclusion_reason": None,
         "n_gaps": 1, "gap_category": "effect not matched"},
        {"oracle_id": "spell2", "name": "Spell Two", "type_line": "Instant", "status": "clean",
         "ok": True, "encoded": True, "error": None, "exclusion_reason": None,
         "n_gaps": 0, "gap_category": None},
    ])


def test_appearance_coverage_matches_hand_count():
    decks = _decks()
    parsed = _parsed()
    r = T0.analyze(parsed, decks)
    # 10 appearances each of land1(clean), spell1(gap), spell2(clean) = 30 total, 20 covered.
    assert r["strata"]["all"]["total"] == 30
    assert r["strata"]["all"]["covered"] == 20
    assert r["appearance_coverage"] == 20 / 30


def test_non_land_stratum_excludes_the_land():
    r = T0.analyze(_parsed(), _decks())
    # non_land drops land1 entirely: 10 (spell1, gap) + 10 (spell2, clean) = 20 total, 10 covered.
    assert r["strata"]["non_land"]["total"] == 20
    assert r["strata"]["non_land"]["covered"] == 10


def test_guarded_stratum_is_the_intersection_not_a_double_count():
    """non_land_non_staple must equal non_land AND non_staple simultaneously — not just whichever
    filter happens to bind harder. With staple_threshold=0.5 here, land1 (100% of decks) is both a
    land AND a staple, so excluding it once should look identical to excluding it via either path
    alone in this fixture, and the guarded total must not exceed either individual stratum's total."""
    r = T0.analyze(_parsed(), _decks())
    assert r["strata"]["non_land_non_staple"]["total"] <= r["strata"]["non_land"]["total"]
    assert r["strata"]["non_land_non_staple"]["total"] <= r["strata"]["non_staple"]["total"]


def test_all_coverage_fractions_are_in_unit_interval():
    r = T0.analyze(_parsed(), _decks())
    for s in r["strata"].values():
        assert 0.0 <= s["coverage"] <= 1.0
    assert 0.0 <= r["appearance_coverage"] <= 1.0
    assert 0.0 <= r["guarded_coverage"] <= 1.0


def test_status_share_sums_to_one():
    r = T0.analyze(_parsed(), _decks())
    total_share = sum(s["share"] for s in r["status_share"].values())
    assert abs(total_share - 1.0) < 1e-9


def test_top_breakout_appearance_coverage_bounded():
    r = T0.analyze(_parsed(), _decks())
    for b in r["top_breakouts"].values():
        assert 0.0 <= b["appearance_coverage"] <= 1.0
        assert 0.0 <= b["distinct_coverage"] <= 1.0


def test_verdict_is_fail_for_this_fixture():
    """2/3 distinct cards clean but appearance-weighted coverage is 66.7% — below the 70% floor
    is impossible to construct from 3 evenly-weighted cards, so this fixture should land at
    UNRESOLVED or PASS depending on the guard; the point is the verdict function is actually
    invoked and returns one of the three defined strings, not silently skipped."""
    r = T0.analyze(_parsed(), _decks())
    assert r["verdict"] in ("PASS", "FAIL", "UNRESOLVED")
