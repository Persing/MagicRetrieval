# T4 — handoff

Operational state for whoever picks this up. Written 2026-07-30, branch
`t4-representation-ablation`, 205 tests passing (`uv run pytest`; 5 more behind `-m slow`).

Findings live in `findings/`. This file is the map and the landmine list, not a results document —
read `t4_representation.md` for the ladder and `t4_conclusion.md` for the write-up.
`RUNPOD.md` is the runbook for Phase 4 on rented GPUs.

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

**Both casual sweeps are dropped.** Recorded here as a deliberate pre-run scope reduction with its
reason, not silently omitted.

- *Positive sweep* — superseded by D4, which ran it wider and de-skewed: `B+_random` (8,702) →
  `B+_full` (234,593) is **27×** against the sweep's 7.2×, same corpus and test set. It also
  retires the 250k cap directly: 1.8× headroom at **0.00389/e-fold** is worth ~**+0.0023**. The one
  thing the sweep would add — whether the slope flattens at high volume — only strengthens that if
  true.
- *Deck sweep* — **kinked, therefore misspecified.** Measured: 786 train decks mine **168,786**
  positives (cap does not bind), 3,142 mine 450,038 and draw 250,000 (it does). So levels 1→2 vary
  example volume and 2→3 do not, and one OLS slope on log(decks) across that is fitting two
  different regimes. If it is ever revived, regress on log(**realized examples**) and do not fit a
  single slope across the kink. The harness supports it: `--train-subsample` with a reference level.

**Phase 4 — cedh transfer. This is now the whole scaling test, and a better one than planned.**
A/B+/C × 3 seeds at full size (46,745 decks, ~39,733 train) *and* a 3,142-deck **train** subsample
**resampled per seed** (a fixed subsample conflates subsample identity with corpus effect).

*Expected but **not yet measured**:* the cap binds at both levels, holding example volume constant
while decks vary ~12.6×, which makes the pure-volume null **exactly 0.0000** and the comparison a
clean diversity isolation. `mr.t4_scaling --preflight` is what settles it, and the frozen rule needs
no amendment either way — the prediction is a function of the realized example ratio, so if the cap
turns out not to bind the null simply stops being zero. Order: pre-flight, freeze the point
predictions from its ratios, then run the grid. Frozen rule in `THRESHOLDS.md` under "T4 scaling".

Non-negotiables, all now enforced in code rather than by discipline: the subsample is applied
**after** `split_by_deck`; the test set is asserted byte-identical by fingerprint; strata and the
popularity baseline are loaded from the full level so the cold-start bucket holds the same cards at
both levels; the layer-0 cache key and the embedding path both carry the subsample.

cedh needs no deck filter (p50 98, max 99) — a useful control on the filter itself. Its played
vocabulary is far narrower than the 30,958-card retrieval pool.

**Phase 5 — seeds 45/46.** ~3.3h total, but chunkable: `main` skips runs whose partial exists, so
`--seeds 45 --arms A B B_type D` (~40 min), then `--arms B+` (~29 min), then `--arms C` (~29 min).
Removes the INTERIM banner. **Must run on the same GPU as seeds 42/43/44** — the pooled seed sd is
the denominator of the null rule, and splitting seeds across hardware adds a term no seed controls.
Must also finish before any cedh partial is written.

**Phase 6 — `findings/t4_conclusion.md`. Done, and it is a renderer** (`mr.t4_conclusion`), not a
hand-written file: every number is read from the frozen findings JSONs, so Phase 5 and Phase 4
refresh it by re-running rather than by anyone remembering to. Two guards, both on rendered text —
an incomplete ladder must carry the INTERIM banner, and each closed question must appear with its
figure.

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
halting the frozen report. Use the `t4_matched_partial_*` / `t4_scaling_partial_*` shape. Tested
end-to-end.

**`merge_partials` is corpus-scoped, and had to become so.** It groups by *arm* alone, so before
the fix a single `--corpus cedh` run dropped beside the casual partials would have reported arm A at
`n_seeds=6` as the mean of two corpora, taken `layer0_meta` from casual, and **lost the INTERIM
banner** because the count cleared n=5 — a report reading as final while averaging two corpora under
one corpus's deck counts. It now takes `corpus=` and writes `t4_representation_<corpus>` for
anything but casual. Tested.

**The INTERIM seed count is the `min` across arms, not the `max`.** Chunked completion makes the
grid ragged on purpose; under `max`, arm A reaching n=5 while C sat at 4 would have dropped the
banner off an unfinished ladder. A verdict is only as complete as its weaker arm.

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

- **Parser pin `174bc3a1`** (was `2f61bf8d` before 2026-07-30 — see below), upstream
  `PileOfCardsParser` at `34dd00f` **plus uncommitted
  `tag_extractor.py` changes** that were deliberately vendored (compound `target_type`: 20.9% of
  emissions changed shape). The per-module sha256 in `runs/t4/repr/parser_pin.json` is the
  authoritative identity; the git rev alone is not. Changing it invalidates the layer-1 clause
  cache automatically.
- **The pin was recomputed on 2026-07-30 and its value changed, without the parser changing.**
  It used to hash raw file bytes, which made it identify *a checkout* rather than *a parser*: the
  vendored tree has mixed line endings and `core.autocrlf=true` on Windows, so the same commit
  produced `2f61bf8d` on Windows and `174bc3a1` on Linux. The first pod run tripped the guard on
  that difference alone. `parser_pin` now normalizes CRLF to LF before hashing, so both platforms
  agree on `174bc3a1`, and `tests/test_parser_pin.py` pins the property in both directions —
  line endings must not move it, a one-character source edit must.
  **Layer 1 is unchanged**: rebuilt under the new pin, `cdl.parquet` and `signature_freq.parquet`
  are byte-identical and the stats match exactly (10,637 clean / 2,042 excluded / 18,362 gap,
  104,578 clauses). `clauses.jsonl.gz` differs in bytes only because gzip stamps the build time into
  its header; decompressed, it hashes identically across rebuilds. **Findings recorded against
  `2f61bf8d` therefore remain valid** — the two pins name the same parser.
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
uv run pytest                                    # 205; add -m slow for the 5 training tests
uv run python -m mr.t4_representation --merge    # re-render the frozen ladder from partials
uv run python -m mr.t4_conclusion                # re-render the conclusion from the findings
uv run python -m mr.t4_staple_reread             # free, ~2 min
uv run python -m mr.t4_diagnostics               # free, ~3 min
uv run python -m mr.t4_matched_data              # ~8 min GPU

# Phase 5, chunked to stay inside a ~1h sustained-load budget. Resumes for free: `main` skips any
# (arm, seed) whose partial exists, and layer 0 is cached.
uv run python -m mr.t4_representation --corpus casual --seeds 45 --arms A B B_type D   # ~40 min
uv run python -m mr.t4_representation --corpus casual --seeds 45 --arms B+             # ~29 min
uv run python -m mr.t4_representation --corpus casual --seeds 45 --arms C              # ~29 min
#   ... repeat for --seeds 46, then --merge and re-run t4_conclusion

# Phase 4. Pre-flight first: counts only, no training, no recall number — safe to run before the
# point predictions are frozen, and its output is what they get frozen from.
uv run python -m mr.t4_scaling --preflight --corpus cedh
uv run python -m mr.t4_scaling --corpus cedh
```

Costs measured on a 4090 at 250k positives: A ~724s/seed, B ~748s, B_type ~885s, B+ ~1,764s,
C ~1,749s, D ~47s. B+/C are ~2.4× A because their sequences are 2.5× longer and attention is
quadratic. Eval is negligible (~2.3s for 40,457 queries) **after** the row-based chunking fix —
query-count chunking was 111s and would have been ~35 min at full scale.

`runs/t4/` is 1.1 GB, gitignored, and fully regenerable. Deleting `runs/t4/emb/` forces retraining;
deleting `runs/t4/repr/` forces a parser re-parse (~50s).
