# T1 — CDL divergence rate

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.


**Question:** do semantically-equivalent cards land on the same CDL, or do different wordings take different branches to different terminal states?

**Gates (frozen in THRESHOLDS.md):** PASS < 10.0% · PARTIAL 10.0%–25.0% · FAIL > 25.0%. The numeric probe set is judged separately at < 5.0%.

## Verdict: **PASS (UNDERPOWERED)**

Primary number: clean-only pairs, scope=rules, loose ordering. The threshold applies to **same_effect_wording**; every other tier is a control or a contrast.

| Probe set | Pairs | Both clean | Masking | Divergence (primary) | 95% upper bound | Strict | scope=full | All encoded |
|---|---|---|---|---|---|---|---|---|
| identical_text | 800 | 491 | none | **0.0%** | ≤ 0.6% | 0.0% | 94.7% | 0.0% |
| same_effect_wording ⚠ | 5 | 4 | none | **0.0%** | ≤ 75.0% | 0.0% | 25.0% | 0.0% |
| parametric_cycles ⚠ | 14 | 14 | numbers,symbols | **0.0%** | ≤ 21.4% | 0.0% | 0.0% | 0.0% |
| numeric | 550 | 348 | numbers | **0.0%** | ≤ 0.9% | 0.0% | 84.5% | 0.0% |
| substitutes_contrast | 57 | 39 | none | **69.2%** | — | 69.2% | 87.2% | 67.9% |

⚠ = fewer than 20 clean pairs; the tier cannot carry a verdict on its own. **95% upper bound** is the rule of three (3/n) for tiers that observed zero divergences — a tier reporting 0% over 4 pairs is statistically consistent with a 75% true rate, so the observed 0% is not by itself evidence of anything.

### What each tier is for

| Tier | Role |
|---|---|
| `identical_text` | **Control.** Oracle text identical once each card's own name is masked — the strongest possible equivalence claim. Divergence here is a parser determinism bug. If this is not ~0%, no other number in T1 is interpretable. |
| `same_effect_wording` | **The threshold tier.** Same effect, different wording. |
| `parametric_cycles` | Same template, different slot values (signets, guildgates). Masked, so a remaining difference is structural rather than a correctly-filled slot. |
| `numeric` | Same effect at different numeric values, numbers masked — tests whether numbers are parameterized out rather than branched on. |
| `substitutes_contrast` | Strategic substitutes, *not* equivalents. These should diverge **more** than the equivalence tiers. If they don't, canonicalization is washing out real differences and every number above is too low. |

## How to read the columns

- **Divergence (primary)** — both cards encode cleanly; header fields (name, cost, types, stats) dropped; only blocks declared order-insensitive are sorted. Functionally equivalent cards differ in name and cost by definition, so scoring the header would report near-100% divergence and measure nothing.
- **Strict ordering** sorts every list. Over-sorting understates divergence and under-sorting overstates it, so the strict/loose pair bounds the true rate rather than asserting one value.
- **All encoded** includes pairs where a card carried `# SPEC_GAP:` markers. A gapped encoding canonicalizes to whatever it managed to express, so this column partly measures gaps rather than representation. It is the looser reading, not the headline.

## Skipped probe sets

- **reprints** — No all-printings bulk supplied. Scryfall's oracle snapshot carries one unified oracle_text per oracle_id, so era-wording pairs cannot be built from it. Pass --all-printings /path/to/all-cards.json to enable this set.

## Deliverable

`t1_divergent_pairs.csv` holds every divergent pair with both canonical forms. That log, not the percentage, is what a canonicalizer would be built from — the rate only decides whether to build one.
