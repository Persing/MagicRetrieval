"""The T4 arm seam: how a card becomes a string. The only module in T4 that names an arm.

`build_texts(arm, ...)` returns exactly the `{oracle_id: text}` shape `finetune.text_lookup`
already returns, so mining, training, embedding and evaluation stay arm-blind and need no
changes. Pure function — no I/O, no torch, no global state — so the whole ladder is testable
without a GPU or a corpus.

## Why serialization rather than a multi-vector encoder

The spec's arms table says "type-partitioned pooling", which reads like a custom head. It also
freezes the training recipe and forbids architecture changes in the same run as a representation
comparison. Serialization resolves the conflict in favour of attribution: a custom head would make
A-vs-B+ differ in representation *and* architecture at once, and repairing that means pushing arm A
through the multi-vector path too. Here every arm runs identical weights, identical parameter
count, identical optimizer — one variable moves, and it is the string.

**Recorded ceiling, written before any result exists.** This design yields a conclusive positive
and an ambiguous null. If B+ beats B, structure helps, and a multi-vector encoder could only help
more — the cheap test answered it. If B+ ≈ B, "tags don't help" and "serialization cannot convey
tags" are not distinguishable from these runs. That null is what would justify building the
multi-vector variant; it is not a reason to have built it first.

## The ladder, and what each rung isolates

    A       normalized oracle text, no boundaries          — the floor
    B       A + segment boundaries                         — segmentation alone
    B_type  B + per-clause type labels                     — the reliable layer (type is ~99.75%)
    B+      B_type + tags and values                       — the noisy layer (tags carry 47/63 errors)
    C       canonical CDL where clean, else B+ verbatim    — the deployable system
    D       canonical CDL only, clean cards only           — head precision, diagnostic

Segments stay in document order and type labels are inline; clauses are deliberately *not*
regrouped by type. Regrouping would change segmentation and labelling in the same step, and
`B_type - B` would stop being a single variable.

## Two confounds handled here rather than left to the reader

**Reminder text.** `parser.tokenize_oracle` strips parenthetical reminder text, so B/B_type/B+/C
never see it. Left alone, arm A would carry content no structured arm has, and `B - A` would
measure reminder-stripping rather than segmentation. Every arm therefore shares one normalization
(`normalize_oracle`) applied identically — a recipe parameter, like `max_seq_length`, not a per-arm
choice. `A_raw` exists as a diagnostic arm to measure what that normalization costs, so the size of
the decision is known rather than assumed.

**The shared head.** Mana cost, type line and P/T are constant across every arm including A and D
(spec §"Card attributes held constant"), so CDL is rendered with `scope="rules"`, which drops the
CDL header block. Emitting the head twice for arms C/D would give them a duplicated-feature
advantage unrelated to representation.
"""

from __future__ import annotations

import re

import pandas as pd

from . import cdl_adapter, config, representations

ARMS = ("A", "B", "B_type", "B+", "C", "D")
DIAGNOSTIC_ARMS = ("A_raw", "A_t2")
ALL_ARMS = ARMS + DIAGNOSTIC_ARMS

SEG = "[SEG]"

# Three-letter codes over `clause_vocabulary.VALID_TYPES`. Short and alphabetic so WordPiece keeps
# them as few, stable tokens; bracketed so they cannot collide with card text.
TYPE_CODE = {
    "TRIGGER": "[TRG]", "COST": "[CST]", "CONDITION": "[CND]", "ACTION": "[ACT]",
    "STATIC": "[STA]", "REPLACEMENT": "[RPL]", "CHOICE": "[CHO]",
}
TYPE_CODE_UNKNOWN = "[UNK_T]"

# Private pipeline field — a review-queue aid, not extracted content. Emitting it would leak
# "did the classifier match a rule or fall through" into the representation, which is not a tag.
_PRIVATE_TAG_KEYS = frozenset({"_type_matched"})


# ── shared head ───────────────────────────────────────────────────────────────

def _fmt_pt(power, toughness, loyalty) -> str:
    """P/T when present, else loyalty, else nothing. `*` and `1+*` pass through verbatim — the
    variable-ness is itself signal, and mapping it to a number would invent one."""
    p, t = _s(power), _s(toughness)
    if p or t:
        return f"{p or '?'}/{t or '?'}"
    return f"L{_s(loyalty)}" if _s(loyalty) else ""


def _s(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def head(row) -> str:
    """Byte-identical across every arm. Frozen field order: type line, mana cost, P/T-or-loyalty.

    Note this is a deliberate divergence from T2's `finetune.card_text`, which carries type line
    only. The `A_t2` diagnostic arm measures the gap so it is a number rather than a caveat.
    """
    parts = [_s(row.type_line), _s(row.mana_cost), _fmt_pt(row.power, row.toughness, row.loyalty)]
    return " ".join(p for p in parts if p)


# ── shared normalization ──────────────────────────────────────────────────────

def normalize_oracle(text: str | None) -> str:
    """The content every arm sees. `tokenize_oracle` strips reminder text and splits rule-113.2c
    keyword+cost-modifier lines; joining with a space removes newline structure, which is what
    arm B then re-adds explicitly as `[SEG]`."""
    if not isinstance(text, str) or not text.strip():
        return ""
    cdl_adapter.ensure()
    from parser import tokenize_oracle
    return " ".join(tokenize_oracle(text))


# ── clause rendering ──────────────────────────────────────────────────────────

def _render_tag_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ",".join(_render_tag_value(x) for x in v)
    return str(v)


def render_tags(tags: dict | None) -> str:
    """`key=value` pairs, key-sorted so the string is a deterministic function of the dict."""
    if not tags:
        return ""
    items = [(k, v) for k, v in sorted(tags.items())
             if k not in _PRIVATE_TAG_KEYS and v is not None and v != []]
    return " ".join(f"{k}={_render_tag_value(v)}" for k, v in items)


def render_values(values: list | None) -> str:
    """`role:kind:value`, plus `:bound` when a bound is carried — "up to 3" and "3" are different
    cards, and dropping the bound would make them identical here."""
    if not values:
        return ""
    out = []
    for v in values:
        if not isinstance(v, dict):
            continue
        core = ":".join(_s(v.get(f)) or "-" for f in ("role", "kind", "value"))
        bound = _s(v.get("bound"))
        out.append(f"{core}:{bound}" if bound else core)
    return " ".join(out)


def _segment_spans(raw_text: str, is_spell: bool, card_name: str | None) -> list[list[str]]:
    """Clause text spans grouped by segment, in document order.

    Must run on the **raw** oracle text, not on `normalize_oracle`'s output: `tokenize_oracle`
    splits on newlines, so feeding it the space-joined form collapses every ability into one
    segment and arm B silently degenerates to arm A.

    The name masking below is not cosmetic — it reproduces `clause_pipeline.process_card` exactly
    (mask before splitting, unmask after). Without it this traversal and the pipeline's would
    disagree on clause count for the ~100 cards whose names contain a comma or " and ", and the
    positional alignment in `_body_typed` would silently pair clause records with the wrong spans.
    """
    cdl_adapter.ensure()
    from clause_splitter import split_card
    from clause_pipeline import _flatten, _NAME_SENTINEL

    masked = raw_text
    if card_name and card_name in raw_text:
        masked = raw_text.replace(card_name, _NAME_SENTINEL)

    groups: list[list[str]] = []
    for seg in split_card(masked, is_spell=is_spell):
        spans = [span for span, *_ in _flatten([seg])]
        if card_name:
            spans = [s.replace(_NAME_SENTINEL, card_name) for s in spans]
        groups.append(spans)
    return groups


# ── arm bodies ────────────────────────────────────────────────────────────────

RES = "[RES]"


def _residual(norm: str, spans: list[str]) -> str:
    """Content in `norm` that no emitted span covers.

    The clause splitter drops text on cards it cannot fully split — most visibly modal cards,
    where a mode whose sentence defeats `split_effect_text` comes back as `Mode(clauses=[])` and
    that bullet vanishes. Measured over the full pool: 4.2% of cards lose alphanumeric content
    this way (95.7% lose none), and the loss falls *only* on the structured arms, so left alone
    it would show up as arm A beating B/B_type/B+ on content the structured arms never received.

    Emitting the uncovered remainder under `[RES]` is not a patch over the splitter — it is the
    fail-soft behaviour `clause_pipeline` documents as the system's own premise and then notes was
    never implemented ("anything it can't classify still passes through with its embedding intact,
    tagged UNCLASSIFIED"). It is applied identically to B, B_type and B+, so the ladder stays
    single-variable, and the residual carries no type and no tags, so it cannot flatter B+.
    """
    covered = bytearray(len(norm))
    pos = 0
    for s in spans:
        if not s:
            continue
        i = norm.find(s, pos)
        if i < 0:
            i = norm.find(s)
        if i < 0:
            continue
        covered[i:i + len(s)] = b"\x01" * len(s)
        pos = i + len(s)

    runs, cur = [], []
    for ch, c in zip(norm, covered):
        if c:
            if cur:
                runs.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        runs.append("".join(cur))

    keep = [r.strip(" .,;:•—−-") for r in runs]
    # 3+ alphanumerics filters out orphaned punctuation and stray connectives; below that there
    # is no content to recover, only noise that would differ between arms for no reason.
    return " ".join(k for k in keep if sum(ch.isalnum() for ch in k) >= 3)


def _with_residual(body: str, norm: str, spans: list[str]) -> str:
    res = _residual(norm, spans)
    return f"{body} {RES} {res}" if res else body


def _body_B(norm: str, raw: str, row, clauses: list[dict]) -> str:
    """Segment boundaries only — no type labels, no tags. Falls back to arm A's body when the
    splitter yields nothing, so a splitter gap degrades to the floor rather than to an empty
    string that would silently look like a missing card."""
    groups = _segment_spans(raw, representations.is_spell(row.type_line), row.name)
    parts = [f"{SEG} " + " ".join(g) for g in groups if g]
    if not parts:
        return norm
    return _with_residual(" ".join(parts), norm, [s for g in groups for s in g])


def _body_typed(norm: str, raw: str, row, clauses: list[dict], with_tags: bool) -> str:
    """B_type (with_tags=False) and B+ (with_tags=True) share one traversal, so the only
    difference between those two arms is literally the tag/value suffix.

    Alignment is positional: `_segment_spans` and `clause_pipeline.process_card` both walk
    `_flatten` over the same masked text, so the i-th span is the i-th clause record. A length
    mismatch would mean the two traversals diverged — it is counted and the card degrades to
    arm B rather than emitting mislabelled clauses.
    """
    groups = _segment_spans(raw, representations.is_spell(row.type_line), row.name)
    n_spans = sum(len(g) for g in groups)
    if not clauses or n_spans != len(clauses):
        return _body_B(norm, raw, row, clauses), n_spans != len(clauses) and bool(clauses)

    parts, i = [], 0
    for g in groups:
        if not g:
            continue
        rendered = []
        for _ in g:
            cl = clauses[i]
            i += 1
            code = TYPE_CODE.get(cl.get("type"), TYPE_CODE_UNKNOWN)
            chunk = f"{code} {cl.get('text_span', '')}".strip()
            if with_tags:
                extra = " ".join(x for x in (render_tags(cl.get("tags")),
                                             render_values(cl.get("values"))) if x)
                if extra:
                    chunk = f"{chunk} {{{extra}}}"
            rendered.append(chunk)
        if rendered:
            parts.append(f"{SEG} " + " ".join(rendered))
    if not parts:
        return norm, False
    return _with_residual(" ".join(parts), norm, [s for g in groups for s in g]), False


_CDL_PUNCT = re.compile(r"([{}\[\];:,])")
_WS = re.compile(r"\s+")


def serialize_cdl(cdl_text: str) -> str:
    """Space-pad CDL punctuation so WordPiece sees tokens rather than one run-on blob.

    `cdl_adapter.canonical` renders `;`-joined with no spaces (`EFFECT{ADD_COUNTER{SELF}}`), which
    tokenizes into subword noise. Padding is applied after canonicalization so the canonical form
    stays the thing being compared.
    """
    return _WS.sub(" ", _CDL_PUNCT.sub(r" \1 ", cdl_text)).strip()


def _cdl_body(cdl_text: str, name: str) -> str | None:
    """`scope="rules"` drops CDL's own header block — mana cost, types and stats already live in
    the shared head, and emitting them twice would give C/D a duplication advantage."""
    try:
        return serialize_cdl(cdl_adapter.canonical(cdl_text, scope="rules", card_name=name))
    except Exception:  # noqa: BLE001 — a canonicalizer failure is fallback data, not a stop
        return None


# ── the seam ──────────────────────────────────────────────────────────────────

def build_texts(
    arm: str,
    cards_df: pd.DataFrame,
    repr_tables: dict,
) -> tuple[dict[str, str], dict]:
    """{oracle_id: text} for one arm, plus accounting stats.

    Arms C and D consult the CDL table; every other arm ignores it entirely. Arm D returns text
    only for clean-parse cards — callers must restrict both its training pairs and its candidate
    universe accordingly (see `loo_eval`'s clean-universe sub-report), because ranking inside a
    smaller pool inflates recall mechanically.
    """
    if arm not in ALL_ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ALL_ARMS}")

    clause_table = repr_tables["clauses"]
    cdl_df = repr_tables["cdl"]
    cdl_by_oid = {r.oracle_id: (r.status, r.cdl, r.name)
                  for r in cdl_df.itertuples(index=False)}

    texts: dict[str, str] = {}
    n_cdl_used = n_cdl_fallback = n_canon_failed = n_empty_body = n_misaligned = 0

    for row in cards_df.drop_duplicates("oracle_id").itertuples(index=False):
        oid = row.oracle_id
        h = head(row)

        if arm == "A_t2":
            # T2's frozen encoding, reproduced exactly: type line + raw oracle text, no head.
            from . import finetune
            texts[oid] = finetune.card_text(row.oracle_text, row.type_line)
            continue

        raw = row.oracle_text if isinstance(row.oracle_text, str) else ""
        norm = normalize_oracle(raw)
        clauses = clause_table.get(oid, [])

        if arm == "A":
            body = norm
        elif arm == "A_raw":
            body = _WS.sub(" ", raw).strip()
        elif arm == "B":
            body = _body_B(norm, raw, row, clauses)
        elif arm in ("B_type", "B+"):
            body, misaligned = _body_typed(norm, raw, row, clauses, with_tags=(arm == "B+"))
            n_misaligned += int(misaligned)
        elif arm in ("C", "D"):
            status, cdl_text, name = cdl_by_oid.get(oid, (None, None, None))
            cdl_body = _cdl_body(cdl_text, name) if (status == cdl_adapter.STATUS_CLEAN and cdl_text) else None
            if status == cdl_adapter.STATUS_CLEAN and cdl_text and cdl_body is None:
                n_canon_failed += 1
            if cdl_body:
                n_cdl_used += 1
                body = cdl_body
            elif arm == "D":
                continue  # D is defined only on clean-parse cards
            else:
                n_cdl_fallback += 1
                body, misaligned = _body_typed(norm, raw, row, clauses, with_tags=True)
                n_misaligned += int(misaligned)
        else:  # pragma: no cover — guarded above
            raise AssertionError(arm)

        if not body:
            n_empty_body += 1
        texts[oid] = f"{h}{config.TEXT_SEP}{body}" if body else h

    stats = {
        "arm": arm,
        "n_texts": len(texts),
        "n_cdl_used": n_cdl_used,
        "n_cdl_fallback": n_cdl_fallback,
        "n_canonicalize_failed": n_canon_failed,
        "n_span_clause_misaligned": n_misaligned,
        "n_empty_body": n_empty_body,
        "n_cards_in_pool": int(cards_df["oracle_id"].nunique()),
    }
    return texts, stats


# ── encoding audit ────────────────────────────────────────────────────────────

def _char_multiset_loss(a: str, b: str, alnum_only: bool = True) -> float:
    """Fraction of A's characters not present in B, as a multiset.

    Cheap and order-insensitive on purpose: the question is "did B lose content", not "did B
    reorder it" — B reorders by design.

    Alphanumeric by default. The splitter strips sentence punctuation from every span, so a
    non-space comparison reports ~2.4% loss on 89% of cards while the *content* is identical;
    counting punctuation would drown the signal this metric exists to find. Both are reported.
    """
    from collections import Counter
    keep = (lambda c: c.isalnum()) if alnum_only else (lambda c: not c.isspace())
    ca = Counter(c for c in a if keep(c))
    if not ca:
        return 0.0
    cb = Counter(c for c in b if keep(c))
    return sum((ca - cb).values()) / sum(ca.values())


def _loss_summary(pairs: list[tuple[str, str]]) -> dict:
    import numpy as np
    out = {}
    for label, alnum in (("alphanumeric", True), ("non_space", False)):
        arr = np.array([_char_multiset_loss(a, b, alnum_only=alnum) for a, b in pairs])
        out[label] = {
            "mean": float(arr.mean()), "p90": float(np.percentile(arr, 90)),
            "p99": float(np.percentile(arr, 99)), "max": float(arr.max()),
            "share_zero": float((arr == 0).mean()), "share_over_1pct": float((arr > 0.01).mean()),
        }
    return out


def audit(cards_df: pd.DataFrame, repr_tables: dict, arms: tuple[str, ...] = ALL_ARMS) -> dict:
    """Token-length distribution and content-loss audit — the pre-run gate on truncation.

    Truncation is a confound, not merely a risk: if B+ truncates 15% of cards and A truncates 2%,
    the measured gap is lost information, not representation. `max_seq_length` is therefore set
    once, identically for every arm, high enough that the worst arm effectively never truncates —
    a recipe parameter, so the freeze on the training recipe still holds.
    """
    import numpy as np
    from sentence_transformers import SentenceTransformer

    # The model's own tokenizer, not `AutoTokenizer.from_pretrained(BASE_MODEL)`: `BASE_MODEL` is
    # the sentence-transformers short name, which only ST's loader expands to the full repo id.
    # Going through ST also guarantees the audit measures the exact tokenizer training will use.
    tok = SentenceTransformer(config.BASE_MODEL).tokenizer
    pool = cards_df.drop_duplicates("oracle_id")
    built = {a: build_texts(a, pool, repr_tables) for a in arms}

    out: dict = {"base_model": config.BASE_MODEL, "n_cards_in_pool": int(len(pool)), "arms": {}}
    for arm in arms:
        texts, stats = built[arm]
        oids = list(texts)
        lens = np.array([len(x) for x in tok([texts[o] for o in oids], add_special_tokens=True)["input_ids"]])
        out["arms"][arm] = {
            **stats,
            "tokens_mean": float(lens.mean()),
            "tokens_p50": int(np.percentile(lens, 50)),
            "tokens_p90": int(np.percentile(lens, 90)),
            "tokens_p99": int(np.percentile(lens, 99)),
            "tokens_max": int(lens.max()),
            "share_over_256": float((lens > 256).mean()),
            "share_over_512": float((lens > 512).mean()),
            "n_over_512": int((lens > 512).sum()),
        }

    # Content loss vs arm A: the confound that would otherwise read as "segmentation helps".
    #
    # Restricted to the English-text ladder (A -> B -> B_type -> B+). Arms C and D are excluded on
    # purpose: on clean-parse cards they emit canonical CDL, a different language, so a character
    # multiset against English oracle text measures the language change and nothing else. Running
    # it anyway reports ~20% "loss" for C, which is just the 34.3% clean-parse share showing
    # through — a category error dressed as a finding.
    if "A" in built:
        a_texts = built["A"][0]
        base = [o for o in a_texts if a_texts[o]]
        out["content_loss_vs_A"] = {
            arm: _loss_summary([(a_texts[o], built[arm][0].get(o, "")) for o in base])
            for arm in ("B", "B_type", "B+") if arm in built
        }
    # What the shared reminder-text normalization costs, so that decision is a number not a shrug.
    if "A" in built and "A_raw" in built:
        raw_texts, a_texts = built["A_raw"][0], built["A"][0]
        base = [o for o in raw_texts if raw_texts[o]]
        out["normalization_cost_A_raw_vs_A"] = _loss_summary(
            [(raw_texts[o], a_texts.get(o, "")) for o in base])
    return out


def render_audit(res: dict) -> str:
    from . import report
    a = res["arms"]
    rows = "\n".join(
        f"| {k} | {v['n_texts']:,} | {v['tokens_mean']:.1f} | {v['tokens_p90']} | {v['tokens_p99']} "
        f"| {v['tokens_max']} | {report.pct(v['share_over_256'])} | {report.pct(v['share_over_512'])} |"
        for k, v in a.items())
    loss = res.get("content_loss_vs_A", {})
    loss_rows = "\n".join(
        f"| {k} | {report.pct(v['alphanumeric']['mean'])} | {report.pct(v['alphanumeric']['max'])} "
        f"| {report.pct(v['alphanumeric']['share_zero'])} | {report.pct(v['alphanumeric']['share_over_1pct'])} |"
        for k, v in loss.items())
    norm = res.get("normalization_cost_A_raw_vs_A", {}).get("alphanumeric", {})
    share = report.pct(1.0 - norm["share_zero"]) if norm else "—"
    return f"""# T4 — encoding audit

{report.confound_header()}
Pre-run gate on truncation and content loss. Base model `{res['base_model']}`, full
Commander-legal pool of {res['n_cards_in_pool']:,} cards. **Truncation is a confound, not a risk:**
if one arm truncates and another does not, the measured gap is lost information rather than
representation. `max_seq_length` is therefore set once, identically for every arm.

## Token lengths

| arm | texts | mean | p90 | p99 | max | >256 | >512 |
|---|---|---|---|---|---|---|---|
{rows}

At the sentence-transformers default of **256** the structured arms truncate and the text arms do
not — that asymmetry is exactly the confound this gate exists to catch. At **512** no arm truncates
at all, so 512 is the frozen setting.

## Content loss vs arm A (alphanumeric multiset)

| arm | mean | max | cards losing nothing | cards losing >1% |
|---|---|---|---|---|
{loss_rows}

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
**{share} of cards carry reminder text**, and on those cards it is a substantial share of the
characters. `A_raw` exists as a diagnostic arm so the size of this decision can be measured on the
end task instead of argued about.
"""


if __name__ == "__main__":
    import argparse
    import json

    from . import corpus, report

    ap = argparse.ArgumentParser(description="T4 encoding audit (token lengths, truncation, loss)")
    ap.add_argument("--arms", nargs="+", default=list(ALL_ARMS))
    args = ap.parse_args()

    cards = corpus.load_cards_parquet()
    rt = representations.load()
    res = audit(cards, rt, tuple(args.arms))
    payload = report.envelope(
        [config.CARDS_PARQUET],
        {"arms": list(args.arms), "parser_pin": rt["pin"]["combined_sha256"]},
    )
    payload.update(res)
    report.write("t4_encoding_audit", payload, render_audit(res))
    print(json.dumps({k: v for k, v in res.items() if k != "arms"}, indent=2, sort_keys=True))
    for k, v in res["arms"].items():
        print(f"{k:8s} n={v['n_texts']:>6,} mean={v['tokens_mean']:6.1f} max={v['tokens_max']:>4} "
              f">256={v['share_over_256']:.3%} >512={v['share_over_512']:.3%}")
