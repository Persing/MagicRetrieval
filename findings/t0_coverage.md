# T0 — Frequency-weighted CDL coverage

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.


**Question:** is 32% of distinct cards actually 32% of the deck content that matters?

**Gates (frozen in THRESHOLDS.md):** PASS ≥ 85.0% appearance-weighted · FAIL < 70.0% · guard: the non-land non-staple slice may not trail the aggregate by more than 10.0%.

## Verdicts

| Corpus | Decks | Distinct cov. | Appearance cov. | Guarded slice | Drop | Verdict |
|---|---|---|---|---|---|---|
| casual | 3,697 | 31.5% | **45.9%** | 39.4% | 6.5% | **FAIL** |
| cedh | 46,745 | 31.1% | **48.6%** | 42.5% | 6.1% | **FAIL** |
| precon | 224 | 33.7% | **48.1%** | 39.1% | 9.0% | **FAIL** |

## Basis change — deck-size filter

Decks with more than **100 distinct cards** are excluded. A legal Commander deck is exactly 100 cards, and duplicate basic lands collapse to one oracle_id, so a count above 100 is definitionally impossible — those entries are Archidekt lists carrying maybeboards, sideboards or whole collections, verified against the raw census as genuinely long lists rather than a name-resolution artifact. See `findings/t4_corpus_deck_sizes.md`.

This matters for T0 specifically because the headline number is **appearance-weighted**: a 1,947-card entry contributes 1,947 appearances, so the distortion lands directly on the metric being gated.

| Corpus | Decks (before → after) | Appearance cov. | Δ | Verdict |
|---|---|---|---|---|
| casual | 4,982 → 3,697 | 44.2% → **45.9%** | 1.6% | FAIL → **FAIL** |
| cedh | 46,798 → 46,745 | 48.6% → **48.6%** | 0.0% | FAIL → **FAIL** |
| precon | 224 → 224 | 48.1% → **48.1%** | 0.0% | FAIL → **FAIL** |

The filtered figures above are the honest ones. The prior basis is retained in the JSON under `unfiltered_basis` — the point of recomputing is to correct a number, not to erase where it came from.

**Two changes are stacked in this file; do not attribute the whole move to the filter.** The vendored parser was also refreshed (T4 phase 0, which added the clause modules and fixed parenthesis handling in the canonicalizer), and that alone raises clean coverage. Both sides of the table above use the *same* parser, so the Δ column isolates the filter — but a reader comparing against previously published T0 numbers is seeing parser and basis together. On casual the split is roughly +1.0pp from the parser and +1.6pp from the filter. The parser build is pinned in the envelope.

The filter is a near-no-op outside casual: cedh keeps 99.9% of decks and precon 100%, because those corpora are already legal Commander lists. This is an Archidekt census problem specifically — the same corpus the standing confound above flags as the most EDHREC-derived.

**T2 is deliberately not re-run on this basis.** Its question was whether leakage is fixable, every arm shared the contamination equally, and T4 trains fresh models regardless; T4's decision rules are all within-T4 comparisons, so internal validity is what matters and the basis change does not threaten it. The change is documented rather than propagated.

## casual

### Stratified appearance coverage

| Stratum | Coverage | Covered / total appearances |
|---|---|---|
| all | 45.9% | 127,575 / 278,088 |
| non_land | 40.8% | 89,202 / 218,769 |
| non_staple | 44.4% | 120,162 / 270,675 |
| non_land_non_staple | 39.4% | 84,244 / 213,811 |

### Head breakout

| Slice | Appearance cov. | Distinct cov. | Cards |
|---|---|---|---|
| top_100 | 87.5% | 83.0% | 94 |
| top_500 | 71.9% | 61.7% | 493 |
| top_2000 | 57.7% | 43.3% | 1,983 |

### Where the uncovered appearances go

| Status | Share of appearances | Cards in corpus |
|---|---|---|
| clean | 45.9% | 6,496 |
| gap | 50.2% | 12,917 |
| crash | 0.0% | 0 |
| excluded | 4.0% | 1,179 |

Encoded-at-all coverage (clean **or** gapped) is 96.0% against 45.9% fully clean. The difference is the share of deck content the parser can partially express — a smaller ask to finish than a card it refuses outright.

**Excluded before parsing** (appearance-weighted): `multi_faced` 7,488, `digital` 3,178, `set_type` 328

> `multi_faced` is the one that matters: MDFCs, split cards and adventures are real Commander cards the parser declines to attempt. They are counted in the denominator here rather than dropped, so this coverage figure is not inflated by excluding the hard cases.

**Top gap categories** (appearance-weighted): `effect not matched` 67,817, `static pattern unmatched` 26,045, `event not matched` 20,007, `ETB replacement` 7,348, `replacement (instead)` 2,840, `UNKNOWN_KEYWORD` 2,797, `replacement` 2,472, `mana side effect` 2,167, `ADDL_COST` 1,898, `create token pattern unmatched` 1,558, `damage pattern unmatched` 1,531, `ALT_COST` 762

Staples excluded by the guard (3): Arcane Signet, Command Tower, Sol Ring

## cedh

### Stratified appearance coverage

| Stratum | Coverage | Covered / total appearances |
|---|---|---|
| all | 48.6% | 2,201,537 / 4,534,390 |
| non_land | 46.2% | 1,531,885 / 3,318,621 |
| non_staple | 40.6% | 1,292,884 / 3,186,992 |
| non_land_non_staple | 42.5% | 1,079,861 / 2,542,928 |

### Head breakout

| Slice | Appearance cov. | Distinct cov. | Cards |
|---|---|---|---|
| top_100 | 57.9% | 54.0% | 100 |
| top_500 | 50.8% | 41.9% | 492 |
| top_2000 | 48.8% | 33.8% | 1,990 |

### Where the uncovered appearances go

| Status | Share of appearances | Cards in corpus |
|---|---|---|
| clean | 48.6% | 3,264 |
| gap | 40.8% | 6,650 |
| crash | 0.0% | 0 |
| excluded | 10.6% | 595 |

Encoded-at-all coverage (clean **or** gapped) is 89.4% against 48.6% fully clean. The difference is the share of deck content the parser can partially express — a smaller ask to finish than a card it refuses outright.

**Excluded before parsing** (appearance-weighted): `digital` 407,876, `multi_faced` 68,211, `set_type` 5,490

> `multi_faced` is the one that matters: MDFCs, split cards and adventures are real Commander cards the parser declines to attempt. They are counted in the denominator here rather than dropped, so this coverage figure is not inflated by excluding the hard cases.

**Top gap categories** (appearance-weighted): `effect not matched` 909,752, `static pattern unmatched` 393,945, `event not matched` 151,410, `ETB replacement` 113,025, `ALT_COST` 103,088, `replacement` 50,753, `damage pattern unmatched` 46,046, `ADDL_COST` 27,696, `replacement (instead)` 21,370, `UNKNOWN_KEYWORD` 14,792, `mana side effect` 8,797, `create token pattern unmatched` 5,409

Staples excluded by the guard (39): Ancient Tomb, Arcane Signet, Arid Mesa, Bloodstained Mire, Chrome Mox, City of Brass, Command Tower, Deflecting Swat, Demonic Tutor, Fellwar Stone, Fierce Guardianship, Flooded Strand, Flusterstorm, Force of Negation, Force of Will, Gemstone Caverns, Lotus Petal, Mana Confluence, Mana Vault, Marsh Flats, Mental Misstep, Mindbreak Trap, Misty Rainforest, Mox Amber, Mox Diamond, Mox Opal, Mystic Remora, Otawara, Soaring City, Pact of Negation, Polluted Delta, Rhystic Study, Scalding Tarn, Sol Ring, Swan Song, The One Ring, Vampiric Tutor, Verdant Catacombs, Windswept Heath, Wooded Foothills

## precon

### Stratified appearance coverage

| Stratum | Coverage | Covered / total appearances |
|---|---|---|
| all | 48.1% | 7,115 / 14,781 |
| non_land | 40.6% | 4,680 / 11,532 |
| non_staple | 46.6% | 6,680 / 14,346 |
| non_land_non_staple | 39.1% | 4,400 / 11,252 |

### Head breakout

| Slice | Appearance cov. | Distinct cov. | Cards |
|---|---|---|---|
| top_100 | 82.0% | 76.0% | 100 |
| top_500 | 70.8% | 61.0% | 500 |
| top_2000 | 57.3% | 42.6% | 2,000 |

### Where the uncovered appearances go

| Status | Share of appearances | Cards in corpus |
|---|---|---|
| clean | 48.1% | 2,124 |
| gap | 50.9% | 4,071 |
| crash | 0.0% | 0 |
| excluded | 1.0% | 111 |

Encoded-at-all coverage (clean **or** gapped) is 99.0% against 48.1% fully clean. The difference is the share of deck content the parser can partially express — a smaller ask to finish than a card it refuses outright.

**Excluded before parsing** (appearance-weighted): `multi_faced` 88, `digital` 58, `set_type` 1

> `multi_faced` is the one that matters: MDFCs, split cards and adventures are real Commander cards the parser declines to attempt. They are counted in the denominator here rather than dropped, so this coverage figure is not inflated by excluding the hard cases.

**Top gap categories** (appearance-weighted): `effect not matched` 4,089, `static pattern unmatched` 1,286, `event not matched` 972, `ETB replacement` 277, `replacement` 168, `UNKNOWN_KEYWORD` 159, `mana side effect` 155, `create token pattern unmatched` 96, `replacement (instead)` 75, `damage pattern unmatched` 67, `ADDL_COST` 59, `ALT_COST` 34

Staples excluded by the guard (3): Arcane Signet, Command Tower, Sol Ring

## Notes

- Highest-appearance parse failures are dumped to `t0_failures_<corpus>.csv`. That list, not the percentage, is the input to any future coverage work.
- Distinct coverage is reported over cards **present in the corpus**, not over the whole card pool — the latter counts cards no deck plays, which is not what the 32% reference figure is about.
