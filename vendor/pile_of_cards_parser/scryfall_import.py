"""
Converts the cached Scryfall oracle-cards bulk file into the corpus schema
parser.py reads (see cards.json / build_header in parser.py).

Measurement-only: writes scryfall_corpus_full.json (gitignored), a separate
analysis pool from the hand-curated mtg_card.txt/cards.json corpus.
"""

import gzip
import json
import os
from typing import Optional

PARSER_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(PARSER_DIR, "scryfall_cache", "oracle-cards-20260724210430.jsonl.gz")
OUT_PATH = os.path.join(PARSER_DIR, "scryfall_corpus_full.json")

# 205.4a: the only five supertypes.
SUPERTYPES = {"Basic", "Legendary", "Ongoing", "Snow", "World"}

# Real, single-faced, non-digital, tournament-legal-ish layouts/set_types only.
# card_faces presence (checked separately) already excludes transform/split/
# modal_dfc/adventure/flip/meld — this list catches non-playable objects that
# don't use card_faces at all.
EXCLUDED_LAYOUTS = {"token", "emblem", "planar", "scheme", "vanguard", "art_series", "double_faced_token"}
EXCLUDED_SET_TYPES = {"token", "memorabilia", "funny"}


def type_line_to_fields(type_line: str) -> dict:
    left, _, right = type_line.partition("—")
    words = left.split()
    supertypes = [w for w in words if w in SUPERTYPES]
    types = [w for w in words if w not in SUPERTYPES]
    subtypes = right.split()
    fields = {}
    if supertypes:
        fields["supertypes"] = "[" + ", ".join(supertypes) + "]"
    if types:
        fields["types"] = "[" + ", ".join(types) + "]"
    if subtypes:
        fields["subtypes"] = "[" + ", ".join(subtypes) + "]"
    return fields


def convert_card(card: dict) -> dict:
    out = {"name": card["name"]}

    mana_cost = card.get("mana_cost") or ""
    if mana_cost:
        out["cost"] = mana_cost

    out.update(type_line_to_fields(card.get("type_line", "")))

    oracle_text = card.get("oracle_text") or ""
    if oracle_text:
        out["oracle_text"] = oracle_text

    for field in ("power", "toughness", "loyalty", "defense"):
        val = card.get(field)
        if val is not None:
            out[field] = val

    return out


def should_include(card: dict) -> Optional[str]:
    """Return None if the card should be included, else a short exclusion reason."""
    if "card_faces" in card:
        return "multi_faced"
    if card.get("layout") in EXCLUDED_LAYOUTS:
        return "layout"
    if card.get("set_type") in EXCLUDED_SET_TYPES:
        return "set_type"
    if card.get("digital"):
        return "digital"
    if not card.get("type_line"):
        return "no_type_line"
    return None


def main():
    from collections import Counter
    excluded = Counter()
    included = []

    with gzip.open(CACHE_PATH, "rt", encoding="utf-8") as f:
        for line in f:
            card = json.loads(line)
            reason = should_include(card)
            if reason:
                excluded[reason] += 1
                continue
            included.append(convert_card(card))

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(included, f, indent=2, ensure_ascii=False)

    print(f"Converted {len(included)} cards -> {OUT_PATH}")
    print("Excluded:")
    for reason, count in excluded.most_common():
        print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()
