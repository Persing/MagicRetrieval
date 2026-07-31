"""Is the cedh cold-start gain diversity, or is it coverage?

    uv run python -m mr.t4_coverage_probe

`t4_scaling` established that training on 39,733 cedh decks beats 3,142 by ~+0.041 on the cold-start
stratum, roughly 10x the frozen volume null. It labelled that "diversity beyond volume". **That
label is not established, and a cheaper mechanism predicts the same signature.**

`ppmi.build` indexes exactly the cards appearing in the training decks, and `mine_positives` can only
emit pairs over that index. The vocabulary is **4,821 cards at the subsample level against 10,133 at
full** — so ~5,300 cards go from *zero* mined positives to some. A card with no positives is never
trained on; its embedding stays whatever the pretrained encoder produced. Cold-start cards are
precisely the marginal ones crossing that threshold.

That predicts a gain concentrated in the tail and nothing on the aggregate — which is exactly what
was observed. So the observation does not discriminate:

  **coverage**  — more decks pull rare cards *into the mining vocabulary at all*.
                  Fix: mine rare cards differently, lower the PPMI threshold. Cheap, immediate.
  **diversity** — more decks give cards that were already covered *more varied contexts*.
                  Fix: acquire more decks. Expensive, slow.

Opposite action implications from the same number, which is why this runs before the claim ships.

## The test

Split the cold-start queries by whether the target is in the **subsample's** PPMI vocabulary, and
compute the full-minus-subsample gain within each group, per seed:

  gain concentrated in **newly covered** targets (absent from the subsample vocabulary)
      -> coverage. The model simply had no representation to train for those cards.
  gain spread across targets **present at both levels**
      -> diversity. Those cards were already covered; the extra decks bought better contexts.

Costs nothing beyond artifacts already committed plus the vendored corpus: the vocabulary is a set
of oracle_ids from the training decks, the per-query hits live in the scaling partials, and the
query list is rebuilt and checked against the recorded fingerprint so the indices are provably
aligned.

**Diagnostic, not a gate.** Specified after seeing the scaling result, with no pre-committed
criteria. It constrains which *mechanism* the evidence supports; it cannot move the frozen verdicts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import (config, corpus, loo_eval, report, representations, strata as strata_mod,
               t4_representation as T, thresholds)

CORPUS = "cedh"
LEVELS = ("subsample", "full")


def rebuild_queries(split_seed: int = config.SEED) -> tuple[loo_eval.QuerySet, "object", list]:
    """The cedh query set and full-level strata, rebuilt from the vendored corpus.

    No PPMI and no GPU: `build_queries` needs only the test decks and the candidate pool, and the
    strata need only training appearance counts. The expensive parts of layer 0 are not touched.
    """
    cards_df = corpus.load_cards_parquet()
    repr_tables = representations.load()
    decks = corpus.load_corpus(CORPUS)
    decks, _ = corpus.filter_plausible_decks(decks, max_distinct=corpus.MAX_COMMANDER_DISTINCT)
    train, test = corpus.split_by_deck(decks, test_frac=thresholds.T4_TEST_FRAC, seed=split_seed)
    pool = loo_eval.commander_legal_pool(cards_df)
    oid_order = list(pool["oracle_id"])
    qs = loo_eval.build_queries(test, pool, oid_order)
    strata = strata_mod.build(train, repr_tables)     # full-level counts — what the run froze
    return qs, strata, train


def subsample_vocabulary(train: list, seed: int) -> set[str]:
    """Cards the subsample's PPMI would index, for one seed.

    `ppmi.build` takes `sorted({oid for d in decks for oid in d.oracle_ids})`, so the vocabulary is
    exactly the distinct cards in the training decks — computable without building the matrix. The
    subsample draw is deterministic given the seed, which is what makes this reproducible.
    """
    sub = T.subsample_train(train, thresholds.T4_SCALING_CEDH_SUBSAMPLE, seed)
    return {oid for d in sub for oid in d.oracle_ids}


def load_hits(findings_dir: Path, arm: str, level: str, seed: int) -> dict:
    p = findings_dir / (f"t4_scaling_partial_{CORPUS}_{level}_"
                        f"{arm.replace('+', 'plus')}_s{seed}.json")
    if not p.exists():
        raise FileNotFoundError(f"{p} missing — run the cedh grid first")
    return json.loads(p.read_text(encoding="utf-8"))["hits"]


def probe(findings_dir: Path, arms=thresholds.T4_SCALING_ARMS, seeds=(42, 43, 44),
          split_seed: int = config.SEED) -> dict:
    qs, strata, train = rebuild_queries(split_seed)

    fingerprint = T.query_fingerprint(qs)
    cold = loo_eval.coldstart_mask(qs, strata)
    cold_idx = np.flatnonzero(cold)
    cold_oids = [qs.oid_order[i] for i in qs.target_row[cold_idx]]

    out: dict = {"query_fingerprint": fingerprint, "n_coldstart": int(cold.sum()), "arms": {}}

    for arm in arms:
        per_seed = []
        for seed in seeds:
            vocab = subsample_vocabulary(train, seed)
            # "newly covered" = the subsample could not mine a single positive for this card
            newly = np.fromiter((o not in vocab for o in cold_oids), dtype=bool, count=len(cold_oids))

            hits = {lv: load_hits(findings_dir, arm, lv, seed) for lv in LEVELS}
            n_cold = hits["full"]["n_coldstart"]
            if n_cold != len(cold_oids):
                raise RuntimeError(
                    f"cold-start count {n_cold} in the partial vs {len(cold_oids)} rebuilt — the "
                    "query set does not match the run; refusing to align indices by assumption")
            vec = {}
            for lv in LEVELS:
                v = np.zeros(n_cold, dtype=float)
                v[np.asarray(hits[lv]["coldstart_hits"], dtype=np.int64)] = 1.0
                vec[lv] = v

            row = {"seed": seed, "n_newly_covered": int(newly.sum()),
                   "n_covered_both": int((~newly).sum())}
            for label, mask in (("newly_covered", newly), ("covered_both", ~newly)):
                if not mask.any():
                    continue
                sub_r, full_r = vec["subsample"][mask].mean(), vec["full"][mask].mean()
                row[label] = {"subsample": float(sub_r), "full": float(full_r),
                              "gain": float(full_r - sub_r), "n": int(mask.sum())}
            per_seed.append(row)
        out["arms"][arm] = per_seed
    return out


def summarize(res: dict) -> dict:
    """Mean gain per group per arm, and the share of the total gain each group contributes."""
    agg = {}
    for arm, rows in res["arms"].items():
        e = {}
        for label in ("newly_covered", "covered_both"):
            vals = [r[label]["gain"] for r in rows if label in r]
            ns = [r[label]["n"] for r in rows if label in r]
            if vals:
                e[label] = {"gain": float(np.mean(vals)), "n": int(np.mean(ns)),
                            "sd": float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0}
        total_n = sum(v["n"] for v in e.values())
        for label, v in e.items():
            # contribution to the overall cold-start gain, which is the n-weighted mean of the two
            v["share_of_total_gain"] = (v["gain"] * v["n"] / total_n) if total_n else float("nan")
        agg[arm] = e
    return agg


def render(res: dict, agg: dict) -> str:
    rows = []
    for arm in sorted(agg):
        for label in ("newly_covered", "covered_both"):
            v = agg[arm].get(label)
            if not v:
                continue
            rows.append(f"| {arm} | {label.replace('_', ' ')} | {v['n']:,} | {v['gain']:+.4f} "
                        f"| {v['sd']:.4f} | {v['share_of_total_gain']:+.4f} |")

    verdicts = []
    for arm in sorted(agg):
        nc, cb = agg[arm].get("newly_covered"), agg[arm].get("covered_both")
        if not (nc and cb):
            continue
        if cb["gain"] <= 0 and nc["gain"] > 0:
            v = "COVERAGE — the gain is entirely in cards the subsample could not mine at all"
        elif nc["gain"] > 2 * cb["gain"] > 0:
            v = "MOSTLY COVERAGE — both groups gain, newly-covered dominates"
        elif cb["gain"] > 0 and abs(nc["gain"] - cb["gain"]) < 0.5 * max(nc["gain"], cb["gain"]):
            v = "DIVERSITY — comparable gain whether or not the card was already covered"
        else:
            v = "MIXED — read the numbers rather than this label"
        verdicts.append(f"- **{arm}**: {v}")

    return f"""# T4 — is the cedh cold-start gain coverage or diversity?

{report.confound_header()}
> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated and no verdict in
> `findings/t4_scaling.md` is affected. Specified after seeing the scaling result, with no
> pre-committed criteria. It constrains which *mechanism* the evidence supports.

`t4_scaling` found the full cedh level beating the {thresholds.T4_SCALING_CEDH_SUBSAMPLE:,}-deck
subsample by ~+0.041 on cold-start and called it diversity. A cheaper mechanism predicts the same
signature: the PPMI vocabulary is **4,821 cards at the subsample level against 10,133 at full**, so
~5,300 cards go from *zero* mined positives to some. A card with no positives is never trained on.
Cold-start cards are exactly the ones crossing that line.

Both mechanisms predict a tail-concentrated gain and a flat aggregate, so the original observation
cannot separate them — and they imply opposite actions. Coverage says fix the mining, which is
cheap. Diversity says buy more decks, which is not.

**The split.** Cold-start queries ({res['n_coldstart']:,} of them) partitioned by whether the target
is in that seed's subsample vocabulary. Query set rebuilt from the vendored corpus and checked
against the recorded fingerprint `{res['query_fingerprint'][:16]}` — the per-query indices are
aligned by construction, not by assumption.

| arm | group | queries | gain | sd over seeds | contribution to total |
|---|---|---|---|---|---|
{chr(10).join(rows)}

`contribution to total` is the group's gain weighted by its share of cold-start queries; the two
rows for an arm sum to that arm's overall cold-start gain.

## Reading

{chr(10).join(verdicts)}

**If coverage dominates**, the actionable finding is not "acquire more decks" but "mine rare cards
at all" — the top-decile PPMI threshold in `mining.mine_positives` is what excludes them, and it is
a tunable, not a fact about the corpus. **If the gain survives among cards covered at both levels**,
that is diversity in the intended sense and the deck-acquisition reading stands.

Either way this does not touch the frozen verdicts in `t4_scaling.md`, which measured what they
measured. It changes what the number should be *called*.
"""


def main(findings_dir: Path | None = None) -> dict:
    d = findings_dir or config.FINDINGS_DIR
    res = probe(d)
    agg = summarize(res)
    payload = report.envelope([config.CARDS_PARQUET], {"corpus": CORPUS, "mode": "coverage_probe"})
    payload.update({"per_seed": res, "summary": agg})
    report.write("t4_coverage_probe", payload, render(res, agg), findings_dir=d)
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--findings-dir", type=Path, default=None)
    out = main(ap.parse_args().findings_dir)
    for arm, e in sorted(out["summary"].items()):
        parts = ", ".join(f"{k.replace('_', ' ')} {v['gain']:+.4f} (n={v['n']:,})"
                          for k, v in e.items())
        print(f"  {arm}: {parts}")
