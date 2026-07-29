"""
Clause type classification: TRIGGER | COST | CONDITION | ACTION | STATIC |
REPLACEMENT | CHOICE. Takes a clause already produced by clause_splitter.py
(text, kind_hint, and its structural context) and assigns the type gold's
`type` field carries. Does not extract tags or values -- see
numeral_extractor.py for values; tag extraction is the next stage.

Most of the work is done BY the splitter already, not by lexical guessing
here: kind_hint='COST'/'TRIGGER' and CHOICE-header status are structural
facts the splitter determined while finding boundaries, and this module
trusts them outright (validated: 0 mismatches against gold, see
validate_type_classifier.py). Only CONDITION, REPLACEMENT, STATIC, and the
STATIC/ACTION default split require new logic.
"""
import re

from clause_splitter import _TRIGGER_CUE

# REPLACEMENT: "would ... instead" is sufficient but not necessary (Section
# _decisions.replacement_vs_static in the gold set) -- a closed list of
# templated forms with no 'would' at all (enters tapped, enters with N
# counters) are replacement effects too. Grow this list one gold/corpus
# example at a time; do not generalize past what's observed.
_REPLACEMENT_TEMPLATED = re.compile(
    r"\benters? (the battlefield )?tapped\b|"
    r"\benters? (the battlefield )?with .+ counters?\b",
    re.IGNORECASE)

# CANT-shaped restriction (CDL Section 18). ONLY is CANT's dual ("only X can
# Y" means everything else can't) -- both classify as STATIC regardless of
# which segment they're nested in, because the restriction itself is a
# continuous rule, not a discrete effect (Last Night Together's "Only the
# chosen creatures can attack during that combat phase" is inside a SPELL
# segment, not a STATIC one, and still classifies STATIC).
_CANT_PATTERN = re.compile(r"\bcan't\b|\bcannot\b", re.IGNORECASE)
_ONLY_PATTERN = re.compile(r'^\s*only\b.*\bcan\b', re.IGNORECASE)

# Optional leading 'then ' -- a ', then' sequential split (clause_splitter's
# _split_one_sentence) can leave a dangling 'Then if X, Y' fragment whose
# condition-cue isn't at position 0 (Darigaaz Reincarnated: 'Then if this
# card has no egg counters on it, return it to the battlefield' -- the
# BOUNDARY is already correct via the generic imperative-comma split, this
# only fixes the classification falling through to ACTION).
_CONDITION_CUE = re.compile(r'^(?:then\s+)?(if|unless|as long as)\b', re.IGNORECASE)


def classify(text: str, kind_hint: str = None, segment_kind: str = None,
             is_choice_header: bool = False, in_choice_mode: bool = False,
             return_matched: bool = False):
    """
    return_matched: when True, returns (type, matched_rule) instead of just
        type. `matched_rule` is False iff the type came from the bare ACTION
        fall-through at the bottom -- i.e. no rule fired and ACTION is a
        DEFAULT, not a decision. Callers need this to distinguish "the
        classifier decided this is an action" from "the classifier ran out
        of rules", which are very different confidence levels and were
        previously indistinguishable in the output. Default False keeps
        every existing caller working unchanged.

    text: the clause's text_span.
    kind_hint: Clause.kind_hint from clause_splitter.py ('COST' | 'TRIGGER' | None).
    segment_kind: the containing Segment.kind ('STATIC' | 'SPELL' | 'ACTIVATED' |
        'TRIGGERED' | 'LOYALTY_ABILITY' | 'SAGA_CHAPTER' | 'KEYWORD' | 'REMINDER').
    is_choice_header: True iff this clause IS a ChoiceContainer.header_span.
    in_choice_mode: True iff this clause lives inside a CHOICE mode's clause
        list (Mode.clauses), not directly in Segment.clauses -- this is what
        keeps Boros Charm's "Permanents you control gain indestructible"
        ACTION instead of STATIC despite otherwise STATIC-shaped phrasing.
    """
    ctype, matched = _classify(text, kind_hint, segment_kind,
                               is_choice_header, in_choice_mode)
    return (ctype, matched) if return_matched else ctype


def _classify(text, kind_hint, segment_kind, is_choice_header, in_choice_mode):
    """Returns (type, matched_rule). Every positive rule reports True; only
    the final fall-through reports False."""
    if is_choice_header:
        return 'CHOICE', True
    if kind_hint == 'COST':
        return 'COST', True
    # kind_hint is a fast-path shortcut, not the sole source of truth: a
    # TRIGGER clause inside an ABILITY_SELECTION mode (Citadel Siege's
    # "Khans"/"Dragons" bullets, each a nested TRIGGERED segment) never gets
    # kind_hint set by the splitter -- that hint is only assigned by the
    # top-level split_card driver, not by _attach_choice's bullet-body path.
    # Text-matching the same cue the splitter itself uses is the fallback.
    if kind_hint == 'TRIGGER' or _TRIGGER_CUE.match(text):
        return 'TRIGGER', True

    if 'would' in text.lower() and 'instead' in text.lower():
        return 'REPLACEMENT', True
    if _REPLACEMENT_TEMPLATED.search(text):
        return 'REPLACEMENT', True

    # CONDITION_CUE is checked BEFORE CANT_PATTERN/ONLY_PATTERN. A clause
    # split off by clause_splitter as its own unit and opening with
    # if/unless/as long as IS the condition clause -- clause_splitter
    # already separates a genuine "as long as X, Y-can't-Z" static
    # restriction into two clauses (the "as long as X" condition and the
    # "Y can't Z" restriction), confirmed by Bladed Bracers' identical
    # shape. So a standalone clause starting with a condition cue is never
    # itself the CANT-restriction being described; it's checking whether
    # something happened/is possible, and "can't" occurring later in its
    # own text (Urgoros the Empty One: "If the player can't", checking
    # whether a prior discard succeeded) must not preempt that. Found via
    # Phase 5 random-sample grading -- CANT_PATTERN's unanchored .search()
    # ran first and hijacked every "if ... can't" clause into STATIC before
    # this check was ever reached.
    if _CONDITION_CUE.match(text):
        return 'CONDITION', True

    if _CANT_PATTERN.search(text) or _ONLY_PATTERN.search(text):
        return 'STATIC', True

    # Structural rule, not grammatical: STATIC only for a clause that is the
    # WHOLE content of a top-level STATIC-kind segment, never one nested
    # inside a spell/mode/trigger-effect -- confirmed by the Boros Charm
    # ("Permanents you control gain indestructible", ACTION, inside a CHOICE
    # mode) vs Glorious Anthem ("Creatures you control get +1/+1", STATIC,
    # top-level) minimal pair. Near-identical phrasing, opposite type,
    # decided entirely by structural position.
    if segment_kind == 'STATIC' and not in_choice_mode:
        return 'STATIC', True

    return 'ACTION', False
