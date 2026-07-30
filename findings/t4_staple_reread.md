# T4 — clean-stratum re-read

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated here and no verdict in
> `findings/t4_representation.md` is affected. This refines *which claim* the CDL evidence
> supports; it does not decide whether to stop CDL. The cold-start dose-response already did that,
> in the one stratum popularity cannot contaminate.

## Why the decisive number needed re-reading

`C − B+ = −0.0216` sits on the clean-parse stratum, which is the only place CDL-as-representation
is testable — on gap and excluded cards C and B+ serialize the target identically and differ only
in query vector and competitor pool. That stratum is also **staple-enriched by construction**: T0
measured CDL clean coverage at 85.3% of the top-100 most-played cards against 43.2% overall,
because common cards use common templates.

And the metric is staple-dominated: popularity scores **0.1808** against the best arm's 0.0624,
all of it from the high-play bucket and **exactly 0.0000** elsewhere. A global frequency ranking
returns the same top-50 for every query, so appearance-weighted recall substantially measures
staple recovery.

### Play distribution by parse status, over queries

| parse status | near_zero | low | mid | high |
|---|---|---|---|---|
| clean | 1.7% | 8.2% | 43.5% | 46.6% |
| gap | 2.5% | 15.9% | 69.6% | 12.0% |
| excluded | 3.7% | 20.7% | 66.7% | 8.9% |

## The re-read

| stratum | queries | B+ | C | C − B+ | paired sd |
|---|---|---|---|---|---|
| all clean | 18,703 | 0.0580 | 0.0364 | **-0.0216** | 0.0018 |
| clean high play | 8,718 | 0.0385 | 0.0088 | **-0.0298** | 0.0021 |
| clean non staple | 9,985 | 0.0749 | 0.0605 | **-0.0144** | 0.0043 |
| clean non staple 50pct | 17,621 | 0.0612 | 0.0380 | **-0.0232** | 0.0017 |

**The deficit survives.** `C − B+` on clean non-staples is at least half its all-clean magnitude, so it is not an artifact of the staple-enriched stratum. The write-up is entitled to the stronger claim: **CDL makes retrieval worse.**

`is_staple_50pct` uses `corpus.identify_staples` (>50% of decks) and is reported only for
completeness — it selects almost nothing on casual, so agreement with the play-bucket cut would
not be independent confirmation.

## Base-rate inversion, resolved

B+ recall climbs clean → gap → excluded, which looked like evidence that parse quality is
unrelated to retrieval quality. It is a play-frequency confound:

| parse status | queries | crude | play-standardized | shift | weight covered |
|---|---|---|---|---|---|
| clean | 18,703 | 0.0580 | 0.0694 | +0.0114 | 100.0% |
| gap | 19,968 | 0.0649 | 0.0527 | -0.0122 | 100.0% |
| excluded | 1,786 | 0.0808 | 0.0661 | -0.0147 | 89.9% |

Standardizing to the overall play distribution removes the inversion. Clean-parse cards are
disproportionately staples, and staples are the hardest cards to retrieve — the ordering was
reporting the play mix, not the parser.

Read the per-decile contrast in the JSON rather than this scalar alone: `train_appearances` is a
target-side property that popularity converts into 0.6481 recall@50 by itself, so standardizing on
it strips a large share of all retrieval variance and the residual is measured on much-reduced
signal.
