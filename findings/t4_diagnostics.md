# T4 — why arm C loses: partition size and displacement

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **Diagnostic, not a gate.** No threshold in `THRESHOLDS.md` is evaluated here and no verdict
> in `findings/t4_representation.md` is affected. These analyses were specified after seeing
> the T4 result and have no pre-committed criteria, so they explain a result rather than
> deciding one.

## D1 — how large is the partition

Arm C serializes 10,628 clean-parse cards as canonical CDL and 20,330 as B+
text. Every other arm uses one encoding throughout, so their rows are the control: the same label,
measured on a space that cannot have partitioned by dialect.

| arm | within-dialect | across-dialect | gap | centroid dist. | probe AUC | kNN CDL share |
|---|---|---|---|---|---|---|
| A | 0.5014 | 0.5008 | **0.0006** | 0.0886 | 0.8196 | 0.544 |
| B | 0.5022 | 0.5013 | **0.0009** | 0.0873 | 0.8189 | 0.544 |
| B_type | 0.5033 | 0.5025 | **0.0009** | 0.0895 | 0.8259 | 0.548 |
| B+ | 0.5061 | 0.5049 | **0.0012** | 0.0876 | 0.8210 | 0.540 |
| C | 0.5293 | 0.5262 | **0.0031** | 0.1092 | 0.9977 | 0.699 |

**Read the gap column, not the AUC.** The probe question — can the dialects be told apart — is
close to a tautology: CDL is a different language with a different token distribution, so any
competent encoder separates it. What matters for retrieval is whether the partition depresses
cross-dialect similarity enough to reorder a top-50, and that is the cosine gap. kNN base rate is
0.343.

## D2 — did C's gap-stratum gain come from displacement

On gap-status targets, C and B+ serialize the held-out card **identically**. C nonetheless scores
+0.0023 there. With the target's own encoding fixed, the only channels are the query vector and the
competitor pool — both of which contain clean cards that C encodes worse. Ranking clean cards down
frees top-50 slots for everything else.

Mean clean cards per top-50, against the arm-independent share available:

| target status | queries | available | B+ | C | C − B+ |
|---|---|---|---|---|---|
| clean | 18,703 | 0.341 | 0.411 | 0.285 | **-0.126** |
| gap | 19,968 | 0.341 | 0.398 | 0.278 | **-0.120** |
| excluded | 1,786 | 0.341 | 0.384 | 0.245 | **-0.138** |

Slots conserved (whatever C takes from one status it gives to another): **True**.

### Where C gains on gap targets, what did it evict?

| seed | gained | lost | net | clean share of displaced | clean share of B+ top-50 |
|---|---|---|---|---|---|
| 42 | 372 | 309 | +63 | 0.503 | 0.399 |
| 43 | 362 | 305 | +57 | 0.497 | 0.398 |
| 44 | 368 | 349 | +19 | 0.505 | 0.399 |

If the displaced cards are more clean-enriched than B+'s top-50 as a whole, C's gains on gapped
cards were bought by evicting clean cards it ranks worse — displacement, not benefit.

## What neither analysis shows

**Neither separates "the space partitioned into two dialects" from "CDL is simply worse text."** A
model that merely encodes CDL badly, with no partition at all, produces the identical displacement
signature and a similar cosine gap. The discriminating experiment is `findings/t4_matched_data.md`,
which holds training volume fixed and varies only the encoding.
