# T4 — representation ablation

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **SMOKE RUN — no gated conclusions.** 200 decks and one seed. This exists to prove the harness runs end-to-end, not to rank arms. Every verdict below is arithmetic on a sample far too small to mean anything.

Six arms differing only in how a card becomes a vector. Task: leave-one-out retrieval on
held-out decks, primary metric recall@50, deck-level split. Corpus **casual**,
170 train / 30 test decks,
3,660 queries against a 30,958-card candidate pool.

## Arms

| arm | seeds | recall@50 mean | sd | recall@50, clean-parse universe |
|---|---|---|---|---|
| A | 1 | 0.0331 | 0.0000 | 0.0450 |
| B | 1 | 0.0355 | 0.0000 | 0.0492 |
| B_type | 1 | 0.0320 | 0.0000 | 0.0450 |
| B+ | 1 | 0.0350 | 0.0000 | 0.0541 |
| C | 1 | 0.0292 | 0.0000 | 0.0346 |
| D | 1 | — | — | 0.0340 |

The clean-parse column re-scores **every** arm with candidates and targets restricted to the
10,628 cards CDL parses cleanly. Arm D appears only there: it has no text
for the other cards, so ranking it against the full pool would compare a model that knows a third
of the corpus against models that know all of it.

## Gates

| comparison | question | gap | pooled seed sd | gate | verdict |
|---|---|---|---|---|---|
| C - B+ | does CDL justify continued development? | -0.0057 | — | +0.02 | **UNDERPOWERED** |
| B+ - B | does clause tagging beat plain segmentation? | -0.0005 | — | +0.02 | **UNDERPOWERED** |
| B - A | does segmentation alone buy anything? | +0.0025 | — | +0.01 | **UNDERPOWERED** |
| B+ - B_type | do tags help despite their error rate? | +0.0030 | — | — | **UNDERPOWERED** |

**The null rule is checked first and can veto a gap that clears its gate:** a difference smaller
than the pooled seed standard deviation is a null result, not a small win. Frozen in
`THRESHOLDS.md` before any arm trained.


## Stratified results

### by parse_status

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| clean | 1,646 | 0.0243 | 0.0292 | 0.0273 | 0.0237 | 0.0182 | 0.3019 | 0.0030 |
| excluded | 138 | 0.0217 | 0.0290 | 0.0435 | 0.0290 | 0.0362 | 0.0145 | 0.0072 |
| gap | 1,876 | 0.0416 | 0.0416 | 0.0410 | 0.0394 | 0.0384 | 0.0416 | 0.0027 |

### by play_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| high | 80 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 1.0000 | 0.0000 |
| low | 1,447 | 0.0574 | 0.0574 | 0.0539 | 0.0574 | 0.0463 | 0.0000 | 0.0035 |
| mid | 896 | 0.0056 | 0.0134 | 0.0145 | 0.0089 | 0.0100 | 0.5547 | 0.0011 |
| near_zero | 1,237 | 0.0267 | 0.0283 | 0.0299 | 0.0210 | 0.0251 | 0.0000 | 0.0040 |

### by unusual_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| common | 1,764 | 0.0317 | 0.0374 | 0.0351 | 0.0312 | 0.0300 | 0.1621 | 0.0040 |
| rare | 1,042 | 0.0384 | 0.0384 | 0.0393 | 0.0393 | 0.0307 | 0.1871 | 0.0029 |
| unknown | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| very_common | 488 | 0.0287 | 0.0307 | 0.0246 | 0.0246 | 0.0246 | 0.1865 | 0.0000 |
| very_rare | 365 | 0.0301 | 0.0247 | 0.0356 | 0.0247 | 0.0274 | 0.0137 | 0.0027 |

Read these before the aggregate. Arms tying overall while separating on the low-play stratum is
itself the finding — that stratum is the cold-start proxy and the entire reason a text-side encoder
exists. `popularity` is the honest floor: under the standing EDHREC confound above, beating it is
the claim that matters.
