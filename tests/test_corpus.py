"""Corpus split integrity — the plan's central leakage trap.

Co-occurrence is a deck-level property; a random pair split leaks the same deck across train and
test. `split_by_deck` and `temporal_split` must never let a deck_id land on both sides.
"""

import pytest

from mr.corpus import (
    MAX_COMMANDER_DISTINCT, Deck, assert_no_deck_leak, filter_plausible_decks, split_by_deck,
    temporal_split,
)


def _decks(n: int, dated: bool = False) -> list[Deck]:
    return [
        Deck(
            deck_id=f"d{i}",
            commander=f"cmd{i % 5}",
            oracle_ids=(f"card{i}", f"card{i+1}", "shared_card"),
            created_at=f"2026-01-{(i % 28) + 1:02d}T00:00:00Z" if dated else None,
        )
        for i in range(n)
    ]


def test_split_by_deck_no_overlap():
    train, test = split_by_deck(_decks(200), test_frac=0.2, seed=1)
    assert_no_deck_leak(train, test)  # must not raise
    assert len(test) == pytest.approx(40, abs=2)
    assert len(train) + len(test) == 200


def test_split_by_deck_deterministic():
    a_train, a_test = split_by_deck(_decks(100), seed=7)
    b_train, b_test = split_by_deck(_decks(100), seed=7)
    assert {d.deck_id for d in a_test} == {d.deck_id for d in b_test}


def test_split_by_deck_different_seeds_differ():
    _, test_a = split_by_deck(_decks(300), seed=1)
    _, test_b = split_by_deck(_decks(300), seed=2)
    assert {d.deck_id for d in test_a} != {d.deck_id for d in test_b}


def test_temporal_split_no_overlap_and_is_chronological():
    decks = _decks(100, dated=True)
    train, test, cutoff = temporal_split(decks, test_frac=0.2)
    assert_no_deck_leak(train, test)
    assert cutoff is not None
    assert all((d.created_at or "") < cutoff for d in train)
    assert all((d.created_at or "") >= cutoff for d in test)


def test_temporal_split_drops_undated_decks_rather_than_train():
    dated = _decks(50, dated=True)
    undated = _decks(10, dated=False)
    for i, d in enumerate(undated):
        undated[i] = Deck(deck_id=f"undated{i}", commander=d.commander, oracle_ids=d.oracle_ids)
    train, test, cutoff = temporal_split(dated + undated)
    all_ids = {d.deck_id for d in train} | {d.deck_id for d in test}
    assert not any(d.deck_id.startswith("undated") for d in train)
    assert not any(d.deck_id.startswith("undated") for d in test)
    assert all(not did.startswith("undated") for did in all_ids)


def test_temporal_split_all_undated_returns_empty():
    train, test, cutoff = temporal_split(_decks(20, dated=False))
    assert train == [] and test == [] and cutoff is None


def test_assert_no_deck_leak_raises_on_overlap():
    train = _decks(5)
    test = [Deck(deck_id="d2", commander="x", oracle_ids=("a",))]  # collides with train's d2
    with pytest.raises(AssertionError):
        assert_no_deck_leak(train, test)


def test_appearance_counts_and_staples():
    from mr.corpus import appearance_counts, identify_staples
    decks = _decks(10)  # "shared_card" appears in every deck
    counts = appearance_counts(decks)
    assert counts["shared_card"] == 10
    staples = identify_staples(decks, threshold=0.5)
    assert "shared_card" in staples
    assert "card0" not in staples


# ── deck plausibility ─────────────────────────────────────────────────────────

def _sized(deck_id: str, n: int) -> Deck:
    return Deck(deck_id=deck_id, commander="c", oracle_ids=tuple(f"{deck_id}_c{i}" for i in range(n)))


def test_over_legal_decks_are_dropped():
    """>100 distinct oracle_ids is definitionally impossible for a legal Commander deck — the deck
    is 100 cards and duplicate basics collapse — so this is a definitional bound, not a tuned one."""
    decks = [_sized("ok", 99), _sized("exact", 100), _sized("big", 101), _sized("huge", 1947)]
    kept, stats = filter_plausible_decks(decks)
    assert {d.deck_id for d in kept} == {"ok", "exact"}
    assert stats["n_dropped_too_large"] == 2
    assert stats["largest_dropped"] == 1947


def test_filter_is_off_by_default_at_the_low_end():
    """The sub-40 stub tail contributes ~0.6% of PPMI pairs, so excluding it buys little and costs
    an arbitrary parameter. Available, but not on by default."""
    kept, _ = filter_plausible_decks([_sized("stub", 1)])
    assert len(kept) == 1
    kept, stats = filter_plausible_decks([_sized("stub", 1)], min_distinct=40)
    assert kept == [] and stats["n_dropped_too_small"] == 1


def test_dropped_ppmi_share_exceeds_dropped_deck_share():
    """The reason the filter matters at all: co-occurrence pairs grow quadratically with deck size,
    so oversized entries dominate PPMI far beyond their headcount. One oversized deck among nine
    normal ones is 10% of decks but a majority of the pairs."""
    decks = [_sized(f"d{i}", 100) for i in range(9)] + [_sized("huge", 1000)]
    _, stats = filter_plausible_decks(decks)
    assert stats["n_dropped_too_large"] == 1
    assert stats["kept_share"] == pytest.approx(0.9)
    assert stats["dropped_ppmi_pair_share"] > 0.9


def test_loaders_are_not_filtered_implicitly():
    """T0/T1/T2 recorded their numbers on the unfiltered basis. If `load_corpus` silently changed
    what it returns, those findings would stop being reproducible from their own inputs."""
    import inspect

    from mr import corpus as C
    for fn in (C.load_casual, C.load_cedh, C.load_precon, C.load_corpus):
        assert "filter_plausible_decks" not in inspect.getsource(fn)


def test_filter_preserves_deck_objects_untouched():
    decks = [_sized("ok", 99)]
    kept, _ = filter_plausible_decks(decks)
    assert kept[0] is decks[0]


def test_max_commander_distinct_is_one_hundred():
    assert MAX_COMMANDER_DISTINCT == 100
