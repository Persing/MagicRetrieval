"""Why arm C loses: how far the embedding space split, and what CDL cards displaced.

    uv run python -m mr.t4_diagnostics

Two analyses, both from cached embeddings, no training.

**D1 — how large is the partition, in the units retrieval uses.** Arm C serializes clean-parse
cards as canonical CDL and everything else as B+ text. The obvious test — can a probe tell the two
apart — is very nearly a tautology: CDL *is* a different language with a different token
distribution, so any competent encoder separates it (measured AUC 0.9977 for C against 0.8185 for
B+, where the same label is only card properties correlating with parse difficulty). The question
that matters is not whether the dialects are distinguishable but whether the partition depresses
cross-dialect similarity enough to move rankings. That is a cosine measurement, and it is reported
as the headline with the probe demoted to a sanity row.

**D2 — did C's apparent gain on gapped cards come from displacement.** On gap-status targets C and
B+ serialize the held-out card identically, yet C scores slightly *higher* (+0.0023, real). Since
the target's own encoding is unchanged, the only channels are the query vector and the competitor
pool — both of which contain clean cards that C encodes worse. Rank clean cards down and top-50
slots mechanically free up for everything else. That is displacement, not benefit, and the test is
whether C's top-50 for gap targets holds fewer clean cards than B+'s, against the arm-independent
share available.

**Neither separates "the space partitioned" from "CDL is simply worse text."** A model that merely
encodes CDL badly — no partition at all — produces the identical displacement signature. The
discriminating experiment is `t4_matched_data`, which holds training volume fixed and varies only
the encoding. Both reports say so rather than claiming "dialect" as established.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import config, corpus, loo_eval, report, representations, t4_representation as T

ARMS = ("A", "B", "B_type", "B+", "C")
SEEDS = (42, 43, 44)


# ── shared plumbing ───────────────────────────────────────────────────────────

def load_basis(root: Path | None = None) -> dict:
    """Layer-0, with a hard guarantee it came from cache.

    A silent rebuild would re-mine positives and rebuild queries, and every number here would then
    describe a basis the cached embeddings never saw — the same class of failure as an arm quietly
    missing from a table: nothing errors, the numbers are just about something else.
    """
    root = root or config.RUNS_DIR / "t4"
    l0 = T.build_layer0("casual", corpus.load_cards_parquet(), representations.load(),
                        config.SEED, root)
    if not l0["meta"].get("from_cache"):
        raise RuntimeError("layer0 was rebuilt, not loaded from cache — refusing to describe a "
                           "basis the cached embeddings never trained against.")
    return l0


def available_runs(l0: dict, root: Path, arms=ARMS, seeds=SEEDS) -> list[tuple[str, int]]:
    """Discovered from disk, so completing the grid to 5 seeds needs no edit here."""
    return [(a, s) for a in arms for s in seeds
            if T.embedding_path("casual", a, s, l0, root).exists()]


def summary(v: dict[int, float]) -> dict:
    vals = [v[s] for s in sorted(v)]
    return {"n_seeds": len(vals), "mean": float(np.mean(vals)),
            "sd": float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0,
            "by_seed": {str(s): v[s] for s in sorted(v)}}


DIAGNOSTIC_BANNER = (
    "> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated here and no verdict\n"
    "> in `findings/t4_representation.md` is affected. These analyses were specified after seeing\n"
    "> the T4 result and have no pre-committed criteria, so they explain a result rather than\n"
    "> deciding one.\n")


# ── D1: partition size ────────────────────────────────────────────────────────

def partition_geometry(E: np.ndarray, is_cdl: np.ndarray) -> dict:
    """Mean within- and across-dialect cosine, in closed form.

    Rows are L2-normalized, so the sum of all pairwise dot products inside a set is
    `‖Σx‖² − n` — exact, O(n·d), and it avoids materializing a 31k × 31k similarity matrix.
    """
    A, B = E[is_cdl], E[~is_cdl]
    nA, nB = len(A), len(B)
    SA, SB = A.sum(0), B.sum(0)
    within_A = (float(SA @ SA) - nA) / (nA * (nA - 1))
    within_B = (float(SB @ SB) - nB) / (nB * (nB - 1))
    across = float(SA @ SB) / (nA * nB)
    # Pair-count weighting: the pooled within-figure must be the mean over within-pairs, not the
    # mean of two group means, or the smaller group is silently upweighted.
    pA, pB = nA * (nA - 1), nB * (nB - 1)
    within = (within_A * pA + within_B * pB) / (pA + pB)
    return {"within_cdl": within_A, "within_fallback": within_B, "within_pooled": within,
            "across": across, "dialect_gap": within - across,
            "centroid_distance": float(np.linalg.norm(SA / nA - SB / nB))}


def probe_auc(E: np.ndarray, y: np.ndarray, seed: int) -> float:
    """Pooled out-of-fold ROC-AUC. A sanity row only — see the module docstring for why this is
    close to a tautology on arm C."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    oof = np.zeros(len(y))
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=seed).split(E, y):
        m = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=1000))
        m.fit(E[tr], y[tr])
        oof[te] = m.predict_proba(E[te])[:, 1]
    return float(roc_auc_score(y, oof))


def knn_dialect_share(E: np.ndarray, is_cdl: np.ndarray, k: int = 25) -> dict:
    """Share of a card's k nearest neighbours that are CDL-serialized.

    The non-linearity check: a space split into many small mono-dialect clumps would look modest to
    a single hyperplane and obvious here. Reported against the base rate, so "a CDL card's
    neighbourhood is X% CDL when chance is 34%" is readable directly.
    """
    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    X = torch.as_tensor(E, dtype=torch.float32, device=dev)
    lab = torch.as_tensor(is_cdl.astype(np.float32), device=dev)
    shares = torch.empty(len(E), device=dev)
    for lo in range(0, len(E), 2048):
        hi = min(lo + 2048, len(E))
        s = X[lo:hi] @ X.T
        s[torch.arange(hi - lo, device=dev), torch.arange(lo, hi, device=dev)] = -2.0  # drop self
        shares[lo:hi] = lab[s.topk(k, dim=1).indices].mean(1)
    sh = shares.cpu().numpy()
    return {"k": k, "base_rate": float(is_cdl.mean()),
            "mean_share_for_cdl": float(sh[is_cdl].mean()),
            "mean_share_for_fallback": float(sh[~is_cdl].mean())}


def run_d1(l0: dict, root: Path) -> dict:
    is_cdl = l0["clean_mask"]
    out: dict = {"n_cdl": int(is_cdl.sum()), "n_fallback": int((~is_cdl).sum()), "arms": {}}
    for arm in ARMS:
        geo, auc, knn = {}, {}, {}
        for seed in SEEDS:
            p = T.embedding_path("casual", arm, seed, l0, root)
            if not p.exists():
                continue
            E = np.load(p)
            g = partition_geometry(E, is_cdl)
            for key, val in g.items():
                geo.setdefault(key, {})[seed] = val
            auc[seed] = probe_auc(E, is_cdl.astype(int), seed)
            if seed == SEEDS[0]:
                knn = knn_dialect_share(E, is_cdl)
        if not geo:
            continue
        out["arms"][arm] = {"geometry": {k: summary(v) for k, v in geo.items()},
                            "probe_auc": summary(auc), "knn": knn}
    return out


# ── D2: displacement ──────────────────────────────────────────────────────────

def eligible_clean_share(l0: dict) -> np.ndarray:
    """Per query, the share of *eligible* candidates that are clean-parse.

    The null D2 needs. A ranker blind to dialect would draw clean cards into its top-50 at roughly
    this rate, so "fewer clean cards" only means something against it. Arm-independent by
    construction — it is a function of the colour mask and the context, never of an embedding.
    """
    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    qs = l0["queries"]
    masks = torch.as_tensor(qs.mask_table, device=dev)
    clean = torch.as_tensor(l0["clean_mask"], device=dev)
    ctx_flat = torch.as_tensor(qs.ctx_flat, device=dev)
    mask_idx = torch.as_tensor(qs.mask_idx, device=dev)
    out = np.empty(len(qs), dtype=np.float32)
    for lo, hi in loo_eval._chunk_bounds(qs.ctx_offsets, loo_eval.MAX_CTX_ROWS_PER_CHUNK, 128):
        b = hi - lo
        off = qs.ctx_offsets[lo:hi + 1]
        seg = torch.repeat_interleave(torch.arange(b, device=dev),
                                      torch.as_tensor(np.diff(off), device=dev))
        elig = masks[mask_idx[lo:hi]].clone()
        elig[seg, ctx_flat[int(off[0]):int(off[-1])]] = False
        out[lo:hi] = ((elig & clean).sum(1).float() / elig.sum(1).float()).cpu().numpy()
    return out


def run_d2(l0: dict, root: Path, k: int = 50) -> dict:
    qs = l0["queries"]
    clean = l0["clean_mask"]
    strata = l0["strata"].set_index("oracle_id")
    tgt_status = strata["parse_status"].reindex(
        [l0["oid_order"][i] for i in qs.target_row]).fillna("unknown").to_numpy()
    avail = eligible_clean_share(l0)

    tops: dict[tuple[str, int], np.ndarray] = {}
    hits: dict[tuple[str, int], np.ndarray] = {}
    for arm in ("B+", "C"):
        for seed in SEEDS:
            p = T.embedding_path("casual", arm, seed, l0, root)
            if not p.exists():
                continue
            top, rank = loo_eval.top_candidates(np.load(p), qs, k=k)
            tops[(arm, seed)] = top
            hits[(arm, seed)] = rank <= k

    def composition(mask: np.ndarray) -> dict:
        """Mean clean cards per top-50, per arm, plus the arm-independent availability."""
        res = {"n_queries": int(mask.sum()),
               "available_clean_share": float(avail[mask].mean())}
        for arm in ("B+", "C"):
            per = {s: float(clean[tops[(arm, s)][mask]].mean())
                   for s in SEEDS if (arm, s) in tops}
            res[arm] = summary(per)
        res["C_minus_Bplus"] = res["C"]["mean"] - res["B+"]["mean"]
        return res

    by_status = {st: composition(tgt_status == st) for st in ("clean", "gap", "excluded")}

    # Conservation: a top-k holds exactly k slots, so whatever C takes away from clean cards it
    # must hand to non-clean ones. Checked rather than assumed — it is cheap, and a violation would
    # mean the two arms were scored over different candidate universes, which would invalidate
    # every number above it.
    gap_mask = tgt_status == "gap"
    slots = {}
    for name, member in (("clean", clean), ("non_clean", ~clean)):
        slots[name] = {arm: float(np.mean([member[tops[(arm, s)][gap_mask]].sum(1).mean()
                                           for s in SEEDS if (arm, s) in tops]))
                       for arm in ("B+", "C")}
    conserved = abs(sum(slots[n]["C"] - slots[n]["B+"] for n in slots)) < 1e-6

    # Paired flip decomposition on gap targets: where C gains a hit, what did it evict?
    flips = {}
    for seed in SEEDS:
        if ("C", seed) not in hits:
            continue
        hb, hc = hits[("B+", seed)][gap_mask], hits[("C", seed)][gap_mask]
        tb, tc = tops[("B+", seed)][gap_mask], tops[("C", seed)][gap_mask]
        gained, lost = (~hb) & hc, hb & (~hc)
        ev = []
        for i in np.flatnonzero(gained):
            displaced = np.setdiff1d(tb[i], tc[i], assume_unique=False)
            if len(displaced):
                ev.append(clean[displaced].mean())
        flips[seed] = {
            "n_gained": int(gained.sum()), "n_lost": int(lost.sum()),
            "net": int(gained.sum() - lost.sum()),
            "clean_share_of_displaced": float(np.mean(ev)) if ev else None,
            "clean_share_of_Bplus_top50": float(clean[tb].mean()),
        }
    return {"k": k, "by_target_parse_status": by_status, "gap_topk_slots": slots,
            "slots_conserved": bool(conserved), "flip_decomposition": flips}


# ── report ────────────────────────────────────────────────────────────────────

def render(d1: dict, d2: dict) -> str:
    g = "\n".join(
        f"| {arm} | {v['geometry']['within_pooled']['mean']:.4f} | {v['geometry']['across']['mean']:.4f} "
        f"| **{v['geometry']['dialect_gap']['mean']:.4f}** | {v['geometry']['centroid_distance']['mean']:.4f} "
        f"| {v['probe_auc']['mean']:.4f} | {v['knn'].get('mean_share_for_cdl', float('nan')):.3f} |"
        for arm, v in d1["arms"].items())
    c = d2["by_target_parse_status"]
    comp = "\n".join(
        f"| {st} | {v['n_queries']:,} | {v['available_clean_share']:.3f} | {v['B+']['mean']:.3f} "
        f"| {v['C']['mean']:.3f} | **{v['C_minus_Bplus']:+.3f}** |"
        for st, v in c.items())
    def _frow(seed: int, v: dict) -> str:
        disp = v["clean_share_of_displaced"]
        disp_txt = "—" if disp is None else f"{disp:.3f}"
        return (f"| {seed} | {v['n_gained']:,} | {v['n_lost']:,} | {v['net']:+,} "
                f"| {disp_txt} | {v['clean_share_of_Bplus_top50']:.3f} |")

    frow = "\n".join(_frow(s, v) for s, v in d2["flip_decomposition"].items())
    knn_base = next(iter(d1["arms"].values()))["knn"].get("base_rate", float("nan"))

    return f"""# T4 — why arm C loses: partition size and displacement

{report.confound_header()}
{DIAGNOSTIC_BANNER}
## D1 — how large is the partition

Arm C serializes {d1['n_cdl']:,} clean-parse cards as canonical CDL and {d1['n_fallback']:,} as B+
text. Every other arm uses one encoding throughout, so their rows are the control: the same label,
measured on a space that cannot have partitioned by dialect.

| arm | within-dialect | across-dialect | gap | centroid dist. | probe AUC | kNN CDL share |
|---|---|---|---|---|---|---|
{g}

**Read the gap column, not the AUC.** The probe question — can the dialects be told apart — is
close to a tautology: CDL is a different language with a different token distribution, so any
competent encoder separates it. What matters for retrieval is whether the partition depresses
cross-dialect similarity enough to reorder a top-50, and that is the cosine gap. kNN base rate is
{knn_base:.3f}.

## D2 — did C's gap-stratum gain come from displacement

On gap-status targets, C and B+ serialize the held-out card **identically**. C nonetheless scores
+0.0023 there. With the target's own encoding fixed, the only channels are the query vector and the
competitor pool — both of which contain clean cards that C encodes worse. Ranking clean cards down
frees top-50 slots for everything else.

Mean clean cards per top-{d2['k']}, against the arm-independent share available:

| target status | queries | available | B+ | C | C − B+ |
|---|---|---|---|---|---|
{comp}

Slots conserved (whatever C takes from one status it gives to another): **{d2['slots_conserved']}**.

### Where C gains on gap targets, what did it evict?

| seed | gained | lost | net | clean share of displaced | clean share of B+ top-50 |
|---|---|---|---|---|---|
{frow}

If the displaced cards are more clean-enriched than B+'s top-50 as a whole, C's gains on gapped
cards were bought by evicting clean cards it ranks worse — displacement, not benefit.

## What neither analysis shows

**Neither separates "the space partitioned into two dialects" from "CDL is simply worse text."** A
model that merely encodes CDL badly, with no partition at all, produces the identical displacement
signature and a similar cosine gap. The discriminating experiment is `findings/t4_matched_data.md`,
which holds training volume fixed and varies only the encoding.
"""


def main(root: Path | None = None) -> dict:
    root = root or config.RUNS_DIR / "t4"
    l0 = load_basis(root)
    d1 = run_d1(l0, root)
    d2 = run_d2(l0, root)
    payload = report.envelope([config.CARDS_PARQUET],
                              {"runs": [f"{a}/s{s}" for a, s in available_runs(l0, root)]})
    payload.update({"d1_partition": d1, "d2_displacement": d2})
    report.write("t4_diagnostics", payload, render(d1, d2))
    return {"d1": d1, "d2": d2}


if __name__ == "__main__":
    r = main()
    print("D1 dialect gap (within - across cosine):")
    for arm, v in r["d1"]["arms"].items():
        print(f"  {arm:7s} gap {v['geometry']['dialect_gap']['mean']:.4f}  "
              f"across {v['geometry']['across']['mean']:.4f}  AUC {v['probe_auc']['mean']:.4f}")
    print("\nD2 clean cards in top-50 by target status (available / B+ / C):")
    for st, v in r["d2"]["by_target_parse_status"].items():
        print(f"  {st:9s} n={v['n_queries']:>6,}  avail {v['available_clean_share']:.3f}  "
              f"B+ {v['B+']['mean']:.3f}  C {v['C']['mean']:.3f}  delta {v['C_minus_Bplus']:+.3f}")
    print(f"\n  slots conserved: {r['d2']['slots_conserved']}")
    for s, v in r["d2"]["flip_decomposition"].items():
        print(f"  seed {s}: gained {v['n_gained']:,} lost {v['n_lost']:,} net {v['net']:+,}  "
              f"displaced-clean {v['clean_share_of_displaced']}  B+top50-clean {v['clean_share_of_Bplus_top50']:.3f}")
