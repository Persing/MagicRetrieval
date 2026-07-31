# T4 — is the cedh cold-start gain coverage or diversity?

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated and no verdict in
> `findings/t4_scaling.md` is affected. Specified after seeing the scaling result, with no
> pre-committed criteria. It constrains which *mechanism* the evidence supports.

`t4_scaling` found the full cedh level beating the 3,142-deck
subsample by ~+0.041 on cold-start and called it diversity. A cheaper mechanism predicts the same
signature: the PPMI vocabulary is **4,821 cards at the subsample level against 10,133 at full**, so
~5,300 cards go from *zero* mined positives to some. A card with no positives is never trained on.
Cold-start cards are exactly the ones crossing that line.

Both mechanisms predict a tail-concentrated gain and a flat aggregate, so the original observation
cannot separate them — and they imply opposite actions. Coverage says fix the mining, which is
cheap. Diversity says buy more decks, which is not.

**The split.** Cold-start queries (2,222 of them) partitioned by whether the target
is in that seed's subsample vocabulary. Query set rebuilt from the vendored corpus and checked
against the recorded fingerprint `3fe8406f6b91e0ed` — the per-query indices are
aligned by construction, not by assumption.

| arm | group | queries | gain | sd over seeds | contribution to total |
|---|---|---|---|---|---|
| A | newly covered | 1,814 | +0.0487 | 0.0060 | +0.0398 |
| A | covered both | 407 | +0.0070 | 0.0234 | +0.0013 |
| B+ | newly covered | 1,814 | +0.0508 | 0.0166 | +0.0415 |
| B+ | covered both | 407 | +0.0099 | 0.0281 | +0.0018 |
| C | newly covered | 1,814 | +0.0427 | 0.0131 | +0.0349 |
| C | covered both | 407 | +0.0042 | 0.0048 | +0.0008 |

The frozen volume null from `t4_scaling.md` is **+0.0042** — what the 2.97x extra
training examples alone are predicted to buy. `covered both` is the group where a diversity effect
could show up, because those cards were already in the mining vocabulary at both levels.

`contribution to total` is the group's gain weighted by its share of cold-start queries; the two
rows for an arm sum to that arm's overall cold-start gain.

## Reading

- A: **COVERAGE.** 97% of the gain is in cards the subsample could not mine at all. Among cards covered at both levels the gain is +0.0070 against a volume null of +0.0042 — a difference of +0.0028, inside its own seed sd of 0.0234. No diversity effect is detectable.
- B+: **COVERAGE.** 96% of the gain is in cards the subsample could not mine at all. Among cards covered at both levels the gain is +0.0099 against a volume null of +0.0042 — a difference of +0.0057, inside its own seed sd of 0.0281. No diversity effect is detectable.
- C: **COVERAGE.** 98% of the gain is in cards the subsample could not mine at all. Among cards covered at both levels the gain is +0.0042 against a volume null of +0.0042 — a difference of +0.0000, inside its own seed sd of 0.0048. No diversity effect is detectable.

**If coverage dominates**, the actionable finding is not "acquire more decks" but "mine rare cards
at all" — the top-decile PPMI threshold in `mining.mine_positives` is what excludes them, and it is
a tunable, not a fact about the corpus. **If the gain survives among cards covered at both levels**,
that is diversity in the intended sense and the deck-acquisition reading stands.

Either way this does not touch the frozen verdicts in `t4_scaling.md`, which measured what they
measured. It changes what the number should be *called*.
