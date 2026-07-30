# T4 — handoff

Operational state for whoever picks this up. Written 2026-07-30, branch
`t4-representation-ablation`, HEAD `1f53df7`, 155 tests passing (`uv run pytest`; 5 more behind
`-m slow`).

Findings live in `findings/`. This file is the map and the landmine list, not a results document —
read `t4_representation.md` for the ladder and `t4_conclusion.md` when it exists.

---

## Where the branch stands

**T4's question is answered on casual at 3 of 5 frozen seeds: representation is not the lever.**
Every gate failed. The finding is not a flat null — it is a monotonic dose-response on cold-start,
ordered by how much structure an arm carries, with a clean sign flip on staples.

| arm | recall@50 | | gated cold-start (n=5,964) | vs A |
|---|---|---|---|---|
| A | 0.0608 | | **0.1415** | — |
| B | 0.0584 | | 0.1386 | −0.0029 (null) |
| B_type | 0.0616 | | 0.1382 | −0.0034 |
| B+ | **0.0624** | | 0.1335 | −0.0080 |
| C | 0.0535 | | 0.1278 | −0.0137 |

On high-play cards the sign flips: B+ beats A by **+0.0148**. Reading: structure raises
representational distinctiveness, which helps where dense co-occurrence makes memorization useful
and hurts where thin data forces generalization. **Structure trades generalization for
memorization.**

**The premise that survives:** cold-start recall is 0.12–0.145, more than double the aggregate,
34× random, against a popularity baseline of **exactly 0.0000**. A content encoder is needed there
and delivers. What fails is *richer structured representation*, not the encoder.

**Stop CDL** is settled and over-determined — see "what is closed" below.

---

## What is closed — do not re-litigate

| question | answer | where |
|---|---|---|
| Does CDL justify continued development? | No. `C − B+ = −0.0089`; on clean-parse cards where CDL is actually used, **−0.0216** | `t4_representation.md` |
| Is that a staple artifact? | No. Survives at **−0.0144** on clean non-staples, ~3.3× paired sd | `t4_staple_reread.md` |
| Is it heterogeneity (two dialects in one space) rather than CDL? | No. Partition gap 0.0031 vs 0.0006–0.0012 controls — real but small against mean cosine 0.526 | `t4_diagnostics.md` |
| Was arm D just data-starved? | No. At **matched 8,702 examples**, encoding effect **+0.0427** vs volume effect +0.0194 | `t4_matched_data.md` |
| Was C's gapped-card gain real? | No — displacement. C evicts cards that are 50.3% clean vs 39.9% baseline | `t4_diagnostics.md` |
| Do tags help? | `B+ − B_type = +0.0008`, NULL | `t4_representation.md` |
| Is the parse-status base-rate inversion meaningful? | No, play-frequency confound. Standardized, it reverses | `t4_staple_reread.md` |
| Popularity baseline? | Computed, arm-independent, `0.18076`. Beats every arm on aggregate; **0.0000** off staples | every stratum table |
| Truncation? | 0.000% over 512 for every arm. 512 frozen because 256 truncates B+ at 1.978% and A at 0% | `t4_encoding_audit.md` |

---

## What is open

**Phase 3 — scaling test (the live question).** Representation is not the constraint; whether
*signal* is has not been tested. Both sweeps, arms **A and B+** (not A alone — the arms are
stratum-dependent with opposite signs, and whether the cold-start structure penalty shrinks with
more decks is the decision-relevant question).

- Deck sweep, 3 levels: 786 / 1,571 / 3,142 train decks.
- Positive sweep, 4 levels at fixed decks: 62.5k / 125k / 250k / **450k**.
  **Only 450,038 positives exist on filtered casual** — 250k/500k/1M is not runnable. Extending
  *down* gives a 7.2× range and retires the 250k cap, an unexamined memory workaround since T2.

Non-negotiables: test set fixed across every level (assert `QuerySet` hashes byte-identical);
criteria **frozen in `THRESHOLDS.md` before the first run** — OLS slope on log(level) with
bootstrap CI excluding zero plus a ≥ +0.010 magnitude gate between extremes, separate constants
per sweep since one has 3 levels and one has 4; evaluate on the **cold-start stratum** as well as
the aggregate; extrapolate with explicit bands only — 3 levels at 3 seeds cannot distinguish
log-linear from saturating.

**Phase 4 — cedh transfer.** A/B+/C × 3 seeds at full size (46,745 decks) *and* a ~3,142-deck
subsample **resampled per seed** (a fixed subsample conflates subsample identity with corpus
effect). cedh needs no deck filter (p50 98, max 99) — a useful control on the filter itself. Note
its played vocabulary is far narrower than the 30,958-card retrieval pool.

**Phase 5 — seeds 45/46.** ~2.5h, unattended, last. Removes the INTERIM banner. Low information;
run it because the write-up is what this branch is judged on.

**Phase 6 — `findings/t4_conclusion.md`.** Order: the dose-response; the premise that survives;
the metric verdict; stop-CDL; then caveats weighted honestly.

---

## Landmines

Things that will silently produce wrong numbers rather than errors.

**`n_trainable_positives` is not the training-set size.** `build_examples` drops a positive when
its assigned *negative* lacks text under that arm, not only when its own ends do. Arm D lost 69.2%
there — 28,215 → **8,702** — and a published caveat understated the confound 3.2× for a whole grid.
Use `n_training_examples`. Fixed at the root in `finetune.build_example_oids`; three tests pin it.

**Never filter positives before `build_example_oids`.** `fallback_pool[i % len(fallback_pool)]` is
position-indexed in the full list, so pre-filtering silently reassigns which negative each anchor
trains against. Tested.

**Layer-0 must come from cache for any diagnostic.** `t4_diagnostics.load_basis` raises if it
didn't. A silent rebuild re-mines positives and rebuilds queries, and every number would then
describe a basis the cached embeddings never saw.

**Cache keys carry the basis.** `t4_representation.embedding_path` and `layer0_dir` both include
the deck-filter max. Changing the filter without that would load models trained on the old corpus
and score them against new queries — no error, numbers describing two corpora at once.

**Diagnostic partials must stay outside `t4_partial_*.json`.** `merge_partials` globs that prefix;
a match becomes a seventh arm, vanishes from the ladder order, and trips `assert_arms_rendered`,
halting the frozen report. Use the `t4_matched_partial_*` shape. Tested end-to-end.

**New `T4_*` constants must be added to `check_in_sync`'s `expected` dict**, or they are frozen in
code and unfrozen in the record. A meta-test enforces it.

**Report guards assert on rendered text, not intermediate dicts.** Both shipped omission bugs had
perfectly correct data; the defect was in the rendering, which is the only artifact anyone reads.
Keep it that way.

**Windows:** `PYTHONIOENCODING=utf-8` on anything that prints findings text — 290 cards carry
U+2212 and the console defaults to cp1252. The `index_reduce() is in beta` warning is expected.

**Do not run other GPU/CPU work during a grid.** Early timings were inflated up to 3× by competing
probe processes and produced a badly wrong cost estimate.

---

## Provenance

- **Parser pin `2f61bf8d`**, upstream `PileOfCardsParser` at `34dd00f` **plus uncommitted
  `tag_extractor.py` changes** that were deliberately vendored (compound `target_type`: 20.9% of
  emissions changed shape). The per-module sha256 in `runs/t4/repr/parser_pin.json` is the
  authoritative identity; the git rev alone is not. Changing it invalidates the layer-1 clause
  cache automatically.
- **Deck filter ≤100 distinct cards** applied in T4 layer-0 only, never in `corpus.load_*` — T0/T1/T2
  recorded their numbers unfiltered and must stay reproducible. 21% of casual entries exceed 100
  distinct cards (max 1,947) and carried 56.5% of PPMI pairs. See `t4_corpus_deck_sizes.md`.
- **T0 recomputed** on the filtered basis: casual 43.2% → 45.9%, FAIL survives. Two stacked
  changes — parser refresh ~+1.0pp, filter +1.6pp — decomposed in `t0_coverage.md`.
- **T2 deliberately not re-run** on the filtered basis. Its question was whether leakage is fixable,
  all arms shared contamination equally, T4 trains fresh models, and T4's rules are all within-T4.
- **`t4_tag_error_breakdown.md` is stale** in one respect: it graded `sample_accuracy_400.jsonl`
  against the *older* tag extractor, and `target_type` is one of the fields that moved.

---

## Commands

```bash
uv run pytest                                    # 155; add -m slow for the 5 training tests
uv run python -m mr.t4_representation --merge    # re-render the frozen ladder from partials
uv run python -m mr.t4_staple_reread             # free, ~2 min
uv run python -m mr.t4_diagnostics               # free, ~3 min
uv run python -m mr.t4_matched_data              # ~8 min GPU
uv run python -m mr.t4_representation --corpus casual --seeds 45 46   # ~2.5h, completes n=5
```

Costs measured on a 4090 at 250k positives: A ~724s/seed, B ~748s, B_type ~885s, B+ ~1,764s,
C ~1,749s, D ~47s. B+/C are ~2.4× A because their sequences are 2.5× longer and attention is
quadratic. Eval is negligible (~2.3s for 40,457 queries) **after** the row-based chunking fix —
query-count chunking was 111s and would have been ~35 min at full scale.

`runs/t4/` is 1.1 GB, gitignored, and fully regenerable. Deleting `runs/t4/emb/` forces retraining;
deleting `runs/t4/repr/` forces a parser re-parse (~50s).
