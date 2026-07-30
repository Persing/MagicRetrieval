# Phase 4 on rented GPUs — runbook

Phase 4 is the cedh scaling test: **A / B+ / C × 3 seeds × 2 levels = 18 independent training
runs**, ~7.1 h of GPU serially. It moves off the local 4090 for **power**, not speed — sustained
local load is capped at ~1–2 h by the breaker. Six GPUs turn it into ~1.2 h drawing nothing at home.

**Phase 5 (casual seeds 45/46) does NOT move.** Those seeds join a pooled seed sd computed from
42/43/44 on the local card, and that sd is the denominator of the null rule for every verdict in the
ladder. Run Phase 5 locally in ~30-minute chunks. Only cedh — a fresh ladder with no shared sd —
goes to rented hardware.

---

## 1. What to rent

**One multi-GPU pod, not N single-GPU pods.** The 18 units share a layer-0 cache that takes ~30 min
to build and whose `queries.npz` is a few hundred MB. On one pod that is a local directory; across N
pods it is a transfer problem plus a risk that two machines build subtly different bases.

| | |
|---|---|
| GPU | **RTX 5090 × 3**, or **A40 × 2** to minimise spend. See the table below |
| System RAM | **≥ 32 GB** — `build_queries` holds ~687k context arrays for cedh. The GPU is not the constraint |
| Container disk | 30 GB |
| Volume disk | **50 GB at `/workspace`** — everything durable goes here, see §2 |
| Image | any recent PyTorch/CUDA base. Its bundled torch goes unused: `uv sync --frozen` builds an isolated venv with its own torch and `nvidia-*` runtime, which is what makes workers byte-identical |
| Ports | TCP **22** for SSH. Jupyter (8888) is not needed |

**Pick on fp32 throughput, not VRAM or tensor cores.** Two frozen decisions in `finetune.py` decide
this: `fp16=False`, so the tensor-core specs in the marketing table do not apply; and batch size 32,
which cannot be raised because under `MultipleNegativesRankingLoss` the batch *is* the negative
sampling. A 22M-parameter 6-layer model at batch 32 is partly clock-bound, so even fp32 TFLOPS
overstates the spread. Peak VRAM is ~4 GB — 3–4 GB training (512-token attention matrices dominate)
and 2.03 GB for the eval block `sims = E[flat] @ E.T` at 16,384 × 30,958 × f32. Every card below
clears it several times over.

| card | $/hr | fp32 | max | TFLOPS/$ | est. shard (vs 71 min on a local 4090) |
|---|---|---|---|---|---|
| **RTX 5090** | 0.99 | ~104 | 8 | **105** | ~65 min |
| **A40** | **0.44** | 37.4 | 10 | 85 | ~130 min |
| L40S | 0.99 | 91.6 | 7 | 93 | ~70 min |
| RTX A6000 | 0.53 | 38.7 | 7 | 73 | ~130 min |
| RTX 4090 | 0.69 | 82.6 | **1 max** | 120 | ~71 min |
| H100 PCIe | 2.89 | 51.2 | 3 | 18 | ~110 min |

**Do not rent an H100/B200/H200**, whatever the "Recommended" tab says — those are the template's
compatibility filters, not this workload's. On fp32 an H100 PCIe is *slower than a 4090* at 4× the
price. Clear the filter banner to see consumer cards at all.

The 4090 is the most efficient card here but capped at **1 max**, so matching the local card is not
available for a multi-GPU plan. That is fine: §5 measures the replicate floor on the pod, so the
pod's own noise floor is what the +0.005 gate gets read against. The hard requirement is only that
**all 18 runs use the same card model**, since they share one ladder's seed sd.

**Estimated totals** (warm-up + replicate floor + `ceil(6/NGPU)` waves):

| config | wall clock | cost |
|---|---|---|
| A40 × 2 | ~7.6 h | **~$6.70** |
| RTX 5090 × 3 | ~3.3 h | ~$9.80 |
| A40 × 4 | ~5.8 h | ~$10.20 |
| RTX 5090 × 6 | ~2.2 h | ~$13.00 |

More GPUs stop paying because §4's warm-up is single-threaded and bills every card you rented.
Workers are restartable, so a preempted pod costs one unit, not the run.

**Before committing to a wave plan, let §5 tell you the real number.** The replicate floor trains
arm A twice — 724 s per run on a local 4090. Whatever it takes on the pod is the measured scaling
factor for that card, and the shard estimates above are only estimates.

## 2. Bootstrap — everything durable on `/workspace`

Container disk is erased when the pod is **stopped**; the volume survives until it is
**terminated**. Putting the repo or `runs/` on container disk means a stop throws away the 30–45 min
cedh layer-0 build. Set these two as template environment variables so they survive a restart:

```
UV_CACHE_DIR=/workspace/.uv-cache
HF_HOME=/workspace/.hf
```

```bash
cd /workspace
git clone https://github.com/Persing/MagicRetrieval.git && cd MagicRetrieval
git checkout t4-representation-ablation
curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="$HOME/.local/bin:$PATH"
uv sync --frozen --extra dev
```

Expect ~15–20 GB on the volume: venv ~8 GB, uv cache ~5 GB, cedh layer 0 ~1–1.5 GB across four
bases, 18 embedding matrices ~856 MB (30,958 × 384 × f32 each), HF model cache ~0.2 GB.

`--frozen` is not optional: `uv.lock` resolves Linux to `torch 2.13.0` from PyPI with 43 pinned
`nvidia-*` packages, so every pod gets byte-identical dependencies. Without it a resolver drift
between workers becomes a hardware-noise term inside one ladder.

No data transfer is needed. All seven MagicSpike inputs (75 MB) and the full CDL parser are plain
git-tracked — a clone is the whole input set.

## 3. Verification gate — before any compute

```bash
uv run pytest
```

Expect **205 passed, 5 deselected**. Then the parser identity, which is what makes results on this
machine comparable to the local findings:

```bash
uv run python -c "from mr import representations as R; p=R.load()['pin']; print(p['combined_sha256'])"
```

Must print exactly:

```
2f61bf8d0f71a57303b2256a17304f95ee13fc912ce0018de4c553a3dd4537e0
```

That pin covers **uncommitted-upstream `tag_extractor.py` changes that were deliberately vendored**
(compound `target_type`, 20.9% of emissions changed shape). The git rev alone does not identify it —
the per-module sha256 does. A mismatch means the layer-1 clause cache would differ and every arm's
text with it; stop and reconcile rather than running.

```bash
uv run python -c "import torch; print(torch.cuda.device_count(), torch.version.cuda, torch.cuda.get_device_name(0))"
```

## 4. Warm the shared caches — serially, once

```bash
uv run python -m mr.t4_scaling --warm --corpus cedh
```

Builds, in order: the **full** cedh layer 0 (~39,733 train decks — PPMI plus hard-negative mining,
the expensive step), then the three per-seed **subsample** layer 0s, then the A / B+ / C text caches.
~30–45 min, CPU-bound.

This step is mandatory and it is why `--warm` exists. Six workers starting against cold caches would
all run the same PPMI and the same clause split, then race to write the same files. The writes are
atomic now (`report.atomic_write_*`), so a race can no longer corrupt anything — but a torn arm-text
parquet was the bad case: it reads back as a *shorter card list*, so the arm silently trains on fewer
examples with nothing reporting an error.

`full` must exist before any `subsample` work: the subsample borrows the full level's strata,
popularity baseline and query fingerprint, and `build_layer0` refuses to derive its own. It raises in
about a second if the reference is missing, so a mis-ordered shard is cheap to discover.

## 5. Measure the replicate floor on THIS stack

```bash
uv run python -m mr.t4_representation --replicate A --corpus cedh --seeds 42
```

~25 min, writing `findings/t4_replicate_floor_cedh.{md,json}` — corpus-suffixed so it cannot land on
top of a casual measurement. Trains arm A twice at the same seed and records the gap: the variance
the seed does not control (cuDNN/cuBLAS reduction order). `torch.use_deterministic_algorithms(True)`
is deliberately not set, so this is measured rather than removed.

Do this because the pod's torch is the **PyPI CUDA build** while local is `2.13.0+cu130`. Same
version, different kernels. The locally measured floor describes a different stack and does not
transfer. Read the scaling magnitude gate (+0.005) against the number this prints: an effect near the
floor is kernel noise regardless of what any seed sd says.

## 6. Freeze the point predictions — commit 2

```bash
uv run python -m mr.t4_scaling --preflight --corpus cedh
```

Fast now that §4 warmed the caches. Prints per level: train decks, positives, **training examples**
per arm, whether the 250k cap bound, and the resulting frozen prediction. **No model is trained and
no recall number is produced or producible** — which is what makes freezing predictions against its
output still a pre-registration.

Then write the measured ratios and their point predictions into `THRESHOLDS.md`, commit them alone,
and say in the message that only counts were observed. The rule they instantiate is already frozen
under "T4 scaling — does deck diversity buy anything beyond pair volume?".

Expected: the cap binds at both levels, the example ratio is ~1.0 and the volume null is exactly
0.0000. **Expected, not measured** — casual mines 168,786 positives at 786 train decks and 450,038 at
3,142, so where the cap starts binding is a property of the corpus. If it does not bind, nothing
changes: the frozen rule takes the *realized* ratio, so the null simply stops being zero.

## 7. Fan out — 6 shards across N GPUs

18 units split cleanly by (level, seed) into 6 shards of 3 arms each, ~71 min per shard
(A 724 s + B+ 1,764 s + C 1,749 s). Set `NGPU` to what you actually rented:

```bash
mkdir -p logs
NGPU=2                       # <-- your GPU count
i=0
for level in full subsample; do
  for seed in 42 43 44; do
    CUDA_VISIBLE_DEVICES=$(( i % NGPU )) MR_HF_DIR=/workspace/hf/w$i \
      uv run python -m mr.t4_scaling --corpus cedh \
        --levels $level --seeds $seed --no-merge > logs/w$i.log 2>&1 &
    i=$(( i + 1 ))
    if [ $(( i % NGPU )) -eq 0 ]; then wait; fi    # finish the wave before starting the next
  done
done
wait
```

Strict waves of `NGPU` rather than a job-slot queue, deliberately: it makes a GPU collision
impossible without any bookkeeping, and it costs nothing here because all six shards are the same
three arms and therefore the same length. `ceil(6 / NGPU)` waves — 3 at `NGPU=2`, 2 at `NGPU=3`,
1 at `NGPU=6`.

`MR_HF_DIR` gives each worker its own HF Trainer scratch directory. `--no-merge` matters: merging
needs every shard's partials, so workers must not each write a partial report.

Workers are restartable — a completed `(level, arm, seed)` is skipped on the strength of its partial
existing, and layer 0 is cached. A preempted pod loses at most one unit. Watch with
`tail -f logs/w*.log`.

## 8. Merge and collect

```bash
uv run python -m mr.t4_scaling --merge --corpus cedh
tar czf t4_scaling_results.tgz findings/t4_scaling*.json findings/t4_scaling.md \
                               findings/t4_replicate_floor_cedh.*
```

Read `findings/t4_scaling.md` before pulling it down — both levels must have a row for every arm
(`assert_levels_rendered` raises otherwise), and each verdict row must show observed, the volume
null, the CI and the seed sd.

Bring the tarball back with `runpodctl send`, the web file browser, or scp — whichever you normally
use. **I can't enter credentials or tokens on your behalf**, so pushing from the pod with a PAT is
something you'd set up yourself; the tarball route avoids it entirely.

Then locally: unpack into `findings/`, and re-render so the conclusion picks up the new evidence.

```bash
uv run python -m mr.t4_conclusion
```

## 9. Do not do on the pod

- **Phase 5 / anything casual.** Seeds 45/46 must share hardware with 42/43/44 — see the top of this
  file. The pod's different CUDA build would inject a hardware term into the pooled seed sd that
  every ladder verdict is measured against.
- **`--force`**, unless you mean to discard and retrain. The skip-if-partial-exists behaviour is what
  makes preempted workers cheap.
- **Anything else on the GPUs during a shard.** Early local timings were inflated up to 3× by
  competing processes and produced a badly wrong cost estimate.

## Cost and timing

| step | wall clock | note |
|---|---|---|
| bootstrap + verification gate | ~10 min | mostly `uv sync` |
| warm caches (§4) | ~30–45 min | serial and CPU-bound — every GPU is idle-billed through it |
| replicate floor (§5) | ~25 min on a 4090-class card | one GPU, after §4; also your speed measurement |
| pre-flight (§6) | ~2 min | cache hits |
| 18 units, `ceil(6/NGPU)` waves (§7) | one shard per wave | shard time is card-dependent — see §1 |
| merge + collect (§8) | ~5 min | |

Totals are in §1. The warm-up is why more GPUs stop paying: single-threaded, and it bills every card
you rented. Timings assume nothing else is on the GPUs — early local measurements were inflated up
to 3× by competing processes and produced a badly wrong cost estimate.
