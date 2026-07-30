"""The scaling sweep: cache keying, the frozen test set, frozen strata, and the report guards.

Almost everything here guards against a run that *completes successfully* while measuring the wrong
thing. A colliding cache key, a test set that moved, or play-count buckets recomputed on the smaller
training set all produce a full report with plausible numbers and no error anywhere.
"""

import json

import numpy as np
import pandas as pd
import pytest

from mr import corpus, loo_eval, t4_representation as T, t4_scaling as S, thresholds


# ── cache keying ──────────────────────────────────────────────────────────────

def test_levels_of_a_sweep_never_share_a_layer0_directory(tmp_path):
    """The failure this prevents: the second level finds the first's `meta.json`, takes the cached
    branch, and trains on the first level's positives while reporting its own deck count."""
    full = T.layer0_dir("cedh", 42, tmp_path, 100)
    sub = T.layer0_dir("cedh", 42, tmp_path, 100, train_subsample=3142, subsample_seed=42)
    assert full != sub
    assert sub.name == "split42_max100_train3142s42"


def test_subsample_seed_is_part_of_the_key(tmp_path):
    """The subsample is redrawn per seed, so two seeds at the same level are different deck sets."""
    a = T.layer0_dir("cedh", 42, tmp_path, 100, train_subsample=3142, subsample_seed=42)
    b = T.layer0_dir("cedh", 42, tmp_path, 100, train_subsample=3142, subsample_seed=43)
    assert a != b


def test_unsubsampled_name_is_unchanged(tmp_path):
    """Existing caches and every embedding file under them must stay reachable."""
    assert T.layer0_dir("casual", 42, tmp_path, 100).name == "split42_max100"


def test_embedding_paths_differ_between_levels(tmp_path):
    """Sharing an embedding path would make the second level silently load the first's model."""
    def l0(basis):
        return {"meta": {"basis": basis, "split_seed": 42, "deck_filter": {"max_distinct": 100}}}
    p_full = T.embedding_path("cedh", "B+", 42, l0("split42_max100"), tmp_path)
    p_sub = T.embedding_path("cedh", "B+", 42, l0("split42_max100_train3142s42"), tmp_path)
    assert p_full != p_sub
    assert "train3142s42" in p_sub.name


def test_embedding_path_falls_back_for_caches_written_before_basis_existed(tmp_path):
    l0 = {"meta": {"split_seed": 42, "deck_filter": {"max_distinct": 100}}}
    assert T.embedding_path("casual", "A", 42, l0, tmp_path).name == \
        "casual_split42_max100_A_s42.npy"


# ── subsampling happens after the split ───────────────────────────────────────

def _decks(n, prefix="d", size=8, seed=0):
    rng = np.random.default_rng(seed)
    return [corpus.Deck(deck_id=f"{prefix}{i}", commander="c",
                        oracle_ids=tuple(f"card{j}" for j in rng.choice(40, size, replace=False)))
            for i in range(n)]


def test_subsample_keeps_n_decks_without_replacement_and_in_order(tmp_path):
    train = _decks(100)
    got = T.subsample_train(train, 30, seed=42)
    ids = [d.deck_id for d in got]
    assert len(got) == 30 and len(set(ids)) == 30
    assert ids == sorted(ids, key=lambda x: [d.deck_id for d in train].index(x))


def test_subsample_larger_than_the_train_set_is_a_no_op(tmp_path):
    train = _decks(10)
    assert len(T.subsample_train(train, 99, seed=42)) == 10


def test_different_seeds_draw_different_subsamples(tmp_path):
    train = _decks(200)
    a = {d.deck_id for d in T.subsample_train(train, 50, seed=42)}
    b = {d.deck_id for d in T.subsample_train(train, 50, seed=43)}
    assert a != b, "a fixed subsample would conflate subsample identity with the corpus effect"


def test_the_split_is_unaffected_by_how_much_train_is_kept(tmp_path):
    """Subsampling the corpus before the split would redraw the test set at every level, and the
    comparison would be measuring two different evaluations."""
    decks = _decks(200)
    train, test = corpus.split_by_deck(decks, test_frac=0.15, seed=42)
    for n in (30, 60, len(train)):
        sub = T.subsample_train(train, n, seed=42)
        assert {d.deck_id for d in sub} <= {d.deck_id for d in train}
        assert not ({d.deck_id for d in sub} & {d.deck_id for d in test})


# ── the reference is mandatory ────────────────────────────────────────────────

def test_a_subsampled_level_without_a_reference_is_refused(tmp_path):
    """Deriving strata on the smaller training set would move cards into the cold-start bucket
    wholesale, so the two levels would score different populations of cards. The default is not a
    silent one."""
    with pytest.raises(ValueError, match="train_subsample requires reference_dir"):
        T.build_layer0("cedh", pd.DataFrame(), {}, 42, tmp_path, train_subsample=3142)


def test_a_missing_reference_strata_file_is_named(tmp_path):
    ref = tmp_path / "cedh" / "split42_max100"
    ref.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="strata.parquet"):
        T.build_layer0("cedh", pd.DataFrame(), {}, 42, tmp_path,
                       train_subsample=10, subsample_seed=42, reference_dir=ref)


# ── the query fingerprint ─────────────────────────────────────────────────────

def _qs(n=12, pool=20, seed=0):
    rng = np.random.default_rng(seed)
    lens = rng.integers(3, 6, size=n)
    return loo_eval.QuerySet(
        oid_order=[f"o{i}" for i in range(pool)],
        target_row=rng.integers(0, pool, size=n),
        ctx_flat=rng.integers(0, pool, size=int(lens.sum())),
        ctx_offsets=np.concatenate([[0], np.cumsum(lens)]),
        deck_ids=[f"d{i}" for i in range(n)],
        mask_table=np.ones((1, pool), dtype=bool),
        mask_idx=np.zeros(n, dtype=np.int64))


def test_fingerprint_is_stable_and_sensitive(tmp_path):
    a, b = _qs(seed=0), _qs(seed=0)
    assert T.query_fingerprint(a) == T.query_fingerprint(b)
    assert T.query_fingerprint(a) != T.query_fingerprint(_qs(seed=1))


def test_fingerprint_covers_the_candidate_universe_too(tmp_path):
    """Identical queries against a different pool are not comparable either — `oid_order` decides
    what can be ranked."""
    a = _qs(seed=0)
    b = _qs(seed=0)
    b.oid_order = [f"x{i}" for i in range(len(b.oid_order))]
    assert T.query_fingerprint(a) != T.query_fingerprint(b)


# ── the paired bootstrap ──────────────────────────────────────────────────────

def test_bootstrap_is_paired_over_queries(tmp_path):
    """A per-query difference of exactly zero must produce an interval containing zero however many
    resamples are drawn — pairing, not two independent means."""
    v = (np.random.default_rng(0).random(500) < 0.3).astype(float)
    b = S.paired_bootstrap(v, v, 500, n_boot=500)
    assert b["observed"] == 0.0 and b["ci_low"] == 0.0 == b["ci_high"]


def test_bootstrap_refuses_misaligned_vectors(tmp_path):
    with pytest.raises(ValueError, match="not scored on the same evaluation"):
        S.paired_bootstrap(np.zeros(10), np.zeros(10), 11, n_boot=10)


def test_hit_rates_average_over_seeds_and_check_alignment(tmp_path):
    recs = [{"level": "full", "arm": "A", "seed": s,
             "hits": {"n_queries": 4, "hits": h, "n_coldstart": 2, "coldstart_hits": [0]}}
            for s, h in ((42, [0, 1]), (43, [1, 2]))]
    r = S.hit_rates(recs, "full", "A", "aggregate")
    assert list(r) == [0.5, 1.0, 0.5, 0.0]
    assert list(S.hit_rates(recs, "full", "A", "cold_start")) == [1.0, 0.0]

    recs[1]["hits"]["n_queries"] = 5
    with pytest.raises(ValueError, match="not scored on the same evaluation"):
        S.hit_rates(recs, "full", "A", "aggregate")


# ── the frozen prediction ─────────────────────────────────────────────────────

def test_an_unknown_ratio_never_renders_as_a_zero_null(tmp_path):
    """A zero volume-null is the *finding* when the cap binds equally. Manufacturing one from
    missing data would be indistinguishable from the real thing in the report."""
    assert S.prediction_for(float("nan")) != S.prediction_for(float("nan"))  # nan
    assert S.prediction_for(0.0) != S.prediction_for(0.0)
    assert S.prediction_for(1.0) == 0.0


def test_example_ratio_uses_examples_not_positives(tmp_path):
    rows = {
        "full_s42": {"level": "full", "n_positives": 250_000, "examples": {"A": 240_000}},
        "subsample_s42": {"level": "subsample", "n_positives": 250_000, "examples": {"A": 120_000}},
        "subsample_s43": {"level": "subsample", "n_positives": 250_000, "examples": {"A": 120_000}},
    }
    assert S.example_ratio(rows, "A") == pytest.approx(2.0)
    assert S.cap_bound(rows) == {"full_s42": True, "subsample_s42": True, "subsample_s43": True}


# ── report guards ─────────────────────────────────────────────────────────────

def _rec(level, arm, seed, agg, cold, decks, examples=240_000, hits=None):
    def m(v, n):
        return {"n": n, "mrr": v / 5, "recall_at_10": v / 3, "recall_at_50": v}
    return {
        "level": level, "arm": arm, "seed": seed, "corpus": "cedh",
        "n_training_examples": examples,
        "full": {"n_queries": 100,
                 "overall": {s: m(agg, 100) for s in ("centroid", "popularity", "random")},
                 "coldstart_gated": {s: m(cold, 40) for s in ("centroid", "popularity", "random")},
                 "strata": {}},
        "hits": hits or {"n_queries": 100, "hits": list(range(int(agg * 100))),
                         "n_coldstart": 40, "coldstart_hits": list(range(int(cold * 40)))},
        "layer0_meta": {"corpus": "cedh", "n_train_decks": decks, "n_positives": 250_000,
                        "n_train_available": 39733, "n_queries": 100, "n_pool": 30958},
    }


def test_report_refuses_to_render_with_a_level_missing(tmp_path):
    recs = [_rec("full", "A", 42, 0.07, 0.15, 39733), _rec("subsample", "A", 42, 0.06, 0.14, 3142)]
    with pytest.raises(S.ReportIncomplete, match="subsample"):
        S.assert_levels_rendered(recs, "| full | A | 1 | 39,733 | 250,000 | 240,000 | 0.07 | 0.15 |")


def test_a_missing_bootstrap_cannot_produce_a_diversity_verdict(tmp_path):
    """Two of three frozen conditions is not the frozen rule."""
    recs = []
    for s in (42, 43, 44):
        recs.append(_rec("full", "A", s, 0.070, 0.160, 39733))
        recs.append(_rec("subsample", "A", s, 0.050, 0.140, 3142))
    for r in recs:
        r.pop("hits")
    res = S.analyse(recs, None)
    u = res["A"]["universes"]["aggregate"]
    assert u["observed"] == pytest.approx(0.020)      # large
    assert u["bootstrap"] is None
    assert u["verdict"] == "UNDERPOWERED"


def test_analyse_reports_both_universes_with_intervals(tmp_path):
    rng = np.random.default_rng(0)
    recs = []
    for s in (42, 43, 44):
        for level, agg, cold in (("full", 0.070, 0.160), ("subsample", 0.050, 0.140)):
            hits = {"n_queries": 4000,
                    "hits": [int(i) for i in np.flatnonzero(rng.random(4000) < agg)],
                    "n_coldstart": 1500,
                    "coldstart_hits": [int(i) for i in np.flatnonzero(rng.random(1500) < cold)]}
            recs.append(_rec(level, "A", s, agg, cold, 39733 if level == "full" else 3142,
                             hits=hits))
    res = S.analyse(recs, None)["A"]
    assert res["example_ratio"] == pytest.approx(1.0)
    assert res["predicted"] == 0.0, "equal example counts -> the volume null is exactly zero"
    for name in ("aggregate", "cold_start"):
        u = res["universes"][name]
        assert u["bootstrap"] is not None, f"{name} must get a real interval"
        assert u["bootstrap"]["ci_low"] < u["observed"] < u["bootstrap"]["ci_high"]
        assert u["verdict"] in {"DIVERSITY_BEYOND_VOLUME", "VOLUME_PROXY",
                                "BELOW_VOLUME_NULL", "UNDERPOWERED"}


def test_partials_cannot_be_ingested_by_the_frozen_ladder(tmp_path):
    """`t4_representation.merge_partials` globs `t4_partial_*.json`. A scaling partial matching that
    prefix would become a phantom arm, vanish from the ladder order and halt the frozen report."""
    p = S.partial_path("cedh", "subsample", "B+", 42, tmp_path)
    assert not p.name.startswith("t4_partial_")
    assert p.name == "t4_scaling_partial_cedh_subsample_Bplus_s42.json"
    p.write_text("{}", encoding="utf-8")
    assert list(tmp_path.glob("t4_partial_*.json")) == []


# ── sharding across parallel workers ──────────────────────────────────────────

def test_run_honours_the_level_shard_and_always_does_full_first(tmp_path, monkeypatch):
    """18 units split by (level, seed) across workers, so `--levels` has to actually reach `run`.
    Order still matters within a worker that got both: the full level is the subsample's
    reference."""
    seen = []
    monkeypatch.setattr(S.corpus, "load_cards_parquet", lambda: None)
    monkeypatch.setattr(S.representations, "load", lambda: {})
    monkeypatch.setattr(S, "level_layer0",
                        lambda level, *a, **k: {"meta": {"corpus": "cedh", "basis": level},
                                                "pool": None, "strata": None, "queries": None})
    monkeypatch.setattr(S, "per_query_hits", lambda *a, **k: {"n_queries": 0, "hits": [],
                                                              "n_coldstart": 0,
                                                              "coldstart_hits": []})

    def fake_run_one(arm, seed, corpus_name, l0, *a, **k):
        seen.append((l0["meta"]["basis"], arm, seed))
        return {"arm": arm, "seed": seed, "corpus": corpus_name, "train_seconds": 1.0,
                "full": {"overall": {"centroid": {"recall_at_50": 0.05}},
                         "coldstart_gated": {"centroid": {"recall_at_50": 0.1}}}}
    monkeypatch.setattr(S.T, "run_one", fake_run_one)

    S.run("cedh", tmp_path, tmp_path, arms=("A",), seeds=(42,), levels=("subsample",))
    assert {lv for lv, _, _ in seen} == {"subsample"}, "the shard flag must be honoured"

    seen.clear()
    S.run("cedh", tmp_path, tmp_path, arms=("A",), seeds=(43,), levels=("subsample", "full"))
    assert [lv for lv, _, _ in seen] == ["full", "subsample"], "full is the subsample's reference"


def test_completed_units_are_skipped_so_a_worker_can_be_restarted(tmp_path, monkeypatch):
    """Shard workers get preempted. Re-running one must not redo finished units."""
    monkeypatch.setattr(S.corpus, "load_cards_parquet", lambda: None)
    monkeypatch.setattr(S.representations, "load", lambda: {})
    called = []
    monkeypatch.setattr(S, "level_layer0", lambda *a, **k: called.append(1) or {"meta": {}})
    S.partial_path("cedh", "full", "A", 42, tmp_path).write_text("{}", encoding="utf-8")
    S.run("cedh", tmp_path, tmp_path, arms=("A",), seeds=(42,), levels=("full",))
    assert not called, "layer 0 must not even be built when every unit is already done"


def test_a_subsample_that_does_not_subsample_is_an_error(tmp_path, monkeypatch):
    """The degenerate case is dangerous because it *succeeds*: `subsample_train` returns the whole
    training set, both levels become the full corpus, and the run yields identical fingerprints, an
    example ratio of exactly 1.0 and a volume null of exactly 0.0000 — the same signature as the
    real cap-binds-equally result it is supposed to be distinguished from."""
    monkeypatch.setattr(S.T, "build_layer0",
                        lambda *a, **k: {"meta": {"n_train_available": 3142}})
    with pytest.raises(ValueError, match="not smaller than"):
        S.level_layer0("subsample", "casual", None, {}, tmp_path, 42, subsample=3142)
    # a genuine subsample passes through untouched
    assert S.level_layer0("subsample", "cedh", None, {}, tmp_path, 42, subsample=500)


def test_atomic_writes_leave_no_temp_files_and_replace_in_place(tmp_path):
    """Parallel workers share layer-0 `meta.json` and the arm text caches. A torn text parquet is
    the dangerous one — it reads back as a *shorter card list*, so the arm trains on fewer examples
    and nothing errors."""
    from mr import report

    p = tmp_path / "meta.json"
    p.write_text('{"old": true}', encoding="utf-8")
    report.atomic_write_text(p, '{"new": true}')
    assert json.loads(p.read_text(encoding="utf-8")) == {"new": True}

    q = tmp_path / "texts.parquet"
    report.atomic_write_parquet(pd.DataFrame({"oracle_id": ["a"], "text": ["t"]}), q, index=False)
    assert len(pd.read_parquet(q)) == 1
    assert not list(tmp_path.glob("*.tmp*")), "temp files must be renamed away, not left behind"


def test_merge_is_scoped_to_one_corpus(tmp_path):
    for level, agg in (("full", 0.070), ("subsample", 0.050)):
        for s in (42, 43, 44):
            r = _rec(level, "A", s, agg, agg + 0.09, 39733 if level == "full" else 3142)
            S.partial_path("cedh", level, "A", s, tmp_path).write_text(
                json.dumps(r), encoding="utf-8")
    stray = _rec("full", "A", 42, 0.9, 0.9, 3142)
    stray["corpus"] = "casual"
    S.partial_path("casual", "full", "A", 42, tmp_path).write_text(
        json.dumps(stray), encoding="utf-8")

    payload = S.merge(tmp_path, "cedh")
    assert len(payload["records"]) == 6, "the casual partial must not be pulled in"
    md = (tmp_path / "t4_scaling.md").read_text(encoding="utf-8")
    assert "| full | A |" in md and "| subsample | A |" in md
    assert "Recorded, not solved." in md
    assert str(thresholds.T4_SCALING_MIN_LIFT) in md or "+0.005" in md
