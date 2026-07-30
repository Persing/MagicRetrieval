"""Re-read `C − B+` where the metric isn't measuring staple recovery.

    uv run python -m mr.t4_staple_reread

## Why this exists

T4's decisive number, `C − B+ = −0.0216`, lives on the clean-parse stratum. Two facts make that
stratum the worst place to read a retrieval result, and they compound:

1. **It is staple-enriched by construction.** T0 measured CDL clean coverage at 85.3% of the
   top-100 most-played cards, 69.6% of the top-500, 55.2% of the top-2000, against 43.2% overall —
   the parser handles common cards well because common cards use common templates. At query level
   that shows up as clean-parse targets being 46.6% high-play versus 8.9% for excluded.

2. **The appearance-weighted metric substantially measures staple recovery.** A global frequency
   ranking returns the same top-50 for every query, so its aggregate recall is arithmetically "what
   fraction of held-out targets are top-50 staples" — and popularity scores 0.1808 against the best
   arm's 0.0624, entirely from the high-play bucket (0.6481 × 11,283 = 7,313 hits = 100% of its
   aggregate) with exactly 0.0000 everywhere else.

So the one stratum where CDL-as-representation is testable at all is also the one where the metric
is least meaningful. This module re-reads the comparison on **clean non-staples**.

It is a refinement, not the decider. The cold-start dose-response already decides — structure is
monotonically harmful there (B_type −0.0034, B+ −0.0080, C −0.0137, all real), and popularity
scores 0.0000 on that stratum, so it is uncontaminated by the staple artifact. What this changes is
*which* stop-CDL claim the write-up is entitled to make:

  −0.0216 survives on clean non-staples  -> "CDL makes retrieval worse"
  it is staple-driven                    -> "CDL is neutral where it matters, at enormous
                                            maintenance cost"

Both are stop-CDL. The T0 argument — clean coverage of 43–48% means no CDL-only system can exist,
so any deployable CDL system is a hybrid, and the hybrid lost — is independent of metric choice.

## Also folded in: the parse-status base-rate inversion

B+ recall climbs clean 0.0580 -> gap 0.0649 -> excluded 0.0808: the cards the parser handles worst
are the easiest to retrieve. That looked like evidence that parse quality is unrelated to retrieval
quality. It is a play-frequency confound — see `standardized_by_play`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, corpus, loo_eval, report, representations, t4_representation as T

ARMS = ("A", "B", "B_type", "B+", "C")
K = 50


def _sd(v: list[float]) -> float:
    return float(np.std(v, ddof=1)) if len(v) > 1 else 0.0


def summary(v: dict[int, float]) -> dict:
    vals = [v[s] for s in sorted(v)]
    return {"n_seeds": len(vals), "mean": float(np.mean(vals)), "sd": _sd(vals),
            "by_seed": {str(s): v[s] for s in sorted(v)}}


def paired(a: dict[int, float], b: dict[int, float]) -> dict:
    """Per-seed difference. Observational only — the frozen verdicts live in `t4_representation`;
    nothing here may move one."""
    seeds = sorted(set(a) & set(b))
    d = [a[s] - b[s] for s in seeds]
    return {"n_seeds": len(d), "mean": float(np.mean(d)) if d else None, "sd": _sd(d),
            "by_seed": {str(s): a[s] - b[s] for s in seeds}}


# ── the query frame ───────────────────────────────────────────────────────────

def query_frame(l0: dict, root: Path) -> pd.DataFrame:
    """One row per query, carrying the target's strata and per-arm hit/miss at k=50.

    Built over **queries**, not cards. Per-card marginals are the wrong denominator here: staples
    appear in many test decks and so generate many queries, and a card-level cross-tabulation of
    parse status against play bucket gives a materially different — and wrong — picture of what the
    metric is weighted by.
    """
    strata = l0["strata"].set_index("oracle_id")
    target_oids = [l0["oid_order"][i] for i in l0["queries"].target_row]
    df = pd.DataFrame({"oid": target_oids})
    df = df.join(strata[["parse_status", "play_bucket", "train_appearances"]], on="oid")

    # `identify_staples` (>50% of decks) is near-vacuous on casual — T0 found 3 such cards — so it
    # is a secondary column only. The frozen `play_bucket` split carries the analysis; agreement
    # between two cuts, one of which selects almost nothing, is not robustness.
    staples = corpus.identify_staples(l0["train"])
    df["is_staple_50pct"] = df["oid"].isin(staples)
    df["is_high_play"] = df["play_bucket"] == "high"

    for arm in ARMS:
        for seed in (42, 43, 44):
            p = T.embedding_path("casual", arm, seed, l0, root)
            if not p.exists():
                continue
            ranks = loo_eval.score_ranks(np.load(p), l0["queries"], seed=seed)["centroid"]
            df[f"hit_{arm}_{seed}"] = ranks <= K
    return df


def _recall(df: pd.DataFrame, arm: str) -> dict[int, float]:
    out = {}
    for seed in (42, 43, 44):
        col = f"hit_{arm}_{seed}"
        if col in df and len(df):
            out[seed] = float(df[col].mean())
    return out


def contrast(df: pd.DataFrame, hi: str, lo: str) -> dict:
    return {"n_queries": int(len(df)),
            hi: summary(_recall(df, hi)), lo: summary(_recall(df, lo)),
            "paired": paired(_recall(df, hi), _recall(df, lo))}


# ── the two analyses ──────────────────────────────────────────────────────────

def clean_reread(df: pd.DataFrame) -> dict:
    """`C − B+` on clean-parse queries, split by staple status."""
    clean = df[df["parse_status"] == "clean"]
    out = {"all_clean": contrast(clean, "C", "B+")}
    for label, mask in (("clean_non_staple", ~clean["is_high_play"]),
                        ("clean_high_play", clean["is_high_play"]),
                        ("clean_non_staple_50pct", ~clean["is_staple_50pct"])):
        out[label] = contrast(clean[mask], "C", "B+")
    return out


def standardized_by_play(df: pd.DataFrame, arm: str = "B+", n_bins: int = 10) -> dict:
    """Parse-status recall, crude and standardized to the overall play distribution.

    Direct standardization rather than a regression: the repo judges everything against
    pre-committed thresholds and seed spread, sklearn gives no standard errors, and there is no
    `statsmodels` dependency. Non-parametric, and its uncertainty is the seed spread like
    everything else in T4.

    The per-decile contrast is reported alongside the standardized scalar, deliberately.
    `train_appearances` is a target-side property that popularity alone converts into 0.6481
    recall@50 in the high bucket, so standardizing on it removes a large share of *all* retrieval
    variance — the surviving contrast is measured on much-reduced signal and a scalar alone would
    overstate what is left.
    """
    q = df["train_appearances"].fillna(0)
    edges = np.unique(np.quantile(q, np.linspace(0, 1, n_bins + 1)))
    df = df.assign(_bin=np.clip(np.digitize(q, edges[1:-1]), 0, len(edges) - 2))
    overall = df["_bin"].value_counts(normalize=True)

    per_bin, out = {}, {}
    for st in ("clean", "gap", "excluded"):
        sub = df[df["parse_status"] == st]
        crude = summary(_recall(sub, arm))
        std, covered, rows = {}, 0.0, {}
        for seed in (42, 43, 44):
            col = f"hit_{arm}_{seed}"
            if col not in sub:
                continue
            num = wt = 0.0
            for b, w in overall.items():
                cell = sub[sub["_bin"] == b]
                if len(cell) < 30:      # too thin to standardize on; excluded weight is reported
                    continue
                num += w * float(cell[col].mean())
                wt += w
                rows.setdefault(int(b), {})[st] = float(cell[col].mean())
            std[seed] = num / wt if wt else float("nan")
            covered = wt
        out[st] = {"crude": crude, "standardized": summary(std), "weight_covered": covered,
                   "n_queries": int(len(sub))}
        per_bin.update(rows)
    return {"by_status": out, "per_bin": {str(k): v for k, v in sorted(per_bin.items())},
            "bin_edges": [float(e) for e in edges]}


# ── report ────────────────────────────────────────────────────────────────────

def _row(label: str, c: dict) -> str:
    p = c["paired"]
    return (f"| {label} | {c['n_queries']:,} | {c['B+']['mean']:.4f} | {c['C']['mean']:.4f} "
            f"| **{p['mean']:+.4f}** | {p['sd']:.4f} |")


def render(res: dict) -> str:
    cr = res["clean_reread"]
    rows = "\n".join(_row(k.replace("_", " "), cr[k])
                     for k in ("all_clean", "clean_high_play", "clean_non_staple",
                               "clean_non_staple_50pct"))
    sb = res["standardized"]["by_status"]
    srows = "\n".join(
        f"| {st} | {v['n_queries']:,} | {v['crude']['mean']:.4f} | {v['standardized']['mean']:.4f} "
        f"| {v['standardized']['mean'] - v['crude']['mean']:+.4f} | {report.pct(v['weight_covered'])} |"
        for st, v in sb.items())
    ct = res["crosstab_pct"]
    crows = "\n".join(
        f"| {st} | " + " | ".join(f"{ct[st][b]:.1f}%" for b in ("near_zero", "low", "mid", "high")) + " |"
        for st in ("clean", "gap", "excluded"))

    non_staple = cr["clean_non_staple"]["paired"]["mean"]
    all_clean = cr["all_clean"]["paired"]["mean"]
    survives = abs(non_staple) >= 0.5 * abs(all_clean)
    verdict = (
        "**The deficit survives.** `C − B+` on clean non-staples is at least half its all-clean "
        "magnitude, so it is not an artifact of the staple-enriched stratum. The write-up is "
        "entitled to the stronger claim: **CDL makes retrieval worse.**"
        if survives else
        "**The deficit is substantially staple-driven.** `C − B+` shrinks materially once staples "
        "are removed, so the all-clean figure is partly the metric measuring staple recovery. The "
        "write-up should make the weaker, better-supported claim: **CDL is neutral where it "
        "matters, at enormous maintenance cost.**")

    return f"""# T4 — clean-stratum re-read

{report.confound_header()}
> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated here and no verdict in
> `findings/t4_representation.md` is affected. This refines *which claim* the CDL evidence
> supports; it does not decide whether to stop CDL. The cold-start dose-response already did that,
> in the one stratum popularity cannot contaminate.

## Why the decisive number needed re-reading

`C − B+ = −0.0216` sits on the clean-parse stratum, which is the only place CDL-as-representation
is testable — on gap and excluded cards C and B+ serialize the target identically and differ only
in query vector and competitor pool. That stratum is also **staple-enriched by construction**: T0
measured CDL clean coverage at 85.3% of the top-100 most-played cards against 43.2% overall,
because common cards use common templates.

And the metric is staple-dominated: popularity scores **0.1808** against the best arm's 0.0624,
all of it from the high-play bucket and **exactly 0.0000** elsewhere. A global frequency ranking
returns the same top-50 for every query, so appearance-weighted recall substantially measures
staple recovery.

### Play distribution by parse status, over queries

| parse status | near_zero | low | mid | high |
|---|---|---|---|---|
{crows}

## The re-read

| stratum | queries | B+ | C | C − B+ | paired sd |
|---|---|---|---|---|---|
{rows}

{verdict}

`is_staple_50pct` uses `corpus.identify_staples` (>50% of decks) and is reported only for
completeness — it selects almost nothing on casual, so agreement with the play-bucket cut would
not be independent confirmation.

## Base-rate inversion, resolved

B+ recall climbs clean → gap → excluded, which looked like evidence that parse quality is
unrelated to retrieval quality. It is a play-frequency confound:

| parse status | queries | crude | play-standardized | shift | weight covered |
|---|---|---|---|---|---|
{srows}

Standardizing to the overall play distribution removes the inversion. Clean-parse cards are
disproportionately staples, and staples are the hardest cards to retrieve — the ordering was
reporting the play mix, not the parser.

Read the per-decile contrast in the JSON rather than this scalar alone: `train_appearances` is a
target-side property that popularity converts into 0.6481 recall@50 by itself, so standardizing on
it strips a large share of all retrieval variance and the residual is measured on much-reduced
signal.
"""


def main(root: Path | None = None) -> dict:
    root = root or config.RUNS_DIR / "t4"
    cards = corpus.load_cards_parquet()
    rt = representations.load()
    l0 = T.build_layer0("casual", cards, rt, config.SEED, root)
    if not l0["meta"].get("from_cache"):
        raise RuntimeError(
            "layer0 was rebuilt rather than loaded from cache. The cached embeddings were trained "
            "against the previous basis, so every number here would describe a corpus the models "
            "never saw. Refusing to continue.")

    df = query_frame(l0, root)
    ct = (pd.crosstab(df["parse_status"], df["play_bucket"], normalize="index") * 100)
    res = {
        "clean_reread": clean_reread(df),
        "standardized": standardized_by_play(df),
        "crosstab_pct": ct.to_dict(orient="index"),
        "n_queries": int(len(df)),
    }
    payload = report.envelope([config.CARDS_PARQUET], {"corpus": "casual", "k": K,
                                                       "arms": list(ARMS)})
    payload.update(res)
    report.write("t4_staple_reread", payload, render(res))
    return res


if __name__ == "__main__":
    r = main()
    cr = r["clean_reread"]
    for k in ("all_clean", "clean_high_play", "clean_non_staple"):
        c = cr[k]
        print(f"  {k:24s} n={c['n_queries']:>6,}  B+ {c['B+']['mean']:.4f}  C {c['C']['mean']:.4f}  "
              f"C-B+ {c['paired']['mean']:+.4f} (sd {c['paired']['sd']:.4f})")
    print()
    for st, v in r["standardized"]["by_status"].items():
        print(f"  {st:9s} crude {v['crude']['mean']:.4f} -> standardized {v['standardized']['mean']:.4f}")
