"""Threshold verdicts and the THRESHOLDS.md/thresholds.py sync check."""

import math

import pytest

from mr import thresholds as T


def test_check_in_sync_has_no_problems():
    assert T.check_in_sync() == []


def test_t0_verdict_boundaries():
    assert T.t0_verdict(0.90, 0.85) == "PASS"
    assert T.t0_verdict(0.90, 0.70) == "UNRESOLVED"   # guard drop > 10pts
    assert T.t0_verdict(0.60, 0.55) == "FAIL"
    assert T.t0_verdict(0.75, 0.70) == "UNRESOLVED"    # between fail/pass
    assert T.t0_verdict(0.69, 0.69) == "FAIL"          # guard can't rescue a FAIL


def test_t1_verdict_boundaries():
    assert T.t1_verdict(0.05, 0.01) == "PASS"
    assert T.t1_verdict(0.05, 0.06) == "PARTIAL"       # numeric miss downgrades a PASS
    assert T.t1_verdict(0.15, None) == "PARTIAL"
    assert T.t1_verdict(0.30, 0.0) == "FAIL"           # aggregate FAIL can't be rescued


def test_t2_verdict_boundaries():
    assert T.t2_verdict(0.08, 0.01, mined_lift=0.09) == "PASS"
    assert T.t2_verdict(0.02, 0.01, mined_lift=0.09) == "FAIL"     # below min lift
    assert T.t2_verdict(0.08, -0.01, mined_lift=0.09) == "FAIL"    # CI crosses zero
    assert T.t2_verdict(0.08, 0.01, mined_lift=0.30) == "PHENOTYPE_FIT"  # curated << mined
    assert T.t2_verdict(0.08, 0.01, mined_lift=None) == "PASS"


def test_every_t4_constant_is_covered_by_check_in_sync():
    """`check_in_sync` only checks names it is told about, so a constant added to thresholds.py
    without an entry in `expected` is frozen in code and unfrozen in the record. This closes that
    hole for the T4 block, which is where new numbers are currently being added."""
    import inspect
    declared = {n for n, _ in inspect.getmembers(T) if n.startswith("T4_")}
    src = inspect.getsource(T.check_in_sync)
    missing = sorted(n for n in declared if f'"{n}"' not in src)
    assert not missing, f"T4 constants absent from check_in_sync's expected dict: {missing}"


def test_t4_null_rule_vetoes_a_gap_that_clears_its_gate():
    """The null rule is checked before the gate and can override it — a +0.025 gap that clears the
    +0.02 gate is still NULL when the seeds are noisier than the gap."""
    assert T.t4_pair_verdict(0.525, 0.04, 5, 0.500, 0.04, 5, gate=0.02) == "NULL"
    assert T.t4_pair_verdict(0.525, 0.002, 5, 0.500, 0.002, 5, gate=0.02) == "PASS"


def test_t4_pair_verdict_boundaries():
    # gap beyond sd but short of the gate
    assert T.t4_pair_verdict(0.515, 0.002, 5, 0.500, 0.002, 5, gate=0.02) == "BELOW_GATE"
    # ungated diagnostic pair
    assert T.t4_pair_verdict(0.515, 0.002, 5, 0.500, 0.002, 5, gate=None) == "REAL_GAP"
    # a negative gap beyond sd is a real finding, not a pass
    assert T.t4_pair_verdict(0.480, 0.002, 5, 0.500, 0.002, 5, gate=0.02) == "BELOW_GATE"
    # too few seeds to have an opinion at all
    assert T.t4_pair_verdict(0.52, 0.0, 1, 0.50, 0.0, 1, gate=0.02) == "UNDERPOWERED"


def test_pooled_sd_matches_the_textbook_formula():
    assert math.isclose(T.pooled_sd(0.02, 5, 0.04, 5), math.sqrt((4 * 0.0004 + 4 * 0.0016) / 8))
    assert math.isnan(T.pooled_sd(0.02, 1, 0.04, 5))


# ── T4 scaling ────────────────────────────────────────────────────────────────

def test_equal_example_counts_predict_exactly_zero():
    """The whole design rests on this. Where the 250k positive cap binds at both levels the example
    ratio is 1, so the pure-volume null is 0.0000 and the comparison isolates deck diversity."""
    assert T.t4_scaling_prediction(250_000, 250_000) == 0.0


def test_prediction_is_driven_by_examples_not_decks():
    """The naive expectation — deck count carries pair count — predicted ~+0.0104 for a ~12.6x deck
    range. That holds only if examples actually scale with decks, which the cap prevents."""
    assert T.t4_scaling_prediction(19_800, 250_000) == pytest.approx(0.0099, abs=5e-5)
    with pytest.raises(ValueError):
        T.t4_scaling_prediction(0, 250_000)


def test_all_three_conditions_are_required_for_a_diversity_verdict():
    """Each condition alone can be satisfied while the claim is still not supported, so each one is
    checked separately rather than trusting a single composite number."""
    assert T.t4_scaling_verdict(0.0120, 0.0, 0.0080, 0.0160, 0.0015) == "DIVERSITY_BEYOND_VOLUME"
    # significant and large, but inside the seed noise — the standing null rule still vetoes
    assert T.t4_scaling_verdict(0.0120, 0.0, 0.0080, 0.0160, 0.0200) == "UNDERPOWERED"
    # significant and outside seed noise, but below the magnitude gate
    assert T.t4_scaling_verdict(0.0030, 0.0, 0.0010, 0.0050, 0.0005) == "UNDERPOWERED"


def test_ci_containing_the_prediction_means_decks_are_a_volume_proxy():
    assert T.t4_scaling_verdict(0.0030, 0.0, -0.0010, 0.0070, 0.0015) == "VOLUME_PROXY"
    # and against a NONZERO prediction, matching it is the same verdict
    assert T.t4_scaling_verdict(0.0099, 0.0099, 0.0060, 0.0140, 0.0015) == "VOLUME_PROXY"


def test_landing_below_the_volume_null_is_its_own_named_outcome():
    """Folding this into UNDERPOWERED would hide a real result about the slope."""
    assert T.t4_scaling_verdict(-0.0100, 0.0, -0.0140, -0.0060, 0.0015) == "BELOW_VOLUME_NULL"


def test_too_few_seeds_cannot_produce_a_verdict():
    assert T.t4_scaling_verdict(0.0120, 0.0, 0.0080, 0.0160, float("nan")) == "UNDERPOWERED"


def test_scaling_block_is_embedded_in_every_findings_json():
    """A number is never readable without its rule — `as_dict` is what every report embeds."""
    d = T.as_dict()["t4_scaling"]
    assert d["min_lift"] == T.T4_SCALING_MIN_LIFT
    assert d["slope_per_efold"] == T.T4_SCALING_SLOPE_PER_EFOLD
    assert "n_training_examples" in d["prediction_rule"]
