"""The three T4 stratifications. All arm-independent, all computed before any arm is named.

The spec is emphatic that the stratified numbers, not the aggregate, are the actual output of
T4 — "if arms tie on the aggregate but separate here, that's the finding". These labels are
therefore computed once per (corpus, split) and reused byte-for-byte across every arm and seed,
which is what makes the per-stratum comparison paired rather than merely parallel.

  1. play count           — the cold-start proxy, and the entire reason a text-side encoder exists
  2. parse status         — the folded-in T3: how gracefully does arm C degrade off the head?
  3. structural unusualness — parse difficulty correlates with weirdness, so a uniform sample
                              overstates fallback quality

Stratifications 2 and 3 must stay *separable*, which drives the one non-obvious choice here:
unusualness is derived from clause signatures, not from CDL constructs. See `unusualness_bucket`.
"""

from __future__ import annotations

import json

import pandas as pd

from . import cdl_adapter, representations, thresholds

# ── 1. play count ─────────────────────────────────────────────────────────────

PLAY_BUCKETS = ("near_zero", "low", "mid", "high")


def play_count_bucket(count: int) -> str:
    """Buckets frozen against the measured casual distribution.

    `near_zero` (0 train appearances) holds only 719 of 71,566 held-out targets — 1.0% — where a
    +0.03 recall@50 difference sits at roughly 2σ binomial and would not reliably be visible. The
    cold-start gate therefore binds on `near_zero ∪ low` (≤5 appearances, 5,770 targets, 8.4%) and
    `near_zero` is reported separately, flagged underpowered, rather than being quietly widened
    until it looked significant.
    """
    if count <= 0:
        return "near_zero"
    if count <= thresholds.T4_COLDSTART_MAX_COUNT:
        return "low"
    if count <= 100:
        return "mid"
    return "high"


def train_appearance_counts(train_decks) -> dict[str, int]:
    """Appearances across **training** decks only.

    Counting test decks too would leak: a card's held-out appearance is the very thing being
    predicted, so its own frequency in the test set is not information the model could have had.
    """
    from . import corpus
    return corpus.appearance_counts(train_decks).to_dict()


# ── 2. parse status ───────────────────────────────────────────────────────────

PARSE_BUCKETS = (cdl_adapter.STATUS_CLEAN, cdl_adapter.STATUS_GAP,
                 cdl_adapter.STATUS_CRASH, cdl_adapter.STATUS_EXCLUDED)


def parse_status_map(cdl_table: pd.DataFrame) -> dict[str, str]:
    """Four buckets, not the spec's three.

    `crash` stays separate from `excluded`. Folding them is exactly the misattribution
    `t0_coverage` warns against: excluded means the parser deliberately declined a card
    (multi-faced, digital, non-playable layout), crash means it tried and broke. One is a scope
    decision and the other is a bug, and averaging them together hides whichever is smaller.
    """
    return dict(zip(cdl_table["oracle_id"], cdl_table["status"]))


# ── 3. structural unusualness ─────────────────────────────────────────────────

UNUSUAL_BUCKETS = ("very_rare", "rare", "common", "very_common")


def signature_rarity(clause_table: dict[str, list[dict]],
                     signature_freq: pd.DataFrame) -> dict[str, int]:
    """Per card: the corpus frequency of its *rarest* clause signature.

    Minimum rather than mean — a card is structurally unusual if it does anything unusual, and
    averaging would let a single exotic clause be washed out by three boilerplate ones.
    """
    freq = dict(zip(signature_freq["signature"], signature_freq["count"]))
    out: dict[str, int] = {}
    for oid, clauses in clause_table.items():
        if not clauses:
            continue
        out[oid] = min(freq.get(representations.signature_key(
            representations.clause_signature(cl)), 0) for cl in clauses)
    return out


def unusualness_bucket(rarity: int | None) -> str:
    """Frozen cut points on the rarest-signature frequency.

    **Derived from clause signatures, not CDL constructs — deliberately.** The spec offers either
    ("cards needing late spec additions, or whose CDL/clause structure uses rare constructs"), but
    CDL constructs only exist for cards that parsed, so a construct-based score would be defined
    on exactly the `clean`/`gap` population and undefined elsewhere. Stratification 3 would then be
    correlated with stratification 2 by construction and the two would stop being separable — you
    could not tell "arm C degrades on gapped cards" from "arm C degrades on weird cards". Clause
    signatures exist for essentially every card with oracle text, so this stays independent.
    """
    if rarity is None:
        return "unknown"
    if rarity <= 5:
        return "very_rare"
    if rarity <= 50:
        return "rare"
    if rarity <= 1000:
        return "common"
    return "very_common"


def construct_rarity(cdl_table: pd.DataFrame) -> dict[str, int]:
    """Secondary cross-check only: rarest CDL construct per card, over cards that parsed.

    Reported alongside the clause-signature version so the two can be compared, never used as the
    primary stratum — see `unusualness_bucket` for why.
    """
    counts: dict[str, int] = {}
    per_card: dict[str, list[str]] = {}
    for row in cdl_table.itertuples(index=False):
        cons = json.loads(row.constructs or "[]")
        if not cons:
            continue
        per_card[row.oracle_id] = cons
        for c in cons:
            counts[c] = counts.get(c, 0) + 1
    return {oid: min(counts[c] for c in cons) for oid, cons in per_card.items()}


# ── assembly ──────────────────────────────────────────────────────────────────

def build(train_decks, repr_tables: dict) -> pd.DataFrame:
    """One row per oracle_id with every stratum label. Arm-independent by construction — nothing
    in this function can see which arm is about to be trained."""
    counts = train_appearance_counts(train_decks)
    status = parse_status_map(repr_tables["cdl"])
    rarity = signature_rarity(repr_tables["clauses"], repr_tables["signature_freq"])
    c_rarity = construct_rarity(repr_tables["cdl"])

    oids = sorted(set(status) | set(rarity) | set(counts))
    return pd.DataFrame([{
        "oracle_id": oid,
        "train_appearances": counts.get(oid, 0),
        "play_bucket": play_count_bucket(counts.get(oid, 0)),
        "parse_status": status.get(oid, "unknown"),
        "signature_rarity": rarity.get(oid),
        "unusual_bucket": unusualness_bucket(rarity.get(oid)),
        "construct_rarity": c_rarity.get(oid),
        "unusual_bucket_cdl": unusualness_bucket(c_rarity.get(oid)),
    } for oid in oids])


STRATUM_COLUMNS = ("play_bucket", "parse_status", "unusual_bucket")
