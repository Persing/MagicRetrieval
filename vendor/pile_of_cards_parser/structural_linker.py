"""
Clause-splitting pipeline, stage 5: structural linking.

Resolves what the four per-clause stages (clause_splitter, type_classifier,
tag_extractor, numeral_extractor) deliberately left unresolved because a
single clause can't see its siblings:

  - refers_to_prior=True clauses: WHICH prior clause the anaphor ("that
    creature", "them", "that player", "the chosen cards", "if you do", ...)
    points at.
  - COUNT/VAR/CHARACTERISTIC values with binding=None: what the dynamic
    quantity is bound to (Questing Beast's "that much damage", Engulfing
    Slagwurm's "that creature's toughness").
  - distributive-chain inheritance: a plural-target antecedent's per-
    instance semantics spreading to clauses that refer back to it.
  - AND-split sibling tag sharing: two clauses produced by splitting one
    "X and Y" sentence share a subject/scope neither literally restates.
  - excludes_prior=True clauses ("all other creatures"): WHICH prior
    clause's selection they're excluded from (Day of the Doctor's "You
    may exile ALL OTHER creatures" excludes "Choose up to three Doctors").

DELIBERATE DESIGN CHOICE: this stage resolves to the antecedent's own
PHRASE (its text_span), not a clause_id. The real pipeline has no clause
ids at all (clause_splitter.Clause is id-less; ids only exist in the gold
file for annotation bookkeeping) and the consumer is an embedding system,
where what a reference MEANS is the useful signal, not a graph pointer to
chase. See clause_pipeline_project.md decision log for this call.

Input contract: an ordered list of per-card clause dicts (document order),
each already carrying `text_span`, `type`, `tags` (dict), `values` (list) --
the union of what the four earlier stages produced. This module only adds
keys; it never overwrites what an earlier stage already set.

Scope boundaries (see clause_pipeline_known_issues.md #8 and this module's
own additions):
  - Orchard Elemental's vote-option bindings are NOT resolved here --
    VOTE_OPTIONS is out of scope pipeline-wide (known-issues #2).
  - Tag inheritance beyond the three shapes below is deliberately NOT
    attempted generically -- Elspeth's "Those creatures gain flying"
    proves a blanket "inherit target_scope/target_type onto any resolved
    anaphor" rule is WRONG (gold wants no inheritance there at all).
"""
import re


# ─────────────────────────────────────────────── entity-registry categories

_NOUN_PATTERNS = {
    'PLAYER': re.compile(r'\b(?:players?|opponents?)\b', re.I),
    'CREATURE': re.compile(r'\bcreatures?\b', re.I),
    'CARD': re.compile(r'\bcards?\b', re.I),
    'TOKEN': re.compile(r'\btokens?\b', re.I),
    'COMBAT_PHASE': re.compile(r'\bcombat phase\b', re.I),
}

# A noun match preceded (within a short window) by an anaphoric determiner
# is a REFERENCE, not a fresh introduction -- must not (re)register the
# entity. "that creature TYPE" is the canonical trap (Wild Shape): "type"
# makes it a compound noun, but the anaphoric "that" already excludes it
# here regardless.
_ANAPHORIC_DETERMINER = re.compile(
    r'\b(?:that|those|these|the chosen|of them|them|they)\b[\s\w\'-]{0,15}$', re.I)


def _fresh_mentions(text):
    """Categories this clause introduces via a non-anaphoric noun mention."""
    found = set()
    for cat, pat in _NOUN_PATTERNS.items():
        for m in pat.finditer(text):
            prefix = text[max(0, m.start() - 24):m.start()]
            if not _ANAPHORIC_DETERMINER.search(prefix):
                found.add(cat)
                break
    return found


# ─────────────────────────────────────────────────────── anaphora cue rules
# Order matters: more specific phrase-shapes must be checked before the
# generic ones they'd otherwise be swallowed by (e.g. "that creature type"
# before bare "that creature").

_CUE_PROCEDURAL = re.compile(r'^(?:if you do\b|.*\bthis way\b)', re.I)
_CUE_CHOICE_CHARACTERISTIC = re.compile(
    r'\bthat (?:base power and toughness|creature type|ability)\b|\bthose characteristics\b', re.I)
_CUE_EVENT_AMOUNT = re.compile(r'\bthat much \w+\b', re.I)
_CUE_THE_CHOSEN = re.compile(r'\bthe chosen (\w+)\b', re.I)
_CUE_COMBAT_PHASE = re.compile(r'\bthat combat phase\b', re.I)
_CUE_THAT_PLAYER = re.compile(r'\bthat player\b', re.I)
_CUE_THAT_CREATURE = re.compile(r"\bthat creature(?:'s)?\b(?!\s+type)", re.I)
_CUE_THAT_CARD = re.compile(r'\bthat card\b', re.I)
_CUE_THOSE_NOUN = re.compile(r'\bthose (?:\w+\s+)?(tokens|creatures|cards)\b', re.I)
_CUE_BARE_PRONOUN = re.compile(r'\b(?:them|they)\b', re.I)
_CUE_BARE_IT = re.compile(r"\bit'?s?\b", re.I)

_NOUN_TO_CATEGORY = {'tokens': 'TOKEN', 'creatures': 'CREATURE', 'cards': 'CARD'}


def _resolve_cues(text, registry, last_touched, prev_clause_type, prev_idx):
    """Returns a list of (clause_index, phrase) antecedents this clause's
    anaphoric cues resolve to, using registry state as of BEFORE this
    clause (its own fresh mentions haven't been applied yet)."""
    hits = []  # list of clause indices, in cue-appearance order

    if _CUE_PROCEDURAL.match(text):
        if 'PROCEDURAL' in registry:
            hits.append(registry['PROCEDURAL'])
        return _dedupe_hits(hits)

    if _CUE_CHOICE_CHARACTERISTIC.search(text) and 'CHOICE' in registry:
        hits.append(registry['CHOICE'])

    if _CUE_EVENT_AMOUNT.search(text) and 'TRIGGER' in registry:
        hits.append(registry['TRIGGER'])

    m = _CUE_THE_CHOSEN.search(text)
    if m:
        cat = _NOUN_TO_CATEGORY.get(m.group(1).lower())
        if cat and cat in registry:
            hits.append(registry[cat])

    if _CUE_COMBAT_PHASE.search(text) and 'COMBAT_PHASE' in registry:
        hits.append(registry['COMBAT_PHASE'])

    if hits:
        return _dedupe_hits(hits)

    # An action clause with none of the STRONG cues above, immediately
    # following a CONDITION clause, is that condition's consequent --
    # takes priority over the WEAK generic-pronoun cues below (Midnight
    # Crusader Shuttle's "tap it, and it's attacking that player" refers
    # to the preceding "If you gain control of a creature this way", not
    # to the villainous-choice's defending player that "that player" would
    # otherwise resolve to). Must stay BELOW the strong cues, though --
    # Victimize's "If you do" / "return the chosen cards..." pair needs
    # "the chosen cards" (a strong cue) to win instead. WATCH -- one
    # example on each side of this ordering.
    if prev_clause_type == 'CONDITION':
        return [prev_idx]

    if _CUE_THAT_PLAYER.search(text) and 'PLAYER' in registry:
        hits.append(registry['PLAYER'])

    if _CUE_THAT_CREATURE.search(text) and 'CREATURE' in registry:
        hits.append(registry['CREATURE'])

    if _CUE_THAT_CARD.search(text) and 'CARD' in registry:
        hits.append(registry['CARD'])

    m = _CUE_THOSE_NOUN.search(text)
    if m:
        cat = _NOUN_TO_CATEGORY.get(m.group(1).lower())
        if cat and cat in registry:
            hits.append(registry[cat])

    if not hits and _CUE_BARE_PRONOUN.search(text) and last_touched.get('idx') is not None:
        hits.append(last_touched['idx'])

    # Bare "it"/"it's" referring to the card itself -- e.g. "Whenever
    # Skanos Dragonheart attacks, IT gets +X/+X..." -- resolved via a
    # dedicated SELF registry slot (set whenever a clause's own
    # target_scope tag is SELF), not the generic entity registry, since
    # the antecedent here is a trigger event, not an introduced object.
    # tag_extractor.py's own same-clause self-reference exclusion (skip
    # "it" when card_name already appears earlier in the SAME text) can't
    # see a card_name mentioned in an EARLIER SIBLING clause -- this is
    # that cross-clause half of the same boundary. Falls back to
    # last-touched like the other bare pronouns if no SELF antecedent
    # exists yet.
    if not hits and _CUE_BARE_IT.search(text):
        if 'SELF' in registry:
            hits.append(registry['SELF'])
        elif last_touched.get('idx') is not None:
            hits.append(last_touched['idx'])

    return _dedupe_hits(hits)


def _dedupe_hits(hits):
    seen = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return seen


# ───────────────────────────────────────────────────────── distributive
# A back-reference to a PLURAL antecedent inherits per-instance semantics
# -- but ONLY when at least two sibling clauses form a genuine chain off
# the same antecedent (a single one-off "put them onto the battlefield"
# does NOT get a distributive tag in gold even though its antecedent is
# equally plural -- Myriad Landscape, Cerebral Vortex, Jeska's Will, and
# Victimize all confirm this; only Hate Mirage's and Last Night Together's
# multi-clause chains want it), and only on ACTION clauses (Last Night
# Together's final STATIC restriction clause resolves to the same plural
# antecedent as its ACTION siblings but explicitly does NOT want the tag).
# "each" is deliberately excluded from the plurality signal -- Elspeth's
# "each creature you control" is target_scope ALL, not a distributive
# multi-target chain.
_PLURAL_NUMBER_WORD = re.compile(r'\b(?:two|three|four|five)\b', re.I)

# "Choose ONE of them/those" explicitly asserts a single pick from a
# plural set -- contradicts distributive (per-instance) semantics no
# matter how plural the antecedent is (Strongbox Raider).
_SINGULAR_SELECTION = re.compile(r'\bchoose one\b', re.I)


def _antecedent_is_plural(clause):
    if clause.get('tags', {}).get('distributive'):
        return True
    if _PLURAL_NUMBER_WORD.search(clause.get('text_span', '')):
        return True
    for v in clause.get('values', []):
        if v.get('role') == 'count' and isinstance(v.get('value'), (int, float)) and v['value'] >= 2:
            return True
    return False


# ───────────────────────────────────────── narrow, WATCH-tagged tag rules
# Cerebral Vortex is the only confirmed example of an anaphor also needing
# its clause-level target_scope/target_type inherited. Scoped tightly to
# "deals damage to that <category>" so it does NOT fire on Elspeth's
# "Those creatures gain flying" (proven wrong for that shape) -- WATCH,
# revisit if a second example disagrees with this narrow trigger.
_CUE_DAMAGE_TO_ANAPHOR = re.compile(r'\bdamage to (?:that|those) (?:player|creature)s?\b', re.I)


def _apply_damage_target_inheritance(clause, antecedent):
    if 'target_scope' in clause['tags'] or 'target_type' in clause['tags']:
        return
    if not _CUE_DAMAGE_TO_ANAPHOR.search(clause['text_span']):
        return
    for key in ('target_scope', 'target_type'):
        if key in antecedent['tags']:
            clause['tags'][key] = antecedent['tags'][key]


# Bare "that creature" as a direct object ("destroy that creature") IS the
# clause's target_type, even though nothing upstream can say so locally --
# tag_extractor's own attempt at this (see known-issues #8) broke on
# "that creature's" (a possessive modifier inside a value expression,
# Engulfing Slagwurm's OWN sibling "equal to that creature's toughness",
# not itself the clause's target) and "that creature type" (a compound
# noun, Wild Shape). Now that structural_linker already knows exactly
# which cue fired the resolution, both traps are avoidable by requiring
# the bare, non-possessive form specifically. WATCH -- one example.
_CUE_THAT_CREATURE_BARE = re.compile(r"\bthat creature\b(?!'s)(?!\s+type)", re.I)


def _apply_bare_that_creature_type(clause):
    if clause['tags'].get('target_type'):
        return
    if _CUE_THAT_CREATURE_BARE.search(clause['text_span']):
        clause['tags']['target_type'] = 'CREATURE'


# "put it/that card onto the battlefield" after a land-fetch clause: the
# object stops being a library CARD and becomes a battlefield LAND, but
# tag_extractor's own "X card(s)" rule only ever produces CARD (correct
# for the zone it started in) and this clause's own text has no type-noun
# of its own to extract locally -- found via testing Farseek/Cultivate/
# Rampant Growth (top-10 Commander staples). Not compound with CARD: once
# on the battlefield it's a LAND, not a zone-card anymore, so the two
# types don't coexist the way a token's ARTIFACT+CREATURE+TOKEN do.
_CUE_ONTO_BATTLEFIELD = re.compile(r'\bonto the battlefield\b', re.I)
_CUE_LAND_ANTECEDENT = re.compile(
    r'\blands?\b|\bplains\b|\bisland\b|\bswamp\b|\bmountain\b|\bforest\b', re.I)


def _apply_battlefield_land_type(clause, antecedent):
    if clause['tags'].get('target_type'):
        return
    if not _CUE_ONTO_BATTLEFIELD.search(clause['text_span']):
        return
    if _CUE_LAND_ANTECEDENT.search(antecedent['text_span']):
        clause['tags']['target_type'] = 'LAND'


# AND-split sibling merge -- one gold-backed example (Lorehold Command).
# The continuation clause is recognizable purely by starting with a bare
# "and " (no subject of its own).
#
# Direction matters and is NOT symmetric across all keys, found via a
# corpus spot-check (Exsanguinator Cavalry): "put a +1/+1 counter on that
# creature and create a Blood token" -- naive bidirectional merge copied
# the AND-clause's OWN target_type (TOKEN, from "create a Blood token",
# a genuinely different, freshly-introduced object) backward onto the
# anaphoric "that creature" clause, overwriting its real referent. So:
#   - target_scope/target_type/restriction flow prev -> cur ONLY, and
#     ONLY when cur introduces no fresh entity of its own (Lorehold's
#     "and gain indestructible and haste" has no target noun at all --
#     Exsanguinator's "and create a Blood token" does, so it's excluded).
#   - duration flows cur -> prev ONLY (a trailing duration modifier
#     naturally applies to the whole compound sentence, not just its
#     second half) -- only one worked example, WATCH.
_AND_CONTINUATION = re.compile(r'^and\b', re.I)
_FORWARD_KEYS = ('target_scope', 'target_type', 'restriction')

# excludes_prior -- "all other X" excludes whatever a prior CHOOSE-shaped
# clause selected (Day of the Doctor's "You may exile all other creatures"
# excludes "Choose up to three Doctors"). WATCH -- one example.
_CHOOSE_VERB = re.compile(r'^\s*choose\b', re.I)


def _apply_and_split_merge(prev, cur):
    if not _AND_CONTINUATION.match(cur['text_span']):
        return
    if prev['type'] != cur['type']:
        return
    if not _fresh_mentions(cur['text_span']):
        for key in _FORWARD_KEYS:
            # Never copy target_type onto a clause whose own scope is SELF:
            # SELF's type is the source card's own, implicit and never
            # restated (the rule tag_extractor enforces at its own two call
            # sites). Construct a Cosmic Cube's "and put a plan counter on
            # THIS ENCHANTMENT" was inheriting ['CREATURE','TOKEN'] from a
            # sibling about creating tokens -- a different object entirely.
            if key == 'target_type' and cur['tags'].get('target_scope') == 'SELF':
                continue
            if key in prev['tags'] and key not in cur['tags']:
                cur['tags'][key] = prev['tags'][key]
    if 'duration' in cur['tags'] and 'duration' not in prev['tags']:
        prev['tags']['duration'] = cur['tags']['duration']


# ──────────────────────────────────────────────────────────────── driver

def link_card(clauses):
    """Mutates `clauses` (an ordered list of per-card clause dicts) in
    place, adding `resolved_reference` to tags and `resolved_binding` to
    values wherever this stage can resolve them, and filling in the
    narrow tag-inheritance cases documented above. See module docstring
    for the input contract and scope boundaries."""
    registry = {}          # category -> clause index
    last_touched = {'idx': None}
    primary_antecedent = {}  # clause index -> antecedent index (pass 1)

    for i, clause in enumerate(clauses):
        text = clause.get('text_span', '')
        tags = clause.setdefault('tags', {})

        if clause.get('type') == 'TRIGGER':
            registry['TRIGGER'] = i
        if clause.get('type') == 'CHOICE':
            registry['CHOICE'] = i
        if clause.get('type') == 'ACTION':
            registry['PROCEDURAL'] = i
        if tags.get('target_scope') == 'SELF':
            registry['SELF'] = i
        if _CHOOSE_VERB.match(text):
            registry['CHOOSE_ACTION'] = i

        if tags.get('excludes_prior') and 'CHOOSE_ACTION' in registry:
            tags['resolved_excludes'] = clauses[registry['CHOOSE_ACTION']]['text_span']

        if tags.get('refers_to_prior'):
            prev_type = clauses[i - 1].get('type') if i > 0 else None
            hits = _resolve_cues(text, registry, last_touched, prev_type, i - 1)
            # A clause can never be its own antecedent -- the SELF/CHOICE/
            # TRIGGER/PROCEDURAL registry entries are set for THIS clause
            # a few lines above, before its own cues are resolved, so a
            # clause whose own target_scope is SELF and which ALSO has a
            # bare "it" cue (no other clause to fall back to) would
            # otherwise resolve to itself (Obstinate Gargoyle: "This
            # creature has flying as long as IT's modified" -- the very
            # first clause on the card, nothing else for "it" to mean, but
            # registry['SELF'] already pointed at this same clause).
            hits = [h for h in hits if h != i]
            if hits:
                phrases = [clauses[h]['text_span'] for h in hits]
                tags['resolved_reference'] = phrases[0] if len(phrases) == 1 else phrases
                primary_antecedent[i] = hits[0]

                antecedent = clauses[hits[0]]
                _apply_damage_target_inheritance(clause, antecedent)
                _apply_bare_that_creature_type(clause)
                _apply_battlefield_land_type(clause, antecedent)

                for v in clause.get('values', []):
                    if v.get('kind') in ('COUNT', 'VAR', 'CHARACTERISTIC') and v.get('binding') is None:
                        v['resolved_binding'] = phrases[0]

        if i > 0:
            _apply_and_split_merge(clauses[i - 1], clause)

        fresh = _fresh_mentions(text)
        for cat in fresh:
            registry[cat] = i
        if fresh:
            last_touched['idx'] = i

    # Pass 2: distributive-chain propagation needs to know, for every
    # antecedent, how many siblings ultimately point at it -- not knowable
    # until pass 1 has resolved everyone's reference.
    chain_size = {}
    for antecedent_idx in primary_antecedent.values():
        chain_size[antecedent_idx] = chain_size.get(antecedent_idx, 0) + 1

    for i, antecedent_idx in primary_antecedent.items():
        clause = clauses[i]
        if clause.get('type') != 'ACTION':
            continue
        if clause['tags'].get('distributive'):
            continue
        # A clause whose OWN text asserts a singular selection ("Choose
        # ONE of them") directly contradicts per-instance/distributive
        # semantics, regardless of how plural its antecedent is --
        # Strongbox Raider: "exile the top two cards...Choose one of
        # them" was getting distributive=True purely from the antecedent
        # being plural ("two"), even though this clause explicitly picks
        # a single one, not one-per-instance.
        if _SINGULAR_SELECTION.search(clause.get('text_span', '')):
            continue
        if chain_size.get(antecedent_idx, 0) >= 2 and _antecedent_is_plural(clauses[antecedent_idx]):
            clause['tags']['distributive'] = True

    return clauses
