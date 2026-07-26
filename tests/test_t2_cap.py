"""cap_positives: must subsample randomly, not truncate a prefix.

This is the fix for the bug that took down a real run — the casual corpus mined 1,288,988
positives (its 24,762-card vocabulary dwarfs precon/cedh's) and an uncapped fine-tune drove the
process to 15GB resident and swap thrashing before it was killed.
"""

from mr.t2_leakage import cap_positives


def _pairs(n):
    return [(f"a{i}", f"b{i}") for i in range(n)]


def test_no_cap_returns_unchanged():
    p = _pairs(10)
    assert cap_positives(p, None) == p
    assert cap_positives(p, 0) == p  # 0 means uncapped, matches the CLI's --max-positives 0


def test_under_cap_returns_unchanged():
    p = _pairs(10)
    assert cap_positives(p, 100) == p


def test_over_cap_subsamples_to_exact_size():
    p = _pairs(10_000)
    capped = cap_positives(p, 100, seed=1)
    assert len(capped) == 100


def test_capping_is_not_a_prefix_slice():
    """The bug this guards against: positions correlate with card_index/oracle_id ordering, so a
    prefix slice would silently favor whichever cards sort first every time."""
    p = _pairs(10_000)
    capped = cap_positives(p, 100, seed=1)
    assert capped != p[:100]


def test_capping_is_deterministic_under_the_same_seed():
    p = _pairs(5_000)
    a = cap_positives(p, 200, seed=7)
    b = cap_positives(p, 200, seed=7)
    assert a == b


def test_different_seeds_give_different_samples():
    p = _pairs(5_000)
    a = cap_positives(p, 200, seed=1)
    b = cap_positives(p, 200, seed=2)
    assert a != b


def test_no_duplicates_in_capped_output():
    p = _pairs(5_000)
    capped = cap_positives(p, 500, seed=3)
    assert len(set(capped)) == len(capped)
