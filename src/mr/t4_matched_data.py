"""The encoding effect at matched training volume — what arm D was supposed to measure.

    uv run python -m mr.t4_matched_data

Arm D is uninterpretable as run. It trained on **8,702 examples against 234,593 for every other
arm — 3.7%** — because it only trains on mined pairs whose both ends parse cleanly, and
`build_example_oids` then drops any survivor whose assigned negative also lacks text under that
arm. Its deficit therefore mixes encoding with training volume, and the two cannot be separated
from that run.

This recovers the comparison by holding volume fixed:

  **B+_matched**  — B+'s encoding on D's *exact* 8,702 triples. `B+_matched − D` is then the
                    encoding effect with data held constant, which is the number arm D was for.
  **B+_random**   — B+'s encoding on 8,702 triples drawn uniformly from the full set. D's triples
                    are not a random 3.7%: they are clean-clean anchors, and clean-parse cards are
                    also the most-played (267 vs 45 mean train appearances at query level). This
                    arm separates "small data" from "small *and* skewed data". Without it,
                    `B+_matched − D` is still confounded, just less so.

**Two limits on what these numbers license.** They are scored clean-only on *both* the query and
candidate sides — arm D cannot embed gap cards at all — so they are not comparable to the ladder's
clean stratum, which ranked against the full 30,958-card pool. And that universe is **46.6%
high-play**: the memorization regime where structure helps and where popularity alone reaches
0.6481. Whatever this shows about encoding does **not** transfer to cold-start, which is where T4's
actual conclusion lives.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import (config, corpus, finetune, loo_eval, report, representations,
               t4_representation as T, thresholds)

SEEDS = (42, 43, 44)
LABELS = ("B+_matched", "B+_random")


def partial_path(seed: int, findings_dir: Path) -> Path:
    """Deliberately NOT `t4_partial_*`.

    `t4_representation.merge_partials` globs that prefix. A file matching it would be ingested as a
    seventh arm, be absent from `render`'s ladder order, and trip `assert_arms_rendered` — halting
    the frozen report. The diagnostic must be unable to reach the gated one.
    """
    return findings_dir / f"t4_matched_partial_casual_s{seed}.json"


def d_example_oids(l0: dict, root: Path) -> list[tuple[str, ...]]:
    """The exact triples arm D trained on.

    Replayed through `build_example_oids` over the **full** positives list rather than filtered:
    `fallback_pool[i % len(fallback_pool)]` is indexed by position, so filtering first would
    silently reassign which negative each surviving anchor trains against, and the "matched" set
    would not be D's.
    """
    texts_d, _ = T.cached_texts("D", l0["pool"], representations.load(), root)
    has_text = {oid for oid, t in texts_d.items() if t}
    return finetune.build_example_oids(l0["positives"], l0["negatives"], has_text)


def run_seed(seed: int, l0: dict, root: Path, findings_dir: Path, force: bool = False) -> dict:
    rt = representations.load()
    texts_bp, _ = T.cached_texts("B+", l0["pool"], rt, root)
    has_bp = {oid for oid, t in texts_bp.items() if t}

    matched_oids = d_example_oids(l0, root)
    full_oids = finetune.build_example_oids(l0["positives"], l0["negatives"], has_bp)
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(full_oids), size=len(matched_oids), replace=False)
    random_oids = [full_oids[i] for i in sorted(idx)]

    out: dict = {"seed": seed, "n_matched_examples": len(matched_oids),
                 "n_full_examples": len(full_oids)}
    for label, oids in (("B+_matched", matched_oids), ("B+_random", random_oids)):
        emb_path = root / "emb" / f"casual_matched_{label.replace('+', 'plus')}_s{seed}.npy"
        emb_path.parent.mkdir(parents=True, exist_ok=True)
        if emb_path.exists() and not force:
            E = np.load(emb_path)
        else:
            examples = [tuple(texts_bp[o] for o in row) for row in oids]
            E = finetune.finetune(l0["positives"], l0["negatives"], texts_bp, l0["oid_order"],
                                  seed=seed, max_seq_length=thresholds.T4_MAX_SEQ_LENGTH,
                                  examples=examples)
            np.save(emb_path, E)
        res = loo_eval.evaluate(E, l0["queries"], l0["strata"], seed=seed,
                                restrict_to=l0["clean_mask"])
        out[label] = {"n_examples": len(oids),
                      "recall_at_50": res["overall"]["centroid"]["recall_at_50"],
                      "recall_at_10": res["overall"]["centroid"]["recall_at_10"],
                      "n_queries": res["n_queries"]}
    return out


def reference(findings_dir: Path) -> dict:
    """D and full-B+ numbers read from the frozen partials, never recomputed.

    Guarded against the merged report so a silent divergence between this diagnostic and the gated
    result is impossible.
    """
    merged = json.loads((findings_dir / "t4_representation.json").read_text(encoding="utf-8"))
    agg = merged["aggregate_clean_universe"]
    out = {}
    for arm, label in (("D", "D"), ("B+", "B+_full")):
        out[label] = {"recall_at_50": agg[arm]["mean"], "sd": agg[arm]["sd"],
                      "n_examples": max(r.get("n_training_examples", 0)
                                        for r in merged["records"] if r["arm"] == arm)}
    return out


def render(rows: dict, ref: dict) -> str:
    def _s(vals):
        return float(np.mean(vals)), (float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0)

    body = []
    for label in LABELS:
        m, s = _s([r[label]["recall_at_50"] for r in rows.values()])
        body.append((label, rows[list(rows)[0]][label]["n_examples"], m, s))
    lines = [f"| {ref['B+_full']['n_examples']:,} | B+ (full, from the frozen ladder) | "
             f"{ref['B+_full']['recall_at_50']:.4f} | {ref['B+_full']['sd']:.4f} |"]
    lines += [f"| {n:,} | {lbl} | {m:.4f} | {s:.4f} |" for lbl, n, m, s in body]
    lines.append(f"| {ref['D']['n_examples']:,} | D (CDL only, from the frozen ladder) | "
                 f"{ref['D']['recall_at_50']:.4f} | {ref['D']['sd']:.4f} |")
    tbl = "\n".join(lines)

    matched = next(m for lbl, _, m, _ in body if lbl == "B+_matched")
    rand = next(m for lbl, _, m, _ in body if lbl == "B+_random")
    enc = matched - ref["D"]["recall_at_50"]
    vol = ref["B+_full"]["recall_at_50"] - matched
    skew = matched - rand

    return f"""# T4 — encoding effect at matched training volume

{report.confound_header()}
> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated and no verdict in
> `findings/t4_representation.md` is affected. Specified after seeing the T4 result, with no
> pre-committed criteria.

Arm D trained on **{ref['D']['n_examples']:,} examples against {ref['B+_full']['n_examples']:,}**
for every other arm — 3.7% — so its deficit mixes encoding with training volume inseparably. These
runs hold volume fixed.

All rows scored **clean-only on both the query and candidate sides**, since arm D cannot embed
gap cards at all.

| training examples | model | recall@50 | sd |
|---|---|---|---|
{tbl}

## Decomposition

| effect | size | reading |
|---|---|---|
| **encoding**, volume held fixed | `B+_matched − D` = **{enc:+.4f}** | what arm D was meant to measure |
| **volume**, encoding held fixed | `B+ full − B+_matched` = **{vol:+.4f}** | cost of the 27× data reduction |
| **skew** of D's slice | `B+_matched − B+_random` = **{skew:+.4f}** | D's triples are clean-clean anchors, not a random 3.7% |

The skew row is why `B+_random` exists. D's 8,702 triples come only from anchors whose partner and
negative all parse cleanly — cards that are also the most-played (267 vs 45 mean train appearances
at query level). Without that row, `B+_matched − D` would still be confounded, just less visibly.

## What this does not license

**These numbers do not transfer to cold-start.** The clean-only universe is 46.6% high-play — the
memorization regime, where structure helps (B+ beats A by +0.0148 on high-play cards) and where a
popularity ranking alone reaches 0.6481. T4's actual conclusion lives on the cold-start stratum,
where structure is monotonically harmful and popularity scores exactly 0.0000. Nothing here speaks
to that.

They are also **not comparable to the ladder's clean stratum**, which scored clean-parse targets
against the full 30,958-card pool rather than a clean-only candidate set.
"""


def main(force: bool = False) -> dict:
    root = config.RUNS_DIR / "t4"
    findings_dir = config.FINDINGS_DIR
    from .t4_diagnostics import load_basis
    l0 = load_basis(root)

    rows = {}
    for seed in SEEDS:
        p = partial_path(seed, findings_dir)
        if p.exists() and not force:
            rows[seed] = json.loads(p.read_text(encoding="utf-8"))
            continue
        rec = run_seed(seed, l0, root, findings_dir, force=force)
        p.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        rows[seed] = rec
        print(f"  [s{seed}] matched {rec['B+_matched']['recall_at_50']:.4f}  "
              f"random {rec['B+_random']['recall_at_50']:.4f}")

    ref = reference(findings_dir)
    payload = report.envelope([config.CARDS_PARQUET], {"seeds": list(SEEDS)})
    payload.update({"runs": rows, "reference": ref})
    report.write("t4_matched_data", payload, render(rows, ref))
    return {"runs": rows, "reference": ref}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    r = main(force=ap.parse_args().force)
    ref = r["reference"]
    ms = [x["B+_matched"]["recall_at_50"] for x in r["runs"].values()]
    rs = [x["B+_random"]["recall_at_50"] for x in r["runs"].values()]
    print(f"\n  B+ full     {ref['B+_full']['n_examples']:>7,} ex  {ref['B+_full']['recall_at_50']:.4f}")
    print(f"  B+_matched  {r['runs'][SEEDS[0]]['n_matched_examples']:>7,} ex  {np.mean(ms):.4f}")
    print(f"  B+_random   {r['runs'][SEEDS[0]]['n_matched_examples']:>7,} ex  {np.mean(rs):.4f}")
    print(f"  D           {ref['D']['n_examples']:>7,} ex  {ref['D']['recall_at_50']:.4f}")
    print(f"\n  encoding (matched - D) {np.mean(ms)-ref['D']['recall_at_50']:+.4f}   "
          f"volume (full - matched) {ref['B+_full']['recall_at_50']-np.mean(ms):+.4f}   "
          f"skew (matched - random) {np.mean(ms)-np.mean(rs):+.4f}")
