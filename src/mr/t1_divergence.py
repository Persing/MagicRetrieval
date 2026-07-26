"""T1 — CDL divergence rate.

Question: do semantically-equivalent cards land on the same CDL, or do different wordings take
different branches to different terminal states?

Three probe sets (see `probe_sets`), scored on the fraction of equivalent pairs whose canonical CDL
differs. Four reporting decisions keep the number honest:

  * **Clean-only is the primary number.** A GAP encoding canonicalizes to whatever it managed to
    express, so comparing two gap-laden encodings measures the gaps, not the representation.
    All-pairs is reported alongside as the looser reading.
  * **scope="rules" is the primary scope.** Functionally equivalent cards differ in name, cost and
    colour by definition; scoring the header would report ~100% divergence and mean nothing.
  * **Strict and loose ordering bound the answer.** Over-sorting understates divergence,
    under-sorting overstates it, so both are reported rather than one being asserted.
  * **The divergent-pair log is the deliverable**, not the percentage. A canonicalizer would be
    built from those cases; the rate only decides whether to build one.

    uv run python -m mr.t1_divergence
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from . import cdl_adapter, config, corpus, probe_sets, report, thresholds

# Below this many clean pairs, a tier cannot carry a verdict on its own — 0/4 is consistent with a
# 75% true divergence rate under the rule of three.
MIN_PAIRS_FOR_VERDICT = 20


def _encode(oracle_cards: dict[str, dict], oid: str) -> tuple[cdl_adapter.ParseResult, str]:
    card = oracle_cards.get(oid)
    if card is None:
        return cdl_adapter.ParseResult(status=cdl_adapter.STATUS_EXCLUDED,
                                       exclusion_reason="not in oracle snapshot"), ""
    return cdl_adapter.parse(card), card.get("name", "")


def score_pairs(
    pairs: list[probe_sets.Pair], oracle_cards: dict[str, dict],
    mask_numbers: bool = False, mask_symbols: bool = False,
) -> tuple[dict, list[dict]]:
    """Return (summary, per-pair rows).

    Masking is per-tier: it separates "the parser took a different branch" from "the parser filled
    the same slot with a different value". A parametric cycle scored unmasked reports divergence
    purely because two signets add different mana, which says nothing about the representation.
    """
    cache: dict[str, tuple[cdl_adapter.ParseResult, str]] = {}

    def enc(oid: str):
        if oid not in cache:
            cache[oid] = _encode(oracle_cards, oid)
        return cache[oid]

    rows: list[dict] = []
    for p in pairs:
        ra, na = enc(p.oracle_id_a)
        rb, nb = enc(p.oracle_id_b)
        both_encoded = ra.encoded and rb.encoded
        row: dict = {
            "source": p.source,
            "name_a": p.name_a or na,
            "name_b": p.name_b or nb,
            "status_a": ra.status,
            "status_b": rb.status,
            "both_clean": ra.ok and rb.ok,
            "both_encoded": both_encoded,
            "note": p.note,
        }
        if both_encoded:
            for scope in ("rules", "full"):
                for label, sort_all in (("loose", False), ("strict", True)):
                    kw = dict(scope=scope, sort_all=sort_all,
                              mask_numbers=mask_numbers, mask_symbols=mask_symbols)
                    ca = cdl_adapter.canonical(ra.cdl or "", card_name=na, **kw)
                    cb = cdl_adapter.canonical(rb.cdl or "", card_name=nb, **kw)
                    row[f"diverge_{scope}_{label}"] = ca != cb
                    if scope == "rules" and label == "loose":
                        row["canonical_a"] = ca
                        row["canonical_b"] = cb
        rows.append(row)

    def rate(subset: list[dict], key: str) -> float | None:
        vals = [r[key] for r in subset if key in r]
        return (sum(vals) / len(vals)) if vals else None

    clean = [r for r in rows if r["both_clean"]]
    enc_rows = [r for r in rows if r["both_encoded"]]
    n_clean = len(clean)
    # Rule of three: with 0 divergences out of n, the 95% upper bound on the true rate is ~3/n.
    # A tier that observes 0/4 is consistent with a 75% true divergence rate, so reporting "0%"
    # without this is reporting a number the sample cannot support.
    observed = sum(1 for r in clean if r.get("diverge_rules_loose"))
    summary = {
        "upper_bound_95": (3.0 / n_clean if n_clean and observed == 0 else None),
        "underpowered": n_clean < MIN_PAIRS_FOR_VERDICT,
        "n_pairs": len(rows),
        "n_both_clean": len(clean),
        "n_both_encoded": len(enc_rows),
        "divergence_clean_rules_loose": rate(clean, "diverge_rules_loose"),
        "divergence_clean_rules_strict": rate(clean, "diverge_rules_strict"),
        "divergence_clean_full_loose": rate(clean, "diverge_full_loose"),
        "divergence_all_rules_loose": rate(enc_rows, "diverge_rules_loose"),
    }
    return summary, rows


def render(sets: dict[str, dict], verdict: str, skips: dict[str, str]) -> str:
    t = thresholds
    lines = [
        "# T1 — CDL divergence rate",
        "",
        report.confound_header(),
        "",
        "**Question:** do semantically-equivalent cards land on the same CDL, or do different "
        "wordings take different branches to different terminal states?",
        "",
        f"**Gates (frozen in THRESHOLDS.md):** PASS < {report.pct(t.T1_PASS)} · "
        f"PARTIAL {report.pct(t.T1_PASS)}–{report.pct(t.T1_FAIL)} · FAIL > {report.pct(t.T1_FAIL)}. "
        f"The numeric probe set is judged separately at < {report.pct(t.T1_NUMERIC_MAX)}.",
        "",
        f"## Verdict: **{verdict}**",
        "",
        "Primary number: clean-only pairs, scope=rules, loose ordering. The threshold applies to "
        "**same_effect_wording**; every other tier is a control or a contrast.",
        "",
        "| Probe set | Pairs | Both clean | Masking | Divergence (primary) | 95% upper bound | Strict | scope=full | All encoded |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, s in sets.items():
        masks = ",".join(m for m in (
            "numbers" if s.get("mask_numbers") else "", "symbols" if s.get("mask_symbols") else "",
        ) if m) or "none"
        ub = s.get("upper_bound_95")
        flag = " ⚠" if s.get("underpowered") else ""
        lines.append(
            f"| {name}{flag} | {s['n_pairs']:,} | {s['n_both_clean']:,} | {masks} | "
            f"**{report.pct(s['divergence_clean_rules_loose'])}** | "
            f"{('≤ ' + report.pct(ub)) if ub is not None else '—'} | "
            f"{report.pct(s['divergence_clean_rules_strict'])} | "
            f"{report.pct(s['divergence_clean_full_loose'])} | "
            f"{report.pct(s['divergence_all_rules_loose'])} |"
        )
    lines += [
        "",
        f"⚠ = fewer than {MIN_PAIRS_FOR_VERDICT} clean pairs; the tier cannot carry a verdict on "
        "its own. **95% upper bound** is the rule of three (3/n) for tiers that observed zero "
        "divergences — a tier reporting 0% over 4 pairs is statistically consistent with a 75% "
        "true rate, so the observed 0% is not by itself evidence of anything.",
    ]
    lines += [
        "",
        "### What each tier is for",
        "",
        "| Tier | Role |",
        "|---|---|",
        "| `identical_text` | **Control.** Oracle text identical once each card's own name is "
        "masked — the strongest possible equivalence claim. Divergence here is a parser "
        "determinism bug. If this is not ~0%, no other number in T1 is interpretable. |",
        "| `same_effect_wording` | **The threshold tier.** Same effect, different wording. |",
        "| `parametric_cycles` | Same template, different slot values (signets, guildgates). "
        "Masked, so a remaining difference is structural rather than a correctly-filled slot. |",
        "| `numeric` | Same effect at different numeric values, numbers masked — tests whether "
        "numbers are parameterized out rather than branched on. |",
        "| `substitutes_contrast` | Strategic substitutes, *not* equivalents. These should diverge "
        "**more** than the equivalence tiers. If they don't, canonicalization is washing out real "
        "differences and every number above is too low. |",
    ]

    lines += [
        "",
        "## How to read the columns",
        "",
        "- **Divergence (primary)** — both cards encode cleanly; header fields (name, cost, types, "
        "stats) dropped; only blocks declared order-insensitive are sorted. Functionally "
        "equivalent cards differ in name and cost by definition, so scoring the header would "
        "report near-100% divergence and measure nothing.",
        "- **Strict ordering** sorts every list. Over-sorting understates divergence and "
        "under-sorting overstates it, so the strict/loose pair bounds the true rate rather than "
        "asserting one value.",
        "- **All encoded** includes pairs where a card carried `# SPEC_GAP:` markers. A gapped "
        "encoding canonicalizes to whatever it managed to express, so this column partly measures "
        "gaps rather than representation. It is the looser reading, not the headline.",
    ]
    if skips:
        lines += ["", "## Skipped probe sets", ""]
        lines += [f"- **{k}** — {v}" for k, v in skips.items()]
    lines += [
        "",
        "## Deliverable",
        "",
        "`t1_divergent_pairs.csv` holds every divergent pair with both canonical forms. That log, "
        "not the percentage, is what a canonicalizer would be built from — the rate only decides "
        "whether to build one.",
    ]
    return "\n".join(lines) + "\n"


def main(all_printings: Path | None = None, findings_dir: Path | None = None) -> dict:
    if not cdl_adapter.available():
        raise cdl_adapter.ParserUnavailable(
            "T1 needs the CDL parser. Set MR_CDL_PARSER=/path/to/PileOfCardsParser."
        )
    out_dir = findings_dir or config.FINDINGS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    cards = corpus.load_cards_parquet()
    oracle_cards = corpus.load_oracle_cards()

    skips: dict[str, str] = {}
    probes = config.DATA_DIR / "probe_sets"

    # (label, pairs-or-path, mask_numbers, mask_symbols)
    #
    # identical_text        control — strongest equivalence claim, must be ~0% or nothing else in
    #                       T1 is interpretable
    # same_effect_wording   THE threshold tier — the plan's "curated functional-equivalence pairs"
    # parametric_cycles     masked; tests structural branch equality across a template
    # numeric               masked; tests whether numbers are parameterized out
    # substitutes_contrast  strategic substitutes, NOT equivalents — should diverge MORE. If they
    #                       don't, canonicalization is washing out real differences and every
    #                       other number here is too low.
    specs: list[tuple[str, Path | list, bool, bool]] = [
        ("identical_text", probes / "identical_text.csv", False, False),
        ("same_effect_wording", probes / "same_effect_diff_wording.csv", False, False),
        ("parametric_cycles", probes / "parametric_cycles.csv", True, True),
        ("numeric", probe_sets.numeric_pairs(cards), True, False),
        ("substitutes_contrast", config.DATA_DIR / "substitute_pairs_v2.csv", False, False),
    ]

    reprints, skip = probe_sets.reprint_pairs(all_printings)
    if skip:
        skips["reprints"] = skip
    else:
        specs.insert(0, ("reprints", reprints, False, False))

    summaries: dict[str, dict] = {}
    all_rows: list[dict] = []
    for label, src, mask_n, mask_s in specs:
        if isinstance(src, Path):
            if not src.exists():
                skips[label] = f"{src} not found — run mr.build_probe_sets first"
                continue
            pairs, unresolved = probe_sets.load_pair_csv(src, cards, label)
            if unresolved:
                skips[f"{label} (unresolved names)"] = ", ".join(sorted(set(unresolved))[:20])
        else:
            pairs = src
        if not pairs:
            skips[label] = "no pairs"
            continue
        s, rows = score_pairs(pairs, oracle_cards, mask_numbers=mask_n, mask_symbols=mask_s)
        s["mask_numbers"], s["mask_symbols"] = mask_n, mask_s
        summaries[label] = s
        all_rows.extend(rows)

    with (out_dir / "t1_divergent_pairs.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "name_a", "name_b", "note", "canonical_a", "canonical_b"])
        for r in all_rows:
            if r.get("diverge_rules_loose"):
                w.writerow([r["source"], r["name_a"], r["name_b"], r["note"],
                            r.get("canonical_a", ""), r.get("canonical_b", "")])

    # The threshold applies to same_effect_wording — the plan's curated functional-equivalence
    # tier. Numeric is judged against its own gate.
    primary = summaries.get("same_effect_wording") or next(iter(summaries.values()), None)
    aggregate = primary["divergence_clean_rules_loose"] if primary else None
    numeric = summaries.get("numeric", {}).get("divergence_clean_rules_loose")
    verdict = thresholds.t1_verdict(aggregate, numeric) if aggregate is not None else "NO DATA"
    if primary and primary.get("underpowered") and verdict == "PASS":
        verdict = "PASS (UNDERPOWERED)"

    payload = report.envelope(
        [config.CARDS_PARQUET, config.SCRYFALL_ORACLE, config.SCRYFALL_TAGS,
         probes / "same_effect_diff_wording.csv", probes / "identical_text.csv",
         probes / "parametric_cycles.csv", config.DATA_DIR / "substitute_pairs_v2.csv"],
        {"parser": cdl_adapter.describe(), "all_printings": str(all_printings) if all_printings else None},
    )
    payload.update({"sets": summaries, "verdict": verdict, "skips": skips,
                    "aggregate_divergence": aggregate, "numeric_divergence": numeric})
    report.write("t1_divergence", payload, render(summaries, verdict, skips), findings_dir=out_dir)
    return payload


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--all-printings", type=Path, default=None,
                   help="Scryfall all-cards bulk, to enable the reprint probe set")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    res = main(all_printings=args.all_printings)
    print(f"T1 verdict: {res['verdict']}  aggregate={res['aggregate_divergence']} "
          f"numeric={res['numeric_divergence']}")
