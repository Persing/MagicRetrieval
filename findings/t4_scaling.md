# T4 — deck diversity beyond pair volume

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

Corpus **cedh**. Two levels differing ~12.6x in training decks
(3,142 subsampled, redrawn per seed, against
39,733 full), scored on a **byte-identical** test set of
675,233 queries — asserted by fingerprint in layer 0, not assumed.

**Pre-registered.** The prediction rule, the magnitude gate and the decision rule were frozen in
`THRESHOLDS.md` before the pre-flight, and the point predictions before any model trained. The
naive expectation that deck count carries pair count — which would have predicted ~+0.0104 here —
is recorded there as corrected, not as a second null to choose between afterwards.

**The positive cap does not bind at every level** ({'full_s42': False, 'subsample_s42': False, 'subsample_s43': False, 'subsample_s44': False}), so the levels differ in training volume as well as decks and the prediction below is correspondingly nonzero. The frozen rule handles this without amendment — it was written as a function of the realized example ratio precisely so that this case needed no new decision.

Realized training-example ratios: A 2.97x, B+ 2.97x, C 2.97x.

## Levels

| level | arm | seeds | train decks | positives | training examples | recall@50 | cold-start |
|---|---|---|---|---|---|---|---|
| subsample | A | 3 | 3,142 | 65,574 | 65,424 | 0.0077 | 0.0374 |
| subsample | B+ | 3 | 3,142 | 65,574 | 65,424 | 0.0108 | 0.0369 |
| subsample | C | 3 | 3,142 | 65,574 | 65,424 | 0.0108 | 0.0339 |
| full | A | 3 | 39,733 | 194,592 | 194,312 | 0.0099 | 0.0783 |
| full | B+ | 3 | 39,733 | 194,592 | 194,312 | 0.0070 | 0.0809 |
| full | C | 3 | 39,733 | 194,592 | 194,312 | 0.0083 | 0.0699 |

`training examples` is what training actually consumed, not `n_positives` and not
`n_trainable_positives` — `build_example_oids` drops a positive whose assigned *negative* lacks
text under the arm, and the frozen prediction is a function of this column.

## Verdicts

| arm | universe | observed | volume null | excess | 95% CI | pooled seed sd | verdict |
|---|---|---|---|---|---|---|---|
| A | aggregate | +0.0022 | +0.0042 | -0.0020 | [+0.0020, +0.0024] | 0.0041 | **BELOW_VOLUME_NULL** |
| A | cold_start | +0.0410 | +0.0042 | +0.0367 | [+0.0320, +0.0503] | 0.0074 | **DIVERSITY_BEYOND_VOLUME** |
| B+ | aggregate | -0.0038 | +0.0042 | -0.0081 | [-0.0040, -0.0036] | 0.0076 | **BELOW_VOLUME_NULL** |
| B+ | cold_start | +0.0440 | +0.0042 | +0.0397 | [+0.0350, +0.0533] | 0.0131 | **DIVERSITY_BEYOND_VOLUME** |
| C | aggregate | -0.0025 | +0.0042 | -0.0068 | [-0.0027, -0.0024] | 0.0052 | **BELOW_VOLUME_NULL** |
| C | cold_start | +0.0360 | +0.0042 | +0.0318 | [+0.0276, +0.0449] | 0.0096 | **DIVERSITY_BEYOND_VOLUME** |

**DIVERSITY_BEYOND_VOLUME** requires all three: excess ≥ +0.005, a
paired query-bootstrap CI excluding the volume null, and a gap exceeding the pooled seed sd.
**VOLUME_PROXY** means the interval contains the null — distinct contexts buy nothing that pair
count did not already buy. **BELOW_VOLUME_NULL** means the interval sits entirely below it.

The CI is over **queries**, which both levels share exactly; the seed sd is a separate check and
carries T4's standing null rule unchanged. A row with no interval reports **UNDERPOWERED**
regardless of its magnitude — a verdict resting on two of the three frozen conditions is not the
frozen rule.

The cold-start CI is bootstrapped over the cold-start queries only, which is possible because the
strata are frozen at the full level: both levels filter to the same queries in the same order, so
the two hit vectors are paired by construction rather than by coincidence.

## What this does not license

**Volume is not matched.** The cap binds nowhere here, so the two levels differ in training examples as well as
decks (A 2.97x, B+ 2.97x, C 2.97x), and the volume null above is what the frozen slope says those extra examples buy.
An excess over it is therefore diversity evidence **conditional on D4's slope transferring from
casual to cedh**. The matched-volume design this was meant to be would not have needed that
assumption.

**The cold-start stratum is frozen at full-level counts**, which is what makes the two levels
comparable — same cards, same queries — but it has a consequence. `near_zero ∪ low` means at most
5 appearances among 39,733 train
decks; in a 3,142-deck subsample those same cards expect at most 0.40 appearances.
So most are not *rare* at the low level, they are **absent**. Part of the cold-start
effect is therefore "a few exposures versus none", which is neither deck diversity nor something the
pair-count null models. Read the cold-start rows with that in mind.

**A gap smaller than the pooled seed sd is a null under T4's standing rule**, and
`t4_scaling_verdict` checks the CI branches before that condition — so a `VOLUME_PROXY` or
`BELOW_VOLUME_NULL` row whose gap sits inside its seed sd is a null result, not a finding. The query
bootstrap is tight because it resamples ~10^5 paired queries; it does not capture training variance,
which the seed sd does. Compare the two columns before reading any row as a result. This is a
limitation of the frozen rule, recorded rather than repaired: the rule was fixed before the run and
is not being edited after seeing the numbers.

cedh's played vocabulary is far narrower than the 30,958-card retrieval pool
(PPMI vocabulary 10,133), so absolute recall here is not comparable to
casual's.
