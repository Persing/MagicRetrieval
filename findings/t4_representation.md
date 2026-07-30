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
| A | 3 | 0.0608 | 0.0007 | 0.0900 |
| B | 3 | 0.0584 | 0.0010 | 0.0868 |
| B_type | 3 | 0.0616 | 0.0015 | 0.0923 |
| B+ | 3 | 0.0624 | 0.0008 | 0.0937 |
| C | 3 | 0.0535 | 0.0006 | 0.0743 |
| D | 3 | — | — | 0.0315 |

The clean-parse column re-scores **every** arm with candidates and targets restricted to the
10,628 cards CDL parses cleanly. Arm D appears only there: it has no text
for the other cards, so ranking it against the full pool would compare a model that knows a third
of the corpus against models that know all of it.

> **Arm D is confounded and its number is not a clean read on CDL.** D trains only on mined pairs
> whose *both* ends parse cleanly, which is 28,215 pairs against 234,752 for every other arm —
> **12% of the training data**. Its deficit therefore mixes representation with training-set size,
> and the two cannot be separated: the restriction is structural, since the missing pairs involve
> cards D has no text for at all. This is a flaw in the arm as specified, not in the run. Treat
> `D − C` as uninterpretable, and use the parse-status stratification below — which holds the model
> fixed and varies only which cards are being scored — for anything D was meant to answer.

## Gates

| comparison | question | gap | pooled seed sd | gate | verdict |
|---|---|---|---|---|---|
| C - B+ | does CDL justify continued development? | -0.0089 | 0.0007 | +0.02 | **BELOW_GATE** |
| B+ - B | does clause tagging beat plain segmentation? | +0.0040 | 0.0009 | +0.02 | **BELOW_GATE** |
| B - A | does segmentation alone buy anything? | -0.0024 | 0.0009 | +0.01 | **BELOW_GATE** |
| B+ - B_type | do tags help despite their error rate? | +0.0008 | 0.0012 | — | **NULL** |

**The null rule is checked first and can veto a gap that clears its gate:** a difference smaller
than the pooled seed standard deviation is a null result, not a small win. Frozen in
`THRESHOLDS.md` before any arm trained.


## Stratified results

### by parse_status

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| clean | 18,703 | 0.0517 | 0.0492 | 0.0580 | 0.0539 | 0.0364 | 0.3369 | 0.0024 |
| excluded | 1,786 | 0.0780 | 0.0780 | 0.0808 | 0.0838 | 0.0803 | 0.0157 | 0.0028 |
| gap | 19,968 | 0.0677 | 0.0652 | 0.0649 | 0.0668 | 0.0672 | 0.0493 | 0.0031 |

### by play_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| high | 11,283 | 0.0196 | 0.0165 | 0.0344 | 0.0253 | 0.0118 | 0.6481 | 0.0000 |
| low | 5,083 | 0.1451 | 0.1409 | 0.1351 | 0.1413 | 0.1298 | 0.0000 | 0.0043 |
| mid | 23,210 | 0.0601 | 0.0581 | 0.0577 | 0.0595 | 0.0547 | 0.0000 | 0.0038 |
| near_zero | 881 | 0.1211 | 0.1252 | 0.1245 | 0.1199 | 0.1162 | 0.0000 | 0.0011 |

### by unusual_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| common | 19,734 | 0.0624 | 0.0602 | 0.0698 | 0.0654 | 0.0528 | 0.1709 | 0.0032 |
| rare | 11,361 | 0.0542 | 0.0511 | 0.0516 | 0.0524 | 0.0499 | 0.2193 | 0.0030 |
| unknown | 165 | 0.0586 | 0.0384 | 0.0424 | 0.0404 | 0.0444 | 0.0000 | 0.0061 |
| very_common | 4,857 | 0.0651 | 0.0620 | 0.0579 | 0.0671 | 0.0560 | 0.2314 | 0.0012 |
| very_rare | 4,340 | 0.0660 | 0.0660 | 0.0625 | 0.0629 | 0.0639 | 0.0751 | 0.0018 |

Read these before the aggregate. Arms tying overall while separating on the low-play stratum is
itself the finding — that stratum is the cold-start proxy and the entire reason a text-side encoder
exists. `popularity` is the honest floor: under the standing EDHREC confound above, beating it is
the claim that matters.
