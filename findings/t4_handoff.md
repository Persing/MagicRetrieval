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

**Phase 4 — cedh transfer. This is now the whole scaling test.** A/B+/C × 3 seeds at full size
(46,745 decks, ~39,733 train) *and* a 3,142-deck **train** subsample **resampled per seed** (a fixed
subsample conflates subsample identity with corpus effect). It **ran on 2026-07-30 and is 15–18/18
complete but unmerged** — see "cedh run state" below before touching it.

Non-negotiables, all enforced in code rather than by discipline: the subsample is applied **after**
`split_by_deck`; the test set is asserted byte-identical by fingerprint (all four cedh levels hashed
`3fe8406f6b91e0ed`); strata and the popularity baseline load from the full level so the cold-start
bucket holds the same cards at both levels; the layer-0 cache key and the embedding path both carry
the subsample.

cedh needs no deck filter (p50 98, max 99) — a useful control on the filter itself. Its played
vocabulary is far narrower than the 30,958-card retrieval pool: **PPMI vocab 10,133 against casual's
20,531**.

**Phase 5 — seeds 45/46. THE NEXT THING TO DO, and it needs no pod.** See the dedicated section
below.

**Phase 6 — `findings/t4_conclusion.md`. Done, and it is a renderer** (`mr.t4_conclusion`), not a
hand-written file: every number is read from the frozen findings JSONs, so Phase 5 and Phase 4
refresh it by re-running rather than by anyone remembering to. Two guards, both on rendered text —
an incomplete ladder must carry the INTERIM banner, and each closed question must appear with its
figure.

---

## Phase 5 — start here. Self-contained, local, no pod.

**What it is.** Casual seeds 45/46 across all six arms, completing the frozen n=5. This is the
highest-value work outstanding: it removes the INTERIM banner from `t4_representation.md`, which is
the ladder every other document cites. Nothing about it depends on cedh.

**Where it runs: the local 4090, and nowhere else.** Seeds 45/46 join a pooled seed sd computed from
42/43/44 on that card, and that sd is the denominator of the null rule for every verdict in the
ladder. Splitting seeds across hardware injects a term no seed controls. This is not a preference.

**Sustained local load is capped at ~1–2h** (breaker, shared with AC), so run it in chunks. No code
change needed: `main` skips any (arm, seed) whose partial already exists and layer 0 is cached, so
the grid resumes for free. Longest single unit is B+ at ~29 min.

```bash
uv run python -m mr.t4_representation --corpus casual --seeds 45 --arms A B B_type D   # ~40 min
uv run python -m mr.t4_representation --corpus casual --seeds 45 --arms B+             # ~29 min
uv run python -m mr.t4_representation --corpus casual --seeds 45 --arms C              # ~29 min
#   ... then repeat all three for --seeds 46
uv run python -m mr.t4_representation --merge          # casual-scoped; writes t4_representation.*
uv run python -m mr.t4_conclusion                      # re-renders the conclusion from the findings
```

**How to know it worked.** `t4_representation.md` loses the INTERIM banner and the payload reports
`is_final: true`. Until *every* arm reaches 5 seeds the banner stays and reports the **least**
complete arm — that is deliberate (`completed_seeds` uses `min`, not `max`), so a half-finished seed
cannot read as a finished ladder. Expect the banner to say "4 of 5" for a while mid-way through
seed 46; that is correct, not a bug.

**Do not** run anything else on the GPU during a chunk — early timings were inflated up to 3× by
competing processes.

---

## cedh run state — 2026-07-30, incomplete and partially stranded

**What happened.** The Phase 4 grid ran on a RunPod 3×RTX 5090 secure pod (`b45vlg6pu83rz7`). It got
through the pre-flight, the pre-registration gate and **at least 15 of 18 partials** — all nine
full-level, plus subsample A and B+ at all three seeds — with subsample C mid-run. RunPod then
auto-stopped the pod on a low account balance. On restart it returned *"not enough free GPUs on the
host machine"*: the pod's volume lives on that host's local disk, and the host had reallocated the
GPUs. **The partials are intact on that volume but unreachable until the host frees capacity.**

**Design lesson, for any re-run: use a RunPod _network_ volume, not a pod-local one.** A network
volume detaches from the host, so a stop, a low balance or a full host cannot strand the results. A
pod-local volume makes every stop a gamble on getting back onto the same machine.

**Results already established** (means ± sd over 3 seeds, `centroid`, recall@50). Provisional: these
came from reading partials directly, not from the merged report.

| level | arm | aggregate | cold-start | max_sim |
|---|---|---|---|---|
| full | A | 0.0099 ± 0.0043 | **0.0783 ± 0.0012** | 0.1340 |
| full | B+ | 0.0070 ± 0.0008 | **0.0809 ± 0.0023** | 0.1479 |
| full | C | 0.0083 ± 0.0043 | 0.0699 ± 0.0029 | 0.1216 |
| subsample | A | 0.0077 ± 0.0038 | **0.0374 ± 0.0104** | 0.1402 |
| subsample | B+ | 0.0108 ± 0.0107 | **0.0369 ± 0.0184** | 0.1670 |

Cold-start gaps: **A +0.0409, B+ +0.0440**, against the frozen volume null of **+0.0042** — roughly
tenfold, far past the +0.005 magnitude gate and well outside the pooled seed sd. The aggregate is
null and noisy (A +0.0022, B+ −0.0038). 675,233 queries; cold-start stratum only 2,222 of them
(0.33%, against casual's 14.7%) because 39,733 train decks leave few cards rare. Popularity scores
**0.6912** aggregate and **0.0000** cold-start — same structure as casual, more extreme.

**What is missing and why it matters.** The paired query bootstrap needs the per-query `hits`
vectors stored inside each partial. Without them `t4_scaling_verdict` returns **UNDERPOWERED** by
design rather than passing a verdict on two of the three frozen conditions. So the headline above is
a point estimate with no interval, and must be reported that way until the merge runs.

**Two interpretation caveats that must reach the write-up.**

1. *The cold-start stratum is frozen at full-level counts* — ≤5 appearances among 39,733 decks. In a
   3,142-deck subsample those same cards have ~0.4 expected appearances, so most are not merely rare
   at the low level, they are **absent**. Part of +0.041 is therefore "a few exposures versus none",
   which is neither diversity nor something the pair-count null models. Get the per-level appearance
   distribution before claiming diversity.
2. *`max_sim` runs the opposite direction* — subsample beats full for both arms (A 0.1402 vs 0.1340;
   B+ 0.1670 vs 0.1479). Both aggregators were frozen up front precisely so this is reported rather
   than chosen between.

**To finish it.** Either recover the pod (retry `start-pod`; reducing it to 1 GPU in the console
makes the host far likelier to fit it — `02_grid.sh` now clamps `NGPU` to the visible device count,
and the pod re-syncs the repo on start, so a resized pod works), or re-run. A re-run costs ~$4.65 on
a **single** 5090 secure over ~4.7h and does not compromise anything: the frozen prediction is
already committed, and the pre-flight is deterministic, so the same counts (194,312 / 65,425, ratio
2.9700, prediction +0.0042) reproduce exactly. Skip the replicate floor — already measured at
delta ~3e-06, three orders of magnitude below the +0.005 gate.

Once partials are in hand, **the merge is free and local**: `uv run python -m mr.t4_scaling --merge
--corpus cedh` reads `findings/` and the bootstrap is pure numpy. No GPU required.

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

**More than one visible GPU silently changes the recipe.** `transformers.Trainer` wraps the model in
DataParallel and multiplies `per_device_train_batch_size` by the device count, so a 3-GPU box trains
at an effective batch of 96 against the frozen 32 — and under `MultipleNegativesRankingLoss` the
batch *is* the negative sampling. It also breaks the schedule: `num_train_steps` is computed as
`len(dataset) // 32`, so WarmupLinear decays against a horizon 3× too long and the LR never reaches
zero (observed: 4,048 steps against an assumed 12,144, final LR 1.4e-05). `finetune` now **raises**
rather than setting `CUDA_VISIBLE_DEVICES` internally, which would be a no-op after torch has
initialized. Callers pin the device. DataParallel was also barely faster — 12.5 min on 3 GPUs vs
16 min on 1 — because gather/scatter dominates for a 22M-param model.

**The parser pin used to hash checkout bytes, not parser content.** Fixed 2026-07-30; see
Provenance. If a pin mismatch ever appears again, check line endings before believing the parser
changed.

**`git pull ... || true` in an automation script runs stale code silently.** The pod ran an old
`01_preflight.sh` for a full cycle because a pull failed on an untracked-file collision and the
`|| true` swallowed it. Use `git fetch && git reset --hard`, and make a sync failure fatal.

**Do not gate on file mtimes.** The first pre-registration gate required `THRESHOLDS.md` to be newer
than the pre-flight output. A fresh `git clone` writes everything at the same instant, so a
correctly pre-registered run would have been blocked — and the natural response is to reach for the
override, after which the gate means nothing. It now checks *content*: recompute the ratio from the
pre-flight JSON and require that ratio and its implied prediction to appear in `THRESHOLDS.md`.

**A RunPod start command replaces the image entrypoint**, so `sshd` never starts and `$PUBLIC_KEY`
is never written to `authorized_keys` — SSH refuses even with the key added in the console. If you
override the start command, start sshd yourself and pass `PUBLIC_KEY` in the pod env. The TCP port
also remaps on every restart; re-read it from `get-pod` rather than reusing the old one.

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
