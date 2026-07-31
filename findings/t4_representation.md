# T4 — representation ablation

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **INTERIM — 3 of 5 frozen seeds.** The verdicts below are computed on 3 seeds and are **not final**. `THRESHOLDS.md` fixes n=5 so that n is not chosen after seeing results, and at n=3 the sample sd is itself noisy enough to make the null rule unstable. Read this pass for harness sanity and for whether the seed spread looks plausible — not for whether an arm won. Complete with `--seeds 45 46` and re-merge.

Six arms differing only in how a card becomes a vector. Task: leave-one-out retrieval on
held-out decks, primary metric recall@50, deck-level split. Corpus **casual**,
3,142 train / 555 test decks,
40,457 queries against a 30,958-card candidate pool.

## Arms

| arm | seeds | recall@50 mean | sd | recall@50, clean-parse universe |
|---|---|---|---|---|
| A | 4 | 0.0607 | 0.0006 | 0.0896 |
| B | 4 | 0.0584 | 0.0009 | 0.0868 |
| B_type | 4 | 0.0613 | 0.0013 | 0.0912 |
| B+ | 4 | 0.0628 | 0.0010 | 0.0953 |
| C | 3 | 0.0535 | 0.0006 | 0.0743 |
| D | 4 | — | — | 0.0314 |

The clean-parse column re-scores **every** arm with candidates and targets restricted to the
10,628 cards CDL parses cleanly. Arm D appears only there: it has no text
for the other cards, so ranking it against the full pool would compare a model that knows a third
of the corpus against models that know all of it.

> **Arm D is confounded and its number is not a clean read on CDL.** D trained on
> **8,702 examples against 234,593 for every other arm — 3.7% of the training
> data**. Two compounding restrictions: it only trains on mined pairs whose *both* ends parse
> cleanly, and `build_example_oids` then drops any survivor whose assigned negative also lacks
> text under this arm, which removes a further 69% of them. Its deficit therefore mixes
> representation with training-set size, and the two cannot be separated — the restriction is
> structural, since the missing pairs involve cards D has no text for at all.
>
> An earlier version of this report quoted 28,215 / 12%, taken from `n_trainable_positives`, a
> both-ends-have-text predicate that is **not** what training consumed. Arms with full text
> coverage lose ~0.1% at that second step, which is why the discrepancy stayed invisible.
>
> Treat `D − C` as uninterpretable. Use the parse-status stratification below — which holds the
> model fixed and varies only which cards are scored — for anything D was meant to answer.

## Gates

| comparison | question | gap | pooled seed sd | gate | verdict |
|---|---|---|---|---|---|
| C - B+ | does CDL justify continued development? | -0.0093 | 0.0009 | +0.02 | **BELOW_GATE** |
| B+ - B | does clause tagging beat plain segmentation? | +0.0044 | 0.0009 | +0.02 | **BELOW_GATE** |
| B - A | does segmentation alone buy anything? | -0.0023 | 0.0007 | +0.01 | **BELOW_GATE** |
| B+ - B_type | do tags help despite their error rate? | +0.0015 | 0.0012 | — | **REAL_GAP** |

**The null rule is checked first and can veto a gap that clears its gate:** a difference smaller
than the pooled seed standard deviation is a null result, not a small win. Frozen in
`THRESHOLDS.md` before any arm trained.


## Stratified results

### by parse_status

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| clean | 18,703 | 0.0516 | 0.0493 | 0.0584 | 0.0531 | 0.0364 | 0.3369 | 0.0024 |
| excluded | 1,786 | 0.0788 | 0.0788 | 0.0812 | 0.0833 | 0.0803 | 0.0157 | 0.0028 |
| gap | 19,968 | 0.0676 | 0.0651 | 0.0653 | 0.0671 | 0.0672 | 0.0493 | 0.0031 |

### by play_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| high | 11,283 | 0.0189 | 0.0167 | 0.0355 | 0.0239 | 0.0118 | 0.6481 | 0.0000 |
| low | 5,083 | 0.1445 | 0.1412 | 0.1359 | 0.1421 | 0.1298 | 0.0000 | 0.0043 |
| mid | 23,210 | 0.0603 | 0.0579 | 0.0578 | 0.0596 | 0.0547 | 0.0000 | 0.0038 |
| near_zero | 881 | 0.1234 | 0.1263 | 0.1220 | 0.1200 | 0.1162 | 0.0000 | 0.0011 |

### by unusual_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| common | 19,734 | 0.0625 | 0.0603 | 0.0700 | 0.0648 | 0.0528 | 0.1709 | 0.0032 |
| rare | 11,361 | 0.0540 | 0.0514 | 0.0529 | 0.0525 | 0.0499 | 0.2193 | 0.0030 |
| unknown | 165 | 0.0576 | 0.0379 | 0.0424 | 0.0394 | 0.0444 | 0.0000 | 0.0061 |
| very_common | 4,857 | 0.0652 | 0.0620 | 0.0578 | 0.0669 | 0.0560 | 0.2314 | 0.0012 |
| very_rare | 4,340 | 0.0651 | 0.0646 | 0.0626 | 0.0632 | 0.0639 | 0.0751 | 0.0018 |

Read these before the aggregate. Arms tying overall while separating on the low-play stratum is
itself the finding — that stratum is the cold-start proxy and the entire reason a text-side encoder
exists. `popularity` is the honest floor: under the standing EDHREC confound above, beating it is
the claim that matters.
