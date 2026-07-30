# T4 — casual corpus deck-size audit

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

Surfaced while building T4's leave-one-out evaluator: some casual "decks" produce retrieval
contexts of over 1,000 cards. They are not decks.

**A legal Commander deck is exactly 100 cards**, so the number of *distinct* oracle_ids in one can
never exceed 100 — and is usually 80–99, because duplicate basic lands collapse to a single
oracle_id. That makes >100 a definitional impossibility rather than an arbitrary cut point.

## casual (n=4,982)

| percentile | p1 | p10 | p25 | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|
| distinct cards | 5 | 61 | 79 | 90 | 101 | 130 | 166 | 271 | 1,947 |

The distribution is bimodal in kind, not just in size:

- **A real-deck mode at 80–99 cards**, holding about half the corpus, plus a spike at exactly 100.
- **An over-legal tail**: 1,285 decks (25.8%) exceed 100 distinct cards, reaching 1,947.
  Checked against the raw census: these are genuinely long source lists, not a name-resolution
  artifact — the 1,947-card entry has 1,958 raw names. They are Archidekt lists carrying
  maybeboards, sideboards or whole collections.
- **A stub tail**: 348 decks (7.0%) hold fewer than 40 cards, including 79 with under 10 and
  a cluster of 1-card entries that are commander-only shells.

## Why it matters more than the deck share suggests

Co-occurrence pairs grow quadratically with deck size, so oversized entries dominate PPMI far
beyond their headcount:

| population | share of decks | share of PPMI pairs |
|---|---|---|
| over 100 distinct cards | 25.8% | **60.5%** |
| under 40 distinct cards | 7.0% | 0.3% |

**The majority of the casual corpus's co-occurrence signal comes from entries that cannot be legal
decks.** This is inherited by T0's appearance weighting and by every T2 arm trained on casual; it
is reported here, not silently corrected in those findings.

For T4 specifically it also breaks the task definition: leave-one-out retrieval given a 1,088-card
"context" is not the deck-building situation the retriever is meant to serve.

## Candidate cut points

| band | decks kept | card-slots kept | PPMI pairs kept | max context |
|---|---|---|---|---|
| 0–100 | 3,697 (74.2%) | 60.7% | 39.5% | 100 |
| 0–105 | 3,932 (78.9%) | 65.8% | 43.5% | 105 |
| 40–100 | 3,349 (67.2%) | 59.4% | 39.3% | 100 |
| 40–105 | 3,584 (71.9%) | 64.5% | 43.3% | 105 |
| 60–105 | 3,445 (69.1%) | 63.0% | 42.7% | 105 |

## cedh, for contrast

n=6,000, p50 98, p99 99, max 99. Every deck is
already a legal Commander list — 100.0% fall within any of the bands above. The problem is
specific to the Archidekt casual census, which is also the corpus the standing EDHREC confound
calls out as the most derived.

## Status

**Open decision, deliberately not taken here.** Filtering is arm-independent and would be applied
before any arm is named, so it cannot bias the comparison between arms — but it changes the corpus
basis away from what T0 and T2 measured, and the spec warns against curating the held-out
population. Recorded for a decision rather than resolved unilaterally.
