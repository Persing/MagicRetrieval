"""T4 — representation ablation. Six arms, one variable: how a card becomes a vector.

    uv run python -m mr.t4_representation --smoke
    uv run python -m mr.t4_representation --corpus casual --arms A B B_type B+ C D --seeds 42 43 44 45 46
    uv run python -m mr.t4_representation --merge

The decisive comparison is **C vs B+**: C is B+ with CDL swapped in wherever CDL parses cleanly, so
if C ≈ B+ then CDL adds nothing over clause tagging on the cards where CDL actually works. The
other arms exist so that a null there is attributable rather than ambiguous.

## Cache layout, and why the arm cannot leak upstream

    runs/t4/repr/                    layer 1  CDL + clause tables       (arm- and corpus-independent)
    runs/t4/<corpus>/split<seed>/    layer 0  split, PPMI, mined pairs, queries, strata
    runs/t4/emb/<corpus>_<arm>_s<seed>.npy    layer 3  embeddings
    findings/t4_partial_<corpus>_<arm>_s<seed>.json

Layer 0 is keyed on `(corpus, split_seed)` and **not** on the arm. That is a correctness device
before it is a speed one: PPMI, the mined positives and negatives, the train/test split, the query
list and every stratum label are computed before any arm is named, so "same corpus, split,
negatives and objective across all arms" is true by construction rather than by discipline. It is
also a large speed win — `mine_hard_negatives` densifies rows of a ~24k-wide PPMI for 4,000
anchors, and T2 paid that cost inside every single arm.

**PPMI is built from training decks only.** T2 built it from every deck in the corpus, which is
harmless there (T2 evaluates on card pairs) and fatal here: the held-out decks would be part of the
co-occurrence signal that trains the encoder being evaluated on them.

## Arm D is only meaningful inside the clean-parse universe

D has no text for cards CDL cannot parse, so ranking it against the full pool compares a model that
knows 10,637 cards against models that know 31,041. Every arm is therefore *also* scored with both
candidates and targets restricted to clean-parse cards, and `D − C` is read only there.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import (config, corpus, encodings, finetune, loo_eval, mining, ppmi as ppmi_mod,
               report, representations, strata as strata_mod, thresholds)

DEFAULT_ARMS = encodings.ARMS
DEFAULT_SEEDS = (42, 43, 44, 45, 46)
SMOKE_DECKS = 200


# ── layer 0: everything that must be identical across arms ───────────────────

def layer0_dir(corpus_name: str, split_seed: int, root: Path) -> Path:
    return root / corpus_name / f"split{split_seed}"


def _pairs_to_frame(pairs: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(pairs, columns=["a", "b"]) if pairs else pd.DataFrame(columns=["a", "b"])


def build_layer0(corpus_name: str, cards_df: pd.DataFrame, repr_tables: dict,
                 split_seed: int, root: Path, limit: int | None = None,
                 max_positives: int | None = None, max_test_decks: int | None = None,
                 force: bool = False) -> dict:
    """Split, PPMI, mined pairs, queries and strata — computed once, reused by every arm.

    Cached to disk as well as within a run. `mine_hard_negatives` densifies PPMI rows for 4,000
    anchors and takes minutes; without persistence, splitting the grid across sessions would pay
    that cost again each time, and — worse for correctness — would recompute the pairs every arm is
    supposed to share.
    """
    d = layer0_dir(corpus_name, split_seed, root)
    d.mkdir(parents=True, exist_ok=True)
    cached = (d / "meta.json").exists() and (d / "queries.npz").exists() and not force

    decks = corpus.load_corpus(corpus_name, limit=limit)
    train, test = corpus.split_by_deck(decks, test_frac=thresholds.T4_TEST_FRAC, seed=split_seed)
    corpus.assert_no_deck_leak(train, test)

    pool = loo_eval.commander_legal_pool(cards_df)
    oid_order = list(pool["oracle_id"])
    train_counts = strata_mod.train_appearance_counts(train)

    if cached:
        pos = pd.read_parquet(d / "positives.parquet")
        neg = pd.read_parquet(d / "negatives.parquet")
        positives = list(zip(pos["a"], pos["b"]))
        negatives = list(zip(neg["a"], neg["b"]))
        strata = pd.read_parquet(d / "strata.parquet")
        z = np.load(d / "queries.npz", allow_pickle=True)
        qs = loo_eval.QuerySet(
            oid_order=oid_order, target_row=z["target_row"], ctx_flat=z["ctx_flat"],
            ctx_offsets=z["ctx_offsets"], deck_ids=list(z["deck_ids"]),
            mask_table=z["mask_table"], mask_idx=z["mask_idx"],
            n_dropped_no_text=int(z["n_dropped_no_text"]),
            n_dropped_target_ineligible=int(z["n_dropped_target_ineligible"]),
            n_commander_fallback_decks=int(z["n_commander_fallback_decks"]),
            n_decks=int(z["n_decks"]))
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    else:
        mat, card_index = ppmi_mod.build(train)
        # No eval-pair exclusion here: T2 excluded a curated substitute set because that set *was*
        # its metric. T4's metric is held-out decks, and those are already absent from the PPMI
        # above — the split is the exclusion.
        positives, _ = mining.mine_positives(mat, card_index, set(), set())
        from .t2_leakage import cap_positives
        positives = cap_positives(positives, max_positives or 250_000, seed=split_seed)
        negatives, _ = mining.mine_hard_negatives(
            mat, card_index, cards_df, set(), set(), set(map(frozenset, positives)))
        qs = loo_eval.build_queries(test, pool, oid_order, max_decks=max_test_decks)
        strata = strata_mod.build(train, repr_tables)

        meta = {
            "corpus": corpus_name, "split_seed": split_seed,
            "n_decks": len(decks), "n_train_decks": len(train), "n_test_decks": len(test),
            "n_ppmi_vocab": len(card_index), "n_positives": len(positives),
            "n_negatives": len(negatives), "n_pool": len(oid_order),
            "n_queries": len(qs),
            "n_dropped_no_text": qs.n_dropped_no_text,
            "n_dropped_target_ineligible": qs.n_dropped_target_ineligible,
            "n_commander_fallback_decks": qs.n_commander_fallback_decks,
        }
        _pairs_to_frame(positives).to_parquet(d / "positives.parquet", index=False)
        _pairs_to_frame(negatives).to_parquet(d / "negatives.parquet", index=False)
        strata.to_parquet(d / "strata.parquet", index=False)
        np.savez_compressed(
            d / "queries.npz", target_row=qs.target_row, ctx_flat=qs.ctx_flat,
            ctx_offsets=qs.ctx_offsets, deck_ids=np.array(qs.deck_ids, dtype=object),
            mask_table=qs.mask_table, mask_idx=qs.mask_idx,
            n_dropped_no_text=qs.n_dropped_no_text,
            n_dropped_target_ineligible=qs.n_dropped_target_ineligible,
            n_commander_fallback_decks=qs.n_commander_fallback_decks, n_decks=qs.n_decks)

    loo_eval.set_popularity(qs, train_counts)
    clean = set(repr_tables["cdl"].query("status == 'clean'")["oracle_id"])
    clean_mask = np.fromiter((o in clean for o in oid_order), dtype=bool, count=len(oid_order))
    meta["n_clean_in_pool"] = int(clean_mask.sum())
    meta["from_cache"] = cached
    (d / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {"train": train, "test": test, "positives": positives, "negatives": negatives,
            "pool": pool, "oid_order": oid_order, "queries": qs, "strata": strata,
            "clean_mask": clean_mask, "train_counts": train_counts, "meta": meta}


# ── one (arm, seed) run ───────────────────────────────────────────────────────

def cached_texts(arm: str, pool: pd.DataFrame, repr_tables: dict, root: Path,
                 force: bool = False) -> tuple[dict[str, str], dict]:
    """Layer 2 — the only arm-keyed cache, and the only thing downstream of the seam.

    Rebuilding is not free: arms B/B_type/B+/C re-run the clause splitter over all 31k cards, and
    across a 6-arm x 5-seed grid that is the same work thirty times. Keyed on the parser pin, so a
    refreshed parser invalidates it rather than silently serving text built by a different build.
    """
    d = root / "texts"
    d.mkdir(parents=True, exist_ok=True)
    pin = repr_tables["pin"]["combined_sha256"][:12]
    path = d / f"{arm.replace('+', 'plus')}_{pin}.parquet"
    meta_path = path.with_suffix(".stats.json")

    if path.exists() and meta_path.exists() and not force:
        df = pd.read_parquet(path)
        return (dict(zip(df["oracle_id"], df["text"])),
                json.loads(meta_path.read_text(encoding="utf-8")))

    texts, stats = encodings.build_texts(arm, pool, repr_tables)
    pd.DataFrame({"oracle_id": list(texts), "text": list(texts.values())}).to_parquet(path, index=False)
    meta_path.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return texts, stats


def run_one(arm: str, seed: int, corpus_name: str, l0: dict, cards_df: pd.DataFrame,
            repr_tables: dict, root: Path, epochs: int = config.FINETUNE_EPOCHS,
            force: bool = False) -> dict:
    emb_dir = root / "emb"
    emb_dir.mkdir(parents=True, exist_ok=True)
    emb_path = emb_dir / f"{corpus_name}_{arm.replace('+', 'plus')}_s{seed}.npy"

    texts, text_stats = cached_texts(arm, l0["pool"], repr_tables, root, force=force)

    # Arm D is defined only on clean-parse cards, so it trains only on mined pairs whose *both*
    # ends parse cleanly. On a small corpus that set can be empty. Reported as a skipped arm with
    # a reason rather than crashing the run or, worse, quietly vanishing from the results table.
    n_trainable = sum(1 for a, b in l0["positives"] if texts.get(a) and texts.get(b))
    if n_trainable == 0:
        return {"arm": arm, "seed": seed, "corpus": corpus_name, "text_stats": text_stats,
                "skipped": "no training pair has text under this arm "
                           f"(0 of {len(l0['positives']):,} mined positives)",
                "full": None, "clean_universe": None}

    t0 = time.time()
    if emb_path.exists() and not force:
        embeddings = np.load(emb_path)
        trained = False
    else:
        embeddings = finetune.finetune(
            l0["positives"], l0["negatives"], texts, l0["oid_order"],
            seed=seed, max_seq_length=thresholds.T4_MAX_SEQ_LENGTH, epochs=epochs)
        np.save(emb_path, embeddings)      # persist before eval so a crash costs minutes, not hours
        trained = True
    train_secs = time.time() - t0

    # Phase timings are recorded, not inferred. Wall-clock per run is dominated by different things
    # for different arms — B+ trains ~2.4x longer than A on identical step counts because its
    # sequences are 2.5x longer and attention is quadratic — and only the training phase scales
    # with the positive count. Projecting grid cost from total wall-clock alone overstates it.
    qs, strata = l0["queries"], l0["strata"]
    t1 = time.time()
    full = None if arm == "D" else loo_eval.evaluate(embeddings, qs, strata, seed=seed)
    t2 = time.time()
    clean = loo_eval.evaluate(embeddings, qs, strata, seed=seed, restrict_to=l0["clean_mask"])
    eval_secs = {"full": round(t2 - t1, 1), "clean_universe": round(time.time() - t2, 1)}

    return {
        "arm": arm, "seed": seed, "corpus": corpus_name,
        "text_stats": text_stats,
        "n_trainable_positives": n_trainable,
        "n_texts_missing_in_pool": int(sum(1 for o in l0["oid_order"] if o not in texts)),
        "trained": trained, "train_seconds": round(train_secs, 1),
        "eval_seconds": eval_secs,
        "full": full, "clean_universe": clean,
    }


def partial_path(corpus_name: str, arm: str, seed: int, findings_dir: Path) -> Path:
    return findings_dir / f"t4_partial_{corpus_name}_{arm.replace('+', 'plus')}_s{seed}.json"


# ── aggregation ───────────────────────────────────────────────────────────────

def _metric(rec: dict, universe: str, scorer: str, key: str):
    u = rec.get(universe)
    return None if not u else u["overall"][scorer].get(key)


def aggregate(records: list[dict], universe: str = "full", scorer: str = "centroid") -> dict:
    """Per-arm mean ± sd over seeds, on the primary metric."""
    key = f"recall_at_{thresholds.T4_RECALL_KS[-1]}"
    out = {}
    for arm in sorted({r["arm"] for r in records}):
        vals = [_metric(r, universe, scorer, key) for r in records if r["arm"] == arm]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        a = np.asarray(vals, dtype=float)
        out[arm] = {"n_seeds": len(a), "mean": float(a.mean()),
                    "sd": float(a.std(ddof=1)) if len(a) > 1 else 0.0,
                    "values": [float(x) for x in a]}
    return out


PAIRS = (
    ("C", "B+", thresholds.T4_GATE_C_MINUS_BPLUS, "does CDL justify continued development?"),
    ("B+", "B", thresholds.T4_GATE_BPLUS_MINUS_B, "does clause tagging beat plain segmentation?"),
    ("B", "A", thresholds.T4_GATE_B_MINUS_A, "does segmentation alone buy anything?"),
    ("B+", "B_type", None, "do tags help despite their error rate?"),
)


def verdicts(agg: dict) -> dict:
    out = {}
    for a, b, gate, question in PAIRS:
        if a not in agg or b not in agg:
            continue
        x, y = agg[a], agg[b]
        out[f"{a} - {b}"] = {
            "question": question, "gate": gate,
            "gap": x["mean"] - y["mean"],
            "pooled_sd": thresholds.pooled_sd(x["sd"], x["n_seeds"], y["sd"], y["n_seeds"]),
            "verdict": thresholds.t4_pair_verdict(
                x["mean"], x["sd"], x["n_seeds"], y["mean"], y["sd"], y["n_seeds"], gate),
            # Paired per-seed difference: strictly more informative, because all arms share seeds,
            # split, queries and masks. Observational ONLY — it was not pre-committed, so it must
            # never move a verdict. Same footing as T2's `narrow_effect`.
            "paired_mean_gap": (float(np.mean(np.asarray(x["values"]) - np.asarray(y["values"])))
                                if len(x["values"]) == len(y["values"]) else None),
            "paired_sd": (float(np.std(np.asarray(x["values"]) - np.asarray(y["values"]), ddof=1))
                          if len(x["values"]) == len(y["values"]) > 1 else None),
        }
    return out


# ── report ────────────────────────────────────────────────────────────────────

def _render_strata(records: list[dict], k: int, scorer: str = "centroid") -> str:
    """One table per stratification, arms across the columns.

    Arms have to be side by side within a stratum, because the finding the spec cares about is
    arms tying on the aggregate and separating here. A per-arm table makes that comparison
    something the reader has to do by hand across pages.
    """
    usable = [r for r in records if r.get("full")]
    if not usable:
        return "\n_No full-universe results to stratify._\n"

    arms = sorted({r["arm"] for r in usable})
    by_arm_seed: dict[str, list[dict]] = {}
    for r in usable:
        by_arm_seed.setdefault(r["arm"], []).append(r)

    out = ""
    for col in usable[0]["full"]["strata"]:
        labels = list(usable[0]["full"]["strata"][col])
        head = "| stratum | n | " + " | ".join(arms) + " | popularity | random |"
        sep = "|---" * (len(arms) + 4) + "|"
        rows = []
        for lbl in labels:
            cells, n_q = [], 0
            for arm in arms:
                vals = [rr["full"]["strata"][col].get(lbl, {}).get(scorer, {}).get(f"recall_at_{k}")
                        for rr in by_arm_seed[arm]]
                vals = [v for v in vals if v is not None]
                ns = [rr["full"]["strata"][col].get(lbl, {}).get(scorer, {}).get("n", 0)
                      for rr in by_arm_seed[arm]]
                n_q = max(n_q, max(ns) if ns else 0)
                cells.append(f"{np.mean(vals):.4f}" if vals else "—")
            if not n_q:
                continue
            base = usable[0]["full"]["strata"][col].get(lbl, {})
            for b in ("popularity", "random"):
                v = base.get(b, {}).get(f"recall_at_{k}")
                cells.append(f"{v:.4f}" if v is not None else "—")
            rows.append(f"| {lbl} | {n_q:,} | " + " | ".join(cells) + " |")
        out += f"\n### by {col}\n\n{head}\n{sep}\n" + "\n".join(rows) + "\n"
    return out


def render(records: list[dict], agg: dict, agg_clean: dict, verd: dict, meta: dict,
           smoke: bool) -> str:
    k = thresholds.T4_RECALL_KS[-1]

    def _arow(arm: str) -> str:
        full = agg.get(arm)
        clean = agg_clean.get(arm)
        n = (full or clean)["n_seeds"]
        # Arm D has no full-universe number by construction. Print an em dash rather than dropping
        # the row: a missing row and an inapplicable cell read identically to a reader, and only
        # one of them is honest.
        f_mean = f"{full['mean']:.4f}" if full else "—"
        f_sd = f"{full['sd']:.4f}" if full else "—"
        c_mean = f"{clean['mean']:.4f}" if clean else "—"
        return f"| {arm} | {n} | {f_mean} | {f_sd} | {c_mean} |"

    # Union of both universes, in ladder order, so D is present rather than silently absent.
    order = [a for a in encodings.ALL_ARMS if a in agg or a in agg_clean]
    rows = "\n".join(_arow(a) for a in order)
    def _vrow(name: str, d: dict) -> str:
        gate = "—" if d["gate"] is None else f"{d['gate']:+.2f}"
        sd = "—" if d["pooled_sd"] != d["pooled_sd"] else f"{d['pooled_sd']:.4f}"
        return (f"| {name} | {d['question']} | {d['gap']:+.4f} | {sd} | {gate} "
                f"| **{d['verdict']}** |")

    vrows = "\n".join(_vrow(n, d) for n, d in verd.items())

    banner = ("> **SMOKE RUN — no gated conclusions.** 200 decks and one seed. This exists to prove "
              "the harness runs end-to-end, not to rank arms. Every verdict below is arithmetic on "
              "a sample far too small to mean anything.\n\n" if smoke else "")

    # An arm that produced no result must be visible. Absent from the table and absent from the
    # page are the same thing to a reader, and one of them is a missing measurement.
    skipped = [r for r in records if r.get("skipped")]
    skip_note = ""
    if skipped:
        lines = "\n".join(f"- **{r['arm']}** (seed {r['seed']}): {r['skipped']}" for r in skipped)
        skip_note = f"\n## Arms that produced no result\n\n{lines}\n"

    strat = _render_strata(records, k)

    return f"""# T4 — representation ablation

{report.confound_header()}
{banner}Six arms differing only in how a card becomes a vector. Task: leave-one-out retrieval on
held-out decks, primary metric recall@{k}, deck-level split. Corpus **{meta.get('corpus')}**,
{meta.get('n_train_decks'):,} train / {meta.get('n_test_decks'):,} test decks,
{meta.get('n_queries'):,} queries against a {meta.get('n_pool'):,}-card candidate pool.

## Arms

| arm | seeds | recall@{k} mean | sd | recall@{k}, clean-parse universe |
|---|---|---|---|---|
{rows}

The clean-parse column re-scores **every** arm with candidates and targets restricted to the
{meta.get('n_clean_in_pool'):,} cards CDL parses cleanly. Arm D appears only there: it has no text
for the other cards, so ranking it against the full pool would compare a model that knows a third
of the corpus against models that know all of it.

## Gates

| comparison | question | gap | pooled seed sd | gate | verdict |
|---|---|---|---|---|---|
{vrows}

**The null rule is checked first and can veto a gap that clears its gate:** a difference smaller
than the pooled seed standard deviation is a null result, not a small win. Frozen in
`THRESHOLDS.md` before any arm trained.
{skip_note}

## Stratified results
{strat}
Read these before the aggregate. Arms tying overall while separating on the low-play stratum is
itself the finding — that stratum is the cold-start proxy and the entire reason a text-side encoder
exists. `popularity` is the honest floor: under the standing EDHREC confound above, beating it is
the claim that matters.
"""


# ── merge ─────────────────────────────────────────────────────────────────────

def merge_partials(findings_dir: Path, smoke: bool = False) -> dict:
    """Combine per-run partials. A duplicate (corpus, arm, seed) is an error, never a silent
    overwrite — same reasoning as T2's `merge_partials`."""
    records, seen = [], {}
    for p in sorted(findings_dir.glob("t4_partial_*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        key = (rec["corpus"], rec["arm"], rec["seed"])
        if key in seen:
            raise ValueError(f"duplicate run {key} in {p.name} and {seen[key]}")
        seen[key] = p.name
        records.append(rec)
    if not records:
        raise ValueError(f"no t4_partial_*.json found in {findings_dir}")

    agg, agg_clean = aggregate(records, "full"), aggregate(records, "clean_universe")
    verd = verdicts(agg)
    meta = records[0].get("layer0_meta", {})

    payload = report.envelope([config.CARDS_PARQUET], {"merged_from": sorted(seen.values())})
    payload.update({"records": records, "aggregate": agg, "aggregate_clean_universe": agg_clean,
                    "verdicts": verd, "layer0_meta": meta, "smoke": smoke})
    report.write("t4_representation", payload,
                 render(records, agg, agg_clean, verd, meta, smoke), findings_dir=findings_dir)
    return payload


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(corpus_name: str = "casual", arms: tuple[str, ...] = DEFAULT_ARMS,
         seeds: tuple[int, ...] = DEFAULT_SEEDS, split_seed: int = config.SEED,
         smoke: bool = False, force: bool = False, limit: int | None = None,
         max_test_decks: int | None = None, epochs: int = config.FINETUNE_EPOCHS) -> dict:
    findings_dir = config.FINDINGS_DIR / "smoke" if smoke else config.FINDINGS_DIR
    root = config.RUNS_DIR / ("t4_smoke" if smoke else "t4")
    findings_dir.mkdir(parents=True, exist_ok=True)
    if smoke:
        limit = limit or SMOKE_DECKS
        seeds = seeds[:1]

    cards_df = corpus.load_cards_parquet()
    repr_tables = representations.load()
    print(f"parser pin {repr_tables['pin']['combined_sha256'][:12]}  "
          f"upstream {repr_tables['pin']['upstream_parser_rev']}")

    l0 = build_layer0(corpus_name, cards_df, repr_tables, split_seed, root,
                      limit=limit, max_test_decks=max_test_decks, force=force)
    print(f"layer0 {corpus_name}{' (cached)' if l0['meta'].get('from_cache') else ''}: "
          f"{l0['meta']['n_train_decks']:,} train / "
          f"{l0['meta']['n_test_decks']:,} test decks, {l0['meta']['n_positives']:,} positives, "
          f"{l0['meta']['n_negatives']:,} negatives, {l0['meta']['n_queries']:,} queries, "
          f"pool {l0['meta']['n_pool']:,} ({l0['meta']['n_clean_in_pool']:,} clean)")

    for arm in arms:
        for seed in seeds:
            p = partial_path(corpus_name, arm, seed, findings_dir)
            if p.exists() and not force:
                print(f"  [{arm} s{seed}] skip — {p.name} exists")
                continue
            rec = run_one(arm, seed, corpus_name, l0, cards_df, repr_tables, root,
                          epochs=epochs, force=force)
            rec["layer0_meta"] = l0["meta"]
            p.write_text(json.dumps(rec, indent=2, sort_keys=True, default=str) + "\n",
                         encoding="utf-8")
            if rec.get("skipped"):
                print(f"  [{arm} s{seed}] SKIPPED — {rec['skipped']}")
                continue
            u = rec["full"] or rec["clean_universe"]
            k = thresholds.T4_RECALL_KS[-1]
            print(f"  [{arm} s{seed}] {rec['train_seconds']:.0f}s  "
                  f"recall@{k} {u['overall']['centroid'][f'recall_at_{k}']:.4f}  "
                  f"(pop {u['overall']['popularity'][f'recall_at_{k}']:.4f}, "
                  f"rand {u['overall']['random'][f'recall_at_{k}']:.6f})")

    return merge_partials(findings_dir, smoke=smoke)


def replicate_floor(arm: str, seed: int, corpus_name: str, l0: dict, cards_df: pd.DataFrame,
                    repr_tables: dict, root: Path, epochs: int) -> dict:
    """Train the same arm twice at the same seed and measure how far apart the results land.

    Without this the pooled seed standard deviation is uninterpretable: it conflates the variance
    the seed actually controls (data order, dropout) with cuDNN/cuBLAS reduction-order
    nondeterminism, which no seed touches. `torch.use_deterministic_algorithms(True)` is
    deliberately NOT set — honest seed variance is the point, and determinism would also cost
    speed. So the floor is measured instead of eliminated.

    Read the null rule against this number: an arm gap near the replicate floor is noise no matter
    what the seed sd says.
    """
    k = thresholds.T4_RECALL_KS[-1]
    texts, _ = cached_texts(arm, l0["pool"], repr_tables, root)
    runs = []
    for i in range(2):
        emb = finetune.finetune(l0["positives"], l0["negatives"], texts,
                                l0["oid_order"], seed=seed,
                                max_seq_length=thresholds.T4_MAX_SEQ_LENGTH, epochs=epochs)
        res = loo_eval.evaluate(emb, l0["queries"], l0["strata"], seed=seed)
        runs.append({s: res["overall"][s][f"recall_at_{k}"] for s in loo_eval.SCORERS})
        print(f"  replicate {i + 1}/2 [{arm} s{seed}] "
              f"recall@{k} centroid {runs[-1]['centroid']:.4f} max_sim {runs[-1]['max_sim']:.4f}")
    return {
        "arm": arm, "seed": seed, "corpus": corpus_name, "metric": f"recall_at_{k}",
        "runs": runs,
        "delta": {s: abs(runs[0][s] - runs[1][s]) for s in loo_eval.SCORERS},
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", default="casual", choices=("casual", "cedh"))
    p.add_argument("--arms", nargs="+", default=list(DEFAULT_ARMS), choices=list(encodings.ALL_ARMS))
    p.add_argument("--seeds", nargs="+", type=int, default=list(DEFAULT_SEEDS))
    p.add_argument("--split-seed", type=int, default=config.SEED)
    p.add_argument("--epochs", type=int, default=config.FINETUNE_EPOCHS)
    p.add_argument("--limit", type=int, default=None, help="cap decks loaded from the corpus")
    p.add_argument("--max-test-decks", type=int, default=None)
    p.add_argument("--smoke", action="store_true", help=f"{SMOKE_DECKS} decks, 1 seed, findings/smoke/")
    p.add_argument("--force", action="store_true", help="retrain and re-evaluate existing runs")
    p.add_argument("--merge", action="store_true", help="merge existing partials and exit")
    p.add_argument("--replicate", metavar="ARM", default=None,
                   help="train ARM twice at the same seed and record the same-seed delta")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    fd = config.FINDINGS_DIR / "smoke" if args.smoke else config.FINDINGS_DIR
    if args.merge:
        merge_partials(fd, smoke=args.smoke)
    elif args.replicate:
        root = config.RUNS_DIR / ("t4_smoke" if args.smoke else "t4")
        cards = corpus.load_cards_parquet()
        rt = representations.load()
        l0 = build_layer0(args.corpus, cards, rt, args.split_seed, root,
                          limit=args.limit or (SMOKE_DECKS if args.smoke else None),
                          max_test_decks=args.max_test_decks)
        res = replicate_floor(args.replicate, args.seeds[0], args.corpus, l0, cards, rt, root,
                              args.epochs)
        payload = report.envelope([config.CARDS_PARQUET], {"mode": "replicate"})
        payload.update(res)
        d = res["delta"]
        report.write("t4_replicate_floor", payload, f"""# T4 — same-seed replicate floor

{report.confound_header()}
Arm **{res['arm']}** trained twice at seed {res['seed']} on **{args.corpus}**, everything else held
identical. The gap between the two runs is the variance the seed does *not* control: cuDNN/cuBLAS
reduction-order nondeterminism on the GPU.

| scorer | run 1 | run 2 | delta |
|---|---|---|---|
""" + "\n".join(f"| {s} | {res['runs'][0][s]:.4f} | {res['runs'][1][s]:.4f} | {d[s]:.4f} |"
                for s in loo_eval.SCORERS) + f"""

`torch.use_deterministic_algorithms(True)` is deliberately not set: honest seed variance is the
point of running five seeds, and forcing determinism would suppress it as well as costing speed.
So the floor is measured rather than removed.

**How to read the null rule against this.** The frozen rule calls a gap smaller than the pooled
seed sd a null. This number is the floor beneath that: an arm difference near
{max(d.values()):.4f} on {res['metric']} is kernel noise regardless of what the seed sd says.
""", findings_dir=fd)
        print(f"same-seed delta: " + ", ".join(f"{s} {d[s]:.4f}" for s in loo_eval.SCORERS))
    else:
        main(corpus_name=args.corpus, arms=tuple(args.arms), seeds=tuple(args.seeds),
             split_seed=args.split_seed, smoke=args.smoke, force=args.force,
             limit=args.limit, max_test_decks=args.max_test_decks, epochs=args.epochs)
