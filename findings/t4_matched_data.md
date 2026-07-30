# T4 — encoding effect at matched training volume

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated and no verdict in
> `findings/t4_representation.md` is affected. Specified after seeing the T4 result, with no
> pre-committed criteria.

Arm D trained on **8,702 examples against 234,593**
for every other arm — 3.7% — so its deficit mixes encoding with training volume inseparably. These
runs hold volume fixed.

All rows scored **clean-only on both the query and candidate sides**, since arm D cannot embed
gap cards at all.

| training examples | model | recall@50 | sd |
|---|---|---|---|
| 234,593 | B+ (full, from the frozen ladder) | 0.0937 | 0.0018 |
| 8,702 | B+_matched | 0.0742 | 0.0015 |
| 8,702 | B+_random | 0.0809 | 0.0049 |
| 8,702 | D (CDL only, from the frozen ladder) | 0.0315 | 0.0010 |

## Decomposition

| effect | size | reading |
|---|---|---|
| **encoding**, volume held fixed | `B+_matched − D` = **+0.0427** | what arm D was meant to measure |
| **volume**, encoding held fixed | `B+ full − B+_matched` = **+0.0194** | cost of the 27× data reduction |
| **skew** of D's slice | `B+_matched − B+_random` = **-0.0066** | D's triples are clean-clean anchors, not a random 3.7% |

The skew row is why `B+_random` exists. D's 8,702 triples come only from anchors whose partner and
negative all parse cleanly — cards that are also the most-played (267 vs 45 mean train appearances
at query level). Without that row, `B+_matched − D` would still be confounded, just less visibly.

## What this does not license

**These numbers do not transfer to cold-start.** The clean-only universe is 46.6% high-play — the
memorization regime, where structure helps (B+ beats A by +0.0148 on high-play cards) and where a
popularity ranking alone reaches 0.6481. T4's actual conclusion lives on the cold-start stratum,
where structure is monotonically harmful and popularity scores exactly 0.0000. Nothing here speaks
to that.

They are also **not comparable to the ladder's clean stratum**, which scored clean-parse targets
against the full 30,958-card pool rather than a clean-only candidate set.
