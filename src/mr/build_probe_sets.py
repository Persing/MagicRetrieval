"""Build the curated pair files, then freeze them.

**Two lists, not one.** The validation plan seeds T1's functional-equivalence probes and T2's
substitute eval set from the same curated list, but they are different relations:

  * **Functional equivalence** (T1) — the cards *do the same thing*. A cycle like Dimir Signet /
    Izzet Signet qualifies: identical template, different colour words. Exactly the case where a
    parser might diverge on wording alone, so they are good T1 probes.

  * **Strategic substitution** (T2) — a deckbuilder would consider them *for the same slot*. Signet
    cycle members do NOT qualify: they are colour-locked, a deck plays the one matching its colours,
    and they never compete. Worse, they share a text template, so scoring them as "substitutes"
    would let T2 pass on text-surface similarity — the exact thing T2 exists to detect.

Merging the two would let colour-cycle pairs inflate T2. They are kept apart.

    uv run python -m mr.build_probe_sets
"""

from __future__ import annotations

import csv
from pathlib import Path

from . import config, corpus, probe_sets

# ── T2: strategic substitutes — same deck slot, a builder picks one ───────────
# Seeded from MagicSpike data/substitute_pairs.csv (15 pairs) and expanded. Every pair is two cards
# that compete for one slot, not merely two cards that read alike.
SUBSTITUTES: list[tuple[str, str, str]] = [
    ("Cultivate", "Kodama's Reach", "basic_ramp"),
    ("Rampant Growth", "Three Visits", "1cmc_ramp"),
    ("Nature's Lore", "Three Visits", "2cmc_ramp"),
    ("Farseek", "Nature's Lore", "2cmc_ramp"),
    ("Skyshroud Claim", "Explosive Vegetation", "4cmc_ramp"),
    ("Harrow", "Explosive Vegetation", "land_ramp"),
    ("Birds of Paradise", "Llanowar Elves", "1cmc_dork"),
    ("Elvish Mystic", "Llanowar Elves", "1cmc_dork"),
    ("Fyndhorn Elves", "Llanowar Elves", "1cmc_dork"),
    ("Sakura-Tribe Elder", "Wood Elves", "etb_ramp"),
    ("Solemn Simulacrum", "Wood Elves", "etb_ramp"),
    ("Sol Ring", "Arcane Signet", "early_mana"),
    ("Arcane Signet", "Fellwar Stone", "2cmc_rock"),
    ("Mind Stone", "Fellwar Stone", "2cmc_rock"),
    ("Thought Vessel", "Mind Stone", "2cmc_rock"),
    ("Commander's Sphere", "Chromatic Lantern", "3cmc_rock"),
    ("Coldsteel Heart", "Fellwar Stone", "2cmc_rock"),
    ("Path to Exile", "Swords to Plowshares", "white_removal"),
    ("Swords to Plowshares", "Condemn", "white_removal"),
    ("Generous Gift", "Beast Within", "permanent_removal"),
    ("Return to Dust", "Crush Contraband", "artifact_ench_removal"),
    ("Disenchant", "Naturalize", "artifact_ench_removal"),
    ("Krosan Grip", "Nature's Claim", "green_artifact_removal"),
    ("Go for the Throat", "Doom Blade", "black_removal"),
    ("Doom Blade", "Cast Down", "black_removal"),
    ("Infernal Grasp", "Go for the Throat", "black_removal"),
    ("Murder", "Hero's Downfall", "3cmc_black_removal"),
    ("Bake into a Pie", "Murder", "3cmc_black_removal"),
    ("Wrath of God", "Damnation", "4cmc_wipe"),
    ("Day of Judgment", "Wrath of God", "4cmc_wipe"),
    ("Toxic Deluge", "Damnation", "black_wipe"),
    ("Blasphemous Act", "Chain Reaction", "red_wipe"),
    ("Cyclonic Rift", "Evacuation", "bounce_wipe"),
    ("Counterspell", "Arcane Denial", "counterspell"),
    ("An Offer You Can't Refuse", "Swan Song", "cheap_counter"),
    ("Dovin's Veto", "Negate", "noncreature_counter"),
    ("Rhystic Study", "Mystic Remora", "draw_tax"),
    ("Phyrexian Arena", "Underworld Connections", "repeat_draw"),
    ("Night's Whisper", "Sign in Blood", "2cmc_draw"),
    ("Divination", "Night's Whisper", "2cmc_draw"),
    ("Fact or Fiction", "Mulldrifter", "draw_spell"),
    ("Harmonize", "Concentrate", "draw_three"),
    ("Demonic Tutor", "Vampiric Tutor", "tutor"),
    ("Diabolic Tutor", "Demonic Tutor", "tutor"),
    ("Diabolic Intent", "Demonic Tutor", "tutor"),
    ("Enlightened Tutor", "Mystical Tutor", "narrow_tutor"),
    ("Idyllic Tutor", "Enlightened Tutor", "enchantment_tutor"),
    ("Worldly Tutor", "Sylvan Tutor", "creature_tutor"),
    ("Eternal Witness", "Regrowth", "recursion"),
    ("Regrowth", "Noxious Revival", "recursion"),
    ("Time Warp", "Temporal Manipulation", "extra_turn"),
    ("Lightning Greaves", "Swiftfoot Boots", "protect_equip"),
    ("Deflecting Swat", "Fierce Guardianship", "free_protection"),
    ("Command Tower", "Exotic Orchard", "fixing_land"),
    ("Exotic Orchard", "Reflecting Pool", "fixing_land"),
    ("Bojuka Bog", "Scavenger Grounds", "graveyard_hate_land"),
    ("Reliquary Tower", "Thought Vessel", "no_max_hand"),
]

# ── T1 tier: parametric cycles — same template, different slot values ────────
# Judged with numbers and mana symbols masked. Unmasked, a signet pair "diverges" purely because
# one adds {U}{B} and the other {U}{R} — that is the parser filling a slot correctly, not taking a
# different branch. Masked, any remaining difference is structural and real.
#
# These belong in T1 only. As T2 eval pairs they would be actively harmful: colour-cycle members
# are colour-locked, never compete for a deck slot, and share a text template — so scoring them as
# "substitutes" would let T2 pass on exactly the text-surface similarity it exists to detect.
PARAMETRIC_CYCLES: list[tuple[str, str, str]] = [
    ("Dimir Signet", "Izzet Signet", "signet_cycle"),
    ("Azorius Signet", "Golgari Signet", "signet_cycle"),
    ("Boros Signet", "Selesnya Signet", "signet_cycle"),
    ("Talisman of Dominance", "Talisman of Progress", "talisman_cycle"),
    ("Talisman of Indulgence", "Talisman of Unity", "talisman_cycle"),
    ("Dimir Guildgate", "Izzet Guildgate", "guildgate_cycle"),
    ("Selesnya Guildgate", "Rakdos Guildgate", "guildgate_cycle"),
    ("Charcoal Diamond", "Sky Diamond", "diamond_cycle"),
    ("Fire Diamond", "Marble Diamond", "diamond_cycle"),
    ("Blossoming Sands", "Dismal Backwater", "gainland_cycle"),
    ("Jungle Hollow", "Rugged Highlands", "gainland_cycle"),
    ("Bant Panorama", "Esper Panorama", "panorama_cycle"),
    ("Dimir Cluestone", "Izzet Cluestone", "cluestone_cycle"),
    ("Boros Locket", "Golgari Locket", "locket_cycle"),
]

# ── T1 tier: same effect, different wording — the plan's actual question ─────
# This is the tier T1's threshold is about: does a different *wording* of the same effect take a
# different branch? It is deliberately small, and that smallness is itself a finding — WotC
# templates functionally identical cards identically, so most true equivalents have byte-identical
# oracle text and get mined into the `identical_text` tier automatically. Genuine wording variation
# lives almost entirely in reprints across eras, which is probe set 1 and needs the all-printings
# bulk. Pairs whose text turns out to be identical are dropped here to avoid double-counting.
SAME_EFFECT_DIFF_WORDING: list[tuple[str, str, str]] = [
    ("Cultivate", "Kodama's Reach", "reveal-clause wording differs"),
    ("Wrath of God", "Damnation", "wipe, colour-shifted"),
    ("Time Warp", "Temporal Manipulation", "extra turn"),
    ("Disenchant", "Naturalize", "artifact/enchantment removal, colour-shifted"),
    ("Nature's Lore", "Three Visits", "forest-fetch"),
    ("Llanowar Elves", "Fyndhorn Elves", "mana dork"),
    ("Elvish Mystic", "Llanowar Elves", "mana dork"),
]


def _verify_and_write(rows: list[tuple[str, str, str]], path: Path, known: set[str]) -> dict:
    """Write only pairs whose both names exist in the card pool. Report what was dropped."""
    kept, dropped, missing = [], [], set()
    for a, b, role in rows:
        if a in known and b in known:
            kept.append((a, b, role))
        else:
            dropped.append((a, b, role))
            missing.update(n for n in (a, b) if n not in known)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["card_a", "card_b", "role"])
        w.writerows(kept)

    return {"path": str(path), "n_written": len(kept), "n_dropped": len(dropped),
            "missing_names": sorted(missing)}


def main() -> dict:
    cards = corpus.load_cards_parquet()
    known = set(cards["name"])

    probes = config.DATA_DIR / "probe_sets"

    # Mine the identical-text tier first, so curated pairs that turn out to be byte-identical are
    # dropped from the wording tier rather than counted twice under a weaker claim.
    identical = probe_sets.identical_text_pairs(cards)
    identical_keys = {frozenset((p.name_a, p.name_b)) for p in identical}
    wording = [t for t in SAME_EFFECT_DIFF_WORDING if frozenset((t[0], t[1])) not in identical_keys]
    demoted = [t for t in SAME_EFFECT_DIFF_WORDING if frozenset((t[0], t[1])) in identical_keys]

    with (probes / "identical_text.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["card_a", "card_b", "role"])
        w.writerows((p.name_a, p.name_b, "identical_text") for p in identical)

    result = {
        "substitutes": _verify_and_write(
            SUBSTITUTES, config.DATA_DIR / "substitute_pairs_v2.csv", known),
        "same_effect_diff_wording": _verify_and_write(
            wording, probes / "same_effect_diff_wording.csv", known),
        "parametric_cycles": _verify_and_write(
            PARAMETRIC_CYCLES, probes / "parametric_cycles.csv", known),
        "identical_text": {"path": str(probes / "identical_text.csv"), "n_written": len(identical)},
        "demoted_to_identical_text": [f"{a} / {b}" for a, b, _ in demoted],
    }

    # Tag-mined review queue. Sharing a tag is not equivalence, so this is never a probe set
    # directly — it is a queue whose `keep` column is filled in by hand.
    candidates = probe_sets.tag_candidates(cards)
    cand_path = config.DATA_DIR / "probe_sets" / "tag_candidates.csv"
    probe_sets.write_candidates_csv(candidates, cand_path)
    result["tag_candidates"] = {"path": str(cand_path), "n": len(candidates)}

    numeric = probe_sets.numeric_pairs(cards)
    result["numeric_mined"] = {"n": len(numeric)}
    return result


if __name__ == "__main__":
    import json
    print(json.dumps(main(), indent=2))
