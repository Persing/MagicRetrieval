"""Leave-one-out retrieval on held-out decks: the T4 end-task metric.

Remove one card from a held-out deck, rank every eligible candidate against the rest of the deck,
and record where the removed card lands. recall@50 primary, recall@10 and MRR secondary.

Nothing here knows which arm produced the embeddings. Queries, eligibility masks and stratum
labels are built once per (corpus, split) and reused byte-for-byte across all arms and seeds — so
the reported seed sd measures training variance and nothing else, and every arm-to-arm comparison
is genuinely paired rather than merely parallel.

## Four decisions worth stating rather than burying

**The candidate universe is the full commander-legal pool, not the PPMI vocabulary.** T2 embedded
`sorted(card_index, ...)` — the cards appearing in the co-occurrence matrix. Under that, a card
that never appears in a *training* deck has no embedding and silently vanishes from the candidate
set, which deletes the cold-start stratum: the entire reason a text-side encoder exists. PPMI over
training decks drives mining; the full pool drives embedding and ranking.

**Two query aggregators, both frozen up front, both always reported.** Averaging ~95 unit vectors
in 384 dimensions pulls hard toward the corpus mean and can wash out exactly the structural signal
T4 is trying to detect, so `max_sim` is carried alongside `centroid` from the start. If the arm
*ranking* flips between them that is a finding to report, not a choice to make after seeing which
one flatters the preferred arm.

**Popularity and random baselines are not optional.** The spec omits them; they are free and
arm-independent. Under the standing EDHREC-circularity confound "recommend the staples" is the
honest floor, and the cold-start argument reduces to whether structure beats popularity on cards
with few training appearances.

**The held-out card never touches its own eligibility mask.** Where a deck's commander string does
not resolve (~8.5% of casual decks), the effective colour identity falls back to the union over
*context* cards only. Including the target would leak the answer into the question.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import thresholds

AGGREGATORS = ("centroid", "max_sim")
BASELINES = ("popularity", "random")
SCORERS = AGGREGATORS + BASELINES


@dataclass
class QuerySet:
    """Everything about the eval that does not depend on the arm.

    Contexts are stored flattened with a segment id rather than as a list of arrays: every scoring
    pass needs them on the GPU, and a ragged list would mean one host-to-device copy per query.
    """
    oid_order: list[str]
    target_row: np.ndarray            # (Q,) int64 — index into oid_order
    ctx_flat: np.ndarray              # (L,) int64 — all contexts concatenated
    ctx_offsets: np.ndarray           # (Q+1,) int64 — context q is ctx_flat[off[q]:off[q+1]]
    deck_ids: list[str]
    mask_table: np.ndarray            # (M, N) bool — one row per distinct effective colour identity
    mask_idx: np.ndarray              # (Q,) int64 — which mask row each query uses
    n_dropped_no_text: int = 0
    n_dropped_target_ineligible: int = 0
    n_commander_fallback_decks: int = 0
    n_decks: int = 0
    stats: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.target_row)


# ── eligibility ───────────────────────────────────────────────────────────────

def commander_legal_pool(cards_df: pd.DataFrame) -> pd.DataFrame:
    """`legalities_commander == "legal"`.

    `recommender_eligible` is deliberately not applied on top: measured on this snapshot the two
    select the identical 30,958 rows, so adding it would imply a second filter is doing work.
    """
    pool = cards_df.drop_duplicates("oracle_id").reset_index(drop=True)
    return pool[pool["legalities_commander"] == "legal"].reset_index(drop=True)


def _color_sets(cards_df: pd.DataFrame) -> dict[str, frozenset]:
    out = {}
    for oid, ci in zip(cards_df["oracle_id"], cards_df["color_identity"]):
        out[oid] = frozenset(ci) if ci is not None and len(ci) else frozenset()
    return out


def build_queries(test_decks, cards_df: pd.DataFrame, oid_order: list[str],
                  max_decks: int | None = None) -> QuerySet:
    """One query per (test deck, distinct card in that deck).

    Exhaustive rather than sampled: it removes a whole variance source and a seed, and scoring is
    cheap — 68,816 casual queries against 31k candidates is ~1.6 TFLOP.
    """
    row_of = {oid: i for i, oid in enumerate(oid_order)}
    colors = _color_sets(cards_df)
    name_to_oid = {str(n).lower(): o for n, o in zip(cards_df["name"], cards_df["oracle_id"])}
    legal_ci = [colors.get(o, frozenset()) for o in oid_order]

    mask_cache: dict[frozenset, int] = {}
    mask_rows: list[np.ndarray] = []

    def mask_id(effective: frozenset) -> int:
        if effective not in mask_cache:
            mask_cache[effective] = len(mask_rows)
            mask_rows.append(np.fromiter((ci <= effective for ci in legal_ci),
                                         dtype=bool, count=len(legal_ci)))
        return mask_cache[effective]

    decks = list(test_decks)
    if max_decks:
        decks = decks[:max_decks]

    targets, ctx_parts, offsets, deck_ids, mask_ids = [], [], [0], [], []
    n_no_text = n_ineligible = n_cmd_fallback = 0

    for deck in decks:
        distinct = list(dict.fromkeys(deck.oracle_ids))
        rows = [row_of[o] for o in distinct if o in row_of]
        n_no_text += len(distinct) - len(rows)
        if len(rows) < 2:
            continue

        cmd_oid = name_to_oid.get((deck.commander or "").lower())
        fixed = colors.get(cmd_oid) if cmd_oid is not None else None
        if fixed is None:
            n_cmd_fallback += 1
            # Per-letter counts over the deck, so removing the target is an O(1) adjustment
            # instead of re-unioning the whole context for every one of ~95 queries.
            letters = Counter(c for r in rows for c in legal_ci[r])

        arr = np.asarray(rows, dtype=np.int64)
        for i, t in enumerate(arr):
            if fixed is not None:
                eff = fixed
            else:
                remaining = letters.copy()
                remaining.subtract(legal_ci[t])
                eff = frozenset(c for c, n in remaining.items() if n > 0)
            mid = mask_id(eff)
            if not mask_rows[mid][t]:
                # A target outside its own deck's colour identity is a data artifact, not a
                # retrieval failure. Counted explicitly rather than silently dropped.
                n_ineligible += 1
                continue
            targets.append(t)
            ctx_parts.append(np.delete(arr, i))
            offsets.append(offsets[-1] + len(arr) - 1)
            deck_ids.append(deck.deck_id)
            mask_ids.append(mid)

    return QuerySet(
        oid_order=oid_order,
        target_row=np.asarray(targets, dtype=np.int64),
        ctx_flat=(np.concatenate(ctx_parts) if ctx_parts else np.zeros(0, dtype=np.int64)),
        ctx_offsets=np.asarray(offsets, dtype=np.int64),
        deck_ids=deck_ids,
        mask_table=(np.stack(mask_rows) if mask_rows else np.zeros((0, len(oid_order)), bool)),
        mask_idx=np.asarray(mask_ids, dtype=np.int64),
        n_dropped_no_text=n_no_text,
        n_dropped_target_ineligible=n_ineligible,
        n_commander_fallback_decks=n_cmd_fallback,
        n_decks=len(decks),
    )


# ── scoring ───────────────────────────────────────────────────────────────────

def _rank(scores, targets):
    """1-indexed rank. Strictly-greater, so ties favour the target — frozen convention, matching
    MagicSpike's `_recall_at_k`. Stated because ties are rare in float cosine but not impossible,
    and an unstated convention is one that quietly differs between runs."""
    return (scores > scores.gather(1, targets[:, None])).sum(1) + 1


MAX_CTX_ROWS_PER_CHUNK = 16_384


def _chunk_bounds(ctx_offsets: np.ndarray, max_rows: int, max_queries: int) -> list[tuple[int, int]]:
    """Chunk by total *context rows*, not by query count.

    `max_sim` materializes a (context rows in chunk) x (candidates) block before reducing it, so
    memory scales with context length, which is not constant. Fixed query-count chunking was
    catastrophic on the casual corpus: most entries are ~99-card decks, but a few Archidekt lists
    carry 1,000+ cards, and a 128-query chunk over one of those is a 17 GB matmul. Chunking on
    rows bounds the block regardless of deck size, and leaves results bit-identical.
    """
    lens = np.diff(ctx_offsets)
    bounds, start, rows = [], 0, 0
    for i, ln in enumerate(lens):
        if i > start and (rows + ln > max_rows or i - start >= max_queries):
            bounds.append((start, i))
            start, rows = i, 0
        rows += int(ln)
    if start < len(lens):
        bounds.append((start, len(lens)))
    return bounds


def score_ranks(embeddings: np.ndarray, qs: QuerySet, seed: int = 0,
                chunk: int = 128, restrict_to: np.ndarray | None = None,
                max_ctx_rows: int = MAX_CTX_ROWS_PER_CHUNK) -> dict[str, np.ndarray]:
    """Per-query 1-indexed ranks under every scorer.

    `chunk` and `max_ctx_rows` are memory knobs, not speed knobs; results are identical for any
    setting. `max_ctx_rows` is the binding one — see `_chunk_bounds`.

    `restrict_to` limits the candidate universe. It exists for the clean-parse sub-report: arm D
    ranks inside a smaller pool, which inflates its recall mechanically, so `D − C` is readable
    only when *every* arm is re-scored against the same restricted universe.
    """
    import torch

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    E = torch.as_tensor(embeddings, dtype=torch.float32, device=dev)
    n = E.shape[0]
    masks = torch.as_tensor(qs.mask_table, device=dev)
    ctx_flat = torch.as_tensor(qs.ctx_flat, device=dev)
    targets_all = torch.as_tensor(qs.target_row, device=dev)
    mask_idx_all = torch.as_tensor(qs.mask_idx, device=dev)

    pop_t = torch.as_tensor(_popularity_vector(qs), dtype=torch.float32, device=dev)
    rand_t = torch.as_tensor(np.random.default_rng(seed).random(n), dtype=torch.float32, device=dev)
    restrict = torch.as_tensor(restrict_to, device=dev) if restrict_to is not None else None
    neg_inf = torch.finfo(torch.float32).min

    q = len(qs)
    out = {s: np.empty(q, dtype=np.int64) for s in SCORERS}

    for lo, hi in _chunk_bounds(qs.ctx_offsets, max_ctx_rows, chunk):
        b = hi - lo
        off = qs.ctx_offsets[lo:hi + 1]
        seg = torch.repeat_interleave(
            torch.arange(b, device=dev),
            torch.as_tensor(np.diff(off), device=dev))
        flat = ctx_flat[int(off[0]):int(off[-1])]
        tgts = targets_all[lo:hi]

        elig = masks[mask_idx_all[lo:hi]].clone()
        if restrict is not None:
            elig &= restrict
        elig[seg, flat] = False                       # context cards are not candidates
        elig[torch.arange(b, device=dev), tgts] = True  # the target always survives its own mask

        # centroid — mean of context embeddings, renormalized
        cent = torch.zeros(b, E.shape[1], device=dev)
        cent.index_add_(0, seg, E[flat])
        cent = torch.nn.functional.normalize(cent, dim=1)
        out["centroid"][lo:hi] = _rank((cent @ E.T).masked_fill(~elig, neg_inf), tgts).cpu().numpy()

        # max_sim — best similarity to any single context card.
        # `index_reduce_` takes a 1-D index over dim 0. The scatter_reduce_ form needs the index
        # broadcast to the source's shape, and `seg[:, None].expand(-1, n)` is (L, N) int64 —
        # ~3 GB of pure index at L=12k, N=31k, on top of the (L, N) similarity block itself.
        sims = E[flat] @ E.T                           # (L, N) — the memory driver
        ms = torch.full((b, n), neg_inf, device=dev)
        ms.index_reduce_(0, seg, sims, reduce="amax", include_self=True)
        del sims
        out["max_sim"][lo:hi] = _rank(ms.masked_fill(~elig, neg_inf), tgts).cpu().numpy()
        del ms

        for name, base in (("popularity", pop_t), ("random", rand_t)):
            out[name][lo:hi] = _rank(
                base.expand(b, n).masked_fill(~elig, neg_inf), tgts).cpu().numpy()

    return out


def top_candidates(embeddings: np.ndarray, qs: QuerySet, k: int = 50,
                   restrict_to: np.ndarray | None = None, chunk: int = 128,
                   max_ctx_rows: int = MAX_CTX_ROWS_PER_CHUNK) -> tuple[np.ndarray, np.ndarray]:
    """Top-k candidate rows per query under `centroid`, plus the target's rank. `(Q, k)` and `(Q,)`.

    A sibling of `score_ranks` rather than an extra return value from it: that function's contract
    is what `summarize` and every partial on disk are keyed on. Both build eligibility the same
    way here, so top-k and rank cannot disagree about which universe was searched.

    `score_ranks` answers "where did the target land"; this answers "what came back instead", which
    is what a displacement question needs. Centroid-only on purpose — `max_sim` needs the full
    (context rows x candidates) block, and that is the cost driver.

    One documented divergence: `topk` breaks ties by index while `_rank` counts strictly-greater,
    so under a mass tie a target can have `rank <= k` and still be absent from `top`. Rare in float
    cosine, pinned in the tests rather than left to be discovered.
    """
    import torch

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    E = torch.as_tensor(embeddings, dtype=torch.float32, device=dev)
    masks = torch.as_tensor(qs.mask_table, device=dev)
    ctx_flat = torch.as_tensor(qs.ctx_flat, device=dev)
    targets_all = torch.as_tensor(qs.target_row, device=dev)
    mask_idx_all = torch.as_tensor(qs.mask_idx, device=dev)
    restrict = torch.as_tensor(restrict_to, device=dev) if restrict_to is not None else None
    neg_inf = torch.finfo(torch.float32).min

    q = len(qs)
    top = np.empty((q, k), dtype=np.int64)
    rank = np.empty(q, dtype=np.int64)

    for lo, hi in _chunk_bounds(qs.ctx_offsets, max_ctx_rows, chunk):
        b = hi - lo
        off = qs.ctx_offsets[lo:hi + 1]
        seg = torch.repeat_interleave(
            torch.arange(b, device=dev), torch.as_tensor(np.diff(off), device=dev))
        flat = ctx_flat[int(off[0]):int(off[-1])]
        tgts = targets_all[lo:hi]

        elig = masks[mask_idx_all[lo:hi]].clone()
        if restrict is not None:
            elig &= restrict
        elig[seg, flat] = False
        elig[torch.arange(b, device=dev), tgts] = True

        cent = torch.zeros(b, E.shape[1], device=dev)
        cent.index_add_(0, seg, E[flat])
        cent = torch.nn.functional.normalize(cent, dim=1)
        s = (cent @ E.T).masked_fill(~elig, neg_inf)

        top[lo:hi] = s.topk(k, dim=1).indices.cpu().numpy()
        rank[lo:hi] = _rank(s, tgts).cpu().numpy()

    return top, rank


def _popularity_vector(qs: QuerySet) -> np.ndarray:
    return qs.stats.get("popularity", np.zeros(len(qs.oid_order), dtype=np.float32))


def set_popularity(qs: QuerySet, train_counts: dict[str, int]) -> None:
    """Train-deck appearance counts, aligned to `oid_order`. Test counts would leak."""
    qs.stats["popularity"] = np.asarray(
        [train_counts.get(o, 0) for o in qs.oid_order], dtype=np.float32)


# ── summarizing ───────────────────────────────────────────────────────────────

def _metrics(ranks: np.ndarray, ks: tuple[int, ...]) -> dict:
    if len(ranks) == 0:
        return {"n": 0, "mrr": None, **{f"recall_at_{k}": None for k in ks}}
    return {
        "n": int(len(ranks)),
        "mrr": float((1.0 / ranks).mean()),
        **{f"recall_at_{k}": float((ranks <= k).mean()) for k in ks},
    }


def summarize(ranks: dict[str, np.ndarray], qs: QuerySet, strata: pd.DataFrame,
              ks: tuple[int, ...] = thresholds.T4_RECALL_KS,
              query_filter: np.ndarray | None = None) -> dict:
    """`query_filter` selects which queries count.

    Needed for the clean-parse sub-report: restricting *candidates* is not enough, because a query
    whose target is not itself in the restricted universe has no correct answer available. Such
    queries are dropped rather than force-included — `score_ranks` keeps the target eligible so a
    rank always exists, and the decision about which queries are meaningful is made here.
    """
    from . import strata as S

    if query_filter is not None:
        ranks = {k: v[query_filter] for k, v in ranks.items()}
        target_rows = qs.target_row[query_filter]
    else:
        target_rows = qs.target_row

    target_oids = pd.Index([qs.oid_order[i] for i in target_rows])
    lab = strata.set_index("oracle_id")

    res: dict = {
        "n_queries": len(target_oids),
        "n_decks": qs.n_decks,
        "n_dropped_no_text": qs.n_dropped_no_text,
        "n_dropped_target_ineligible": qs.n_dropped_target_ineligible,
        "n_commander_fallback_decks": qs.n_commander_fallback_decks,
        "overall": {k: _metrics(v, ks) for k, v in ranks.items()},
        "strata": {},
    }
    for col in S.STRATUM_COLUMNS:
        labels = lab[col].reindex(target_oids).fillna("unknown").to_numpy()
        res["strata"][col] = {
            str(val): {k: _metrics(v[labels == val], ks) for k, v in ranks.items()}
            for val in sorted(set(labels))
        }

    # The gated cold-start stratum is near_zero ∪ low. `near_zero` alone stays visible in the
    # play_bucket table above but is too small to gate on — it is flagged, not merged away.
    play = lab["play_bucket"].reindex(target_oids).fillna("unknown").to_numpy()
    cold = np.isin(play, ["near_zero", "low"])
    res["coldstart_gated"] = {k: _metrics(v[cold], ks) for k, v in ranks.items()}
    res["n_near_zero"] = int((play == "near_zero").sum())
    res["near_zero_underpowered"] = bool(res["n_near_zero"] < 1000)
    return res


def evaluate(embeddings: np.ndarray, qs: QuerySet, strata: pd.DataFrame, seed: int = 0,
             chunk: int = 128, restrict_to: np.ndarray | None = None,
             ks: tuple[int, ...] = thresholds.T4_RECALL_KS) -> dict:
    ranks = score_ranks(embeddings, qs, seed=seed, chunk=chunk, restrict_to=restrict_to)
    qf = restrict_to[qs.target_row] if restrict_to is not None else None
    return summarize(ranks, qs, strata, ks=ks, query_filter=qf)
