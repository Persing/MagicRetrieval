"""
Clause-splitting system, stage 1-3: tokenize -> segment -> clause-split.

Design rationale and every boundary decision below is backed by a specific
worked example in clause_gold_v1.json -- see that file's per-clause "note"
fields for the reasoning. This module does NOT classify clause type, extract
tags, or pull numerals; it only finds boundaries. See
clause-splitting-system-outline.md for the full pipeline and build order.

Reuses parser.py's stage-1/2 tokenizer rather than re-solving newline
splitting, reminder-text stripping, and the keyword/cost-modifier split.

KNOWN OUT OF SCOPE this version: VOTE_OPTIONS (will-of-the-council/council's-
dilemma cards, e.g. Orchard Elemental) are not detected -- they never say
"choose" and need their own header grammar (Section _vote_census in the gold
set). Left as a documented gap, not silently mishandled.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from parser import tokenize_oracle, _find_cost_colon, _looks_like_cost


# ─────────────────────────────────────────────────────────────────── model

@dataclass
class Clause:
    text_span: str
    kind_hint: Optional[str] = None  # COST | TRIGGER | CONDITION | None -- structural hint only, not final type


@dataclass
class Mode:
    index: int
    label: Optional[str]
    clauses: list = field(default_factory=list)


@dataclass
class ChoiceContainer:
    header_span: str
    delimiter: str  # BULLET | INLINE_OR | PLUS_COST | PIP_COST
    modes: list = field(default_factory=list)  # list[Mode]


@dataclass
class Segment:
    kind: str  # SPELL | ACTIVATED | TRIGGERED | STATIC | KEYWORD | SAGA_CHAPTER | REMINDER | LOYALTY_ABILITY
    clauses: list = field(default_factory=list)  # list[Clause] -- empty for REMINDER, empty when choice is set
    chapters: Optional[list] = None
    choice: Optional[ChoiceContainer] = None


# ─────────────────────────────────────────────────────────────────── lexicons
# Every entry here traces back to a specific gold-set clause. Do not add
# words speculatively -- grow this list only when a new gold card forces it
# (clause-splitting-system-outline.md section 5's bottom-up discipline).

_IMPERATIVE_VERBS = {
    'put', 'create', 'destroy', 'exile', 'draw', 'discard', 'search', 'shuffle',
    'return', 'sacrifice', 'tap', 'untap', 'gain', 'gains', 'lose', 'loses',
    'deal', 'deals', 'counter', 'reveal', 'mill', 'scry', 'fight',
    'fights', 'attach', 'proliferate', 'distribute', 'choose', 'play', 'cast',
    'copy', 'become', 'becomes', 'get', 'gets', 'regenerate', 'add',
    # Added after a corpus coverage check found 91 CHOICE modes across 62
    # cards being SILENTLY DELETED -- _looks_like_parameter_value treats a
    # mode with no recognized verb as a bare parameter descriptor and emits
    # zero clauses for it, so a real effect whose verb was missing here
    # vanished entirely (Shifting Grift lost 96% of its text; 10 cards lost
    # every mode they had). These are the verbs that were missing.
    'prevent', 'change', 'exchange', 'switch', 'transform', 'explore',
    'connive', 'block', 'blocks', 'phase', 'goad', 'surveil', 'venture',
    'manifest', 'populate', 'support', 'bolster', 'monstrosity', 'adapt',
    'amass', 'investigate', 'meld', 'unlock', 'untaps', 'taps',
}
# NOTE: 'target' deliberately excluded -- at clause-start it is virtually
# always the noun-phrase-initial "target creature/player" (adjective use),
# never a bare verb. Including it caused Wild Shape's "target creature you
# control has..." to false-split after "Until end of turn,".

_TRIGGER_CUE = re.compile(r'^(whenever|when|at the beginning of)\b', re.IGNORECASE)
_CONDITION_CUE = re.compile(r'^(if|unless|as long as)\b', re.IGNORECASE)

# Section 15.29-style distributive quantifier -- NOT a CONDITION cue despite
# the original design outline lumping "for each" in with if/unless. CDL models
# EACH as its own construct (Section 15.29), distinct from CONDITION. Comma
# after "for each ..." does NOT split -- confirmed by Hate Mirage's
# "For each of those creatures, create a token..." staying one clause.
_DISTRIBUTIVE_CUE = re.compile(r'^for each\b', re.IGNORECASE)

# Chapter/level line. Two surface forms for the same structure:
#   "I, II — Exile cards..."   Saga chapters, roman numerals + em-dash
#   "9+ | Flying, first strike"  Level Up / Class / dice-table tiers, which
#                                this corpus renders with N|/N+| instead
# The second form had no handler at all: 73 lines fell through to generic
# per-line splitting, which produced junk like keyword:'otherwise' on
# Treasure Chest. Both parser.py and this splitter disagreed on every one
# of them, which is how the cross-oracle check surfaced it.
# The N-form also covers dice-outcome RANGES ("2—9 | Create five Treasure
# tokens", Treasure Chest) alongside plain tiers ("9+ |").
_SAGA_CHAPTER = re.compile(
    r'^([IVX]+(?:,\s*[IVX]+)*)\s*—\s*(.*)$'
    r'|^(\d+(?:\s*[—–-]\s*\d+)?\+?)\s*\|\s*(.*)$')

# Comma boundary for the leading-TRIGGER split, guarded against digit
# grouping separators ("1,000"). A comma immediately followed by a digit is
# part of a number, never a clause boundary.
_TRIGGER_COMMA = re.compile(r',(?!\d)\s*')

_VILLAINOUS_CHOICE = re.compile(r"faces? an? villainous choice\s*[—-]", re.IGNORECASE)

# Ability-word flavor prefix on a TOP-LEVEL line ("Midnight Entity — Whenever
# ...", "Council's dilemma — When ..."). Only strips when immediately
# followed by a TRIGGER cue -- never touches bullet/mode-label text, which
# uses _strip_mode_label instead and is meaningful (Khans/Dragons, Combine
# Powers!/Defense!/Fight!).
_FLAVOR_PREFIX = re.compile(
    r"^[\w'!:,.\- ]{1,40}? — (?=(?:Whenever|When|At the beginning of)\b)")

_PLUS_MODE = re.compile(r'^\+\s*((?:\{[^}]+\})+)\s*—\s*(.*)$')
# (?:\{[^}]+\})+ -- one or MORE mana-symbol groups, not just one. The
# original single-group version silently failed to match any Spree/
# additional-cost mode whose cost has more than one symbol ('{2}{B}'),
# which broke mode/choice detection for the WHOLE card, not just that
# mode (known-issues #12, Unfortunate Accident -- confirmed the single-
# group version returned None on '+ {2}{B} — Destroy target creature.'
# while matching the single-symbol '+ {1} — ...' line fine).
_PIP_MODE = re.compile(r'^((?:\{P\})+)\s*—\s*(.*)$')  # (?:...)+ -- NOT \{P\}+, which only repeats the closing brace


def _starts_with_imperative(text: str) -> bool:
    m = re.match(r"[A-Za-z']+", text.strip())
    return bool(m) and m.group(0).lower() in _IMPERATIVE_VERBS


# "target X ... GETS ..." starting the right-hand side of an ' and ' split:
# a SECOND independent target+predicate, not a shared trailing verb applied
# to both targets jointly (which would take PLURAL agreement, "get" not
# "gets", since the joint subject would be "target A and target B"). Confirmed
# corpus shape, exactly 3 instances, all using "gets" (Skulduggery: "target
# creature you control gets +1/+1 and target creature an opponent controls
# gets -1/-1"; same shape in Gurmag Rakshasa, Monoist Circuit-Feeder) -- kept
# to the verb this evidence forces, not speculatively widened to other verbs.
_TARGET_VERBED_RHS = re.compile(r'^target\b.{0,80}?\bgets\b', re.IGNORECASE)

# Elliptical second-recipient continuation of a "deals N damage to ..." clause
# (Fire and Brimstone: "deals 4 damage to target player who attacked this
# turn and 4 damage to you"; Lunge: "...to target creature and 2 damage to
# target player or planeswalker") -- the RHS has NO verb of its own (the
# "deals" is elided, understood from the LHS), so _starts_with_imperative can
# never fire for it. type_classifier/tag_extractor already handle a bare
# "N damage to X" fragment correctly with no verb present (pattern-matched,
# not verb-dependent) -- confirmed directly before writing this, not assumed
# -- so only the SPLIT BOUNDARY itself was missing.
#
# Deliberately scoped to two confirmed-safe recipient shapes (11 + 2 corpus
# lines respectively, exhaustively checked): a literal "target" noun phrase
# (optionally "up to N [other] target...") and the bare pronouns "you"/
# "them". Deliberately does NOT cover "any target" (different wording, not
# in the reviewed evidence set), "another target"/"a third target" (N-way
# chains -- Cone of Flame, Serpentine Spike -- a different, still-open
# splitting shape), "that creature's controller"/"its controller" (a
# relative back-reference, not an independent recipient), "itself", or "each
# creature ..." (mass/ALL scope) -- all confirmed present in the corpus under
# the same surface "and N damage to ..." shape but deliberately deferred,
# scoped out by the user pending their own downstream-tag review. See
# clause_pipeline_known_issues.md, "multi-target restriction conflation".
_ELLIPTICAL_DAMAGE_RHS = re.compile(
    r'^\d+\s+damage\s+to\s+(?:up to \w+ (?:other )?)?target\b'
    r'|^\d+\s+damage\s+to\s+(?:you|them)\b', re.IGNORECASE)


def _mode_line_kind(line: str) -> Optional[str]:
    if line.startswith('•'):
        return 'BULLET'
    if _PLUS_MODE.match(line):
        return 'PLUS_COST'
    if _PIP_MODE.match(line):
        return 'PIP_COST'
    return None


# ─────────────────────────────────────────────────────────────────── comma/and/sentence splitting

def _split_top_level(text: str, seps: list) -> list:
    """Split on any of `seps` (each a compiled regex) at paren/brace depth 0."""
    depth = 0
    out = []
    buf = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in '({':
            depth += 1
        elif ch in ')}':
            depth -= 1
        if depth == 0:
            matched = False
            for sep in seps:
                m = sep.match(text, i)
                if m:
                    out.append(''.join(buf))
                    buf = []
                    i = m.end()
                    matched = True
                    break
            if matched:
                continue
        buf.append(ch)
        i += 1
    out.append(''.join(buf))
    return [s.strip() for s in out]


_THEN_SEP = re.compile(r',\s*then\s+')
_COMMA_SEP = re.compile(r',\s+')
_AND_SEP = re.compile(r'\s+and\s+')
_SEMI_SEP = re.compile(r';\s*')
_SENTENCE_SEP = re.compile(r'\.\s+(?=[A-Z])')


def _split_sentences(text: str) -> list:
    """Split on internal sentence-final periods ('. ' followed by a capital
    letter). MTG oracle text has no abbreviations that would false-positive
    this (no 'Mr.', 'etc.', decimals) -- safe to be simple."""
    return _split_top_level(text, [_SENTENCE_SEP])


def _split_and_if_both_sides_verbed(text: str) -> list:
    """
    Split on ' and ' only when the right-hand side starts with its own
    imperative verb -- i.e. two independent actions, not a compound object
    or an Oxford-list continuation. Validated against Lorehold Command
    ('get +1/+0 and gain indestructible and haste' -- splits once, after
    'get', not twice) and Last Night Together (3-item Oxford list after one
    verb 'gain' -- does not split at all). The split-off clause KEEPS the
    'and' prefix (gold: 'and gain indestructible and haste until end of
    turn', 'and gains that ability') -- it is not stripped.

    Also splits when the right-hand side is a SECOND "target X ... gets ..."
    predicate (_TARGET_VERBED_RHS) -- these have their own verb too, just not
    as the literal first word (Skulduggery: "target creature you control
    gets +1/+1 and target creature an opponent controls gets -1/-1" is two
    independent target+predicate clauses, not a compound object of one verb;
    a shared trailing predicate over both targets would use plural
    agreement -- "get", not "gets" -- so this doesn't false-positive on that
    shape).

    Also splits on an elliptical "N damage to <target/you/them>" second
    recipient with no verb of its own (_ELLIPTICAL_DAMAGE_RHS) -- see that
    regex's own comment for exactly which recipient shapes are (and are
    deliberately not yet) covered.
    """
    parts = _AND_SEP.split(text)
    if len(parts) == 1:
        return [text]
    out = [parts[0]]
    for p in parts[1:]:
        if _starts_with_imperative(p) or _TARGET_VERBED_RHS.match(p) \
                or _ELLIPTICAL_DAMAGE_RHS.match(p):
            out.append('and ' + p)
        else:
            out[-1] = out[-1] + ' and ' + p
    return [c.rstrip(',').strip() for c in out]


_QUOTE_PUNCT_MASK = {'.': '\x00', ',': '\x01', ';': '\x02'}
_QUOTE_PUNCT_UNMASK = {v: k for k, v in _QUOTE_PUNCT_MASK.items()}


def _mask_quoted_punctuation(text: str) -> str:
    """A double-quoted substring is a NESTED granted/printed ability's own
    text -- its internal '.'/','/';' are sentence/clause boundaries WITHIN
    that embedded ability, never boundaries of the outer clause describing
    it (Unfortunate Accident: 'Create a ... token with "{T}: ... end of
    turn. Activate only as a sorcery."' must stay ONE clause, not split at
    the period before 'Activate'). Sentinel-replace them so every splitter
    downstream (sentence/semicolon/comma) simply never sees them; reversed
    by _unmask_quoted_punctuation on each final split-off piece."""
    def repl(m):
        inner = m.group(0)
        for k, v in _QUOTE_PUNCT_MASK.items():
            inner = inner.replace(k, v)
        return inner
    return re.sub(r'"[^"]*"', repl, text)


def _unmask_quoted_punctuation(text: str) -> str:
    for placeholder, original in _QUOTE_PUNCT_UNMASK.items():
        text = text.replace(placeholder, original)
    return text


def split_effect_text(text: str) -> list:
    """
    Split one chunk of effect/condition/action text into clause-candidate
    spans. Order: strip trailing period -> semicolons -> sentences -> the
    comma/and/then family within each sentence.
    """
    text = _mask_quoted_punctuation(text.strip())
    if text.endswith('.'):
        text = text[:-1]
    if not text:
        return []

    out = []
    for semi_part in _SEMI_SEP.split(text):
        semi_part = semi_part.strip()
        if not semi_part:
            continue
        for sentence in _split_sentences(semi_part):
            sentence = sentence.strip()
            if sentence:
                out.extend(_split_one_sentence(sentence))
    return [_unmask_quoted_punctuation(s) for s in out if s]


# Delayed trigger folded into a flat spell effect list (Hate Mirage: "Exile
# them at the beginning of the next end step"). No comma boundary exists --
# the trigger-cue phrase itself is the only anchor. Gold's clause ORDER puts
# the trigger first even though it appears second in the text -- narrow,
# single-example rule (see _decisions.known_gaps / gaps_closed_this_pass in
# the gold set: the 5th containment shape), not generalized beyond this shape.
_TRAILING_TRIGGER_INLINE = re.compile(r'^(.*?)\s+(at the beginning of\b.*)$', re.IGNORECASE)


def _split_one_sentence(text: str) -> list:
    # "for each ..." is a distributive quantifier (CDL Section 15.29 EACH),
    # not a CONDITION -- stays one atomic clause. Confirmed by Hate Mirage's
    # "For each of those creatures, create a token..." staying unsplit.
    if _DISTRIBUTIVE_CUE.match(text):
        return [text]

    # "If [X] would [Y], [Z] instead" is a REPLACEMENT effect's own trigger
    # condition (614.1c), not a general CONDITION -- stays one atomic clause,
    # same treatment as Myriad Landscape's 'enters tapped'. Elspeth, Storm
    # Slayer: 'If one or more tokens would be created under your control,
    # twice that many...are created instead' must NOT split at the comma.
    if _CONDITION_CUE.match(text) and 'would' in text.lower() and 'instead' in text.lower():
        return [text]

    # Leading TRIGGER or CONDITION cue: split off at the first top-level comma,
    # recurse on the remainder.
    if _TRIGGER_CUE.match(text) or _CONDITION_CUE.match(text):
        parts = _split_top_level(text, [re.compile(r',\s*')])
        if len(parts) > 1:
            head, rest = parts[0], ', '.join(parts[1:])
            return [head] + _split_one_sentence(rest)
        return [text]

    trailing_trigger = _TRAILING_TRIGGER_INLINE.match(text)
    if trailing_trigger and trailing_trigger.group(1).strip():
        return [trailing_trigger.group(2).strip(), trailing_trigger.group(1).strip()]

    # ", then <verb>" -- always a boundary (sequential action chain).
    then_parts = _split_top_level(text, [_THEN_SEP])
    if len(then_parts) > 1:
        first = _split_comma_imperative_chain(then_parts[0])
        rest = [f'then {p}' for p in then_parts[1:]]
        return first + rest

    return _split_comma_imperative_chain(text)


def _split_comma_imperative_chain(text: str) -> list:
    """
    Bare comma before an imperative verb = a new sequential action
    (Myriad Landscape: 'Search...that share a land type, put them onto the
    battlefield tapped'). A comma NOT followed by an imperative verb is left
    alone (relative clauses, 'of their choice', 'for each of X,' etc. never
    trigger this).
    """
    parts = _split_top_level(text, [_COMMA_SEP])
    if len(parts) == 1:
        return _split_and_if_both_sides_verbed(text)

    out = [parts[0]]
    for p in parts[1:]:
        if _starts_with_imperative(p):
            out.append(p)
        else:
            out[-1] = out[-1] + ', ' + p
    return [seg for chunk in out for seg in _split_and_if_both_sides_verbed(chunk)]


# ─────────────────────────────────────────────────────────────────── cost/effect split

def split_activated_or_mana_line(line: str) -> Optional[tuple]:
    """Returns (cost_text, effect_text) if `line` has a cost:effect colon, else None."""
    pos = _find_cost_colon(line)
    if pos is None:
        return None
    cost_text = line[:pos].strip()
    effect_text = line[pos + 1:].strip()
    return cost_text, effect_text


def split_cost_components(cost_text: str) -> list:
    """Cost side splits on EVERY top-level comma -- components are always
    independent (Myriad Landscape: '{2}, {T}, Sacrifice this land' -> 3)."""
    return [c.strip() for c in _split_top_level(cost_text, [_COMMA_SEP]) if c.strip()]


# ─────────────────────────────────────────────────────────────────── modal grouping

def _strip_mode_label(bullet_text: str) -> tuple:
    """'Khans — At the beginning...' -> ('Khans', 'At the beginning...').
    'Combine Powers! — Put three...' -> ('Combine Powers!', 'Put three...').
    No label present -> (None, bullet_text) unchanged."""
    m = re.match(r"^([\w!' ]{1,24}?) — (.+)$", bullet_text)
    if m and not _TRIGGER_CUE.match(bullet_text):
        return m.group(1).strip(), m.group(2).strip()
    return None, bullet_text


def _looks_like_parameter_value(text: str) -> bool:
    """Bare descriptor with no verb anywhere (Wild Shape/Genku PARAMETER_VALUES
    bullets: '1/3 Turtle with hexproof', '2/2 white Fox with vigilance'), as
    opposed to a real effect clause. Syntactic signal: no imperative-verb
    token anywhere in the text. This is a boundary-stage decision (does a
    clause exist here at all), not a type-classification decision -- the
    splitter still needs to know whether to emit a clause for a bullet body."""
    # crude stem match (strip a trailing 's') so 3rd-person-singular
    # conjugations (sacrifices, creates) count as the verb they conjugate --
    # bug found via Season of Loss / Ruinous Wrecking Crew's "Each player
    # sacrifices..." and Inscription of Insight's "...creates..." silently
    # vanishing because only the bare form was in the lexicon.
    words = re.findall(r"[A-Za-z']+", text.lower())
    return not any(w in _IMPERATIVE_VERBS or w.rstrip('s') in _IMPERATIVE_VERBS for w in words)


def _split_header_and_trailing(body: str) -> tuple:
    """
    A CHOICE header's source line sometimes runs past the header itself into
    unrelated trailing sentences (Wild Shape: 'Choose one. Until end of
    turn, ... and gains that ability.' -- only 'Choose one.' is the header;
    Genku: '... choose one that hasn't been chosen this turn. Create a
    creature token with those characteristics.' -- only the choose-sentence
    is the header). A subsequent sentence is ABSORBED into the header only
    if it still talks about the choice mechanic itself (contains 'choose') --
    covers both the conditional-count override (Jeska's Will, Inscription of
    Insight: '...you may choose both/any number instead') and the
    repeatable-flag sentence (Season of Loss: 'You may choose the same mode
    more than once.'). Anything else becomes trailing top-level clauses.
    """
    if not body.strip():
        return '', []  # Spree's empty header (Requisition Raid) -- no sentence to absorb-check

    sentences = [s.rstrip('.') for s in _split_sentences(body)]
    k = 1
    while k < len(sentences) and 'choose' in sentences[k].lower():
        k += 1
    header = '. '.join(sentences[:k])
    if not header.endswith(('—', '-')):
        header += '.'
    return header, sentences[k:]


# ─────────────────────────────────────────────────────────────────── top-level driver

def split_card(oracle_text: str, is_spell: bool = False) -> list:
    """Stage 1-3 driver: oracle text -> list[Segment], clauses split but not
    typed/tagged. Segments/clauses inside a CHOICE container live under
    Segment.choice.modes[i].clauses, not Segment.clauses directly.

    is_spell: True for Instant/Sorcery cards. Needed because a bare line
    with no cost-colon/trigger/loyalty pattern is a STATIC ability on a
    permanent but a one-shot SPELL effect on an instant/sorcery -- the
    oracle text alone can't tell these apart, this is genuine card-type
    context the caller must supply. Defaults to False (permanent) rather
    than guessing."""
    lines = tokenize_oracle(oracle_text)
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # A parenthetical-only ORACLE LINE (e.g. a Saga's intro reminder
        # text) is a segment with zero clauses, not a bug.
        if line.startswith('(') and line.endswith(')'):
            segments.append(Segment(kind='REMINDER', clauses=[]))
            i += 1
            continue

        chapter_m = _SAGA_CHAPTER.match(line)
        chapters = None
        body = line
        if chapter_m:
            # Groups 1/2 are the roman-numeral form, 3/4 the N|/N+| form.
            label = chapter_m.group(1) or chapter_m.group(3)
            chapters = [c.strip() for c in label.split(',')]
            body = chapter_m.group(2) if chapter_m.group(1) else chapter_m.group(4)

        body = _strip_flavor_prefix(body)

        kind = 'SAGA_CHAPTER' if chapters else _infer_ability_kind(body, is_spell=is_spell)
        seg = Segment(kind=kind, chapters=chapters)

        # Split off a leading TRIGGER clause BEFORE checking for a modal
        # CHOICE header, so a card like Genku ("Whenever ... , choose one
        # that hasn't been chosen this turn. Create a token...") gets the
        # TRIGGER separated first and the choice-header check runs on the
        # remainder, not the whole trigger+choice+trailing blob.
        remainder = body
        if _TRIGGER_CUE.match(body):
            # Two guards on this comma split, both from real corpus damage:
            #  - _TRIGGER_COMMA skips a comma sitting between digits, so a
            #    literal like "1,000" is not torn into "1" and "000 life"
            #    (The Millennium Calendar).
            #  - quoted punctuation is masked first, so a comma INSIDE a
            #    granted ability's quoted text is not a boundary (Vren,
            #    Tangled Colony, The Mycotyrant). split_effect_text already
            #    does this downstream, but this split runs before it.
            # Both previously produced spans that were not even substrings
            # of the source, because the ', '.join below re-joined them
            # with an injected space.
            masked_body = _mask_quoted_punctuation(body)
            parts = _split_top_level(masked_body, [_TRIGGER_COMMA])
            if len(parts) > 1:
                parts = [_unmask_quoted_punctuation(p) for p in parts]
                seg.clauses.append(Clause(text_span=parts[0], kind_hint='TRIGGER'))
                remainder = ', '.join(parts[1:])

        villainous = _VILLAINOUS_CHOICE.search(remainder)
        is_choice_header = ('choose' in remainder.lower() and _next_is_mode_line(lines, i)) or villainous

        if is_choice_header:
            delimiter = 'INLINE_OR' if villainous else _mode_line_kind(lines[i + 1])
            trailing_sentences = _attach_choice(seg, remainder, lines, i, delimiter)
            for sent in trailing_sentences:
                for c in _split_one_sentence(sent):
                    seg.clauses.append(Clause(text_span=c))
            segments.append(seg)
            if delimiter != 'INLINE_OR':
                j = i + 1
                while j < len(lines) and _mode_line_kind(lines[j]) == delimiter:
                    j += 1
                i = j
                continue
            i += 1
            continue

        # Spree: KEYWORD segment, then a PLUS_COST choice with an EMPTY
        # header (there is no literal 'choose' sentence on these cards --
        # Spree itself IS the modal marker). See Requisition Raid's gold note.
        if body.strip().lower() == 'spree' and i + 1 < len(lines) and _mode_line_kind(lines[i + 1]) == 'PLUS_COST':
            segments.append(Segment(kind='KEYWORD', clauses=[Clause(text_span='Spree', kind_hint='COST')]))
            seg = Segment(kind='SPELL')
            _attach_choice(seg, '', lines, i, 'PLUS_COST')
            segments.append(seg)
            j = i + 1
            while j < len(lines) and _mode_line_kind(lines[j]) == 'PLUS_COST':
                j += 1
            i = j
            continue

        _fill_segment_body(seg, remainder, lines, i)
        segments.append(seg)
        i += 1

    return segments


def _strip_flavor_prefix(line: str) -> str:
    m = _FLAVOR_PREFIX.match(line)
    return line[m.end():] if m else line


def _next_is_mode_line(lines: list, idx: int) -> bool:
    return idx + 1 < len(lines) and _mode_line_kind(lines[idx + 1]) is not None


_KEYWORD_COST_LINE = re.compile(r'^(Crew|Kicker|Flashback|Buyback|Awaken|Bestow)\b')
# Crew/Kicker are gold-backed (Midnight Crusader Shuttle, Inscription of
# Insight). Flashback/Buyback/Awaken/Bestow added per known-issues #3b and
# clause_gold_v1.json's Cabal Therapy entry -- all four are corpus-common
# (checked: Flashback 228, Bestow 43, Buyback 29, Awaken 16 cards) and share
# the same shape (a keyword line naming its own -- possibly non-mana --
# cost), extended together rather than one at a time since the forcing
# example (Cabal Therapy) validates the shared mechanism, not just Flashback
# specifically.


def _infer_ability_kind(line: str, is_spell: bool = False) -> str:
    if _find_cost_colon(line) is not None:
        return 'ACTIVATED'
    if _TRIGGER_CUE.match(line):
        return 'TRIGGERED'
    if re.match(r'^[+\-−0-9]+:', line):
        return 'LOYALTY_ABILITY'
    if _KEYWORD_COST_LINE.match(line):
        return 'KEYWORD'
    # Default fallback needs card-type context: a bare line with no cost-
    # colon/trigger/loyalty pattern is a STATIC ability on a permanent, but
    # the identical shape on an instant/sorcery is a one-shot SPELL effect
    # (Cerebral Vortex, Victimize, Wild Shape, Hate Mirage, Last Night
    # Together all mistyped ACTION-as-STATIC downstream before this was
    # threaded through -- see clause_pipeline_known_issues.md #3).
    return 'SPELL' if is_spell else 'STATIC'


def _fill_segment_body(seg: Segment, line: str, all_lines: list, idx: int) -> None:
    cost_split = split_activated_or_mana_line(line)
    if cost_split:
        cost_text, effect_text = cost_split
        # ONE COST clause even for multi-component costs ('{2}, {T}, Sacrifice
        # this land' stays one clause with 3 cost_components in its later
        # tags, not 3 separate COST clauses) -- per the gold set's own
        # decision, Myriad Landscape. split_cost_components() remains
        # available for the tag-extraction stage to populate cost_components.
        seg.clauses.append(Clause(text_span=cost_text, kind_hint='COST'))
        for c in split_effect_text(effect_text):
            seg.clauses.append(Clause(text_span=c))
        return

    if seg.kind == 'LOYALTY_ABILITY':
        m = re.match(r'^([+\-−0-9]+):\s*(.*)$', line)
        if m:
            seg.clauses.append(Clause(text_span=m.group(1), kind_hint='COST'))
            for c in split_effect_text(m.group(2)):
                seg.clauses.append(Clause(text_span=c))
            return

    if seg.kind == 'KEYWORD' and _KEYWORD_COST_LINE.match(line):
        # Crew/Kicker lines never carry a trailing period; Flashback/
        # Buyback/Awaken/Bestow lines are full sentences and do (Cabal
        # Therapy: "Flashback—Sacrifice a creature.") -- strip it so the
        # clause span matches the sentence-boundary convention every other
        # clause in the pipeline follows (split_effect_text does this too).
        seg.clauses.append(Clause(text_span=line.rstrip('.'), kind_hint='COST'))
        return

    # NOTE: TRIGGERED-line leading-comma splitting now happens centrally in
    # split_card (before the choice-header check runs), not here -- `line`
    # arriving in this function is already past any TRIGGER clause.

    # Comma-separated BARE keyword list (e.g. "Vigilance, deathtouch, haste")
    # -- split on every comma, no verb-gating. Deliberately narrow: every
    # comma-part must be a single word/token, or this would also swallow
    # ordinary prose sentences that happen to start with a non-imperative
    # word and contain a comma (Elspeth, Storm Slayer's replacement clause,
    # 'If one or more tokens would be created..., twice that many...instead',
    # starts with 'If' and has a comma but is NOT a keyword list).
    comma_parts = _split_top_level(line, [re.compile(r',\s*')])
    if len(comma_parts) > 1 and all(re.fullmatch(r"[A-Za-z][\w'-]*", p) for p in comma_parts):
        for c in comma_parts:
            seg.clauses.append(Clause(text_span=c))
        return

    for c in split_effect_text(line):
        seg.clauses.append(Clause(text_span=c))


def _attach_choice(seg: Segment, remainder: str, all_lines: list, idx: int, delimiter: str) -> list:
    """Builds seg.choice and returns any TRAILING sentence texts (not yet
    clause-split) that follow the choice on the same source line but are not
    part of it -- the caller appends these to seg.clauses."""
    container = ChoiceContainer(header_span='', delimiter=delimiter)
    seg.choice = container

    if delimiter == 'INLINE_OR':
        m = re.search(r'(faces? an? villainous choice)\s*[—-]\s*(.*)$', remainder, re.IGNORECASE)
        container.header_span = remainder[:m.start(2)].strip()  # keeps the trailing em-dash, per gold
        rest = m.group(2)
        sentences = _split_sentences(rest)
        modes_blob = sentences[0]
        trailing = [s.rstrip('.') for s in sentences[1:]]
        modes_text = re.split(r',?\s+or\s+', modes_blob, maxsplit=1)
        for i, mt in enumerate(modes_text):
            if i > 0:
                mt = 'or ' + mt  # gold keeps the 'or' on the split-off mode (Midnight Crusader Shuttle)
            mode = Mode(index=i, label=None)
            for c in split_effect_text(mt):
                mode.clauses.append(Clause(text_span=c))
            container.modes.append(mode)
        return trailing

    header, trailing = _split_header_and_trailing(remainder)
    container.header_span = header

    j = idx + 1
    mode_i = 0
    while j < len(all_lines) and _mode_line_kind(all_lines[j]) == delimiter:
        raw = all_lines[j]
        label = None
        if delimiter == 'BULLET':
            body_text = raw[1:].strip()
            label, body_text = _strip_mode_label(body_text)
        elif delimiter == 'PLUS_COST':
            m = _PLUS_MODE.match(raw)
            body_text = m.group(2)
        else:  # PIP_COST
            m = _PIP_MODE.match(raw)
            body_text = m.group(2)

        mode = Mode(index=mode_i, label=label)
        # PARAMETER_VALUES bullets (Wild Shape, Genku): bare descriptors with
        # no verb anywhere -- no clauses emitted, only labels-with-no-body
        # would be misleading. Whether a mode's content is "real effect
        # text" vs "parameter data" is decided here, syntactically, not left
        # for a later stage -- see _looks_like_parameter_value's docstring.
        if label is None and _looks_like_parameter_value(body_text):
            pass
        else:
            for c in split_effect_text(body_text):
                mode.clauses.append(Clause(text_span=c))
        container.modes.append(mode)
        mode_i += 1
        j += 1

    return trailing
