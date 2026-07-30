"""Deck corpus loading, oracle-id resolution, and splits.

Three corpora, deliberately kept distinct rather than merged:

  casual  — 5,000 Archidekt decks (archidekt_census_popular.jsonl), has timestamps
  cedh    — 46,798 EDHTop16 tournament decks (decks_combined.parquet, source=edhtop16)
  precon  — 224 MTGJSON precons (backup_precon_only/decks_precon.parquet), the T2 arm-2a reference

**Splits are by deck, never by card-deck pair.** Co-occurrence is a deck-level property; a random
pair split puts the same deck on both sides and leaks it. `split_by_deck` and `temporal_split` both
partition deck_ids and are the only supported way to get a train/test boundary here.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import config


@dataclass(frozen=True)
class Deck:
    deck_id: str
    commander: str
    oracle_ids: tuple[str, ...]
    created_at: str | None = None


@dataclass
class ResolutionStats:
    n_decks: int = 0
    n_decks_resolved: int = 0
    n_names_total: int = 0
    n_resolved_exact: int = 0
    n_resolved_frontface: int = 0
    n_unresolved: int = 0
    unresolved_examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["unresolved_examples"] = self.unresolved_examples[:50]
        d["name_resolution_rate"] = (
            self.n_resolved_exact + self.n_resolved_frontface
        ) / self.n_names_total if self.n_names_total else 0.0
        return d


# ── Card / oracle data ────────────────────────────────────────────────────────

def load_oracle_cards(path: Path | None = None) -> dict[str, dict]:
    """{oracle_id: scryfall card dict} from the pinned Scryfall oracle snapshot."""
    path = path or config.SCRYFALL_ORACLE
    out: dict[str, dict] = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            card = json.loads(line)
            oid = card.get("oracle_id")
            if oid:
                out[oid] = card
    return out


def load_cards_parquet(path: Path | None = None) -> pd.DataFrame:
    """MagicSpike's filtered card table (oracle_id, name, oracle_text, type_line, ...)."""
    return pd.read_parquet(path or config.CARDS_PARQUET)


def build_name_lookup(oracle_cards: dict[str, dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (exact_lut, frontface_lut), both {lowercase_name: oracle_id}.

    `exact_lut` reproduces MagicSpike's resolution (run_rung0_5_tags.py::build_name_lookup) so
    join rates stay comparable. `frontface_lut` is an addition: Archidekt writes DFC and split
    cards inconsistently, sometimes "Front // Back" and sometimes just "Front". Front-face
    resolutions are counted separately so the extension's contribution is always visible rather
    than silently inflating the join rate.
    """
    exact: dict[str, str] = {}
    frontface: dict[str, str] = {}
    for oid, card in oracle_cards.items():
        name = (card.get("name") or "").lower()
        if not name:
            continue
        exact[name] = oid
        if " // " in name:
            front = name.split(" // ", 1)[0]
            frontface.setdefault(front, oid)
    return exact, frontface


def resolve_names(
    names: list[str],
    exact: dict[str, str],
    frontface: dict[str, str],
    stats: ResolutionStats | None = None,
) -> list[str]:
    out: list[str] = []
    for n in names:
        key = n.lower()
        if stats is not None:
            stats.n_names_total += 1
        oid = exact.get(key)
        if oid is not None:
            if stats is not None:
                stats.n_resolved_exact += 1
            out.append(oid)
            continue
        oid = frontface.get(key) or frontface.get(key.split(" // ", 1)[0])
        if oid is not None:
            if stats is not None:
                stats.n_resolved_frontface += 1
            out.append(oid)
            continue
        if stats is not None:
            stats.n_unresolved += 1
            if len(stats.unresolved_examples) < 200:
                stats.unresolved_examples.append(n)
    return out


# ── Corpus loaders ────────────────────────────────────────────────────────────

def load_casual(
    path: Path | None = None,
    oracle_cards: dict[str, dict] | None = None,
    limit: int | None = None,
) -> tuple[list[Deck], ResolutionStats]:
    path = path or config.CENSUS_JSONL
    oracle_cards = oracle_cards if oracle_cards is not None else load_oracle_cards()
    exact, frontface = build_name_lookup(oracle_cards)

    decks: list[Deck] = []
    stats = ResolutionStats()
    with path.open() as fh:
        for line in fh:
            if limit is not None and len(decks) >= limit:
                break
            rec = json.loads(line)
            names = rec.get("card_names") or []
            if not names:
                continue
            stats.n_decks += 1
            oids = resolve_names(names, exact, frontface, stats)
            if not oids:
                continue
            stats.n_decks_resolved += 1
            decks.append(
                Deck(
                    deck_id=f"archidekt:{rec['deck_id']}",
                    commander=rec.get("commander") or "",
                    oracle_ids=tuple(oids),
                    created_at=rec.get("created_at"),
                )
            )
    return decks, stats


def _decks_from_frame(df: pd.DataFrame, prefix: str, commander_col: str) -> list[Deck]:
    """Build Deck objects from a card-deck-pair frame.

    Filters `temporally_valid` when present, matching MagicSpike's own `src/analysis/ppmi.py`
    (`mask = decks_df["temporally_valid"] & decks_df["oracle_id"].notna()`) — a card whose print
    date postdates the deck's release is not real co-occurrence signal. Small in practice (47 of
    14,828 precon rows, ~0.3%; the cEDH pool is 100% valid) but real, and cheap to get right.
    """
    if "temporally_valid" in df.columns:
        df = df[df["temporally_valid"].fillna(False)]
    decks: list[Deck] = []
    for deck_id, grp in df.groupby("deck_id", sort=True):
        oids = tuple(str(o) for o in grp["oracle_id"].dropna().unique())
        if not oids:
            continue
        commanders = grp[commander_col].dropna().unique() if commander_col in grp else []
        decks.append(
            Deck(
                deck_id=f"{prefix}:{deck_id}",
                commander=str(commanders[0]) if len(commanders) else "",
                oracle_ids=oids,
            )
        )
    return decks


def load_cedh(path: Path | None = None, limit: int | None = None) -> list[Deck]:
    df = pd.read_parquet(
        path or config.DECKS_COMBINED,
        columns=["deck_id", "oracle_id", "commander_name", "source", "temporally_valid"],
    )
    df = df[df["source"] == "edhtop16"]
    if limit is not None:
        keep = df["deck_id"].drop_duplicates().head(limit)
        df = df[df["deck_id"].isin(set(keep))]
    return _decks_from_frame(df, "edhtop16", "commander_name")


def load_precon(root: Path | None = None, limit: int | None = None) -> list[Deck]:
    path = (root or config.PRECON_BACKUP) / "decks_precon.parquet"
    df = pd.read_parquet(
        path, columns=["deck_id", "oracle_id", "commander_name", "temporally_valid"]
    )
    if limit is not None:
        keep = df["deck_id"].drop_duplicates().head(limit)
        df = df[df["deck_id"].isin(set(keep))]
    return _decks_from_frame(df, "precon", "commander_name")


def load_corpus(name: str, limit: int | None = None) -> list[Deck]:
    if name == "casual":
        return load_casual(limit=limit)[0]
    if name == "cedh":
        return load_cedh(limit=limit)
    if name == "precon":
        return load_precon(limit=limit)
    raise ValueError(f"unknown corpus {name!r}; expected one of {config.CORPORA}")


# ── Deck plausibility ─────────────────────────────────────────────────────────

# A legal Commander deck is exactly 100 cards, so the number of DISTINCT oracle_ids in one can
# never exceed 100 — and is usually 80-99, because duplicate basic lands collapse to a single
# oracle_id. That makes this a definitional bound, not a tuned parameter.
MAX_COMMANDER_DISTINCT = 100


def filter_plausible_decks(
    decks: list[Deck], max_distinct: int = MAX_COMMANDER_DISTINCT, min_distinct: int = 0
) -> tuple[list[Deck], dict]:
    """Drop entries that cannot be Commander decks. Returns (kept, stats).

    The Archidekt casual census carries lists of up to 1,947 distinct cards — verified against the
    raw source as genuinely long lists, not a name-resolution artifact, i.e. maybeboards, sideboards
    and whole collections. Because co-occurrence pairs grow quadratically with deck size, the 21%
    of casual entries above 100 cards contribute 56.5% of all PPMI pairs: the majority of that
    corpus's training signal comes from things that are not decks. See
    `findings/t4_corpus_deck_sizes.md`.

    Not applied inside the loaders. T0/T1/T2's recorded numbers were produced on the unfiltered
    basis, and silently changing what `load_corpus` returns would make those findings
    irreproducible from their own inputs. Callers opt in, and the drop counts below are meant to be
    reported rather than swallowed.

    `min_distinct` defaults to 0: the sub-40 stub tail is real (79 entries under 10 cards, some
    commander-only shells) but contributes ~0.6% of PPMI pairs, so excluding it buys little and
    costs an arbitrary parameter. Available for callers who want it.
    """
    kept, dropped_big, dropped_small = [], [], []
    for d in decks:
        n = len(set(d.oracle_ids))
        if n > max_distinct:
            dropped_big.append(n)
        elif n < min_distinct:
            dropped_small.append(n)
        else:
            kept.append(d)

    def _pairs(sizes) -> int:
        return sum(n * (n - 1) // 2 for n in sizes)

    all_sizes = [len(set(d.oracle_ids)) for d in decks]
    total_pairs = _pairs(all_sizes)
    return kept, {
        "max_distinct": max_distinct,
        "min_distinct": min_distinct,
        "n_input": len(decks),
        "n_kept": len(kept),
        "n_dropped_too_large": len(dropped_big),
        "n_dropped_too_small": len(dropped_small),
        "kept_share": len(kept) / len(decks) if decks else 0.0,
        # The headline: oversized entries dominate co-occurrence far beyond their headcount.
        "dropped_ppmi_pair_share": (
            (_pairs(dropped_big) + _pairs(dropped_small)) / total_pairs if total_pairs else 0.0),
        "largest_dropped": max(dropped_big) if dropped_big else None,
    }


# ── Splits ────────────────────────────────────────────────────────────────────

def split_by_deck(
    decks: list[Deck], test_frac: float = 0.15, seed: int = config.SEED
) -> tuple[list[Deck], list[Deck]]:
    """Partition whole decks. Never split card-deck pairs — that leaks."""
    ids = sorted({d.deck_id for d in decks})
    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(ids))
    n_test = max(1, int(round(len(ids) * test_frac)))
    test_ids = {ids[i] for i in perm[:n_test]}
    train = [d for d in decks if d.deck_id not in test_ids]
    test = [d for d in decks if d.deck_id in test_ids]
    return train, test


def temporal_split(
    decks: list[Deck], test_frac: float = 0.15
) -> tuple[list[Deck], list[Deck], str | None]:
    """Split at a date so 'new card' behaviour is testable honestly.

    Only the casual corpus carries timestamps. Decks with no `created_at` are dropped rather than
    dumped into train — silently backdating them would defeat the point of a temporal split.
    Returns (train, test, cutoff).
    """
    dated = [d for d in decks if d.created_at]
    if not dated:
        return [], [], None
    dated.sort(key=lambda d: d.created_at or "")
    n_test = max(1, int(round(len(dated) * test_frac)))
    cutoff = dated[-n_test].created_at
    train = [d for d in dated if (d.created_at or "") < (cutoff or "")]
    test = [d for d in dated if (d.created_at or "") >= (cutoff or "")]
    return train, test, cutoff


def assert_no_deck_leak(train: list[Deck], test: list[Deck]) -> None:
    overlap = {d.deck_id for d in train} & {d.deck_id for d in test}
    if overlap:
        raise AssertionError(
            f"{len(overlap)} deck_id(s) appear in both train and test, e.g. {sorted(overlap)[:5]}"
        )


# ── Appearance counts ─────────────────────────────────────────────────────────

def appearance_counts(decks: list[Deck]) -> pd.Series:
    """{oracle_id: number of decks it appears in}, descending.

    Counts each card once per deck. Commander decks are singleton, so appearances and copies
    coincide, but the census carries only unique names anyway.
    """
    counts: dict[str, int] = {}
    for d in decks:
        for oid in set(d.oracle_ids):
            counts[oid] = counts.get(oid, 0) + 1
    return pd.Series(counts, dtype="int64").sort_values(ascending=False)


def identify_staples(decks: list[Deck], threshold: float = 0.5) -> set[str]:
    """Cards in more than `threshold` of decks. Matches MagicSpike's Rung 0.5 definition."""
    counts = appearance_counts(decks)
    return set(counts[counts > threshold * len(decks)].index)
