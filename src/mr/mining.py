"""T2 pair mining: positives, hard negatives, and the mined-phenotype pairs for arm 2d.

Every miner takes an `exclude` set of frozenset({oid_a, oid_b}) and never emits a pair inside it.
`eval_exclusion_sets` builds two such sets from the frozen eval CSV — a **strict** one (any pair
touching any eval card) and a **pair-only** one (only the exact eval pairs) — and every mined set
is built under both, so the gap between them bounds how much of any result is pair-level disjointness
laundering distributional leakage.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import scipy.sparse as sp

from . import config

TOP_DECILE_FRACTION = 0.10
NEGATIVE_SAMPLE_CAP = 4000  # MagicSpike's original code capped this at 500 of 13,262; raised here


def eval_exclusion_sets(eval_pairs: list[tuple[str, str]]) -> tuple[set[frozenset], set[str]]:
    """Return (pair_only_exclusion, cards_touched). `pair_only` excludes exactly the eval pairs;
    `cards_touched` is every oracle_id appearing in eval, for building the strict exclusion."""
    pair_only = {frozenset((a, b)) for a, b in eval_pairs}
    touched = {oid for pair in eval_pairs for oid in pair}
    return pair_only, touched


def _strict_exclude(pair: tuple[str, str], touched: set[str]) -> bool:
    return pair[0] in touched or pair[1] in touched


def mine_positives(
    ppmi: sp.csr_matrix,
    card_index: dict[str, int],
    exclude_pair_only: set[frozenset],
    exclude_touched: set[str],
    top_decile_fraction: float = TOP_DECILE_FRACTION,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Top-decile PPMI pairs. Returns (pairs_under_pair_exclusion, pairs_under_strict_exclusion)."""
    idx_to_oid = {v: k for k, v in card_index.items()}
    coo = ppmi.tocoo()
    if coo.nnz == 0:
        return [], []
    threshold = np.percentile(coo.data, (1 - top_decile_fraction) * 100)

    pair_only: list[tuple[str, str]] = []
    strict: list[tuple[str, str]] = []
    seen: set[frozenset] = set()
    for i, j, v in zip(coo.row, coo.col, coo.data):
        if i >= j or v < threshold:
            continue
        oid_a, oid_b = idx_to_oid.get(i), idx_to_oid.get(j)
        if not oid_a or not oid_b:
            continue
        key = frozenset((oid_a, oid_b))
        if key in seen:
            continue
        seen.add(key)
        if key in exclude_pair_only:
            continue
        pair_only.append((oid_a, oid_b))
        if not _strict_exclude((oid_a, oid_b), exclude_touched):
            strict.append((oid_a, oid_b))
    return pair_only, strict


def mine_hard_negatives(
    ppmi: sp.csr_matrix,
    card_index: dict[str, int],
    cards_df: pd.DataFrame,
    exclude_pair_only: set[frozenset],
    exclude_touched: set[str],
    positive_pairs: set[frozenset],
    seed: int = config.SEED,
    sample_size: int = NEGATIVE_SAMPLE_CAP,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Same-colour-identity, zero-co-occurrence pairs. Returns (pair_only, strict) variants.

    MagicSpike's original implementation sampled only 500 of 13,262 anchor cards
    (src/analysis/embeddings.py:100) and — separately — computed these negatives but never passed
    them to the loss (see T2 context). Both are fixed here: the sample cap is raised, and
    `finetune.py` actually wires this output into training.
    """
    oid_to_color = dict(zip(cards_df["oracle_id"], cards_df["color_identity"]))
    idx_to_oid = {v: k for k, v in card_index.items()}
    all_oids = list(card_index.keys())

    rng = np.random.default_rng(seed)
    sample_size = min(sample_size, len(all_oids))
    anchors = [all_oids[i] for i in rng.choice(len(all_oids), size=sample_size, replace=False)]

    ppmi_csr = ppmi.tocsr()
    pair_only: list[tuple[str, str]] = []
    strict: list[tuple[str, str]] = []
    seen: set[frozenset] = set()

    for oid_a in anchors:
        ci_a = card_index.get(oid_a)
        if ci_a is None:
            continue
        ci_val = oid_to_color.get(oid_a)
        color_a = set(ci_val) if ci_val is not None and len(ci_val) else set()
        row = ppmi_csr.getrow(ci_a).toarray().flatten()
        zero_idx = np.where(row == 0)[0]
        if len(zero_idx) == 0:
            continue
        candidates = []
        for ci_b in zero_idx:
            oid_b = idx_to_oid.get(ci_b)
            if not oid_b or oid_b == oid_a:
                continue
            cb_val = oid_to_color.get(oid_b)
            color_b = set(cb_val) if cb_val is not None and len(cb_val) else set()
            if color_a.isdisjoint(color_b):
                continue  # require shared colour — otherwise this is a trivial negative
            key = frozenset((oid_a, oid_b))
            if key in seen or key in positive_pairs or key in exclude_pair_only:
                continue
            candidates.append(oid_b)
        if not candidates:
            continue
        chosen = candidates[rng.integers(len(candidates))]
        key = frozenset((oid_a, chosen))
        seen.add(key)
        pair_only.append((oid_a, chosen))
        if not _strict_exclude((oid_a, chosen), exclude_touched):
            strict.append((oid_a, chosen))
    return pair_only, strict


def mine_phenotype_pairs(
    ppmi: sp.csr_matrix,
    card_index: dict[str, int],
    text_embeddings: np.ndarray,
    embedding_order: list[str],
    exclude_pair_only: set[frozenset],
    exclude_touched: set[str],
    n_pairs: int = 300,
    seed: int = config.SEED,
) -> dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]]:
    """Arm 2d's two mined-phenotype sets, each returned as (pair_only, strict):

      extra_negatives — text-similar, co-occurrence-distant (the confusable near-miss pairs a
                        text-only model would wrongly pull together)
      extra_positives — text-distant, co-occurrence-close (the true substitutes a text-only model
                        would wrongly push apart — this IS the leakage failure mode from Gate 1-3)

    `text_embeddings` must be zero-shot (pre-fine-tune) so the mining criterion is independent of
    what any arm's fine-tune later produces — mining with an arm's own geometry would be circular.
    """
    oid_to_row = {oid: i for i, oid in enumerate(embedding_order)}
    idx_to_oid = {v: k for k, v in card_index.items()}
    coo = ppmi.tocoo()

    rng = np.random.default_rng(seed)
    cand_idx = rng.choice(coo.nnz, size=min(20000, coo.nnz), replace=False)

    scored_close, scored_distant = [], []
    for k in cand_idx:
        i, j, v = coo.row[k], coo.col[k], coo.data[k]
        if i >= j:
            continue
        oid_a, oid_b = idx_to_oid.get(i), idx_to_oid.get(j)
        if not oid_a or not oid_b or oid_a not in oid_to_row or oid_b not in oid_to_row:
            continue
        key = frozenset((oid_a, oid_b))
        if key in exclude_pair_only:
            continue
        ra, rb = text_embeddings[oid_to_row[oid_a]], text_embeddings[oid_to_row[oid_b]]
        cos = float(np.dot(ra, rb))  # embeddings are pre-normalized
        scored_close.append((v, cos, oid_a, oid_b))

    # co-occurrence-close, text-distant → extra positives (the true leakage-fixing signal)
    scored_close.sort(key=lambda t: (-t[0], t[1]))
    # co-occurrence-distant (v near 0, drawn from zero-PPMI same-colour space is expensive here;
    # approximate with the lowest-PPMI tail among sampled nonzero pairs), text-similar → negatives
    scored_distant = sorted(scored_close, key=lambda t: (t[0], -t[1]))

    def split(scored: list, cap: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        pair_only, strict = [], []
        for _, _, a, b in scored:
            if len(pair_only) >= cap:
                break
            pair_only.append((a, b))
            if not _strict_exclude((a, b), exclude_touched):
                strict.append((a, b))
        return pair_only, strict

    return {
        "extra_positives": split(scored_close[:5000], n_pairs),   # high PPMI, low cosine ranks first
        "extra_negatives": split(scored_distant[:5000], n_pairs),  # low PPMI, high cosine ranks first
    }
