"""T0 — frequency-weighted CDL coverage.

Question: is 32% of *distinct* cards actually 32% of the deck content that matters?

No training. Parse every card's oracle text once, then weight by how often each card actually
appears in decks. The stratified breakdown is the part that keeps the number honest: lands parse
trivially and three staples account for ~2.1 cards per deck, so an aggregate coverage figure is
inflated by exactly the cards CDL does not need to help with.

    uv run python -m mr.t0_coverage --corpus casual cedh
    uv run python -m mr.t0_coverage --smoke
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pandas as pd

from . import cdl_adapter, config, corpus, report, thresholds

TOP_N_BREAKOUTS = (100, 500, 2000)
N_FAILURES_DUMPED = 200


def _is_land(type_line: str) -> bool:
    return "land" in (type_line or "").lower()


def parse_all(cards: pd.DataFrame, oracle_cards: dict[str, dict]) -> pd.DataFrame:
    """One parse per distinct card in the Commander-legal pool.

    The parser takes Scryfall-shaped dicts, so the card list comes from the oracle snapshot and is
    restricted to oracle_ids present in MagicSpike's filtered `cards.parquet`. Cards the parser
    refuses before parsing (multi-faced, digital, non-playable layouts) come back as EXCLUDED and
    are counted as their own stratum — not dropped from the denominator, which would inflate
    coverage, and not lumped with parse failures, which would misattribute the cause.
    """
    rows = []
    for row in cards.drop_duplicates("oracle_id").itertuples(index=False):
        card = oracle_cards.get(row.oracle_id)
        if card is None:
            rows.append({"oracle_id": row.oracle_id, "name": row.name,
                         "type_line": row.type_line, "status": cdl_adapter.STATUS_EXCLUDED,
                         "ok": False, "encoded": False, "error": None,
                         "exclusion_reason": "not in oracle snapshot", "n_gaps": 0,
                         "gap_category": None})
            continue
        res = cdl_adapter.parse(card)
        first_gap = (res.gaps or res.unknown_keywords or [None])[0]
        rows.append({
            "oracle_id": row.oracle_id,
            "name": row.name,
            "type_line": row.type_line,
            "status": res.status,
            "ok": res.ok,
            "encoded": res.encoded,
            "error": res.error,
            "exclusion_reason": res.exclusion_reason,
            "n_gaps": len(res.gaps) + len(res.unknown_keywords),
            "gap_category": cdl_adapter.gap_category(first_gap) if first_gap else None,
        })
    return pd.DataFrame(rows)


def _weighted(parsed: pd.DataFrame, counts: pd.Series, subset: set[str] | None = None,
              col: str = "ok") -> tuple[float, int, int]:
    """(coverage, covered_appearances, total_appearances) over `subset` of oracle_ids."""
    df = parsed if subset is None else parsed[parsed["oracle_id"].isin(subset)]
    w = df["oracle_id"].map(counts).fillna(0).astype("int64")
    total = int(w.sum())
    covered = int(w[df[col].to_numpy()].sum())
    return (covered / total if total else 0.0), covered, total


def analyze(parsed: pd.DataFrame, decks: list[corpus.Deck]) -> dict:
    counts = corpus.appearance_counts(decks)
    staples = corpus.identify_staples(decks)
    lands = set(parsed[parsed["type_line"].map(_is_land)]["oracle_id"])

    in_corpus = set(counts.index)
    nonland = in_corpus - lands
    nonstaple = in_corpus - staples
    guarded = in_corpus - lands - staples

    distinct_cov = float(parsed["ok"].mean()) if len(parsed) else 0.0
    distinct_cov_in_corpus = (
        float(parsed[parsed["oracle_id"].isin(in_corpus)]["ok"].mean())
        if len(in_corpus) else 0.0
    )

    appearance_cov, cov_app, tot_app = _weighted(parsed, counts)
    strata = {
        "all": {"coverage": appearance_cov, "covered": cov_app, "total": tot_app},
    }
    for label, subset in (("non_land", nonland), ("non_staple", nonstaple),
                          ("non_land_non_staple", guarded)):
        c, cv, tt = _weighted(parsed, counts, subset)
        strata[label] = {"coverage": c, "covered": cv, "total": tt}

    top_breakouts = {}
    for n in TOP_N_BREAKOUTS:
        top_ids = set(counts.head(n).index)
        c, cv, tt = _weighted(parsed, counts, top_ids)
        sub = parsed[parsed["oracle_id"].isin(top_ids)]
        top_breakouts[f"top_{n}"] = {
            "appearance_coverage": c,
            "distinct_coverage": float(sub["ok"].mean()) if len(sub) else 0.0,
            "n_cards": len(sub),
        }

    # Where the uncovered appearances actually go. "Fully clean" is the headline, but a card that
    # encodes with one gap line is a different problem from one the parser refuses outright, and
    # lumping them hides which of the two the tail is made of.
    status_share = {}
    for st in (cdl_adapter.STATUS_CLEAN, cdl_adapter.STATUS_GAP,
               cdl_adapter.STATUS_CRASH, cdl_adapter.STATUS_EXCLUDED):
        ids = set(parsed[parsed["status"] == st]["oracle_id"])
        w = int(counts[counts.index.isin(ids)].sum())
        status_share[st] = {"appearances": w, "share": w / tot_app if tot_app else 0.0,
                            "n_cards": len(ids & in_corpus)}
    encoded_cov, _, _ = _weighted(parsed, counts, col="encoded")

    exclusion_reasons = (
        parsed[parsed["status"] == cdl_adapter.STATUS_EXCLUDED]
        .assign(appearances=lambda d: d["oracle_id"].map(counts).fillna(0).astype("int64"))
        .groupby("exclusion_reason")["appearances"].sum().sort_values(ascending=False).to_dict()
    )
    gap_categories = (
        parsed[parsed["status"] == cdl_adapter.STATUS_GAP]
        .assign(appearances=lambda d: d["oracle_id"].map(counts).fillna(0).astype("int64"))
        .groupby("gap_category")["appearances"].sum().sort_values(ascending=False).head(12).to_dict()
    )

    guarded_cov = strata["non_land_non_staple"]["coverage"]
    verdict = thresholds.t0_verdict(appearance_cov, guarded_cov)

    failures = (
        parsed[~parsed["ok"].astype(bool)]
        .assign(appearances=lambda d: d["oracle_id"].map(counts).fillna(0).astype("int64"))
        .sort_values("appearances", ascending=False)
        .head(N_FAILURES_DUMPED)
    )

    return {
        "n_decks": len(decks),
        "n_cards_parsed": int(len(parsed)),
        "n_cards_in_corpus": len(in_corpus),
        "n_staples": len(staples),
        "staple_names": sorted(parsed[parsed["oracle_id"].isin(staples)]["name"].tolist()),
        "distinct_coverage_all_cards": distinct_cov,
        "distinct_coverage_in_corpus": distinct_cov_in_corpus,
        "appearance_coverage": appearance_cov,
        "encoded_coverage": encoded_cov,
        "guarded_coverage": guarded_cov,
        "guard_drop": appearance_cov - guarded_cov,
        "strata": strata,
        "top_breakouts": top_breakouts,
        "status_share": status_share,
        "exclusion_reasons": exclusion_reasons,
        "gap_categories": gap_categories,
        "verdict": verdict,
        "_failures": failures,
    }


def render(results: dict[str, dict]) -> str:
    t = thresholds
    lines = [
        "# T0 — Frequency-weighted CDL coverage",
        "",
        report.confound_header(),
        "",
        "**Question:** is 32% of distinct cards actually 32% of the deck content that matters?",
        "",
        f"**Gates (frozen in THRESHOLDS.md):** PASS ≥ {report.pct(t.T0_PASS)} appearance-weighted · "
        f"FAIL < {report.pct(t.T0_FAIL)} · guard: the non-land non-staple slice may not trail the "
        f"aggregate by more than {report.pct(t.T0_GUARD_MAX_DROP)}.",
        "",
        "## Verdicts",
        "",
        "| Corpus | Decks | Distinct cov. | Appearance cov. | Guarded slice | Drop | Verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, r in results.items():
        lines.append(
            f"| {name} | {r['n_decks']:,} | {report.pct(r['distinct_coverage_in_corpus'])} | "
            f"**{report.pct(r['appearance_coverage'])}** | {report.pct(r['guarded_coverage'])} | "
            f"{report.pct(r['guard_drop'])} | **{r['verdict']}** |"
        )

    for name, r in results.items():
        lines += [
            "",
            f"## {name}",
            "",
            "### Stratified appearance coverage",
            "",
            "| Stratum | Coverage | Covered / total appearances |",
            "|---|---|---|",
        ]
        for label, s in r["strata"].items():
            lines.append(
                f"| {label} | {report.pct(s['coverage'])} | {s['covered']:,} / {s['total']:,} |"
            )
        lines += [
            "",
            "### Head breakout",
            "",
            "| Slice | Appearance cov. | Distinct cov. | Cards |",
            "|---|---|---|---|",
        ]
        for label, b in r["top_breakouts"].items():
            lines.append(
                f"| {label} | {report.pct(b['appearance_coverage'])} | "
                f"{report.pct(b['distinct_coverage'])} | {b['n_cards']:,} |"
            )
        lines += [
            "",
            "### Where the uncovered appearances go",
            "",
            "| Status | Share of appearances | Cards in corpus |",
            "|---|---|---|",
        ]
        for st, s in r["status_share"].items():
            lines.append(f"| {st} | {report.pct(s['share'])} | {s['n_cards']:,} |")
        lines += [
            "",
            f"Encoded-at-all coverage (clean **or** gapped) is {report.pct(r['encoded_coverage'])} "
            f"against {report.pct(r['appearance_coverage'])} fully clean. The difference is the "
            "share of deck content the parser can partially express — a smaller ask to finish "
            "than a card it refuses outright.",
        ]
        if r["exclusion_reasons"]:
            lines += ["", "**Excluded before parsing** (appearance-weighted): " + ", ".join(
                f"`{k}` {v:,}" for k, v in r["exclusion_reasons"].items())]
            lines += [
                "",
                "> `multi_faced` is the one that matters: MDFCs, split cards and adventures are "
                "real Commander cards the parser declines to attempt. They are counted in the "
                "denominator here rather than dropped, so this coverage figure is not inflated by "
                "excluding the hard cases.",
            ]
        if r["gap_categories"]:
            lines += ["", "**Top gap categories** (appearance-weighted): " + ", ".join(
                f"`{k}` {v:,}" for k, v in r["gap_categories"].items())]
        lines += [
            "",
            f"Staples excluded by the guard ({r['n_staples']}): "
            + (", ".join(r["staple_names"]) if r["staple_names"] else "none"),
        ]

    lines += [
        "",
        "## Notes",
        "",
        "- Highest-appearance parse failures are dumped to `t0_failures_<corpus>.csv`. That list, "
        "not the percentage, is the input to any future coverage work.",
        "- Distinct coverage is reported over cards **present in the corpus**, not over the whole "
        "card pool — the latter counts cards no deck plays, which is not what the 32% reference "
        "figure is about.",
    ]
    return "\n".join(lines) + "\n"


def main(corpora: list[str], limit: int | None, findings_dir: Path | None = None) -> dict:
    if not cdl_adapter.available():
        raise cdl_adapter.ParserUnavailable(
            "T0 needs the CDL parser. Set MR_CDL_PARSER=/path/to/PikeOfCardsParser "
            f"(clone of {config.CDL_PARSER_REPO}). T2 runs without it."
        )

    cards = corpus.load_cards_parquet()
    oracle_cards = corpus.load_oracle_cards()
    parsed = parse_all(cards, oracle_cards)
    parser_desc = cdl_adapter.describe()

    out_dir = findings_dir or config.FINDINGS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}
    for name in corpora:
        decks = corpus.load_corpus(name, limit=limit)
        r = analyze(parsed, decks)
        failures = r.pop("_failures")
        with (out_dir / f"t0_failures_{name}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["oracle_id", "name", "type_line", "appearances", "status",
                        "gap_category", "exclusion_reason", "error"])
            for row in failures.itertuples(index=False):
                w.writerow([row.oracle_id, row.name, row.type_line, row.appearances, row.status,
                            row.gap_category, row.exclusion_reason, row.error])
        results[name] = r

    payload = report.envelope(
        [config.CARDS_PARQUET, config.CENSUS_JSONL, config.DECKS_COMBINED, config.PRECON_BACKUP],
        {"corpora": corpora, "limit": limit, "parser": parser_desc},
    )
    payload["results"] = results
    report.write("t0_coverage", payload, render(results), findings_dir=out_dir)
    return results


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", nargs="+", default=["casual", "cedh"], choices=list(config.CORPORA))
    p.add_argument("--limit", type=int, default=None, help="cap decks per corpus")
    p.add_argument("--smoke", action="store_true", help="200-deck subsample, writes to findings/smoke/")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    res = main(
        args.corpus,
        limit=200 if args.smoke else args.limit,
        findings_dir=config.FINDINGS_DIR / "smoke" if args.smoke else None,
    )
    for corpus_name, result in res.items():
        print(f"{corpus_name}: appearance={result['appearance_coverage']:.3f} "
              f"guarded={result['guarded_coverage']:.3f} → {result['verdict']}")
