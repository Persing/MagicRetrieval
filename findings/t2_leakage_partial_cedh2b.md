# T2 — text-geometry leakage retest

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.


**Question:** was the substitute-pair result (0.717 vs 0.748, `Gate_1_3_Findings.md:82`) a training deficiency or structural?

**Gates:** lift ≥ +5.0% with 95% CI excluding zero. Generalization guard: curated lift < 0.5× mined-holdout lift → PHENOTYPE_FIT, not PASS.

## Arm 2a vs. the historical reference (not a gate — see caveat)

> arm 2a was not run

## Ladder results

| Arm | Corpus | Sub. cosine | Random (cluster) | Lift | 95% CI | Mined-holdout lift | Verdict |
|---|---|---|---|---|---|---|---|
| 2b | cedh | 0.8201 | 0.7030 | +0.1171 | [+0.0841, +0.1489] | +0.0108 | **PASS** |

## Decision rules

- **2d passes** → leakage was a training deficiency; branch is healthy, raw text is a viable fallback tier. Check PHENOTYPE_FIT before believing it.
- **2d fails, 2b > 2a** → corpus was the driver; investigate mining quality.
- **2c > 2b materially** → negative supervision was the missing piece — MagicSpike's Gate 2 conclusion needs amending.
- **2d fails outright** → surface text is structurally load-bearing; canonicalized CDL becomes required, raising T4's stakes.

Every arm is scored against the SAME frozen k=50 clustering (`artifacts/cluster_assignments.parquet`) and against an embedding-independent colour×CMC×type stratum control, so the baseline cannot move with the thing being measured.
