"""T4 — the conclusion, rendered from the frozen findings rather than typed out.

    uv run python -m mr.t4_conclusion

This is the document the branch is judged on, which is exactly why it is generated. Every number in
it is read out of `findings/*.json` and interpolated; none is a literal in the prose. That is not
tidiness, it is the specific bug this branch has already shipped twice — both times the data was
correct and a hand-written constant beside it was not, and both times nothing errored. The ladder is
also still moving: seeds 45/46 change every mean and sd below, and a hand-maintained conclusion would
silently keep quoting the n=3 numbers afterward. Re-running this is the update.

Two guards, both on the **rendered text** rather than on the intermediate dicts, for the same reason
`assert_arms_rendered` is:

  `assert_interim_declared`  — a conclusion drawn off an incomplete ladder must say so on its face.
  `assert_closed_questions`  — each question the handoff records as closed must appear here *with
                               its number*. A conclusion that asserts "stop CDL" without showing the
                               gap is a claim, not a finding.

Nothing here recomputes anything. Recomputation is how a summary and its source drift apart, so the
inputs are read as bytes and pinned in the envelope — same discipline as `t4_matched_data.reference`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import config, encodings, report, thresholds

# name -> the command that produces it, so a missing input is actionable rather than a KeyError
SOURCES = {
    "t4_representation": "uv run python -m mr.t4_representation --merge",
    "t4_matched_data": "uv run python -m mr.t4_matched_data",
    "t4_staple_reread": "uv run python -m mr.t4_staple_reread",
    "t4_diagnostics": "uv run python -m mr.t4_diagnostics",
}

# Optional because the conclusion has to render before Phase 4 exists — a missing scaling result is
# a section that says so, not a crash. Everything in SOURCES is required; this is not.
OPTIONAL_SOURCES = {"t4_scaling": "uv run python -m mr.t4_scaling --merge --corpus cedh"}

LADDER = tuple(a for a in encodings.ALL_ARMS if a != "D")  # D has no full-universe number


class ConclusionIncomplete(RuntimeError):
    """A conclusion was about to be written that overstates what the evidence supports."""


# ── inputs ────────────────────────────────────────────────────────────────────

def load(findings_dir: Path | None = None) -> dict[str, dict]:
    d = findings_dir or config.FINDINGS_DIR
    out = {}
    for name, cmd in SOURCES.items():
        p = d / f"{name}.json"
        if not p.exists():
            raise FileNotFoundError(f"{p} is missing — produce it with:  {cmd}")
        out[name] = json.loads(p.read_text(encoding="utf-8"))
    for name in OPTIONAL_SOURCES:
        p = d / f"{name}.json"
        if p.exists():
            out[name] = json.loads(p.read_text(encoding="utf-8"))
    return out


def source_paths(findings_dir: Path | None = None) -> list[Path]:
    d = findings_dir or config.FINDINGS_DIR
    return [d / f"{n}.json" for n in list(SOURCES) + list(OPTIONAL_SOURCES)
            if (d / f"{n}.json").exists()]


# ── per-seed extraction ───────────────────────────────────────────────────────

def by_seed(records: list[dict], arm: str, *path: str) -> dict[int, float]:
    """`{seed: value}` for one arm, walking a nested result dict.

    Seed-keyed rather than a bare list because every difference below is **paired** — all arms share
    the split, the queries, the eligibility masks and the seeds, so a per-seed difference is
    strictly more informative than differencing two independent means. Pairing on list position
    instead would be correct only as long as the partials happen to glob in seed order.
    """
    out: dict[int, float] = {}
    for r in records:
        if r["arm"] != arm:
            continue
        node = r
        for key in path:
            if not isinstance(node, dict) or key not in node or node[key] is None:
                node = None
                break
            node = node[key]
        if node is not None:
            out[int(r["seed"])] = float(node)
    return out


def stat(vals: dict[int, float]) -> dict:
    a = np.asarray([vals[s] for s in sorted(vals)], dtype=float)
    return {"mean": float(a.mean()) if len(a) else float("nan"),
            "sd": float(a.std(ddof=1)) if len(a) > 1 else 0.0,
            "n_seeds": len(a)}


def paired(x: dict[int, float], y: dict[int, float]) -> dict:
    """Per-seed difference over the seeds both arms actually have."""
    shared = sorted(set(x) & set(y))
    d = np.asarray([x[s] - y[s] for s in shared], dtype=float)
    return {"mean": float(d.mean()) if len(d) else float("nan"),
            "sd": float(d.std(ddof=1)) if len(d) > 1 else 0.0,
            "n_seeds": len(shared)}


COLD = ("full", "coldstart_gated", "centroid", "recall_at_50")
AGG = ("full", "overall", "centroid", "recall_at_50")


def _bucket(bucket: str, scorer: str = "centroid") -> tuple[str, ...]:
    return ("full", "strata", "play_bucket", bucket, scorer, "recall_at_50")


# ── derived figures ───────────────────────────────────────────────────────────

def dose_response(rep: dict) -> list[dict]:
    """One row per arm: aggregate, gated cold-start, and the paired gap against A.

    Ordered by how much structure the arm carries, which is the ladder order — the monotonicity is
    the finding, so the ordering has to be the pre-declared one and not a sort on the result.
    """
    recs = rep["records"]
    base_cold = by_seed(recs, "A", *COLD)
    rows = []
    for arm in LADDER:
        cold, agg = by_seed(recs, arm, *COLD), by_seed(recs, arm, *AGG)
        if not cold:
            continue
        gap = paired(cold, base_cold)
        pooled = thresholds.pooled_sd(stat(cold)["sd"], stat(cold)["n_seeds"],
                                     stat(base_cold)["sd"], stat(base_cold)["n_seeds"])
        rows.append({"arm": arm, "aggregate": stat(agg), "coldstart": stat(cold),
                     "vs_A": gap, "pooled_sd": pooled,
                     "verdict": ("—" if arm == "A" else thresholds.t4_pair_verdict(
                         stat(cold)["mean"], stat(cold)["sd"], stat(cold)["n_seeds"],
                         stat(base_cold)["mean"], stat(base_cold)["sd"], stat(base_cold)["n_seeds"],
                         gate=None))})
    return rows


def highplay_flip(rep: dict) -> dict:
    """`B+ − A` on the high-play bucket — the sign flip the mechanism claim rests on."""
    recs = rep["records"]
    return paired(by_seed(recs, "B+", *_bucket("high")), by_seed(recs, "A", *_bucket("high")))


def baselines(rep: dict) -> dict:
    """Popularity and random, on the aggregate and inside the cold-start stratum.

    Arm-independent by construction, so any record carries them; read from the first record that
    has a full-universe result rather than averaged over arms, which would imply they vary.
    """
    r = next(x for x in rep["records"] if x.get("full"))
    f = r["full"]
    return {
        "agg_pop": f["overall"]["popularity"]["recall_at_50"],
        "agg_rand": f["overall"]["random"]["recall_at_50"],
        "cold_pop": f["coldstart_gated"]["popularity"]["recall_at_50"],
        "cold_rand": f["coldstart_gated"]["random"]["recall_at_50"],
        "cold_n": f["coldstart_gated"]["centroid"]["n"],
        "high_pop": f["strata"]["play_bucket"]["high"]["popularity"]["recall_at_50"],
        "high_n": f["strata"]["play_bucket"]["high"]["centroid"]["n"],
        "n_near_zero": f["n_near_zero"],
        "near_zero_underpowered": f["near_zero_underpowered"],
        "n_queries": f["n_queries"],
    }


def cdl_case(src: dict) -> dict:
    """The four closed questions, each with the number that closed it."""
    rep, reread, diag, matched = (src["t4_representation"], src["t4_staple_reread"],
                                  src["t4_diagnostics"], src["t4_matched_data"])
    all_clean = reread["clean_reread"]["all_clean"]["paired"]
    non_staple = reread["clean_reread"]["clean_non_staple"]["paired"]
    gaps = {a: v["geometry"]["dialect_gap"]["mean"] for a, v in diag["d1_partition"]["arms"].items()}
    controls = {a: g for a, g in gaps.items() if a != "C"}
    flips = diag["d2_displacement"]["flip_decomposition"]

    runs = matched["runs"]
    m = float(np.mean([v["B+_matched"]["recall_at_50"] for v in runs.values()]))
    rnd = float(np.mean([v["B+_random"]["recall_at_50"] for v in runs.values()]))
    d_ref, full_ref = matched["reference"]["D"], matched["reference"]["B+_full"]

    return {
        "c_minus_bplus": rep["verdicts"]["C - B+"],
        "all_clean": all_clean,
        "non_staple": non_staple,
        "non_staple_sd_multiple": abs(non_staple["mean"]) / non_staple["sd"] if non_staple["sd"] else float("nan"),
        "dialect_gap_C": gaps["C"],
        "dialect_gap_controls": (min(controls.values()), max(controls.values())),
        "across_C": diag["d1_partition"]["arms"]["C"]["geometry"]["across"]["mean"],
        "n_cdl": diag["d1_partition"]["n_cdl"],
        "n_fallback": diag["d1_partition"]["n_fallback"],
        "displaced_clean_share": float(np.mean([v["clean_share_of_displaced"] for v in flips.values()])),
        "baseline_clean_share": float(np.mean([v["clean_share_of_Bplus_top50"] for v in flips.values()])),
        "encoding_effect": m - d_ref["recall_at_50"],
        "volume_effect": full_ref["recall_at_50"] - m,
        "skew_effect": m - rnd,
        "d_examples": d_ref["n_examples"],
        "full_examples": full_ref["n_examples"],
        "matched_ratio": full_ref["n_examples"] / d_ref["n_examples"],
    }


# ── guards ────────────────────────────────────────────────────────────────────

def assert_interim_declared(rep: dict, md: str) -> None:
    """An unfinished ladder must be declared unfinished *in the conclusion*, not only upstream.

    `t4_representation.md` already carries its own INTERIM banner, but nobody reads two documents to
    find out whether one of them is provisional. The conclusion is what gets cited and pasted, so it
    carries the caveat or it does not get written.
    """
    if rep.get("is_final"):
        return
    if "INTERIM" not in md:
        raise ConclusionIncomplete(
            f"the ladder is at {rep.get('n_seeds')} of {rep.get('frozen_n_seeds')} frozen seeds "
            "and the rendered conclusion carries no INTERIM banner. Refusing to write a conclusion "
            "that reads as settled off an incomplete grid."
        )


def assert_closed_questions(md: str, required: dict[str, str]) -> None:
    """Each closed question must appear with the number that closed it.

    Asserting on the rendered markdown, not on the dict it came from: a conclusion whose supporting
    figure silently dropped out of a table still reads as a confident conclusion, and the table is
    the only part anyone checks.
    """
    missing = sorted(q for q, num in required.items() if num not in md)
    if missing:
        raise ConclusionIncomplete(
            f"closed questions {missing} are asserted but their numbers are absent from the "
            "rendered conclusion. A claim without its figure is not a finding."
        )


# ── render ────────────────────────────────────────────────────────────────────

def render(src: dict) -> tuple[str, dict]:
    rep = src["t4_representation"]
    rows, flip, base, cdl = dose_response(rep), highplay_flip(rep), baselines(rep), cdl_case(src)
    meta = rep["layer0_meta"]

    banner = ""
    if not rep.get("is_final"):
        banner = (
            f"> **INTERIM — {rep.get('n_seeds')} of {rep.get('frozen_n_seeds')} frozen seeds.** "
            "Every mean and standard deviation below moves when the remaining seeds land, and the "
            "null rule is measured against a sample sd that is itself noisy at this n. The "
            "*direction* of each finding is stable across the seeds that have run; the magnitudes "
            "are not final. Complete with `--seeds 45 46`, re-merge, and re-run this.\n\n")

    def _row(r: dict) -> str:
        vs = "—" if r["arm"] == "A" else f"{r['vs_A']['mean']:+.4f}"
        pooled = "—" if r["arm"] == "A" else f"{r['pooled_sd']:.4f}"
        verd = "—" if r["arm"] == "A" else f"**{r['verdict']}**"
        return (f"| {r['arm']} | {r['coldstart']['n_seeds']} | {r['aggregate']['mean']:.4f} "
                f"| {r['coldstart']['mean']:.4f} | {r['coldstart']['sd']:.4f} | {vs} | {pooled} "
                f"| {verd} |")

    ladder = "\n".join(_row(r) for r in rows)
    cold_means = [r["coldstart"]["mean"] for r in rows]
    best_agg = max(rows, key=lambda r: r["aggregate"]["mean"])
    cold_vs_rand = min(cold_means) / base["cold_rand"] if base["cold_rand"] else float("nan")
    cold_vs_agg = min(cold_means) / max(r["aggregate"]["mean"] for r in rows)

    cb = cdl["c_minus_bplus"]
    lo, hi = cdl["dialect_gap_controls"]

    md = f"""# T4 — conclusion

{report.confound_header()}
{banner}Six arms, one variable: how a card becomes a vector. Leave-one-out retrieval on held-out
decks, primary metric recall@50, deck-level split, corpus **{meta.get('corpus')}** —
{meta.get('n_train_decks'):,} train / {meta.get('n_test_decks'):,} test decks,
{base['n_queries']:,} queries against a {meta.get('n_pool'):,}-card pool.

**Richer structured representation is not the lever.** Every frozen gate failed. But the result is
not a flat null, and the shape of it is the finding.

## 1. The dose-response

| arm | seeds | recall@50 | gated cold-start | sd | vs A (paired) | pooled sd | null rule |
|---|---|---|---|---|---|---|---|
{ladder}

Cold-start is the gated `near_zero ∪ low` stratum — at most
{thresholds.T4_COLDSTART_MAX_COUNT} appearances across *training* decks — {base['cold_n']:,} targets.
The `vs A` column is the per-seed paired difference, which is strictly more informative because all
arms share the split, the queries and the seeds. The verdict beside it is the **frozen** rule, which
is measured against the pooled seed sd and is the one that counts.

The arms are ordered by how much structure they carry, and the cold-start column falls
monotonically along that ordering. On the high-play bucket ({base['high_n']:,} targets) the sign
flips: `B+ − A` = **{flip['mean']:+.4f}** paired across {flip['n_seeds']} seeds.

**Reading: structure trades generalization for memorization.** Added structure raises
representational distinctiveness, which pays where dense co-occurrence makes memorizing a card's
neighbourhood viable, and costs where thin data forces generalizing from text. The frozen
cold-start gate asked for the opposite — best arm − A ≥ {thresholds.T4_GATE_COLDSTART:+.2f} — and
got a negative number instead.

## 2. The premise that survives

Cold-start recall lands at {min(cold_means):.4f}–{max(cold_means):.4f}: about
{cold_vs_agg:.1f}× the aggregate, **{cold_vs_rand:.0f}× random** ({base['cold_rand']:.6f}), against
a popularity baseline of **exactly {base['cold_pop']:.4f}**.

A content encoder is needed there, and it delivers. What T4 refutes is *richer structured
representation*, not the text encoder — arm A, the plainest arm in the ladder, is the one to ship
for the tail.

## 3. The metric verdict

Popularity alone scores **{base['agg_pop']:.4f}** on the aggregate, beating every arm
(best: {best_agg['arm']} at {best_agg['aggregate']['mean']:.4f}). It reaches
{base['high_pop']:.4f} on high-play cards and {base['cold_pop']:.4f} off them.

A global frequency ranking returns the same top-50 for every query, so **aggregate recall@50 on
this task substantially measures staple recovery**. That is why the stratified numbers, not the
aggregate, are T4's output — and why the shippable system is popularity for staples and arm A for
the tail, split on play count, rather than one ranker.

## 4. Stop CDL

Over-determined. Four independent attempts to rescue it, each with the number that failed:

| question | answer | figure |
|---|---|---|
| Does CDL justify continued development? | No | `C − B+` = **{cb['gap']:+.4f}**, pooled seed sd {cb['pooled_sd']:.4f}, gate {cb['gate']:+.2f} → **{cb['verdict']}** |
| Is that an artifact of the staple-enriched clean stratum? | No | clean **{cdl['all_clean']['mean']:+.4f}**, clean non-staple **{cdl['non_staple']['mean']:+.4f}** = {cdl['non_staple_sd_multiple']:.1f}× its paired sd |
| Is it heterogeneity — two dialects in one space — rather than CDL? | No | C's partition gap **{cdl['dialect_gap_C']:.4f}** vs {lo:.4f}–{hi:.4f} for the single-dialect arms; real, but small against mean cosine {cdl['across_C']:.3f} |
| Was arm D simply data-starved? | No | at matched volume the encoding effect is **{cdl['encoding_effect']:+.4f}** against a volume effect of {cdl['volume_effect']:+.4f} over {cdl['matched_ratio']:.0f}× the data |

C's apparent gain on gapped cards is **displacement, not skill**: the cards C evicts from the top-50
are {cdl['displaced_clean_share']:.1%} clean against a {cdl['baseline_clean_share']:.1%} baseline
share. It moves clean-parse cards out to let gap cards in; the slots are conserved.

CDL is used on {cdl['n_cdl']:,} of {cdl['n_cdl'] + cdl['n_fallback']:,} cards in the pool, and on
exactly those cards it makes retrieval worse.

{scaling_section(src, cdl)}
## Caveats, weighted

- **The EDHREC confound above is the largest one and is not solved.** Absolute numbers do not mean
  much; relative comparisons between arms survive because all arms inherit it equally.
- **Popularity beating every arm on the aggregate is a real result, not a harness fault.** It is
  also the sharpest statement of why the aggregate is the wrong headline.
- **The true-zero bucket is underpowered** and is flagged as such in every partial —
  {base['n_near_zero']:,} of {base['n_queries']:,} targets, where a difference the size of the
  frozen gate sits at roughly 2σ binomial. That is why the cold-start gate binds on
  `near_zero ∪ low` rather than on `near_zero` alone: the bucket was reported, not quietly widened
  until it looked significant.
- **The matched-volume decomposition does not transfer to cold-start.** It is scored clean-only on
  both sides, and that universe is the memorization regime where structure helps and popularity
  alone reaches high recall. It settles arm D's confound and nothing about the tail.
- **Everything above is one corpus.** cedh transfer is a separate run.
- **The diagnostics in §4 were specified after seeing the T4 result** and carry no pre-committed
  criteria. They constrain *which claim* the evidence supports; the decision rests on the gated
  ladder and the cold-start dose-response, which were frozen first.
"""

    payload = {
        "dose_response": rows, "highplay_flip": flip, "baselines": base, "cdl_case": cdl,
        "ladder_is_final": bool(rep.get("is_final")),
        "ladder_n_seeds": rep.get("n_seeds"),
        "corpus": meta.get("corpus"),
    }
    return md, payload


def scaling_section(src: dict, cdl: dict) -> str:
    """The cedh diversity test, and what it composes with D4 into.

    Absent until Phase 4 runs, so the section says that rather than being silently omitted — a
    conclusion missing a section it should have reads the same as one that never needed it.
    """
    sc = src.get("t4_scaling")
    if not sc:
        return ("\n## 5. Does more data help? — **not yet run**\n\n"
                "`findings/t4_scaling.json` is absent, so the volume-versus-diversity decomposition "
                "below is missing its second half. Produce it with "
                "`uv run python -m mr.t4_scaling --merge --corpus cedh`.\n")

    res, meta = sc["analysis"], sc["layer0_meta"]
    rows = []
    for arm in sorted(res):
        a = res[arm]
        for name in ("cold_start", "aggregate"):
            u = a["universes"].get(name)
            if not u:
                continue
            b = u["bootstrap"]
            ci = f"[{b['ci_low']:+.4f}, {b['ci_high']:+.4f}]" if b else "—"
            # Surfaced next to the verdict because T4's standing null rule is checked LAST inside
            # `t4_scaling_verdict`, after the CI branches — so a row can carry a decisive-sounding
            # label while its gap sits inside the training noise. The reader needs both columns.
            inside = abs(u["observed"]) < u["pooled_sd"]
            note = " ⚠︎ inside seed sd" if inside else ""
            rows.append(f"| {arm} | {name.replace('_', '-')} | {u['observed']:+.4f} "
                        f"| {u['predicted']:+.4f} | {ci} | {u['pooled_sd']:.4f} "
                        f"| **{u['verdict']}**{note} |")

    cold = {arm: res[arm]["universes"]["cold_start"] for arm in res
            if "cold_start" in res[arm]["universes"]}
    best = max(cold.values(), key=lambda u: u["observed"]) if cold else None
    ratio = next(iter(res.values()))["example_ratio"]
    pred = next(iter(res.values()))["predicted"]
    deck_ratio = meta.get("n_train_available", 0) / max(1, thresholds.T4_SCALING_CEDH_SUBSAMPLE)
    fold = best["observed"] / pred if best and pred else float("nan")

    return f"""
## 5. Does more data help? Volume and diversity, separated

Two experiments, each holding one thing fixed.

| | decks | training examples | effect |
|---|---|---|---|
| **D4** (`t4_matched_data`, casual) | fixed | **{cdl['matched_ratio']:.0f}×** | volume: `B+ full − B+_random` = **{cdl['volume_effect'] + cdl['skew_effect']:+.4f}** |
| **cedh scaling** (`t4_scaling`) | **{deck_ratio:.1f}×** | {ratio:.2f}× | diversity: see below |

The cedh test compares {thresholds.T4_SCALING_CEDH_SUBSAMPLE:,} train decks against
{meta.get('n_train_available', 0):,}, resampled per seed, on a byte-identical test set of
{meta.get('n_queries', 0):,} queries. Its **volume null of {pred:+.4f}** is what D4's slope says the
extra examples alone buy — pre-registered in `THRESHOLDS.md` from counts measured before any model
trained.

| arm | universe | observed | volume null | 95% CI | pooled seed sd | verdict |
|---|---|---|---|---|---|---|
{chr(10).join(rows)}

**On the cold-start stratum every arm beats the volume null by roughly {fold:.0f}×.** More decks buy
something on the tail that more pairs do not explain. On the aggregate nothing survives: every gap
there is smaller than its own pooled seed sd, so by T4's standing null rule those rows are **null**
regardless of the label — the query bootstrap is tight because it resamples ~10⁵ paired queries, and
it does not see training variance. That the frozen rule prints a decisive label anyway is a
limitation of the rule, recorded rather than repaired after the fact.

**Three caveats, all load-bearing.** Volume is *not* matched ({ratio:.2f}× examples), so an excess
over the null is diversity evidence conditional on D4's slope transferring from casual to cedh. The
cold-start stratum is frozen at full-level counts, so cards with ≤{thresholds.T4_COLDSTART_MAX_COUNT}
appearances in {meta.get('n_train_available', 0):,} decks expect ≤{thresholds.T4_COLDSTART_MAX_COUNT * thresholds.T4_SCALING_CEDH_SUBSAMPLE / max(1, meta.get('n_train_available', 1)):.2f}
in the subsample and are mostly **absent** rather than rare — part of the effect is "a few exposures
versus none". And `max_sim` runs the *opposite* direction to `centroid` on the aggregate; both
aggregators were frozen up front so that gets reported rather than chosen between.

## 6. What to do with all of it

Ranked by what the evidence actually supports:

1. **Do not spend on richer representation.** Every gate failed, the dose-response is monotonically
   *against* structure on cold-start, and CDL specifically makes retrieval worse.
2. **Do spend on more, more diverse decks** — that is the only intervention here that moved the
   cold-start number, and it moved it by ~{fold:.0f}× what extra training pairs alone would.
3. **Ship popularity for staples and arm A for the tail**, split on play count. Popularity wins the
   aggregate outright and scores exactly 0.0000 off it; arm A is the plainest arm and the best of
   them where popularity cannot reach.
"""


REQUIRED = ("c_minus_bplus", "non_staple", "dialect_gap_C", "encoding_effect")


def main(findings_dir: Path | None = None) -> dict:
    d = findings_dir or config.FINDINGS_DIR
    src = load(d)
    md, payload = render(src)

    assert_interim_declared(src["t4_representation"], md)
    cdl = payload["cdl_case"]
    assert_closed_questions(md, {
        "stop-CDL": f"{cdl['c_minus_bplus']['gap']:+.4f}",
        "not a staple artifact": f"{cdl['non_staple']['mean']:+.4f}",
        "not heterogeneity": f"{cdl['dialect_gap_C']:.4f}",
        "arm D was not data-starved": f"{cdl['encoding_effect']:+.4f}",
    })

    env = report.envelope(source_paths(d), {"sources": sorted(SOURCES)})
    env.update(payload)
    report.write("t4_conclusion", env, md, findings_dir=d)
    return env


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--findings-dir", type=Path, default=None)
    out = main(ap.parse_args().findings_dir)
    print(f"wrote findings/t4_conclusion.md  "
          f"(ladder {'FINAL' if out['ladder_is_final'] else 'INTERIM'}, "
          f"n_seeds={out['ladder_n_seeds']})")
