# T4 — representation ablation

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

> **SMOKE RUN — no gated conclusions.** 200 decks and one seed. This exists to prove the harness runs end-to-end, not to rank arms. Every verdict below is arithmetic on a sample far too small to mean anything.

Six arms differing only in how a card becomes a vector. Task: leave-one-out retrieval on
held-out decks, primary metric recall@50, deck-level split. Corpus **casual**,
136 train / 24 test decks,
2,002 queries against a 30,958-card candidate pool.

## Arms

| arm | seeds | recall@50 mean | sd | recall@50, clean-parse universe |
|---|---|---|---|---|
| A | 1 | 0.0534 | 0.0000 | 0.0639 |
| B | 1 | 0.0614 | 0.0000 | 0.0670 |
| B_type | 1 | 0.0549 | 0.0000 | 0.0607 |
| B+ | 1 | 0.0554 | 0.0000 | 0.0660 |
| C | 1 | 0.0500 | 0.0000 | 0.0482 |
| D | 1 | — | — | 0.0188 |

The clean-parse column re-scores **every** arm with candidates and targets restricted to the
10,628 cards CDL parses cleanly. Arm D appears only there: it has no text
for the other cards, so ranking it against the full pool would compare a model that knows a third
of the corpus against models that know all of it.

## Gates

| comparison | question | gap | pooled seed sd | gate | verdict |
|---|---|---|---|---|---|
| C - B+ | does CDL justify continued development? | -0.0055 | — | +0.02 | **UNDERPOWERED** |
| B+ - B | does clause tagging beat plain segmentation? | -0.0060 | — | +0.02 | **UNDERPOWERED** |
| B - A | does segmentation alone buy anything? | +0.0080 | — | +0.01 | **UNDERPOWERED** |
| B+ - B_type | do tags help despite their error rate? | +0.0005 | — | — | **UNDERPOWERED** |

**The null rule is checked first and can veto a gap that clears its gate:** a difference smaller
than the pooled seed standard deviation is a null result, not a small win. Frozen in
`THRESHOLDS.md` before any arm trained.


## Stratified results

### by parse_status

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| clean | 955 | 0.0325 | 0.0293 | 0.0356 | 0.0366 | 0.0283 | 0.4147 | 0.0031 |
| excluded | 50 | 0.2400 | 0.2400 | 0.2000 | 0.2400 | 0.1800 | 0.0000 | 0.0000 |
| gap | 997 | 0.0642 | 0.0832 | 0.0672 | 0.0632 | 0.0642 | 0.0762 | 0.0030 |

### by play_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| high | 62 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0161 | 1.0000 | 0.0000 |
| low | 774 | 0.0891 | 0.0969 | 0.0853 | 0.0879 | 0.0827 | 0.0052 | 0.0052 |
| mid | 510 | 0.0078 | 0.0098 | 0.0039 | 0.0078 | 0.0039 | 0.7961 | 0.0000 |
| near_zero | 656 | 0.0518 | 0.0655 | 0.0655 | 0.0579 | 0.0503 | 0.0000 | 0.0030 |

### by unusual_bucket

| stratum | n | A | B | B+ | B_type | C | popularity | random |
|---|---|---|---|---|---|---|---|---|
| common | 963 | 0.0530 | 0.0582 | 0.0602 | 0.0571 | 0.0509 | 0.2274 | 0.0031 |
| rare | 575 | 0.0470 | 0.0591 | 0.0504 | 0.0504 | 0.0487 | 0.2835 | 0.0035 |
| unknown | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| very_common | 236 | 0.0339 | 0.0381 | 0.0339 | 0.0339 | 0.0339 | 0.3305 | 0.0000 |
| very_rare | 227 | 0.0925 | 0.1057 | 0.0705 | 0.0793 | 0.0661 | 0.0529 | 0.0044 |

Read these before the aggregate. Arms tying overall while separating on the low-play stratum is
itself the finding — that stratum is the cold-start proxy and the entire reason a text-side encoder
exists. `popularity` is the honest floor: under the standing EDHREC confound above, beating it is
the claim that matters.
