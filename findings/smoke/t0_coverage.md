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
| casual | 200 | 29.2% | **44.8%** | 37.1% | 7.7% | **FAIL** |

## casual

### Stratified appearance coverage

| Stratum | Coverage | Covered / total appearances |
|---|---|---|
| all | 44.8% | 8,523 / 19,032 |
| non_land | 38.5% | 5,718 / 14,857 |
| non_staple | 43.0% | 7,912 / 18,421 |
| non_land_non_staple | 37.1% | 5,381 / 14,520 |

### Head breakout

| Slice | Appearance cov. | Distinct cov. | Cards |
|---|---|---|---|
| top_100 | 89.8% | 88.4% | 95 |
| top_500 | 71.5% | 60.8% | 492 |
| top_2000 | 55.6% | 39.0% | 1,986 |

### Where the uncovered appearances go

| Status | Share of appearances | Cards in corpus |
|---|---|---|
| clean | 44.8% | 2,273 |
| gap | 51.8% | 5,109 |
| crash | 0.0% | 0 |
| excluded | 3.4% | 392 |

Encoded-at-all coverage (clean **or** gapped) is 96.6% against 44.8% fully clean. The difference is the share of deck content the parser can partially express — a smaller ask to finish than a card it refuses outright.

**Excluded before parsing** (appearance-weighted): `multi_faced` 415, `digital` 201, `set_type` 30

> `multi_faced` is the one that matters: MDFCs, split cards and adventures are real Commander cards the parser declines to attempt. They are counted in the denominator here rather than dropped, so this coverage figure is not inflated by excluding the hard cases.

**Top gap categories** (appearance-weighted): `effect not matched` 4,948, `static pattern unmatched` 1,827, `event not matched` 1,453, `ETB replacement` 523, `mana side effect` 201, `UNKNOWN_KEYWORD` 168, `replacement` 160, `replacement (instead)` 131, `create token pattern unmatched` 118, `ADDL_COST` 95, `damage pattern unmatched` 82, `ALT_COST` 40

Staples excluded by the guard (5): Arcane Signet, Command Tower, Exotic Orchard, Sol Ring

## Notes

- Highest-appearance parse failures are dumped to `t0_failures_<corpus>.csv`. That list, not the percentage, is the input to any future coverage work.
- Distinct coverage is reported over cards **present in the corpus**, not over the whole card pool — the latter counts cards no deck plays, which is not what the 32% reference figure is about.
