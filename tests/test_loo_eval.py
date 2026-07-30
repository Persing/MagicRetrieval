"""LOO retrieval metrics, verified against planted answers.

recall@50 is T4's primary metric and every gate is denominated in it, so the arithmetic gets
tested against constructed cases where the correct answer is known by hand — not against a model,
whose output would make a wrong metric look plausible.
"""

import numpy as np
import pandas as pd
import pytest

from mr import loo_eval


class _Deck:
    def __init__(self, deck_id, commander, oracle_ids):
        self.deck_id, self.commander, self.oracle_ids = deck_id, commander, oracle_ids


N = 60
OIDS = [f"o{i}" for i in range(N)]


@pytest.fixture
def cards():
    return pd.DataFrame([{
        "oracle_id": o, "name": f"C{i}", "color_identity": np.array(["G"]),
        "legalities_commander": "legal",
    } for i, o in enumerate(OIDS)])


@pytest.fixture
def strata():
    return pd.DataFrame([{
        "oracle_id": o, "play_bucket": "mid", "parse_status": "clean",
        "unusual_bucket": "common", "train_appearances": 10,
    } for o in OIDS])


def _unit(sims: np.ndarray, dim: int = 8) -> np.ndarray:
    """Rows whose cosine against row 0 is exactly `sims[i]`.

    The obvious construction — put the similarity in coordinate 0 and leave the rest zero — does
    not survive the L2 normalization every embedding here is subject to: each row collapses to the
    same unit vector and all the planted structure is lost. Carrying the complement in coordinate 1
    keeps `E[i] · E[0] == sims[i]` after normalization.
    """
    E = np.zeros((len(sims), dim), dtype=np.float32)
    E[:, 0] = sims
    E[:, 1] = np.sqrt(np.maximum(0.0, 1.0 - sims ** 2))
    return E


def _planted(target_row: int, rank: int, context_row: int = 0, dim: int = 8) -> np.ndarray:
    """Embeddings where `target_row` lands at exactly `rank` for a centroid query whose context is
    the single card `context_row`."""
    sims = np.full(N, 0.10, dtype=np.float32)
    sims[context_row] = 1.0                     # the context card is the query direction itself
    sims[target_row] = 0.50
    # Exactly rank-1 eligible candidates must score strictly above the target. Eligible means
    # everything except the context card (excluded as already-in-deck) and the target itself.
    better = [i for i in range(N) if i not in (context_row, target_row)][: rank - 1]
    sims[better] = 0.90
    return _unit(sims, dim)


def _qs(cards, contexts_and_targets):
    decks = [_Deck(f"d{i}", "C0", tuple(OIDS[j] for j in members))
             for i, members in enumerate(contexts_and_targets)]
    return loo_eval.build_queries(decks, cards, OIDS)


def test_rank_one_gives_perfect_recall_and_mrr(cards, strata):
    qs = _qs(cards, [[0, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    E = _planted(target_row=5, rank=1)
    res = loo_eval.evaluate(E, qs, strata)
    # query with target=5 and context=[0] is the one we planted
    ranks = loo_eval.score_ranks(E, qs)["centroid"]
    tgt = list(qs.target_row).index(5)
    assert ranks[tgt] == 1
    assert res["overall"]["centroid"]["recall_at_10"] is not None


@pytest.mark.parametrize("planted_rank,expect_at_10,expect_at_50", [
    (1, True, True),
    (10, True, True),
    (11, False, True),
    (50, False, True),
    (51, False, False),
])
def test_recall_boundaries_are_inclusive(cards, strata, planted_rank, expect_at_10, expect_at_50):
    qs = _qs(cards, [[0, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    tgt = list(qs.target_row).index(5)
    ranks = loo_eval.score_ranks(_planted(5, planted_rank), qs)["centroid"]
    assert ranks[tgt] == planted_rank
    assert bool(ranks[tgt] <= 10) is expect_at_10
    assert bool(ranks[tgt] <= 50) is expect_at_50


def test_mrr_is_the_reciprocal_of_the_rank(cards, strata):
    qs = _qs(cards, [[0, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    ranks = {"centroid": np.array([51])}
    m = loo_eval._metrics(ranks["centroid"], (10, 50))
    assert m["mrr"] == pytest.approx(1 / 51)
    assert m["recall_at_50"] == 0.0


def test_context_cards_are_never_candidates(cards, strata):
    """A context card scoring 1.0 must not occupy a rank slot — it is already in the deck, so
    'retrieving' it is not a recommendation."""
    qs = _qs(cards, [[0, 1, 2, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    sims = np.full(N, 0.10, dtype=np.float32)
    sims[[0, 1, 2]] = 1.0     # every context card is maximally similar to the query
    sims[5] = 0.5
    ranks = loo_eval.score_ranks(_unit(sims), qs)["centroid"]
    tgt = [i for i, t in enumerate(qs.target_row) if t == 5][0]
    assert ranks[tgt] == 1, "context cards leaked into the candidate set"


def test_target_is_never_in_its_own_context(cards):
    qs = _qs(cards, [[0, 1, 2, 3]])
    for i, t in enumerate(qs.target_row):
        ctx = qs.ctx_flat[qs.ctx_offsets[i]:qs.ctx_offsets[i + 1]]
        assert t not in set(ctx.tolist())


def test_ineligible_card_cannot_displace_the_target(cards, strata):
    """A blue card in a mono-green deck must be masked out even when it scores top."""
    cards = cards.copy()
    cards.loc[cards["oracle_id"] == "o7", "color_identity"] = pd.Series(
        [np.array(["U"])], index=cards.index[cards["oracle_id"] == "o7"])
    qs = _qs(cards, [[0, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    sims = np.full(N, 0.10, dtype=np.float32)
    sims[0] = 1.0
    sims[7] = 0.99      # off-colour card, would rank first if eligibility were ignored
    sims[5] = 0.5
    ranks = loo_eval.score_ranks(_unit(sims), qs)["centroid"]
    tgt = [i for i, t in enumerate(qs.target_row) if t == 5][0]
    assert ranks[tgt] == 1


def test_ties_favour_the_target(cards, strata):
    """Frozen convention: rank counts *strictly* greater scores, so a tie does not push the
    target down. Unstated, this silently differs between implementations."""
    qs = _qs(cards, [[0, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    sims = np.full(N, 0.5, dtype=np.float32)   # every candidate ties with the target
    sims[0] = 1.0
    ranks = loo_eval.score_ranks(_unit(sims), qs)["centroid"]
    tgt = [i for i, t in enumerate(qs.target_row) if t == 5][0]
    assert ranks[tgt] == 1


def test_commander_fallback_never_uses_the_target(cards):
    """When the commander does not resolve, the effective identity comes from context cards only.
    If the target contributed, an off-colour target would mask itself in and the eligibility
    filter would be scored against the answer."""
    cards = cards.copy()
    idx = cards.index[cards["oracle_id"] == "o5"]
    cards.loc[idx, "color_identity"] = pd.Series([np.array(["U"])], index=idx)
    decks = [_Deck("d0", "NoSuchCommander", ("o0", "o1", "o5"))]
    qs = loo_eval.build_queries(decks, cards, OIDS)
    assert qs.n_commander_fallback_decks == 1
    # o5 is blue, its mono-green context cannot make it eligible -> counted, not silently dropped
    assert qs.n_dropped_target_ineligible == 1
    assert 5 not in set(qs.target_row.tolist())


def test_cards_without_embeddings_are_counted_not_dropped_silently(cards):
    decks = [_Deck("d0", "C0", ("o0", "o1", "missing-oid"))]
    qs = loo_eval.build_queries(decks, cards, OIDS)
    assert qs.n_dropped_no_text == 1


def test_max_sim_is_the_max_over_context_not_the_mean(cards, strata):
    """Pins the semantics of the aggregator, independently of how it is computed. A candidate
    similar to ONE context card must outrank a candidate mildly similar to all of them — that is
    the whole reason max_sim is carried alongside centroid."""
    dim = 8
    E = np.zeros((N, dim), dtype=np.float32)
    E[0, 2] = 1.0          # context card A
    E[1, 3] = 1.0          # context card B
    E[2] = E[0]            # candidate: identical to one context card  -> max_sim 1.0
    E[3, 2] = E[3, 3] = np.sqrt(0.5)   # candidate: halfway between   -> max_sim 0.707
    for i in range(4, N):
        E[i, 4] = 1.0      # everything else is orthogonal to the context
    E[5] = E[3]            # make the target the halfway card
    qs = _qs(cards, [[0, 1, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    r = loo_eval.score_ranks(E, qs)
    tgt = [i for i, t in enumerate(qs.target_row) if t == 5][0]
    # card 2 (max_sim 1.0) must beat the target (max_sim 0.707) under max_sim
    assert r["max_sim"][tgt] == 2
    # under centroid the target sits ON the centroid direction and wins outright
    assert r["centroid"][tgt] == 1


def test_max_sim_and_centroid_can_disagree(cards, strata):
    """The two aggregators are both reported precisely because they can rank differently; if they
    could not, carrying both would be theatre."""
    qs = _qs(cards, [[0, 1, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    rng = np.random.default_rng(0)
    E = rng.normal(size=(N, 16)).astype(np.float32)
    E /= np.linalg.norm(E, axis=1, keepdims=True)
    r = loo_eval.score_ranks(E, qs)
    assert set(r) == set(loo_eval.SCORERS)
    assert all(len(v) == len(qs) for v in r.values())


def test_restrict_to_shrinks_the_candidate_universe(cards, strata):
    """Arm D's sub-report depends on this: restricting candidates mechanically improves rank, so
    every arm must be re-scored under the same restriction for D − C to mean anything."""
    qs = _qs(cards, [[0, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    E = _planted(5, rank=30)
    full = loo_eval.score_ranks(E, qs)["centroid"]
    keep = np.zeros(N, dtype=bool)
    keep[[0, 5, 6, 7]] = True
    restricted = loo_eval.score_ranks(E, qs, restrict_to=keep)["centroid"]
    tgt = [i for i, t in enumerate(qs.target_row) if t == 5][0]
    assert restricted[tgt] < full[tgt]


def test_summarize_reports_every_stratum_column(cards, strata):
    qs = _qs(cards, [[0, 1, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    res = loo_eval.evaluate(_planted(5, 3), qs, strata)
    assert set(res["strata"]) == {"play_bucket", "parse_status", "unusual_bucket"}
    assert res["overall"]["popularity"]["n"] == len(qs)
    assert res["overall"]["random"]["n"] == len(qs)


# ── top_candidates: the sibling D2 needs ──────────────────────────────────────

def test_top_candidates_rank_matches_score_ranks(cards, strata):
    """The contract that keeps displacement accounting and recall on one universe. If these two
    ever disagree, D2's slot counts describe a different search than the reported recall."""
    qs = _qs(cards, [[0, 1, 5], [2, 3, 7]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    rng = np.random.default_rng(3)
    E = rng.normal(size=(N, 16)).astype(np.float32)
    E /= np.linalg.norm(E, axis=1, keepdims=True)
    _, rank = loo_eval.top_candidates(E, qs, k=10)
    assert np.array_equal(rank, loo_eval.score_ranks(E, qs)["centroid"])


def test_top_candidates_excludes_context_and_ineligible(cards, strata):
    cards = cards.copy()
    idx = cards.index[cards["oracle_id"] == "o7"]
    cards.loc[idx, "color_identity"] = pd.Series([np.array(["U"])], index=idx)
    qs = _qs(cards, [[0, 1, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    sims = np.full(N, 0.10, dtype=np.float32)
    sims[[0, 1]] = 1.0          # context cards
    sims[7] = 0.99              # off-colour
    sims[5] = 0.50
    top, _ = loo_eval.top_candidates(_unit(sims), qs, k=10)
    for row in top:
        assert 7 not in row, "off-colour card must not be retrievable"
    q = [i for i, t in enumerate(qs.target_row) if t == 5][0]
    assert 0 not in top[q] and 1 not in top[q], "context cards must not be retrievable"


def test_top_candidates_honours_restrict_to(cards, strata):
    qs = _qs(cards, [[0, 5]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    keep = np.zeros(N, dtype=bool)
    keep[[0, 5, 6, 7, 8]] = True
    top, _ = loo_eval.top_candidates(_planted(5, 3), qs, k=3, restrict_to=keep)
    assert set(top.ravel().tolist()) <= {5, 6, 7, 8}


def test_top_candidates_tie_divergence_is_pinned():
    """Documented, not accidental: topk breaks ties by index, _rank counts strictly-greater. Under
    a mass tie the target can have rank <= k and be absent from top."""
    cards = pd.DataFrame([{"oracle_id": o, "name": f"C{i}", "color_identity": np.array(["G"]),
                           "legalities_commander": "legal"} for i, o in enumerate(OIDS)])
    qs = _qs(cards, [[0, 40]])
    loo_eval.set_popularity(qs, {o: 1 for o in OIDS})
    sims = np.full(N, 0.5, dtype=np.float32)     # everything ties
    sims[0] = 1.0
    top, rank = loo_eval.top_candidates(_unit(sims), qs, k=5)
    q = [i for i, t in enumerate(qs.target_row) if t == 40][0]
    assert rank[q] == 1
    assert 40 not in top[q], "tie-break divergence is expected here; if this fails, revisit the docstring"
