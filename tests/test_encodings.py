"""The single-variable guarantee, in tests.

T4's whole claim is that six arms differ in exactly one thing. That claim is only as good as
these assertions: nothing else in the harness re-checks it, and a serialization bug that quietly
gives one arm more content than another would show up as a representation finding.
"""

import pandas as pd
import pytest

from mr import cdl_adapter, config, encodings


# ── fixtures ──────────────────────────────────────────────────────────────────

def _card(oracle_id, name, type_line, oracle_text, mana_cost="{1}", power=None, toughness=None,
          loyalty=None):
    return {"oracle_id": oracle_id, "name": name, "type_line": type_line,
            "oracle_text": oracle_text, "mana_cost": mana_cost, "power": power,
            "toughness": toughness, "loyalty": loyalty}


@pytest.fixture
def cards():
    return pd.DataFrame([
        _card("oid-bolt", "Bolt", "Instant", "Bolt deals 3 damage to any target.", "{R}"),
        _card("oid-ring", "Ring", "Artifact", "{T}: Add {C}{C}.", "{1}"),
        _card("oid-bear", "Bear", "Creature — Bear",
              "Whenever Bear attacks, draw a card.", "{1}{G}", power="2", toughness="2"),
        _card("oid-blank", "Blank", "Artifact", None, "{2}"),
    ])


@pytest.fixture
def repr_tables(cards):
    """Real clause + CDL tables for the fixture cards — the pipeline is deterministic and fast,
    and stubbing it would let a serialization bug pass by testing the stub instead."""
    from mr import representations
    cdl_adapter.ensure()
    from clause_pipeline import process_card

    clauses = {
        r.oracle_id: (process_card(r.oracle_text, is_spell=representations.is_spell(r.type_line),
                                   card_name=r.name) if isinstance(r.oracle_text, str) else [])
        for r in cards.itertuples(index=False)
    }
    cdl = pd.DataFrame([
        {"oracle_id": "oid-bolt", "name": "Bolt", "status": cdl_adapter.STATUS_CLEAN,
         "cdl": 'CARD {\n  NAME: "Bolt"\n  SPELL {\n    EFFECT: [ DAMAGE { AMOUNT: 3 } ]\n  }\n}'},
        {"oracle_id": "oid-ring", "name": "Ring", "status": cdl_adapter.STATUS_CLEAN,
         "cdl": 'CARD {\n  NAME: "Ring"\n  MANA_ABILITY {\n    COST: TAP\n  }\n}'},
        {"oracle_id": "oid-bear", "name": "Bear", "status": cdl_adapter.STATUS_GAP,
         "cdl": 'CARD {\n  # SPEC_GAP: effect not matched: x\n  NAME: "Bear"\n}'},
        {"oracle_id": "oid-blank", "name": "Blank", "status": cdl_adapter.STATUS_EXCLUDED,
         "cdl": None},
    ])
    return {"clauses": clauses, "cdl": cdl}


def _build(arm, cards, repr_tables):
    return encodings.build_texts(arm, cards, repr_tables)


# ── the shared head ───────────────────────────────────────────────────────────

def test_head_is_byte_identical_across_every_arm(cards, repr_tables):
    """Spec: mana cost, type line and P/T are held constant in every arm. If they aren't, an arm
    could win on a card attribute rather than on its representation."""
    built = {a: _build(a, cards, repr_tables)[0] for a in encodings.ARMS}
    for r in cards.itertuples(index=False):
        head = encodings.head(r)
        for arm, texts in built.items():
            if r.oracle_id not in texts:
                continue  # arm D is defined only on clean-parse cards
            assert texts[r.oracle_id].startswith(head), f"{arm} lost the shared head"


def test_head_carries_mana_cost_and_pt_unlike_t2(cards):
    bear = next(r for r in cards.itertuples(index=False) if r.name == "Bear")
    assert encodings.head(bear) == "Creature — Bear {1}{G} 2/2"


def test_loyalty_is_used_when_there_is_no_pt():
    row = pd.DataFrame([_card("x", "Walker", "Planeswalker", "text", "{3}", loyalty="4")]).itertuples(index=False)
    assert encodings.head(next(row)).endswith("L4")


# ── the ladder is single-variable ─────────────────────────────────────────────

def test_arm_A_body_is_exactly_the_normalized_oracle_text(cards, repr_tables):
    texts, _ = _build("A", cards, repr_tables)
    for r in cards.itertuples(index=False):
        body = texts[r.oracle_id].removeprefix(encodings.head(r)).removeprefix(config.TEXT_SEP)
        assert body == encodings.normalize_oracle(r.oracle_text)


def test_B_adds_segment_markers_and_nothing_else(cards, repr_tables):
    a, _ = _build("A", cards, repr_tables)
    b, _ = _build("B", cards, repr_tables)
    for oid in a:
        assert encodings.SEG not in a[oid]
        # every clause-type marker belongs to B_type and above, never to B
        assert not any(code in b[oid] for code in encodings.TYPE_CODE.values())


def test_B_type_adds_type_markers_over_B(cards, repr_tables):
    b, _ = _build("B", cards, repr_tables)
    bt, _ = _build("B_type", cards, repr_tables)
    assert any(any(c in bt[oid] for c in encodings.TYPE_CODE.values()) for oid in bt)
    for oid in b:
        assert b[oid] != bt[oid] or not encodings.SEG in b[oid]


def test_Bplus_is_B_type_plus_tags_only(cards, repr_tables):
    """The only difference between B_type and B+ must be the `{...}` tag/value suffixes. Strip
    them and the two arms have to be byte-identical — otherwise `B+ - B_type` is not measuring tags."""
    import re
    bt, _ = _build("B_type", cards, repr_tables)
    bp, _ = _build("B+", cards, repr_tables)
    strip = lambda s: re.sub(r"\s*\{[^{}]*\}\s*", " ", s)
    for oid in bt:
        # mana symbols are themselves braced, so compare only cards with no mana symbol in body
        if "{" in bt[oid].split(config.TEXT_SEP, 1)[-1]:
            continue
        assert strip(bp[oid]).split() == strip(bt[oid]).split()


# ── arms C and D ──────────────────────────────────────────────────────────────

def test_C_equals_Bplus_on_every_non_clean_card(cards, repr_tables):
    c, _ = _build("C", cards, repr_tables)
    bp, _ = _build("B+", cards, repr_tables)
    clean = set(repr_tables["cdl"].query("status == 'clean'")["oracle_id"])
    for oid in bp:
        if oid in clean:
            assert c[oid] != bp[oid], "C must use CDL on clean-parse cards"
        else:
            assert c[oid] == bp[oid], "C's fallback must be B+ byte-for-byte"


def test_D_is_defined_only_on_clean_parse_cards(cards, repr_tables):
    d, stats = _build("D", cards, repr_tables)
    clean = set(repr_tables["cdl"].query("status == 'clean'")["oracle_id"])
    assert set(d) == clean
    assert stats["n_texts"] == len(clean)


def test_C_and_D_agree_on_clean_cards(cards, repr_tables):
    c, _ = _build("C", cards, repr_tables)
    d, _ = _build("D", cards, repr_tables)
    for oid in d:
        assert c[oid] == d[oid]


def test_cdl_is_rendered_without_its_own_header_block(cards, repr_tables):
    """The shared head already supplies name/cost/types/stats. Emitting CDL's header too would
    give arms C/D a duplicated-feature advantage that has nothing to do with representation."""
    d, _ = _build("D", cards, repr_tables)
    assert not any("NAME" in t for t in d.values())


def test_serialize_cdl_pads_punctuation_for_wordpiece():
    assert encodings.serialize_cdl("CARD{SPELL{EFFECT:[DAMAGE{AMOUNT:3}]}}") == (
        "CARD { SPELL { EFFECT : [ DAMAGE { AMOUNT : 3 } ] } }")


# ── accounting: nothing may be silently dropped ───────────────────────────────

def test_every_pool_card_gets_text_in_every_arm_except_D(cards, repr_tables):
    for arm in ("A", "B", "B_type", "B+", "C"):
        texts, stats = _build(arm, cards, repr_tables)
        assert len(texts) == stats["n_cards_in_pool"] == len(cards)


def test_C_accounting_closes(cards, repr_tables):
    _, stats = _build("C", cards, repr_tables)
    assert stats["n_cdl_used"] + stats["n_cdl_fallback"] == stats["n_cards_in_pool"]


def test_card_without_oracle_text_still_gets_its_head(cards, repr_tables):
    texts, _ = _build("A", cards, repr_tables)
    assert texts["oid-blank"] == encodings.head(
        next(r for r in cards.itertuples(index=False) if r.oracle_id == "oid-blank"))


# ── determinism ───────────────────────────────────────────────────────────────

def test_build_texts_is_byte_identical_on_repeat(cards, repr_tables):
    for arm in encodings.ARMS:
        assert _build(arm, cards, repr_tables)[0] == _build(arm, cards, repr_tables)[0]


def test_tag_rendering_is_key_sorted_not_dict_ordered():
    a = encodings.render_tags({"zone": "HAND", "modality": "MAY"})
    b = encodings.render_tags({"modality": "MAY", "zone": "HAND"})
    assert a == b == "modality=MAY zone=HAND"


def test_private_pipeline_fields_never_reach_the_representation():
    """`_type_matched` records whether the classifier matched a rule or fell through. It is a
    review-queue aid, not extracted content, and leaking it would put classifier confidence into
    the representation under the guise of a tag."""
    assert encodings.render_tags({"_type_matched": True, "zone": "HAND"}) == "zone=HAND"


def test_value_bound_is_kept():
    """"up to 3" and "3" are different cards; dropping `bound` would make them identical here."""
    with_bound = encodings.render_values([{"role": "count", "kind": "LITERAL", "value": 3, "bound": "UP_TO"}])
    exact = encodings.render_values([{"role": "count", "kind": "LITERAL", "value": 3, "bound": "EXACT"}])
    assert with_bound != exact


# ── the residual tail ─────────────────────────────────────────────────────────

def test_residual_recovers_text_no_span_covered():
    norm = "Choose one — Destroy target Aura. The next time it would be destroyed, remove damage."
    spans = ["Choose one —", "Destroy target Aura"]
    res = encodings._residual(norm, spans)
    assert "The next time it would be destroyed" in res
    assert "Destroy target Aura" not in res


def test_residual_is_empty_when_spans_cover_everything():
    assert encodings._residual("Draw a card.", ["Draw a card"]) == ""


def test_residual_ignores_orphaned_punctuation():
    assert encodings._residual("Draw a card. •", ["Draw a card"]) == ""


def test_unknown_arm_raises(cards, repr_tables):
    with pytest.raises(ValueError, match="unknown arm"):
        _build("B++", cards, repr_tables)
