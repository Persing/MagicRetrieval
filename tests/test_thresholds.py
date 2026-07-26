"""Threshold verdicts and the THRESHOLDS.md/thresholds.py sync check."""

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
