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
import hashlib
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

def layer0_dir(corpus_name: str, split_seed: int, root: Path,
               max_distinct: int = corpus.MAX_COMMANDER_DISTINCT,
               train_subsample: int | None = None, subsample_seed: int | None = None) -> Path:
    """Everything that changes the mined pairs is part of the cache key, not just the payload.

    The deck filter is here because a cache built on the unfiltered corpus would otherwise be
    silently reused against filtered train decks — mixing queries drawn from decks no longer in the
    training set. That is the class of silent inconsistency the arm-D bug belonged to: nothing
    errors, the numbers just quietly describe two different corpora at once.

    **The train subsample is here for exactly the same reason, and the scaling sweep is what makes
    it urgent.** Two levels of a deck sweep differ only in how many train decks they keep. Keyed
    without that, the second level would find the first level's `meta.json` and `queries.npz`,
    take the cached branch, and load the first level's positives, negatives and queries — while
    recomputing `train` from its own smaller deck set. The run would complete and report a scaling
    curve in which nothing scaled.

    The subsample *seed* is in the key too: the subsample is redrawn per seed on purpose, so two
    seeds at the same level are genuinely different deck sets and must not share a directory.

    A `train_subsample` of None reproduces the original name byte-for-byte, so every existing cache
    directory and embedding file stays reachable.
    """
    name = f"split{split_seed}_max{max_distinct}"
    if train_subsample is not None:
        name += f"_train{train_subsample}s{subsample_seed}"
    return root / corpus_name / name


def embedding_path(corpus_name: str, arm: str, seed: int, l0: dict, root: Path) -> Path:
    """Where an arm's embeddings live.

    The embedding depends on the mined positives and negatives, which depend on the split, the deck
    filter and the train subsample — so the basis is in the key, not just the arm and seed. Keyed on
    (corpus, arm, seed) alone, a rerun after changing any of those would silently load a model
    trained on one basis and score it against another: nothing errors, the numbers simply describe
    two bases at once.

    Reads `meta["basis"]`, which `build_layer0` records as the literal directory name it used,
    rather than rebuilding the name from parts — two constructions of the same name is how they
    drift apart, and this function must resolve exactly the files the runner wrote. The fallback
    reconstruction exists only for layer-0 caches written before `basis` was recorded.
    """
    meta = l0["meta"]
    basis = meta.get("basis") or layer0_dir(
        corpus_name, meta["split_seed"], root, meta["deck_filter"]["max_distinct"],
        meta.get("train_subsample"), meta.get("subsample_seed")).name
    return root / "emb" / f"{corpus_name}_{basis}_{arm.replace('+', 'plus')}_s{seed}.npy"


def query_fingerprint(qs: loo_eval.QuerySet) -> str:
    """A hash of everything the evaluation is, so "same test set" is checkable rather than assumed.

    The scaling comparison is only meaningful if both levels are scored on a byte-identical test
    set; with training volume held constant by the positive cap, the test set is the only remaining
    thing that could move. Subsampling *after* the split is what keeps it fixed, and this is the
    assertion that the subsampling actually happened in that order.

    Covers the candidate universe as well as the queries: `oid_order` decides what can be ranked,
    so two runs with identical queries against different pools are not comparable either.
    """
    h = hashlib.sha256()
    h.update("\n".join(qs.oid_order).encode("utf-8"))
    for arr in (qs.target_row, qs.ctx_flat, qs.ctx_offsets, qs.mask_idx, qs.mask_table):
        h.update(np.ascontiguousarray(arr).tobytes())
    h.update("\n".join(qs.deck_ids).encode("utf-8"))
    return h.hexdigest()


def _pairs_to_frame(pairs: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(pairs, columns=["a", "b"]) if pairs else pd.DataFrame(columns=["a", "b"])


def subsample_train(train: list, n: int, seed: int) -> list:
    """Keep `n` train decks, drawn without replacement, in the original order.

    **After the split, never before.** Subsampling the corpus first would redraw the test set at
    every level and the comparison would be measuring two different evaluations. `corpus.load_*`'s
    `limit` is doubly wrong for this: it is a head truncation of the source file, not a sample, and
    it applies before both the plausibility filter and the split.

    Order is preserved rather than permuted so that a level's deck list is a function of the drawn
    set alone — PPMI is order-independent, but the mined positive list is emitted in matrix order
    and `build_example_oids` indexes negatives positionally, so a stable order keeps the level
    reproducible from its own key.
    """
    if n >= len(train):
        return list(train)
    idx = np.random.default_rng(seed).choice(len(train), size=n, replace=False)
    return [train[i] for i in sorted(idx)]


def build_layer0(corpus_name: str, cards_df: pd.DataFrame, repr_tables: dict,
                 split_seed: int, root: Path, limit: int | None = None,
                 max_positives: int | None = None, max_test_decks: int | None = None,
                 force: bool = False,
                 max_distinct: int = corpus.MAX_COMMANDER_DISTINCT,
                 train_subsample: int | None = None, subsample_seed: int | None = None,
                 reference_dir: Path | None = None) -> dict:
    """Split, PPMI, mined pairs, queries and strata — computed once, reused by every arm.

    Cached to disk as well as within a run. `mine_hard_negatives` densifies PPMI rows for 4,000
    anchors and takes minutes; without persistence, splitting the grid across sessions would pay
    that cost again each time, and — worse for correctness — would recompute the pairs every arm is
    supposed to share.

    ## `train_subsample` and why it drags `reference_dir` in with it

    Subsampling train decks is how the scaling sweep varies deck diversity. But the stratum labels
    are *derived from train appearance counts* (`strata.build`), and so is the popularity baseline.
    Shrink the training set an order of magnitude and cards pour into the `≤5 appearances` bucket
    wholesale — so "cold-start recall at 3,142 decks" and "cold-start recall at 39,733 decks" would
    be measured over **different populations of cards**, and the difference between them would be
    mostly re-labelling. The comparison would look clean and mean nothing.

    So a subsampled level takes its strata and its train counts from the full level's
    `strata.parquet` instead of deriving its own: same cards, same buckets, same baseline, and the
    only thing varying is the model. `reference_dir` is therefore required whenever
    `train_subsample` is set — not defaulted, because a silent default here reintroduces exactly
    the bug.
    """
    if train_subsample is not None and reference_dir is None:
        raise ValueError(
            "train_subsample requires reference_dir: a subsampled level must take its strata and "
            "popularity baseline from the full level, or the play-count buckets are recomputed on "
            "the smaller training set and the levels stop being comparable.")
    # Checked here rather than where it is consumed: everything between this point and the strata
    # load is PPMI, mining and query construction, which is hours on a full-size corpus. A missing
    # reference should cost a second, not a night.
    if reference_dir is not None and not (Path(reference_dir) / "strata.parquet").exists():
        raise FileNotFoundError(
            f"{Path(reference_dir) / 'strata.parquet'} is missing — a subsampled level needs the "
            "full level's strata. Build the full level first; deriving strata here would relabel "
            "the play-count buckets on the smaller training set and make the levels incomparable.")

    d = layer0_dir(corpus_name, split_seed, root, max_distinct, train_subsample, subsample_seed)
    d.mkdir(parents=True, exist_ok=True)
    cached = (d / "meta.json").exists() and (d / "queries.npz").exists() and not force

    decks = corpus.load_corpus(corpus_name, limit=limit)
    # Filter BEFORE the split, so train and test share one basis and the drop cannot correlate
    # with which side a deck landed on. Arm-independent and applied before any arm is named, so it
    # cannot bias the comparison between arms — but it does move T4's corpus off the basis T0 and
    # T2 measured, which is recorded in `meta` and reported rather than left implicit.
    decks, filter_stats = corpus.filter_plausible_decks(decks, max_distinct=max_distinct)
    train, test = corpus.split_by_deck(decks, test_frac=thresholds.T4_TEST_FRAC, seed=split_seed)
    corpus.assert_no_deck_leak(train, test)
    n_train_available = len(train)
    if train_subsample is not None:
        train = subsample_train(train, train_subsample, subsample_seed or split_seed)

    pool = loo_eval.commander_legal_pool(cards_df)
    oid_order = list(pool["oracle_id"])

    if cached:
        pos = pd.read_parquet(d / "positives.parquet")
        neg = pd.read_parquet(d / "negatives.parquet")
        positives = list(zip(pos["a"], pos["b"]))
        negatives = list(zip(neg["a"], neg["b"]))
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

        meta = {
            "corpus": corpus_name, "split_seed": split_seed,
            "deck_filter": filter_stats,
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
        np.savez_compressed(
            d / "queries.npz", target_row=qs.target_row, ctx_flat=qs.ctx_flat,
            ctx_offsets=qs.ctx_offsets, deck_ids=np.array(qs.deck_ids, dtype=object),
            mask_table=qs.mask_table, mask_idx=qs.mask_idx,
            n_dropped_no_text=qs.n_dropped_no_text,
            n_dropped_target_ineligible=qs.n_dropped_target_ineligible,
            n_commander_fallback_decks=qs.n_commander_fallback_decks, n_decks=qs.n_decks)

    # ── strata and the popularity baseline ────────────────────────────────────
    # Both are functions of TRAIN appearance counts, so both move when the train set is subsampled.
    # A subsampled level borrows the full level's, which is what keeps the play-count buckets — and
    # therefore the cold-start stratum the whole comparison is read on — the same set of cards at
    # every level. `train_appearances` rides along in the same parquet, so the labels and the counts
    # they were derived from cannot disagree.
    if reference_dir is not None:
        strata = pd.read_parquet(Path(reference_dir) / "strata.parquet")   # existence checked above
        train_counts = dict(zip(strata["oracle_id"], strata["train_appearances"]))
        meta["strata_from"] = str(reference_dir)
    elif cached:
        strata = pd.read_parquet(d / "strata.parquet")
        train_counts = strata_mod.train_appearance_counts(train)
    else:
        strata = strata_mod.build(train, repr_tables)
        strata.to_parquet(d / "strata.parquet", index=False)
        train_counts = strata_mod.train_appearance_counts(train)

    loo_eval.set_popularity(qs, train_counts)
    clean = set(repr_tables["cdl"].query("status == 'clean'")["oracle_id"])
    clean_mask = np.fromiter((o in clean for o in oid_order), dtype=bool, count=len(oid_order))
    meta["n_clean_in_pool"] = int(clean_mask.sum())
    meta["from_cache"] = cached
    meta["basis"] = d.name
    meta["train_subsample"] = train_subsample
    meta["subsample_seed"] = subsample_seed
    meta["n_train_available"] = n_train_available
    meta["queries_sha256"] = query_fingerprint(qs)

    # The test set is the one thing a scaling comparison cannot afford to have move. Subsampling
    # after the split is what holds it fixed; this is the check that it actually did, and it raises
    # rather than annotating — a level scored on a different evaluation is not a level.
    if reference_dir is not None:
        ref_meta_path = Path(reference_dir) / "meta.json"
        ref_meta = json.loads(ref_meta_path.read_text(encoding="utf-8"))
        ref_fp = ref_meta.get("queries_sha256")
        if ref_fp and ref_fp != meta["queries_sha256"]:
            raise RuntimeError(
                f"test set differs from the reference level: {meta['queries_sha256'][:12]} vs "
                f"{ref_fp[:12]} ({ref_meta_path}). Levels must be scored on a byte-identical "
                "evaluation — check that the subsample is applied after `split_by_deck`, not to "
                "the corpus before it.")

    # Atomic: rewritten on every call including cache hits, so every parallel worker reading this
    # directory is also a writer of this file.
    report.atomic_write_text(d / "meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")

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
    # Atomic: parallel shard workers all miss this cache on a cold start and race to create it.
    # A torn parquet reads back as a shorter card list, so the arm trains on fewer examples with
    # nothing reporting an error. `--warm` exists so the race should never happen; this is what
    # makes it harmless when it does.
    report.atomic_write_parquet(
        pd.DataFrame({"oracle_id": list(texts), "text": list(texts.values())}), path, index=False)
    report.atomic_write_text(meta_path, json.dumps(stats, indent=2, sort_keys=True) + "\n")
    return texts, stats


def run_one(arm: str, seed: int, corpus_name: str, l0: dict, cards_df: pd.DataFrame,
            repr_tables: dict, root: Path, epochs: int = config.FINETUNE_EPOCHS,
            force: bool = False) -> dict:
    (root / "emb").mkdir(parents=True, exist_ok=True)
    emb_path = embedding_path(corpus_name, arm, seed, l0, root)

    texts, text_stats = cached_texts(arm, l0["pool"], repr_tables, root, force=force)

    # Two different counts, and conflating them cost arm D two thirds of its data invisibly.
    # `n_trainable_positives` is the obvious predicate — both ends have text — and it is NOT what
    # training consumes: `build_example_oids` also drops a positive whose assigned negative has no
    # text under this arm. For arm D that is 69.2% of them (28,215 -> 8,702). Record both, and
    # judge coverage on the one training actually saw.
    has_text = {oid for oid, t in texts.items() if t}
    n_trainable = sum(1 for a, b in l0["positives"] if a in has_text and b in has_text)
    example_oids = finetune.build_example_oids(l0["positives"], l0["negatives"], has_text)
    n_examples = len(example_oids)
    if n_examples == 0:
        return {"arm": arm, "seed": seed, "corpus": corpus_name, "text_stats": text_stats,
                "n_trainable_positives": n_trainable, "n_training_examples": 0,
                "skipped": "no training example survives under this arm "
                           f"({n_trainable:,} positives had text at both ends, but none kept a "
                           f"negative with text, of {len(l0['positives']):,} mined)",
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
        "n_training_examples": n_examples,
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


def completed_seeds(agg: dict, agg_clean: dict) -> int:
    """Seeds the *ladder* is complete to — the **least** complete arm, not the most.

    Deliberately `min`, and the difference is load-bearing. The grid is written one (arm, seed)
    partial at a time and `main` skips runs that already exist, so the grid is routinely completed
    in chunks — an arm at a time, when a whole-grid block is not available. Halfway through, arm A
    may have 4 seeds while arm C still has 3.

    Under `max` the report would announce "INTERIM — 4 of 5" while every verdict involving C was
    still computed on 3. A verdict is a comparison between two arms and is only as complete as the
    weaker one, so the honest headline is the weaker one. Same principle as `assert_arms_rendered`:
    the artifact must not read as more finished than it is.

    Extracted rather than inlined at both call sites because `render` and `merge_partials` must
    agree — two constructions of the same number is how they drift apart.
    """
    return min((v["n_seeds"] for v in {**agg_clean, **agg}.values()), default=0)


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

class ReportIncomplete(RuntimeError):
    """A report was about to be written that omits an arm which actually ran."""


def assert_arms_rendered(records: list[dict], rows: str) -> None:
    """Every arm with a result must appear in the arms table. Raise rather than write.

    This is a louder guard than it looks, because of what it is guarding against. A slow eval
    announces itself; a report that *reads as complete* while silently dropping an arm does not,
    and these files are the record — they get read, cited and pasted into write-ups long after the
    run. Arm D was dropped exactly this way: the table iterated the full-universe aggregate, D has
    no full-universe number by construction, and the row vanished directly beneath prose saying D
    appears in the clean-parse column. Nothing errored and every number present was correct.

    So the check is on the *rendered text*, not on the intermediate dict. Verifying the aggregate
    would have passed while the table was still wrong — the bug lived in the rendering, which is
    the only artifact anyone actually reads.
    """
    ran = {r["arm"] for r in records if not r.get("skipped")}
    missing = sorted(a for a in ran if not any(ln.startswith(f"| {a} |") for ln in rows.splitlines()))
    if missing:
        raise ReportIncomplete(
            f"arms {missing} produced results but have no row in the report table. "
            f"Refusing to write a report that reads as complete. Arms with results: {sorted(ran)}."
        )


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
    assert_arms_rendered(records, rows)
    def _vrow(name: str, d: dict) -> str:
        gate = "—" if d["gate"] is None else f"{d['gate']:+.2f}"
        sd = "—" if d["pooled_sd"] != d["pooled_sd"] else f"{d['pooled_sd']:.4f}"
        return (f"| {name} | {d['question']} | {d['gap']:+.4f} | {sd} | {gate} "
                f"| **{d['verdict']}** |")

    vrows = "\n".join(_vrow(n, d) for n, d in verd.items())

    # Read the arm-D callout's numbers from the records rather than hard-coding them: the figure
    # that was wrong before was wrong precisely because it was a constant in the prose that no
    # longer matched what the code recorded.
    def _examples(arm: str) -> int | None:
        vals = [r.get("n_training_examples") for r in records if r["arm"] == arm]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    d_n, ref_n = _examples("D"), _examples("B+") or _examples("A")
    d_examples = f"{d_n:,}" if d_n else "an unrecorded number of"
    ref_examples = f"{ref_n:,}" if ref_n else "far more"
    d_share = f"{d_n / ref_n:.1%}" if (d_n and ref_n) else "a small fraction of"

    banner = ("> **SMOKE RUN — no gated conclusions.** 200 decks and one seed. This exists to prove "
              "the harness runs end-to-end, not to rank arms. Every verdict below is arithmetic on "
              "a sample far too small to mean anything.\n\n" if smoke else "")

    # A run short of the frozen seed count must say so on its face. THRESHOLDS.md fixes n=5
    # precisely so that n is not chosen after seeing results; an interim artifact that renders
    # identically to a final one invites exactly that, and these files get cited. Same principle as
    # the completeness guards: the report should not read as more finished than it is.
    n_seeds = completed_seeds(agg, agg_clean)
    if not smoke and 0 < n_seeds < thresholds.T4_N_SEEDS:
        banner += (
            f"> **INTERIM — {n_seeds} of {thresholds.T4_N_SEEDS} frozen seeds.** The verdicts below "
            f"are computed on {n_seeds} seeds and are **not final**. `THRESHOLDS.md` fixes "
            f"n={thresholds.T4_N_SEEDS} so that n is not chosen after seeing results, and at n=3 "
            "the sample sd is itself noisy enough to make the null rule unstable. Read this pass "
            "for harness sanity and for whether the seed spread looks plausible — not for whether "
            f"an arm won. Complete with `--seeds "
            f"{' '.join(str(s) for s in DEFAULT_SEEDS[n_seeds:])}` and re-merge.\n\n")

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

> **Arm D is confounded and its number is not a clean read on CDL.** D trained on
> **{d_examples} examples against {ref_examples} for every other arm — {d_share} of the training
> data**. Two compounding restrictions: it only trains on mined pairs whose *both* ends parse
> cleanly, and `build_example_oids` then drops any survivor whose assigned negative also lacks
> text under this arm, which removes a further 69% of them. Its deficit therefore mixes
> representation with training-set size, and the two cannot be separated — the restriction is
> structural, since the missing pairs involve cards D has no text for at all.
>
> An earlier version of this report quoted 28,215 / 12%, taken from `n_trainable_positives`, a
> both-ends-have-text predicate that is **not** what training consumed. Arms with full text
> coverage lose ~0.1% at that second step, which is why the discrepancy stayed invisible.
>
> Treat `D − C` as uninterpretable. Use the parse-status stratification below — which holds the
> model fixed and varies only which cards are scored — for anything D was meant to answer.

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

def report_name(base: str, corpus_name: str) -> str:
    """One report per corpus. `casual` keeps the bare name so every existing citation still resolves
    — `t4_matched_data.reference()` and `t4_diagnostics` both read `t4_representation.json` by that
    path, and a second corpus must not land on top of it.

    Applies to the replicate floor too: that number is a property of one corpus on one machine's
    kernels, and running it on cedh must not overwrite a casual measurement (or the reverse) with
    something that merely shares a filename.
    """
    return base if corpus_name == "casual" else f"{base}_{corpus_name}"


def merge_partials(findings_dir: Path, smoke: bool = False, corpus: str = "casual") -> dict:
    """Combine per-run partials **for one corpus**. A duplicate (corpus, arm, seed) is an error,
    never a silent overwrite — same reasoning as T2's `merge_partials`.

    **The corpus filter is a correctness device, not a convenience.** `aggregate` groups by arm
    alone, and this glob is `t4_partial_*.json` across the whole findings directory. Without the
    filter, a single `--corpus cedh` run dropped next to the casual partials would make arm A report
    `n_seeds=6` as the mean of two corpora, take `layer0_meta` from whichever file sorted first, and
    lose the INTERIM banner because the seed count now clears the frozen n=5 — a report that reads
    as final while averaging two corpora under one corpus's deck counts. Nothing would error and
    every individual number in it would be correct, which is exactly the failure class the arm-D
    omission belonged to.
    """
    records, seen, other = [], {}, {}
    for p in sorted(findings_dir.glob("t4_partial_*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if rec["corpus"] != corpus:
            other[rec["corpus"]] = other.get(rec["corpus"], 0) + 1
            continue
        key = (rec["corpus"], rec["arm"], rec["seed"])
        if key in seen:
            raise ValueError(f"duplicate run {key} in {p.name} and {seen[key]}")
        seen[key] = p.name
        records.append(rec)
    if not records:
        found = (" Partials present for other corpora: "
                 + ", ".join(f"{c} ({n})" for c, n in sorted(other.items()))) if other else ""
        raise ValueError(f"no t4_partial_*.json for corpus {corpus!r} in {findings_dir}.{found}")

    agg, agg_clean = aggregate(records, "full"), aggregate(records, "clean_universe")
    verd = verdicts(agg)
    meta = records[0].get("layer0_meta", {})

    payload = report.envelope([config.CARDS_PARQUET], {"merged_from": sorted(seen.values()),
                                                       "corpus": corpus})
    n_seeds = completed_seeds(agg, agg_clean)
    payload.update({"records": records, "aggregate": agg, "aggregate_clean_universe": agg_clean,
                    "verdicts": verd, "layer0_meta": meta, "smoke": smoke,
                    "corpus": corpus,
                    "excluded_other_corpora": other,
                    "n_seeds": n_seeds,
                    "frozen_n_seeds": thresholds.T4_N_SEEDS,
                    "is_final": bool(smoke is False and n_seeds >= thresholds.T4_N_SEEDS)})
    report.write(report_name("t4_representation", corpus), payload,
                 render(records, agg, agg_clean, verd, meta, smoke), findings_dir=findings_dir)
    return payload


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(corpus_name: str = "casual", arms: tuple[str, ...] = DEFAULT_ARMS,
         seeds: tuple[int, ...] = DEFAULT_SEEDS, split_seed: int = config.SEED,
         smoke: bool = False, force: bool = False, limit: int | None = None,
         max_test_decks: int | None = None, epochs: int = config.FINETUNE_EPOCHS,
         max_positives: int | None = None) -> dict:
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
                      limit=limit, max_test_decks=max_test_decks, force=force,
                      max_positives=max_positives)
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

    return merge_partials(findings_dir, smoke=smoke, corpus=corpus_name)


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
    import torch
    return {
        "arm": arm, "seed": seed, "corpus": corpus_name, "metric": f"recall_at_{k}",
        "runs": runs,
        # Recorded so a floor measured under a different training configuration cannot be mistaken
        # for a valid one. A multi-GPU run trains at a different effective batch (see
        # `finetune.finetune`), so its delta describes a recipe no ladder used.
        "n_gpu": int(torch.cuda.device_count()),
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
    p.add_argument("--max-positives", type=int, default=None,
                   help="cap on mined positives (default 250,000); part of the layer-0 payload, "
                        "held identical across every level of a comparison")
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
        merge_partials(fd, smoke=args.smoke, corpus=args.corpus)
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
        report.write(report_name("t4_replicate_floor", args.corpus), payload,
                     f"""# T4 — same-seed replicate floor

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
             limit=args.limit, max_test_decks=args.max_test_decks, epochs=args.epochs,
             max_positives=args.max_positives)
