"""Mining disjointness — T2's leakage trap.

If a mined training pair coincides with an eval pair, the fine-tune is training on its own test
set. Two exclusion variants are tested: pair-only (exact eval pairs) and strict (any pair touching
any eval card) — the gap between them is what bounds undisclosed leakage.
"""

import numpy as np
import pandas as pd
import scipy.sparse as sp

from mr import mining


def _toy_ppmi(n: int = 6) -> tuple[sp.csr_matrix, dict]:
    """6 cards, dense-ish PPMI so top-decile mining has real candidates."""
    card_index = {f"c{i}": i for i in range(n)}
    rng = np.random.default_rng(0)
    mat = rng.random((n, n))
    mat = (mat + mat.T) / 2
    np.fill_diagonal(mat, 0)
    return sp.csr_matrix(mat), card_index


def test_eval_exclusion_sets():
    eval_pairs = [("c0", "c1"), ("c2", "c3")]
    pair_only, touched = mining.eval_exclusion_sets(eval_pairs)
    assert frozenset(("c0", "c1")) in pair_only
    assert touched == {"c0", "c1", "c2", "c3"}


def test_mine_positives_excludes_eval_pair_and_strict_variant():
    ppmi, card_index = _toy_ppmi()
    eval_pairs = [("c0", "c1")]
    pair_only_excl, touched = mining.eval_exclusion_sets(eval_pairs)

    pairs, strict_pairs = mining.mine_positives(
        ppmi, card_index, pair_only_excl, touched, top_decile_fraction=0.9
    )
    assert frozenset(("c0", "c1")) not in {frozenset(p) for p in pairs}
    # Strict excludes ANY pair touching c0 or c1, so it must be a subset of pair_only results.
    strict_keys = {frozenset(p) for p in strict_pairs}
    pair_keys = {frozenset(p) for p in pairs}
    assert strict_keys <= pair_keys
    assert not any(("c0" in p or "c1" in p) for p in strict_pairs)


def test_mine_hard_negatives_respects_exclusions_and_shared_colour():
    ppmi, card_index = _toy_ppmi(n=8)
    # Zero out co-occurrence between c0 and c1..c7 so they're eligible negative candidates.
    ppmi = ppmi.tolil()
    ppmi[0, :] = 0
    ppmi[:, 0] = 0
    ppmi = ppmi.tocsr()

    cards_df = pd.DataFrame({
        "oracle_id": [f"c{i}" for i in range(8)],
        "color_identity": [["W"]] * 4 + [["U"]] * 4,
    })
    eval_pairs = [("c0", "c2")]
    pair_only_excl, touched = mining.eval_exclusion_sets(eval_pairs)

    pairs, strict_pairs = mining.mine_hard_negatives(
        ppmi, card_index, cards_df, pair_only_excl, touched, positive_pairs=set(),
        sample_size=8,
    )
    assert frozenset(("c0", "c2")) not in {frozenset(p) for p in pairs}
    # c0 is W; only W-colour cards (c1..c3) are valid negative partners for it.
    for a, b in pairs:
        if a == "c0":
            assert b in ("c1", "c2", "c3")


def test_mine_phenotype_pairs_shapes():
    ppmi, card_index = _toy_ppmi(n=10)
    order = sorted(card_index, key=lambda o: card_index[o])
    embeddings = np.random.default_rng(1).normal(size=(10, 16))
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)

    pair_only_excl, touched = mining.eval_exclusion_sets([("c0", "c1")])
    result = mining.mine_phenotype_pairs(
        ppmi, card_index, embeddings, order, pair_only_excl, touched, n_pairs=5
    )
    assert set(result) == {"extra_positives", "extra_negatives"}
    for key in result:
        pair_only, strict = result[key]
        assert frozenset(("c0", "c1")) not in {frozenset(p) for p in pair_only}
        assert len(strict) <= len(pair_only)
