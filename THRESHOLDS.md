# Pre-commit thresholds — FROZEN

**Committed before the first run.** Every number here was chosen against a stated anchor, not
against a result. If any of these change, the change must be a separate commit with a reason, made
before the run it affects — never after seeing a number.

Consumed programmatically by `mr.thresholds`; every findings JSON embeds a copy of what it was
judged against.

---

## T0 — Frequency-weighted coverage

| Gate | Value |
|---|---|
| PASS — tail is not load-bearing, CDL growth deprioritized | appearance-weighted coverage **≥ 85%** |
| FAIL — tail is load-bearing | appearance-weighted coverage **< 70%** |
| UNRESOLVED — defer to T3 | 70% – 85% |

**Honesty guard.** The headline number must also hold on the **non-land, non-staple** slice, within
**10 percentage points**. If the aggregate passes but the guarded slice is >10 points lower, the
verdict is UNRESOLVED regardless of the aggregate.

*Anchor:* 85% of ~95 cards/deck leaves ~14 unparsed cards per deck — absorbable by a fallback tier.
Below 70%, the fallback is carrying a third of every deck and is no longer a fallback. The guard
exists because Sol Ring / Command Tower / Arcane Signet alone account for 2.1 cards per deck
(MagicSpike `findings/Rung0_5_TagSpace.md`) and lands parse trivially — an aggregate number is
inflated by exactly the cards CDL does not need to help with.

## T1 — CDL divergence rate

| Gate | Value |
|---|---|
| PASS — no canonicalizer; reviewed lookup table for residuals | divergence **< 10%** |
| PARTIAL — targeted rewrite rules for observed classes only | 10% – 25% |
| FAIL — build the canonicalizer from observed cases | divergence **> 25%** |

**Numeric subset.** The *same-effect-different-numbers* probe set is judged separately and must
diverge at **< 5%**.

*Anchor:* above 25%, terminal states are effectively wording-indexed — the exact failure the branch
exists to avoid. The numeric subset should be near zero because numbers are supposed to be
parameterized out; a miss there is a targeted parser bug, not evidence for a canonicalizer.

## T2 — Text-geometry leakage retest

| Gate | Value |
|---|---|
| PASS — leakage is fixed | substitute mean cosine exceeds random same-cluster by **≥ +0.05**, with a paired-bootstrap **95% CI excluding zero** |

**Generalization guard.** Report curated-pair lift *and* held-out-mined-pair lift. If curated lift
is **< half** the mined lift, the run is recorded as **phenotype-fit, not generalization**, even if
the numeric threshold is met.

**Reference point, not a gate (amended after building the harness).** Arm 2a was meant to reproduce
the prior baseline, **≈0.717 substitutes vs ≈0.748 random**. Building it surfaced that this isn't
achievable: per Gate 1's own corpus description (`Gate_1_3_Findings.md`), the original run used 224
precon decks **plus "several hundred" early cEDH decks** — a mixed corpus that no longer exists in
isolated form. The current cEDH pool has grown to 46,798 decks, and `backup_precon_only`'s
PPMI/embeddings turned out to be a byte-identical copy of the current combined-corpus build (md5
confirmed), not an isolated precon-only snapshot. Arm 2a here is a precon-**only** rebuild (224
decks, zero cEDH) — a smaller, different corpus than the historical one. Its numbers are reported
as the closest available reference, and `t2_verdict` does not gate on matching them: a mismatch
means the corpus changed, not that the harness is broken. This amendment is dated 2026-07-25 and
made before any arm produced a result, not after seeing an unfavorable number.

*Anchor:* the prior result is −0.031 (0.717 vs 0.748), so +0.05 demands a ≥0.08 swing. The CI clause
is what makes it a result rather than noise. The generalization guard exists because mining
positives as "high PPMI + text-distant" *is* the substitute phenotype — pair-level disjointness does
not prevent distributional teaching-to-the-test.

## T4 — Representation ablation

Six arms differing **only** in how a card becomes a vector: **A** (whole oracle text), **B**
(segment-split), **B_type** (+ clause type labels), **B+** (+ tags and values), **C** (CDL where it
parses cleanly, B+ everywhere else), **D** (CDL only, clean-parse cards — diagnostic). Same corpus,
split, negatives, objective, hyperparameters and seeds throughout.

Task: leave-one-out retrieval on held-out decks. Primary metric **recall@50**; secondary
**recall@10** and MRR. Deck-level split, **15%** held out.

| Comparison | Question | Gate |
|---|---|---|
| **C − B+** | does CDL justify continued development? | **≥ +0.02** |
| **B+ − B** | does clause tagging beat plain segmentation? | ≥ +0.02 |
| **B − A** | does segmentation alone buy anything? | ≥ +0.01 |
| **B+ − B_type** | do tags help despite 13% error? | no gate — any gap beyond seed sd is a finding; **negative means tags are net-harmful** |
| **D − C** | cost of the fallback tier | diagnostic, no gate |
| **low-play stratum, best arm − A** | does structure help cold-start specifically? | ≥ +0.03 |

**Null rule — frozen before any number was seen.** If the gap between two arms is smaller than the
pooled seed standard deviation, the result is **null**, not a small win. This is checked *first* and
can veto a gap that clears its gate.

**Seeds.** **5 seeds** per arm, mean ± sd, frozen now rather than "3 and extend if it looks
interesting" — choosing n after seeing results is choosing a stopping rule from the data, and at
n=3 the sample sd is itself too noisy for the null rule to mean anything.

**Low-play stratum.** Defined as **at most 5** appearances across *training* decks (test-deck
appearances would leak). Measured on casual, the true-zero bucket holds only 719 of 71,566 held-out
targets (1.0%), where a +0.03 difference sits at roughly 2σ binomial — so the gate binds on the
≤5 bucket (5,770 targets, 8.4%) and the zero bucket is reported separately, flagged underpowered.

**`max_seq_length` is 512**, set once and identically for every arm. At the sentence-transformers
default of 256 the structured arms truncate ~1.9% of cards while the text arms truncate none; that
asymmetry would make the measured gap lost information rather than representation. At 512 no arm
truncates at all (`findings/t4_encoding_audit.json`).

*Anchor:* the gates are the plan's suggested starting values, adopted deliberately. +0.02 recall@50
on a ~31,000-card candidate pool is roughly a 1-in-50 improvement in whether the held-out card
surfaces at all — small enough to be reachable, large enough to justify maintaining a second
representation. +0.01 for `B − A` is lower because segmentation is nearly free; +0.03 for cold-start
is higher because that stratum is the entire reason a text-side encoder exists, and the aggregate
can hide it.

---

## Standing confound — recorded, not solved

Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not
independent of EDHREC's recommendations. The effect is **strongest in the Archidekt casual corpus**,
which is the most EDHREC-derived population of the two.

All arms inherit it equally, so relative comparisons between arms survive. Absolute numbers do not
mean much. This paragraph is reproduced in the header of every findings file.
