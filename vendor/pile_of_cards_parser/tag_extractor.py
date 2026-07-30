"""
Tag extraction: the last per-clause stage. Takes a clause (text, type,
kind_hint, segment context) already produced by clause_splitter.py and
typed by type_classifier.py, and produces its `tags` dict per
_tag_vocabulary in clause_gold_v1.json.

Two tags are deliberately NEVER resolved to a specific value here, same
scope boundary as numeral_extractor.py's `binding`: `refers_to` (which
clause_id) requires seeing sibling clauses this per-clause function does
not have access to. `refers_to_prior` (the bool) IS extracted locally, via
lexical anaphora cues -- knowing "this clause depends on an earlier one"
doesn't require knowing WHICH one.

card_name and is_spell are optional external context (same category as
numeral_extractor's kind_hint or clause_splitter's is_spell) -- genuinely
available to a real caller, not guessable from clause text alone. Omitting
them degrades SELF detection for the few clauses that use the card's own
name as subject instead of "this X"; everything else is unaffected.
"""
import re

from clause_splitter import split_cost_components

# ─────────────────────────────────────────────────────────────────── lexicons
# Every mapping here traces to a specific gold clause. Grown one card at a
# time, per the outline's bottom-up discipline -- do not add entries
# speculatively.

_EVENT_MAP = [
    # Each of these accepts BOTH the singular-subject verb form ("attacks")
    # and the bare plural-subject form MTG templating uses for "one or more
    # X"/"X and Y"/multiple-named-character subjects ("attack", with no
    # trailing s) -- "Whenever you attack", "Whenever one or more creatures
    # you control attack" were reaching the tag stage with every OTHER tag
    # (target_type, restriction, ...) correctly extracted and only `event`
    # silently missing, because the original patterns only matched the
    # singular form. Same root cause across all six event verbs below, not
    # six separate bugs -- found via the empty-output review stratum on
    # "Whenever you attack" specifically, then confirmed systematic by
    # checking each sibling verb's plural form too before fixing only one.
    (re.compile(r'\battacks?\b', re.I), 'ATTACKS'),
    (re.compile(r'\benters?\b', re.I), 'ETB'),
    (re.compile(r'\bdeals? combat damage to (a player|an opponent|you)\b', re.I), 'COMBAT_DAMAGE_DEALT'),
    # 898 TRIGGER clauses ("Whenever you cast ...") were reaching the tag
    # stage, getting classified TRIGGER, and then emitting no `event` at
    # all -- the single largest eventless-trigger population in the corpus.
    # Found by the empty-output stratum, not by gold (no gold card casts).
    (re.compile(r'\bcasts?\b', re.I), 'CAST'),
    (re.compile(r'\bleaves? the battlefield\b', re.I), 'LEAVES_BATTLEFIELD'),
    (re.compile(r'\bbecomes? blocked\b', re.I), 'BECOMES_BLOCKED'),
    (re.compile(r'\bblocks?\b', re.I), 'BLOCKS'),
    # New this pass: DIES (rule 700.4) and life-total change events, absent
    # from the map entirely rather than singular-only -- "Whenever you gain
    # life"/"...lose life"/"...an opponent loses life"/"When Toluz dies"
    # were all reaching the tag stage with zero `event` output.
    (re.compile(r'\bdies?\b', re.I), 'DIES'),
    (re.compile(r'\bgains? life\b', re.I), 'LIFE_GAINED'),
    (re.compile(r'\bloses? life\b', re.I), 'LIFE_LOST'),
    # Found by the new A6 corpus check (validate_corpus_invariants.py),
    # built specifically because the plural-verb bug above proved the
    # UNCLASSIFIED stratum can't see a clause that's missing ONE field
    # while other tags (target_type, restriction, ...) are already
    # present. A6 surfaced ~2,000 eventless TRIGGER clauses; these are the
    # highest deck-frequency ones, not just the highest raw count.
    #
    # DAMAGE_DEALT/DAMAGE_TAKEN generalize COMBAT_DAMAGE_DEALT (which stays
    # exactly as gold validated it -- combat damage specifically TO a
    # player/opponent/you) to every other damage shape: non-combat damage,
    # and combat damage to a creature/planeswalker/etc. The negative
    # lookahead on DAMAGE_DEALT's "combat damage" branch exists so a clause
    # already claimed by COMBAT_DAMAGE_DEALT doesn't ALSO produce a
    # redundant DAMAGE_DEALT (verified: "deals combat damage to a player"
    # fires only COMBAT_DAMAGE_DEALT; "deals combat damage to a creature"
    # and "deals damage to a player" -- non-combat -- fire only
    # DAMAGE_DEALT; both cases checked against real corpus examples, not
    # just the shape of the regex).
    (re.compile(r'\bdeals? (?:combat damage(?! to (?:a player|an opponent|you)\b)|damage)\b', re.I), 'DAMAGE_DEALT'),
    (re.compile(r'\b(?:is|are) dealt (?:combat )?damage\b', re.I), 'DAMAGE_TAKEN'),
    # PUT_INTO_GRAVEYARD/LEAVES_GRAVEYARD -- the templated phrase MTG uses
    # for non-creature permanents (DIES is creature-only by rule 700.4;
    # an aura/artifact/enchantment card "is put into a graveyard").
    # LEAVES_GRAVEYARD is the opposite direction (a card leaving, e.g. via
    # exile-from-graveyard effects), a genuinely separate event.
    (re.compile(r'\b(?:is|are) put into .{0,20}graveyard\b', re.I), 'PUT_INTO_GRAVEYARD'),
    (re.compile(r"\bleaves? (?:your|an opponent's|their) graveyard\b", re.I), 'LEAVES_GRAVEYARD'),
    (re.compile(r'\bdraws?\b', re.I), 'DRAW'),
    # TAPPED_FOR_MANA (a land tapped specifically as a mana-ability cost,
    # rule 605) is a DIFFERENT triggering condition from TAPPED (any
    # state-change to tapped, any source) -- Verdant Haven cares about the
    # former specifically, not just "becomes tapped" in general.
    (re.compile(r'\b(?:is|are) tapped for mana\b', re.I), 'TAPPED_FOR_MANA'),
    (re.compile(r'\bbecomes? tapped\b', re.I), 'TAPPED'),
    (re.compile(r'\bbecomes? untapped\b', re.I), 'UNTAPPED'),
    (re.compile(r'\bdiscards?\b', re.I), 'DISCARD'),
    (re.compile(r'\bsacrifices?\b', re.I), 'SACRIFICE'),
    (re.compile(r'\bbecomes? the target of\b', re.I), 'BECOMES_TARGET'),
    (re.compile(r'\bcycles?\b', re.I), 'CYCLE'),
    (re.compile(r'\bscry\b', re.I), 'SCRY'),
    (re.compile(r'\bsurveils?\b', re.I), 'SURVEIL'),
    (re.compile(r'\bexploits?\b', re.I), 'EXPLOIT'),
]

_PHASE_MAP = [
    (re.compile(r'\bcombat\b', re.I), 'COMBAT'),
    (re.compile(r'\bupkeep\b', re.I), 'UPKEEP'),
    (re.compile(r'\bend step\b', re.I), 'END_STEP'),
    (re.compile(r'\bdraw step\b', re.I), 'DRAW_STEP'),
    (re.compile(r'\bmain phase\b', re.I), 'MAIN_PHASE'),
]

_ZONE_MAP = [
    (re.compile(r'\blibrar(?:y|ies)\b', re.I), 'LIBRARY'),
    (re.compile(r'\bgraveyards?\b', re.I), 'GRAVEYARD'),
    (re.compile(r'\bhands?\b', re.I), 'HAND'),
    (re.compile(r'\bbattlefields?\b', re.I), 'BATTLEFIELD'),
    (re.compile(r'\bcommand zones?\b', re.I), 'COMMAND'),
    # 'exile' as a NOUN (the zone) is only unambiguous in these two
    # prepositional shapes ('from exile'/'in exile', checked directly
    # against the corpus) -- a bare \bexile\b would catastrophically
    # false-positive on the vastly more common ACTION verb ('Exile
    # target creature'), so this stays narrow unlike every other entry
    # here, which matches the zone noun on its own.
    (re.compile(r'\b(?:from|in) exile\b', re.I), 'EXILE'),
]

# A destination preposition ('to the X'/'onto X'/'into X') names the
# clause's real target zone; some OTHER zone word present elsewhere is
# usually just an incidental descriptor, not where the object is going
# (Zero Point Ballad: "...put into a GRAVEYARD this way to the
# BATTLEFIELD..." is a reanimation effect whose destination is the
# battlefield, "graveyard" only identifies WHICH card). Checked before
# the fixed-order fallback scan below so it always wins when both are
# present. 'exile' is included here (unlike _ZONE_MAP's narrow
# from/in-only pattern) because 'to/into/onto exile' is unambiguously
# the zone, never the verb. The negative lookbehind excludes 'equal TO'
# specifically -- that "to" is part of a VALUE-computation phrase, not a
# movement-destination preposition (Doubtless One: "power and toughness
# are each EQUAL TO the number of Clerics on the battlefield" was being
# read as a destination "to...battlefield", when nothing is being moved
# to the battlefield at all).
_ZONE_DESTINATION = re.compile(
    r"\b(?<!equal )(?:to|onto|into)\b[^.]{0,30}?\b(librar(?:y|ies)|graveyards?|hands?|battlefields?|command zones?|exile)\b",
    re.IGNORECASE)

# noun -> target_type. Longer/more specific phrases first so 'creature
# token' doesn't get caught by a bare 'token' before 'creature' can match,
# etc. '(?:s)?' plurals covered by \b on the stem.
_TARGET_TYPE_MAP = [
    (re.compile(r'\bplaneswalkers?\b', re.I), 'PLANESWALKER'),
    (re.compile(r'\bcreatures?\b', re.I), 'CREATURE'),
    (re.compile(r'\bartifacts?\b', re.I), 'ARTIFACT'),
    (re.compile(r'\benchantments?\b', re.I), 'ENCHANTMENT'),
    (re.compile(r'\blands?\b', re.I), 'LAND'),
    (re.compile(r'\bpermanents?\b', re.I), 'PERMANENT'),
    (re.compile(r'\bplayers?\b|\bopponents?\b', re.I), 'PLAYER'),
    (re.compile(r'\btreasure\b', re.I), 'ARTIFACT'),
    (re.compile(r'\btokens?\b', re.I), 'TOKEN'),
    # NOTE: deliberately no bare '\bcards?\b' entry here -- a bare "cards"
    # mention with no type-adjective (Jeska's Will's "the top three cards
    # of your library", "then draw two cards") gets no target_type at all
    # in gold; only "X card(s)" WITH a type-adjective (handled separately,
    # see the priority check in _target_type) maps to CARD.
]

# Basic land names, for the "<name> card" -> ['CARD', 'LAND'] compound in
# _target_type -- not in _TARGET_TYPE_MAP since these are proper names, not
# the generic supertype noun 'land' that map already matches.
_BASIC_LAND_TYPE = {
    'plains': 'LAND', 'island': 'LAND', 'swamp': 'LAND',
    'mountain': 'LAND', 'forest': 'LAND',
}

def _collect_types(text):
    """All _TARGET_TYPE_MAP matches in `text`, for COMPOUND-type collection
    (a single object's multiple simultaneous types, e.g. "artifact
    creature token"). PLAYER is dropped WHEN compounding with something
    else, but kept if it's the only match -- a "player" mention alongside
    another type is always a SEPARATE entity (the actor of "each player
    sacrifices...", or the controller in "target player controls...",
    captured by chooser/restriction), never a second type of the object
    itself; but "each opponent loses life" has no other type word at all,
    and PLAYER is exactly right there (Season of Loss). Found via
    false-positive compounds on Ruinous Wrecking Crew, Season of Loss,
    Requisition Raid, Dawnglare Invoker before this split was added."""
    matches = []
    for pat, val in _TARGET_TYPE_MAP:
        if pat.search(text) and val not in matches:
            matches.append(val)
    if len(matches) > 1 and 'PLAYER' in matches:
        matches = [v for v in matches if v != 'PLAYER']
    return matches

# 'exile' deliberately excluded despite reading like a selection verb --
# no gold example actually pairs it with target_scope=NONE (Jeska's Will's
# "Exile the top three cards of your library" is a deterministic ordinal
# slice, not a player choice, and gets no target_scope at all).
_NONE_SELECTION_VERB = re.compile(
    r'\b(sacrifice[s]?|discard[s]?|choose[s]?)\b', re.IGNORECASE)

# sacrifice/discard default to restriction=YOU by MTG RULE, not text -- "a
# creature"/"a card" with no owner phrase is still always YOUR OWN
# permanent/hand by the rules meaning of the verb itself (you cannot
# sacrifice/discard something you don't own/control). 'choose' has no such
# default (Discerning Financier's "Choose another player" is NOT_YOU).
_IMPLICIT_YOU_VERB = re.compile(r'\b(sacrifice[s]?|discard[s]?)\b', re.IGNORECASE)

# Relative-clause "that VERB..." ("cards THAT SHARE a land type") is not
# anaphora -- only "that NOUN" refers back to an established object. This
# is a stoplist of the relative-clause verb-starts observed so far, not a
# full parser; extend when a new one shows up.
_RELATIVE_CLAUSE_VERB = {
    'share', 'shares', 'would', 'is', 'are', 'has', 'have', 'deals',
    'controls', 'was', 'were', 'remains', 'gets', 'becomes', 'dies', 'died',
    'attacks', 'blocks', 'enters',
}


def _norm(s):
    return ' '.join(s.strip().rstrip('.').split()).lower()


def _load_keyword_names():
    """Multi-word keyword names, from parser.py's keyword_store.json -- the
    repo's existing keyword authority, rather than a second hand-kept list
    that would drift against it."""
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keyword_store.json')
    try:
        with open(path, encoding='utf-8') as f:
            return {k.lower() for k in json.load(f)}
    except (OSError, ValueError):
        return set()


_KEYWORD_NAMES = _load_keyword_names()

# A parameterized keyword line carries its own cost/number after the name
# ("Equip {3}", "Cycling {3}", "Ward {2}"). The name itself is the keyword;
# the parameter belongs to the value/cost layer.
_PARAM_KEYWORD_LINE = re.compile(r"^([A-Za-z][\w'-]*)\s+(?:\{|\d)")

# "Equip <Subtype> {cost}" -- a real, printed MTG templating variant (cost
# reduced/restricted when attaching to a creature matching the named
# condition: a subtype like "Human"/"Elf", or a qualifier like "commander"/
# "planeswalker"/Mjolnir's own "worthy") that _PARAM_KEYWORD_LINE never
# matches, because it requires the cost token immediately after the keyword
# name with no bridging word. Deliberately narrow -- anchored to the literal
# word "equip", not a blanket allowance for any bridging word before
# PARAM_KEYWORD_LINE -- because broadening that shared regex would also
# swallow unrelated ACTION text shaped "Word CapitalizedWord {N}" (e.g.
# "create X 1/1 black Rat creature tokens", where "X" is exactly such a
# bridging word). Found via Phase 5 random-sample grading (Steelclaw Lance).
_EQUIP_SUBTYPE_LINE = re.compile(r"^equip\s+[A-Za-z][\w'-]*\s+(?:\{|\d)", re.IGNORECASE)


# Keyword-ABILITY names missing from keyword_store.json (which is CDL's own
# authority, shared with the CDL initiative -- kept as a small local
# supplement here rather than added there, since these have no CDL expansion
# template and adding them to that file would silently claim CDL support
# that doesn't exist). Single-word entries here also feed _PARAM_KEYWORD_LINE
# for their parameterized form (Entwine {2}, Foretell {2}{W}) -- ACTION type
# only accepts a "Word N" shape when the word is a KNOWN name, so these need
# to be here for that path to fire at all (see _single_bare_keyword).
_LOCAL_KEYWORD_NAMES = {
    'living weapon', "doctor's companion", 'rebound', 'retrace', 'populate',
    'split second', 'cipher', 'bargain', 'job select', 'umbra armor',
    'hidden agenda', 'battle cry', 'for mirrodin!', 'jump-start', 'entwine',
    'foretell',
    # 'level' -- the SAME tier-marker word appears in two shapes: a
    # Class-enchantment ability line ("{3}{U}: Level 2") splits into a COST
    # clause plus a leftover ACTION clause whose entire text is "Level 2"
    # (a level-tier IDENTIFIER, never a real effect -- see
    # numeral_extractor's matching "Level N" value-suppression fix); a
    # Level Up creature's "LEVEL 1-2"/"LEVEL 3+" tier header is STATIC and
    # already reached 'level' via the STATIC-only shape fallback above.
    # Adding it here makes the ACTION-typed Class-card shape resolve the
    # SAME way instead of surfacing as an empty-tag, confidence=UNCLASSIFIED
    # clause with nothing to say about itself. Checked every one of the
    # corpus's 131 "level <digit>" occurrences before adding this (see
    # clause_pipeline_known_issues.md's Group I) -- all 131 are one of
    # these two mechanics, so this can't collide with an unrelated ACTION
    # clause that happens to start with the word "level".
    'level',
}

# A prefix whose entire remainder is inherently part of THAT keyword's own
# parameter/description, never separate target/zone content in the same
# clause -- unlike _KEYWORD_ACTION_CUES, no trailer whitelist needed, the
# rest of the line is swallowed unconditionally once the prefix matches.
_KEYWORD_PREFIX_SWALLOW = [
    (re.compile(r'^protection from\b', re.I), 'protection'),
    (re.compile(r'^splice onto\b', re.I), 'splice'),
    (re.compile(r'^level up\b', re.I), 'level up'),
    # 'Champion an Elemental'/'Champion a Vampire'/... -- the creature
    # type is an open-ended parameter (a rule-702.71 keyword ability),
    # same shape as protection's color/type parameter (Nova Chaser, found
    # via Phase 5 hand-grading -- correctly self-flagged UNCLASSIFIED by
    # the pipeline's own confidence field until this entry existed).
    (re.compile(r'^champion\b', re.I), 'champion'),
    # 'Hexproof from each color'/'Hexproof from black' -- same shape as
    # 'protection from <color>' immediately above, just never given the
    # same treatment (Breaker of Creation, found via Phase 5 hand-grading
    # -- was previously hitting the bare-'each' ALL-rule false positive
    # on 'each color' instead of being recognized as a keyword at all).
    (re.compile(r'^hexproof from\b', re.I), 'hexproof'),
]

# Keyword ACTIONS (rule 701), leading-anchored rather than whole-line, since
# they carry trailing count/repeat modifiers a STATIC keyword-ABILITY line
# never does ("investigate twice", "then shuffle", "mill three cards").
# Matched only when the entire remainder after the cue is itself just such
# a modifier (_is_bare_trailer) -- "shuffle your library" or "mill cards
# equal to its power" must fall through to normal target/zone extraction,
# not be swallowed as a bare keyword. Found via the UNCLASSIFIED review
# stratum: shuffle/mill/investigate/proliferate/roll-a-die/flip-a-coin/
# storm/learn/manifest dread/venture-into-the-dungeon/the-monarch/the-
# initiative/the-Ring-tempts-you were reaching the tag stage typed ACTION
# (or STATIC, off a SAGA_CHAPTER tier-bar body like "10+ | Flying") and
# emitting nothing, despite being MTG's own named keyword actions.
_KEYWORD_ACTION_CUES = [
    (re.compile(r'^shuffles?\b', re.I), 'shuffle'),
    (re.compile(r'^mills?\b', re.I), 'mill'),
    (re.compile(r'^investigates?\b', re.I), 'investigate'),
    (re.compile(r'^proliferates?\b', re.I), 'proliferate'),
    (re.compile(r'^storm\b', re.I), 'storm'),
    (re.compile(r'^learns?\b', re.I), 'learn'),
    (re.compile(r'^flips? a coin\b', re.I), 'flip a coin'),
    (re.compile(r'^rolls? (?:a |two |three )?d\d+\b', re.I), 'roll a die'),
    (re.compile(r'^ventures? into the dungeon\b', re.I), 'venture into the dungeon'),
    (re.compile(r'^manifest dread\b', re.I), 'manifest dread'),
    (re.compile(r'^(?:you )?becomes? the monarch\b', re.I), 'the monarch'),
    (re.compile(r'^(?:you )?takes? the initiative\b', re.I), 'the initiative'),
    (re.compile(r'^the ring tempts you\b', re.I), 'the ring tempts you'),
    (re.compile(r'^cumulative upkeep\b', re.I), 'cumulative upkeep'),
]

_LEADING_CONNECTOR = re.compile(r'^(?:then|and)\s+', re.IGNORECASE)

# What's allowed to follow a keyword-action cue and still count as "bare" --
# a count/repeat modifier, not a real target/zone/condition. Anything else
# in the remainder means the clause carries real semantic content beyond
# the keyword itself and must fall through to normal extraction instead.
_COUNT_TRAILER = re.compile(
    r'(?:a|an|\d+|x|one|two|three|four|five|six|seven|eight|nine|ten)\s+cards?'
    r'|x?\s*times?'
    r'|twice|thrice'
    r'|that many times(?: or until you lose a flip,?\s*whichever comes first)?'
    r'|an additional time'
    r'|a number of times equal to .+'
    r'|until you lose a flip(?: or choose to stop flipping)?'
    r'|at end of combat'
    r'|instead|again',
    re.IGNORECASE)


def _is_bare_trailer(remainder):
    r = remainder.strip()
    if not r:
        return True
    return bool(_COUNT_TRAILER.fullmatch(r))


def _single_bare_keyword(t, clause_type):
    """One comma-separated segment of a bare keyword line, or a single
    keyword-action cue with only a count/repeat modifier trailing it.

    The shape-only fallbacks (any single alphabetic word; any "Word N"/
    "Word {N}" line) are STATIC-only -- a STATIC segment with a single-word
    or bare-parameterized clause is basically always a genuine keyword by
    construction. ACTION is far more free-form ("Scry 2" is a real corpus
    clause gold deliberately leaves untagged), so on ACTION a "Word N" line
    only counts if the word itself is a KNOWN keyword name -- this is what
    lets "Cycling {2}" resolve while "Scry 2" correctly does not."""
    if not t:
        return None
    if clause_type == 'STATIC' and re.fullmatch(r"[A-Za-z][\w'-]*", t):
        return t.lower()
    tl = t.lower()
    if tl in _KEYWORD_NAMES or tl in _LOCAL_KEYWORD_NAMES:
        return tl
    if _EQUIP_SUBTYPE_LINE.match(t):
        return 'equip'
    m = _PARAM_KEYWORD_LINE.match(t)
    if m:
        name = m.group(1).lower()
        if clause_type == 'STATIC' or name in _KEYWORD_NAMES or name in _LOCAL_KEYWORD_NAMES:
            return name
    for pat, name in _KEYWORD_PREFIX_SWALLOW:
        if pat.match(t):
            return name
    for pat, name in _KEYWORD_ACTION_CUES:
        cm = pat.match(t)
        if cm and _is_bare_trailer(t[cm.end():]):
            return name
    return None


# ACTIVATED-ability timing restriction ("Activate only as a sorcery"),
# rule 602.5a. Reuses CDL's own DURING vocabulary (parser.py's
# `_infer_during`) rather than inventing a separate one -- confirmed with
# the user. Order matters: the more specific "...turn, before attackers
# are declared" combos must be tried before the bare "...turn" forms they'd
# otherwise also match as a prefix.
_DURING_CUES = [
    (re.compile(r"^activate only during your turn,\s*before attackers are declared", re.I), 'BEFORE_ATTACKERS'),
    (re.compile(r"^activate only during an opponent'?s turn,\s*before attackers are declared", re.I), 'OPPONENTS_TURN'),
    (re.compile(r"^activate only before attackers are declared", re.I), 'BEFORE_ATTACKERS'),
    (re.compile(r"^activate only as a sorcery", re.I), 'SORCERY_SPEED'),
    (re.compile(r"^activate only during your turn", re.I), 'YOUR_TURN'),
    (re.compile(r"^activate only during an opponent'?s turn", re.I), 'OPPONENTS_TURN'),
]

# Only a stray trailing quote/period is tolerated after a DURING cue -- a
# real corpus artifact from a granted-ability's closing quote leaking onto
# the last inner sentence (Prosperity Tycoon: 'Activate only as a
# sorcery."'). Anything else means the clause carries real content beyond
# the timing restriction ("...and only if you have one or fewer cards in
# hand", which already correctly gets zone=HAND elsewhere) and must fall
# through untouched.
_DURING_TRAILER = re.compile(r'^[."\']*$')


def _detect_during(text):
    t = text.strip()
    for pat, val in _DURING_CUES:
        m = pat.match(t)
        if m and _DURING_TRAILER.match(t[m.end():]):
            return val
    return None


def _bare_keyword(text, clause_type='STATIC'):
    """The keyword name(s) on a bare-keyword line, or None.

    Previously this was a single-word fullmatch, which silently dropped
    EVERY multi-word keyword -- "First strike" alone is 223 cards, and
    "Double strike"/"Cumulative upkeep" the same shape -- and every
    parameterized keyword line ("Equip {3}", 510 cards; "Cycling {3}", 127).
    All of them reached the tag stage, got typed STATIC, and emitted no tags
    at all. Found by the UNCLASSIFIED review stratum, which is exactly the
    population it was built to surface: a clause the pipeline typed
    confidently and then had nothing to say about.

    Multi-word names are checked against keyword_store.json rather than
    accepted on shape, because "Target creature" is also two alphabetic
    words and must NOT become a keyword.

    A comma-separated line ("Flying, first strike") returns a LIST -- some
    tier-bar/level-up bodies pack multiple bare keywords onto one line
    rather than one-per-clause (Flailing Manticore); if ANY segment fails
    to resolve, the whole line is rejected rather than partially tagged, to
    avoid guessing on an unfamiliar shape.

    A leading 'then '/'and ' sequencing connector is stripped first --
    harmless on a genuine STATIC keyword line (never has one), necessary
    for keyword-ACTION lines reached via the same function ("then
    shuffle")."""
    t = _LEADING_CONNECTOR.sub('', text.strip().rstrip('.'))
    if not t:
        return None
    parts = [p.strip() for p in re.split(r',\s*', t)] if ',' in t else [t]
    names = []
    for p in parts:
        n = _single_bare_keyword(p, clause_type)
        if n is None:
            return None
        names.append(n)
    return names[0] if len(names) == 1 else names


def extract_tags(text: str, clause_type: str, kind_hint: str = None,
                  segment_kind: str = None, card_name: str = None) -> dict:
    # A double-quoted substring is a NESTED granted/printed ability's own
    # text (e.g. a created token's reminder-quoted rules text) -- it
    # describes that embedded object's own effect, not this outer
    # clause's target/zone/restriction/duration. Blanked, same fix and
    # same corpus example (U.S.Agent, John Walker) as numeral_extractor's
    # equivalent guard -- "Equipped creature gets +1/+2" inside the quotes
    # was leaking target_type=CREATURE onto the outer token-creation
    # clause.
    text = re.sub(r'"[^"]*"', lambda m: ' ' * len(m.group(0)), text)
    tags = {}
    tl = text.lower()

    # ---- CHOICE header carries no tags -- its container fields (mode_count,
    # chooser, delimiter, ...) live on ChoiceContainer, not here. ----
    if clause_type == 'CHOICE':
        return tags

    # ---- COST ----
    if clause_type == 'COST':
        if segment_kind == 'KEYWORD':
            m = re.match(r"[A-Za-z']+", text)
            tags['keyword'] = m.group(0).lower()
            # A non-mana alternative cost (Flashback/Buyback/Awaken/Bestow)
            # that is itself a synergy-relevant action (sacrifice/discard/
            # life payment) gets full effect-shaped tags too, alongside
            # cost_components -- per _decisions.synergy_vs_rules_accuracy_
            # tiebreak (Cabal Therapy's "Flashback—Sacrifice a creature" is
            # a repeatable sac outlet, not merely a payment; a plain mana
            # cost like "Kicker {2}{U}{U}" has no such verb and stays
            # keyword-only, unaffected). Reuses extract_tags's own ACTION
            # branch rather than duplicating target_scope/type/restriction
            # logic -- confirmed to match Victimize's standalone "Sacrifice
            # a creature" clause exactly.
            rest = text[m.end():].lstrip('—- :').strip()
            if rest and re.match(r'^(sacrifice|discard|pay)\b', rest, re.IGNORECASE):
                tags['cost_components'] = [rest]
                effect_tags = extract_tags(rest, 'ACTION', segment_kind=segment_kind, card_name=card_name)
                for key in ('target_scope', 'target_type', 'restriction'):
                    if key in effect_tags:
                        tags[key] = effect_tags[key]
        elif segment_kind != 'LOYALTY_ABILITY':
            tags['cost_components'] = split_cost_components(text)
        return tags  # COST clauses carry no other tags in gold

    # ---- STATIC bare keyword / ACTION bare keyword-action ----
    # ACTION is included because a tier-bar/level-up body ("10+ | Flying")
    # gets typed ACTION by default (SAGA_CHAPTER segment_kind, not STATIC),
    # and MTG's own keyword ACTIONS (investigate, mill, shuffle, proliferate,
    # roll a die, ...) are themselves ACTION-typed clauses, never STATIC.
    if clause_type in ('STATIC', 'ACTION'):
        kw = _bare_keyword(text, clause_type)
        if kw:
            tags['keyword'] = kw
            return tags

    # ---- ACTION: ACTIVATED-ability timing restriction ("Activate only
    # as a sorcery") ----
    if clause_type == 'ACTION':
        during = _detect_during(text)
        if during:
            tags['during'] = during
            return tags

    # ---- TRIGGER ----
    if clause_type == 'TRIGGER':
        # "When you do"/"When they do" -- the TRIGGER-side counterpart of
        # CONDITION's existing "if you do" idiom: an anaphoric reference
        # back to the immediately preceding "you may ..." action, not a
        # game event. Same shape, same handling (refers_to_prior only, no
        # event/target of its own) -- structural_linker resolves WHICH
        # prior clause at the cross-clause stage, same as everywhere else
        # refers_to_prior is set locally.
        if _norm(text) in ('when you do', 'when they do'):
            tags['refers_to_prior'] = True
            return tags

        is_phase_trigger = 'at the beginning of' in tl
        if is_phase_trigger:
            for pat, val in _PHASE_MAP:
                if pat.search(text):
                    tags['phase'] = val
                    break
            if re.search(r'\byour\b', tl):
                tags['whose_turn'] = 'YOURS'
            elif re.search(r"each opponent'?s?\b", tl):
                tags['whose_turn'] = 'EACH_OPPONENT'
        else:
            # Sort by match POSITION in text, not _EVENT_MAP's check order --
            # BECOMES_BLOCKED must be checked before the bare BLOCKS pattern
            # to match correctly, but that must not put it first in the
            # output list when it appears second in the text (Engulfing
            # Slagwurm: "blocks or becomes blocked" -> [BLOCKS,
            # BECOMES_BLOCKED], matching reading order).
            matches = [(m.start(), val) for pat, val in _EVENT_MAP for m in [pat.search(text)] if m]
            events = [val for _, val in sorted(matches)]
            if len(events) == 1:
                tags['event'] = events[0]
            elif len(events) > 1:
                tags['event'] = events
            # target_scope only applies to EVENT triggers (who/what performs
            # the triggering action) -- a phase trigger's "each opponent's
            # turn" is about WHEN, already captured by whose_turn, and must
            # not also produce target_scope=EACH_OPPONENT (Citadel Siege).
            scope = _target_scope(text, card_name, is_trigger=True)
            if scope:
                tags['target_scope'] = scope
            if scope != 'SELF':
                ttype = _target_type(text)
                if ttype:
                    tags['target_type'] = ttype
        # restriction is skipped for phase triggers -- "on YOUR turn"/"each
        # OPPONENT's turn" is already fully captured by whose_turn above,
        # a redundant restriction=YOU/OPPONENT there is not wanted in gold
        # (Citadel Siege, Discerning Financier).
        if not is_phase_trigger:
            rest = _restriction(text)
            if rest:
                tags['restriction'] = rest
        excl = _excludes(text)
        if excl:
            tags['excludes'] = excl
        return tags

    # ---- CONDITION ----
    if clause_type == 'CONDITION':
        if _norm(text) == 'if you do':
            tags['condition_kind'] = 'PRIOR_ACTION_SUCCEEDED'
            tags['refers_to_prior'] = True  # "if you DO" always references
            # back to the immediately preceding action -- that's what makes
            # it PRIOR_ACTION_SUCCEEDED rather than GAME_STATE.
        else:
            tags['condition_kind'] = 'GAME_STATE'
            _add_anaphora(tags, text, card_name)
        return tags

    # ---- REPLACEMENT / STATIC (structural, non-keyword) / ACTION share the
    # rest of the vocabulary (target/restriction/zone/duration/modality/...)
    rk, ra = _restriction_kind(text)
    if rk:
        tags['restriction_kind'] = rk
        tags['restricted_action'] = ra

    if re.search(r'\byou may\b', tl):
        tags['modality'] = 'MAY'
    elif re.search(r'\bmust be blocked\b', tl):
        # "X must be blocked (this turn) (by a Dalek) if able" -- a real,
        # if narrow (38 corpus instances, all this exact 2-word verb
        # shape), MUST-modality effect (Jurin, The Foretold Soldier).
        # modality: MUST is declared vocabulary (clause_vocabulary.py)
        # but had ZERO emissions anywhere in the corpus before this --
        # completely unreachable, same class as the EXILE zone gap fixed
        # earlier this session. Scoped to the single confirmed verb
        # ("blocked") rather than a general "must be <verb>ed" pattern,
        # which would also catch "the new target must be A PLAYER"
        # (Reflecting Mirror) -- a targeting-restriction description, not
        # a MUST-attack/block effect at all.
        tags['modality'] = 'MUST'

    # Case-insensitive: a mana-adding effect isn't always sentence-initial
    # ("if you attacked this turn, add {R}{W}{B}" -- Mardu Warshrieker --
    # arrives here lowercase after the comma-split from its CONDITION).
    # segment_kind == 'ACTIVATED' is still the only True case -- a
    # TRIGGERED or SPELL "add" is never a mana ability by rule 605.1a
    # (excludes ETB/modal-spell mana effects), already correctly False for
    # Jeska's Will's SPELL-segment "Add {R} for each card..."; this only
    # fixes the tag going MISSING entirely on a lowercase match, not the
    # boolean value.
    if clause_type == 'ACTION' and re.match(r'^add \{', text, re.IGNORECASE):
        tags['is_mana_ability'] = (segment_kind == 'ACTIVATED')

    scope = _target_scope(text, card_name, is_trigger=False)
    if scope:
        tags['target_scope'] = scope

    # target_type is never extracted for SELF -- SELF's type is the source
    # card's own type, implicit, never restated in gold (Parapet Watchers'
    # "This creature gets +0/+1" has no target_type despite "creature"
    # appearing in the subject).
    if scope != 'SELF':
        ttype = _target_type(text)
        if ttype:
            tags['target_type'] = ttype

    zone = _zone(text)
    if zone:
        tags['zone'] = zone

    rest = _restriction(text)
    if rest:
        tags['restriction'] = rest

    ch = _chooser(text)
    if ch:
        tags['chooser'] = ch

    dur = _duration(text)
    if dur:
        tags['duration'] = dur

    excl = _excludes(text)
    if excl:
        tags['excludes'] = excl
    elif _needs_excludes_resolution(text):
        tags['excludes_prior'] = True

    if re.search(r'^for each\b|,\s*for each\b', tl):
        tags['distributive'] = True

    _add_anaphora(tags, text, card_name)

    return tags


# ─────────────────────────────────────────────────────────────────── per-tag helpers

_ANAPHORIC_PHRASE = re.compile(
    r'\b(the chosen \w+|those \w+|that \w+|them|it\'?s?)\b', re.IGNORECASE)

# bare plural noun + 'you control'/'target player controls', no article --
# a mass/blanket effect idiom with neither 'all' nor 'each' spelled out
# (Boros Charm: 'Permanents you control gain...', Glorious Anthem:
# 'Creatures you control get...').
_BARE_PLURAL_MASS = re.compile(
    r'^[A-Z][a-z]+s (you control|target player controls)\b')

# Bare plural noun with NO qualifier of any kind before the verb --
# "Players have no maximum hand size" (The Second Doctor), "Creatures
# can't attack you" (Blazing Archon) -- an even more implicit form of the
# same mass/blanket idiom above, this time with no "you control"/"each"
# either. English grammar alone makes this universal (an article-less
# plural subject means "every one of them"), but scoped to a verb
# whitelist matching only the shapes actually checked in the corpus (79
# clauses) rather than a bare "next word is anything" match -- a
# qualifying phrase immediately after the noun ("Creatures WITH power
# less than...", "Creatures OF the chosen color...", "Creatures YOUR
# opponents control...") changes the semantics and needs its own
# handling, not blanket ALL. Deliberately checked ALONGSIDE
# _NONE_SELECTION_VERB at the call site (not folded into this regex): a
# bare-plural clause about SACRIFICING/DISCARDING/CHOOSING ("Players
# can't pay life or sacrifice creatures...", Angel of Jubilation/
# Yasharn) already correctly resolves to target_scope=NONE further down
# this function (a player-chosen single-object selection, not a mass
# effect over every creature) -- this rule must not preempt that by
# firing first just because "Players can't" also matches here.
_BARE_PLURAL_UNQUALIFIED = re.compile(
    r"^(?:Players|Creatures|Permanents|Artifacts|Enchantments|Lands|Spells|Opponents)\s+"
    r"(?:have|has|can'?t|cannot|don'?t|doesn'?t|are|is|play|gain|lose)\b")


def _strip_anaphora(text):
    """For noun-type lookups only -- an anaphoric mention ('the chosen
    cards', 'that ability') re-describes an object a PRIOR clause already
    introduced, it is not a fresh type declaration (Victimize: 'return the
    chosen cards to the battlefield' has no target_type, unlike a fresh
    'target ... cards' mention)."""
    return _ANAPHORIC_PHRASE.sub(' ', text)


def _target_scope(text, card_name, is_trigger=False):
    tl = text.lower()
    # "target player/opponent controls" is a RESTRICTION embedded in a
    # mass-scoped clause ("each creature TARGET PLAYER CONTROLS"), not a
    # fresh TARGET declaration of its own (Requisition Raid, Dawnglare
    # Invoker) -- excluded here so the real scope (ALL, from "each") gets
    # a chance to fire below instead of this false TARGET.
    # "becomes THE TARGET OF" is the BECOMES_TARGET event idiom describing
    # what happens to THIS clause's own subject (already captured by
    # event: BECOMES_TARGET) -- the word "target" there isn't a fresh
    # TARGET declaration, unlike every other bare "target" occurrence
    # (Eternal Scourge: "this creature becomes the target of a spell or
    # ability an opponent controls" was resolving to TARGET/PLAYER from
    # this word alone, on top of the correct event tag).
    if re.search(r'\btarget\b', tl) and not re.search(r'\btarget (player|opponent) controls\b', tl) \
            and not re.search(r'\bbecomes? the target of\b', tl):
        return 'TARGET'
    if re.search(r'\beach player\b', tl):
        return 'EACH_PLAYER'
    if re.search(r'\beach opponent\b', tl):
        return 'EACH_OPPONENT'
    # "this X" (SELF) is checked BEFORE the mass-effect ALL check: "Put two
    # +1/+1 counters on THIS CREATURE for each sprout vote" contains both
    # "this creature" (SELF, the real subject) and "for each" (Orchard
    # Elemental) -- ALL must not win just because "each" appears somewhere.
    # Excludes "this X deals ... to Y" (Day of the Doctor: "this Saga deals
    # 13 damage to you") -- there "this X" is the AGENT causing an effect on
    # a SEPARATE recipient, not the affected object itself (same agent-vs-
    # patient distinction as the card_name check below). Also excludes
    # "this X" governed by a TEMPORAL CONNECTIVE ("until this creature
    # leaves the battlefield", "for as long as this Saga remains on the
    # battlefield") -- there "this X" is the duration's reference point,
    # not the affected object. Originally this exclusion was written for
    # the single phrase "remains on the battlefield"; a corpus check (A5)
    # found 40 more cards with the same shape and a different verb --
    # Turncoat Kunoichi's "Exile THAT CREATURE until THIS CREATURE leaves
    # the battlefield" was reporting SELF when the affected object is
    # plainly "that creature". Generalizing to the connective itself
    # covers the family rather than one member of it.
    m = re.search(r'\bthis ([a-z]+)\b', tl)
    if m and m.group(1) not in ('turn', 'way', 'phase', 'ability', 'combat', 'game') and not re.search(r'\bthis \w+ deals\b.*\bto\b', tl) \
            and not re.search(r'\b(?:until|for as long as|as long as)\b[^.]{0,24}?\bthis \w+\b', tl) \
            and not re.search(r'\bthis \w+ phase\b', tl) \
            and not re.search(r'\bcast this \w+\b', tl) \
            and not re.search(r"\bthis spell(?:'s| was)\b", tl) \
            and not re.search(r'\bwith this \w+\b', tl) \
            and not (re.search(r'\b(?:for each|equal to|number of)\b[^.]{0,40}?\bthis \w+\b', tl)
                     and re.search(r'\b(?:creatures|permanents) you control\b', tl)):
        # 'this ability' isn't a game object (Caretaker's Talent: "This
        # ability triggers only once each turn" is a repeat-limit
        # restriction, not a targetable-object statement).
        # 'cast this X' is a CASTING-CONDITION reference (Sphinx's
        # Insight: "if you cast this spell during your main phase, you
        # gain 2 life" -- "this spell" belongs to the condition, not the
        # effect "you gain 2 life", which per established precedent
        # (Black Market Connections' "You lose 2 life") wants no
        # target_scope at all; Jeska's Will's "as you cast this spell"
        # is the same idiom, already gold-confirmed empty).
        # 'with this X' is a LOCATIONAL modifier, not the clause's own
        # object (Ethereal Forager: "return an instant or sorcery card
        # exiled WITH THIS CREATURE to its owner's hand" -- the real
        # object is "an instant or sorcery card"; the false SELF match
        # here also suppressed target_type extraction as a side effect,
        # since target_type is never computed when scope==SELF).
        # "this X phase" ("this MAIN phase", Last Night Together: "After
        # this main phase, there is an additional combat phase") -- a
        # game-structure/timing reference, not a targetable object. The
        # captured word is "main", not "phase" itself, so this needs its
        # own check beyond the ('turn','way','phase') exclusion list above.
        # 'this spell'S' / 'this spell WAS <verb>' is the SAME casting-
        # condition idiom as 'cast this spell', just passive/possessive
        # word order ("if this spell's additional cost was paid", Cinder
        # Strike; "if this spell was cast using teamwork", Beast Mode;
        # gold-confirmed empty via Inscription of Insight's "if this
        # spell was kicked"). Deliberately narrower than excluding the
        # word 'spell' outright -- 'this spell' IS the genuine SELF
        # object as the direct patient of an action verb ("copy this
        # spell", "exile this spell", 67 corpus instances), only these
        # two passive/possessive shapes describe the spell's own casting
        # history rather than naming it as what the clause acts on.
        # 'this combat', like 'this turn', is a temporal reference (False
        # Orders, Winter's Chill: "...this combat become unblocked" /
        # "...dealt by that creature this combat"), not a targetable
        # object -- added to the same exclusion tuple as 'turn'/'phase'.
        # 'this game', same family again (Genesis Storm: "copy it for each
        # time you've cast your commander from the command zone THIS
        # GAME") -- this exclusion tuple has grown by one almost every
        # session; if another non-object noun turns up, consider an
        # allowlist of real game-object nouns instead of one more entry.
        # 'for each X .../equal to .../number of ... this Y' is the SAME
        # VALUE-computation-tail leak already excluded from the ALL-rule
        # and _restriction (#30) -- Banner of Kinship's "Creatures you
        # control...get +1/+1 FOR EACH fellowship counter ON THIS
        # ARTIFACT" was resolving to the badly wrong SELF (this check runs
        # BEFORE the ALL-rule below, so the incidental "this artifact"
        # inside the VALUE tail won by running first) instead of the
        # correct ALL. Deliberately requires a genuine "creatures/
        # permanents you control" mass-subject to ALSO be present, not
        # just the value-phrase-then-this-X shape alone -- Voracious
        # Hydra's "Double the number of +1/+1 counters ON THIS CREATURE"
        # has the identical shape (number-of ... this-X) but NO other
        # subject anywhere in the clause, so "this creature" there IS the
        # genuine, sole, correct SELF target; excluding it unconditionally
        # regressed a previously-correct case. Also mirrors this
        # function's own established precedent (Orchard Elemental: "Put
        # two +1/+1 counters on THIS CREATURE for each sprout vote" is
        # gold-confirmed SELF despite "this X"+"for each" co-occurring --
        # only order-reversed there, but the Banner-of-Kinship shape
        # showed order alone isn't sufficient either).
        return 'SELF'
    # "for each X" is the DISTRIBUTIVE quantifier (CDL EACH, Section 15.29),
    # not a mass-effect scope -- excluded here so "You gain 3 life FOR EACH
    # harvest vote" doesn't produce ALL. "each creature you control"/"each
    # player" (no leading "for") still does. "each OF THEM/THOSE X" is also
    # excluded -- that's a distributive reference to a SPECIFIC small
    # already-chosen set (Last Night Together: "Put two +1/+1 counters on
    # EACH OF THEM"), not a mass effect over an open-ended set.
    # "creatures/permanents you control" is excluded when it's part of a
    # VALUE-computation tail ("equal to the number of artifact creatures
    # you control", Robobrain War Mind) rather than the clause's own mass-
    # scoped subject -- the clause's real subject there is "you" (getting
    # energy counters), not the creatures being counted. Scoped narrowly
    # to "number of ... you control" (not a blanket "equal to" strip,
    # which would also eat Combo Attack's unrelated "equal to their power
    # TO TARGET CREATURE", where "target" is a second, genuine reference
    # after the value expression).
    # "X's power and toughness ARE EACH equal to..." (140 corpus instances,
    # all the identical characteristic-defining-ability shape) means "both,
    # individually" -- a grammatical distributive adjective, not a mass
    # effect over multiple creatures/players (Doubtless One). "each of
    # (their/his/her/its) turns" is the same temporal-frequency idiom as
    # "each turn", just possessive (Valgavoth: "the first time...during
    # EACH OF THEIR turns").
    if re.search(r'\ball\b', tl) \
            or (re.search(r'\beach\b', tl) and not re.search(
                r'\bfor each\b|\beach of (them|those)\b|\beach turn\b|\bare each\b|\beach of (?:their|his|her|its) turns?\b',
                tl)) \
            or _BARE_PLURAL_MASS.match(text) \
            or (_BARE_PLURAL_UNQUALIFIED.match(text) and not _NONE_SELECTION_VERB.search(tl)) \
            or (re.search(r'\b(creatures|permanents) you control\b', tl)
                and not re.search(r'\bnumber of\b[^.]{0,20}\b(?:creatures|permanents) you control\b', tl)) \
            or re.search(r'\bthe chosen \w+s\b.*\bcan\b', tl):
        # "the chosen Xs ... can" (Last Night Together's ONLY-restriction:
        # "Only THE CHOSEN CREATURES CAN attack...") scopes the restriction
        # over creatures broadly, same as an explicit "all"/"each" -- the
        # ONLY semantics (everything but the chosen ones is restricted)
        # implies
        # the blanket set, not a fresh TARGET or SELF.
        return 'ALL'
    if card_name:
        # Candidate self-reference strings, full legal name first. MTG
        # oracle text for a comma-named card ("Name, Title") usually
        # self-references by the SHORT form alone (Kumena, Tyrant of
        # Orazca's "Kumena can't be blocked"; Marath, Will of the Wild's
        # "Marath enters with..." -- both confirmed via Phase 5 grading to
        # get NO self-reference match at all, since every check below only
        # ever tried the full name) -- but not universally: some real
        # cards DO use the full name (Gregor, Shrewd Magistrate's "Whenever
        # GREGOR, SHREWD MAGISTRATE deals combat damage", confirmed present
        # verbatim in 66 corpus cards' own oracle text). Trying the full
        # name first preserves those; the short name is a fallback so
        # short-only self-references aren't missed, not a replacement.
        cn_candidates = (card_name.lower(),)
        if ',' in card_name:
            cn_candidates = (card_name.lower(), card_name.split(',')[0].strip().lower())
        # Subject-position resolution is a TRUE fallback (first candidate
        # that's a prefix of `bare`), not an independent per-candidate
        # loop -- the short name is trivially always also a prefix
        # whenever the full name is, so looping both independently let
        # the short-name attempt silently win on full-name text, breaking
        # the '<name> deals...to' adjacency check below: matched against
        # "gregor" alone, ", shrewd magistrate" sits between it and
        # "deals", so the exclusion missed the match it needed to make.
        # Caught by testing Gregor itself, not assumed safe.
        bare = re.sub(r'^(whenever|when)\s+', '', tl)
        subj_cn = next((c for c in cn_candidates if bare.startswith(c)), None)
        if subj_cn:
            # card-name-as-subject means SELF for a TRIGGER (the event's AGENT
            # is the card itself), a self-referential "enters" (Ruinous
            # Wrecking Crew), or a CANT-shaped self-restriction (Questing
            # Beast's "can't be blocked"/"can't be prevented" -- the card
            # itself is what the restriction describes). NOT for a plain ACTION
            # like Cerebral Vortex's "Cerebral Vortex deals damage to that
            # player" -- there the card is the AGENT of an effect on something
            # ELSE, and target_scope describes the AFFECTED object.
            #
            # NOTE: a "Whenever <Name> deals damage to Y" TRIGGER (is_trigger
            # branch) is gold-CONFIRMED to want SELF here (Questing Beast:
            # "Whenever Questing Beast deals combat damage to an opponent" ->
            # target_scope=SELF, restriction=OPPONENT) -- deliberately NOT
            # given the same agent-vs-patient exclusion the generic 'this X
            # deals...to Y' check has. This is a real semantic difference, not
            # an inconsistency: ACTION's target_scope names the AFFECTED
            # OBJECT of an effect, but TRIGGER's target_scope names the
            # SOURCE OF THE TRIGGERING EVENT -- "whose combat damage are we
            # watching for" is a different question than "what does this
            # effect act on", and the card itself is correctly SELF for the
            # former even though it's the agent, not the patient, of "deals
            # damage". Nine-Fingers Keene/Gregor's identically-shaped
            # "Whenever <Name> deals combat damage to a player" -> SELF is
            # therefore ALSO correct by the same reasoning, not a bug --
            # ruled on explicitly 2026-07-29 after Phase 5 grading had
            # flagged it as apparently wrong by ACTION's (inapplicable) logic.
            if is_trigger or re.search(rf'^{re.escape(subj_cn)} enters\b', bare) \
                    or re.search(r"can't|cannot", bare):
                return 'SELF'
        # Card-name-as-OBJECT: the clause names itself as the thing being
        # acted upon, not as subject/agent -- a different position the
        # check above never covers (it only tests bare.startswith(cn)).
        # Deliberately narrow to two verified-safe shapes, not a blanket
        # "clause ends with own name" rule, which would also fire on
        # "search your library for a card named <X>" (a DIFFERENT copy
        # being searched for, not the source itself -- 43 corpus instances
        # checked) and "where X is the number of counters on <X>" (the
        # self-reference is inside a VALUE's count-characteristic, not the
        # clause's own primary target -- The Eternity Elevator, Wilfred
        # Mott, Vulshok Factory, Instrument of the Bards all confirmed as
        # this shape and excluded by the where/equal-to guard below).
        # Found via Phase 5 grading (Rite of Renewal, Hawkeye, Bowslinger).
        # Independent per-candidate loop here (unlike subject-position
        # above) is safe: both `exile <cn>` (fullmatch) and `counters on
        # <cn>$` (end-anchored) require an EXACT match of the whole
        # remaining string, so a short-name candidate can never
        # spuriously match text that actually used the full name the way
        # the loose startswith prefix check could.
        for cn in cn_candidates:
            if f'named {cn}' not in tl:
                if re.fullmatch(r'(?:then\s+)?exile\s+' + re.escape(cn), tl):
                    return 'SELF'
                m = re.search(r'\bcounters?\s+on\s+' + re.escape(cn) + r'$', tl)
                if m and not re.search(r'\bwhere\b|\bequal to\b', tl[:m.start()]):
                    return 'SELF'
    if _NONE_SELECTION_VERB.search(tl) or 'of their choice' in tl or 'of your choice' in tl \
            or re.search(r'\bto you\b', tl):
        return 'NONE'
    return None


def _strip_for_each_immediate_noun(text):
    """Strip only 'for each <noun>' (the immediate quantified noun),
    leaving any trailing phrase intact -- Jeska's Will needs 'in target
    opponent's hand' to remain visible for target_type after 'for each
    card' is removed. Narrower than _strip_for_each_clause below, which
    would also (wrongly, for this purpose) delete that trailing phrase."""
    return re.sub(r'\bfor each \w+\b', '', text, flags=re.IGNORECASE)


def _strip_dealt_by(text):
    """Strip 'dealt by X' -- names the SOURCE of an effect, not what a
    restriction/type describes (Questing Beast: "damage that would be
    dealt BY CREATURES you control can't be prevented" -- "creatures" is
    the source, not the target)."""
    return re.sub(r'\bdealt by\b.*?(?=\bcan\'?t\b|\bcannot\b|$)', ' ', text, flags=re.IGNORECASE)


def _strip_for_each_clause(text):
    """Strip the WHOLE 'for each ...$' tail, including any trailing
    relative clause or temporal/ownership qualifier -- used for duration
    and restriction, where a word trapped INSIDE the quantifier's own
    relative clause must not leak out as if it described the outer
    clause (Season of Loss: "Draw a card for each creature that died
    under YOUR control THIS TURN" must set neither restriction=YOU nor
    duration=THIS_TURN -- both words belong to the quantifier, not the
    draw). Broader than _strip_for_each_immediate_noun, which target_type
    needs the trailing phrase preserved for (Jeska's Will)."""
    return re.sub(r'\bfor each\b.*$', '', text, flags=re.IGNORECASE)


def _target_type(text):
    # Strip a trailing 'where X is ...' value-expression -- that text
    # belongs to numeral_extractor's expr_span, not this clause's own
    # target description (Inscription of Insight: "...creature token,
    # where X is the number of cards in their hand" must not contribute
    # CARD from "cards" or HAND from "hand" buried inside the value clause).
    text = re.sub(r'\bwhere X is\b.*$', '', text, flags=re.IGNORECASE)
    text = _strip_for_each_immediate_noun(text)
    text = _strip_dealt_by(text)

    if re.match(r'^\s*choose (another|a) player\b', text, re.IGNORECASE):
        return None  # a pure player-selection clause -- WATCH, single example (Discerning Financier)

    if re.search(r'\bto you\b', text, re.IGNORECASE):
        return 'PLAYER'  # dative recipient (Day of the Doctor)

    # "a copy of that/those X" or "with that/those X" REDEFINES a preceding
    # generic type-noun via reference, rather than adding a second one
    # (Hate Mirage: "create a token that's a copy of THAT CREATURE" -> the
    # token IS a creature, not TOKEN). Checked before anaphora-stripping,
    # since that would erase this exact phrase.
    m = re.search(r'\b(?:copy of|with) (?:that|those) (\w+)\b', text, re.IGNORECASE)
    if m:
        word = m.group(1).lower().rstrip('s')
        for pat, val in _TARGET_TYPE_MAP:
            if pat.fullmatch(word) or pat.fullmatch(word + 's'):
                return val
        # Referenced noun isn't a real type (e.g. "characteristics") --
        # unknowable on its own, but doesn't erase type-nouns the clause's
        # OWN text already states outright before this phrase (Genku: "a
        # CREATURE TOKEN with those characteristics" -- "characteristics"
        # just modifies P/T/abilities, it doesn't redefine a type that was
        # already stated plainly). Compound per compound_target_type.
        matches = _collect_types(text[:m.start()])
        if matches:
            return matches[0] if len(matches) == 1 else matches
        return None

    if re.search(r'\bDoctors\b', text):  # WATCH: single example, Day of the Doctor
        return 'Doctor'

    stripped = _strip_anaphora(text)
    tl = stripped.lower()
    if re.search(r'\bany target\b', tl):
        return 'ANY'
    # 'X card(s)' where X is a type-adjective -- the object IS a card (in a
    # zone), compounded with the type-adjective's own value per
    # compound_target_type (Cemetery Recruitment: 'target creature card' ->
    # ['CARD','CREATURE'], not CARD alone; contrast Citadel Siege: 'target
    # creature' (no 'card' suffix) -> CREATURE, an on-battlefield object,
    # a genuinely different, non-compound case).
    # REVERSED 2026-07-29 from the original CARD-only policy (Victimize's
    # gold entry updated to match) -- Tier-D oracle cluster spot-check
    # (clause_pipeline_known_issues.md) found this dropped either the
    # card-ness or the type-specificity across 5 corpus clusters / ~1,000+
    # sampled clause instances (Cemetery Recruitment, Sister Hospitaller,
    # Sanguine Indulgence, etc.), the same synergy-signal loss
    # compound_target_type was introduced to fix for on-battlefield
    # objects. User confirmed treating "creature card" the same as an
    # on-battlefield "artifact creature token" rather than keeping the
    # zone-vs-battlefield distinction the original policy drew.
    # Basic land names (Plains/Island/Swamp/Mountain/Forest) map to LAND
    # for the same compounding, via _BASIC_LAND_TYPE (not in
    # _TARGET_TYPE_MAP, which only has the generic supertype noun 'land').
    # 'legendary' is a supertype with no target_type value of its own
    # (The Day of the Doctor's "...until you exile a legendary card" has
    # nothing to compound CARD with) -- stays bare CARD, unchanged.
    # 'permanent' was missing from this alternation entirely before this
    # pass (Karn's Temporal Sundering/Sevinne's Reclamation's "target
    # permanent card" fell through to the later 'target X' authoritative-
    # noun check instead, losing CARD entirely rather than losing the
    # type specificity) -- added so it goes through the same compounding
    # path as every other type-adjective here.
    # 'instant'/'sorcery' stay matched here (unchanged from before) but
    # still compound to nothing -- INSTANT/SORCERY have never existed as
    # target_type values anywhere in this codebase (checked before this
    # pass), so adding them would be new VOCABULARY growth, a separate
    # decision from this pass's combination-only compounding fix. "target
    # instant or sorcery card" (Stormchaser's Talent) stays bare CARD,
    # logged as an open question in clause_pipeline_known_issues.md, not
    # silently expanded into here.
    m = re.search(r'\b(creature|artifact|land|instant|sorcery|planeswalker|enchantment'
                  r'|permanent|plains|island|swamp|mountain|forest|legendary)\s+cards?\b', tl)
    if m:
        word = m.group(1)
        companion = _BASIC_LAND_TYPE.get(word)
        if companion is None:
            for pat, val in _TARGET_TYPE_MAP:
                if pat.fullmatch(word) or pat.fullmatch(word + 's'):
                    companion = val
                    break
        return ['CARD', companion] if companion else 'CARD'
    # The noun immediately after 'target' is authoritative when present --
    # checked BEFORE the whole-text scan, so a later, unrelated noun
    # (Inscription of Insight: "Target player creates ... creature token")
    # can't outrank the actual targeted object. Excludes "target player/
    # opponent controls" -- that's a RESTRICTION on some other mass-scoped
    # object (Requisition Raid, Dawnglare Invoker), not this clause's type.
    m = re.search(r'\btarget ([a-z]+(?: or [a-z]+)?)', tl)
    if m and not re.match(r'(player|opponent) controls\b', tl[m.start(1):]):
        if ' or ' in m.group(1):
            parts = m.group(1).split(' or ')
            types = [t for p in parts for pat, t in _TARGET_TYPE_MAP if pat.fullmatch(p.strip() + 's') or pat.fullmatch(p.strip())]
            if len(types) == len(parts):
                return types
        else:
            word = m.group(1)
            for pat, val in _TARGET_TYPE_MAP:
                if pat.fullmatch(word) or pat.fullmatch(word + 's'):
                    return val
    # Compound type: a single described/created object frequently has
    # several simultaneous type identities that each carry independent
    # synergy signal (Nimble Thopterist's "Thopter artifact creature
    # token" is ARTIFACT + CREATURE + TOKEN at once, not one of the
    # three) -- collect ALL matches rather than stopping at the first.
    # Distinct from the 'target X or Y' disjunctive-list case above
    # (explicit 'or' between DIFFERENT possible targets, handled and
    # returned earlier): this is one object, adjacent type-nouns, no
    # 'or'. See _decisions.compound_target_type.
    #
    # "this X" (the clause's own AGENT, e.g. "this creature") must not
    # contribute a type here -- by the time we reach this line the caller
    # has already confirmed target_scope != SELF (see the "target_type is
    # never extracted for SELF" gate at both call sites), so any "this X"
    # still present is, by construction, the SOURCE causing an effect on a
    # SEPARATE object, not the affected object itself (Spawnwrithe:
    # "Whenever THIS CREATURE deals combat damage to a player" was
    # resolving to CREATURE -- the source -- instead of PLAYER -- the
    # actual recipient -- because "creature" and "player" both matched and
    # PLAYER lost the tie-break meant for a different false-positive shape,
    # Ruinous Wrecking Crew's "each player sacrifices..."). Same agent-vs-
    # patient distinction _target_scope's own "this X deals ... to Y"
    # exclusion already documents, just not previously applied here too.
    for_types = re.sub(r'\bthis \w+\b', ' ', stripped, flags=re.IGNORECASE)
    matches = _collect_types(for_types)
    if matches:
        return matches[0] if len(matches) == 1 else matches

    # TRIED and REVERTED: a "last resort" fallback re-deriving target_type
    # from a bare anaphoric "that X" when the stripped scan found nothing.
    # Helped exactly one case (Engulfing Slagwurm's "destroy that creature"
    # -> CREATURE) but actively broke three others that share the identical
    # surface shape (Wild Shape's "becomes that creature type" wants no
    # type at all -- "creature type" is a compound noun, not "a creature";
    # Midnight Crusader Shuttle's "attacking that player" and Engulfing
    # Slagwurm's OWN second clause "equal to that creature's toughness"
    # both want no type either). Net negative -- removed rather than kept
    # for a single win. "destroy that creature" reports no target_type,
    # which is a defensible under-extraction, not a wrong guess.
    return None


def _zone(text):
    text = re.sub(r'\bwhere X is\b.*$', '', text, flags=re.IGNORECASE)
    # "remains on the BATTLEFIELD" is part of the WHILE_SOURCE_ON_BATTLEFIELD
    # duration idiom, not a zone the clause moves something to/from (Day of
    # the Doctor: "for as long as this Saga remains on the battlefield").
    text = re.sub(r'\bremains on the battlefield\b', '', text, flags=re.IGNORECASE)
    # LAST destination match wins, not first -- an earlier "into X" can be
    # part of a restrictive relative clause describing the object's own
    # history ("a creature card put INTO A GRAVEYARD this way"), while the
    # clause's own destination ("to the BATTLEFIELD") comes later, at the
    # main verb (Zero Point Ballad).
    dest_matches = list(_ZONE_DESTINATION.finditer(text))
    if dest_matches:
        word = dest_matches[-1].group(1).lower()
        if word == 'exile':
            return 'EXILE'
        for pat, val in _ZONE_MAP:
            if pat.search(word):
                return val
    # TRIED: stripping a "equal to/for each/number of...$" VALUE-computation
    # tail before this fallback scan, the same class of fix already applied
    # to _target_scope/_restriction (#30), to fix Doubtless One's spurious
    # zone=BATTLEFIELD ("equal to the number of Clerics ON THE BATTLEFIELD")
    # and Genesis Storm's spurious zone=COMMAND ("for each time you've cast
    # ... FROM THE COMMAND ZONE this game"). REVERTED: it also strips
    # Jeska's Will's gold-confirmed, genuinely-wanted zone=HAND ("Add {R}
    # FOR EACH card IN TARGET OPPONENT'S HAND") -- both Genesis Storm and
    # Jeska's Will use "for each", so the phrase alone doesn't distinguish
    # "incidental VALUE-tail zone" from "the actual zone this clause cares
    # about". Needs a real distinguishing rule from more examples, not a
    # blanket strip -- left as a documented, unfixed finding.
    for pat, val in _ZONE_MAP:
        if pat.search(text):
            return val
    return None


def _restriction(text):
    text = _strip_for_each_clause(text)
    # Strip "where X is ..." (Season of Loss's VAR value-expression, "in
    # YOUR graveyard" there belongs to numeral_extractor's expr_span, not
    # this clause's own restriction), "your next turn"/"your turn" (part
    # of the DURATION phrase, Elspeth: "until YOUR NEXT TURN" is not an
    # ownership signal), and "your ... phase" the same way (Sphinx's
    # Insight: "if you cast this spell during YOUR MAIN PHASE, you gain 2
    # life" -- a casting-timing condition, not a restriction on the
    # effect "you gain 2 life", which per established precedent (Black
    # Market Connections' "You lose 2 life") wants no restriction tag).
    text = re.sub(r'\bwhere X is\b.*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\byour (next )?turn\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\byour (?:\w+ )?phase\b', '', text, flags=re.IGNORECASE)
    tl = text.lower()
    if re.search(r"that (player|opponent) controls", tl):
        return 'THAT_PLAYER'
    if re.search(r'\btarget player\b.*\bcontrols\b', tl):
        return 'TARGET_PLAYER'
    if re.search(r"you don't control\b", tl) or re.search(r'\banother player\b', tl):
        return 'NOT_YOU'
    # "you control" describes the TARGETED/AFFECTED OBJECT's own ownership
    # ("target Treasure YOU CONTROL") -- must outrank the subject-position
    # "That player" default below, which is about who the clause's ACTOR
    # is, not what the object it acts on belongs to (Discerning Financier:
    # "That player gains control of target Treasure you control" is
    # restriction=YOU, from the Treasure, not DEFENDING_PLAYER from the
    # subject -- those are different questions).
    # Excluded when "you control" is itself inside a VALUE-computation
    # tail ("equal to the number of artifact creatures you control",
    # Robobrain War Mind) -- there it describes what's being COUNTED, not
    # the clause's own affected object's ownership. Narrowly scoped to
    # "number of ... you control" so genuine object-ownership mentions
    # ("target Treasure you control") are unaffected.
    if re.search(r'\byou control\b', tl) \
            and not re.search(r'\bnumber of\b[^.]{0,20}\byou control\b', tl):
        return 'YOU'
    # Subject-position "That player"/"that opponent" (Midnight Crusader
    # Shuttle's mode 0: "That player sacrifices a creature of their
    # choice", no explicit object-ownership phrase of its own) resolves to
    # the CHOICE container's own chooser field (DEFENDING_PLAYER on every
    # gold example so far) -- same structural unit, not a separate cross-
    # clause reference like the embedded "...controls" case above. WATCH:
    # single-example, may need generalizing once a non-villainous-choice
    # case appears.
    if re.match(r'^\s*that (player|opponent)\b', text, re.IGNORECASE):
        return 'DEFENDING_PLAYER'
    if re.search(r"target opponent'?s?\b", tl) or re.search(r'\ban opponent controls\b', tl) \
            or re.search(r'\ban opponent\b', tl):
        return 'OPPONENT'
    if re.search(r'\bto you\b', tl):
        return 'YOU'  # dative recipient (Day of the Doctor: "this Saga deals 13 damage TO YOU")
    # sacrifice/discard default to YOU only when the ACTOR is you (implicit
    # subject) -- "EACH PLAYER sacrifices a creature" means each player's
    # OWN creature, not YOU specifically (Ruinous Wrecking Crew, Season of
    # Loss); target_scope=EACH_PLAYER + chooser=EACH_PLAYER already cover
    # this, restriction=YOU here would be actively wrong, not just redundant.
    if _IMPLICIT_YOU_VERB.search(tl) and not re.match(r'^\s*each (player|opponent)\b', text, re.IGNORECASE):
        return 'YOU'
    # Broad possessive fallback -- "of YOUR library"/"in YOUR graveyard"
    # (Jeska's Will, Victimize, Day of the Doctor x2, Myriad Landscape --
    # 4 of 5 "your <zone>" clauses in gold want restriction=YOU; Myriad was
    # the true outlier and was brought in line with the majority rather
    # than the other way around). Checked last, after every more specific
    # pattern, so it never outranks NOT_YOU/OPPONENT/TARGET_PLAYER/
    # THAT_PLAYER/DEFENDING_PLAYER.
    # Excluded when "your" is itself inside a VALUE-computation tail
    # ("equal to YOUR devotion to white", Evangel of Heliod) -- there it's
    # part of computing a NUMBER, not a restriction on the clause's own
    # affected object (which happens to end up under your control anyway,
    # but not because this possessive says so).
    if re.search(r'\byour\b', tl) and not re.search(r'\bequal to\b[^.]{0,20}\byour\b', tl):
        return 'YOU'
    return None


def _chooser(text):
    tl = text.lower()
    if 'each player' in tl and ('of their choice' in tl or 'of its choice' in tl):
        return 'EACH_PLAYER'
    if 'of their choice' in tl:
        # subject-driven: whoever the clause's own subject is
        if tl.strip().startswith('that player'):
            return 'DEFENDING_PLAYER'
        return None
    if 'of your choice' in tl:
        return 'YOU'
    return None


_DURATION_MAP = [
    (re.compile(r'\buntil end of turn\b', re.I), 'UNTIL_EOT'),
    (re.compile(r'\buntil your next turn\b', re.I), 'UNTIL_YOUR_NEXT_TURN'),
    (re.compile(r'\bfor as long as this .* remains on the battlefield\b', re.I), 'WHILE_SOURCE_ON_BATTLEFIELD'),
    (re.compile(r'\bthis turn\b', re.I), 'THIS_TURN'),
]


def _duration(text):
    # Strip "for each X...$" -- a duration word trapped inside the
    # quantifier clause describes WHEN the counted objects qualify, not
    # how long THIS clause's own effect lasts (Season of Loss: "Draw a
    # card for each creature that died under your control THIS TURN" has
    # no duration tag at all; "this turn" scopes the count, not the draw).
    text = _strip_for_each_clause(text)
    # Same leak, different quantifier shape: "equal to the number of X
    # ... THIS TURN" -- the temporal word describes the COUNT expression,
    # not the outer clause's own effect duration (Cerebral Vortex: "deals
    # damage to that player equal to the number of cards they've drawn
    # THIS TURN" must get no duration tag at all -- the damage is dealt
    # once, immediately, not "this turn"). Found via structural_linker.py's
    # build (a corpus spot-check surfaced it), fixed here since it's a
    # local single-clause leak, not a cross-clause one.
    text = re.sub(r'\bequal to\b.*$', '', text, flags=re.IGNORECASE)
    # Same leak as target_type/zone/restriction: a temporal word inside a
    # VAR value's own "where X is ..." expression describes THAT value, not
    # this clause's effect duration (Florian, Voldaren Scion: "...where X
    # is the total amount of life your opponents lost THIS TURN" must not
    # set duration=THIS_TURN on the outer "look at the top X cards" action).
    text = re.sub(r'\bwhere X is\b.*$', '', text, flags=re.IGNORECASE)
    for pat, val in _DURATION_MAP:
        if pat.search(text):
            return val
    return None


def _excludes(text):
    # "another PLAYER" is a restriction on WHICH player (already captured
    # as restriction=NOT_YOU), not an "exclude the source from its own
    # matched set" pattern -- that EXCEPT:SELF semantic is specifically for
    # "another PERMANENT/CREATURE" in a trigger's own filter (Genku:
    # "another nontoken permanent you control"). Discerning Financier's
    # "Choose another player" must not get excludes=SELF.
    m = re.search(r'\banother\b', text, re.IGNORECASE)
    if m and not re.search(r'\banother player\b', text, re.IGNORECASE):
        # An earlier "target <noun>" in the SAME clause means "another" is
        # ORDINAL -- distinguishing this target from one ALREADY named in a
        # multi-target spell/effect (Incremental Growth: "target creature,
        # ANOTHER target creature, a third target creature"; Domri Rade:
        # "Target creature you control fights ANOTHER target creature";
        # Cruel Entertainment: "Choose target player and ANOTHER target
        # player" -- also catches the "another player" guard's own blind
        # spot, since "target" sits between the two words there) -- not the
        # self-exclusion idiom this rule exists for (a permanent excluding
        # ITSELF from its own trigger/target filter). Checked at clause
        # granularity: 37 corpus clauses have an earlier same-clause
        # "target <noun>" before "another", every one confirmed a genuine
        # second/third target relationship, not self-exclusion -- no
        # counterexample. No replacement tag invented for this relationship
        # (the schema has no per-target distinctness field, same class of
        # gap as the multi-target restriction-conflation limitation already
        # logged) -- just correctly emitting nothing rather than something
        # actively misleading.
        before = text[:m.start()]
        if re.search(r'\btarget \w+', before, re.IGNORECASE):
            return None
        return 'SELF'
    # Plural "other creatures"/"other permanents" (no leading "an") is the
    # same SELF-exclusion semantic, just pluralized (Kylox: "sacrifice any
    # number of OTHER creatures") -- deliberately narrower than a bare
    # \bother\b match, which would also fire on "other creature TYPES" (a
    # compound noun), "other THAN" (a different idiom entirely), "other
    # COSTS"/"other EFFECTS"/"other ABILITIES" etc. (2,202 total "other"
    # occurrences in the corpus, the overwhelming majority NOT this
    # semantic) -- scoped to the exact noun classes ("creature(s)"/
    # "permanent(s)") this exclusion is already documented as being FOR.
    # Checked 317 corpus instances of this narrower shape; all confirmed
    # genuine self-exclusion, no counterexample found.
    if re.search(r'\bother (?:creature|permanent)s\b', text, re.IGNORECASE):
        return 'SELF'
    return None


def _needs_excludes_resolution(text):
    """'all other X' excludes whatever a PRIOR clause selected -- not
    locally resolvable (same cross-clause boundary as refers_to), but the
    NEED for resolution -- unlike the antecedent itself -- IS locally
    decidable, same split as refers_to_prior vs refers_to. Structural
    linking (structural_linker.py) resolves this to the nearest preceding
    CHOOSE-shaped clause (Day of the Doctor: "You may exile ALL OTHER
    creatures" excludes "Choose up to three Doctors") -- WATCH, one
    example."""
    return bool(re.search(r'\ball other\b', text, re.IGNORECASE))


def _restriction_kind(text):
    if re.search(r"can't\b|cannot\b", text, re.IGNORECASE):
        m = re.search(r"can't be (\w+)|cannot be (\w+)", text, re.IGNORECASE)
        if m:
            verb = (m.group(1) or m.group(2)).upper()
            return 'CANT', f'BE_{verb}'
        m2 = re.search(r"can't (\w+)|cannot (\w+)", text, re.IGNORECASE)
        if m2:
            verb = (m2.group(1) or m2.group(2)).upper()
            return 'CANT', verb
    if re.match(r'^\s*only\b.*\bcan\s+(\w+)', text, re.IGNORECASE):
        m = re.match(r'^\s*only\b.*\bcan\s+(\w+)', text, re.IGNORECASE)
        return 'ONLY', m.group(1).upper()
    return None, None


# Generic: 'that/those <any noun>' is anaphoric regardless of which noun
# follows (was restricted to player/creature/opponent -- missed "and gains
# THAT ABILITY", Wild Shape). 'this way' is also anaphoric ("as just
# described", Midnight Crusader Shuttle).
_ANAPHORA_CUE = re.compile(
    r'\b(?:that (\w+)|those (\w+)|the chosen \w+|them|they|it\'?s?|this way)\b', re.IGNORECASE)


def _add_anaphora(tags, text, card_name=None):
    """refers_to_prior is locally decidable (an anaphora cue is present);
    refers_to (WHICH clause) is not -- deliberately left unset, same
    boundary as numeral_extractor's `binding`."""
    for m in _ANAPHORA_CUE.finditer(text):
        # bare "it"/"it's" referring back to the card's OWN name already
        # named earlier in the SAME clause is a self-reference, not a
        # cross-clause dependency (Ruinous Wrecking Crew: "The Ruinous
        # Wrecking Crew enters with X +1/+1 counters on IT" -- "it" =
        # the same card named at the start of this same text).
        # Comma-named cards ("Name, Title") often self-reference by the
        # SHORT form alone in body text (Kumena/Marath-class, same root
        # cause as _target_scope's card-name check) -- try the short form
        # too, not just the full legal name, so this exclusion still fires.
        if m.group(0).lower().startswith('it') and card_name and (
                card_name.lower() in text[:m.start()].lower()
                or (',' in card_name and card_name.split(',')[0].strip().lower() in text[:m.start()].lower())):
            continue
        # Same self-reference shape, but the antecedent is a "target X"
        # phrase established earlier in the SAME clause rather than the
        # card's own name (Chaos Warp: "The owner of TARGET PERMANENT
        # shuffles IT into their library" -- "it" = the permanent just
        # named a few words earlier, not a cross-clause dependency).
        if m.group(0).lower().startswith('it') \
                and re.search(r'\btarget \w+', text[:m.start()], re.IGNORECASE):
            continue
        # Same shape again, antecedent established via "this X" instead of
        # a card name or "target X" (Obstinate Gargoyle: "This creature has
        # flying as long as IT'S modified" -- "it" = "this creature",
        # named at the very start of the same clause).
        if m.group(0).lower().startswith('it') \
                and re.search(r'\bthis \w+', text[:m.start()], re.IGNORECASE):
            continue
        # Same shape again, antecedent established via "Enchanted X" (the
        # Aura idiom naming what it's attached to) instead of a card name,
        # "target X", or "this X" (Clutch of Undeath: "Enchanted creature
        # gets +3/+3 as long as IT'S a Zombie" -- "it" = "enchanted
        # creature", named at the very start of the same clause; same bug
        # class also found on Encrust and Mesmerizing Dose, both "Enchanted
        # X doesn't/can't... its...").
        if m.group(0).lower().startswith('it') \
                and re.search(r'\benchanted \w+', text[:m.start()], re.IGNORECASE):
            continue
        # 'that share'/'that would'/'that has' etc is a RELATIVE CLAUSE
        # ("cards THAT SHARE a land type"), not anaphora -- only 'that NOUN'
        # refers back to an established object. Myriad Landscape.
        following = m.group(1) or m.group(2)
        # "that much"/"that many" is a SCALED-value phrase (numeral_extractor's
        # domain, "twice THAT MANY tokens"), not object anaphora.
        if following and following.lower() in _RELATIVE_CLAUSE_VERB | {'much', 'many'}:
            continue
        # Self-contained reference: the noun ALSO appears earlier in the
        # SAME text, before this mention -- it's referring within its own
        # sentence, not across a clause boundary (Elspeth, Storm Slayer:
        # "If one or more TOKENS would be created..., twice that many of
        # THOSE TOKENS are created instead" -- "tokens" was already named
        # in this same clause, so this is not the kind of cross-clause
        # dependency refers_to_prior tracks).
        if following:
            stem = following.lower().rstrip('s')
            before = text[:m.start()].lower()
            if re.search(rf'\b{re.escape(stem)}s?\b', before):
                continue
            # Same self-containment check, but for a HYPERNYM: "that
            # PERMANENT" after "target creature or planeswalker" (Unleash
            # Shell) is still a same-clause self-reference -- "permanent"
            # never appears literally, but creature/planeswalker (both
            # permanent types) already established the referent earlier
            # in this same text. WATCH -- one example; extend the map only
            # if another permanent-type hypernym forces it.
            if stem == 'permanent' and re.search(
                    r'\b(creature|artifact|enchantment|land|planeswalker)s?\b', before):
                continue
        tags['refers_to_prior'] = True
        return
