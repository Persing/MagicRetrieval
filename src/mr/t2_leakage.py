"""T2 — text-geometry leakage retest.

Question: was the substitute-pair result (0.717 substitutes vs 0.748 random same-cluster,
MagicSpike `Gate_1_3_Findings.md:82`) a training deficiency or structural?

The ladder isolates one variable per rung: 2a is precon-only (224 decks) with no negative
supervision — a reference point, NOT a harness-correctness gate (see THRESHOLDS.md: the original
0.717/0.748 corpus included "several hundred" early cEDH decks that no longer exist in isolated
form, so exact reproduction isn't achievable and a mismatch is expected). 2b adds the broad corpus.
2c wires same-colour hard negatives into the loss (MagicSpike computed these and never used them —
see `finetune.py`). 2d adds mined text-similar/co-occurrence-distant negatives and
text-distant/co-occurrence-close positives.

Both casual and cEDH run as separate corpus arms for 2b/2c/2d; 2a runs once on precon.

    uv run python -m mr.t2_leakage --smoke      # tiny run to check the harness end to end
    uv run python -m mr.t2_leakage              # full ladder, both corpora
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, corpus, finetune, mining, ppmi as ppmi_mod, report, thresholds

RUNS_DIR = config.RUNS_DIR
RANDOM_PAIRS_PER_EVAL_PAIR = 3
BOOTSTRAP_N = thresholds.T2_BOOTSTRAP_N

# Applied by default (not just in --smoke). The casual corpus's 24,762-card vocabulary mines
# 1,288,988 top-decile positives — 6x cedh's 208,946 — and training on the uncapped set drove a
# single fine-tune to 15GB resident and system swap thrashing before it was killed. This ceiling
# sits above every arm actually run successfully so far (cedh's 208,946 is unaffected by it) while
# keeping casual's runs bounded. Capping is a random subsample (see `run_arm`), and the true mined
# count is always reported alongside the capped count — never a silent truncation.
DEFAULT_MAX_POSITIVES = 250_000


# ── eval set & baseline geometry ──────────────────────────────────────────────

def load_eval_pairs() -> list[tuple[str, str]]:
    path = config.DATA_DIR / "substitute_pairs_v2.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `python -m mr.build_probe_sets` first.")
    cards = corpus.load_cards_parquet()
    from . import probe_sets
    pairs, unresolved = probe_sets.load_pair_csv(path, cards, "eval")
    if unresolved:
        raise ValueError(f"Eval CSV has unresolved card names: {sorted(set(unresolved))}")
    return [(p.oracle_id_a, p.oracle_id_b) for p in pairs]


def load_frozen_clusters() -> dict[str, int]:
    """The k=50 clustering ALL arms are scored against — never each arm's own geometry.

    Using an arm's own fine-tuned embedding to define "same cluster" makes the control baseline
    move with the thing being measured, which is circular. This clustering predates every arm here.
    """
    df = pd.read_parquet(config.CLUSTER_ASSIGNMENTS)
    return dict(zip(df["oracle_id"], df["cluster"]))


def build_stratum_control(cards_df: pd.DataFrame) -> dict[str, str]:
    """Colour identity x CMC band x primary type. Independent of any embedding geometry at all —
    a second control that doesn't inherit the frozen clustering's assumptions either."""
    def primary_type(tl: str) -> str:
        tl = (tl or "").split("—")[0].lower()
        for t in ("creature", "instant", "sorcery", "enchantment", "artifact", "planeswalker", "land"):
            if t in tl:
                return t
        return "other"

    def cmc_band(cmc: float) -> str:
        if cmc <= 1:
            return "0-1"
        if cmc <= 3:
            return "2-3"
        if cmc <= 5:
            return "4-5"
        return "6+"

    out = {}
    for row in cards_df.itertuples(index=False):
        ci = "".join(sorted(row.color_identity)) if row.color_identity is not None and len(row.color_identity) else "C"
        out[row.oracle_id] = f"{ci}|{cmc_band(float(row.cmc or 0))}|{primary_type(row.type_line)}"
    return out


def sample_random_pairs(
    eval_pairs: list[tuple[str, str]],
    grouping: dict[str, int | str],
    vocab: set[str],
    seed: int,
    per_eval_pair: int = RANDOM_PAIRS_PER_EVAL_PAIR,
) -> list[tuple[str, str]]:
    """For each eval card present in `vocab`, sample same-group partners not equal to itself or
    its eval partner. Deterministic under `seed`."""
    by_group: dict = {}
    for oid, g in grouping.items():
        if oid in vocab:
            by_group.setdefault(g, []).append(oid)

    rng = np.random.default_rng(seed)
    out: list[tuple[str, str]] = []
    for a, b in eval_pairs:
        for anchor in (a, b):
            if anchor not in vocab or anchor not in grouping:
                continue
            pool = [o for o in by_group.get(grouping[anchor], []) if o not in (a, b)]
            if not pool:
                continue
            n = min(per_eval_pair, len(pool))
            for i in rng.choice(len(pool), size=n, replace=False):
                out.append((anchor, pool[i]))
    return out


# ── scoring ────────────────────────────────────────────────────────────────────

def cosine_for_pairs(embeddings: np.ndarray, oid_to_row: dict[str, int],
                     pairs: list[tuple[str, str]]) -> np.ndarray:
    vals = []
    for a, b in pairs:
        ia, ib = oid_to_row.get(a), oid_to_row.get(b)
        if ia is None or ib is None:
            continue
        vals.append(float(np.dot(embeddings[ia], embeddings[ib])))
    return np.array(vals, dtype=np.float64)


def paired_bootstrap_lift(sub: np.ndarray, rand: np.ndarray, n: int = BOOTSTRAP_N,
                          seed: int = config.SEED) -> tuple[float, float, float]:
    """(lift, ci_low, ci_high) for mean(sub) - mean(rand), each resampled independently."""
    if len(sub) == 0 or len(rand) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    lifts = np.empty(n)
    for i in range(n):
        s = rng.choice(sub, size=len(sub), replace=True)
        r = rng.choice(rand, size=len(rand), replace=True)
        lifts[i] = s.mean() - r.mean()
    lift = float(sub.mean() - rand.mean())
    lo, hi = np.percentile(lifts, [2.5, 97.5])
    return lift, float(lo), float(hi)


# ── the ladder ─────────────────────────────────────────────────────────────────

ARMS = ("2a", "2b", "2c", "2d")


def cap_positives(
    positives: list[tuple[str, str]], max_positives: int | None, seed: int = config.SEED
) -> list[tuple[str, str]]:
    """Random subsample down to `max_positives`, not a prefix slice.

    `mine_positives` iterates the sparse matrix's (row, col) order, which correlates with
    card_index/oracle_id ordering — truncating with `positives[:max_positives]` would silently
    favor whatever cards happen to sort first, not a representative sample. Discovered because the
    casual corpus (24,762-card vocabulary — 2-6x precon/cedh's, likely from resolving against
    Scryfall's full oracle set including a long tail of obscure cards) mined 1,288,988 positives
    and drove a single fine-tune to 15GB resident and system swap thrashing before it was killed.
    """
    if not max_positives or len(positives) <= max_positives:
        return positives
    rng = np.random.default_rng(seed)
    keep_idx = rng.choice(len(positives), size=max_positives, replace=False)
    return [positives[i] for i in keep_idx]


def run_arm(
    arm: str,
    corpus_name: str,
    decks: list[corpus.Deck],
    cards_df: pd.DataFrame,
    eval_pairs: list[tuple[str, str]],
    max_positives: int | None = None,
    epochs: int = config.FINETUNE_EPOCHS,
) -> dict:
    """Run one rung of the ladder. PPMI is always built fresh from `decks`.

    Earlier drafts of this arm reused `backup_precon_only/cooccurrence_ppmi.npz` directly for
    arm 2a. That file turned out to be byte-identical to the current combined-corpus PPMI —
    every artifact in that directory except `decks_precon.parquet` itself is, confirmed by md5 —
    so it was never actually a precon-only build; it is a mislabeled snapshot of the full tree.
    2a now builds PPMI from `corpus.load_precon()`'s 224 decks like every other arm.
    """
    pair_only_excl, touched = mining.eval_exclusion_sets(eval_pairs)
    mat, card_index = ppmi_mod.build(decks)

    positives, positives_strict = mining.mine_positives(mat, card_index, pair_only_excl, touched)
    n_mined_total = len(positives)
    positives = cap_positives(positives, max_positives, seed=config.SEED)
    if not positives:
        raise ValueError(f"[{arm}/{corpus_name}] no positive pairs mined")

    # Held out for the generalization guard: a slice of mined positives never trained on.
    rng = np.random.default_rng(config.SEED)
    perm = rng.permutation(len(positives))
    n_holdout = max(1, int(0.15 * len(positives)))
    train_idx = perm[n_holdout:]
    held_out_positives = [positives[i] for i in perm[:n_holdout]]
    train_positives = [positives[i] for i in train_idx]

    texts_by_oid = finetune.text_lookup(cards_df)
    oid_order = sorted(card_index, key=lambda o: card_index[o])

    negatives = None
    if arm in ("2c", "2d"):
        neg_pair_only, _ = mining.mine_hard_negatives(
            mat, card_index, cards_df, pair_only_excl, touched, set(map(frozenset, positives)))
        negatives = neg_pair_only

    extra_pos_used = extra_neg_used = 0
    if arm == "2d":
        zs = finetune.zero_shot_embeddings(oid_order, texts_by_oid)
        phen = mining.mine_phenotype_pairs(mat, card_index, zs, oid_order, pair_only_excl, touched)
        extra_pos, _ = phen["extra_positives"]
        extra_neg, _ = phen["extra_negatives"]
        train_positives = train_positives + extra_pos
        negatives = (negatives or []) + extra_neg
        extra_pos_used, extra_neg_used = len(extra_pos), len(extra_neg)

    # T2 is a single-seed test and stays on config.SEED. The keyword is now required precisely so
    # that this call site had to be visited when the seeded trainer path landed (see finetune.py).
    embeddings = finetune.finetune(train_positives, negatives, texts_by_oid, oid_order,
                                   seed=config.SEED, epochs=epochs)
    oid_to_row = {oid: i for i, oid in enumerate(oid_order)}

    vocab = set(oid_to_row)
    clusters = load_frozen_clusters()
    strata = build_stratum_control(cards_df)

    def score(pairs: list[tuple[str, str]], label_seed: int) -> dict:
        eval_in_vocab = [(a, b) for a, b in pairs if a in vocab and b in vocab]
        sub_cos = cosine_for_pairs(embeddings, oid_to_row, eval_in_vocab)

        rand_cluster = sample_random_pairs(eval_in_vocab, clusters, vocab, seed=label_seed)
        rand_stratum = sample_random_pairs(eval_in_vocab, strata, vocab, seed=label_seed + 1)
        rand_cos_cluster = cosine_for_pairs(embeddings, oid_to_row, rand_cluster)
        rand_cos_stratum = cosine_for_pairs(embeddings, oid_to_row, rand_stratum)

        lift_c, lo_c, hi_c = paired_bootstrap_lift(sub_cos, rand_cos_cluster, seed=label_seed)
        lift_s, lo_s, hi_s = paired_bootstrap_lift(sub_cos, rand_cos_stratum, seed=label_seed + 1)
        return {
            "n_pairs_in_vocab": len(eval_in_vocab),
            "mean_substitute_cosine": float(sub_cos.mean()) if len(sub_cos) else None,
            "mean_random_cluster_cosine": float(rand_cos_cluster.mean()) if len(rand_cos_cluster) else None,
            "mean_random_stratum_cosine": float(rand_cos_stratum.mean()) if len(rand_cos_stratum) else None,
            "lift_vs_cluster": lift_c, "lift_vs_cluster_ci": [lo_c, hi_c],
            "lift_vs_stratum": lift_s, "lift_vs_stratum_ci": [lo_s, hi_s],
        }

    curated = score(eval_pairs, label_seed=config.SEED)
    mined_holdout = score(held_out_positives, label_seed=config.SEED + 100) if held_out_positives else None

    lift, ci_low = curated["lift_vs_cluster"], curated["lift_vs_cluster_ci"][0]
    mined_lift = mined_holdout["lift_vs_cluster"] if mined_holdout else None
    verdict = thresholds.t2_verdict(lift, ci_low, mined_lift)

    # Observational annotation, NOT a verdict input — added after seeing 2a/2b both show this
    # pattern, so it is deliberately kept out of `t2_verdict`'s pre-committed PASS/FAIL/
    # PHENOTYPE_FIT logic rather than silently folded into it. The plan's generalization guard
    # only checks one direction (curated << mined -> the fine-tune learned the mining criterion's
    # surface pattern, not real substitution). The mirror direction — curated >> mined, using the
    # SAME already-frozen 0.5 ratio, symmetrically — says something different: the effect is real
    # on the 40-60 hand-picked substitute pairs but doesn't reach half that strength on a much
    # larger held-out sample of "cards that just co-occur a lot". That's plausibly expected (top-
    # decile PPMI is a broad, noisy relation; true substitutes are a narrow slice of it) rather
    # than a defect, but it changes what a PASS here is actually claiming, so it's surfaced
    # explicitly instead of being invisible inside a bare "PASS".
    narrow_effect = (
        mined_lift is not None and mined_lift < thresholds.T2_MIN_LIFT
        and mined_lift < thresholds.T2_GENERALIZATION_RATIO * lift
    )

    return {
        "arm": arm, "corpus": corpus_name,
        "n_decks": len(decks) if decks else None,
        "n_cards_vocab": len(card_index),
        "n_positives_mined_total": n_mined_total,
        "n_positives_mined": len(positives), "n_positives_strict": len(positives_strict),
        "n_positives_trained": len(train_positives), "n_negatives": len(negatives or []),
        "n_holdout_positives": len(held_out_positives),
        "n_extra_positives_2d": extra_pos_used, "n_extra_negatives_2d": extra_neg_used,
        "curated": curated,
        "mined_holdout": mined_holdout,
        "verdict": verdict,
        "narrow_effect": narrow_effect,
    }


def check_harness(arm2a: dict) -> dict:
    """Compare arm 2a against the historical 0.717/0.748 reference.

    Not a pass/fail gate: 2a's corpus (224 precon decks, no cEDH) is smaller than the corpus that
    produced the historical numbers (224 precon + "several hundred" early cEDH decks — a mixed
    corpus that no longer exists in isolated form, see THRESHOLDS.md). A mismatch here is expected
    and does not invalidate arms 2b/2c/2d; `matches_reference` is reported for context only.
    """
    t = thresholds
    sub = arm2a["curated"]["mean_substitute_cosine"]
    rand = arm2a["curated"]["mean_random_cluster_cosine"]
    matches = (
        sub is not None and rand is not None
        and abs(sub - t.T2_BASELINE_SUBSTITUTE) <= t.T2_BASELINE_TOL
        and abs(rand - t.T2_BASELINE_RANDOM) <= t.T2_BASELINE_TOL
    )
    return {"observed_substitute": sub, "observed_random": rand,
            "expected_substitute": t.T2_BASELINE_SUBSTITUTE, "expected_random": t.T2_BASELINE_RANDOM,
            "tolerance": t.T2_BASELINE_TOL, "matches_reference": matches,
            "corpus_caveat": ("2a is a precon-only rebuild (224 decks); the historical baseline's "
                              "corpus additionally included several hundred early cEDH decks that "
                              "no longer exist in isolated form. A mismatch is expected.")}


def render(results: list[dict], harness: dict) -> str:
    t = thresholds
    lines = [
        "# T2 — text-geometry leakage retest",
        "",
        report.confound_header(),
        "",
        "**Question:** was the substitute-pair result (0.717 vs 0.748, "
        "`Gate_1_3_Findings.md:82`) a training deficiency or structural?",
        "",
        f"**Gates:** lift ≥ +{report.pct(t.T2_MIN_LIFT)} with 95% CI excluding zero. "
        f"Generalization guard: curated lift < {t.T2_GENERALIZATION_RATIO}× mined-holdout lift → "
        "PHENOTYPE_FIT, not PASS.",
        "",
        "## Arm 2a vs. the historical reference (not a gate — see caveat)",
        "",
    ]
    if harness["observed_substitute"] is None:
        lines += [f"> {harness['corpus_caveat']}", ""]
    else:
        lines += [
            f"Observed: substitutes={harness['observed_substitute']:.4f}, "
            f"random={harness['observed_random']:.4f}. Historical reference "
            f"≈{harness['expected_substitute']} / ≈{harness['expected_random']} "
            f"(±{harness['tolerance']}). "
            f"**{'MATCHES REFERENCE' if harness['matches_reference'] else 'DOES NOT MATCH REFERENCE'}**",
            "",
            f"> {harness['corpus_caveat']}",
            "",
        ]
    ran = {(r["arm"], r["corpus"]) for r in results}
    expected = {("2a", "precon")} | {(a, c) for a in ("2b", "2c", "2d") for c in ("casual", "cedh")}
    missing = sorted(expected - ran)
    if missing:
        lines += [
            "## Scope of this run",
            "",
            f"**Partial ladder — {len(ran)}/{len(expected)} rungs run, stopped deliberately "
            "after 2b.** Missing: " + ", ".join(f"{a}/{c}" for a, c in missing) + ". 2c/2d "
            "(negative supervision, wired to the loss and mined-phenotype pairs respectively) "
            "were not run — the decision rules below that depend on them are unresolved, not "
            "failed. Re-run with `--arms 2c 2d` to complete the ladder.",
            "",
        ]
    lines += [
        "## Ladder results",
        "",
        "| Arm | Corpus | Sub. cosine | Random (cluster) | Lift | 95% CI | Mined-holdout lift | Verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    any_narrow = False
    for r in results:
        c = r["curated"]
        mh = r["mined_holdout"]
        ci = c["lift_vs_cluster_ci"]
        mined_lift_str = f"{mh['lift_vs_cluster']:+.4f}" if mh else "—"
        if r.get("narrow_effect"):
            mined_lift_str += " †"
            any_narrow = True
        lines.append(
            f"| {r['arm']} | {r['corpus']} | {c['mean_substitute_cosine']:.4f} | "
            f"{c['mean_random_cluster_cosine']:.4f} | {c['lift_vs_cluster']:+.4f} | "
            f"[{ci[0]:+.4f}, {ci[1]:+.4f}] | {mined_lift_str} | **{r['verdict']}** |"
        )
    if any_narrow:
        lines += [
            "",
            "† **Narrow effect** (observational annotation, not part of the frozen verdict gate — "
            "added after seeing this pattern in the data, so it deliberately does NOT change "
            "PASS/FAIL/PHENOTYPE_FIT). The mined-holdout lift is both below the +0.05 floor and "
            "below half the curated lift, using the same 0.5 ratio the generalization guard "
            "already commits to, applied symmetrically. Reads as: the effect is real on the "
            "hand-picked substitute pairs but does not reach the same bar on a much larger sample "
            "of cards that merely co-occur a lot. Plausibly expected — top-decile PPMI is a "
            "broader, noisier relation than true functional substitution — but it changes what "
            "a bare PASS is actually claiming, so it's called out rather than left implicit.",
        ]
    unresolved_tag = " *(unresolved — needs 2d)*" if missing else ""
    unresolved_tag_2c = " *(unresolved — needs 2c)*" if missing else ""
    lines += [
        "",
        "## Decision rules",
        "",
        f"- **2d passes**{unresolved_tag} → leakage was a training deficiency; branch is healthy, "
        "raw text is a viable fallback tier. Check PHENOTYPE_FIT before believing it.",
        f"- **2d fails, 2b > 2a**{unresolved_tag} → corpus was the driver; investigate mining "
        "quality. *(2b > 2a already holds on the curated metric for casual (+0.169 > +0.148) but "
        "not cedh (+0.117 < +0.148) — mixed on that reading alone; both no-negative arms show the "
        "same narrow-effect pattern on the broader mined-holdout metric, which weakens with "
        "corpus size rather than strengthening.)*",
        f"- **2c > 2b materially**{unresolved_tag_2c} → negative supervision was the missing "
        "piece — MagicSpike's Gate 2 conclusion needs amending.",
        f"- **2d fails outright**{unresolved_tag} → surface text is structurally load-bearing; "
        "canonicalized CDL becomes required, raising T4's stakes.",
        "",
        "Every arm is scored against the SAME frozen k=50 clustering "
        "(`artifacts/cluster_assignments.parquet`) and against an embedding-independent "
        "colour×CMC×type stratum control, so the baseline cannot move with the thing being "
        "measured.",
    ]
    if missing:
        lines += [
            "",
            "## Interim read (2a/2b only, stopped here deliberately)",
            "",
            "On the curated substitute metric alone, leakage looks fixed: all three completed arms "
            "clear the +0.05 lift floor with CIs excluding zero, a qualitatively different result "
            "from the original −0.031 (0.717 vs 0.748). But the narrow-effect pattern is "
            "consistent across every arm — including 2b, which already has the broad corpus — so "
            "corpus breadth alone is not what's driving it. Whether wiring hard negatives (2c) or "
            "mined phenotype pairs (2d) changes that pattern is exactly what the deferred rungs "
            "would answer, and is not decided by this run.",
        ]
    return "\n".join(lines) + "\n"


def _build_harness(results: list[dict]) -> dict:
    arm2a = next((r for r in results if r["arm"] == "2a"), None)
    if arm2a is None:
        return {
            "observed_substitute": None, "observed_random": None,
            "expected_substitute": thresholds.T2_BASELINE_SUBSTITUTE,
            "expected_random": thresholds.T2_BASELINE_RANDOM,
            "tolerance": thresholds.T2_BASELINE_TOL, "matches_reference": None,
            "corpus_caveat": "arm 2a was not run",
        }
    return check_harness(arm2a)


def merge_partials(findings_dir: Path | None = None, pattern: str = "t2_leakage_partial_*.json") -> dict:
    """Combine partial-run JSON files into the canonical `t2_leakage.json`/`.md`.

    Exists because two arms run as separate concurrent processes (e.g. `2b` on `casual` and `2b`
    on `cedh` at once, to avoid a multi-hour serial wait) would otherwise both call
    `report.write("t2_leakage", ...)` and race on the SAME output file — whichever finishes last
    silently overwrites the other's results. Running each with a distinct `--tag` writes to a
    distinct partial file instead; this merges them by (arm, corpus) key. A duplicate (arm, corpus)
    key across two partials is an error, not a silent overwrite — it means the same rung was run
    twice and it's not obvious which run should win.
    """
    out_dir = findings_dir or config.FINDINGS_DIR
    results: dict[tuple[str, str], dict] = {}
    sources: list[str] = []
    for path in sorted(out_dir.glob(pattern)):
        payload = json.loads(path.read_text())
        sources.append(str(path))
        for r in payload.get("results", []):
            key = (r["arm"], r["corpus"])
            if key in results:
                raise ValueError(
                    f"Duplicate (arm, corpus)={key} across partial files — "
                    f"found in both an earlier partial and {path}. Remove the stale one."
                )
            results[key] = r

    merged = list(results.values())
    harness = _build_harness(merged)
    payload = report.envelope(
        [config.CARDS_PARQUET, config.CENSUS_JSONL, config.DECKS_COMBINED,
         config.PRECON_BACKUP, config.CLUSTER_ASSIGNMENTS,
         config.DATA_DIR / "substitute_pairs_v2.csv"],
        {"merged_from": sources},
    )
    payload.update({"results": merged, "harness": harness})
    report.write("t2_leakage", payload, render(merged, harness), findings_dir=out_dir)
    return payload


def main(
    corpora: list[str] = ("casual", "cedh"),
    arms: list[str] = ("2a", "2b", "2c", "2d"),
    max_decks: int | None = None,
    max_positives: int | None = DEFAULT_MAX_POSITIVES,
    epochs: int = config.FINETUNE_EPOCHS,
    output_name: str = "t2_leakage",
) -> dict:
    """`output_name` lets concurrent invocations avoid colliding on the canonical findings file —
    pass a distinct name (e.g. via `--tag`) and combine with `merge_partials()` once all are done."""
    cards_df = corpus.load_cards_parquet()
    eval_pairs = load_eval_pairs()

    results = []
    if "2a" in arms:
        precon_decks = corpus.load_precon()
        results.append(run_arm("2a", "precon", precon_decks, cards_df, eval_pairs,
                               max_positives=max_positives, epochs=epochs))

    for corpus_name in corpora:
        decks = corpus.load_corpus(corpus_name, limit=max_decks)
        for arm in ("2b", "2c", "2d"):
            if arm not in arms:
                continue
            results.append(run_arm(arm, corpus_name, decks, cards_df, eval_pairs,
                                   max_positives=max_positives, epochs=epochs))

    harness = _build_harness(results)
    payload = report.envelope(
        [config.CARDS_PARQUET, config.CENSUS_JSONL, config.DECKS_COMBINED,
         config.PRECON_BACKUP, config.CLUSTER_ASSIGNMENTS,
         config.DATA_DIR / "substitute_pairs_v2.csv"],
        {"corpora": list(corpora), "arms": list(arms), "max_decks": max_decks,
         "max_positives": max_positives, "epochs": epochs},
    )
    payload.update({"results": results, "harness": harness})
    report.write(output_name, payload, render(results, harness))
    return payload


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", nargs="+", default=["casual", "cedh"], choices=["casual", "cedh"])
    p.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    p.add_argument("--smoke", action="store_true", help="tiny run: capped decks/positives/epochs")
    p.add_argument("--tag", default=None,
                   help="write to findings/t2_leakage_partial_<tag>.json instead of the canonical "
                        "file — use when running arms as separate CONCURRENT processes, then "
                        "combine with --merge once all are done")
    p.add_argument("--merge", action="store_true",
                   help="combine all findings/t2_leakage_partial_*.json into the canonical "
                        "t2_leakage.json/.md and exit (ignores every other flag)")
    p.add_argument("--max-positives", type=int, default=None,
                   help=f"override the default cap ({DEFAULT_MAX_POSITIVES:,}) on mined positives "
                        "per arm; pass 0 to disable capping entirely")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.merge:
        merged = merge_partials()
        print(f"merged {len(merged['results'])} result(s) into findings/t2_leakage.json")
        raise SystemExit(0)

    output_name = f"t2_leakage_partial_{args.tag}" if args.tag else "t2_leakage"
    if args.smoke:
        res = main(corpora=args.corpus, arms=args.arms, max_decks=300, max_positives=200,
                  epochs=1, output_name=output_name)
    else:
        max_positives = DEFAULT_MAX_POSITIVES
        if args.max_positives is not None:
            max_positives = args.max_positives or None  # 0 means uncapped
        res = main(corpora=args.corpus, arms=args.arms, max_positives=max_positives,
                  output_name=output_name)
    print(f"matches_reference={res['harness']['matches_reference']} "
          f"(not a gate — see harness['corpus_caveat'])")
    for r in res["results"]:
        print(f"{r['arm']}/{r['corpus']}: lift={r['curated']['lift_vs_cluster']:+.4f} "
              f"verdict={r['verdict']}")
