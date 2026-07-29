# T4 — clause tag error breakdown

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

Step 0 follow-up, run alongside T4. Source: the 400-clause hand-graded audit
(`sample_accuracy_400.jsonl`), whose headline is 87.5% per-clause accuracy with tags carrying 47 of the
63 failures. This asks *which* tags.

- Clauses graded: **400**
- Clauses failing on tags: **47**
- Field-level errors: **52** across
  **13** distinct fields
- Top 2 fields hold **38.5%** of field-level errors; top 3 hold **51.9%**

**Diffuse.** 52 field-level errors are spread across 13 distinct fields, with the top two holding only 38.5%. There is no single bounded fix and no field whose removal would recover most of the accuracy — so a weak `B+ − B_type` points at tag extraction broadly, not at a repairable subset, and should not be re-read by dropping one or two fields.

## How attribution works, and why it is split

The graded sample records blame two different ways, and reading only the structured fields
undercounts by more than half:

| attribution route | tag failures |
|---|---|
| structured (`wrong_tags` / `omitted_tags`) | 21 |
| named only in the grader's prose `notes` | 20 |
| no field identifiable at all | 6 |

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
| targeting | 25 | 48.1% | 348 | 7.2% |
| cross clause linking | 18 | 34.6% | 123 | 14.6% |
| other | 9 | 17.3% | 279 | 3.2% |

Read rate, not volume. Targeting carries the most raw errors simply because it is emitted most
often; per emission it is roughly half as error-prone as cross-clause linking, which is the worst
subsystem by rate despite a smaller share of the total. The two busiest fields of all —
`target_scope` (135 emissions, 3.0% error) and `target_type` (133 emissions, 2.3% error) — are the two most reliable.

So the honest summary is: **diffuse across fields, but not uniform across stages.** No single field
is worth dropping from arm B+, and `structural_linker`'s outputs are the ones to distrust first if
`B+ − B_type` comes back weak.

## Per field

| tag | emitted | wrong value | omitted | from notes | total errors | error rate | 95% CI |
|---|---|---|---|---|---|---|---|
| `refers_to_prior` | 65 | 3 | 1 | 6 | 10 | 13.8% | [7.5%, 24.3%] |
| `restriction` | 80 | 4 | 1 | 5 | 10 | 11.2% | [6.0%, 20.0%] |
| `resolved_reference` | 50 | 1 | 0 | 6 | 7 | 14.0% | [7.0%, 26.2%] |
| `target_scope` | 135 | 1 | 3 | 3 | 7 | 3.0% | [1.2%, 7.4%] |
| `target_type` | 133 | 1 | 2 | 2 | 5 | 2.3% | [0.8%, 6.4%] |
| `zone` | 49 | 2 | 0 | 2 | 4 | 8.2% | [3.2%, 19.2%] |
| `condition_kind` | 21 | 2 | 0 | 0 | 2 | 9.5% | [2.7%, 28.9%] |
| `distributive` | 0 | 0 | 1 | 1 | 2 | — | — |
| `cost_components` | 36 | 0 | 0 | 1 | 1 | 2.8% | [0.5%, 14.2%] |
| `event` | 52 | 0 | 0 | 1 | 1 | 1.9% | [0.3%, 10.1%] |
| `excludes` | 7 | 0 | 1 | 0 | 1 | 0.0% | [0.0%, 35.4%] |
| `keyword` | 54 | 0 | 0 | 1 | 1 | 1.9% | [0.3%, 9.8%] |
| `target_type_compound` | 0 | 0 | 1 | 0 | 1 | — | — |

**Reading the rates.** The denominator is the number of times the field was emitted in the sample,
so the rate answers "when this field is produced, how often is it wrong". Omissions are excluded
from the numerator: their true denominator is "clauses that should have carried this field", which
the graded sample does not record, and inventing one would manufacture a rate that looks
authoritative and is not. Confidence intervals are Wilson, not normal-approximation — most
denominators here are small enough that the normal interval misbehaves.

Every field listed is emitted verbatim into arm B+'s serialization, so these rates bound what
`B+ − B_type` can be measuring.
