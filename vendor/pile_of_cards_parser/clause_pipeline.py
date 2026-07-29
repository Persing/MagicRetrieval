"""
Clause-splitting pipeline driver: chains all five stages into one call for
a real (non-gold) card.

    split_card -> classify -> extract_tags -> extract_values -> link_card

Each stage was built and validated independently against clause_gold_v1.json
(see clause_pipeline_handoff.md); this module is the glue that assembles a
real card's oracle text into the per-clause record shape structural_linker
expects (text_span/type/tags/values, in document order) and runs the full
chain. It does not add any new logic of its own beyond the assembly.
"""
from clause_splitter import split_card
from type_classifier import classify
from tag_extractor import extract_tags
from numeral_extractor import extract_values
from structural_linker import link_card


def _flatten(segs):
    """(text_span, kind_hint, segment_kind, in_choice_mode, is_choice_header)
    in document order -- the order structural_linker's antecedent search
    depends on.

    When a segment has a choice container, `seg.clauses` holds clauses
    from BOTH sides of the choice header (an optional leading TRIGGER
    clause split off before the header check ran, plus any trailing
    sentences after the mode lines) concatenated into one flat list --
    the header itself sits between them in the real text but isn't
    represented positionally in the data. split_card() only ever adds a
    leading clause via one path (clause_splitter.py's `_TRIGGER_CUE.match`
    branch), which always produces at most one clause and always tags it
    kind_hint='TRIGGER' -- so that's the exact, not approximate, signal
    for where the split falls (confirmed against Midnight Crusader
    Shuttle, Genku, and Wild Shape's differing shapes)."""
    out = []
    for seg in segs:
        if seg.choice:
            leading = seg.clauses[:1] if seg.clauses and seg.clauses[0].kind_hint == 'TRIGGER' else []
            trailing = seg.clauses[len(leading):]
            for cl in leading:
                out.append((cl.text_span, cl.kind_hint, seg.kind, False, False))
            out.append((seg.choice.header_span, None, seg.kind, False, True))
            for mode in seg.choice.modes:
                for cl in mode.clauses:
                    out.append((cl.text_span, cl.kind_hint, seg.kind, True, False))
            for cl in trailing:
                out.append((cl.text_span, cl.kind_hint, seg.kind, False, False))
        else:
            for cl in seg.clauses:
                out.append((cl.text_span, cl.kind_hint, seg.kind, False, False))
    return out


# A card's own name is a PROPER NOUN, not parseable game content -- but
# every stage downstream reads it as ordinary text, so its incidental
# characters get treated as grammar and arithmetic:
#
#   "Whenever Gregor, Shrewd Magistrate deals combat damage to a player"
#       -> the comma splits the name in half; the TRIGGER loses its event
#          and target_scope, and "Shrewd Magistrate deals combat damage"
#          becomes a bogus standalone ACTION       (~100 cards)
#   "Wrenn and Six deals 1 damage to any target"
#       -> " and " splits the name, AND "Six" is extracted as a numeral,
#          reporting damage:6 alongside the real damage:1   (24 cards)
#
# Masking the name to a single inert alphabetic token before any stage
# sees it fixes both classes at once, and leaves all five stages
# untouched. The token is deliberately alphabetic and space/comma/digit
# free so that every existing word-boundary regex keeps working, and it is
# threaded through as `card_name` so tag_extractor's self-reference
# detection still matches. Spans and string tag values are un-masked on
# the way out, so callers never see the sentinel.
_NAME_SENTINEL = 'Selfcardname'


def _unmask(value, name):
    if isinstance(value, str):
        return value.replace(_NAME_SENTINEL, name)
    if isinstance(value, list):
        return [_unmask(v, name) for v in value]
    return value


def process_card(oracle_text, is_spell=False, card_name=None):
    """Runs the full pipeline over one card's oracle text and returns the
    linked clause list: a list of dicts, each with `text_span`, `type`,
    `tags` (dict, includes `resolved_reference` where an anaphor was
    resolved), and `values` (list, each possibly carrying
    `resolved_binding`).

    `is_spell` and `card_name` are genuine external context oracle text
    alone can't supply (see known-issues #3) -- pass them from the card
    record, same as every validator does.
    """
    masked, effective_name = oracle_text, card_name
    if card_name and card_name in oracle_text:
        masked = oracle_text.replace(card_name, _NAME_SENTINEL)
        effective_name = _NAME_SENTINEL

    segs = split_card(masked, is_spell=is_spell)
    clauses = []
    for text_span, kind_hint, seg_kind, in_choice, is_header in _flatten(segs):
        ctype, matched = classify(text_span, kind_hint=kind_hint, segment_kind=seg_kind,
                                   in_choice_mode=in_choice, is_choice_header=is_header,
                                   return_matched=True)
        tags = extract_tags(text_span, ctype, kind_hint=kind_hint,
                             segment_kind=seg_kind, card_name=effective_name)
        values = extract_values(text_span, kind_hint=kind_hint)
        clauses.append({'text_span': text_span, 'type': ctype, 'tags': tags,
                        'values': values, '_type_matched': matched})
    link_card(clauses)

    # confidence, per clause_gold_v1.json's VALID_CONF vocabulary.
    #
    # The system's own premise (clause-splitting-system-outline.md section 1)
    # is that it is FAIL-SOFT: "anything it can't classify still passes
    # through with its embedding intact, tagged UNCLASSIFIED, never blocking
    # the pipeline." That was never actually implemented -- no stage emitted
    # confidence at all, so the headline coverage metric (%RULE vs
    # %UNCLASSIFIED) was unmeasurable and a clause the pipeline understood
    # perfectly was indistinguishable from one it understood not at all.
    #
    # UNCLASSIFIED means "nothing was learned here": no tag and no value was
    # extracted. Deliberately NOT keyed on the type alone -- "Draw a card"
    # reaches the ACTION fall-through but carries a real value and is a
    # correct, useful clause. Conversely a TRIGGER whose cue matched but
    # which produced no `event` learned nothing despite its type coming from
    # a positive rule, and is exactly the population worth reviewing.
    # `_type_matched` is kept as a private field so the review queue can
    # still separate "decided ACTION" from "defaulted to ACTION".
    for cl in clauses:
        cl['confidence'] = 'RULE' if (cl['tags'] or cl['values']) else 'UNCLASSIFIED'

    if effective_name is not card_name:
        for cl in clauses:
            cl['text_span'] = _unmask(cl['text_span'], card_name)
            cl['tags'] = {k: _unmask(v, card_name) for k, v in cl['tags'].items()}
            for v in cl['values']:
                for field in ('expr_span', 'resolved_binding'):
                    if field in v:
                        v[field] = _unmask(v[field], card_name)
    return clauses


if __name__ == '__main__':
    import sys
    import json

    if len(sys.argv) > 1:
        name = ' '.join(sys.argv[1:])
        corpus = json.load(open('scryfall_corpus_full.json', encoding='utf-8'))
        card = next((c for c in corpus if c['name'].lower() == name.lower()), None)
        if card is None:
            print(f"no card named {name!r} in scryfall_corpus_full.json")
            sys.exit(1)
        is_spell = 'Instant' in card.get('types', '') or 'Sorcery' in card.get('types', '')
        clauses = process_card(card['oracle_text'], is_spell=is_spell, card_name=card['name'])
        print(f"{card['name']}  ({card.get('types', '?')})")
        print(card['oracle_text'])
        print()
        for cl in clauses:
            print(f"[{cl['type']}] {cl['text_span']!r}")
            if cl['tags']:
                print(f"    tags: {cl['tags']}")
            if cl['values']:
                print(f"    values: {cl['values']}")
    else:
        print("usage: python clause_pipeline.py <card name>")
