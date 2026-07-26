"""Corpus split integrity — the plan's central leakage trap.

Co-occurrence is a deck-level property; a random pair split leaks the same deck across train and
test. `split_by_deck` and `temporal_split` must never let a deck_id land on both sides.
"""

import pytest

from mr.corpus import Deck, assert_no_deck_leak, split_by_deck, temporal_split


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
