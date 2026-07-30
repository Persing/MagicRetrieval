# T4 — encoding audit

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

Pre-run gate on truncation and content loss. Base model `all-MiniLM-L6-v2`, full
Commander-legal pool of 31,041 cards. **Truncation is a confound, not a risk:**
if one arm truncates and another does not, the measured gap is lost information rather than
representation. `max_seq_length` is therefore set once, identically for every arm.

## Token lengths

| arm | texts | mean | p90 | p99 | max | >256 | >512 |
|---|---|---|---|---|---|---|---|
| A | 31,041 | 45.5 | 72 | 101 | 158 | 0.0% | 0.0% |
| B | 31,041 | 49.8 | 77 | 107 | 164 | 0.0% | 0.0% |
| B_type | 31,041 | 61.6 | 96 | 134 | 210 | 0.0% | 0.0% |
| B+ | 31,041 | 115.1 | 192 | 281 | 440 | 2.0% | 0.0% |
| C | 31,041 | 105.4 | 186 | 278 | 440 | 1.8% | 0.0% |
| D | 10,637 | 65.3 | 104 | 160 | 269 | 0.0% | 0.0% |
| A_raw | 31,041 | 53.5 | 85 | 112 | 172 | 0.0% | 0.0% |
| A_t2 | 31,041 | 45.4 | 76 | 103 | 154 | 0.0% | 0.0% |

At the sentence-transformers default of **256** the structured arms truncate and the text arms do
not — that asymmetry is exactly the confound this gate exists to catch. At **512** no arm truncates
at all, so 512 is the frozen setting.

## Content loss vs arm A (alphanumeric multiset)

| arm | mean | max | cards losing nothing | cards losing >1% |
|---|---|---|---|---|
| B | 0.0% | 5.1% | 99.2% | 0.6% |
| B_type | 0.0% | 5.1% | 99.2% | 0.6% |
| B+ | 0.0% | 3.6% | 99.6% | 0.2% |

Punctuation is excluded: the splitter strips sentence-final punctuation from every span, which
registers as ~2.4% "loss" on 89% of cards while the content is identical. The residual `[RES]`
tail carries whatever the splitter dropped — chiefly modal-card bullets whose sentence defeats
`split_effect_text` — so the structured arms are not handicapped on content arm A retains.

Arms C and D are excluded from this table by design: on clean-parse cards they emit canonical CDL,
a different language, so a character comparison against English oracle text measures the language
change rather than any loss.

## Cost of the shared normalization

Every arm, arm A included, sees oracle text with reminder text stripped (`parser.tokenize_oracle`),
because the structured arms cannot see it and letting A alone keep it would make `B - A` a measure
of reminder-stripping. That is a real cost and it is recorded rather than assumed:
**30.8% of cards carry reminder text**, and on those cards it is a substantial share of the
characters. `A_raw` exists as a diagnostic arm so the size of this decision can be measured on the
end task instead of argued about.
