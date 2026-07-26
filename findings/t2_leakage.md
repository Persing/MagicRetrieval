# T2 — text-geometry leakage retest

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.


**Question:** was the substitute-pair result (0.717 vs 0.748, `Gate_1_3_Findings.md:82`) a training deficiency or structural?

**Gates:** lift ≥ +5.0% with 95% CI excluding zero. Generalization guard: curated lift < 0.5× mined-holdout lift → PHENOTYPE_FIT, not PASS.

## Arm 2a vs. the historical reference (not a gate — see caveat)

Observed: substitutes=0.7934, random=0.6456. Historical reference ≈0.717 / ≈0.748 (±0.02). **DOES NOT MATCH REFERENCE**

> 2a is a precon-only rebuild (224 decks); the historical baseline's corpus additionally included several hundred early cEDH decks that no longer exist in isolated form. A mismatch is expected.

## Scope of this run

**Partial ladder — 3/7 rungs run, stopped deliberately after 2b.** Missing: 2c/casual, 2c/cedh, 2d/casual, 2d/cedh. 2c/2d (negative supervision, wired to the loss and mined-phenotype pairs respectively) were not run — the decision rules below that depend on them are unresolved, not failed. Re-run with `--arms 2c 2d` to complete the ladder.

## Ladder results

| Arm | Corpus | Sub. cosine | Random (cluster) | Lift | 95% CI | Mined-holdout lift | Verdict |
|---|---|---|---|---|---|---|---|
| 2a | precon | 0.7934 | 0.6456 | +0.1478 | [+0.1035, +0.1920] | +0.0453 † | **PASS** |
| 2b | casual | 0.8408 | 0.6721 | +0.1687 | [+0.1372, +0.1982] | +0.0218 † | **PASS** |
| 2b | cedh | 0.8201 | 0.7030 | +0.1171 | [+0.0841, +0.1489] | +0.0108 † | **PASS** |

† **Narrow effect** (observational annotation, not part of the frozen verdict gate — added after seeing this pattern in the data, so it deliberately does NOT change PASS/FAIL/PHENOTYPE_FIT). The mined-holdout lift is both below the +0.05 floor and below half the curated lift, using the same 0.5 ratio the generalization guard already commits to, applied symmetrically. Reads as: the effect is real on the hand-picked substitute pairs but does not reach the same bar on a much larger sample of cards that merely co-occur a lot. Plausibly expected — top-decile PPMI is a broader, noisier relation than true functional substitution — but it changes what a bare PASS is actually claiming, so it's called out rather than left implicit.

## Decision rules

- **2d passes** *(unresolved — needs 2d)* → leakage was a training deficiency; branch is healthy, raw text is a viable fallback tier. Check PHENOTYPE_FIT before believing it.
- **2d fails, 2b > 2a** *(unresolved — needs 2d)* → corpus was the driver; investigate mining quality. *(2b > 2a already holds on the curated metric for casual (+0.169 > +0.148) but not cedh (+0.117 < +0.148) — mixed on that reading alone; both no-negative arms show the same narrow-effect pattern on the broader mined-holdout metric, which weakens with corpus size rather than strengthening.)*
- **2c > 2b materially** *(unresolved — needs 2c)* → negative supervision was the missing piece — MagicSpike's Gate 2 conclusion needs amending.
- **2d fails outright** *(unresolved — needs 2d)* → surface text is structurally load-bearing; canonicalized CDL becomes required, raising T4's stakes.

Every arm is scored against the SAME frozen k=50 clustering (`artifacts/cluster_assignments.parquet`) and against an embedding-independent colour×CMC×type stratum control, so the baseline cannot move with the thing being measured.

## Interim read (2a/2b only, stopped here deliberately)

On the curated substitute metric alone, leakage looks fixed: all three completed arms clear the +0.05 lift floor with CIs excluding zero, a qualitatively different result from the original −0.031 (0.717 vs 0.748). But the narrow-effect pattern is consistent across every arm — including 2b, which already has the broad corpus — so corpus breadth alone is not what's driving it. Whether wiring hard negatives (2c) or mined phenotype pairs (2d) changes that pattern is exactly what the deferred rungs would answer, and is not decided by this run.
