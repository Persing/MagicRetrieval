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

## Scope of this run

**Partial ladder — 4/7 rungs run.** Present: 2c/casual, 2c/cedh, 2d/casual, 2d/cedh. Missing: 2a/precon, 2b/casual, 2b/cedh. The decision rules below that depend on 2a, 2b are unresolved, not failed. Re-run with `--arms 2a 2b` to complete the ladder, then `--merge`.

## Ladder results

| Arm | Corpus | Sub. cosine | Random (cluster) | Lift | 95% CI | Mined-holdout lift | Verdict |
|---|---|---|---|---|---|---|---|
| 2c | casual | 0.8401 | 0.6632 | +0.1769 | [+0.1469, +0.2055] | +0.0298 † | **PASS** |
| 2d | casual | 0.8134 | 0.6344 | +0.1790 | [+0.1460, +0.2114] | +0.0328 † | **PASS** |
| 2c | cedh | 0.8135 | 0.6632 | +0.1503 | [+0.1168, +0.1836] | +0.0236 † | **PASS** |
| 2d | cedh | 0.7952 | 0.6515 | +0.1437 | [+0.1057, +0.1813] | +0.0258 † | **PASS** |

† **Narrow effect** (observational annotation, not part of the frozen verdict gate — added after seeing this pattern in the data, so it deliberately does NOT change PASS/FAIL/PHENOTYPE_FIT). The mined-holdout lift is both below the +0.05 floor and below half the curated lift, using the same 0.5 ratio the generalization guard already commits to, applied symmetrically. Reads as: the effect is real on the hand-picked substitute pairs but does not reach the same bar on a much larger sample of cards that merely co-occur a lot. Plausibly expected — top-decile PPMI is a broader, noisier relation than true functional substitution — but it changes what a bare PASS is actually claiming, so it's called out rather than left implicit.

## Decision rules

- **2d passes** → leakage was a training deficiency; branch is healthy, raw text is a viable fallback tier. Check PHENOTYPE_FIT before believing it.
- **2d fails, 2b > 2a** → corpus was the driver; investigate mining quality. *(2b > 2a already holds on the curated metric for casual (+0.169 > +0.148) but not cedh (+0.117 < +0.148) — mixed on that reading alone; both no-negative arms show the same narrow-effect pattern on the broader mined-holdout metric, which weakens with corpus size rather than strengthening.)*
- **2c > 2b materially** → negative supervision was the missing piece — MagicSpike's Gate 2 conclusion needs amending.
- **2d fails outright** → surface text is structurally load-bearing; canonicalized CDL becomes required, raising T4's stakes.

Every arm is scored against the SAME frozen k=50 clustering (`artifacts/cluster_assignments.parquet`) and against an embedding-independent colour×CMC×type stratum control, so the baseline cannot move with the thing being measured.
