"""T4 — does deck *diversity* buy anything beyond pair volume?

    uv run python -m mr.t4_scaling --preflight        # counts only, no GPU, no recall
    uv run python -m mr.t4_scaling --corpus cedh      # the grid
    uv run python -m mr.t4_scaling --merge

Representation is not the constraint. Whether *signal* is has not been tested, and the two halves
of that question decompose cleanly:

  **D4** (`t4_matched_data`) — decks fixed, examples 27x   -> pure **volume**:    +0.0128
  **this**                   — decks ~12.6x, examples ~1x  -> pure **diversity**: null 0.0000

The second corner exists only if the 250k positive cap binds at *both* levels — then both train on
the same number of examples while differing an order of magnitude in decks, training volume is held
constant by construction, and the null is exactly zero. That is the expectation, **not a
measurement**: casual mines 168,786 positives at 786 train decks and 450,038 at 3,142, so where the
cap starts binding is a property of the corpus and has to be checked. `--preflight` checks it, and
nothing downstream depends on the answer — `t4_scaling_prediction` takes the *realized* example
ratio, so a cap that does not bind simply yields a nonzero null and no rule changes.

**What this is not.** Where the cap binds equally the two levels draw 250k positives from
*different-sized pools*, so pair composition still shifts. That shift is the intervention, correctly
isolated — but it is diversity-of-pairs, not deck diversity holding the pairs themselves fixed.

## Why the pre-flight is a separate command

`THRESHOLDS.md` freezes the prediction *rule* before anything runs, and the point predictions after
the pre-flight measures the example ratio. That is only honest if the measuring pass cannot see an
outcome — so `--preflight` stops after layer 0 and reports deck, positive and example counts. It
trains nothing and can produce no recall number.

## Layout

    findings/t4_scaling_partial_<corpus>_<level>_<arm>_s<seed>.json
    findings/t4_scaling.{md,json}

Partials are deliberately NOT named `t4_partial_*`: `t4_representation.merge_partials` globs that
prefix, and a match would be ingested as a phantom arm, vanish from the ladder order and trip
`assert_arms_rendered` — halting the frozen report. Same reasoning as `t4_matched_data.partial_path`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import (config, corpus, loo_eval, report, representations,
               t4_representation as T, thresholds)

# Two orderings, deliberately separate, because they genuinely differ and collapsing them into one
# constant is what made the pre-flight try to build the subsample before its own reference existed.
LEVELS = ("subsample", "full")       # display: ascending in deck count, so the table reads as a curve
BUILD_ORDER = ("full", "subsample")  # build: `full` owns the strata and fingerprint `subsample` reads
DEFAULT_SEEDS = (42, 43, 44)


class ReportIncomplete(RuntimeError):
    """A scaling report was about to be written that omits a level which actually ran."""


# ── levels ────────────────────────────────────────────────────────────────────

def level_layer0(level: str, corpus_name: str, cards_df, repr_tables, root: Path,
                 seed: int, split_seed: int = config.SEED, force: bool = False,
                 subsample: int = thresholds.T4_SCALING_CEDH_SUBSAMPLE) -> dict:
    """Layer 0 for one level of the sweep.

    `full` is the reference: it owns the strata, the popularity baseline and the query fingerprint
    that `subsample` is checked against. Building it first is not an optimization, it is a
    precondition — `build_layer0` refuses a subsampled level without a reference precisely so the
    play-count buckets cannot be silently recomputed on the smaller training set.

    The subsample is redrawn per seed. A single fixed subsample would make "the low level" one
    particular draw of 3,142 decks, and its distance from the full corpus would be inseparable from
    which decks happened to be in it. Redrawing per seed puts that variance into the reported sd
    where it belongs.
    """
    ref_dir = T.layer0_dir(corpus_name, split_seed, root, corpus.MAX_COMMANDER_DISTINCT)
    if level == "full":
        return T.build_layer0(corpus_name, cards_df, repr_tables, split_seed, root, force=force)
    if level != "subsample":
        raise ValueError(f"unknown level {level!r}; expected one of {LEVELS}")

    l0 = T.build_layer0(corpus_name, cards_df, repr_tables, split_seed, root, force=force,
                        train_subsample=subsample, subsample_seed=seed, reference_dir=ref_dir)
    # A subsample at least as large as the training set is not a subsample: `subsample_train`
    # returns everything, and the two "levels" become the same level. Everything downstream then
    # works perfectly — identical fingerprints, an example ratio of exactly 1.0, a volume null of
    # exactly 0.0000 — and the report reads as a completed sweep of a corpus against itself. That
    # is indistinguishable from the real cap-binds-equally result, which is precisely why it has to
    # be an error and not a warning.
    available = l0["meta"]["n_train_available"]
    if subsample >= available:
        raise ValueError(
            f"subsample of {subsample:,} is not smaller than the {available:,} available train "
            f"decks on {corpus_name!r}, so both levels would be the full corpus. The sweep needs a "
            f"corpus with more train decks than T4_SCALING_CEDH_SUBSAMPLE ({subsample:,}) — cedh "
            f"has ~39,733, casual has {available:,}.")
    return l0


def partial_path(corpus_name: str, level: str, arm: str, seed: int, findings_dir: Path) -> Path:
    return (findings_dir /
            f"t4_scaling_partial_{corpus_name}_{level}_{arm.replace('+', 'plus')}_s{seed}.json")


# ── pre-flight ────────────────────────────────────────────────────────────────

def preflight(corpus_name: str, root: Path, seeds=DEFAULT_SEEDS, split_seed: int = config.SEED,
              force: bool = False) -> dict:
    """Counts only. Builds layer 0 at both levels and reports what training would consume.

    Produces no recall number and trains nothing, so freezing the point predictions against its
    output is still freezing them before any result exists. Recording `n_training_examples` rather
    than `n_positives` is the load-bearing part: `build_example_oids` drops a positive whose
    assigned *negative* lacks text under the arm, and conflating the two is what cost arm D two
    thirds of its data invisibly.
    """
    from . import finetune

    cards_df, repr_tables = corpus.load_cards_parquet(), representations.load()
    out: dict[str, dict] = {}
    for level in BUILD_ORDER:                  # full first — it is the subsample's reference
        for seed in (seeds if level == "subsample" else seeds[:1]):
            l0 = level_layer0(level, corpus_name, cards_df, repr_tables, root, seed,
                              split_seed=split_seed, force=force)
            row = {"level": level, "seed": seed,
                   "n_train_decks": l0["meta"]["n_train_decks"],
                   "n_train_available": l0["meta"]["n_train_available"],
                   "n_positives": l0["meta"]["n_positives"],
                   "n_ppmi_vocab": l0["meta"]["n_ppmi_vocab"],
                   "queries_sha256": l0["meta"]["queries_sha256"],
                   "n_queries": l0["meta"]["n_queries"],
                   "examples": {}}
            for arm in thresholds.T4_SCALING_ARMS:
                texts, _ = T.cached_texts(arm, l0["pool"], repr_tables, root)
                has_text = {o for o, t in texts.items() if t}
                row["examples"][arm] = len(
                    finetune.build_example_oids(l0["positives"], l0["negatives"], has_text))
            out[f"{level}_s{seed}"] = row
            print(f"  [{level} s{seed}] {row['n_train_decks']:,} train decks, "
                  f"{row['n_positives']:,} positives, examples "
                  + ", ".join(f"{a} {n:,}" for a, n in row["examples"].items()))
    return out


def cap_bound(rows: dict, max_positives: int = 250_000) -> dict[str, bool]:
    return {k: r["n_positives"] >= max_positives for k, r in rows.items()}


def example_ratio(rows: dict, arm: str) -> float:
    """Mean examples at `full` over mean examples at `subsample`, for one arm.

    Averaged over seeds because the subsample is redrawn per seed, so its example count is itself a
    random variable. This ratio is the only input the frozen prediction takes.
    """
    def mean_for(level: str) -> float:
        vals = [r["examples"][arm] for r in rows.values() if r["level"] == level]
        return float(np.mean(vals)) if vals else float("nan")
    return mean_for("full") / mean_for("subsample")


# ── the grid ──────────────────────────────────────────────────────────────────

def run(corpus_name: str, root: Path, findings_dir: Path, arms=thresholds.T4_SCALING_ARMS,
        seeds=DEFAULT_SEEDS, split_seed: int = config.SEED, force: bool = False,
        epochs: int = config.FINETUNE_EPOCHS, levels=LEVELS) -> None:
    """`levels` and `seeds` are the sharding knobs: 18 units split cleanly by (level, seed).

    A worker given `--levels subsample` needs the **full** level's layer 0 already on disk, because
    that is where its strata, popularity baseline and query fingerprint come from. `build_layer0`
    raises immediately when the reference is absent rather than deriving its own, so a mis-ordered
    shard costs a second instead of a night — but the warm-up step still has to run first.
    """
    cards_df, repr_tables = corpus.load_cards_parquet(), representations.load()
    for level in [lv for lv in BUILD_ORDER if lv in levels]:  # full first — the reference
        for seed in seeds:
            l0 = None
            for arm in arms:
                p = partial_path(corpus_name, level, arm, seed, findings_dir)
                if p.exists() and not force:
                    print(f"  [{level} {arm} s{seed}] skip — {p.name} exists")
                    continue
                if l0 is None:
                    l0 = level_layer0(level, corpus_name, cards_df, repr_tables, root, seed,
                                      split_seed=split_seed)
                rec = T.run_one(arm, seed, corpus_name, l0, cards_df, repr_tables, root,
                                epochs=epochs, force=force)
                rec["level"] = level
                rec["layer0_meta"] = l0["meta"]
                # Per-query hit/miss on the primary metric, kept so the paired bootstrap can run
                # later without retraining. Both levels share a byte-identical query list, so these
                # vectors are aligned by construction — that is what makes the bootstrap paired.
                rec["hits"] = per_query_hits(arm, seed, l0, root)
                p.write_text(json.dumps(rec, indent=2, sort_keys=True, default=str) + "\n",
                             encoding="utf-8")
                k = thresholds.T4_RECALL_KS[-1]
                print(f"  [{level} {arm} s{seed}] {rec['train_seconds']:.0f}s  "
                      f"recall@{k} {rec['full']['overall']['centroid'][f'recall_at_{k}']:.4f}  "
                      f"cold {rec['full']['coldstart_gated']['centroid'][f'recall_at_{k}']:.4f}")


def warm_caches(corpus_name: str, root: Path, seeds=DEFAULT_SEEDS, split_seed: int = config.SEED,
                arms=thresholds.T4_SCALING_ARMS) -> dict:
    """Build every shared cache serially, so parallel workers only ever read them.

    Three of them are written by whichever process arrives first and none of the writes is atomic
    across processes:

      layer 0, full        — also the *reference* every subsample level reads its strata and query
                             fingerprint from, so it must exist before any subsample shard starts
      layer 0, subsample   — one per seed, because the subsample is redrawn per seed
      layer 2, arm texts   — `runs/t4/texts/<arm>_<pin>.parquet`, one per arm

    Fanning six workers out against cold caches means six processes running the same PPMI and the
    same clause split, then racing to write the same files. Cheap to avoid, and the failure it
    avoids is a truncated parquet that reads back as a short card list — fewer texts, quietly
    fewer training examples, no error.
    """
    cards_df, repr_tables = corpus.load_cards_parquet(), representations.load()
    out = {}
    for level in BUILD_ORDER:                  # full first — it is the subsample's reference
        for seed in (seeds if level == "subsample" else seeds[:1]):
            l0 = level_layer0(level, corpus_name, cards_df, repr_tables, root, seed,
                              split_seed=split_seed)
            out[f"{level}_s{seed}"] = l0["meta"]["basis"]
            print(f"  layer0 {level} s{seed}: {l0['meta']['basis']}  "
                  f"{l0['meta']['n_train_decks']:,} train decks, "
                  f"{l0['meta']['n_positives']:,} positives")
            for arm in arms:
                T.cached_texts(arm, l0["pool"], repr_tables, root)
    print(f"\nwarm: {len(out)} layer-0 bases and {len(arms)} text caches ready. "
          "Workers may now run in parallel.")
    return out


def per_query_hits(arm: str, seed: int, l0: dict, root: Path) -> dict:
    """Per-query `recall@50` hit/miss under `centroid`, for the aggregate and the cold-start subset.

    Kept in the partial so the paired bootstrap can run at merge time without retraining, and stored
    as hit *indices* rather than dense 0/1 arrays because these files get read by hand — a 687k
    array of mostly zeros is unreadable and the hit set is a few percent of it.

    The cold-start entries are indexed **within the cold-start-filtered query list**, not within all
    queries. That is what makes the cold-start bootstrap possible from the partials alone: strata
    are frozen across levels, so both levels filter to the same queries in the same order and the
    two vectors are aligned by construction. Without this the cold-start row would have no interval,
    and the frozen rule would report UNDERPOWERED forever on the one stratum the mechanism claim is
    actually about.
    """
    emb = np.load(T.embedding_path(l0["meta"]["corpus"], arm, seed, l0, root))
    ranks = loo_eval.score_ranks(emb, l0["queries"], seed=seed)["centroid"]
    hit = ranks <= thresholds.T4_RECALL_KS[-1]
    cold = loo_eval.coldstart_mask(l0["queries"], l0["strata"])
    return {
        "n_queries": int(len(hit)),
        "hits": [int(i) for i in np.flatnonzero(hit)],
        "n_coldstart": int(cold.sum()),
        "coldstart_hits": [int(i) for i in np.flatnonzero(hit[cold])],
    }


# ── the paired bootstrap ──────────────────────────────────────────────────────

def paired_bootstrap(hits_high: np.ndarray, hits_low: np.ndarray, n_queries: int,
                     seed: int = config.SEED,
                     n_boot: int = thresholds.T4_SCALING_BOOTSTRAP_N,
                     ci: float = thresholds.T4_SCALING_CI) -> dict:
    """Resample *queries*, not seeds, and difference within each resampled query.

    Both levels are scored on the same query list in the same order, so `hits_high - hits_low` is a
    per-query paired difference and the resample preserves that pairing. Three seeds cannot carry a
    confidence interval; the queries can. Seeds keep their own separate job — the pooled-sd null
    rule — so neither statistic is standing in for the other.

    `hits_*` are seed-averaged per-query hit rates in [0, 1], so the returned interval is on the
    same scale as the reported recall difference.
    """
    d = np.asarray(hits_high, dtype=float) - np.asarray(hits_low, dtype=float)
    if len(d) != n_queries:
        raise ValueError(f"paired vectors are {len(d)} long but the query set is {n_queries} — "
                         "the levels were not scored on the same evaluation")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_queries, size=(n_boot, n_queries))
    means = d[idx].mean(axis=1)
    lo, hi = (1 - ci) / 2, 1 - (1 - ci) / 2
    return {"observed": float(d.mean()),
            "ci_low": float(np.quantile(means, lo)), "ci_high": float(np.quantile(means, hi)),
            "n_boot": n_boot, "ci": ci, "n_queries": n_queries}


HIT_KEYS = {"aggregate": ("hits", "n_queries"),
            "cold_start": ("coldstart_hits", "n_coldstart")}


def hit_rates(records: list[dict], level: str, arm: str, universe: str) -> np.ndarray:
    """Per-query hit rate averaged over seeds, for one (level, arm) and universe.

    Averaging over seeds before differencing, rather than differencing per seed, because the
    subsample is redrawn each seed — there is no meaningful pairing *between* a seed's subsample and
    a seed's full run. The pairing that matters is per query, and it holds for every seed.
    """
    idx_key, n_key = HIT_KEYS[universe]
    runs = [r for r in records if r["level"] == level and r["arm"] == arm and r.get("hits")]
    if not runs:
        return np.zeros(0)
    n = runs[0]["hits"][n_key]
    if any(r["hits"][n_key] != n for r in runs):
        raise ValueError(f"{universe} query count differs across seeds for {level}/{arm} — the "
                         "levels are not scored on the same evaluation")
    acc = np.zeros(n, dtype=float)
    for r in runs:
        acc[np.asarray(r["hits"][idx_key], dtype=np.int64)] += 1.0
    return acc / len(runs)


# ── aggregation ───────────────────────────────────────────────────────────────

def _vals(records: list[dict], level: str, arm: str, *path: str) -> list[float]:
    out = []
    for r in records:
        if r["level"] != level or r["arm"] != arm:
            continue
        node = r
        for key in path:
            node = None if not isinstance(node, dict) else node.get(key)
            if node is None:
                break
        if node is not None:
            out.append(float(node))
    return out


AGG = ("full", "overall", "centroid", "recall_at_50")
COLD = ("full", "coldstart_gated", "centroid", "recall_at_50")
UNIVERSES = (("aggregate", AGG), ("cold_start", COLD))


def prediction_for(ratio: float) -> float:
    """The frozen prediction at a realized example ratio. `nan` in, `nan` out — never a silent 0.

    A missing ratio must not render as "the volume null is zero": that is the *finding* when the cap
    binds equally, and manufacturing it from absent data would be indistinguishable from the real
    thing in the report.
    """
    if ratio != ratio or ratio <= 0:
        return float("nan")
    return thresholds.t4_scaling_prediction(1.0, ratio)


def analyse(records: list[dict], preflight_rows: dict | None) -> dict:
    """Per arm and universe: observed gap, frozen prediction, CI, seed sd, verdict."""
    out: dict[str, dict] = {}
    for arm in sorted({r["arm"] for r in records}):
        ratio = (example_ratio(preflight_rows, arm) if preflight_rows else
                 _ratio_from_records(records, arm))
        predicted = prediction_for(ratio)
        arm_out = {"example_ratio": ratio, "predicted": predicted, "universes": {}}
        for name, path in UNIVERSES:
            hi, lo = _vals(records, "full", arm, *path), _vals(records, "subsample", arm, *path)
            if not hi or not lo:
                continue
            hi_a, lo_a = np.asarray(hi), np.asarray(lo)
            pooled = thresholds.pooled_sd(
                float(hi_a.std(ddof=1)) if len(hi_a) > 1 else 0.0, len(hi_a),
                float(lo_a.std(ddof=1)) if len(lo_a) > 1 else 0.0, len(lo_a))
            boot = _bootstrap_for(records, arm, name)
            observed = float(hi_a.mean() - lo_a.mean())
            arm_out["universes"][name] = {
                "full": {"mean": float(hi_a.mean()),
                         "sd": float(hi_a.std(ddof=1)) if len(hi_a) > 1 else 0.0,
                         "n_seeds": len(hi_a)},
                "subsample": {"mean": float(lo_a.mean()),
                              "sd": float(lo_a.std(ddof=1)) if len(lo_a) > 1 else 0.0,
                              "n_seeds": len(lo_a)},
                "observed": observed, "predicted": predicted,
                "excess": observed - predicted, "pooled_sd": pooled,
                "bootstrap": boot,
                "verdict": thresholds.t4_scaling_verdict(
                    observed, predicted,
                    boot["ci_low"] if boot else float("nan"),
                    boot["ci_high"] if boot else float("nan"),
                    pooled),
            }
        out[arm] = arm_out
    return out


def _ratio_from_records(records: list[dict], arm: str) -> float:
    """Fall back to the example counts each partial recorded, when no pre-flight is at hand."""
    def mean_for(level):
        v = [r["n_training_examples"] for r in records
             if r["level"] == level and r["arm"] == arm and r.get("n_training_examples")]
        return float(np.mean(v)) if v else float("nan")
    return mean_for("full") / mean_for("subsample")


def _bootstrap_for(records: list[dict], arm: str, universe: str) -> dict | None:
    """The paired query bootstrap, for either universe. `None` when the partials cannot support it.

    Returning None rather than a degenerate interval is deliberate: `t4_scaling_verdict` treats an
    absent CI as UNDERPOWERED, so a missing bootstrap can never let a verdict through on two of the
    three frozen conditions.
    """
    arm_recs = [r for r in records if r["arm"] == arm]
    if not arm_recs or not all(r.get("hits") for r in arm_recs):
        return None
    hi = hit_rates(records, "full", arm, universe)
    lo = hit_rates(records, "subsample", arm, universe)
    if not len(hi) or not len(lo) or len(hi) != len(lo):
        return None
    return paired_bootstrap(hi, lo, len(hi))


# ── report ────────────────────────────────────────────────────────────────────

def assert_levels_rendered(records: list[dict], rows: str) -> None:
    """Every (level, arm) that produced a result must appear in the counts table.

    On the rendered text, not the intermediate dict, for the reason `assert_arms_rendered` gives:
    a report that reads as complete while silently dropping a level is a scaling curve with a point
    missing, and nothing about it looks wrong.
    """
    ran = {(r["level"], r["arm"]) for r in records if not r.get("skipped")}
    missing = sorted(k for k in ran if f"| {k[0]} | {k[1]} |" not in rows)
    if missing:
        raise ReportIncomplete(
            f"{missing} produced results but have no row in the levels table. Refusing to write a "
            f"scaling report that reads as complete. Present: {sorted(ran)}.")


def render(records: list[dict], res: dict, pre: dict | None, meta: dict, corpus_name: str) -> str:
    k = thresholds.T4_RECALL_KS[-1]
    caps = cap_bound(pre) if pre else {}
    all_bound = bool(caps) and all(caps.values())

    lines = []
    for level in LEVELS:
        for arm in sorted({r["arm"] for r in records}):
            runs = [r for r in records if r["level"] == level and r["arm"] == arm]
            if not runs:
                continue
            decks = int(np.mean([r["layer0_meta"]["n_train_decks"] for r in runs]))
            pos = int(np.mean([r["layer0_meta"]["n_positives"] for r in runs]))
            ex = int(np.mean([r["n_training_examples"] for r in runs]))
            agg = np.mean(_vals(records, level, arm, *AGG))
            cold = np.mean(_vals(records, level, arm, *COLD))
            lines.append(f"| {level} | {arm} | {len(runs)} | {decks:,} | {pos:,} | {ex:,} "
                         f"| {agg:.4f} | {cold:.4f} |")
    rows = "\n".join(lines)
    assert_levels_rendered(records, rows)

    vlines = []
    for arm, a in sorted(res.items()):
        for name, u in a["universes"].items():
            b = u["bootstrap"]
            ci = f"[{b['ci_low']:+.4f}, {b['ci_high']:+.4f}]" if b else "—"
            vlines.append(f"| {arm} | {name} | {u['observed']:+.4f} | {u['predicted']:+.4f} "
                          f"| {u['excess']:+.4f} | {ci} | {u['pooled_sd']:.4f} "
                          f"| **{u['verdict']}** |")
    verdict_rows = "\n".join(vlines)

    ratios = ", ".join(f"{arm} {a['example_ratio']:.2f}x" for arm, a in sorted(res.items()))
    deck_ratio = (meta.get("n_train_available", 0) / thresholds.T4_SCALING_CEDH_SUBSAMPLE
                  if meta.get("n_train_available") else float("nan"))

    cap_para = (
        f"**The 250k positive cap binds at every level**, so training volume is held constant by "
        f"construction and the pure-volume null is exactly +0.0000. The comparison is a clean "
        f"diversity isolation." if all_bound else
        f"**The positive cap does not bind at every level** ({caps}), so the levels differ in "
        f"training volume as well as decks and the prediction below is correspondingly nonzero. "
        f"The frozen rule handles this without amendment — it was written as a function of the "
        f"realized example ratio precisely so that this case needed no new decision.")

    return f"""# T4 — deck diversity beyond pair volume

{report.confound_header()}
Corpus **{corpus_name}**. Two levels differing ~{deck_ratio:.1f}x in training decks
({thresholds.T4_SCALING_CEDH_SUBSAMPLE:,} subsampled, redrawn per seed, against
{meta.get('n_train_available', 0):,} full), scored on a **byte-identical** test set of
{meta.get('n_queries', 0):,} queries — asserted by fingerprint in layer 0, not assumed.

**Pre-registered.** The prediction rule, the magnitude gate and the decision rule were frozen in
`THRESHOLDS.md` before the pre-flight, and the point predictions before any model trained. The
naive expectation that deck count carries pair count — which would have predicted ~+0.0104 here —
is recorded there as corrected, not as a second null to choose between afterwards.

{cap_para}

Realized training-example ratios: {ratios}.

## Levels

| level | arm | seeds | train decks | positives | training examples | recall@{k} | cold-start |
|---|---|---|---|---|---|---|---|
{rows}

`training examples` is what training actually consumed, not `n_positives` and not
`n_trainable_positives` — `build_example_oids` drops a positive whose assigned *negative* lacks
text under the arm, and the frozen prediction is a function of this column.

## Verdicts

| arm | universe | observed | volume null | excess | 95% CI | pooled seed sd | verdict |
|---|---|---|---|---|---|---|---|
{verdict_rows}

**DIVERSITY_BEYOND_VOLUME** requires all three: excess ≥ {thresholds.T4_SCALING_MIN_LIFT:+.3f}, a
paired query-bootstrap CI excluding the volume null, and a gap exceeding the pooled seed sd.
**VOLUME_PROXY** means the interval contains the null — distinct contexts buy nothing that pair
count did not already buy. **BELOW_VOLUME_NULL** means the interval sits entirely below it.

The CI is over **queries**, which both levels share exactly; the seed sd is a separate check and
carries T4's standing null rule unchanged. A row with no interval reports **UNDERPOWERED**
regardless of its magnitude — a verdict resting on two of the three frozen conditions is not the
frozen rule.

The cold-start CI is bootstrapped over the cold-start queries only, which is possible because the
strata are frozen at the full level: both levels filter to the same queries in the same order, so
the two hit vectors are paired by construction rather than by coincidence.

## What this does not license

Where the cap binds equally, the two levels draw {min(r['layer0_meta']['n_positives'] for r in records):,}
positives from **different-sized pools**, so pair composition shifts even at constant count. That
shift is the diversity intervention, correctly isolated — but it is diversity *of pairs*, not deck
diversity holding the pairs themselves fixed. The distinction is not rhetorical: it is the
difference between "more decks give better pairs" and "more decks give more contexts per card".

cedh's played vocabulary is far narrower than the {meta.get('n_pool', 0):,}-card retrieval pool, so
absolute recall here is not comparable to casual's.
"""


def merge(findings_dir: Path, corpus_name: str, root: Path | None = None) -> dict:
    records, seen = [], {}
    for p in sorted(findings_dir.glob("t4_scaling_partial_*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if rec.get("corpus") != corpus_name:
            continue
        key = (rec["level"], rec["arm"], rec["seed"])
        if key in seen:
            raise ValueError(f"duplicate run {key} in {p.name} and {seen[key]}")
        seen[key] = p.name
        records.append(rec)
    if not records:
        raise ValueError(f"no t4_scaling_partial_*.json for corpus {corpus_name!r} in {findings_dir}")

    pre_path = findings_dir / f"t4_scaling_preflight_{corpus_name}.json"
    pre = json.loads(pre_path.read_text(encoding="utf-8")) if pre_path.exists() else None
    res = analyse(records, pre)
    meta = next((r["layer0_meta"] for r in records if r["level"] == "full"), records[0]["layer0_meta"])

    payload = report.envelope([config.CARDS_PARQUET],
                              {"merged_from": sorted(seen.values()), "corpus": corpus_name})
    payload.update({"records": records, "analysis": res, "preflight": pre,
                    "layer0_meta": meta, "corpus": corpus_name})
    report.write("t4_scaling", payload, render(records, res, pre, meta, corpus_name),
                 findings_dir=findings_dir)
    return payload


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default="cedh", choices=("casual", "cedh"))
    ap.add_argument("--arms", nargs="+", default=list(thresholds.T4_SCALING_ARMS))
    ap.add_argument("--seeds", nargs="+", type=int, default=list(DEFAULT_SEEDS))
    ap.add_argument("--levels", nargs="+", default=list(LEVELS), choices=list(LEVELS))
    ap.add_argument("--split-seed", type=int, default=config.SEED)
    ap.add_argument("--epochs", type=int, default=config.FINETUNE_EPOCHS)
    ap.add_argument("--preflight", action="store_true",
                    help="build layer 0 at both levels and report counts only — no training, "
                         "no recall number, safe to run before the predictions are frozen")
    ap.add_argument("--warm", action="store_true",
                    help="build every layer 0 and arm text cache serially, then exit. Run this "
                         "ONCE before fanning workers out: the caches are written by whichever "
                         "process gets there first, and N workers racing to create the same file "
                         "is how a torn parquet happens")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--no-merge", action="store_true",
                    help="for shard workers: write partials and stop. Merging needs every shard's "
                         "partials, so only the last machine standing should do it")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    root, findings_dir = config.RUNS_DIR / "t4", config.FINDINGS_DIR
    findings_dir.mkdir(parents=True, exist_ok=True)

    if args.preflight:
        rows = preflight(args.corpus, root, seeds=tuple(args.seeds),
                         split_seed=args.split_seed, force=args.force)
        (findings_dir / f"t4_scaling_preflight_{args.corpus}.json").write_text(
            json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        caps = cap_bound(rows)
        print(f"\ncap binds: {caps}")
        for arm in thresholds.T4_SCALING_ARMS:
            ratio = example_ratio(rows, arm)
            print(f"  {arm}: example ratio {ratio:.4f}x  ->  frozen prediction "
                  f"{thresholds.t4_scaling_prediction(1_000_000, int(round(1_000_000 * ratio))):+.4f}")
        print("\nCounts only — no model trained, no recall number produced. "
              "Freeze the point predictions from these ratios before running the grid.")
        return

    if args.warm:
        warm_caches(args.corpus, root, seeds=tuple(args.seeds), split_seed=args.split_seed,
                    arms=tuple(args.arms))
        return

    if not args.merge:
        run(args.corpus, root, findings_dir, arms=tuple(args.arms), seeds=tuple(args.seeds),
            split_seed=args.split_seed, force=args.force, epochs=args.epochs,
            levels=tuple(args.levels))
    if args.no_merge:
        return
    merge(findings_dir, args.corpus, root)


if __name__ == "__main__":
    main()
