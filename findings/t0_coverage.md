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
| casual | 4,982 | 29.9% | **43.2%** | 36.9% | 6.3% | **FAIL** |
| cedh | 46,798 | 29.6% | **48.3%** | 42.1% | 6.2% | **FAIL** |
| precon | 224 | 32.3% | **47.0%** | 37.7% | 9.2% | **FAIL** |

## casual

### Stratified appearance coverage

| Stratum | Coverage | Covered / total appearances |
|---|---|---|
| all | 43.2% | 198,195 / 458,588 |
| non_land | 38.1% | 138,479 / 363,492 |
| non_staple | 41.9% | 187,694 / 448,087 |
| non_land_non_staple | 36.9% | 131,506 / 356,519 |

### Head breakout

| Slice | Appearance cov. | Distinct cov. | Cards |
|---|---|---|---|
| top_100 | 85.3% | 80.9% | 94 |
| top_500 | 69.6% | 58.7% | 492 |
| top_2000 | 55.2% | 40.9% | 1,983 |

### Where the uncovered appearances go

| Status | Share of appearances | Cards in corpus |
|---|---|---|
| clean | 43.2% | 7,042 |
| gap | 52.5% | 15,042 |
| crash | 0.0% | 0 |
| excluded | 4.3% | 1,441 |

Encoded-at-all coverage (clean **or** gapped) is 95.7% against 43.2% fully clean. The difference is the share of deck content the parser can partially express — a smaller ask to finish than a card it refuses outright.

**Excluded before parsing** (appearance-weighted): `multi_faced` 13,059, `digital` 5,848, `set_type` 594

> `multi_faced` is the one that matters: MDFCs, split cards and adventures are real Commander cards the parser declines to attempt. They are counted in the denominator here rather than dropped, so this coverage figure is not inflated by excluding the hard cases.

**Top gap categories** (appearance-weighted): `effect not matched` 119,705, `static pattern unmatched` 44,598, `event not matched` 33,996, `ETB replacement` 12,145, `replacement (instead)` 4,783, `replacement` 4,543, `UNKNOWN_KEYWORD` 4,091, `mana side effect` 3,635, `ADDL_COST` 3,084, `create token pattern unmatched` 2,650, `damage pattern unmatched` 2,500, `ALT_COST` 1,435

Staples excluded by the guard (3): Arcane Signet, Command Tower, Sol Ring

## cedh

### Stratified appearance coverage

| Stratum | Coverage | Covered / total appearances |
|---|---|---|
| all | 48.3% | 2,192,689 / 4,540,009 |
| non_land | 45.9% | 1,523,928 / 3,322,795 |
| non_staple | 40.2% | 1,282,980 / 3,191,054 |
| non_land_non_staple | 42.1% | 1,071,380 / 2,546,208 |

### Head breakout

| Slice | Appearance cov. | Distinct cov. | Cards |
|---|---|---|---|
| top_100 | 57.9% | 54.0% | 100 |
| top_500 | 50.7% | 41.7% | 492 |
| top_2000 | 48.6% | 32.8% | 1,990 |

### Where the uncovered appearances go

| Status | Share of appearances | Cards in corpus |
|---|---|---|
| clean | 48.3% | 3,123 |
| gap | 41.1% | 6,828 |
| crash | 0.0% | 0 |
| excluded | 10.6% | 597 |

Encoded-at-all coverage (clean **or** gapped) is 89.4% against 48.3% fully clean. The difference is the share of deck content the parser can partially express — a smaller ask to finish than a card it refuses outright.

**Excluded before parsing** (appearance-weighted): `digital` 408,404, `multi_faced` 68,281, `set_type` 5,496

> `multi_faced` is the one that matters: MDFCs, split cards and adventures are real Commander cards the parser declines to attempt. They are counted in the denominator here rather than dropped, so this coverage figure is not inflated by excluding the hard cases.

**Top gap categories** (appearance-weighted): `effect not matched` 923,096, `static pattern unmatched` 393,998, `event not matched` 153,250, `ETB replacement` 113,165, `ALT_COST` 103,181, `replacement` 50,885, `damage pattern unmatched` 46,094, `ADDL_COST` 27,722, `replacement (instead)` 21,412, `UNKNOWN_KEYWORD` 11,965, `mana side effect` 9,773, `create token pattern unmatched` 5,412

Staples excluded by the guard (39): Ancient Tomb, Arcane Signet, Arid Mesa, Bloodstained Mire, Chrome Mox, City of Brass, Command Tower, Deflecting Swat, Demonic Tutor, Fellwar Stone, Fierce Guardianship, Flooded Strand, Flusterstorm, Force of Negation, Force of Will, Gemstone Caverns, Lotus Petal, Mana Confluence, Mana Vault, Marsh Flats, Mental Misstep, Mindbreak Trap, Misty Rainforest, Mox Amber, Mox Diamond, Mox Opal, Mystic Remora, Otawara, Soaring City, Pact of Negation, Polluted Delta, Rhystic Study, Scalding Tarn, Sol Ring, Swan Song, The One Ring, Vampiric Tutor, Verdant Catacombs, Windswept Heath, Wooded Foothills

## precon

### Stratified appearance coverage

| Stratum | Coverage | Covered / total appearances |
|---|---|---|
| all | 47.0% | 6,968 / 14,828 |
| non_land | 39.3% | 4,540 / 11,565 |
| non_staple | 45.4% | 6,533 / 14,393 |
| non_land_non_staple | 37.7% | 4,260 / 11,285 |

### Head breakout

| Slice | Appearance cov. | Distinct cov. | Cards |
|---|---|---|---|
| top_100 | 81.5% | 75.0% | 100 |
| top_500 | 70.2% | 59.8% | 500 |
| top_2000 | 56.6% | 41.7% | 2,000 |

### Where the uncovered appearances go

| Status | Share of appearances | Cards in corpus |
|---|---|---|
| clean | 47.0% | 2,044 |
| gap | 52.0% | 4,177 |
| crash | 0.0% | 0 |
| excluded | 1.0% | 111 |

Encoded-at-all coverage (clean **or** gapped) is 99.0% against 47.0% fully clean. The difference is the share of deck content the parser can partially express — a smaller ask to finish than a card it refuses outright.

**Excluded before parsing** (appearance-weighted): `multi_faced` 88, `digital` 58, `set_type` 1

> `multi_faced` is the one that matters: MDFCs, split cards and adventures are real Commander cards the parser declines to attempt. They are counted in the denominator here rather than dropped, so this coverage figure is not inflated by excluding the hard cases.

**Top gap categories** (appearance-weighted): `effect not matched` 4,262, `static pattern unmatched` 1,293, `event not matched` 999, `ETB replacement` 277, `replacement` 170, `mana side effect` 160, `UNKNOWN_KEYWORD` 142, `create token pattern unmatched` 94, `replacement (instead)` 75, `damage pattern unmatched` 67, `ADDL_COST` 60, `ALT_COST` 34

Staples excluded by the guard (3): Arcane Signet, Command Tower, Sol Ring

## Notes

- Highest-appearance parse failures are dumped to `t0_failures_<corpus>.csv`. That list, not the percentage, is the input to any future coverage work.
- Distinct coverage is reported over cards **present in the corpus**, not over the whole card pool — the latter counts cards no deck plays, which is not what the 32% reference figure is about.
