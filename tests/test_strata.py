"""Stratification invariants.

The spec says the stratified numbers, not the aggregate, are T4's actual output. Two properties
have to hold for them to mean anything: play counts must not leak the test set, and stratum 3 must
stay separable from stratum 2.
"""

import pandas as pd
import pytest

from mr import cdl_adapter, strata


class _Deck:
    def __init__(self, deck_id, oracle_ids):
        self.deck_id, self.oracle_ids, self.commander, self.created_at = deck_id, oracle_ids, "", None


# ── play count ────────────────────────────────────────────────────────────────

def test_play_buckets_at_their_boundaries():
    assert strata.play_count_bucket(0) == "near_zero"
    assert strata.play_count_bucket(1) == "low"
    assert strata.play_count_bucket(5) == "low"      # the frozen cold-start cut
    assert strata.play_count_bucket(6) == "mid"
    assert strata.play_count_bucket(100) == "mid"
    assert strata.play_count_bucket(101) == "high"


def test_counts_come_from_train_decks_only():
    """A card appearing only in held-out decks must land in `near_zero`. Counting test decks would
    leak: its held-out appearance is precisely what the model is being asked to predict."""
    train = [_Deck("d1", ("a", "b")), _Deck("d2", ("a",))]
    counts = strata.train_appearance_counts(train)
    assert counts.get("a") == 2
    assert counts.get("test_only") is None
    assert strata.play_count_bucket(counts.get("test_only", 0)) == "near_zero"


# ── parse status ──────────────────────────────────────────────────────────────

def test_crash_stays_separate_from_excluded():
    """Excluded means the parser deliberately declined the card; crash means it broke. Folding
    them together hides whichever is smaller, and they imply different work."""
    cdl = pd.DataFrame([
        {"oracle_id": "a", "status": cdl_adapter.STATUS_CRASH},
        {"oracle_id": "b", "status": cdl_adapter.STATUS_EXCLUDED},
    ])
    m = strata.parse_status_map(cdl)
    assert m["a"] != m["b"]
    assert cdl_adapter.STATUS_CRASH in strata.PARSE_BUCKETS
    assert cdl_adapter.STATUS_EXCLUDED in strata.PARSE_BUCKETS


# ── unusualness, and its independence from parse status ───────────────────────

def test_unusualness_buckets_at_their_boundaries():
    assert strata.unusualness_bucket(1) == "very_rare"
    assert strata.unusualness_bucket(5) == "very_rare"
    assert strata.unusualness_bucket(6) == "rare"
    assert strata.unusualness_bucket(50) == "rare"
    assert strata.unusualness_bucket(51) == "common"
    assert strata.unusualness_bucket(1001) == "very_common"
    assert strata.unusualness_bucket(None) == "unknown"


def test_rarity_is_the_minimum_not_the_mean():
    """A card is structurally unusual if it does anything unusual; averaging would let one exotic
    clause be washed out by three boilerplate ones."""
    clauses = {"card": [
        {"type": "ACTION", "tags": {}, "values": []},
        {"type": "TRIGGER", "tags": {"weird": 1}, "values": []},
    ]}
    freq = pd.DataFrame([
        {"signature": "ACTION||", "count": 9000},
        {"signature": "TRIGGER|weird|", "count": 2},
    ])
    assert strata.signature_rarity(clauses, freq)["card"] == 2


def test_unusualness_is_defined_for_cards_with_no_cdl():
    """This is the whole reason unusualness is derived from clause signatures rather than CDL
    constructs. Constructs exist only for cards that parsed, so a construct-based score would be
    undefined exactly on the `excluded`/`crash` population — stratum 3 would become a restatement
    of stratum 2 and the two would stop being separable."""
    clauses = {"unparseable": [{"type": "ACTION", "tags": {}, "values": []}]}
    freq = pd.DataFrame([{"signature": "ACTION||", "count": 3}])
    cdl = pd.DataFrame([{"oracle_id": "unparseable", "status": cdl_adapter.STATUS_EXCLUDED,
                         "constructs": "[]"}])

    rarity = strata.signature_rarity(clauses, freq)
    assert strata.unusualness_bucket(rarity["unparseable"]) == "very_rare"
    # the CDL-construct cross-check, by contrast, has nothing to say about this card
    assert strata.construct_rarity(cdl).get("unparseable") is None


def test_build_assembles_every_stratum_column():
    train = [_Deck("d1", ("a", "b")), _Deck("d2", ("a",))]
    repr_tables = {
        "cdl": pd.DataFrame([
            {"oracle_id": "a", "status": cdl_adapter.STATUS_CLEAN, "constructs": '["DAMAGE"]'},
            {"oracle_id": "b", "status": cdl_adapter.STATUS_GAP, "constructs": '["SEARCH"]'},
        ]),
        "clauses": {"a": [{"type": "ACTION", "tags": {}, "values": []}],
                    "b": [{"type": "TRIGGER", "tags": {"x": 1}, "values": []}]},
        "signature_freq": pd.DataFrame([
            {"signature": "ACTION||", "count": 5000}, {"signature": "TRIGGER|x|", "count": 3}]),
    }
    df = strata.build(train, repr_tables)
    for col in strata.STRATUM_COLUMNS:
        assert col in df.columns
    assert df.set_index("oracle_id").loc["a", "train_appearances"] == 2
    assert df.set_index("oracle_id").loc["b", "unusual_bucket"] == "very_rare"


def test_strata_are_independent_of_any_arm():
    """`build` takes decks and representation tables — there is no parameter through which an arm
    could influence a label. Guarding the signature keeps it that way."""
    import inspect
    params = set(inspect.signature(strata.build).parameters)
    assert params == {"train_decks", "repr_tables"}
    assert "arm" not in params
