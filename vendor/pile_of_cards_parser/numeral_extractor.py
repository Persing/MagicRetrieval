"""
Numeral parameterization: pulls quantities out of an already-split clause's
text_span into structured value records, per _decisions.value_model in
clause_gold_v1.json. Does not classify clause type or extract other tags --
input is one clause's text, output is its `values` list.

Five value kinds: LITERAL | VAR | COUNT | SCALED | CHARACTERISTIC. Every
record carries {role, kind, value, var_name, binding, expr_span, bound}
(+ multiplier when kind is SCALED or a per-unit COUNT). binding is always
emitted as None here -- resolving which OTHER clause a dynamic value points
at requires cross-clause context this per-clause extractor does not have;
see _decisions.value_model.gaps_closed_building_extractor for why that's a
deliberate scope boundary, not an oversight.
"""
import re

_NUMBER_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    # Rare past ten, but real (found via corpus check, not speculative):
    # Persistent Petitioners "mills TWELVE cards", Polukranos, Unchained
    # "escapes with TWELVE +1/+1 counters", Conqueror's Pledge "create
    # TWELVE of those tokens instead".
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'twenty': 20,
}
_NUMBER_WORD_ALT = '|'.join(_NUMBER_WORDS)
_NUM = rf'(?:\d+|{_NUMBER_WORD_ALT})'


def _num_val(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBER_WORDS[word.lower()]


def _lit(role, value, bound='EXACT'):
    return {'role': role, 'kind': 'LITERAL', 'value': value, 'var_name': None,
            'binding': None, 'expr_span': None, 'bound': bound}


def _count(role, expr_span, bound='EXACT', multiplier=None):
    v = {'role': role, 'kind': 'COUNT', 'value': None, 'var_name': None,
         'binding': None, 'expr_span': expr_span, 'bound': bound}
    if multiplier is not None:
        v['multiplier'] = multiplier
    return v


def _var(role, var_name, expr_span, bound='EXACT'):
    return {'role': role, 'kind': 'VAR', 'value': None, 'var_name': var_name,
            'binding': None, 'expr_span': expr_span, 'bound': bound}


def _characteristic(role, expr_span, bound='EXACT'):
    return {'role': role, 'kind': 'CHARACTERISTIC', 'value': None, 'var_name': None,
            'binding': None, 'expr_span': expr_span, 'bound': bound}


def _scaled(role, multiplier, expr_span, bound='EXACT'):
    return {'role': role, 'kind': 'SCALED', 'value': None, 'var_name': None,
            'binding': None, 'expr_span': expr_span, 'bound': bound, 'multiplier': multiplier}


# ─────────────────────────────────────────────────────────────────── role/bound inference

def _infer_role(text: str, counter_window: str = None) -> str:
    """Governing-keyword lookup, priority-ordered. Grown one gold clause at a
    time -- every branch traces to a specific worked example, none added
    speculatively (outline section 5's bottom-up discipline).

    `counter_window`, when given, is used only for the 'counter'/'add'
    checks below, while `text` (which may be a narrower local window -- see
    the bare-number fallback call site) governs everything else. 'N or
    more <adjective> counters' (Vivisection Evangelist's poison-counter
    threshold, Voice of the Blessed's +1/+1-counter threshold) is a common
    idiom with enough intervening descriptive words that the tighter
    window built for the damage/life/mana-value checks (see that call
    site's own comment) cuts 'counters' off entirely, so 'counter'/'add'
    get their own, wider window -- but NOT the unwindowed whole clause
    (tried first, reverted: it let an unrelated 'counter'/'countered'
    mention anywhere else in a longer clause bleed onto a bare number that
    has nothing to do with it, e.g. Metamorphosis Fanatic's "up to ONE
    target creature card ... with a lifelink COUNTER on it" tagging the
    creature count 'counter_count', and Psychic Rebuttal's "two or more
    instant and/or sorcery cards ... the spell COUNTERED this way" false-
    matching the substring 'counter' inside 'countered' from clear across
    the clause -- the exact same class of bug this whole fix targets, just
    reintroduced at a wider radius). Defaults to `text` itself, preserving
    the original whole-clause behavior for every other call site."""
    if counter_window is None:
        counter_window = text
    tl = text.lower()
    if 'damage' in tl:
        return 'damage'
    if re.search(r'\blose[s]?\b.*\blife\b', tl):
        return 'life_loss'
    if re.search(r'\bgain[s]?\b.*\blife\b', tl):
        return 'life'
    if 'mana value' in tl:
        return 'mv_threshold'
    if re.search(r'\bpower\b', tl) and re.search(r'\bor (less|fewer|greater|more)\b', tl):
        return 'power_threshold'
    cwl = counter_window.lower()
    if 'counter' in cwl:
        return 'counter_count'
    if cwl.strip().startswith('add '):
        return 'mana_amount'
    return 'count'


def _local_bound(text: str, start: int, end: int) -> str:
    before = text[max(0, start - 15):start].lower()
    after = text[end:end + 20].lower()
    if 'up to' in before:
        return 'UP_TO'
    # Leading "AT LEAST N" (Locthwain Paladin's Adamant: "if at least
    # three black mana was spent") -- was only checked as a TRAILING "N
    # or more/greater" (below), an asymmetric gap for the equally common
    # leading phrasing (Adamant/Ferocious/threshold mechanics).
    if 'at least' in before:
        return 'AT_LEAST'
    if re.match(r'\s*or (less|fewer)\b', after):
        return 'UP_TO'
    if re.match(r'\s*or (greater|more)\b', after):
        return 'AT_LEAST'
    # "more than N" as an action/attacker/blocker-count CAP ("can't be
    # blocked by more than one creature", "can't cast more than one spell
    # each turn", "No card... has more than one of the same mana symbol")
    # is mathematically identical to "up to N" -- exceeding N is exactly
    # what's forbidden, so N is the inclusive ceiling, same as any other
    # UP_TO. Deliberately requires a "can't"/"no" restriction cue earlier
    # in the SAME clause, not just "more than" anywhere -- a comparative
    # CONDITION threshold ("if you've drawn more than one card this turn",
    # "if its controller has more than four cards in hand") is the OPPOSITE
    # direction (closer to an AT_LEAST(N+1) semantic than an UP_TO(N) cap)
    # and has no "can't"/"no" cue, so it's left as EXACT rather than guessed
    # at. Checked all 73 corpus instances of "more than N" before writing
    # this: 70 are the CAP shape (confirmed this bound is exactly correct
    # for all of them, not an approximation), only the remaining 3 are the
    # condition-threshold shape -- none of those 3 have a "can't"/"no" cue,
    # so this check can't collide with them.
    if 'more than' in before and re.search(r"\b(?:can'?t|no)\b", text[:start], re.IGNORECASE):
        return 'UP_TO'
    return 'EXACT'


# ─────────────────────────────────────────────────────────────────── main extractor

def extract_values(text: str, kind_hint: str = None) -> list:
    text = text.strip()
    # A double-quoted substring is a NESTED granted/printed ability's own
    # text (a token's reminder-quoted rules text, an equip cost inside it,
    # ...) -- it describes that embedded object's own effect, not this
    # outer clause's. Blanked (not deleted) to keep any position-based
    # logic below aligned. Found via unseen-corpus spot check (U.S.Agent,
    # John Walker: the equip cost's "{2}" nested inside a quoted granted-
    # ability string was getting extracted as a generic outer `count`).
    text = re.sub(r'"[^"]*"', lambda m: ' ' * len(m.group(0)), text)
    masked = text  # working copy; matched spans get blanked so later, lower-
    # priority rules never re-match the same characters (e.g. the '1' inside
    # an already-consumed '+1/+1 counter' must not also become a bare LITERAL)

    # A mana symbol is a COST, not a game quantity. Brace characters are not
    # word characters, so `\b2\b` happily matches the 2 inside "{2}" and the
    # generic fallback at the bottom turned every generic mana cost into a
    # spurious `count` LITERAL -- 4,708 of 10,428 COST clauses (45%) carried
    # one. Gold is unambiguous that mana-cost clauses carry no values at all
    # ('{2}, {T}, Sacrifice this land' -> [], '{W/U}' -> [], '{T}' -> []);
    # the only COST clauses with values are Crew N and loyalty deltas, both
    # of which are matched by fullmatch rules ABOVE this masking, so they
    # are unaffected.
    masked = re.sub(r'\{[^}]*\}', lambda m: ' ' * len(m.group(0)), masked)

    # "Level N" / "LEVEL N-M" / "LEVEL N+" is a Class-ability or Level-Up
    # creature TIER IDENTIFIER, never a real extractable quantity. Checked
    # every corpus occurrence of "level <digit>" (131 across 60 cards): every
    # single one is either a Class card's own bare "{cost}: Level N"
    # ability-name line (left over after the cost splits off) or its
    # "becomes level N" trigger, or a Level Up creature's "LEVEL N-M"/
    # "LEVEL N+" tier header -- none is a real quantity. Masked so the
    # generic fallback below doesn't turn a level number into a spurious
    # `count` value (Stormchaser's Talent's bare "Level 2" clause getting
    # count=2; Zulaport Enforcer's "LEVEL 1-2" tier header getting BOTH
    # count=1 and count=2).
    masked = re.sub(r'\blevel\s+\d+(?:\s*[-–—]\s*\d+)?\+?',
                     lambda m: ' ' * len(m.group(0)), masked, flags=re.IGNORECASE)

    def mask(span):
        nonlocal masked
        s, e = span
        masked = masked[:s] + ' ' * (e - s) + masked[e:]

    values = []

    # Loyalty cost: the WHOLE clause is a bare signed number ('+1', '0',
    # '−3' -- note U+2212 minus, not ASCII hyphen, Elspeth Storm Slayer).
    if kind_hint == 'COST':
        m = re.fullmatch(r'[+\-−]?\d+', text)
        if m:
            return [_lit('loyalty_delta', int(text.replace('−', '-')))]

    # Crew N
    m = re.fullmatch(r'Crew (\d+)', text)
    if m:
        return [_lit('crew_power', int(m.group(1)))]

    # SCALED: 'twice/three times that much/many [of <noun phrase>]'
    m = re.search(
        r'\b(twice|three times) that (much|many)(?: of \w[\w\s]*?(?=\s+(?:are|is|will be)\b))?\b',
        masked, re.IGNORECASE)
    if m:
        mult = 2 if m.group(1).lower() == 'twice' else 3
        # 'token_count' inferred from THIS match's own text ('of those
        # tokens'), not the whole clause -- a whole-clause 'token' check
        # false-positived on unrelated numbers elsewhere in other clauses
        # that merely mention a token in passing (U.S.Agent, John Walker's
        # nested granted-ability mana cost '{2}' got mislabeled token_count
        # just because the outer clause says 'artifact token').
        role = 'token_count' if 'token' in m.group(0).lower() else _infer_role(text)
        values.append(_scaled(role, mult, m.group(0)))
        mask(m.span())

    # SCALED: 'double the number/amount of <counter-type> counters' --
    # doubles an EXISTING, variable counter count, not a one-time delta
    # (Voracious Hydra: 'Double the number of +1/+1 counters on this
    # creature' was falling through to the generic bare +N/+M delta rule,
    # producing power_delta=1/toughness_delta=1 -- actively wrong, implying
    # a one-time +1/+1 grant rather than doubling whatever count is already
    # there). Must run before the counter-quantifier loop and the +N/+M
    # delta rule below, both of which would otherwise consume the literal
    # '+1/+1' inside this phrase first. 'each kind of counter' (Aetheric
    # Amplifier, Vorel of the Hull Clade) has no single counter-type name to
    # extract, so it's handled as the same role (no established vocabulary
    # distinguishes "some specific counter type" from "every counter type
    # collectively" -- both are just 'counter_count'). Checked all 53
    # corpus instances of 'double the number/amount of' before writing
    # this: every one is this exact counter-doubling idiom, only "double"
    # ever observed (no "triple"/"quadruple" in the corpus, so not
    # speculatively generalized past the multiplier actually seen).
    m = re.search(
        r'\bdouble the (?:number|amount) of\s+(?:each kind of\s+counter|([+\-−]?\d+/[+\-−]?\d+|[a-z]+)\s+counters?)\b',
        masked, re.IGNORECASE)
    if m:
        ctype = (m.group(1) or '').lower()
        role = 'shield_counter_count' if ctype == 'shield' else 'counter_count'
        values.append(_scaled(role, 2, m.group(0)))
        mask(m.span())

    # N <counter-type> counter(s) -- 'a shield counter', 'two +1/+1 counters',
    # 'X +1/+1 counters'. MUST run before the bare +N/+M delta rule below, or
    # the '+1/+1' inside 'a +1/+1 counter' gets consumed there first and this
    # rule can never match (found via Citadel Siege/Elspeth/etc. all
    # producing a spurious power_delta/toughness_delta pair alongside --
    # the +1/+1 magnitude is redundant with the counter TYPE name and must
    # not be extracted as a second, separate value).
    for m in re.finditer(
            rf'\b(a number of|a|an|X|that many|{_NUM})\s+([+\-−]?\d+/[+\-−]?\d+|[a-z]+)\s+counters?\b',
            masked, re.IGNORECASE):
        qty_word = m.group(1)
        ctype = m.group(2).lower()
        role = 'shield_counter_count' if ctype == 'shield' else 'counter_count'
        bound = _local_bound(text, *m.span())
        if qty_word.lower() == 'a number of':
            # 'a NUMBER OF <counter type> counters equal to <expr>'
            # (Reverent Hunter: '...equal to your devotion to green') --
            # the quantity is itself a computed COUNT, not the literal 1
            # that bare 'a counter' means. Previously this phrasing broke
            # the adjacency this regex requires ("a" matched alone,
            # leaving "number of" unconsumed before the P/T pattern), so
            # the whole quantity silently fell through to the generic
            # literal-P/T fallback below and was lost entirely.
            tail = re.search(r'\b(?:equal to|for each)\b.*$', masked[m.end():], re.IGNORECASE)
            expr = tail.group(0).strip() if tail else None
            values.append(_count(role, expr, bound=bound))
            mask((m.start(), m.end() + tail.end()) if tail else m.span())
            continue
        if qty_word.lower() == 'that many':
            # dynamic quantity referring back to a prior clause (Aveline de
            # Grandpré: '...deals combat damage..., put that many +1/+1
            # counters on that creature' -- same shape as 'that much damage',
            # pluralized for count-nouns). binding out of scope, see module
            # docstring.
            values.append(_count(role, 'that many', bound=bound))
            mask(m.span())
            continue
        if qty_word == 'X':
            # 'enters with X counters' -- CDL auto-binds $X from a {X} in the
            # mana cost when no inline where-clause exists. This is a fixed
            # TEMPLATE, not a cross-clause reference, so unlike other binding
            # values it's knowable from local text alone -- the one exception
            # to the module's binding=None rule (see module docstring).
            v = _var(role, 'X', None, bound=bound)
            v['binding'] = 'COST'
            values.append(v)
            mask(m.span())
            continue
        qty = 1 if qty_word.lower() in ('a', 'an') else _num_val(qty_word)
        # per-unit multiplier: "two +1/+1 counters ... for each X" -- the
        # literal qty is a MULTIPLIER on a dynamic repeat count elsewhere in
        # the same clause, not the total (Orchard Elemental).
        if qty != 1 and re.search(r'\bfor each\b', masked[m.end():], re.IGNORECASE):
            fe = re.search(r'\bfor each\b.*$', masked[m.end():], re.IGNORECASE)
            values.append(_count(role, fe.group(0).strip(), bound=bound, multiplier=qty))
            mask((m.start(), m.end() + fe.end()))
        else:
            values.append(_lit(role, qty, bound=bound))
            mask(m.span())

    # COUNT: bare 'that many <noun>' outside the counters-specific idiom
    # above (Rapacious One: 'create THAT MANY 0/1 Eldrazi Spawn creature
    # tokens') -- same anaphoric-quantity reference as the counter-scoped
    # rule, generalized past '+1/+1 counters' to plain object counts.
    # Runs after the counter-specific loop (already masked) so it only
    # catches the case that loop doesn't.
    m = re.search(r'\bthat many\b', masked, re.IGNORECASE)
    if m:
        values.append(_count(_infer_role(text), 'that many'))
        mask(m.span())

    # +N/+M delta (power_delta, toughness_delta) -- both sides explicitly signed
    for m in re.finditer(r'([+\-−]\d+)/([+\-−]\d+)', masked):
        p = int(m.group(1).replace('−', '-'))
        t = int(m.group(2).replace('−', '-'))
        values.append(_lit('power_delta', p))
        values.append(_lit('toughness_delta', t))
        mask(m.span())

    # bare X/X -- created-object base stats defined by a variable, not a
    # literal (Inscription of Insight: 'an X/X blue Illusion creature token,
    # where X is...'). Must run before the bare-N/M and bare-X rules.
    m = re.search(r'\bX/X\b', masked)
    if m:
        where = re.search(r'\bwhere X is\b.*$', text, re.IGNORECASE)
        expr = where.group(0) if where else None
        values.append(_var('power', 'X', expr))
        values.append(_var('toughness', 'X', expr))
        if where:
            mask(where.span())
        mask(m.span())

    # bare N/M (no + sign) -- created-object base stats, e.g. '3/2 ... token'
    for m in re.finditer(r'\b(\d+)/(\d+)\b', masked):
        values.append(_lit('power', int(m.group(1))))
        values.append(_lit('toughness', int(m.group(2))))
        mask(m.span())

    # CHARACTERISTIC: 'equal to its/that X's/their <property>'
    m = re.search(r"equal to (its|that [\w']+'s|their) \w+", masked, re.IGNORECASE)
    if m:
        values.append(_characteristic(_infer_role(text), m.group(0)))
        mask(m.span())

    # 'N or more <noun> would/could ...' -- an EXISTENTIAL condition-check
    # idiom (does at least one exist), not a counted quantity, distinct from
    # 'mana value 3 or greater' which IS a real threshold on an extracted
    # value. Elspeth, Storm Slayer: 'If one or more tokens would be created
    # under your control, ...' -- the 'one' here is not a value.
    m = re.search(rf'\b{_NUM} or more\b(?=.*\bwould\b)', masked, re.IGNORECASE)
    if m:
        mask(m.span())

    # COUNT: 'for each ...', 'equal to the number of ...', 'that much ...'.
    # If a not-yet-consumed literal number sits BEFORE the count phrase in
    # the same clause, it's a per-unit multiplier on the dynamic count
    # (Orchard Elemental: 'gain 3 life for each harvest vote' -- 3 life PER
    # vote, not a separate flat 3), not a second independent value.
    m = re.search(r"(?:equal to the number of|for each)\b.*$|\bthat much \w+\b", masked, re.IGNORECASE)
    if m:
        role = _infer_role(text)
        mult = None
        before = masked[:m.start()]
        nm = None
        for cand in re.finditer(rf'\b({_NUM})\b', before, re.IGNORECASE):
            nm = cand  # keep the LAST (closest-preceding) unconsumed number
        if nm:
            mult = _num_val(nm.group(1))
            mask(nm.span())
        values.append(_count(role, m.group(0).strip(), multiplier=mult))
        mask(m.span())

    # VAR: bare 'X' (X/X already handled above). Two sub-cases -- inline
    # 'where X is ...' binding (self-contained), or the CDL auto-bind-from-
    # cost pattern with no where-clause -- see module docstring on binding.
    m = re.search(r'\bX\b', masked)
    if m:
        where = re.search(r'\bwhere X is\b.*$', text, re.IGNORECASE)
        role = _infer_role(text)
        if where:
            values.append(_var(role, 'X', where.group(0)))
            mask(where.span())
        else:
            values.append(_var(role, 'X', None))
        mask(m.span())

    # 'draw(s)/create(s)/etc a <noun>' -- implicit singular, no digit or
    # number-word in the text at all (Discerning Financier: 'You draw a
    # card' -- count=1). Narrow, single-example rule; extend only when
    # another gold card forces a second verb.
    if not values and re.search(r'\bdraws? a \w+\b', text, re.IGNORECASE):
        return [_lit('count', 1)]

    # generic LITERAL numbers/number-words remaining after everything above
    # has masked its own matches out. Excludes 'one' used as a pronoun
    # ('this one', 'that one', 'the other one' -- Rise of the Eldrazi's
    # 'take an extra turn after this one'), which is not a numeral at all.
    for m in re.finditer(rf'\b({_NUM})\b', masked, re.IGNORECASE):
        if m.group(1).lower() == 'one':
            before = text[max(0, m.start() - 6):m.start()].lower()
            if re.search(r'\b(this|that|other)\s*$', before):
                continue
        bound = _local_bound(text, *m.span())
        # Role keyword lookup is scoped to a LOCAL WINDOW around this
        # specific match, not the whole clause -- a leading count-of-
        # objects number word (Combo Attack: "TWO target creatures ...
        # deal damage equal to their power...") must not pick up 'damage'
        # from an unrelated later clause fragment just because both words
        # appear somewhere in the same text. Same class of bug already
        # fixed once for token_count's role inference (see "Fixed this
        # session" in known-issues); this generalizes it to the bare-
        # number fallback specifically (issue #11) -- the OTHER
        # _infer_role call sites (CHARACTERISTIC, COUNT, VAR, SCALED) are
        # deliberately left on whole-clause scope, since each has at
        # least one gold case that legitimately relies on it (e.g. the
        # CHARACTERISTIC site needs to see a governing verb like "deals
        # DAMAGE equal to its power" that sits BEFORE the matched "equal
        # to..." span) and none has a forcing counter-example yet.
        #
        # Window narrowed 40 -> 20 chars for the damage/life/mana-value/
        # power-threshold checks (Phase 5 grading, records idx304/idx363):
        # a ±40 window was still wide enough to span an entire short
        # compound clause, so a sibling conjunct's keyword leaked across.
        # Ranger of Eos's "up to TWO creature cards with mana value 1 or
        # less" tagged the CARD COUNT 'two' as mv_threshold purely because
        # 'mana value' sits ~21 chars later in the same clause; Gastal
        # Thrillseeker's "deals 1 damage ... and you gain 1 life" tagged
        # BOTH numbers 'damage' because the first conjunct's keyword was
        # still inside the second number's window. ±20 keeps enough
        # context for the adjacent-verb idioms this rule depends on ("N
        # damage", "gain N life", "mana value N") while excluding a
        # differently-governed number/keyword pair one clause segment
        # away. The 'counter'/'add' checks keep the ORIGINAL ±40 window
        # (via _infer_role's `counter_window` param) rather than either
        # extreme: narrowing them to ±20 the same way broke real cases
        # first (Vivisection Evangelist's "three or more poison counters",
        # Voice of the Blessed's "four/ten or more +1/+1 counters" both
        # lost their counter_count role -- the gap to "counters" is longer
        # than to "damage"/"mana value"), but widening them to the whole
        # untouched clause broke DIFFERENT real cases the other direction
        # (Metamorphosis Fanatic's "up to ONE target creature card ... with
        # a lifelink COUNTER on it" tagged the creature count
        # 'counter_count'; Psychic Rebuttal's "two or more instant and/or
        # sorcery cards ... the spell COUNTERED this way" false-matched the
        # substring 'counter' inside 'countered' from clear across the
        # clause). ±40 is the width already proven safe in production
        # (issue #11's original fix), so 'counter'/'add' keep exactly that
        # scope rather than either narrower or unbounded.
        window = text[max(0, m.start() - 20):min(len(text), m.end() + 20)]
        counter_window = text[max(0, m.start() - 40):min(len(text), m.end() + 40)]
        values.append(_lit(_infer_role(window, counter_window), _num_val(m.group(1)), bound=bound))

    return values
