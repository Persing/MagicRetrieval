# T4 phase 1 — recipe-identity check

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

**Gate:** T4 freezes the training recipe to T2's, but multi-seed training required replacing
`SentenceTransformer.fit()` — a shim that accepts no seed and hard-codes 42 — with an explicit
`SentenceTransformerTrainer`. If that rewrite moved the recipe, every T4 result would be
uninterpretable against T2's. So arm 2c was re-run on cEDH through the new path at seed 42 and
compared to the recorded result.

| quantity | recorded | new trainer | delta |
|---|---|---|---|
| curated lift vs cluster | 0.1503 | 0.1510 | +0.0006 |
| mined-holdout lift | 0.0236 | 0.0242 | +0.0006 |
| positives trained | 177,605 | 177,605 | — |
| negatives | 3,471 | 3,471 | — |
| verdict | PASS | PASS | — |

**PASS.** The lift moved by +0.0006 against a bootstrap CI half-width of
0.0334 — well inside run-to-run noise. Mined pair counts are identical, so the
data path is unchanged and only the trainer construction differs. The verdict and the
`narrow_effect` annotation both reproduce.

What changed, and what did not: the optimizer (AdamW, lr 2e-5, weight decay 0.01 except on
bias/LayerNorm), the scheduler (WarmupLinear over `steps_per_epoch * epochs`), batch size 32,
2 epochs, `max_grad_norm` 1 and fp32 are transcribed verbatim from the shim. Two arguments were
added — `seed` and `data_seed` — and `max_seq_length` is now set explicitly to 512. The latter is
inert here: T2's texts reach 154 tokens at most, so nothing truncates at either 256 or 512.
