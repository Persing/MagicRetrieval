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


def check_in_sync() -> list[str]:
    """Assert each numeric threshold appears in THRESHOLDS.md. Returns a list of problems."""
    md = (REPO_ROOT / "THRESHOLDS.md").read_text()
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
    }
    return [f"{name} ({text!r}) not found in THRESHOLDS.md"
            for name, text in expected.items() if text not in md]
