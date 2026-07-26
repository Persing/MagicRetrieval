"""canonical() invariance — the correctness-critical piece of T1.

If these don't hold, T1's divergence rate is measuring formatting, not representation.
"""

from mr import cdl_adapter as A


def test_permuted_order_insensitive_block_is_identical():
    a = 'CARD {\n  SPELL {\n    EFFECT: [\n      AND { TYPE: "Basic" TYPE: "Land" }\n    ]\n  }\n}'
    b = 'CARD {\n  SPELL {\n    EFFECT: [\n      AND { TYPE: "Land" TYPE: "Basic" }\n    ]\n  }\n}'
    assert A.canonical(a) == A.canonical(b)


def test_sequential_effect_order_is_significant():
    """EFFECT lists are sequences, not sets — draw-then-discard != discard-then-draw."""
    a = "CARD {\n  SPELL {\n    EFFECT: [\n      DRAW { COUNT: 1 };\n      DISCARD { COUNT: 1 }\n    ]\n  }\n}"
    b = "CARD {\n  SPELL {\n    EFFECT: [\n      DISCARD { COUNT: 1 };\n      DRAW { COUNT: 1 }\n    ]\n  }\n}"
    assert A.canonical(a) != A.canonical(b)
    # Strict mode sorts everything, so it should NOT be able to tell these apart —
    # exactly why strict is reported as a bound, not the primary number.
    assert A.canonical(a, sort_all=True) == A.canonical(b, sort_all=True)


def test_renamed_binding_is_identical():
    a = 'CARD {\n  SPELL {\n    EFFECT: [\n      SEARCH { ZONE: YOU.library } AS $found;\n      MOVE { $found TO: YOU.hand }\n    ]\n  }\n}'
    b = 'CARD {\n  SPELL {\n    EFFECT: [\n      SEARCH { ZONE: YOU.library } AS $x;\n      MOVE { $x TO: YOU.hand }\n    ]\n  }\n}'
    assert A.canonical(a) == A.canonical(b)


def test_whitespace_and_indentation_do_not_matter():
    a = 'CARD {\nNAME: "Foo"\nMANA_COST: "{1}"\n}'
    b = 'CARD {   NAME:    "Foo"\n\n\nMANA_COST:  "{1}"   }'
    assert A.canonical(a) == A.canonical(b)


def test_genuinely_different_effect_is_different():
    a = 'CARD {\n  SPELL {\n    EFFECT: [\n      DRAW { COUNT: 1 }\n    ]\n  }\n}'
    b = 'CARD {\n  SPELL {\n    EFFECT: [\n      DESTROY { TARGET: target }\n    ]\n  }\n}'
    assert A.canonical(a) != A.canonical(b)


def test_scope_rules_drops_header_fields():
    a = 'CARD {\n  NAME: "Alpha"\n  MANA_COST: "{1}{W}"\n  MANA_ABILITY {\n    COST: TAP\n    ADD_MANA: { "{W}" }\n  }\n}'
    b = 'CARD {\n  NAME: "Beta"\n  MANA_COST: "{2}"\n  MANA_ABILITY {\n    COST: TAP\n    ADD_MANA: { "{W}" }\n  }\n}'
    assert A.canonical(a, scope="rules") == A.canonical(b, scope="rules")
    assert A.canonical(a, scope="full") != A.canonical(b, scope="full")


def test_scope_rules_drops_list_form_header_fields():
    """TYPES/SUBTYPES are emitted as bracketed lists (TYPES: ["Creature"]), which parse to a
    tuple node rather than a flushed leaf string. Two cards differing only in creature type must
    still compare equal under scope=rules — otherwise every functional pair with different
    subtypes reports as divergent for a reason that has nothing to do with the CDL representation."""
    a = 'CARD {\n  TYPES: ["Creature"]\n  SUBTYPES: ["Frog"]\n  TRIGGERED {\n    EFFECT: {}\n  }\n}'
    b = 'CARD {\n  TYPES: ["Artifact", "Creature"]\n  SUBTYPES: ["Robot", "Scorpion"]\n  TRIGGERED {\n    EFFECT: {}\n  }\n}'
    assert A.canonical(a, scope="rules") == A.canonical(b, scope="rules")
    assert A.canonical(a, scope="full") != A.canonical(b, scope="full")


def test_card_name_self_reference_is_masked():
    a = 'CARD {\n  NAME: "Foo"\n  STATIC {\n    EFFECT: { PUMP { TARGET: "Foo" AMOUNT: 1 } }\n  }\n}'
    b = 'CARD {\n  NAME: "Bar"\n  STATIC {\n    EFFECT: { PUMP { TARGET: "Bar" AMOUNT: 1 } }\n  }\n}'
    assert A.canonical(a, card_name="Foo") == A.canonical(b, card_name="Bar")


def test_mask_numbers_ignores_numeric_literal_differences():
    a = "CARD {\n  MANA_ABILITY {\n    EFFECT: { DRAW { COUNT: 2 } }\n  }\n}"
    b = "CARD {\n  MANA_ABILITY {\n    EFFECT: { DRAW { COUNT: 3 } }\n  }\n}"
    assert A.canonical(a) != A.canonical(b)
    assert A.canonical(a, mask_numbers=True) == A.canonical(b, mask_numbers=True)


def test_mask_symbols_ignores_mana_and_colour_differences():
    a = 'CARD {\n  MANA_ABILITY {\n    ADD_MANA: { "{U}{B}" }\n  }\n}'
    b = 'CARD {\n  MANA_ABILITY {\n    ADD_MANA: { "{U}{R}" }\n  }\n}'
    assert A.canonical(a) != A.canonical(b)
    assert A.canonical(a, mask_symbols=True) == A.canonical(b, mask_symbols=True)


def test_mask_symbols_handles_hybrid_mana():
    """A hybrid symbol like {R/W} must be masked as a unit, not left partially matched —
    otherwise a locket/hybrid cycle looks structurally divergent when only its colours differ."""
    a = 'CARD {\n  MANA_ABILITY {\n    ADD_MANA: { CHOICE ["{R/W}", "{R/W}"] }\n  }\n}'
    b = 'CARD {\n  MANA_ABILITY {\n    ADD_MANA: { CHOICE ["{B/G}", "{B/G}"] }\n  }\n}'
    assert A.canonical(a, mask_symbols=True) == A.canonical(b, mask_symbols=True)


def test_comments_and_gap_markers_are_stripped():
    a = 'CARD {\n  # SPEC_GAP: effect not matched: something\n  NAME: "Foo"\n}'
    b = 'CARD {\n  NAME: "Foo"\n}'
    assert A.canonical(a) == A.canonical(b)


def test_gap_category_extraction():
    assert A.gap_category("# SPEC_GAP: effect not matched: foo bar") == "effect not matched"
    assert A.gap_category("# UNKNOWN_KEYWORD: Ninjutsu") == "UNKNOWN_KEYWORD"


def test_parse_result_status_properties():
    clean = A.ParseResult(status=A.STATUS_CLEAN, cdl="CARD {}")
    gap = A.ParseResult(status=A.STATUS_GAP, cdl="CARD { # SPEC_GAP: x }")
    excluded = A.ParseResult(status=A.STATUS_EXCLUDED, exclusion_reason="multi_faced")
    crash = A.ParseResult(status=A.STATUS_CRASH, error="boom")

    assert clean.ok and clean.encoded
    assert not gap.ok and gap.encoded
    assert not excluded.ok and not excluded.encoded
    assert not crash.ok and not crash.encoded
