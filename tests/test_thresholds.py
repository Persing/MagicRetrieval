"""Threshold verdicts and the THRESHOLDS.md/thresholds.py sync check."""

import math

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
