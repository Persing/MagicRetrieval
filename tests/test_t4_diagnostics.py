"""Diagnostic modules must be correct, and must be unable to reach the frozen ladder."""

import json

import numpy as np
import pandas as pd
import pytest

from mr import t4_diagnostics as D, t4_matched_data as M, t4_representation as T


# ── D1: the closed-form geometry ──────────────────────────────────────────────

def _unit_rows(n, dim, rng):
    E = rng.normal(size=(n, dim)).astype(np.float32)
    return E / np.linalg.norm(E, axis=1, keepdims=True)


def test_partition_geometry_matches_the_brute_force_pairwise_mean():
    """The closed form exists to avoid a 31k x 31k similarity matrix; it has to agree with the
    naive computation it replaces."""
    rng = np.random.default_rng(0)
    E = _unit_rows(60, 8, rng)
    is_cdl = np.zeros(60, dtype=bool)
    is_cdl[:25] = True
    g = D.partition_geometry(E, is_cdl)

    S = E @ E.T
    A, B = np.flatnonzero(is_cdl), np.flatnonzero(~is_cdl)
    wa = S[np.ix_(A, A)][~np.eye(len(A), dtype=bool)].mean()
    wb = S[np.ix_(B, B)][~np.eye(len(B), dtype=bool)].mean()
    across = S[np.ix_(A, B)].mean()
    assert g["within_cdl"] == pytest.approx(wa, abs=1e-6)
    assert g["within_fallback"] == pytest.approx(wb, abs=1e-6)
    assert g["across"] == pytest.approx(across, abs=1e-6)


def test_within_pooled_is_pair_weighted_not_a_mean_of_means():
    """Averaging the two group means would silently upweight the smaller dialect."""
    rng = np.random.default_rng(1)
    E = _unit_rows(50, 6, rng)
    is_cdl = np.zeros(50, dtype=bool)
    is_cdl[:5] = True                     # heavily imbalanced
    g = D.partition_geometry(E, is_cdl)
    naive = (g["within_cdl"] + g["within_fallback"]) / 2
    assert g["within_pooled"] != pytest.approx(naive, abs=1e-9)
    assert min(g["within_cdl"], g["within_fallback"]) <= g["within_pooled"] <= max(
        g["within_cdl"], g["within_fallback"])


def test_planted_partition_shows_a_large_gap_and_a_flat_control():
    """A deliberately split space must register; an unsplit one must not."""
    rng = np.random.default_rng(2)
    dim = 8
    is_cdl = np.zeros(40, dtype=bool)
    is_cdl[:20] = True
    split = np.zeros((40, dim), dtype=np.float32)
    split[is_cdl, 0] = 1.0                 # two orthogonal clumps
    split[~is_cdl, 1] = 1.0
    assert D.partition_geometry(split, is_cdl)["dialect_gap"] == pytest.approx(1.0, abs=1e-5)

    flat = _unit_rows(40, dim, rng)        # label carries no geometric meaning
    assert abs(D.partition_geometry(flat, is_cdl)["dialect_gap"]) < 0.15


# ── D2: slot conservation ─────────────────────────────────────────────────────

def test_topk_slots_are_conserved_by_construction():
    """A top-k holds exactly k slots, so a gain for one card class is a loss for the other. If this
    ever fails on real data, the two arms were scored over different candidate universes and every
    displacement number above it is meaningless."""
    rng = np.random.default_rng(3)
    k, q, n = 50, 200, 400
    clean = rng.random(n) < 0.34
    top_a = rng.integers(0, n, size=(q, k))
    top_b = rng.integers(0, n, size=(q, k))
    delta_clean = clean[top_b].sum(1).mean() - clean[top_a].sum(1).mean()
    delta_other = (~clean)[top_b].sum(1).mean() - (~clean)[top_a].sum(1).mean()
    assert delta_clean + delta_other == pytest.approx(0.0, abs=1e-9)


# ── containment: the diagnostics must not reach the frozen report ─────────────

def test_matched_partials_do_not_match_the_frozen_glob(tmp_path):
    """`merge_partials` globs `t4_partial_*.json`. A diagnostic file matching it would be ingested
    as a seventh arm, be absent from render's ladder order, and halt the frozen report."""
    p = M.partial_path(42, tmp_path)
    assert not p.name.startswith("t4_partial_")
    assert p not in set(tmp_path.glob("t4_partial_*.json"))


def test_frozen_merge_ignores_diagnostic_partials(tmp_path):
    """End to end: a directory holding both kinds still renders the six-arm ladder."""
    def rec(arm, seed, r):
        u = {"n_queries": 100, "strata": {}, "coldstart_gated": {}, "n_near_zero": 10,
             "overall": {s: {"n": 100, "mrr": 0.1, "recall_at_10": r / 2, "recall_at_50": r}
                         for s in ("centroid", "max_sim", "popularity", "random")}}
        return {"arm": arm, "seed": seed, "corpus": "casual", "full": u, "clean_universe": u,
                "n_training_examples": 1000,
                "layer0_meta": {"corpus": "casual", "n_train_decks": 1, "n_test_decks": 1,
                                "n_queries": 100, "n_pool": 100, "n_clean_in_pool": 30}}
    for arm in ("A", "B", "B_type", "B+", "C", "D"):
        T.partial_path("casual", arm, 42, tmp_path).write_text(json.dumps(rec(arm, 42, 0.5)),
                                                               encoding="utf-8")
    M.partial_path(42, tmp_path).write_text(json.dumps({"seed": 42, "B+_matched": {}}),
                                            encoding="utf-8")
    T.merge_partials(tmp_path)
    md = (tmp_path / "t4_representation.md").read_text(encoding="utf-8")
    for arm in ("A", "B", "B_type", "B+", "C", "D"):
        assert any(ln.startswith(f"| {arm} |") for ln in md.splitlines())
    assert "matched" not in md


def test_no_diagnostic_module_imports_the_gated_verdict():
    """Mechanical guarantee that a diagnostic cannot move a frozen verdict."""
    import inspect

    from mr import t4_staple_reread
    for mod in (D, M, t4_staple_reread):
        src = inspect.getsource(mod)
        assert "t4_pair_verdict" not in src, f"{mod.__name__} reaches for a gated verdict"


def test_reports_carry_the_diagnostic_banner():
    assert "Diagnostic, not a gate" in D.DIAGNOSTIC_BANNER
