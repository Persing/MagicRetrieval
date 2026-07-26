"""T1 probe sets, and the frozen substitute eval set T2 scores against.

Three T1 probe sets, per the validation plan:

  1. reprints        — same card, different era wording. Needs Scryfall's ALL-printings bulk for
                       `printed_text`; the pinned oracle snapshot carries one unified text per
                       oracle_id and cannot express this. Skipped unless that bulk is supplied.
  2. numeric         — same effect at different numeric values. Mined free and deterministically
                       by templating integers out of oracle text. Isolates whether numbers are
                       properly parameterized out.
  3. functional      — curated functional-equivalence pairs. Seeded from MagicSpike's 15-pair list,
                       expanded via the Scryfall Tagger snapshot, hand-reviewed.

The tagger is used only to *propose* candidates. Sharing a tag is not functional equivalence —
`typal-*` tags in particular group cards that share a creature type and nothing else — so the
mined output is a review queue, never a probe set.
"""

from __future__ import annotations

import ast
import csv
import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import config

NUMERIC_SLOT = re.compile(r"\d+")
_WS = re.compile(r"\s+")

# Tag prefixes that group by identity rather than function. A shared `typal-elf` says both cards
# mention elves, which is exactly the text-surface similarity this branch is trying to see past.
EXCLUDED_TAG_PREFIXES = (
    "typal-", "type-errata-", "art-", "artist-", "cycle-", "flavor-", "french-vanilla",
    "vanilla", "reprint", "banned", "restricted", "format-", "set-",
)
MAX_TAG_SIZE = 40   # bigger tags are categories, not effect signatures
MIN_TAG_SIZE = 2


@dataclass(frozen=True)
class Pair:
    oracle_id_a: str
    oracle_id_b: str
    name_a: str
    name_b: str
    source: str
    note: str = ""

    def key(self) -> frozenset[str]:
        return frozenset((self.oracle_id_a, self.oracle_id_b))


# ── set 2: numeric parameterization ───────────────────────────────────────────

def template_numbers(text: str) -> str:
    return _WS.sub(" ", NUMERIC_SLOT.sub("#", text or "")).strip().lower()


def numeric_pairs(cards: pd.DataFrame, max_per_group: int = 3) -> list[Pair]:
    """Cards whose oracle text is identical once integers are replaced by '#'.

    Requires the raw texts to actually differ, so identical-text cards (functional reprints under
    different names) don't leak in — those belong in the functional set, where a divergence would
    mean something different.
    """
    df = cards[cards["oracle_text"].notna() & (cards["oracle_text"].str.strip() != "")].copy()
    df["_tmpl"] = df["oracle_text"].map(template_numbers)
    df["_has_num"] = df["oracle_text"].str.contains(r"\d", regex=True, na=False)
    df = df[df["_has_num"] & (df["_tmpl"].str.len() > 12)]

    pairs: list[Pair] = []
    for tmpl, grp in df.groupby("_tmpl", sort=True):
        if len(grp) < 2:
            continue
        rows = grp.drop_duplicates("oracle_id").sort_values("name").to_dict("records")
        emitted = 0
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["oracle_text"].strip() == b["oracle_text"].strip():
                    continue  # identical text — not a numeric-variation pair
                pairs.append(Pair(
                    a["oracle_id"], b["oracle_id"], a["name"], b["name"],
                    source="numeric", note=tmpl[:120],
                ))
                emitted += 1
                if emitted >= max_per_group:
                    break
            if emitted >= max_per_group:
                break
    return pairs


def identical_text_pairs(cards: pd.DataFrame, max_pairs: int = 800) -> list[Pair]:
    """Cards whose oracle text is identical once each card's own name is masked out.

    Mined free and at scale. These are the strongest possible equivalence claim — Llanowar Elves
    and Fyndhorn Elves are the same card with different names — so divergence here is a parser
    determinism bug, not a representational choice. It is the control the curated tiers are read
    against: if this tier is not ~0%, nothing else in T1 is interpretable.
    """
    df = cards[cards["oracle_text"].notna() & (cards["oracle_text"].str.strip() != "")].copy()
    df = df.drop_duplicates("oracle_id")

    def mask(row) -> str:
        txt = _WS.sub(" ", str(row["oracle_text"])).strip().lower()
        return txt.replace(str(row["name"]).lower(), "~")

    df["_masked"] = df.apply(mask, axis=1)
    df = df[df["_masked"].str.len() > 20]

    pairs: list[Pair] = []
    for txt, grp in df.groupby("_masked", sort=True):
        if len(grp) < 2 or len(pairs) >= max_pairs:
            continue
        rows = grp.sort_values("name").to_dict("records")
        for i in range(len(rows) - 1):
            a, b = rows[i], rows[i + 1]
            pairs.append(Pair(a["oracle_id"], b["oracle_id"], a["name"], b["name"],
                              source="identical_text", note=txt[:100]))
            if len(pairs) >= max_pairs:
                break
    return pairs


# ── set 3: functional equivalence ─────────────────────────────────────────────

def load_tag_index(path: Path | None = None) -> dict[str, set[str]]:
    """{tag_slug: {oracle_id}} for effect-shaped tags of usable size."""
    path = path or config.SCRYFALL_TAGS
    index: dict[str, set[str]] = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            rec = json.loads(line)
            slug = rec.get("slug") or ""
            if not slug or slug.startswith(EXCLUDED_TAG_PREFIXES):
                continue
            taggings = rec.get("taggings")
            if isinstance(taggings, str):
                try:
                    taggings = ast.literal_eval(taggings)
                except (ValueError, SyntaxError):
                    continue
            oids = {t.get("oracle_id") for t in (taggings or []) if t.get("oracle_id")}
            if MIN_TAG_SIZE <= len(oids) <= MAX_TAG_SIZE:
                index[slug] = oids
    return index


def tag_candidates(
    cards: pd.DataFrame,
    tag_index: dict[str, set[str]] | None = None,
    max_cmc_delta: float = 1.0,
    limit: int = 400,
) -> list[Pair]:
    """Propose functional-equivalence candidates for hand review.

    Two cards are proposed when they share a small effect tag, sit within `max_cmc_delta` of each
    other, and share a primary type. Ranked by how specific their rarest shared tag is — a pair
    whose only connection is a 40-member tag is a weaker candidate than one sharing a 3-member tag.
    """
    tag_index = tag_index if tag_index is not None else load_tag_index()
    info = cards.drop_duplicates("oracle_id").set_index("oracle_id")

    def primary_type(tl: str) -> str:
        tl = (tl or "").split("—")[0].lower()
        for t in ("creature", "instant", "sorcery", "enchantment", "artifact", "planeswalker", "land"):
            if t in tl:
                return t
        return "other"

    scored: dict[frozenset[str], tuple[int, str]] = {}
    for slug, oids in tag_index.items():
        present = sorted(o for o in oids if o in info.index)
        if len(present) < 2:
            continue
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                a, b = present[i], present[j]
                ra, rb = info.loc[a], info.loc[b]
                if primary_type(ra["type_line"]) != primary_type(rb["type_line"]):
                    continue
                if abs(float(ra["cmc"] or 0) - float(rb["cmc"] or 0)) > max_cmc_delta:
                    continue
                k = frozenset((a, b))
                if k not in scored or len(present) < scored[k][0]:
                    scored[k] = (len(present), slug)

    ranked = sorted(scored.items(), key=lambda kv: kv[1][0])[:limit]
    out: list[Pair] = []
    for k, (size, slug) in ranked:
        a, b = sorted(k)
        out.append(Pair(a, b, str(info.loc[a]["name"]), str(info.loc[b]["name"]),
                        source="tag_candidate", note=f"{slug} (n={size})"))
    return out


# ── set 1: reprints (needs the all-printings bulk) ────────────────────────────

def reprint_pairs(all_printings_path: Path | None = None) -> tuple[list[Pair], str | None]:
    """Same card, different era wording. Returns (pairs, skip_reason).

    Scryfall's oracle bulk carries one unified `oracle_text` per oracle_id, so this set is not
    derivable from the pinned snapshot — it needs the ~2GB all-printings bulk for `printed_text`.
    Skipped by default; the skip is reported in the findings rather than silently omitted.
    """
    if all_printings_path is None or not Path(all_printings_path).exists():
        return [], (
            "No all-printings bulk supplied. Scryfall's oracle snapshot carries one unified "
            "oracle_text per oracle_id, so era-wording pairs cannot be built from it. Pass "
            "--all-printings /path/to/all-cards.json to enable this set."
        )

    by_oracle: dict[str, dict[str, str]] = {}
    with Path(all_printings_path).open() as fh:
        for line in fh:
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError:
                continue
            oid, printed = card.get("oracle_id"), card.get("printed_text")
            if not oid or not printed:
                continue
            by_oracle.setdefault(oid, {})[_WS.sub(" ", printed).strip()] = card.get("name", "")

    pairs: list[Pair] = []
    for oid, texts in by_oracle.items():
        if len(texts) < 2:
            continue
        variants = sorted(texts)
        name = texts[variants[0]]
        # Both endpoints are the same oracle_id; the pair is over wordings, carried in `note`.
        pairs.append(Pair(oid, oid, name, name, source="reprint",
                          note=f"{len(variants)} distinct printed wordings"))
    return pairs, None


# ── curated files ─────────────────────────────────────────────────────────────

def load_pair_csv(path: Path, cards: pd.DataFrame, source: str) -> tuple[list[Pair], list[str]]:
    """Read a curated name-pair CSV. Returns (pairs, unresolved_names)."""
    name_to_oid = dict(zip(cards["name"], cards["oracle_id"]))
    pairs: list[Pair] = []
    unresolved: list[str] = []
    with Path(path).open() as fh:
        for row in csv.DictReader(fh):
            a, b = row["card_a"].strip(), row["card_b"].strip()
            oid_a, oid_b = name_to_oid.get(a), name_to_oid.get(b)
            if not oid_a:
                unresolved.append(a)
            if not oid_b:
                unresolved.append(b)
            if oid_a and oid_b:
                pairs.append(Pair(oid_a, oid_b, a, b, source=source, note=row.get("role", "")))
    return pairs, unresolved


def write_candidates_csv(pairs: list[Pair], path: Path) -> None:
    """Emit a review queue. `keep` is filled in by hand; only kept rows enter a probe set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["keep", "card_a", "card_b", "shared_tag", "oracle_id_a", "oracle_id_b"])
        for p in pairs:
            w.writerow(["", p.name_a, p.name_b, p.note, p.oracle_id_a, p.oracle_id_b])
