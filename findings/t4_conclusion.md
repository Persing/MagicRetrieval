# T4 — conclusion

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **INTERIM — 3 of 5 frozen seeds.** Every mean and standard deviation below moves when the remaining seeds land, and the null rule is measured against a sample sd that is itself noisy at this n. The *direction* of each finding is stable across the seeds that have run; the magnitudes are not final. Complete with `--seeds 45 46`, re-merge, and re-run this.

Six arms, one variable: how a card becomes a vector. Leave-one-out retrieval on held-out
decks, primary metric recall@50, deck-level split, corpus **casual** —
3,142 train / 555 test decks,
40,457 queries against a 30,958-card pool.

**Richer structured representation is not the lever.** Every frozen gate failed. But the result is
not a flat null, and the shape of it is the finding.

## 1. The dose-response

| arm | seeds | recall@50 | gated cold-start | sd | vs A (paired) | pooled sd | null rule |
|---|---|---|---|---|---|---|---|
| A | 3 | 0.0608 | 0.1415 | 0.0004 | — | — | — |
| B | 3 | 0.0584 | 0.1386 | 0.0048 | -0.0029 | 0.0034 | **NULL** |
| B_type | 3 | 0.0616 | 0.1382 | 0.0013 | -0.0034 | 0.0010 | **REAL_GAP** |
| B+ | 3 | 0.0624 | 0.1335 | 0.0019 | -0.0080 | 0.0014 | **REAL_GAP** |
| C | 3 | 0.0535 | 0.1278 | 0.0017 | -0.0137 | 0.0012 | **REAL_GAP** |

Cold-start is the gated `near_zero ∪ low` stratum — at most
5 appearances across *training* decks — 5,964 targets.
The `vs A` column is the per-seed paired difference, which is strictly more informative because all
arms share the split, the queries and the seeds. The verdict beside it is the **frozen** rule, which
is measured against the pooled seed sd and is the one that counts.

The arms are ordered by how much structure they carry, and the cold-start column falls
monotonically along that ordering. On the high-play bucket (11,283 targets) the sign
flips: `B+ − A` = **+0.0148** paired across 3 seeds.

**Reading: structure trades generalization for memorization.** Added structure raises
representational distinctiveness, which pays where dense co-occurrence makes memorizing a card's
neighbourhood viable, and costs where thin data forces generalizing from text. The frozen
cold-start gate asked for the opposite — best arm − A ≥ +0.03 — and
got a negative number instead.

## 2. The premise that survives

Cold-start recall lands at 0.1278–0.1415: about
2.0× the aggregate, **33× random** (0.003856), against
a popularity baseline of **exactly 0.0000**.

A content encoder is needed there, and it delivers. What T4 refutes is *richer structured
representation*, not the text encoder — arm A, the plainest arm in the ladder, is the one to ship
for the tail.

## 3. The metric verdict

Popularity alone scores **0.1808** on the aggregate, beating every arm
(best: B+ at 0.0624). It reaches
0.6481 on high-play cards and 0.0000 off them.

A global frequency ranking returns the same top-50 for every query, so **aggregate recall@50 on
this task substantially measures staple recovery**. That is why the stratified numbers, not the
aggregate, are T4's output — and why the shippable system is popularity for staples and arm A for
the tail, split on play count, rather than one ranker.

## 4. Stop CDL

Over-determined. Four independent attempts to rescue it, each with the number that failed:

| question | answer | figure |
|---|---|---|
| Does CDL justify continued development? | No | `C − B+` = **-0.0089**, pooled seed sd 0.0007, gate +0.02 → **BELOW_GATE** |
| Is that an artifact of the staple-enriched clean stratum? | No | clean **-0.0216**, clean non-staple **-0.0144** = 3.4× its paired sd |
| Is it heterogeneity — two dialects in one space — rather than CDL? | No | C's partition gap **0.0031** vs 0.0006–0.0012 for the single-dialect arms; real, but small against mean cosine 0.526 |
| Was arm D simply data-starved? | No | at matched volume the encoding effect is **+0.0427** against a volume effect of +0.0194 over 27× the data |

C's apparent gain on gapped cards is **displacement, not skill**: the cards C evicts from the top-50
are 50.2% clean against a 39.8% baseline
share. It moves clean-parse cards out to let gap cards in; the slots are conserved.

CDL is used on 10,628 of 30,958 cards in the pool, and on
exactly those cards it makes retrieval worse.

## Caveats, weighted

- **The EDHREC confound above is the largest one and is not solved.** Absolute numbers do not mean
  much; relative comparisons between arms survive because all arms inherit it equally.
- **Popularity beating every arm on the aggregate is a real result, not a harness fault.** It is
  also the sharpest statement of why the aggregate is the wrong headline.
- **The true-zero bucket is underpowered** and is flagged as such in every partial —
  881 of 40,457 targets, where a difference the size of the
  frozen gate sits at roughly 2σ binomial. That is why the cold-start gate binds on
  `near_zero ∪ low` rather than on `near_zero` alone: the bucket was reported, not quietly widened
  until it looked significant.
- **The matched-volume decomposition does not transfer to cold-start.** It is scored clean-only on
  both sides, and that universe is the memorization regime where structure helps and popularity
  alone reaches high recall. It settles arm D's confound and nothing about the tail.
- **Everything above is one corpus.** cedh transfer is a separate run.
- **The diagnostics in §4 were specified after seeing the T4 result** and carry no pre-committed
  criteria. They constrain *which claim* the evidence supports; the decision rests on the gated
  ladder and the cold-start dose-response, which were frozen first.
