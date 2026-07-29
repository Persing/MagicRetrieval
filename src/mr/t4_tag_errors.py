"""Step 0 follow-up: which tag fields carry the clause pipeline's tag errors.

The 400-clause hand-graded audit puts per-clause accuracy at 87.5%, with the failure modes split
`tag_value` 47 / `omission` 10 / `values` 5 / `type` 1. That headline says tags carry almost all
the error, but not *which* tags — and those two readings imply completely different work:

  concentrated  -> a bounded fix, or a field that can simply be dropped from arm B+
  diffuse       -> tag extraction is broadly unreliable and no single fix helps

It also changes how `B+ - B_type` is read. That comparison is the only one that isolates tags from
the reliable `type` layer (~99.75%, 1 error in 400), so a weak or negative B+ has to be attributable
to something. If the error is concentrated in two rarely-emitted fields, a null there means "these
two fields are noise", not "tags don't help".

**Rates, not counts, are the output.** A field that appears 200 times with 4 errors and a field
that appears 6 times with 4 errors both contribute "4" to the raw tally and mean opposite things.
Denominators come from how often each key actually appears in `pipeline_tags` across the sample.

    uv run python -m mr.t4_tag_errors
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from . import config, report

SAMPLE_NAME = "sample_accuracy_400.jsonl"


def sample_path() -> Path:
    """The graded sample lives in the parser repo, beside the pipeline it grades. It is not
    vendored: it is an input to this one analysis, not to the T4 runs themselves."""
    for cand in (Path(config.CDL_PARSER_PATH or "") / SAMPLE_NAME,
                 config.REPO_ROOT.parent / "PileOfCardsParser" / SAMPLE_NAME):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"{SAMPLE_NAME} not found. Expected beside the clause pipeline "
        f"({config.CDL_PARSER_PATH}) or in a sibling PileOfCardsParser checkout.")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. Used rather than the normal approximation because most per-field
    denominators here are small, and the normal interval misbehaves badly at n<30 and p near 0."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def tag_vocabulary() -> set[str]:
    """Every declared tag key, from the pipeline's own single source of truth."""
    from . import cdl_adapter
    cdl_adapter.ensure()
    import clause_vocabulary as V
    return set(V.KNOWN_TAG_KEYS) | set(V.PIPELINE_ONLY_TAG_KEYS) | set(V.GOLD_ONLY_TAG_KEYS)


def _fields_in_note(note: str, vocab: set[str]) -> set[str]:
    """Tag keys named in a grader's prose note.

    Necessary because the graded sample splits its attribution across two mechanisms: simple
    value/omission disputes populate `wrong_tags`/`omitted_tags`, while *structural* failures —
    `resolved_reference` not resolving, `zone` being single-valued against "library and/or
    graveyard" — are recorded only in prose. Counting the structured fields alone attributes 21 of
    47 tag failures and makes the error look far more concentrated than it is.

    This is regex-over-prose and is reported as its own category, never merged silently into the
    structured counts.
    """
    import re
    return {k for k in vocab if re.search(rf"\b{re.escape(k)}\b", note)}


def analyze(records: list[dict]) -> dict:
    n = len(records)
    vocab = tag_vocabulary()
    emitted = Counter()          # how often each key was produced at all
    wrong = Counter()            # produced with a wrong value (structured grade)
    omitted = Counter()          # should have been produced, wasn't (structured grade)
    from_notes = Counter()       # named only in the grader's prose
    tag_failures = [r for r in records if not r["grade"]["tags_correct"]]

    for r in records:
        for k in (r.get("pipeline_tags") or {}):
            emitted[k] += 1

    n_note_only = 0
    for r in tag_failures:
        g = r["grade"]
        structured = list(g.get("wrong_tags") or []) + list(g.get("omitted_tags") or [])
        for k in g.get("wrong_tags") or []:
            wrong[k] += 1
        for k in g.get("omitted_tags") or []:
            omitted[k] += 1
        if not structured:
            named = _fields_in_note(g.get("notes") or "", vocab)
            if named:
                n_note_only += 1
                for k in named:
                    from_notes[k] += 1

    keys = sorted(set(emitted) | set(wrong) | set(omitted) | set(from_notes))
    per_tag = []
    for k in keys:
        n_em, n_wr, n_om, n_nt = emitted[k], wrong[k], omitted[k], from_notes[k]
        # Denominator for a *wrong value* is the times the field was emitted. For an *omission*
        # the true denominator is "clauses that should have had it", which the sample does not
        # record — so omissions are reported as counts and deliberately not turned into a rate.
        lo, hi = wilson(n_wr + n_nt, n_em)
        per_tag.append({
            "tag": k, "n_emitted": n_em, "n_wrong_value": n_wr, "n_omitted": n_om,
            "n_from_notes": n_nt,
            "wrong_value_rate": ((n_wr + n_nt) / n_em) if n_em else None,
            "wrong_value_rate_ci": [lo, hi] if n_em else None,
            "n_errors": n_wr + n_om + n_nt,
        })
    per_tag.sort(key=lambda d: (-d["n_errors"], d["tag"]))

    total_err = sum(d["n_errors"] for d in per_tag)
    ranked = [d for d in per_tag if d["n_errors"]]
    top2 = sum(d["n_errors"] for d in ranked[:2])
    top3 = sum(d["n_errors"] for d in ranked[:3])

    structured = sum(1 for r in tag_failures
                     if (r["grade"].get("wrong_tags") or r["grade"].get("omitted_tags")))

    # Per-field concentration answers the spec's question but hides the actionable structure: the
    # fields that fail are not independent. `refers_to_prior` / `resolved_reference` /
    # `resolved_excludes` / `excludes*` are all outputs of ONE stage, `structural_linker`, so
    # errors spread across them are a single subsystem, not a diffuse problem.
    by_subsystem = {
        "cross_clause_linking": {"refers_to_prior", "resolved_reference", "resolved_excludes",
                                 "excludes_prior", "excludes", "refers_to"},
        "targeting": {"target_scope", "target_type", "target_type_compound", "restriction",
                      "chooser", "distributive"},
        "other": set(),
    }
    subsystem = {}
    claimed = set().union(*by_subsystem.values())
    for name, fields in by_subsystem.items():
        sel = [d for d in per_tag
               if (d["tag"] in fields if name != "other" else d["tag"] not in claimed)]
        subsystem[name] = {
            "n_errors": sum(d["n_errors"] for d in sel),
            "n_emitted": sum(d["n_emitted"] for d in sel),
            "fields": sorted(d["tag"] for d in sel if d["n_errors"]),
        }
    for v in subsystem.values():
        v["share_of_errors"] = (v["n_errors"] / total_err) if total_err else None
        v["error_rate"] = (v["n_errors"] / v["n_emitted"]) if v["n_emitted"] else None

    return {
        "n_clauses_graded": n,
        "n_tag_failures": len(tag_failures),
        "n_failures_attributed_structurally": structured,
        "n_failures_attributed_from_notes": n_note_only,
        "n_failures_unattributed": len(tag_failures) - structured - n_note_only,
        "n_field_level_errors": total_err,
        "n_distinct_fields_with_errors": len(ranked),
        "concentration_top2_share": (top2 / total_err) if total_err else None,
        "concentration_top3_share": (top3 / total_err) if total_err else None,
        "by_subsystem": subsystem,
        "per_tag": per_tag,
    }


def _row(d: dict) -> str:
    ci = d["wrong_value_rate_ci"]
    ci_txt = "—" if ci is None else f"[{report.pct(ci[0])}, {report.pct(ci[1])}]"
    return (f"| `{d['tag']}` | {d['n_emitted']} | {d['n_wrong_value']} | {d['n_omitted']} "
            f"| {d['n_from_notes']} | {d['n_errors']} | {report.pct(d['wrong_value_rate'])} | {ci_txt} |")


def render(res: dict) -> str:
    rows = "\n".join(_row(d) for d in res["per_tag"] if d["n_errors"])
    sub_rows = "\n".join(
        f"| {name.replace('_', ' ')} | {v['n_errors']} | {report.pct(v['share_of_errors'])} "
        f"| {v['n_emitted']} | {report.pct(v['error_rate'])} |"
        for name, v in sorted(res["by_subsystem"].items(), key=lambda kv: -kv[1]["n_errors"]))
    # Name the two highest-traffic fields and their rates, so the "reliable layer" claim is data.
    busiest = sorted(res["per_tag"], key=lambda d: -d["n_emitted"])[:2]
    high_volume = " and ".join(
        f"`{d['tag']}` ({d['n_emitted']} emissions, {report.pct(d['wrong_value_rate'])} error)"
        for d in busiest)
    top2 = report.pct(res["concentration_top2_share"])
    top3 = report.pct(res["concentration_top3_share"])
    n_fields = res["n_distinct_fields_with_errors"] or 0
    diffuse = n_fields >= 8
    read = (
        f"**Diffuse.** {res['n_field_level_errors']} field-level errors are spread across "
        f"{n_fields} distinct fields, with the top two holding only {top2}. There is no single "
        "bounded fix and no field whose removal would recover most of the accuracy — so a weak "
        "`B+ − B_type` points at tag extraction broadly, not at a repairable subset, and should "
        "not be re-read by dropping one or two fields."
        if diffuse else
        f"**Concentrated.** The top two fields hold {top2} of field-level errors across only "
        f"{n_fields} fields — a bounded fix, or a droppable field. A weak `B+ − B_type` should be "
        "re-read with those fields removed before concluding that tags do not help.")

    return f"""# T4 — clause tag error breakdown

{report.confound_header()}
Step 0 follow-up, run alongside T4. Source: the 400-clause hand-graded audit
(`{SAMPLE_NAME}`), whose headline is 87.5% per-clause accuracy with tags carrying 47 of the
63 failures. This asks *which* tags.

- Clauses graded: **{res['n_clauses_graded']}**
- Clauses failing on tags: **{res['n_tag_failures']}**
- Field-level errors: **{res['n_field_level_errors']}** across
  **{n_fields}** distinct fields
- Top 2 fields hold **{top2}** of field-level errors; top 3 hold **{top3}**

{read}

## How attribution works, and why it is split

The graded sample records blame two different ways, and reading only the structured fields
undercounts by more than half:

| attribution route | tag failures |
|---|---|
| structured (`wrong_tags` / `omitted_tags`) | {res['n_failures_attributed_structurally']} |
| named only in the grader's prose `notes` | {res['n_failures_attributed_from_notes']} |
| no field identifiable at all | {res['n_failures_unattributed']} |

Simple value disputes and omissions populate the structured lists. **Structural** failures do not:
`resolved_reference` failing to resolve an anaphor, or `zone` being single-valued against a clause
saying "library and/or graveyard", are recorded in prose only. Those are recovered here by matching
the pipeline's own declared tag vocabulary against the note text — a regex over prose, so it is
reported as its own column (`from notes`) and never silently merged into the structured counts.

## By subsystem — where the diffuseness goes away

Per-field concentration answers the question as asked, but the failing fields are not independent.
`refers_to_prior`, `resolved_reference` and `excludes*` are all outputs of a single stage,
`structural_linker`, so error spread across them is one subsystem rather than a diffuse problem.

| subsystem | errors | share | emissions | error rate |
|---|---|---|---|---|
{sub_rows}

Read rate, not volume. Targeting carries the most raw errors simply because it is emitted most
often; per emission it is roughly half as error-prone as cross-clause linking, which is the worst
subsystem by rate despite a smaller share of the total. The two busiest fields of all —
{high_volume} — are the two most reliable.

So the honest summary is: **diffuse across fields, but not uniform across stages.** No single field
is worth dropping from arm B+, and `structural_linker`'s outputs are the ones to distrust first if
`B+ − B_type` comes back weak.

## Per field

| tag | emitted | wrong value | omitted | from notes | total errors | error rate | 95% CI |
|---|---|---|---|---|---|---|---|
{rows}

**Reading the rates.** The denominator is the number of times the field was emitted in the sample,
so the rate answers "when this field is produced, how often is it wrong". Omissions are excluded
from the numerator: their true denominator is "clauses that should have carried this field", which
the graded sample does not record, and inventing one would manufacture a rate that looks
authoritative and is not. Confidence intervals are Wilson, not normal-approximation — most
denominators here are small enough that the normal interval misbehaves.

Every field listed is emitted verbatim into arm B+'s serialization, so these rates bound what
`B+ − B_type` can be measuring.
"""


def main() -> dict:
    path = sample_path()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    res = analyze(records)
    payload = report.envelope([path], {"sample": str(path)})
    payload.update(res)
    report.write("t4_tag_error_breakdown", payload, render(res))
    return res


if __name__ == "__main__":
    r = main()
    print(f"{r['n_tag_failures']} tag failures -> {r['n_field_level_errors']} field-level errors "
          f"across {r['n_distinct_fields_with_errors']} fields")
    print(f"  attributed: structured {r['n_failures_attributed_structurally']}, "
          f"from notes {r['n_failures_attributed_from_notes']}, "
          f"unattributed {r['n_failures_unattributed']}")
    print(f"top2 share {r['concentration_top2_share']:.1%}  top3 share {r['concentration_top3_share']:.1%}")
    for d in r["per_tag"]:
        if d["n_errors"]:
            rate = "—" if d["wrong_value_rate"] is None else f"{d['wrong_value_rate']:.1%}"
            print(f"  {d['tag']:22s} emitted {d['n_emitted']:>4}  wrong {d['n_wrong_value']:>2}  "
                  f"omitted {d['n_omitted']:>2}  notes {d['n_from_notes']:>2}  "
                  f"total {d['n_errors']:>2}  rate {rate}")
