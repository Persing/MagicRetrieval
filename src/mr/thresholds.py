"""The frozen thresholds from THRESHOLDS.md, in code.

Duplicated deliberately: the markdown is the human record, this is what the runners judge against,
and `check_in_sync()` asserts the numbers here appear verbatim in the markdown so the two cannot
drift. Every findings JSON embeds `as_dict()` so a number is never readable without its rule.
"""

from __future__ import annotations

from .config import REPO_ROOT

# ── T0 ────────────────────────────────────────────────────────────────────────
T0_PASS = 0.85           # appearance-weighted coverage at or above this → tail not load-bearing
T0_FAIL = 0.70           # below this → tail is load-bearing
T0_GUARD_MAX_DROP = 0.10  # non-land non-staple slice may not trail the aggregate by more than this

# ── T1 ────────────────────────────────────────────────────────────────────────
T1_PASS = 0.10           # divergence below this → no canonicalizer
T1_FAIL = 0.25           # above this → build the canonicalizer
T1_NUMERIC_MAX = 0.05    # numeric-parameterization probe set, judged separately

# ── T2 ────────────────────────────────────────────────────────────────────────
T2_MIN_LIFT = 0.05           # substitutes must exceed random same-cluster by at least this
T2_BOOTSTRAP_N = 10_000
T2_CI = 0.95
T2_GENERALIZATION_RATIO = 0.5  # curated lift below this fraction of mined lift → phenotype-fit
T2_BASELINE_SUBSTITUTE = 0.717  # historical reference point — see caveat below
T2_BASELINE_RANDOM = 0.748
T2_BASELINE_TOL = 0.02

# CAVEAT (discovered building this repo, not before): the corpus that produced 0.717/0.748 was
# 224 precon decks + "several hundred" early cEDH decks (Gate_1_3_Findings.md's own Gate 1
# description). That mixed small-cEDH corpus does not exist in isolated form anymore — the current
# cEDH pool has grown to 46,798 decks, and `backup_precon_only`'s PPMI/embeddings turned out to be
# a byte-identical copy of the current combined-corpus build (confirmed by md5), not an isolated
# precon-only snapshot. Arm 2a is therefore a precon-ONLY rebuild (224 decks, no cEDH at all) — a
# different, smaller corpus than the one that produced the historical numbers. It is reported as
# the closest available reference point, not as a hard reproduction target: `t2_verdict` does not
# gate on `T2_BASELINE_*` at all. A mismatch here means "the corpus changed," not "the harness is
# broken" — check it against `harness_ok` in the report for the plain-language read.

# ── T4 ────────────────────────────────────────────────────────────────────────
# Frozen before the first training run. Gates are on the PRIMARY metric, recall@50, and every one
# of them additionally requires the gap to exceed the pooled seed standard deviation — the null
# rule below. A gate alone is not enough: with ~1,000 effective independent decks, run-to-run
# variance can plausibly exceed the arm differences being measured.
T4_PRIMARY_METRIC = "recall_at_50"
T4_RECALL_KS = (10, 50)
T4_N_SEEDS = 5               # frozen at 5, not "3 and extend if interesting" — see note below
T4_TEST_FRAC = 0.15
T4_MAX_SEQ_LENGTH = 512      # set once for every arm; justified by findings/t4_encoding_audit.json
T4_COLDSTART_MAX_COUNT = 5   # "low play" = at most this many TRAIN-deck appearances

T4_GATE_C_MINUS_BPLUS = 0.02    # the decisive pair: does CDL justify continued development?
T4_GATE_BPLUS_MINUS_B = 0.02    # does clause tagging beat plain segmentation?
T4_GATE_B_MINUS_A = 0.01        # does segmentation alone buy anything?
T4_GATE_COLDSTART = 0.03        # best arm - A, on the low-play stratum specifically

# `B+ - B_type` and `D - C` deliberately have NO gate. The first is a finding at any gap exceeding
# seed sd, in either direction — negative means tags are net-harmful at their current 87.5%
# accuracy. The second is diagnostic only, and is readable *only* inside the clean-parse universe
# where every arm is re-scored against the same candidate pool; D otherwise ranks in a smaller
# pool, which inflates its recall mechanically.

# Why 5 seeds are frozen now rather than "3, then extend if it looks interesting": choosing n after
# seeing results is choosing a stopping rule from the data. With n=3 the sample sd is itself so
# noisy that the null rule below becomes unstable, which defeats its purpose.

CONFOUND_NOTE = (
    "Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is "
    "not independent of EDHREC's recommendations. The effect is strongest in the Archidekt casual "
    "corpus. All arms inherit it equally, so relative comparisons between arms survive; absolute "
    "numbers do not mean much. Recorded, not solved."
)


def as_dict() -> dict:
    return {
        "t0": {"pass": T0_PASS, "fail": T0_FAIL, "guard_max_drop": T0_GUARD_MAX_DROP},
        "t1": {"pass": T1_PASS, "fail": T1_FAIL, "numeric_max": T1_NUMERIC_MAX},
        "t2": {
            "min_lift": T2_MIN_LIFT,
            "bootstrap_n": T2_BOOTSTRAP_N,
            "ci": T2_CI,
            "generalization_ratio": T2_GENERALIZATION_RATIO,
            "baseline_substitute": T2_BASELINE_SUBSTITUTE,
            "baseline_random": T2_BASELINE_RANDOM,
            "baseline_tol": T2_BASELINE_TOL,
        },
        "t4": {
            "primary_metric": T4_PRIMARY_METRIC,
            "recall_ks": list(T4_RECALL_KS),
            "n_seeds": T4_N_SEEDS,
            "test_frac": T4_TEST_FRAC,
            "max_seq_length": T4_MAX_SEQ_LENGTH,
            "coldstart_max_count": T4_COLDSTART_MAX_COUNT,
            "gate_c_minus_bplus": T4_GATE_C_MINUS_BPLUS,
            "gate_bplus_minus_b": T4_GATE_BPLUS_MINUS_B,
            "gate_b_minus_a": T4_GATE_B_MINUS_A,
            "gate_coldstart": T4_GATE_COLDSTART,
            "null_rule": "gap smaller than the pooled seed sd is NULL, not a small win",
        },
        "confound_note": CONFOUND_NOTE,
    }


def t0_verdict(appearance_cov: float, guarded_cov: float) -> str:
    """PASS / FAIL / UNRESOLVED. The guard can only downgrade a PASS, never rescue a FAIL."""
    if appearance_cov < T0_FAIL:
        return "FAIL"
    if appearance_cov >= T0_PASS:
        if appearance_cov - guarded_cov > T0_GUARD_MAX_DROP:
            return "UNRESOLVED"
        return "PASS"
    return "UNRESOLVED"


def t1_verdict(divergence: float, numeric_divergence: float | None) -> str:
    """Aggregate verdict; the numeric subset is reported separately but can force PARTIAL.

    A numeric-subset miss means numbers are not parameterized out. That is a targeted parser bug,
    so it cannot be waved through by a good aggregate — but it also is not evidence for a full
    canonicalizer, so it downgrades to PARTIAL rather than FAIL.
    """
    if divergence > T1_FAIL:
        return "FAIL"
    base = "PASS" if divergence < T1_PASS else "PARTIAL"
    if numeric_divergence is not None and numeric_divergence >= T1_NUMERIC_MAX and base == "PASS":
        return "PARTIAL"
    return base


def t2_verdict(lift: float, ci_low: float, mined_lift: float | None) -> str:
    """PASS / PHENOTYPE_FIT / FAIL."""
    if lift < T2_MIN_LIFT or ci_low <= 0.0:
        return "FAIL"
    if mined_lift is not None and mined_lift > 0 and lift < T2_GENERALIZATION_RATIO * mined_lift:
        return "PHENOTYPE_FIT"
    return "PASS"


def pooled_sd(sd_a: float, n_a: int, sd_b: float, n_b: int) -> float:
    """Pooled sample standard deviation over seeds, the scale the null rule is measured against."""
    if n_a < 2 or n_b < 2:
        return float("nan")
    num = (n_a - 1) * sd_a ** 2 + (n_b - 1) * sd_b ** 2
    return (num / (n_a + n_b - 2)) ** 0.5


def t4_pair_verdict(mean_a: float, sd_a: float, n_a: int,
                    mean_b: float, sd_b: float, n_b: int,
                    gate: float | None) -> str:
    """NULL / BELOW_GATE / PASS, or REAL_GAP for the ungated diagnostic pairs.

    The null rule, frozen before any number was seen: **a gap smaller than the pooled seed
    standard deviation is a null result, not a small win.** It is checked first and it can veto a
    gap that clears its gate — that ordering is the whole point.
    """
    pooled = pooled_sd(sd_a, n_a, sd_b, n_b)
    gap = mean_a - mean_b
    if pooled != pooled:  # nan — too few seeds to have an opinion
        return "UNDERPOWERED"
    if abs(gap) < pooled:
        return "NULL"
    if gate is None:
        return "REAL_GAP"
    return "PASS" if gap >= gate else "BELOW_GATE"


def check_in_sync() -> list[str]:
    """Assert each numeric threshold appears in THRESHOLDS.md. Returns a list of problems.

    Only names present in `expected` are checked, so a constant added here without a corresponding
    entry is silently unguarded — `tests/test_thresholds.py` carries a meta-test that every
    module-level `T4_*` name appears below, precisely to close that hole.
    """
    md = (REPO_ROOT / "THRESHOLDS.md").read_text(encoding="utf-8")
    expected = {
        "T0_PASS": "85%",
        "T0_FAIL": "70%",
        "T0_GUARD_MAX_DROP": "10 percentage points",
        "T1_PASS": "10%",
        "T1_FAIL": "25%",
        "T1_NUMERIC_MAX": "5%",
        "T2_MIN_LIFT": "+0.05",
        "T2_CI": "95%",
        "T2_BASELINE_SUBSTITUTE": "0.717",
        "T2_BASELINE_RANDOM": "0.748",
        "T4_PRIMARY_METRIC": "recall@50",
        "T4_RECALL_KS": "recall@10",
        "T4_N_SEEDS": "5 seeds",
        "T4_TEST_FRAC": "15%",
        "T4_MAX_SEQ_LENGTH": "512",
        "T4_COLDSTART_MAX_COUNT": "at most 5",
        "T4_GATE_C_MINUS_BPLUS": "**≥ +0.02**",
        "T4_GATE_BPLUS_MINUS_B": "≥ +0.02",
        "T4_GATE_B_MINUS_A": "≥ +0.01",
        "T4_GATE_COLDSTART": "≥ +0.03",
    }
    return [f"{name} ({text!r}) not found in THRESHOLDS.md"
            for name, text in expected.items() if text not in md]
