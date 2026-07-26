"""PPMI co-occurrence, ported from MagicSpike src/analysis/ppmi.py.

Ported rather than imported — the spin-off should not depend on an untracked tree. Logic is
unchanged: symmetric card x card PPMI over deck-card incidence, vectorized via D^T @ D.

MagicSpike's own PPMI artifact (`artifacts/cooccurrence_ppmi.npz`) was built over its combined
corpus (precon + cEDH as one pool) and cannot be split back into "cedh" vs "precon" the way this
repo's `corpus.py` defines them. So T2 builds its own PPMI per corpus from `Deck` objects, except
for the precon reference arm, which reuses `backup_precon_only/cooccurrence_ppmi.npz` directly
since that one already IS precon-only.
"""

from __future__ import annotations

import numpy as np
import scipy.sparse as sp

from .corpus import Deck


def build(decks: list[Deck]) -> tuple[sp.csr_matrix, dict[str, int]]:
    """Symmetric oracle_id x oracle_id PPMI matrix. Returns (ppmi, card_index)."""
    unique_cards = sorted({oid for d in decks for oid in d.oracle_ids})
    card_index = {oid: i for i, oid in enumerate(unique_cards)}
    n = len(card_index)

    deck_index = {d.deck_id: i for i, d in enumerate(decks)}
    n_decks = len(deck_index)

    rows, cols = [], []
    for d in decks:
        di = deck_index[d.deck_id]
        for oid in set(d.oracle_ids):
            rows.append(di)
            cols.append(card_index[oid])

    data = np.ones(len(rows), dtype=np.float32)
    D = sp.csr_matrix((data, (rows, cols)), shape=(n_decks, n))
    D.data = np.ones_like(D.data)

    count_matrix = (D.T @ D).tocsr()
    count_matrix.setdiag(0)
    count_matrix.eliminate_zeros()

    total = float(count_matrix.sum())
    if total == 0:
        raise ValueError("Empty co-occurrence matrix — no valid card pairs found.")

    row_sums = np.asarray(count_matrix.sum(axis=1)).flatten()
    coo = count_matrix.tocoo()
    r, c, d = coo.row, coo.col, coo.data.astype(np.float64)

    with np.errstate(divide="ignore", invalid="ignore"):
        log_num = np.log(d * total)
        log_den = np.log(row_sums[r]) + np.log(row_sums[c])
        vals = np.maximum(0.0, log_num - log_den)
    vals = np.where(np.isfinite(vals), vals, 0.0)

    ppmi = sp.csr_matrix((vals, (r, c)), shape=(n, n))
    ppmi.eliminate_zeros()
    return ppmi, card_index
