"""
CDL Parser v0.1
Deterministic rule-based translator from MTG card data to CDL v0.53.

Architecture:
  1. Header builder     — structured fields → CDL header
  2. Oracle tokenizer   — oracle text → ability lines
  3. Ability classifier — each line → ability type
  4. Pattern matchers   — per type, regex rules → CDL fragments
  5. CDL assembler      — header + fragments → output

Unmatched lines emit:  # SPEC_GAP: <original text>
Unknown keywords emit: # UNKNOWN_KEYWORD: <keyword text>
  (with --bootstrap flag, these trigger LLM expansion + store update)
"""

import re
import json
import os
import sys
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────────

PARSER_DIR = os.path.dirname(os.path.abspath(__file__))
KEYWORD_STORE_PATH = os.path.join(PARSER_DIR, "keyword_store.json")


# ── Keyword store ─────────────────────────────────────────────────────────────

class KeywordStore:
    def __init__(self, path: str):
        self.path = path
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self._meta = raw.pop("_meta", {})
        self.keywords = raw  # name → definition dict

    def match(self, text: str) -> Optional[str]:
        """Try to match text against all keyword patterns. Return CDL template or None."""
        for name, defn in self.keywords.items():
            pattern = defn["pattern"]
            m = re.match(pattern, text.strip(), re.IGNORECASE)
            if m:
                return self._render(defn["cdl_template"], m)
        return None

    def _render(self, template: str, match: re.Match) -> str:
        """Substitute captured groups into the CDL template."""
        result = template
        groups = match.groups()
        if groups:
            # Named substitutions by position: {cost} for first group, {n} for numeric.
            # The captured mana expression keeps its own braces intact — every
            # template already wraps {cost} in quotes (e.g. @WARD("{cost}")),
            # matching the spec's own canonical example @WARD("{2}"). A prior
            # version of this stripped the outer braces before substituting,
            # so every parameterized mana-cost keyword rendered without them
            # at all (@WARD("2") instead of @WARD("{2}")) — silent, since
            # nothing here validates against the spec's own documented format.
            g = groups[0] if groups else ""
            result = result.replace("{cost}", g)
            result = result.replace("{n}", g)
            # If multiple groups, handle them
            if len(groups) > 1:
                result = result.replace("{cost2}", groups[1])
        return result

    def add_entry(self, name: str, defn: dict):
        """Add a new keyword definition and persist."""
        self.keywords[name] = defn
        self._save()

    def _save(self):
        data = {"_meta": self._meta, **self.keywords}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ── Mana cost normalization ───────────────────────────────────────────────────

def normalize_mana(raw: str) -> str:
    """Ensure mana expression is quoted and well-formed."""
    s = raw.strip().strip('"')
    return f'"{s}"'


# ── Header builder ────────────────────────────────────────────────────────────

def build_header(card: dict) -> list[str]:
    lines = ["CARD {"]
    lines.append(f'  NAME: "{card["name"]}"')

    if "cost" in card:
        lines.append(f"  MANA_COST: {normalize_mana(card['cost'])}")

    if "supertypes" in card:
        types = parse_list_field(card["supertypes"])
        if types:
            lines.append(f"  SUPERTYPES: {format_string_list(types)}")

    if "types" in card:
        types = parse_list_field(card["types"])
        lines.append(f"  TYPES: {format_string_list(types)}")

    if "subtypes" in card:
        subtypes = parse_list_field(card["subtypes"])
        if subtypes:
            lines.append(f"  SUBTYPES: {format_string_list(subtypes)}")

    if "power" in card:
        lines.append(f"  POWER: {card['power']}")
    if "toughness" in card:
        lines.append(f"  TOUGHNESS: {card['toughness']}")
    if "loyalty" in card:
        lines.append(f"  LOYALTY: {card['loyalty']}")
    if "defense" in card:
        lines.append(f"  DEFENSE: {card['defense']}")

    return lines


def parse_list_field(val: str) -> list[str]:
    """Parse '[Creature, Artifact]' or 'Legendary' into a list of strings."""
    val = val.strip().strip("[]")
    return [v.strip() for v in val.split(",") if v.strip()]


def format_string_list(items: list[str]) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


# ── Oracle text tokenizer ─────────────────────────────────────────────────────

def tokenize_oracle(oracle: str) -> list[str]:
    """
    Split oracle text into discrete ability lines.
    Handles:
    - Newline-separated abilities
    - Strips reminder text in parentheses
    - Splits keyword + inline cost-modifier on the same paragraph
      e.g. "Disguise {5}{R}. This cost is reduced by {1} for each..."
           → ["Disguise {5}{R}", "This cost is reduced by {1} for each..."]
    """
    lines = []
    raw_lines = oracle.split("\n")

    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        clean = strip_reminder_text(raw)
        if not clean:
            continue
        # Split keyword + cost-modifier written as one paragraph (rule 113.2c edge case)
        for part in _split_keyword_modifier(clean):
            if part.strip():
                lines.append(part.strip())

    return lines


# Cost-modifier phrases that follow a keyword declaration on the same line
_COST_MODIFIER_SIGNALS = re.compile(
    r'\.\s+(This (?:ability |spell )?costs? (?:less|more|\{)|'
    r'This cost is reduced|'
    r'Activate this ability only|'
    r'This (?:ability|spell) costs? \{)',
    re.IGNORECASE
)


def _split_keyword_modifier(line: str) -> list[str]:
    """
    If a line is a keyword declaration followed by a cost-modifier sentence,
    split them into two separate lines.

    Signal: [keyword word(s)] [optional param]. [Cost modifier sentence.]

    Examples:
      "Disguise {5}{R}. This cost is reduced by {1} for each instant..."
      "Blitz costs you pay cost {1} less for each time..."
      "Equip {4}. This ability costs {3} less to activate if you're the monarch."
    """
    m = _COST_MODIFIER_SIGNALS.search(line)
    if not m:
        return [line]

    # Split at the ". " before the modifier signal
    split_pos = m.start() + 1  # position of the '.' separator
    keyword_part = line[:split_pos].strip().rstrip('.')
    modifier_part = line[split_pos:].strip().lstrip('. ')

    # Sanity check: keyword_part should look like a keyword declaration
    # (starts with a known keyword word)
    first_word = keyword_part.split()[0].lower() if keyword_part.split() else ''
    all_kw_starts = _PARAMETERIZED_KEYWORD_STARTS | _SIMPLE_KEYWORDS
    if first_word in all_kw_starts:
        return [keyword_part, modifier_part]

    # Not a keyword — don't split
    return [line]


def strip_reminder_text(line: str) -> str:
    """
    Remove reminder text (parenthetical clarifications) from a line.
    Reminder text typically: appears at end, starts with capital, is a full sentence.
    Keep parenthetical content that's part of mechanics (e.g., cost expressions).
    """
    # Don't strip if line IS entirely a reminder (standalone paren line)
    if re.match(r'^\([^)]+\)$', line.strip()):
        return ""  # discard pure reminder lines entirely

    # Strip trailing reminder text: " (This creature...)", " (You may...)"
    # But NOT cost-modifier parens like "({T}, Sacrifice...)"
    result = re.sub(
        r'\s*\([A-Z][^)]{10,}\)\s*$',
        '',
        line.strip()
    )
    return result.strip()


# ── Ability classifier ────────────────────────────────────────────────────────
#
# Implements the decision tree from ability_categories.md, derived directly from
# MTG Comprehensive Rules (April 2026), primarily rules 113.3, 602–606, 614–615.
#
# Priority order (strictly applied top to bottom):
#   LOYALTY → ENTERS_TAPPED → CANT_BE_COUNTERED → ADDL_COST → ALT_COST →
#   COST_REDUCTION → REPLACEMENT(ETB) → TRIGGERED → ACTIVATED/MANA →
#   KEYWORD → CHOICE_OPTION → SAGA_CHAPTER → REPLACEMENT → EQUIPPED_GRANT →
#   SPELL_EFFECT(if spell card) → STATIC

class AbilityType:
    # Stack-using abilities
    LOYALTY = "loyalty"  # rule 606 — +N/0/−N planeswalker cost
    TRIGGERED = "triggered"  # rule 603 — When/Whenever/At
    ACTIVATED = "activated"  # rule 602 — [Cost]: [Effect]
    MANA_ABILITY = "mana_ability"  # rule 605 — activated/triggered, no target, adds mana
    # Non-stack abilities
    KEYWORD = "keyword"  # rule 702 — named ability with rules expansion
    STATIC = "static"  # rule 604 — declarative, always true
    REPLACEMENT = "replacement"  # rule 614 — watches for event, replaces it
    EQUIPPED_GRANT = "equipped_grant"  # sub-type of static for equip/aura grants
    # Spell-only
    SPELL_EFFECT = "spell_effect"  # rule 113.3a — instant/sorcery resolution text
    CHOICE_OPTION = "choice_option"  # bullet • option within a modal ability
    # Card-level modifiers (not stack abilities — map to header fields)
    ADDL_COST = "addl_cost"  # "As an additional cost to cast..."
    ALT_COST = "alt_cost"  # "rather than pay this spell's mana cost"
    COST_REDUCTION = "cost_reduction"  # "This spell costs {N} less to cast"
    CANT_BE_COUNTERED = "cant_be_countered"
    ENTERS_TAPPED = "enters_tapped"
    CONSTRUCTED_OVERRIDE = "constructed_override"  # "A deck can have any number of cards named X"
    SAGA_CHAPTER = "saga_chapter"  # "N+ | [text]" from our card data encoding
    UNKNOWN = "unknown"


# ── Keyword name registry (rule 702) ─────────────────────────────────────────
#
# Simple keywords: the word alone is the complete ability text (after stripping reminder)
# Parameterized keywords: followed by a cost, number, or type restriction
# Named-prefix triggers: keyword-like prefix before " — Whenever/When/At"
#   These are TRIGGERED abilities, not keywords — listed here only to detect and handle

_SIMPLE_KEYWORDS = {
    "flying", "trample", "vigilance", "haste", "lifelink", "deathtouch",
    "reach", "hexproof", "shroud", "indestructible", "menace", "flash",
    "defender", "prowess", "horsemanship", "cascade", "exert", "exalted",
    "convoke", "delve", "persist", "undying", "infect", "wither",
    "shadow", "fear", "intimidate", "phasing", "islandwalk", "swampwalk",
    "forestwalk", "mountainwalk", "plainswalk", "changeling", "devoid",
    "proliferate", "riot", "first strike", "double strike", "partner",
    "companion",  # companion has a condition but the word alone on a line is a keyword
    "skulk", "melee", "training", "myriad", "improvise", "dethrone",
    "evolve", "demonstrate", "extort", "decayed", "unleash",
}

_PARAMETERIZED_KEYWORD_STARTS = {
    # keyword word → True (followed by cost/number/type)
    "ward", "equip", "crew", "cycling", "swampcycling", "forestcycling",
    "plainscycling", "islandcycling", "mountaincycling", "flashback", "overload",
    "kicker", "offspring", "surveil", "toxic", "bushido", "mobilize",
    "harmonize", "bestow", "affinity", "annihilator", "echo", "emerge",
    "escape", "adapt", "protection", "flanking", "banding", "freerunning",
    "blitz", "ninjutsu", "commander ninjutsu", "channel", "evoke", "morph",
    "megamorph", "disguise", "dash", "buyback", "madness", "miracle", "suspend",
    "replicate", "transmute", "dredge", "graft", "modular", "amplify",
    "bloodthirst", "provoke", "rampage", "landwalk",
    "afflict", "devour", "afterlife", "prowl",
    # Station is a keyword that expands to an activated ability
    "station",
    # Eminence appears before "—" like a named trigger but it IS the keyword name
    # (handled separately in named-prefix detection)
}

# Named-prefix triggers: these look like keywords but are actually category labels
# for triggered abilities. The trigger word (When/Whenever/At) follows the " — ".
_NAMED_TRIGGER_PREFIXES = {
    "landfall", "eminence", "alliance", "metalcraft", "threshold",
    "morbid", "heroic", "raid", "revolt", "ferocious", "formidable",
    "delirium", "battalion", "tempting offer", "cheer", "trance",
}

# Named-prefix STATICS: look like triggers but are "As long as" conditions
_NAMED_STATIC_PREFIXES = {
    "threshold",  # "Threshold — As long as..."
}


def classify_line(line: str, card: dict) -> str:
    """
    Classify a single oracle text line per CR rules 113.3, 602–606, 614.
    Returns an AbilityType constant.

    Decision tree (strict top-to-bottom priority):
    1.  Loyalty cost           rule 606
    2.  Enters tapped          replacement, maps to header
    3.  Can't be countered     static, maps to header
    4.  Additional cost        rule 601.2b, maps to header
    5.  Alternative cost       rule 601.2b, maps to header
    6.  Cost reduction         rule 601.2e, maps to header
    7.  ETB replacement        rule 614.1c — "As this [permanent] enters"
    8.  Triggered ability      rule 603 — When/Whenever/At (incl. named-prefix triggers)
    9.  Activated/Mana         rule 602/605 — [Cost]: [Effect]
    10. Keyword ability        rule 702 — named ability word(s)
    11. Choice option          bullet • sub-option
    12. Saga chapter           N+ | [text] encoding
    13. Replacement effect     rule 614 — "instead", "skip", "if X would"
    14. Equipped/enchanted grant
    15. Spell effect           rule 113.3a — only if card is instant/sorcery
    16. Static ability         rule 604 — default
    """
    ll = line.lower().strip()
    card_types = card.get("types", "").lower()
    is_spell_card = any(t in card_types for t in ["instant", "sorcery"])

    # ── 1. Loyalty ability (rule 606) ─────────────────────────────────────────
    # Cost is +N, 0, or −N (unicode minus or ascii minus)
    if re.match(r'^[+\u2212\-]\d+\s*:|^0\s*:', ll):
        return AbilityType.LOYALTY

    # ── 2. Enters tapped (replacement, header-level) ──────────────────────────
    # "This [type] enters tapped" or bare "This land enters tapped with..."
    if re.match(r'^this \w+(?: \w+)? enters(?: the battlefield)? tapped', ll):
        return AbilityType.ENTERS_TAPPED

    # ── 3. Can't be countered (static, header-level) ──────────────────────────
    if ll.startswith("this spell can't be countered"):
        return AbilityType.CANT_BE_COUNTERED

    # ── 4. Additional cost (rule 601.2b, header-level) ────────────────────────
    if ll.startswith("as an additional cost to cast"):
        return AbilityType.ADDL_COST

    # ── 5. Alternative cost (rule 601.2b, header-level) ───────────────────────
    if ("rather than pay this spell" in ll or
            "without paying its mana cost" in ll or
            "without paying their mana cost" in ll):
        return AbilityType.ALT_COST

    # ── 6. Cost reduction (rule 601.2e, header-level) ─────────────────────────
    if re.match(r'^this spell costs? .+ less to cast', ll):
        return AbilityType.COST_REDUCTION

    # ── 6a. Deck-construction override (Section 2.5, header-level) ───────────
    if re.match(r'^a deck can have any number of cards named ', ll):
        return AbilityType.CONSTRUCTED_OVERRIDE

    # ── 7. ETB replacement effect (rule 614.1c) ───────────────────────────────
    # "As this [permanent] enters" / "As it enters" / "As [cardname] is turned
    # face up" / "As <own name> enters" (self-reference by proper name rather
    # than "this", e.g. Morophon: "As Morophon enters, choose ...")
    card_name_l = card.get("name", "").lower()
    short_name_l = card_name_l.split(",", 1)[0].strip() if "," in card_name_l else card_name_l
    self_ref = r'(?:this|it' + (f'|{re.escape(card_name_l)}|{re.escape(short_name_l)}' if card_name_l else '') + ')'
    if re.match(r'^as ' + self_ref + r'\b', ll) and re.search(r'\benters\b|\bturned face up\b|\bbecomes attached\b', ll):
        return AbilityType.REPLACEMENT  # ETB replacement — maps to REPLACE { AS_ETB }

    # ── 8. Triggered ability (rule 603) ───────────────────────────────────────
    # Standard trigger words
    if re.match(r'^when(?:ever)?\s|^at\s', ll):
        return AbilityType.TRIGGERED

    # Named-prefix triggers: "Landfall — Whenever...", "Eminence — Whenever..."
    # Pattern: [Word(s)] — When/Whenever/At ...
    named_prefix_match = re.match(r"^([a-z][a-z\s',]*?)\s*[—\-]+\s*(when(?:ever)?\s|at\s)", ll)
    if named_prefix_match:
        prefix = named_prefix_match.group(1).strip()
        following = named_prefix_match.group(2).strip()
        # If what follows the dash is a trigger word → TRIGGERED
        if re.match(r'^when(?:ever)?|^at\b', following):
            return AbilityType.TRIGGERED
        # If what follows is "as long as" → STATIC (Threshold-style)
        # (falls through to static classification below)

    # Named-prefix statics: "Threshold — As long as..."
    threshold_match = re.match(r'^([a-z][a-z\s\']*?)\s*[—\-]+\s*as long as\b', ll)
    if threshold_match:
        return AbilityType.STATIC

    # ── 9. Activated / Mana ability (rules 602, 605) ──────────────────────────
    colon_pos = _find_cost_colon(line)
    if colon_pos is not None:
        cost_text = line[:colon_pos].strip()
        effect_text = line[colon_pos + 1:].strip()
        if _is_mana_ability(cost_text, effect_text):
            return AbilityType.MANA_ABILITY
        return AbilityType.ACTIVATED

    # ── 10. Keyword ability (rule 702) ─────────────────────────────────────────
    # Strip reminder text, then check if what remains is purely keyword words
    clean = _strip_reminder(line).rstrip('.')
    if _is_keyword_line(clean):
        return AbilityType.KEYWORD

    # Special: keyword on spell card (Flashback, Overload, etc.)
    if is_spell_card and _first_word(ll) in _PARAMETERIZED_KEYWORD_STARTS:
        return AbilityType.KEYWORD

    # ── 11. Choice option (bullet point) ──────────────────────────────────────
    if line.strip().startswith('•'):
        return AbilityType.CHOICE_OPTION

    # ── 12. Saga chapter (our encoding: "N+ | [text]") ───────────────────────
    if re.match(r'^\d+\+\s*\|', line.strip()):
        return AbilityType.SAGA_CHAPTER

    # ── 13. Replacement effect (rule 614) ─────────────────────────────────────
    # 614.1a: "instead"
    if re.search(r'\binstead\b', ll):
        return AbilityType.REPLACEMENT
    # 614.1b: "skip"
    if re.match(r'^skip\b', ll):
        return AbilityType.REPLACEMENT
    # 614.1d: continuous "X enters tapped" / "X enters with"
    # The "this ... enters tapped" shape is excluded here because step 2
    # (ENTERS_TAPPED, header-level) already claims it earlier in this same
    # function — excluding it again at this step is redundant for that verb,
    # but must not also swallow "this ... enters with a counter" (Festercreep),
    # which no earlier step handles.
    if re.search(r'\benters(?: the battlefield)? with\b', ll):
        return AbilityType.REPLACEMENT
    if re.search(r'\benters(?: the battlefield)? tapped\b', ll) and not ll.startswith("this"):
        return AbilityType.REPLACEMENT
    # "if [subject] would [event]" pattern (without "instead" — partial replacement)
    if re.match(r'^if .+\bwould\b', ll) and not re.match(r'^if you would (gain|lose) life', ll):
        # Check it's really replacement not just a conditional static
        if re.search(r'\binstead\b|\btwice\b|\bdouble\b', ll):
            return AbilityType.REPLACEMENT

    # ── 14. Equipped/enchanted creature grant ────────────────────────────────
    if re.match(r'^equipped creature\b|^enchanted creature\b|^enchanted \w+ gets?\b|^enchanted \w+ has\b', ll):
        return AbilityType.EQUIPPED_GRANT

    # ── 15. Spell effect (rule 113.3a) ────────────────────────────────────────
    # Any text on an instant/sorcery that's not one of the above is a spell ability
    if is_spell_card:
        return AbilityType.SPELL_EFFECT

    # ── 16. Static ability (rule 604) — default ──────────────────────────────
    return AbilityType.STATIC


# ── Classifier helpers ────────────────────────────────────────────────────────

def _find_cost_colon(line: str) -> Optional[int]:
    """
    Find the position of the colon that separates cost from effect in an
    activated ability. Returns None if no such colon exists.

    Rules:
    - Cost appears before the colon
    - Cost tokens: mana symbols {X}, {T}, {Q}, numbers, 'Sacrifice', 'Discard',
      'Remove', 'Pay', 'Tap', 'Exile', named ability keyword + cost
    - Colons inside reminder text (parentheses) don't count
    - Colons in card-name strings don't count
    - A colon in the middle of effect text (e.g. "choose one —") doesn't count
    """
    ll = line.lower()

    # Skip lines that are clearly not activated abilities
    # (pure reminder text, bullet options, etc.)
    if line.strip().startswith('(') and line.strip().endswith(')'):
        return None
    if line.strip().startswith('•'):
        return None

    # Find first colon outside parentheses
    depth = 0
    first_colon = None
    for i, ch in enumerate(line):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ':' and depth == 0:
            first_colon = i
            break

    if first_colon is None:
        return None

    # Check that the text before the colon looks like a cost
    cost_candidate = line[:first_colon].strip()
    if _looks_like_cost(cost_candidate):
        return first_colon

    return None


_COST_LEADING_WORDS = {
    "sacrifice", "discard", "remove", "pay", "tap", "untap", "exile",
    "return", "put", "reveal",
}

_NAMED_ABILITY_PREFIXES = {
    "channel", "commander ninjutsu", "ninjutsu", "cycling", "evoke",
    "chapter master", "three autostubs", "in you all things are possible",
    "the minstrel's ballad",
}


def _looks_like_cost(text: str) -> bool:
    """
    Return True if text looks like an activated ability cost.
    A cost contains at least one of: mana symbol, {T}, {Q}, a cost keyword,
    or is a named ability keyword followed by a cost.
    """
    if not text:
        return False
    tl = text.lower().strip()

    # Mana/tap/untap symbols
    if re.search(r'\{[^}]+\}', text):
        return True

    # Tap N untapped [X] you control
    if re.match(r'^tap \w', tl):
        return True

    # Leading cost verb: "Sacrifice this creature", "Discard a card", etc.
    first_word = tl.split()[0] if tl.split() else ''
    if first_word in _COST_LEADING_WORDS:
        return True

    # Named ability keyword costs: "Channel — {1}{G}, Discard..."
    # "Commander ninjutsu {U}{B}"
    for prefix in _NAMED_ABILITY_PREFIXES:
        if tl.startswith(prefix):
            return True

    # Loyalty cost form accidentally reaching here (shouldn't, but guard)
    if re.match(r'^[+\u2212\-]\d+$|^0$', tl):
        return False

    # Numeric cost alone: "3" or "1" — only valid if followed by comma or is sole cost
    if re.match(r'^\d+$', tl):
        return True

    return False


def _is_mana_ability(cost_text: str, effect_text: str) -> bool:
    """
    Per CR 605.1a, an activated ability is a mana ability if ALL of:
    1. It doesn't require a target
    2. It could add mana when it resolves
    3. It is not a loyalty ability (already filtered before reaching here)

    The 'could add mana' test: does the effect text contain an Add instruction?
    The 'no target' test: does the cost NOT contain 'target'?

    Key insight from the rules: side effects (damage to self, conditional sacrifice,
    spending restrictions) do NOT disqualify a mana ability. Only having a target
    in the cost disqualifies it.
    """
    # No target anywhere in the cost
    if 'target' in cost_text.lower():
        return False

    # Effect must contain "Add {" or "add one mana" or "add [N] mana"
    el = effect_text.lower()

    # Split multi-sentence effects; check if ANY sentence adds mana
    # (Talisman pattern: "Add {U} or {B}. This artifact deals 1 damage.")
    sentences = re.split(r'\.\s+', el)
    for sentence in sentences:
        if re.match(r'^add\b', sentence.strip()):
            return True
        # "Add an amount of {G} equal to..."
        if re.match(r'^add an amount', sentence.strip()):
            return True

    # "add X mana" patterns
    if re.search(r'\badd\s+(?:one|two|three|\d+)\s+mana\b', el):
        return True
    if re.search(r'\badd\s+\{', el):
        return True

    # Remove-counter mana abilities (Peat Bog pattern):
    # "{T}, Remove a depletion counter: Add {B}{B}."
    # The remove-counter is a cost, not a target — still qualifies
    if re.search(r'\badd\b', el) and re.search(r'\{[WUBRG0-9C/]+\}', effect_text):
        return True

    return False


def _strip_reminder(line: str) -> str:
    """Strip parenthetical reminder text from a line, keeping the ability text."""
    # Remove trailing reminder: " (This creature can't be blocked...)" — also
    # matches reminders that open with a mana cost instead of a capital
    # letter, e.g. "({U}{B}, Return an unblocked attacker ...)" (Commander
    # ninjutsu) or "({2}, Discard this card: ...)" (Swampcycling).
    result = re.sub(r'\s*\((?:[A-Z]|\{)[^)]{8,}\)\s*$', '', line.strip())
    # Remove standalone reminder-only parens anywhere (rare)
    result = re.sub(r'\s*\((?:[A-Z]|\{)[^)]{8,}\)', '', result)
    return result.strip()


def _is_keyword_line(clean: str) -> bool:
    """
    Return True if `clean` (reminder-stripped) consists entirely of keyword
    ability declarations — i.e., comma-separated keyword words/phrases.
    """
    if not clean:
        return False
    parts = [p.strip().lower() for p in clean.split(',') if p.strip()]
    if not parts:
        return False
    # "Protection from X[, from Y][, and from Z]" is a single keyword with a
    # compound, comma-separated source list — fold the "from Y"/"and from Z"
    # continuation fragments back into it before the per-fragment check
    # below, which would otherwise see them as their own (invalid) keyword
    # starts and reject the whole line.
    parts = _fold_protection_parts(parts)
    return all(_is_single_keyword(p) for p in parts)


def _is_single_keyword(word: str) -> bool:
    """
    Check if a single word/phrase is a keyword ability declaration.
    The full text (after reminder strip) must be:
      - the keyword word alone, OR
      - the keyword word followed by a cost/number parameter only
    It must NOT be the keyword word followed by sentence text (that's a static).
    """
    word = word.lower().strip().rstrip('.')
    if not word:
        return True

    # Exact match in simple keywords
    if word in _SIMPLE_KEYWORDS:
        return True

    # Multi-word simple keywords
    if word.startswith("first strike") or word.startswith("double strike"):
        # Allow "first strike" alone or "first strike, ..." (handled by comma split)
        remainder = word[len("first strike"):].strip()
        return not remainder or remainder.startswith(',')
    if word.startswith("double strike"):
        remainder = word[len("double strike"):].strip()
        return not remainder or remainder.startswith(',')
    if word.startswith("commander ninjutsu"):
        remainder = word[len("commander ninjutsu"):].strip()
        return _is_cost_or_empty(remainder)

    # Parameterized keyword: keyword_word followed by cost/number only
    first = word.split()[0]
    if first in _PARAMETERIZED_KEYWORD_STARTS:
        remainder = word[len(first):].strip()
        # "protection from X[, from Y][, and from Z]" — its own bounded
        # parameter grammar, exempted from the generic 4-word cutoff below
        # ("from white and from black" is 5 words but still just a parameter,
        # not a sentence).
        if first == "protection" and remainder.startswith("from "):
            return True
        # Valid: empty, or a mana cost like "{3}{R}", or a number like "2"
        # Invalid: a sentence like "costs you pay {1} less for each..."
        return _is_cost_or_empty(remainder)

    return False


def _is_cost_or_empty(text: str) -> bool:
    """
    Return True if text is empty or looks like a keyword parameter:
    a mana cost expression, a number, or a simple type restriction.
    Return False if it looks like sentence/clause text.
    """
    text = text.strip().rstrip('.')
    if not text:
        return True
    # Mana cost: starts with { or is purely mana symbols
    if re.match(r'^\{[^}]+\}', text):
        return True
    # Pure number
    if re.match(r'^\d+$', text):
        return True
    # Simple type restriction: "for artifacts", "for creature spells" — short phrase
    # but NOT a full sentence with verbs
    # Heuristic: contains a verb word that makes it a sentence
    sentence_verbs = {'costs', 'pays', 'less', 'more', 'reduce', 'increases',
                      'you', 'each', 'control', 'have', 'may', 'can'}
    first_word = text.split()[0].lower().rstrip(',')
    if first_word in sentence_verbs:
        return False
    # Short type qualifier: "for artifacts", "for enchantments" — up to 4 words, no verb
    words = text.split()
    if len(words) <= 4 and not any(w.lower() in sentence_verbs for w in words):
        return True
    return False


def _first_word(ll: str) -> str:
    """Return the first word of a lowercased line."""
    parts = ll.split()
    return parts[0] if parts else ""


# ── Pattern matchers ──────────────────────────────────────────────────────────

class PatternMatcher:
    """
    Ordered list of (regex_pattern, cdl_emitter_fn) pairs per ability type.
    Most specific patterns first.
    """

    def __init__(self, keyword_store: KeywordStore):
        self.kw_store = keyword_store

    # ── Keywords ──────────────────────────────────────────────────────────────

    def match_keyword_line(self, line: str) -> list[str]:
        """Handle a keyword line (may be comma- or ';'-separated list)."""
        clean = re.sub(r'\s*\([^)]+\)', '', line).strip().rstrip('.')

        # "Protection from X[, from Y][, and from Z]" is a single keyword
        # with a compound source list, not multiple distinct keywords, even
        # when it shares a line with other keywords ("Flying, protection
        # from black") — _fold_protection_parts re-merges the "from Y"/"and
        # from Z" continuation fragments the generic comma-split below would
        # otherwise treat as their own (invalid) keyword starts.
        results = []
        for segment in clean.split(';'):
            raw_parts = [p.strip() for p in segment.split(',') if p.strip()]
            for part in _fold_protection_parts(raw_parts):
                prot = _match_protection_keyword(part)
                if prot is not None:
                    results.extend(prot)
                    continue
                cdl = self.kw_store.match(part)
                if cdl:
                    results.append(f"  {cdl}")
                else:
                    results.append(f"  # UNKNOWN_KEYWORD: {part}")
        return results

    # ── Mana abilities ────────────────────────────────────────────────────────
    #
    # Structure: MANA_ABILITY { COST: ... ADD_MANA: ... EFFECT?: [...] }
    #
    # Cost shapes (6): TAP, MANA+TAP, MANA+TAP+SAC, TAP+REMOVE, TAP_CREATURES, ZERO
    # Output shapes (13): see _parse_mana_output
    # Side effects handled inline — don't disqualify per CR 605.1a

    def match_mana_ability(self, line: str, card: dict) -> list[str]:
        # Step 1: split cost from effect at the cost colon
        colon = _find_cost_colon(line)
        if colon is None:
            return [f"  # SPEC_GAP: mana ability no colon: {line}"]

        cost_raw = line[:colon].strip()
        effect_raw = line[colon + 1:].strip()

        # Step 2: parse cost shape
        cost_cdl = _parse_mana_cost(cost_raw)

        # Step 3: parse output shape + side effects
        add_mana, side_effects, limit = _parse_mana_output(effect_raw)

        # Step 4: assemble
        result = ["  MANA_ABILITY {"]

        if limit:
            result.append(f"    DURING: {limit}")

        if len(cost_cdl) == 1:
            result.append(f"    COST: {cost_cdl[0]}")
        else:
            result.append("    COST {")
            result += [f"      {c}" for c in cost_cdl]
            result.append("    }")

        result.append(f"    ADD_MANA: {add_mana}")

        if side_effects:
            result.append("    EFFECT: [")
            for se in side_effects:
                result.append(f"      {se}")
            result.append("    ]")

        result.append("  }")
        return result

    # ── Activated abilities ───────────────────────────────────────────────────

    def match_activated(self, line: str, card: dict) -> list[str]:
        """Parse cost: effect pattern."""
        # Strip a named-category prefix ("Channel —", "Chapter Master —",
        # ...) — a flavor/category label, not part of the cost itself.
        # Mirrors match_triggered's own prefix stripping; without it
        # _parse_cost sees "Channel — {1}{G}" as a whole cost token and
        # can't recognize it as a plain mana cost.
        line = re.sub(r"^[A-Z][\w', ]*? [—-]\s*(?=\{|[Ss]acrifice\b|[Tt]ap\b|[Dd]iscard\b)", "", line)
        # Split on first colon that follows the cost
        cost_raw, _, effect_raw = line.partition(":")
        cost_raw = cost_raw.strip()
        effect_raw = effect_raw.strip()

        if not effect_raw:
            return [f"  # SPEC_GAP: activated with no effect: {line}"]

        cost_lines = _parse_cost(cost_raw)
        effect_lines = self.match_effect(effect_raw, card)

        during = _infer_during(effect_raw, line)

        result = ["  ACTIVATED {"]
        if during:
            result.append(f"    DURING: {during}")
        if len(cost_lines) == 1 and cost_lines[0].startswith("MANA:") or cost_lines[0] == "TAP":
            result.append(f"    COST: {cost_lines[0]}")
        else:
            result.append("    COST {")
            result += [f"      {c}" for c in cost_lines]
            result.append("    }")
        result.append("    EFFECT: [")
        result += [f"      {e}" for e in effect_lines]
        result.append("    ]")
        result.append("  }")
        return result

    # ── Triggered abilities ───────────────────────────────────────────────────

    def match_triggered(self, line: str, card: dict) -> list[str]:
        """Parse trigger condition and effect."""
        # Strip a named-category prefix ("Eminence -", "Landfall -",
        # "Alliance -", "Cheer -", "Trance -", "Station -", ...). Per
        # ability_categories.md Sec. "Named/Category Triggers": this is a
        # flavor/category label, not a separate ability, whenever the text
        # after the dash begins with When/Whenever/At.
        m = re.match(r"^[A-Z][\w', ]*? [—-]\s*((?:when(?:ever)?|at)\b.*)$", line, re.IGNORECASE)
        if m:
            line = m.group(1)
            line = line[0].upper() + line[1:] if line else line

        ll = line.lower()
        card_name = card.get("name", "")

        # Split on first comma after the trigger condition
        # "Whenever X, [condition,] effect"
        trigger_text, _, effect_text = _split_trigger(line)
        if not effect_text:
            return [f"  # SPEC_GAP: triggered with no effect: {line}"]

        # "...during each opponent's turn"/"...during your turn" — a DURING
        # scope (Section 19.2) baked into the trigger sentence itself
        # rather than a separate "Activate only..." clause (that form is
        # already handled for ACTIVATED via _infer_during). Strip it before
        # event-matching so the underlying event pattern still recognizes
        # the rest of the sentence (Alela: "cast your first spell during
        # each opponent's turn" — the ordinal-per-turn pattern otherwise
        # expects a bare "each turn" suffix, not this turn-scope phrasing).
        during = None
        dm = re.search(r"\s+during (each opponent'?s|your) turn$", trigger_text, re.IGNORECASE)
        if dm:
            during = "OPPONENTS_TURN" if dm.group(1).lower().startswith("each opponent") else "YOUR_TURN"
            trigger_text = trigger_text[:dm.start()].strip()

        # "...while <name>/this creature is attacking" — a CONDITION baked
        # into the trigger sentence itself; `attacking` is already a
        # documented boolean permanent property (Section 13), so SELF.
        # attacking composes directly (Fire Lord Azula).
        trigger_cond = None
        am = re.search(
            r"\s+while (?:this creature|" + re.escape(card_name) + r") is attacking$",
            trigger_text, re.IGNORECASE)
        if am:
            trigger_cond = "SELF.attacking"
            trigger_text = trigger_text[:am.start()].strip()

        # "...attacks and isn't blocked" — same idea, using the
        # $obj.blocked_by collection accessor (Appendix D.2, v0.69):
        # empty means unblocked, per the same collection-truthiness
        # convention already established for counter presence.
        bm = re.search(r"\s+and isn'?t blocked$", trigger_text, re.IGNORECASE)
        if bm:
            trigger_cond = "COUNT(SELF.blocked_by) EQ 0"
            trigger_text = trigger_text[:bm.start()].strip()

        event_block = self._build_event_block(trigger_text, card)
        condition = _extract_condition(effect_text, card_name)
        actual_effect = _strip_condition(effect_text) if condition else effect_text
        if trigger_cond:
            condition = f"AND({trigger_cond}, {condition})" if condition else trigger_cond

        pay_or_effect = self._match_pay_or_effect(actual_effect, card)
        if pay_or_effect is not None:
            event_block = _bind_event_block(event_block, "$event")
            may, effect_lines = False, pay_or_effect
        else:
            may, actual_effect = _extract_may(actual_effect)
            effect_lines = self.match_effect(actual_effect, card)
            # "defending player" resolves to $event.defending (WHEN_ATTACKS
            # exposes it), "that many" resolves to $event.amount — bind the
            # event only when actually referenced, to avoid adding a no-op
            # "AS $event" to every other TRIGGERED block.
            aef = actual_effect.lower()
            if ("defending player" in aef or "that many" in aef or "that player" in aef
                    or "copy of it" in aef or "that permanent" in aef or "copy that spell" in aef):
                event_block = _bind_event_block(event_block, "$event")

        result = ["  TRIGGERED {"]
        if during:
            result.append(f"    DURING: {during}")
        result += [f"    {e}" for e in event_block]
        if condition:
            result.append(f"    CONDITION: {condition}")
        result.append("    EFFECT: [")
        if may:
            effect_lines = _inject_may_true(effect_lines)
        result += [f"      {e}" for e in effect_lines]
        result.append("    ]")
        result.append("  }")
        return result

    def _match_pay_or_effect(self, text: str, card: dict) -> Optional[list[str]]:
        """
        Match the "[you may] <effect> unless that player pays <cost>" idiom
        (Rhystic Study, Esper Sentinel) and its "that player may pay <cost>.
        If the player doesn't, <effect>" variant (Smothering Tithe).

        Both describe an opponent choosing whether to pay a cost to prevent
        an effect — encoded as a two-option CHOICE (one costed OPTION with
        no EFFECT, one uncosted OPTION with the fallback EFFECT), per the
        "Optional cost pattern" in Section 10 and the v0.51 changelog entry.
        Returns None if the text doesn't match either form.
        """
        el = text.strip().rstrip('.')
        ll = el.lower()

        # "[you may] sacrifice a/another <type>[ you control]. If you do,
        # <effect>" — same "Optional cost pattern" shape as the mana-cost
        # branch below, with a SACRIFICE cost (COST schema, Section 4)
        # instead of MANA. Binds the sacrificed object as $sacrificed so the
        # inner effect can reference its properties (e.g. "that creature's
        # power") — substituted from "that creature"/"that permanent" before
        # handing the inner text to match_effect.
        m = re.match(r"^(?:you may )?sacrifice (another|a|an) (\w+)(?: you control)?\.\s*if you do,\s*(.+)$", ll)
        if m:
            article, type_word, sac_effect = m.groups()
            excl = " EXCEPT: SELF" if article == "another" else ""
            type_filter = "CONTROLLER: YOU" if type_word == "permanent" else f'TYPE: "{type_word.title()}" CONTROLLER: YOU'
            sac_effect = re.sub(r'\bthat (?:creature|permanent)\b', '$sacrificed', sac_effect)
            effect_lines = self.match_effect(sac_effect, card)
            return [
                "CHOICE {",
                "  TIMING: RESOLUTION",
                "  PLAYER: YOU",
                "  COUNT: RANGE 0..1",
                "  OPTION {",
                f"    COST: SACRIFICE: CHOOSE {{ FROM: BATTLEFIELD COUNT: 1 FILTER {{ {type_filter}{excl} }} }} AS $sacrificed",
                "    EFFECT: [",
            ] + [f"      {e}" for e in effect_lines] + ["    ]", "  }", "}"]

        # "[you may] pay <cost>. If/When you do, <effect>" — the Section 10
        # "Optional cost pattern" worked example: a single OPTION carrying
        # both COST and EFFECT, COUNT: RANGE 0..1 (distinct from the
        # two-OPTION "unless"/"doesn't" shape below, where the cost and the
        # fallback effect belong to different players/branches).
        m = re.match(r"^(?:you may )?pay ((?:\{[^}]+\})+)\.\s*(?:if|when) you do,\s*(.+)$", ll)
        if m:
            pay_cost = el[m.start(1):m.end(1)]
            pay_effect = el[m.start(2):m.end(2)].strip()
            effect_lines = self.match_effect(pay_effect, card)
            return [
                "CHOICE {",
                "  TIMING: RESOLUTION",
                "  PLAYER: YOU",
                "  COUNT: RANGE 0..1",
                "  OPTION {",
                f'    COST: MANA: "{pay_cost}"',
                "    EFFECT: [",
            ] + [f"      {e}" for e in effect_lines] + ["    ]", "  }", "}"]

        cost = None
        inner_effect = None

        m = re.match(r"^(?:you may )?(.+?) unless that player pays \{x\}, where x is this creature's power$", ll)
        if m:
            inner_effect = el[m.start(1):m.end(1)].strip()
            cost = "SELF.power"

        if cost is None:
            m = re.match(r'^(?:you may )?(.+?) unless that player pays \{(\d+)\}$', ll)
            if m:
                inner_effect = el[m.start(1):m.end(1)].strip()
                cost = f'"{{{m.group(2)}}}"'

        if cost is None:
            m = re.match(r"^that player may pay \{(\d+)\}\.\s*if (?:the|that) player doesn't,\s*(.+)$", ll)
            if m:
                cost = f'"{{{m.group(1)}}}"'
                inner_effect = el[m.start(2):m.end(2)].strip()

        if cost is None:
            return None

        if inner_effect.lower().startswith("you "):
            inner_effect = inner_effect[len("you "):].strip()
        effect_lines = self.match_effect(inner_effect, card)
        lines = [
            "CHOICE {",
            "  PLAYER: $event.source.controller",
            "  TIMING: RESOLUTION",
            "  COUNT: 1",
            f"  OPTION {{ COST: MANA: {cost} }}",
            "  OPTION {",
            "    EFFECT: [",
        ]
        lines += [f"      {e}" for e in effect_lines]
        lines += ["    ]", "  }", "}"]
        return lines

    def _build_event_block(self, trigger_text: str, card: dict) -> list[str]:
        """Convert trigger condition text to CDL event block."""
        ll = trigger_text.lower().strip()
        card_name = card.get("name", "").lower()
        # Legendary creatures self-reference by a short "nickname" in oracle
        # text (e.g. "Yorion, Sky Nomad" -> "Yorion") rather than their full
        # printed name. Match either form for self-referencing triggers.
        if "," in card_name:
            short_name = card_name.split(",", 1)[0].strip()
        else:
            short_name = card_name.split()[0] if card_name.split() else card_name
        name_pattern = re.escape(card_name) if short_name == card_name else \
            f'(?:{re.escape(card_name)}|{re.escape(short_name)})'
        # Self-reference by card type word ("When this Aura enters") is as
        # common as self-reference by name — oracle text uses whichever
        # type word matches the printed card. Shared by every "this
        # <type>" event branch below so the word list stays in one place.
        self_type_words = ("creature|permanent|artifact|enchantment|equipment|aura|land|"
                            "vehicle|spacecraft|class|case")

        # ── Compound "X or Y" trigger (Section 5, v0.68's EVENTS: [...]) ────
        # "Whenever this creature enters or attacks, ..." is one ability
        # firing off either of two event types with the same EFFECT, not
        # two abilities that happen to share text. The right-hand clause
        # typically elides the repeated subject ("enters or attacks" =
        # "enters or [this creature] attacks") — reconstruct it, then
        # recurse each half through the normal per-event matching below.
        # Declines (falls through to the single-clause path, and
        # ultimately an honest whole-sentence gap) unless BOTH halves
        # resolve to a real event and the right-hand side doesn't name a
        # different subject of its own ("...or the creature it haunts
        # dies" — Haunt's cross-reference, a different composition
        # entirely, not attempted).
        or_m = re.match(
            r'^(when(?:ever)?)\s+((?:this (?:' + self_type_words + r')|' + name_pattern + r'))\s+'
            r'(.+?)\s+or\s+(.+)$', ll)
        if or_m:
            lead_word, subject, verb1, tail = or_m.groups()
            tail_has_own_subject = re.match(
                r'^(?:this |a |an |another |the |' + name_pattern + r')\b', tail)
            if not tail_has_own_subject:
                clause1 = f"{lead_word} {subject} {verb1}"
                clause2 = f"{lead_word} {subject} {tail}"
                event1 = self._build_event_block(clause1, card)
                event2 = self._build_event_block(clause2, card)
                if (not any('# SPEC_GAP' in l for l in event1)
                        and not any('# SPEC_GAP' in l for l in event2)):
                    # Flatten rather than nest if either half is itself a
                    # three-way "X or Y or Z" compound.
                    sub1 = event1[1:-1] if event1[0].strip() == "EVENTS: [" else event1
                    sub2 = event2[1:-1] if event2[0].strip() == "EVENTS: [" else event2
                    return ["EVENTS: ["] + [f"  {l}" for l in sub1] + [f"  {l}" for l in sub2] + ["]"]

        # ── Phase/step events ──────────────────────────────────────────────
        if re.match(r'at the beginning of your upkeep', ll):
            return ["WHEN_UPKEEP_BEGIN { FILTER { YOU } }"]
        if re.match(r'at the beginning of your end step', ll):
            return ["WHEN_END_STEP_BEGIN { FILTER { YOU } }"]
        if re.match(r'at the beginning of your draw step', ll):
            return ["WHEN_DRAW_STEP_BEGIN { FILTER { YOU } }"]
        if re.match(r'at the beginning of each (?:player\'s )?end step', ll):
            return ["WHEN_END_STEP_BEGIN { }"]
        if re.match(r"at the beginning of each player's upkeep", ll):
            return ["WHEN_UPKEEP_BEGIN { }"]
        if re.match(r"at the beginning of each player's draw step", ll):
            return ["WHEN_DRAW_STEP_BEGIN { }"]
        if re.match(r'at the beginning of combat on your turn', ll):
            return ["WHEN_COMBAT_BEGIN { FILTER { YOU } }"]

        # ── ETB events ─────────────────────────────────────────────────────
        # Anchored to end-of-clause, not just the "enters" prefix — an
        # unanchored match here silently truncated any qualified/compound
        # trigger ("enters and isn't blocked", "enters from your
        # graveyard", "enters or dies") to a bare ETB, discarding the
        # qualifier with no gap marker at all. Found via a 319-card audit
        # across ETB/DIES/ATTACKS: compound "X or Y" triggers are handled
        # above (EVENTS, v0.68); anything else with a real qualifier now
        # correctly falls through to an honest gap instead.
        if re.match(r'when(?:ever)? (?:this (?:' + self_type_words + r')|' +
                    name_pattern + r') enters\.?$', ll):
            return ["@WHEN_ETB { FILTER { SELF } }"]

        # ETB with a power filter — "a creature you control with power N or greater enters"
        m = re.match(r'when(?:ever)? a creature you control with power (\d+) or greater enters', ll)
        if m:
            return [f'WHEN_ETB {{ FILTER {{ TYPE: "Creature" CONTROLLER: YOU POWER: GTE {m.group(1)} }} }}']

        # "<Name> or another [nontoken] <Type>[ or <Type>] you control enters"
        # (Q17 — Maralen, Pantlaza, High Perfect Morcant, Go-Shintai) — no
        # OR{SELF, ...} composition needed: SELF already satisfies its own
        # type filter (Maralen IS an Elf/Faerie), so this is exactly
        # equivalent to a bare type-filtered WHEN_ETB with no EXCEPT: SELF
        # (the "<Name> or" is naming-convention clarity in the oracle text,
        # not a distinct rules requirement).
        m = re.match(
            r'when(?:ever)? ' + name_pattern + r' or another (nontoken )?(\w+)(?: or (\w+))? you control enters', ll)
        if m:
            nontoken, type1, type2 = m.groups()
            type_clause = (f'TYPE: ["{type1.title()}", "{type2.title()}"]' if type2
                            else f'TYPE: "{type1.title()}"')
            filter_parts = [type_clause, "CONTROLLER: YOU"]
            if nontoken:
                filter_parts.append("CATEGORY: NONTOKEN")
            return [f'WHEN_ETB {{ FILTER {{ {" ".join(filter_parts)} }} }}']

        # "(a|another) [nontoken] [legendary] <Type> you control [with <keyword>] enters"
        m = re.match(
            r'when(?:ever)? (?:a|another) (nontoken )?(legendary )?(\w+) you control(?: with (\w+))? enters', ll)
        if m:
            nontoken, legendary, type_word, keyword = m.groups()
            type_clause = f'AND {{ TYPE: "Legendary" TYPE: "{type_word.title()}" }}' if legendary \
                else f'TYPE: "{type_word.title()}"'
            filter_parts = [type_clause, "CONTROLLER: YOU"]
            if nontoken:
                filter_parts.append("CATEGORY: NONTOKEN")
            if keyword:
                filter_parts.append(f"HAS_KEYWORD: @{keyword.upper()}")
            if "another" in ll:
                filter_parts.append("EXCEPT: SELF")
            return [f'WHEN_ETB {{ FILTER {{ {" ".join(filter_parts)} }} }}']

        # "Whenever one or more <Type> you control enter" (Marneus Calgar —
        # "tokens" isn't a real type word, use CATEGORY: TOKEN instead;
        # mirrors the "one or more <Type> you control attack" generalization).
        m = re.match(r'when(?:ever)? one or more (\w+) you control enters?', ll)
        if m:
            word = _singularize(m.group(1)).title()
            type_clause = "CATEGORY: TOKEN" if word == "Token" else f'TYPE: "{word}"'
            return [f'WHEN_ETB {{ FILTER {{ {type_clause} CONTROLLER: YOU }} }}']

        # ── Dies events ────────────────────────────────────────────────────
        # Anchored — see the ETB events comment above for why (same
        # silent-truncation risk: "dies during combat", "dies or...").
        if re.match(r'when(?:ever)? (?:this creature|' + name_pattern + r') dies\.?$', ll):
            return ["WHEN_DIES { FILTER { SELF } }"]
        # "When enchanted/equipped creature dies" (Demonic Vigor, Squee's
        # Embrace) — a triggered ability printed on the Aura/Equipment
        # itself, referencing the object it's attached to rather than
        # itself; $enchanted/$equipped is already bound card-wide via the
        # header's @ENCHANT/@EQUIP(...) AS $<n>. Exact match, not a prefix
        # check — "dies" can carry a trailing qualifier ("...dies this
        # turn", conditional clauses) that would otherwise be silently
        # dropped rather than gapped.
        if re.match(r"^when(?:ever)? enchanted creature dies$", ll):
            return ["WHEN_DIES { FILTER { $enchanted } }"]
        if re.match(r"^when(?:ever)? equipped creature dies$", ll):
            return ["WHEN_DIES { FILTER { $equipped } }"]
        m = re.match(r'when(?:ever)? (?:a|an|another) (\w+) (?:you control )?dies', ll)
        if m:
            type_word = m.group(1).title()
            ctrl = "YOU" if "you control" in ll else "ANY"
            excl = " EXCEPT: SELF" if "another" in ll else ""
            return [f"WHEN_DIES {{ FILTER {{ TYPE: \"{type_word}\" CONTROLLER: {ctrl}{excl} }} }}"]

        # ── Leaves-battlefield events ───────────────────────────────────────
        # Distinct from WHEN_DIES — any zone change off the battlefield
        # (bounce, exile, sacrifice, death), not just death. Anchored —
        # see the ETB events comment above for why.
        if re.match(r'when(?:ever)? (?:this (?:' + self_type_words + r')|' +
                    name_pattern + r') leaves the battlefield\.?$', ll):
            return ["WHEN_LEAVES_BATTLEFIELD { FILTER { SELF } } AS $event"]
        m = re.match(r'when(?:ever)? (?:a|an|another) (\w+)(?: (token))? (?:you control )?leaves the battlefield', ll)
        if m:
            type_word, token_word = m.groups()
            type_word = type_word.title()
            cat = " CATEGORY: TOKEN" if token_word else ""
            ctrl = "YOU" if "you control" in ll else "ANY"
            excl = " EXCEPT: SELF" if "another" in ll else ""
            return [f'WHEN_LEAVES_BATTLEFIELD {{ FILTER {{ TYPE: "{type_word}" CONTROLLER: {ctrl}{cat}{excl} }} }} AS $event']

        # ── Sacrifice events (WHEN_SACRIFICE, v0.56) ────────────────────────
        # Distinct from WHEN_DIES — sacrifice is the specific action, not
        # any battlefield-to-graveyard move (destroy, lethal damage, etc.).
        if re.match(r'when(?:ever)? you sacrifice a permanent', ll):
            return ["WHEN_SACRIFICE { FILTER { CONTROLLER: YOU } }"]
        m = re.match(r'when(?:ever)? you sacrifice an? (nontoken )?(\w+)', ll)
        if m:
            nontoken, type_word = m.groups()
            type_word = _singularize(type_word).title()
            cat = " CATEGORY: NONTOKEN" if nontoken else ""
            return [f'WHEN_SACRIFICE {{ FILTER {{ TYPE: "{type_word}" CONTROLLER: YOU{cat} }} }}']

        # ── Attack events ──────────────────────────────────────────────────
        # "attacks?" — compound/partner names ("Raph & Mikey") take the
        # plural verb form ("Raph & Mikey attack") even for a single card.
        # Anchored — see the ETB events comment above for why (same
        # silent-truncation risk: "attacks and isn't blocked", "attacks
        # the player with the most life", "attacks while saddled", ...).
        if re.match(r'when(?:ever)? (?:this (?:creature|vehicle)|' + name_pattern + r') attacks?\.?$', ll):
            return ["WHEN_ATTACKS { FILTER { SELF } }"]
        # "Whenever equipped/enchanted creature attacks" (Atomic
        # Microsizer, Fractal Harness) — same self-vs-attached-object
        # distinction as the dies events above. Exact match, not a prefix
        # check — Seraphic Greatsword's "...attacks the player with the
        # most life or tied for most life" showed a startswith check
        # silently drops a real targeting qualifier instead of gapping it.
        if re.match(r"^when(?:ever)? equipped creature attacks?$", ll):
            return ["WHEN_ATTACKS { FILTER { $equipped } }"]
        if re.match(r"^when(?:ever)? enchanted creature attacks?$", ll):
            return ["WHEN_ATTACKS { FILTER { $enchanted } }"]
        if re.match(r'when(?:ever)? (?:a |another )?creature you control attacks', ll):
            excl = " EXCEPT: SELF" if "another" in ll else ""
            return [f'WHEN_ATTACKS {{ FILTER {{ TYPE: "Creature" CONTROLLER: YOU{excl} }} }}']

        # "Whenever one or more <Type> creatures you control attack" (Aloy —
        # a card-type qualifier needing AND{} for the compound "artifact
        # creature") vs. "Whenever one or more <Type> you control attack"
        # (Choco/Ur-Dragon — a creature-type word standing alone).
        m = re.match(r'when(?:ever)? one or more (\w+) creatures? you control attacks?', ll)
        if m:
            type_word = _singularize(m.group(1)).title()
            return [f'WHEN_ATTACKS {{ FILTER {{ AND {{ TYPE: "{type_word}" TYPE: "Creature" }} CONTROLLER: YOU }} }}']
        m = re.match(r'when(?:ever)? one or more (\w+) you control attacks?', ll)
        if m:
            type_word = _singularize(m.group(1)).title()
            return [f'WHEN_ATTACKS {{ FILTER {{ TYPE: "{type_word}" CONTROLLER: YOU }} }}']

        # "Whenever you attack with one or more <Type>" (Sidar Jabari) —
        # same shape as the "one or more <Type> you control attack" pattern
        # above, just with "you" as the grammatical subject instead of the
        # attacking creatures themselves. A very common tribal/aggro
        # trigger template ("attack with three or more creatures", etc.).
        m = re.match(r'when(?:ever)? you attack with one or more (\w+)', ll)
        if m:
            type_word = _singularize(m.group(1)).title()
            return [f'WHEN_ATTACKS {{ FILTER {{ TYPE: "{type_word}" CONTROLLER: YOU }} }}']

        # ── Block events ───────────────────────────────────────────────────
        # WHEN_BLOCKS itself has been documented since before this session
        # (Section 5) but never had a self-reference branch wired at all —
        # same "documented, never wired" gap as several keywords fixed
        # earlier this session, just for an event instead of a keyword.
        # Anchored from the start, unlike its older siblings above — no
        # legacy unanchored version of this branch ever shipped.
        if re.match(r'when(?:ever)? (?:this creature|' + name_pattern + r') blocks?\.?$', ll):
            return ["WHEN_BLOCKS { FILTER { SELF } }"]

        # ── Cast events ────────────────────────────────────────────────────
        # Plain "Whenever you cast a spell" (Ms. Bumbleflower) — no color,
        # type, or ordinal qualifier. Checked first since it's a strict
        # prefix of several of the more specific patterns below.
        if re.match(r'when(?:ever)? you cast a spell$', ll):
            return ['WHEN_CAST { FILTER { CONTROLLER: YOU } }']
        if "you cast an artifact or enchantment spell" in ll:
            return ['WHEN_CAST { FILTER { TYPE: ["Artifact", "Enchantment"] CONTROLLER: YOU } }']
        if "you cast an instant or sorcery spell" in ll:
            return ['WHEN_CAST { FILTER { TYPE: ["Instant", "Sorcery"] CONTROLLER: YOU } }']
        if re.match(r'when(?:ever)? a player casts an instant or sorcery spell', ll):
            return ['WHEN_CAST { FILTER { TYPE: ["Instant", "Sorcery"] } }']
        if "you cast a noncreature spell" in ll:
            return ['WHEN_CAST { FILTER { NOT { TYPE: "Creature" } CONTROLLER: YOU } }']
        if "an opponent casts a noncreature spell" in ll:
            return ['WHEN_CAST { FILTER { NOT { TYPE: "Creature" } CONTROLLER: OPPONENT } }']
        if "an opponent casts a spell" in ll:
            return ['WHEN_CAST { FILTER { CONTROLLER: OPPONENT } }']
        # "an opponent casts their first/second/third noncreature spell each turn"
        m = re.match(r'when(?:ever)? an opponent casts their (first|second|third) noncreature spell each turn', ll)
        if m:
            ordinal = {"first": 1, "second": 2, "third": 3}[m.group(1)]
            return [
                'WHEN_CAST {',
                '  SELF.source.controller AS $caster',
                '  FILTER { NOT { TYPE: "Creature" } CONTROLLER: OPPONENT }',
                f'  CONDITION: COUNT($caster.spells_this_turn) EQ {ordinal}',
                '}',
            ]
        if "a player casts" in ll and "second spell" in ll:
            return [
                'WHEN_CAST {',
                '  SELF.source.controller AS $caster',
                '  FILTER { }',
                '  CONDITION: COUNT($caster.spells_this_turn) EQ 2',
                '}',
            ]
        if "you cast a myr spell" in ll:
            return ['WHEN_CAST { FILTER { TYPE: "Myr" CONTROLLER: YOU } }']
        # "Whenever you cast a/an/another <color|card-type|creature-type> spell
        #  [with mana value N or greater/less]" — Aragorn (color), Animar/Helga
        # (card type, optional MV), K'rrik (color), Rin and Seri (creature type),
        # Edgar Markov/Ulalek ("another"/indefinite creature type).
        m = re.match(
            r'when(?:ever)? you cast (a|an|another) (\w+) spell'
            r'(?: with mana value (\d+) or (greater|less))?$', ll)
        if m:
            article, word, mv_num, mv_dir = m.groups()
            color_map = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
            type_clause = f"COLOR: {color_map[word]}" if word in color_map else f'TYPE: "{word.title()}"'
            parts = [type_clause, "CONTROLLER: YOU"]
            if article == "another":
                parts.append("EXCEPT: SELF")
            if mv_num:
                op = "GTE" if mv_dir == "greater" else "LTE"
                parts.append(f"MV: {op} {mv_num}")
            return [f'WHEN_CAST {{ FILTER {{ {" ".join(parts)} }} }}']
        # "Whenever you cast your first/second/third spell [each turn]"
        # (Stella Lee; Alela — "each turn" is already implied once the
        # "during each opponent's turn" scope above has been stripped to a
        # DURING field, so it's optional here rather than required).
        m = re.match(r"when(?:ever)? you cast your (first|second|third) spell(?: each turn)?$", ll)
        if m:
            ordinal = {"first": 1, "second": 2, "third": 3}[m.group(1)]
            return [
                'WHEN_CAST {',
                '  FILTER { CONTROLLER: YOU }',
                f'  CONDITION: COUNT(YOU.spells_this_turn) EQ {ordinal}',
                '}',
            ]

        # ── Draw events ────────────────────────────────────────────────────
        if "you draw a card" in ll:
            return ["@WHEN_DRAW { FILTER { YOU } }"]
        if "an opponent draws a card" in ll:
            return ["@WHEN_DRAW { FILTER { CONTROLLER: OPPONENT } }"]
        if "a player draws a card" in ll or "whenever a player draws" in ll:
            return ["@WHEN_DRAW { FILTER { ANY } }"]

        # ── Life-gain events ────────────────────────────────────────────────
        # WHEN_GAIN_LIFE has existed in the spec (Section 5) since before
        # this pattern was ever wired to trigger text — lifegain-matters is
        # a whole recurring archetype (Oloro, Aetherflux Reservoir, ...).
        if "whenever you gain life" in ll:
            return ["WHEN_GAIN_LIFE { FILTER { YOU } }"]
        if "whenever an opponent gains life" in ll:
            return ["WHEN_GAIN_LIFE { FILTER { CONTROLLER: OPPONENT } }"]
        if "whenever a player gains life" in ll:
            return ["WHEN_GAIN_LIFE { FILTER { ANY } }"]

        # ── Damage events ──────────────────────────────────────────────────
        # "deals combat damage to..." (COMBAT: TRUE) vs. the unqualified
        # "deals damage to an opponent" (Lu Xun) — any damage, combat or
        # not, so COMBAT is omitted entirely (the field is optional and
        # unrestricted when absent).
        if re.search(r'deals (?:combat )?damage to (an opponent|a player)', ll):
            combat = " COMBAT: TRUE" if "combat damage" in ll else ""
            return [f"WHEN_DAMAGE_DEALT {{ FILTER {{ SELF }}{combat} TARGET_TYPE: PLAYER }}"]

        m = re.match(r'when(?:ever)? one or more (\w+) you control deals? combat damage to a player', ll)
        if m:
            type_word = _singularize(m.group(1)).title()
            return [f'WHEN_DAMAGE_DEALT {{ FILTER {{ TYPE: "{type_word}" CONTROLLER: YOU }} COMBAT: TRUE TARGET_TYPE: PLAYER }}']

        # "Whenever another source you control deals exactly N damage to a
        # permanent or player" (Ghyrson Starn) — TARGET_TYPE omitted since
        # "a permanent or player" already covers the full damage-recipient
        # space (creature/planeswalker/battle/player).
        m = re.match(r'when(?:ever)? another source you control deals exactly (\d+) damage to a permanent or player', ll)
        if m:
            return [
                'WHEN_DAMAGE_DEALT {',
                '  FILTER { CONTROLLER: YOU EXCEPT: SELF }',
                f'  CONDITION: $event.amount EQ {m.group(1)}',
                '}',
            ]

        # ── Counter events ─────────────────────────────────────────────────
        # "Whenever you put one or more <type> counters on a creature you control"
        m = re.match(r'when(?:ever)? you put one or more (\S+) counters? on a creature you control', ll)
        if m:
            counter_type = m.group(1)
            return [
                'WHEN_COUNTER_ADDED {',
                '  FILTER { TYPE: "Creature" CONTROLLER: YOU }',
                f'  CONDITION: $event.counters."{counter_type}" GTE 1',
                '} AS $event',
            ]
        if "counter is put on a creature you control" in ll:
            return ['WHEN_COUNTER_ADDED { FILTER { TYPE: "Creature" CONTROLLER: YOU } }']

        # ── Tapped events (WHEN_TAPPED, v0.58) ──────────────────────────────
        if re.match(r'when(?:ever)? (?:this (?:creature|permanent)|' + name_pattern + r') becomes tapped', ll):
            return ["WHEN_TAPPED { FILTER { SELF } }"]
        m = re.match(r'when(?:ever)? (?:a|an|another) (\w+) you control becomes tapped', ll)
        if m:
            type_word = _singularize(m.group(1)).title()
            excl = " EXCEPT: SELF" if "another" in ll else ""
            return [f'WHEN_TAPPED {{ FILTER {{ TYPE: "{type_word}" CONTROLLER: YOU{excl} }} }}']

        # ── Discard events ─────────────────────────────────────────────────
        if "you discard a card" in ll:
            return ["WHEN_DISCARD { FILTER { YOU } }"]

        # ── Landfall ──────────────────────────────────────────────────────
        if "land you control enters" in ll or "a land enters" in ll:
            return ['WHEN_ZONE_CHANGE { FILTER { TYPE: "Land" CONTROLLER: YOU } TO: BATTLEFIELD }']

        # ── Face-up ────────────────────────────────────────────────────────
        if "turned face up" in ll:
            return ["WHEN_ZONE_CHANGE { FILTER { SELF } TO: BATTLEFIELD }  # face-up event"]

        # ── Equip events ───────────────────────────────────────────────────
        if "whenever equipped creature" in ll and "deals combat damage" in ll:
            return ["WHEN_DAMAGE_DEALT { FILTER { $equipped } COMBAT: TRUE TARGET_TYPE: PLAYER }"]

        # ── Fallback ───────────────────────────────────────────────────────
        return [f"# SPEC_GAP: event not matched: {trigger_text}"]

    # ── Static abilities ──────────────────────────────────────────────────────

    def match_static(self, line: str, card: dict) -> list[str]:
        ll = line.lower().strip()
        card_name = card.get("name", "").lower()

        # "You have no maximum hand size." (Thought Vessel, v0.61)
        if ll == "you have no maximum hand size.":
            return [
                "  STATIC {",
                "    EFFECT: [ MAX_HAND_SIZE { PLAYER: YOU VALUE: NONE } ]",
                "  }",
            ]

        # "Eminence — As long as <Name> is in the command zone or on the
        # battlefield, other <Type> spells you cast cost {N} less to cast."
        # Same self-reference-by-name-prefix approach as _extract_condition,
        # and the same v0.56 .zone/OR() composition.
        em_m = re.match(
            r'^eminence\s*[—-]+\s*as long as (.+?) is in the command zone( or on the battlefield)?,\s*'
            r'other (\w+) spells you cast cost (\{[^}]+\}) less to cast\.?$', ll)
        if em_m:
            name_ref, or_battlefield, type_word, reduction = em_m.groups()
            orig_reduction_m = re.search(r'\{[^}]+\} less to cast\.?$', line.strip())
            if orig_reduction_m:
                reduction = orig_reduction_m.group(0).split(' less')[0]
            if card_name.startswith(name_ref.strip()):
                zone_cond = ("OR(SELF.zone EQ COMMAND_ZONE, SELF.zone EQ BATTLEFIELD)" if or_battlefield
                             else "SELF.zone EQ COMMAND_ZONE")
                return [
                    "  STATIC {",
                    f"    CONDITION: {zone_cond}",
                    "    EFFECT: [",
                    "      GRANT {",
                    f'        FILTER {{ TYPE: "{type_word.title()}" CATEGORY: SPELL CONTROLLER: YOU EXCEPT: SELF }}',
                    f'        COST_REDUCTION: "{reduction}"',
                    "      }",
                    "    ]",
                    "  }",
                ]

        # "Threshold — As long as there are N or more cards in your
        # graveyard, this creature gets +X/+X, is <color>, and has
        # "<activated ability>"." (Possessed Aven) — conditional self-grant
        # via STATIC's own CONDITION, reusing match_activated to build the
        # granted ability (GRANT bodies may contain whole ACTIVATED/
        # TRIGGERED/STATIC blocks per Section 15.26).
        m = re.match(
            r'^threshold\s*[—-]+\s*as long as there are? (\w+) or more cards in your graveyard,\s*'
            r'this creature gets ([+\-]\d+)/([+\-]\d+), is (\w+), and has "(.+)"\.?$', ll)
        if m:
            n_word, pw, tg, color_word, ability_text = m.groups()
            n = _word_to_num(n_word)
            color = _color_word_to_symbol(color_word)
            orig_m = re.search(r'has "(.+)"', line, re.IGNORECASE)
            granted = self.match_activated(orig_m.group(1) if orig_m else ability_text, card)
            # Re-indent match_activated's output (already "  ACTIVATED {"-
            # prefixed) one level deeper to nest inside GRANT.
            granted_indented = ["  " + gl for gl in granted]
            return [
                "  STATIC {",
                f"    CONDITION: COUNT(YOU.graveyard) GTE {n}",
                "    EFFECT: [",
                "      GRANT {",
                "        SELF",
                f"        POWER: {pw}",
                f"        TOUGHNESS: {tg}",
                f"        COLOR: {color}",
            ] + granted_indented + [
                "      }",
                "    ]",
                "  }",
            ]

        # "Spells of the chosen type you cast cost {W}{U}{B}{R}{G} less to
        # cast. This effect reduces only the amount of colored mana you
        # pay." (Morophon) — $chosen_type from the ETB CHOOSE above. The
        # "reduces only colored mana" note doesn't need its own field: a
        # mana-expression COST_REDUCTION of exactly "{W}{U}{B}{R}{G}"
        # already only ever subtracts those five colored symbols.
        m = re.match(
            r'^spells of the chosen type you cast cost (\{[^}]+(?:\}\{[^}]+)*\}) less to cast\.', ll)
        if m:
            orig_m = re.match(
                r'^spells of the chosen type you cast cost (\{[^}]+(?:\}\{[^}]+)*\}) less to cast\.',
                line.strip(), re.IGNORECASE)
            reduction = orig_m.group(1) if orig_m else m.group(1)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                "        FILTER { CATEGORY: SPELL CONTROLLER: YOU TYPE: $chosen_type }",
                f'        COST_REDUCTION: "{reduction}"',
                "      }",
                "    ]",
                "  }",
            ]

        # "Other creatures you control of the chosen type get +1/+1." (Morophon)
        m = re.match(r'^other creatures you control of the chosen type get ([+\-]\d+)/([+\-]\d+)\.?$', ll)
        if m:
            pw, tg = m.groups()
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { AND { TYPE: "Creature" TYPE: $chosen_type } CONTROLLER: YOU SELECTION: ALL }',
                "        EXCEPT: SELF",
                f"        POWER: {pw}",
                f"        TOUGHNESS: {tg}",
                "      }",
                "    ]",
                "  }",
            ]

        # "Each creature spell you cast with toughness greater than its
        # power costs {N} less to cast." (Doran) — the Kutzil pattern
        # (Section 14.1): SELF inside a FILTER means the object currently
        # being evaluated, so this compares each spell's own two stats
        # directly rather than needing a new comparison mechanism.
        m = re.match(
            r'^each creature spell you cast with toughness greater than its power '
            r'costs (\{[^}]+\}) less to cast\.?$', ll)
        if m:
            reduction = m.group(1)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { TYPE: "Creature" CATEGORY: SPELL CONTROLLER: YOU TOUGHNESS: GT SELF.power }',
                f'        COST_REDUCTION: "{reduction}"',
                "      }",
                "    ]",
                "  }",
            ]
        short_name = card_name.split(",", 1)[0].strip() if "," in card_name else card_name

        # "Cumulative upkeep {N}" — reminder text: "At the beginning of your
        # upkeep, put an age counter on this permanent, then sacrifice it
        # unless you pay its upkeep cost for each age counter on it."
        # Composition mirrors the existing "unless that player pays" CHOICE
        # shape in _match_pay_or_effect (pay to avoid vs. the fallback
        # effect), scaled by age-counter count via the v0.55-widened `*`
        # operator.
        m = re.match(r'^cumulative upkeep (\{[^}]+\})\.?$', ll)
        if m:
            cost = m.group(1)
            return [
                "  TRIGGERED {",
                "    WHEN_UPKEEP_BEGIN { FILTER { YOU } }",
                "    EFFECT: [",
                '      ADD_COUNTER { SELF NAME: "age" COUNT: 1 }',
                "      CHOICE {",
                "        PLAYER: YOU",
                "        TIMING: RESOLUTION",
                "        COUNT: 1",
                f'        OPTION {{ COST: MANA: "{cost}" * SELF.counters.age }}',
                "        OPTION { EFFECT: [ SACRIFICE { SELF } ] }",
                "      }",
                "    ]",
                "  }",
            ]

        # "Firebending N" / "Firebending X, where X is <Name>'s power" —
        # reminder text: "Whenever this creature attacks, add N red mana.
        # This mana lasts until end of combat." Pure composition: a real
        # TRIGGERED ability (classify_line buckets the bare keyword-style
        # line as STATIC, but the emitted block need not be STATIC — callers
        # append match_static's return value directly). "Lasts until end of
        # combat" is covered by the blanket "any effect block may include an
        # optional DURATION" rule (Section 15 preamble), not a new field.
        m = re.match(r'^firebending (\d+)\.?$', ll)
        if m:
            return [
                "  TRIGGERED {",
                "    WHEN_ATTACKS { FILTER { SELF } }",
                "    EFFECT: [",
                f'      ADD_MANA {{ PLAYER: YOU AMOUNT: {{ "{{R}}" COUNT: {m.group(1)} }} DURATION: END_OF_COMBAT }}',
                "    ]",
                "  }",
            ]
        m = re.match(r"^firebending x,? where x is (?:" + re.escape(card_name) + "|" +
                     re.escape(short_name) + r")'s power\.?$", ll)
        if m:
            return [
                "  TRIGGERED {",
                "    WHEN_ATTACKS { FILTER { SELF } }",
                "    EFFECT: [",
                '      ADD_MANA { PLAYER: YOU AMOUNT: { "{R}" COUNT: SELF.power } DURATION: END_OF_COMBAT }',
                "    ]",
                "  }",
            ]

        # "Each creature that's enchanted by an Aura you control can't attack
        # you or planeswalkers you control." (Eriette of the Charmed Apple)
        # — v0.55 ENCHANTED_BY filter field. DEFENDING accepting a FILTER
        # (not just a single object reference) for "any of your
        # planeswalkers" is a small extrapolation of the established
        # DEFENDING field (GRANT's WHEN_ATTACK) consistent with FILTER being
        # the spec's general mechanism for "any object matching criteria."
        if ll == "each creature that's enchanted by an aura you control can't attack you or planeswalkers you control.":
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      CANT {",
                '        FILTER { TYPE: "Creature" ENCHANTED_BY: { CONTROLLER: YOU } SELECTION: ALL }',
                "        EFFECTS: [ ATTACK { DEFENDING: YOU } ]",
                "      }",
                "      CANT {",
                '        FILTER { TYPE: "Creature" ENCHANTED_BY: { CONTROLLER: YOU } SELECTION: ALL }',
                '        EFFECTS: [ ATTACK { DEFENDING: FILTER { TYPE: "Planeswalker" CONTROLLER: YOU } } ]',
                "      }",
                "    ]",
                "  }",
            ]

        # "Each <Type> card in your hand has miracle. Its miracle cost is
        # equal to its mana cost reduced by {N}." — literally the spec's own
        # "Aminatou pattern" worked example (Section 15.26).
        m = re.match(
            r'^each (\w+) cards? in your hand has miracle\.\s*its miracle cost is equal to its '
            r'mana cost reduced by (\{[^}]+\})\.?$', ll)
        if m:
            type_word, reduction = m.groups()
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ TYPE: "{type_word.title()}" ZONE: YOU.hand SELECTION: ALL }}',
                f'        @MIRACLE(mana_cost - "{reduction}")',
                "      }",
                "    ]",
                "  }",
            ]

        # "As long as there are N or more <counter> counters among <Type>s you
        # control, <Name> has <grants>." (Tom Bombadil) — conditional
        # self-grant using the STATIC block's own CONDITION field with
        # SUM(<collection>.<property>) (Section 13, REDUCTION worked example).
        m = re.match(
            r'^as long as there are? (\w+) or more (\w+) counters? among (\w+)s? you control,? '
            r'(?:' + re.escape(card_name) + r') has (.+)$', ll)
        if m:
            n_word, counter_type, type_word, grant_text = m.groups()
            n = _word_to_num(n_word)
            grants = self._parse_keyword_grants(grant_text.rstrip('.'))
            count_expr = f'SUM(BATTLEFIELD FILTER {{ TYPE: "{type_word.title()}" CONTROLLER: YOU }}.counters."{counter_type}")'
            return [
                "  STATIC {",
                f"    CONDITION: {count_expr} GTE {n}",
                "    EFFECT: [",
            ] + self._render_grant("SELF", grants, indent="      ") + [
                "    ]",
                "  }",
            ]

        # ── Continuous dynamic P/T (self, "for each other <Type> you control") ──
        # "This creature gets +1/+0 for each other Rat you control." Delta
        # slots accept scalar expressions directly (v0.44) — no WHERE/$X
        # indirection needed for a plain +1-per-unit count. Only the +1-per-
        # unit shape is handled: the spec's `*` scaling operator is valid
        # for COST_REDUCTION/ADD_MANA only, not POWER/TOUGHNESS deltas, so a
        # "+2 per unit" phrasing would need a different composition and is
        # left to gap rather than guessed at.
        m = re.match(r'^this creature gets \+([01])/\+([01]) for each other (\w+) you control\.?$', ll)
        if m:
            pw, tg, type_word = m.groups()
            count_expr = f'COUNT(BATTLEFIELD FILTER {{ TYPE: "{type_word.title()}" CONTROLLER: YOU NOT {{ SELF }} }})'
            parts = []
            if pw == "1":
                parts.append(f"POWER: +{count_expr}")
            if tg == "1":
                parts.append(f"TOUGHNESS: +{count_expr}")
            if parts:
                return [
                    "  STATIC {",
                    "    EFFECT: [",
                    "      GRANT {",
                    "        SELF",
                    f"        {' '.join(parts)}",
                    "      }",
                    "    ]",
                    "  }",
                ]

        # "Legendary creatures you control get +X/+X, where X is the number
        # of legendary creatures you control." — mass, self-inclusive.
        m = re.match(
            r'^legendary creatures you control get \+x/\+x,? where x is '
            r'the number of legendary creatures you control\.?$', ll)
        if m:
            count_expr = 'COUNT(BATTLEFIELD FILTER { AND { TYPE: "Legendary" TYPE: "Creature" } CONTROLLER: YOU })'
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { AND { TYPE: "Legendary" TYPE: "Creature" } CONTROLLER: YOU SELECTION: ALL }',
                f"        POWER: +{count_expr} TOUGHNESS: +{count_expr}",
                "      }",
                "    ]",
                "  }",
            ]

        # "<Name> gets +X/+0, where X is the number of other creatures you
        # control with base power N." — self, filtered count. Cards
        # self-reference by short name ("Zinnia" not "Zinnia, Valley's Voice").
        short_name = card_name.split(",", 1)[0].strip() if "," in card_name else card_name
        m = re.match(
            r'(?:' + re.escape(card_name) + '|' + re.escape(short_name) + ')'
            r" gets \+x/\+0,? where x is the number of other creatures "
            r'you control with base power (\d+)\.?$', ll)
        if m:
            pw = m.group(1)
            count_expr = (f'COUNT(BATTLEFIELD FILTER {{ TYPE: "Creature" CONTROLLER: YOU '
                          f'NOT {{ SELF }} base_power: {pw} }})')
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                "        SELF",
                f"        POWER: +{count_expr}",
                "      }",
                "    ]",
                "  }",
            ]

        # "If [a creature attacking/dying] causes a triggered ability of
        # <subject> to trigger, that ability triggers an additional time"
        m = re.match(
            r'^if (?:a |an )?(.+?) causes an? (?:triggered )?ability of (.+?) to trigger, '
            r'that ability triggers an additional time', ll)
        if m:
            cause_text, subject_text = m.groups()
            cause = None
            if "attacking" in cause_text:
                cause = "WHEN_ATTACKS"
            elif "dying" in cause_text or "dies" in cause_text:
                cause = "WHEN_DIES"
            filter_clause = _filter_for_subject_phrase(subject_text)
            lines = ["  STATIC {", "    EFFECT: [", "      TRIGGER_COUNT {", f"        {filter_clause}"]
            if cause:
                lines.append(f"        CAUSE: {cause}")
            lines += ["        COUNT: 2", "      }", "    ]", "  }"]
            return lines

        # "If a/an [triggered] ability of <subject> triggers, that ability
        # triggers an additional time" (no CAUSE — any triggered ability)
        m = re.match(
            r'^if an? (?:triggered )?ability of (.+?) triggers, '
            r'that ability triggers an additional time', ll)
        if m:
            filter_clause = _filter_for_subject_phrase(m.group(1))
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      TRIGGER_COUNT {",
                f"        {filter_clause}",
                "        COUNT: 2",
                "      }",
                "    ]",
                "  }",
            ]

        # "Ward—<non-mana cost>" (mana-cost Ward, e.g. "Ward {2}", is a
        # simple parameterized keyword already handled via keyword_store).
        # Per the v0.46 changelog, @WARD expands to
        # GRANT { SELF WHEN_TARGET { } ADDL_COST: MANA: "<cost>" } - only
        # the ADDL_COST value's shape depends on the cost type here.
        m = re.match(r'^ward[—-]\s*(.+)$', ll)
        if m:
            cost_text = m.group(1).rstrip('.')
            addl_cost = None
            lm = re.match(r'^pay (\d+) life$', cost_text)
            bm = re.match(r'^blight (\d+)$', cost_text)
            if lm:
                addl_cost = f"PAY_LIFE: {lm.group(1)}"
            elif bm:
                # "Blight N" — reminder text: "a player puts N -1/-1 counters
                # on a creature they control." v0.55 ADD_COUNTER cost field.
                addl_cost = (f'ADD_COUNTER: {{ NAME: "-1/-1" COUNT: {bm.group(1)} '
                             f'TARGET: CHOOSE {{ FROM: BATTLEFIELD COUNT: 1 FILTER {{ TYPE: "Creature" CONTROLLER: YOU }} }} }}')
            else:
                sm = re.match(r'^sacrifice (a|an) (\w+)$', cost_text)
                if sm:
                    addl_cost = (f'SACRIFICE: CHOOSE {{ FROM: BATTLEFIELD COUNT: 1 '
                                 f'FILTER {{ TYPE: "{sm.group(2).title()}" CONTROLLER: YOU }} }}')
                else:
                    # Compound cost: "sacrifice a [legendary] X or [legendary] Y"
                    sm2 = re.match(r'^sacrifice (?:a|an) (legendary )?(\w+) or (legendary )?(\w+)$', cost_text)
                    if sm2:
                        leg1, type1, leg2, type2 = sm2.groups()

                        def _clause(leg, t):
                            return f'AND {{ TYPE: "Legendary" TYPE: "{t.title()}" }}' if leg else f'TYPE: "{t.title()}"'

                        addl_cost = (
                            'SACRIFICE: CHOOSE { FROM: BATTLEFIELD COUNT: 1 '
                            f'FILTER {{ CONTROLLER: YOU OR {{ {_clause(leg1, type1)} {_clause(leg2, type2)} }} }} }}')
            if addl_cost:
                return [
                    "  STATIC {",
                    "    EFFECT: [",
                    "      GRANT {",
                    "        SELF",
                    "        WHEN_TARGET { }",
                    f"        ADDL_COST: {addl_cost}",
                    "      }",
                    "    ]",
                    "  }",
                ]
            # Compound/unrecognized Ward cost (e.g. "sacrifice a legendary
            # artifact or legendary creature", or a novel cost type like
            # "Blight 2") - fall through to the generic gap rather than
            # guess at a FILTER that would silently drop the "or" clause.

        # "Each creature you control [with defender] assigns combat damage
        # equal to its toughness rather than its power [and can attack as
        # though it didn't have defender]." (Arcades combines both clauses
        # in one sentence with a "with defender" qualifier; Felothar splits
        # them into two separate, unqualified sentences — see the standalone
        # "creatures you control can attack..." pattern just below for that
        # second half.) DAMAGE_ASSIGNMENT and NULLIFY are both v0.59
        # (user-approved): NULLIFY deliberately leaves Defender itself
        # intact — these creatures still HAS_KEYWORD: @DEFENDER, only its
        # attack restriction stops applying.
        m = re.match(
            r"^each creature you control(?P<defender> with defender)? assigns combat damage equal to its "
            r"toughness rather than its power(?P<attack> and can attack as though it didn'?t have defender)?\.?$",
            ll)
        if m:
            filter_parts = ['TYPE: "Creature"']
            if m.group("defender"):
                filter_parts.append("HAS_KEYWORD: @DEFENDER")
            filter_parts += ["CONTROLLER: YOU", "SELECTION: ALL"]
            filter_str = " ".join(filter_parts)
            lines = [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f"        FILTER {{ {filter_str} }}",
                "        DAMAGE_ASSIGNMENT: TOUGHNESS",
                "      }",
            ]
            if m.group("attack"):
                lines += [
                    "      NULLIFY {",
                    f"        FILTER {{ {filter_str} }}",
                    "        EFFECTS: [ATTACK]",
                    "      }",
                ]
            lines += ["    ]", "  }"]
            return lines

        # "Creatures you control can attack as though they didn't have
        # defender." (Felothar's second sentence)
        if re.match(r"^creatures you control can attack as though they didn'?t have defender\.?$", ll):
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      NULLIFY {",
                '        FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }',
                "        EFFECTS: [ATTACK]",
                "      }",
                "    ]",
                "  }",
            ]

        # "Creatures you control have haste"
        m = re.match(r'^creatures you control (?:have|gain) (.+)', ll)
        if m:
            grant_text = m.group(1).rstrip('.')
            grants = self._parse_keyword_grants(grant_text)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }',
            ] + [f"        {g}" for g in grants] + [
                "      }",
                "    ]",
                "  }",
            ]

        # "Artifact creatures you control have menace"
        m = re.match(r'^artifact creatures you control (?:have|gain) (.+)', ll)
        if m:
            grant_text = m.group(1).rstrip('.')
            grants = self._parse_keyword_grants(grant_text)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { AND { TYPE: "Artifact" TYPE: "Creature" } CONTROLLER: YOU SELECTION: ALL }',
            ] + [f"        {g}" for g in grants] + [
                "      }",
                "    ]",
                "  }",
            ]

        # "[Other/Nontoken] <Type> you control have/get <grant>"
        # (generalizes beyond the hardcoded "creatures"/"artifact creatures"
        # cases above to arbitrary subtypes — Dwarves, Horrors, artifacts, ...)
        m = re.match(r'^(other |nontoken )?(\w+) you control (?:have|has|get|gain) (.+)', ll)
        if m:
            qualifier, type_word, grant_text = m.groups()
            grant_text = grant_text.rstrip('.')
            type_name = _singularize(type_word).title()
            excl = " EXCEPT: SELF" if qualifier and "other" in qualifier else ""
            filter_line = f'        FILTER {{ TYPE: "{type_name}" CONTROLLER: YOU SELECTION: ALL{excl} }}'
            pump_m = re.match(r'^([+\-]\d+)/([+\-]\d+)$', grant_text)
            # "get +N/+N and have/has/gain <keyword-list>" (Feline Sovereign,
            # Devil Dinosaur) — previously fed whole into
            # _parse_keyword_grants, which has no notion of a P/T pump and
            # produced "+1/+1" and "have <keyword>" (has-prefix intact)
            # garbage fragments instead of composing both fields.
            compound_m = re.match(r'^([+\-]\d+)/([+\-]\d+) and (?:have|has|gain) (.+)$', grant_text)
            if compound_m:
                pw, tg, kw_text = compound_m.groups()
                body = [f"        POWER: {pw}", f"        TOUGHNESS: {tg}"]
                body += [f"        {g}" for g in self._parse_keyword_grants(kw_text)]
            elif pump_m:
                body = [f"        POWER: {pump_m.group(1)}", f"        TOUGHNESS: {pump_m.group(2)}"]
            else:
                body = [f"        {g}" for g in self._parse_keyword_grants(grant_text)]
            return ["  STATIC {", "    EFFECT: [", "      GRANT {", filter_line] + body + [
                "      }", "    ]", "  }",
            ]

        # "<Type> tokens you control have/get <grant>" (Teysa Karlov)
        m = re.match(r'^(\w+) tokens you control (?:have|has|get|gain) (.+)', ll)
        if m:
            type_word, grant_text = m.groups()
            grant_text = grant_text.rstrip('.')
            type_name = _singularize(type_word).title()
            grants = self._parse_keyword_grants(grant_text)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ TYPE: "{type_name}" CATEGORY: TOKEN CONTROLLER: YOU SELECTION: ALL }}',
            ] + [f"        {g}" for g in grants] + [
                "      }",
                "    ]",
                "  }",
            ]

        # "All <Type> creatures have/get <grant>" (Sidewinder Sliver — no "you control")
        m = re.match(r'^all (\w+) creatures (?:have|has|get|gain) (.+)', ll)
        if m:
            type_word, grant_text = m.groups()
            grant_text = grant_text.rstrip('.')
            type_name = _singularize(type_word).title()
            grants = self._parse_keyword_grants(grant_text)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ AND {{ TYPE: "{type_name}" TYPE: "Creature" }} SELECTION: ALL }}',
            ] + [f"        {g}" for g in grants] + [
                "      }",
                "    ]",
                "  }",
            ]

        # "Nontoken <Type>s you control are <Type2> in addition to their
        # other types." (Toph) — GRANT's TYPES field is additive, so this
        # composes directly without needing STATS' substitutive semantics.
        m = re.match(r'^nontoken (\w+)s you control are (\w+) in addition to their other types\.?$', ll)
        if m:
            src_type, add_type = m.groups()
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ TYPE: "{src_type.title()}" CONTROLLER: YOU CATEGORY: NONTOKEN SELECTION: ALL }}',
                f'        TYPES: ["{add_type.title()}"]',
                "      }",
                "    ]",
                "  }",
            ]

        # "Each nonland permanent you control is all colors." (Leyline of
        # the Guildpact) — GRANT is documented as "fully open" (any card-
        # level property, incl. COLOR, per the v0.31 changelog); adding all
        # five colors achieves the same result as a substitutive "is all
        # colors" effect since the union is already the complete set.
        if ll == "each nonland permanent you control is all colors.":
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { CATEGORY: NONLAND CONTROLLER: YOU SELECTION: ALL }',
                '        COLOR: ["W", "U", "B", "R", "G"]',
                "      }",
                "    ]",
                "  }",
            ]

        # "Lands you control are every basic land type in addition to their
        # other types." (Leyline of the Guildpact) — basic land types are
        # SUBTYPES (additive, same reasoning as the Toph pattern above).
        if ll == "lands you control are every basic land type in addition to their other types.":
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { TYPE: "Land" CONTROLLER: YOU SELECTION: ALL }',
                '        SUBTYPES: ["Plains", "Island", "Swamp", "Mountain", "Forest"]',
                "      }",
                "    ]",
                "  }",
            ]

        # "<Type> spells you control can't be countered" (Rhythm of the
        # Wild) — mass restriction, distinct from "this spell can't be
        # countered" (self, header-level ability type).
        m = re.match(r"^(\w+) spells you control can'?t be countered\.?$", ll)
        if m:
            type_name = _singularize(m.group(1)).title()
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      CANT {",
                f'        FILTER {{ TYPE: "{type_name}" CATEGORY: SPELL CONTROLLER: YOU SELECTION: ALL }}',
                "        EFFECTS: [COUNTER]",
                "      }",
                "    ]",
                "  }",
            ]

        # "Spells you cast with mana value N or greater/less have <grant>"
        # (Imoti) — no type qualifier, MV-gated.
        m = re.match(r'^spells you cast with mana value (\d+) or (greater|less) have (.+)$', ll)
        if m:
            mv_num, mv_dir, grant_text = m.groups()
            grant_text = grant_text.rstrip('.')
            op = "GTE" if mv_dir == "greater" else "LTE"
            grants = self._parse_keyword_grants(grant_text)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ CATEGORY: SPELL CONTROLLER: YOU MV: {op} {mv_num} SELECTION: ALL }}',
            ] + [f"        {g}" for g in grants] + [
                "      }",
                "    ]",
                "  }",
            ]

        # "<Type> spells you cast have/gain <grant>" (First Sliver, Ezio)
        m = re.match(r'^(\w+) spells you cast (?:have|gain) (.+)', ll)
        if m:
            type_word, grant_text = m.groups()
            grant_text = grant_text.rstrip('.')
            # Trailing "as you cast them/it" is boilerplate on cast-time
            # keywords (Freerunning, Offspring) — inherent to what those
            # keywords mean, not part of the grant itself.
            grant_text = re.sub(r'\s+as you cast (?:them|it)$', '', grant_text)
            type_name = _singularize(type_word).title()
            # Extract from the original-case line, not the lowercased ll —
            # _parse_keyword_grants looks up keyword names case-sensitively
            # and mana symbols inside a parameterized keyword (e.g.
            # "freerunning {B}{B}") must keep their printed case.
            orig_m = re.match(r'^\w+ spells you cast (?:have|gain) (.+)', line.strip(), re.IGNORECASE)
            if orig_m:
                grant_text = re.sub(r'\s+as you cast (?:them|it)$', '', orig_m.group(1).rstrip('.'), flags=re.IGNORECASE)
            grants = self._parse_keyword_grants(grant_text)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ TYPE: "{type_name}" CATEGORY: SPELL CONTROLLER: YOU SELECTION: ALL }}',
            ] + [f"        {g}" for g in grants] + [
                "      }",
                "    ]",
                "  }",
            ]

        # "Other creatures you control with flying get +1/+0"
        m = re.match(r'^other (\w+ )?creatures you control(?: with (\w+))? get ([+\-]\d+)/([+\-]\d+)', ll)
        if m:
            extra_filter = f' TYPE: "{m.group(2).title()}"' if m.group(2) else ""
            pw, tgh = m.group(3), m.group(4)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }}',
                "        EXCEPT: SELF",
                f"        POWER: {pw}",
                f"        TOUGHNESS: {tgh}",
                "      }",
                "    ]",
                "  }",
            ]

        # "You may play an additional land on each of your turns"
        if "may play an additional land" in ll:
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      ADDITIONAL_LAND_PLAY { PLAYER: YOU }",
                "    ]",
                "  }",
            ]

        # "You may cast/play this card from <zone>." — a bare alternate-
        # casting permission. CAST/PLAY (Section 15.34/15.34a) already
        # document a general-purpose FROM field for exactly this ("cast a
        # spell selected from a zone"), so this composes GRANT + CAST/PLAY
        # rather than introducing anything new. Zone-parameterized so
        # exile and the command zone are covered by the same pattern as
        # graveyard, not just graveyard specifically. Note: this differs
        # from the KEYWORD("Flashback")/KEYWORD("Harmonize") macro
        # definitions elsewhere in the spec, which use a dedicated
        # ALT_CAST { CAST_FROM: ... } block — that construct is documented
        # only inside named KEYWORD(...) macro bodies (Section 2.4), not as
        # a standalone card ability, so it wasn't reused here; flagged for
        # review rather than guessed at blind. Condition-gated ("...as long
        # as you control a Zombie") and extra-cost ("...by discarding a
        # card...") variants aren't attempted — no clear documented way to
        # attach either to this composition without guessing.
        zone_m = re.match(r'^you may (cast|play) this card from (your graveyard|exile|the command zone)\.?$', ll)
        if zone_m:
            verb, zone_text = zone_m.groups()
            zone = {"your graveyard": "YOU.graveyard", "exile": "YOU.exile",
                     "the command zone": "COMMAND_ZONE"}[zone_text]
            action = "CAST" if verb == "cast" else "PLAY"
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                "        SELF",
                f"        {action} {{ SELF FROM: {zone} }}",
                "      }",
                "    ]",
                "  }",
            ]

        # "You may play lands from your graveyard"
        if "may play lands from your graveyard" in ll:
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      LAND_PLAY_FROM { PLAYER: YOU ZONE: YOU.graveyard }",
                "    ]",
                "  }",
            ]

        # "Green spells you cast cost {1} less"
        m = re.match(r'^(\w+) spells you cast cost (\{[^}]+\}) less', ll)
        if m:
            color_word = m.group(1)
            reduction = m.group(2)
            color = _color_word_to_symbol(color_word)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                f'        FILTER {{ COLOR: {color} CATEGORY: SPELL }}',
                f'        COST_REDUCTION: "{reduction}"',
                "      }",
                "    ]",
                "  }",
            ]

        # "If one or more +1/+1 counters would be put on a creature you control, twice that many"
        if "twice that many" in ll and "+1/+1 counter" in ll:
            return [
                "  REPLACE {",
                '    EVENT: AS_COUNTER_ADDED { FILTER { TYPE: "Creature" CONTROLLER: YOU } } AS $event',
                '    CONDITION: $event.counters."+1/+1" GTE 1',
                "    WITH: [",
                '      ADD_COUNTER FROM $event { COUNT: $event.counters."+1/+1" * 2 }',
                "    ]",
                "  }",
            ]

        # "If one or more tokens would be created under your control, twice that many"
        if "twice that many" in ll and "token" in ll:
            return [
                "  REPLACE {",
                "    EVENT: AS_CREATE_TOKEN { FILTER { CONTROLLER: YOU } } AS $event",
                "    WITH: [",
                "      CREATE_TOKEN FROM $event { COUNT: $event.tokens.count * 2 }",
                "    ]",
                "  }",
            ]

        # "Combat damage that would be dealt by creatures you control can't be prevented"
        if "can't be prevented" in ll and "combat damage" in ll:
            return [
                "  STATIC {",
                "    EFFECT: [",
                '      GRANT {',
                '        FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }',
                '        DAMAGE_UNPREVENTABLE',
                '      }',
                "    ]",
                "  }",
            ]

        # "CARDNAME can't be blocked by creatures with power 2 or less"
        m = re.match(r'^' + re.escape(card_name) + r" can't be blocked by creatures with power (\d+) or less", ll)
        if m:
            n = m.group(1)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      CANT {",
                f'        FILTER {{ TYPE: "Creature" POWER: LTE {n} SELECTION: ALL }}',
                "        EFFECTS: [ BLOCK { SELF } ]",
                "      }",
                "    ]",
                "  }",
            ]

        # "Your opponents can't cast spells during your turn"
        if "opponents can't cast spells during your turn" in ll:
            return [
                "  STATIC {",
                "    DURING: YOUR_TURN",
                "    EFFECT: [",
                "      CANT {",
                '        FILTER { CONTROLLER: OPPONENT SELECTION: ALL }',
                '        EFFECTS: [ CAST { } ]',
                "      }",
                "    ]",
                "  }",
            ]

        # "Creatures can't attack you unless their controller pays {N} for
        # each creature they control that's attacking you" — the spec's own
        # "Propaganda pattern" worked example (Section 15.26 GRANT WHEN_
        # modifier blocks): an attack tax via WHEN_ATTACK's ADDL_COST.
        m = re.match(
            r"^creatures can'?t attack you unless their controller pays (\{[^}]+\}) "
            r"for each creature they control that'?s attacking you", ll)
        if m:
            cost = m.group(1)
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      GRANT {",
                '        FILTER { TYPE: "Creature" SELECTION: ALL }',
                "        WHEN_ATTACK { DEFENDING: YOU }",
                f'        ADDL_COST: MANA: "{cost}"',
                "      }",
                "    ]",
                "  }",
            ]

        # "This artifact doesn't untap during your untap step"
        if "doesn't untap during your untap step" in ll:
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      CANT {",
                "        SELF",
                "        EFFECTS: [UNTAP]",
                "        DURATION: UNTAP_STEP",
                "      }",
                "    ]",
                "  }",
            ]

        # "Prevent all damage that would be dealt to this creature."/etc. —
        # a continuous prevention shield described as a lone STATIC line
        # (rule 611/615), not inside a TRIGGERED/ACTIVATED/SPELL EFFECT
        # list. Reuses the same _match_prevent logic match_effect uses,
        # wrapped in STATIC { EFFECT: [ ] } to match the DAMAGE_UNPREVENTABLE
        # precedent (Section 15.29).
        if ll.startswith("prevent "):
            prevented = self._match_prevent(line, card)
            if prevented is not None:
                return (
                    ["  STATIC {", "    EFFECT: ["]
                    + [f"      {p}" for p in prevented]
                    + ["    ]", "  }"]
                )

        # ── Self-referential CANT/MUST/CASE ("This creature can't block.",
        # "...attacks each combat if able.", "...gets +N/+N as long as X.")
        # — the same shapes already composed for equipped/enchanted grants
        # (Section 18/18b, 16), just SELF as the subject instead of
        # $equipped/$enchanted, reusing the same helpers.
        lls = ll.rstrip('.')

        conditional = self._try_conditional_equipped_grant(lls, "SELF", prefix="this creature")
        if conditional is not None:
            return conditional

        if lls == "this creature can't block":
            return ["  STATIC {", "    EFFECT: [", "      CANT { SELF EFFECTS: [BLOCK] }", "    ]", "  }"]
        if lls == "this creature can't attack":
            return ["  STATIC {", "    EFFECT: [", "      CANT { SELF EFFECTS: [ATTACK] }", "    ]", "  }"]
        if lls == "this creature can't be blocked":
            return ["  STATIC {", "    EFFECT: [",
                     "      CANT { ALL EFFECTS: [ BLOCK { SELF } ] }", "    ]", "  }"]
        if lls == "this creature attacks each combat if able":
            return ["  STATIC {", "    EFFECT: [", "      MUST { SELF EFFECTS: [ATTACK] }", "    ]", "  }"]
        if lls == "this creature blocks each combat if able":
            return ["  STATIC {", "    EFFECT: [", "      MUST { SELF EFFECTS: [BLOCK] }", "    ]", "  }"]

        m = re.match(r"^this creature can't be blocked by creatures with (\w+)$", lls)
        if m:
            return [
                "  STATIC {", "    EFFECT: [", "      CANT {",
                f'        FILTER {{ HAS_KEYWORD: @{m.group(1).upper()} SELECTION: ALL }}',
                "        EFFECTS: [ BLOCK { SELF } ]", "      }", "    ]", "  }",
            ]
        m = re.match(r"^this creature can't be blocked by creatures with power (\d+) or less$", lls)
        if m:
            return [
                "  STATIC {", "    EFFECT: [", "      CANT {",
                f'        FILTER {{ POWER: LTE {m.group(1)} SELECTION: ALL }}',
                "        EFFECTS: [ BLOCK { SELF } ]", "      }", "    ]", "  }",
            ]
        # "can't be blocked except by <color> creatures" — restricted to a
        # simple single-color exception; compound ("artifact creatures
        # and/or white creatures") or count-based ("three or more
        # creatures") exceptions are a different shape and still gap.
        m = re.match(r"^this creature can't be blocked except by (white|blue|black|red|green|colorless) creatures$",
                      lls)
        if m:
            sym = _color_word_to_symbol(m.group(1))
            return [
                "  STATIC {", "    EFFECT: [", "      CANT {",
                f'        FILTER {{ NOT {{ COLOR: {sym} }} SELECTION: ALL }}',
                "        EFFECTS: [ BLOCK { SELF } ]", "      }", "    ]", "  }",
            ]
        # "can block only creatures with <keyword>" — SELF is the one
        # restricted; the inner BLOCK subject is who it's restricted
        # against (creatures lacking the keyword), same inner-subject/
        # FILTER convention as the "except by" case above, just with the
        # outer/inner roles swapped.
        m = re.match(r"^this creature can block only creatures with (\w+)$", lls)
        if m:
            return [
                "  STATIC {", "    EFFECT: [", "      CANT {",
                "        SELF",
                f'        EFFECTS: [ BLOCK {{ FILTER {{ NOT {{ HAS_KEYWORD: @{m.group(1).upper()} }} SELECTION: ALL }} }} ]',
                "      }", "    ]", "  }",
            ]

        return [f"  # SPEC_GAP: static pattern unmatched: {line}"]

    def _parse_keyword_grants(self, text: str) -> list[str]:
        """Convert 'haste and lifelink' or 'flying' into @KEYWORD lines."""
        stripped = text.strip()
        # A fully quoted clause is a granted ability description (a whole
        # TRIGGERED/STATIC sub-ability granted as text, e.g. Crystalline
        # Nautilus's "has \"When this creature becomes the target ...,
        # sacrifice it.\""), not a keyword list — splitting it on internal
        # commas/"and" would shatter one honest gap into several bogus
        # UNKNOWN_KEYWORD fragments. Same guard already used for
        # CREATE_TOKEN's quoted ability capture.
        if re.match(r'^".+"$', stripped):
            return [f"# UNKNOWN_KEYWORD: {stripped}"]
        # Split on "and" and "," — _fold_protection_parts re-merges a
        # compound "protection from X, from Y, and from Z" back together
        # (see match_keyword_line for the same fold, applied to static
        # keyword lines rather than dynamically granted ones).
        raw_parts = re.split(r',\s*|\s+and\s+', stripped.rstrip('.'))
        result = []
        for part in _fold_protection_parts(raw_parts):
            if not part:
                continue
            prot = _match_protection_keyword(part)
            if prot is not None:
                result.extend(s.strip() for s in prot)
                continue
            cdl = self.kw_store.match(part)
            if cdl:
                result.append(cdl)
            else:
                result.append(f"# UNKNOWN_KEYWORD: {part}")
        return result

    def _render_grant(self, subj: str, grants: list[str], duration: Optional[str] = None,
                       indent: str = "") -> list[str]:
        """
        Render a GRANT block for one or more keyword grants (from
        _parse_keyword_grants). When every grant resolved to real CDL, keep
        the compact one-line form callers used to build by hand. When any
        grant is an `# UNKNOWN_KEYWORD: ...` placeholder, its `#` would
        otherwise open a comment mid-line and swallow everything after it —
        DURATION and the closing brace included — producing unterminated
        CDL. Placeholders get their own multi-line block instead, one grant
        per line, mirroring the block form already used by callers that
        build a GRANT with a FILTER subject.
        """
        duration_part = f" DURATION: {duration}" if duration else ""
        if not any(g.lstrip().startswith('#') for g in grants):
            return [f"{indent}GRANT {{ {subj} {' '.join(grants)}{duration_part} }}"]
        lines = [f"{indent}GRANT {{", f"{indent}  {subj}"]
        lines += [f"{indent}  {g}" for g in grants]
        if duration:
            lines.append(f"{indent}  DURATION: {duration}")
        lines.append(f"{indent}}}")
        return lines

    # ── Spell effects ─────────────────────────────────────────────────────────

    def match_spell_body(self, oracle: str, card: dict) -> list[str]:
        """Match full spell oracle text into SPELL { EFFECT: [...] }."""
        lines_in = [l.strip() for l in oracle.split('\n') if l.strip()]
        effect_lines = []

        for line in lines_in:
            ll = line.lower()
            # Skip reminder text
            if re.match(r'^\([A-Z]', line) and line.endswith(')'):
                continue
            # Keywords at top of spells (e.g. CANT_BE_COUNTERED handled at header level)
            if "can't be countered" in ll:
                continue
            # Additional cost → ADDL_COST at header level
            if ll.startswith("as an additional cost"):
                continue
            # Alternate cost (Force of Will pattern, free-cast pattern) → ALT_COST at header level
            if ("rather than pay this spell's mana cost" in ll or
                    "without paying its mana cost" in ll or "without paying their mana cost" in ll):
                continue
            # Cost reduction for spell itself
            if "this spell costs" in ll and "less" in ll:
                continue  # handled in header COST_REDUCTION
            # Deck-construction override → CONSTRUCTED_OVERRIDE at header level
            if re.match(r'^a deck can have any number of cards named ', ll):
                continue

            # Bullet point modes (CHOICE)
            if line.startswith('•'):
                effect_lines.append(f"# CHOICE_OPTION: {line[1:].strip()}")
                continue

            matched = self.match_effect(line, card)
            effect_lines += matched

        return effect_lines

    # ── Equipped/enchanted creature grants ───────────────────────────────────

    def _extract_equipped_grant_extras(self, grants_text: str, binding: str = "$equipped") -> tuple:
        """
        Strip trailing clauses from an equipped/enchanted "has <X>" grant
        list that _parse_keyword_grants has no notion of — a type/color
        addition, the changeling "is every creature type" idiom, a combat
        damage assignment swap, a keyword-loss, a forced-attack/-block
        requirement, or a simple attack/block/untap restriction. Every
        field used here already exists in the spec (GRANT's additive
        TYPES/SUBTYPES/COLOR/DAMAGE_ASSIGNMENT and inverse LOSE, Section
        15.26; CANT/MUST, Section 18/18b) — this is pure composition, not
        a new primitive. Loops so a sentence with more than one such
        trailing clause (a keyword list plus a type-add, say) gets both,
        one strip per iteration. Only the common single-clause shapes are
        recognized; anything else (compound "by more than one creature"/
        "except by"/"unless..." block-restriction filters, "can't attack
        alone", multi-restriction chains) is left in the remainder for an
        honest gap rather than guessed at.

        Returns (remaining_text, extra_grant_lines, sibling_effect_lines).
        """
        extra: list[str] = []
        sibling: list[str] = []
        text = grants_text
        for _ in range(4):
            m = re.search(r'(?:^|,)\s*(?:and\s+)?is goaded$', text)
            if m:
                # GOAD's own default DURATION is "until your next turn"
                # (Section 15.35a) — an explicit DURATION: PERMANENT
                # override is needed for the continuous while-equipped
                # reading these cards need (Section 19.1 documents
                # PERMANENT as a valid explicit value on any effect block).
                sibling += [f"GOAD {{ {binding} DURATION: PERMANENT }}"]
                text = text[:m.start()]
                continue

            m = re.search(r'(?:^|,)\s*(?:and\s+)?is (.+?)'
                           r'(?:\s+in addition to its other (?:colors and )?(?:creature )?types)?$', text)
            if m:
                phrase = m.group(1).strip()
                if phrase in ("every creature type", "all creature types"):
                    extra.append("@CHANGELING")
                    text = text[:m.start()]
                    continue
                words = re.sub(r'^an?\s+', '', phrase).split()
                if words and words[0] in ("white", "blue", "black", "red", "green", "colorless"):
                    extra.append(f'COLOR: ["{_color_word_to_symbol(words[0])}"]')
                    words = words[1:]
                if len(words) == 1 and words[0] in ("artifact", "enchantment", "land", "planeswalker"):
                    extra.append(f'TYPES: ["{words[0].title()}"]')
                    words = []
                elif words:
                    extra.append(f'SUBTYPES: ["{" ".join(w.title() for w in words)}"]')
                text = text[:m.start()]
                continue

            m = re.search(r'(?:^|,)\s*(?:and\s+)?assigns combat damage equal to its toughness '
                           r'rather than its power$', text)
            if m:
                extra.append("DAMAGE_ASSIGNMENT: TOUGHNESS")
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?loses all abilities$", text)
            if m:
                extra.append("LOSE: ALL_ABILITIES")
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?loses (\w+)$", text)
            if m:
                extra.append(f"LOSE: [@{m.group(1).upper()}]")
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?attacks each combat if able$", text)
            if m:
                sibling += [f"MUST {{ {binding} EFFECTS: [ATTACK] }}"]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?blocks each combat if able$", text)
            if m:
                sibling += [f"MUST {{ {binding} EFFECTS: [BLOCK] }}"]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?must be blocked if able$", text)
            if m:
                sibling += [f"MUST {{ ALL EFFECTS: [ BLOCK {{ {binding} }} ] }}"]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?must be blocked by an? (\w+) if able$", text)
            if m:
                sibling += [
                    "MUST {",
                    f'  FILTER {{ TYPE: "{m.group(1).title()}" SELECTION: ALL }}',
                    f"  EFFECTS: [ BLOCK {{ {binding} }} ]",
                    "}",
                ]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?doesn't untap during its controller's untap step$", text)
            if m:
                sibling += [f"CANT {{ {binding} EFFECTS: [UNTAP] }}"]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?can't attack$", text)
            if m:
                sibling += [f"CANT {{ {binding} EFFECTS: [ATTACK] }}"]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?can't block$", text)
            if m:
                sibling += [f"CANT {{ {binding} EFFECTS: [BLOCK] }}"]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?can't be blocked$", text)
            if m:
                sibling += [f"CANT {{ ALL EFFECTS: [ BLOCK {{ {binding} }} ] }}"]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?can't be blocked by creatures with (\w+)$", text)
            if m:
                sibling += [
                    "CANT {",
                    f'  FILTER {{ HAS_KEYWORD: @{m.group(1).upper()} SELECTION: ALL }}',
                    f"  EFFECTS: [ BLOCK {{ {binding} }} ]",
                    "}",
                ]
                text = text[:m.start()]
                continue

            m = re.search(r"(?:^|,)\s*(?:and\s+)?can't be blocked by creatures with power (\d+) or less$", text)
            if m:
                sibling += [
                    "CANT {",
                    f'  FILTER {{ POWER: LTE {m.group(1)} SELECTION: ALL }}',
                    f"  EFFECTS: [ BLOCK {{ {binding} }} ]",
                    "}",
                ]
                text = text[:m.start()]
                continue

            break
        return text, extra, sibling

    def _parse_equipped_condition(self, clause: str, binding: str) -> Optional[str]:
        """
        "as long as <clause>" for a conditional grant's own WHEN/CONDITION
        — self-referential (about the granted-to/self object), about a
        board-state count the controller or an opponent holds, or a
        few other common single-clause shapes. Reuses existing vocabulary
        only: the `.attacking` boolean accessor (Appendix D), and the `IN`
        membership operator against a `FILTER`-scoped collection (Section
        16) to ask "is this object white/a Zombie/etc." without needing a
        dedicated `.color`/`.subtypes` property accessor that doesn't
        exist. Returns None for anything else, so the caller can decline
        the whole composition rather than guess.
        """
        clause = clause.strip()
        if clause in ("it's attacking", "its attacking"):
            return f"{binding}.attacking"
        m = re.match(r"^it'?s (white|blue|black|red|green|colorless)$", clause)
        if m:
            return f'{binding} IN BATTLEFIELD FILTER {{ COLOR: {_color_word_to_symbol(m.group(1))} }}'
        m = re.match(r"^it'?s an? ([\w\s]+)$", clause)
        if m:
            type_word = _singularize(m.group(1)).title()
            return f'{binding} IN BATTLEFIELD FILTER {{ TYPE: "{type_word}" }}'

        def _type_or_color_filter(word: str, category: Optional[str]) -> Optional[str]:
            # "a blue creature"/"a black permanent" (color word + a
            # trailing category noun) needs both COLOR and TYPE; a bare
            # type word ("a Swamp"/"an artifact") needs only TYPE. Any
            # other word+category combination ("a legendary creature") is
            # ambiguous with this simple a word-pair split — decline
            # rather than guess which part is the real TYPE.
            if word in ("white", "blue", "black", "red", "green", "colorless"):
                color = f"COLOR: {_color_word_to_symbol(word)}"
                if category in (None, "permanent"):
                    return color
                if category == "creature":
                    return f'AND {{ {color} TYPE: "Creature" }}'
                return None
            if category is not None:
                return None
            return f'TYPE: "{_singularize(word).title()}"'

        m = re.match(r"^an opponent controls an? (\w+)(?: (\w+))?$", clause)
        if m:
            word, category = m.groups()
            filt = _type_or_color_filter(word, category)
            if filt is not None:
                return f'COUNT(BATTLEFIELD FILTER {{ {filt} CONTROLLER: OPPONENT }}) GTE 1'
        # "you control a/an <Type/color>[ <category>]" — the dominant
        # phrasing for this shape by far (Mire Kavu, Gearsmith Guardian,
        # ...); mirrors the single-branch "as long as you control a/an
        # <Type>" CONDITION already used elsewhere (match_equipped_grant's
        # "has (.+)" branch).
        m = re.match(r"^you control an? (\w+)(?: (\w+))?$", clause)
        if m:
            word, category = m.groups()
            filt = _type_or_color_filter(word, category)
            if filt is not None:
                return f'COUNT(BATTLEFIELD FILTER {{ {filt} CONTROLLER: YOU }}) GTE 1'
        # "you control another <color> creature" — excludes the object
        # itself from the count, same EXCEPT: SELF convention used
        # throughout for "other <Type>s you control" filters.
        m = re.match(r"^you control another (\w+) creature$", clause)
        if m:
            sym = _color_word_to_symbol(m.group(1))
            return (f'COUNT(BATTLEFIELD FILTER {{ TYPE: "Creature" COLOR: {sym} '
                    f'CONTROLLER: YOU EXCEPT: {binding} }}) GTE 1')
        return None

    def _parse_equipped_otherwise(self, clause: str, binding: str) -> Optional[list]:
        """The "otherwise, <clause>" branch of a conditional equipped/enchanted
        pump — either a fallback pump or a simple attack/block restriction.
        Returns None (decline) for anything else."""
        clause = clause.strip()
        m = re.match(r'^gets? ([+\-]\d+)/([+\-]\d+)$', clause)
        if m:
            return [f"GRANT {{ {binding} POWER: {m.group(1)} TOUGHNESS: {m.group(2)} }}"]
        if clause == "can't attack or block":
            return [f"CANT {{ {binding} EFFECTS: [ATTACK] }}", f"CANT {{ {binding} EFFECTS: [BLOCK] }}"]
        if clause == "can't attack":
            return [f"CANT {{ {binding} EFFECTS: [ATTACK] }}"]
        if clause == "can't block":
            return [f"CANT {{ {binding} EFFECTS: [BLOCK] }}"]
        return None

    def _try_conditional_equipped_grant(self, ll: str, binding: str,
                                         prefix: str = r"(?:equipped|enchanted) creature") -> Optional[list]:
        """
        "<prefix> gets +A/+B as long as <condition>[. Otherwise, it
        <otherwise>]." or "<prefix> has <keyword> as long as <condition>."
        — composes a single-branch CONDITION (no "otherwise") or a binary
        CASE (Section 16) when one is present. `prefix` lets the same
        composition serve both equipped/enchanted grants ("Equipped
        creature...") and self-referential static lines ("This
        creature..."), which are otherwise identical shapes with a
        different subject. Declines (returns None) unless every clause
        involved is a shape _parse_equipped_condition/
        _parse_equipped_otherwise/the keyword store recognizes — a partial
        composition that silently dropped a clause would be worse than
        the honest gap the caller falls back to.
        """
        m = re.match(
            rf"^{prefix} gets ([+\-]\d+)/([+\-]\d+) as long as (.+?)"
            r"(?:\.\s*otherwise,?\s*(?:it\s+)?(.+))?$", ll)
        if m:
            pw, tg, when_clause, otherwise_clause = m.groups()
            condition = self._parse_equipped_condition(when_clause, binding)
            if condition is None:
                return None
            then_line = f"GRANT {{ {binding} POWER: {pw} TOUGHNESS: {tg} }}"
            if otherwise_clause:
                else_lines = self._parse_equipped_otherwise(otherwise_clause, binding)
                if else_lines is None:
                    return None
                return [
                    "  STATIC {",
                    "    EFFECT: [",
                    "      CASE {",
                    f"        WHEN: {condition}",
                    f"        THEN: [ {then_line} ]",
                    f"        ELSE: [ {' '.join(else_lines)} ]",
                    "      }",
                    "    ]",
                    "  }",
                ]
            return [
                "  STATIC {",
                f"    CONDITION: {condition}",
                "    EFFECT: [",
                f"      {then_line}",
                "    ]",
                "  }",
            ]

        # "<prefix> has <keyword> as long as <condition>." — a conditional
        # keyword grant rather than a pump (Wingrattle Scarecrow: "This
        # creature has flying as long as you control a blue creature").
        # No "otherwise" variant observed for this shape; not attempted.
        m = re.match(rf"^{prefix} has (\w+) as long as (.+)$", ll)
        if m:
            kw_word, when_clause = m.groups()
            condition = self._parse_equipped_condition(when_clause, binding)
            if condition is None:
                return None
            kw_cdl = self.kw_store.match(kw_word)
            if not kw_cdl:
                return None
            return [
                "  STATIC {",
                f"    CONDITION: {condition}",
                "    EFFECT: [",
                f"      GRANT {{ {binding} {kw_cdl} }}",
                "    ]",
                "  }",
            ]
        return None

    def match_equipped_grant(self, line: str, card: dict) -> list[str]:
        """
        Handle static grants via $equipped or $enchanted binding.
        E.g. "Equipped creature gets +3/+2."
             "Equipped creature has hexproof and haste."
             "Enchanted creature gets +4/+4 and has [ability]."
        """
        ll = line.lower().strip().rstrip('.')
        binding = "$equipped" if ll.startswith("equipped") else "$enchanted"

        # "can't be blocked by <color> creatures" — a bare top-level
        # restriction line (the Scarab cycle's first ability), not a
        # trailing clause on a pump/keyword grant.
        m = re.match(r"^(?:equipped|enchanted) creature can't be blocked by (\w+) creatures$", ll)
        if m:
            sym = _color_word_to_symbol(m.group(1))
            return [
                "  STATIC {",
                "    EFFECT: [",
                "      CANT {",
                f"        FILTER {{ COLOR: {sym} SELECTION: ALL }}",
                f"        EFFECTS: [ BLOCK {{ {binding} }} ]",
                "      }",
                "    ]",
                "  }",
            ]

        conditional = self._try_conditional_equipped_grant(ll, binding)
        if conditional is not None:
            return conditional

        result = [
            "  STATIC {",
            "    EFFECT: [",
            f"      GRANT {{ {binding}",
        ]

        # gets +P/+T
        m = re.match(r'^(?:equipped|enchanted) creature gets ([+\-]\d+)/([+\-]\d+)', ll)
        if m:
            pw, tg = m.group(1), m.group(2)
            result.append(f"        POWER: {pw}")
            result.append(f"        TOUGHNESS: {tg}")
            # Check for "for each X you control" rider
            rider = re.search(r'for each (.+)', ll)
            sibling = []
            if rider:
                result.append(f"        # PER: {rider.group(1)}")
            else:
                # This regex isn't end-anchored, so without checking the
                # tail it silently matched on the P/T prefix alone and
                # dropped any trailing grant entirely, no gap marker at all
                # (Runechanter's Pike-shape cards with the reverse order,
                # "has X and gets Y", are handled by the "has (.+)" branch
                # below instead). The tail may be a plain "and has
                # <keyword-list>", a type/CANT/damage-assignment clause
                # _extract_equipped_grant_extras recognizes, or both at
                # once ("has intimidate, and is a black Zombie").
                tail = ll[m.end():].strip()
                if tail:
                    remaining, extra, sibling = self._extract_equipped_grant_extras(tail, binding)
                    result += [f"        {e}" for e in extra]
                    # Strip a leading ", "/"and " left over from whatever
                    # _extract_equipped_grant_extras stripped off the far
                    # end (its match starts at the delimiter, not past it —
                    # e.g. ", has intimidate, and is a black Zombie" leaves
                    # ", has intimidate" behind, comma and all).
                    cleaned = re.sub(r'^[,\s]*(?:and\s+)?', '', remaining)
                    has_m = re.match(r'^has\s+(.+)$', cleaned)
                    if has_m:
                        for g in self._parse_keyword_grants(has_m.group(1)):
                            result.append(f"        {g}")
                    elif cleaned:
                        # No "has" verb introducing it and nothing else
                        # recognized it — an unhandled shape, not a keyword
                        # list; gap it honestly rather than risk
                        # _parse_keyword_grants shattering it.
                        result.append(f"        # SPEC_GAP: equipped grant trailing clause: {cleaned}")
            result += ["      }"] + [f"      {s}" for s in sibling] + ["    ]", "  }"]
            return result

        # has [keywords and/or abilities][ as long as/if you control a/an
        # <Type>] — the condition scopes the whole STATIC ability (CONDITION
        # field, Section 15.25), not the GRANT itself. Only the single-type
        # "you control a/an <Type>" shape is handled; color-OR ("a black or
        # green permanent") and other compound conditions ("no other
        # creatures", "this creature is enchanted") fall through to an
        # honest gap rather than guess at unprecedented CONDITION syntax.
        m = re.match(r'^(?:equipped|enchanted) creature has (.+)', ll)
        if m:
            grants_text = m.group(1)
            # "<keyword-list> and gets ±N/±N[, where X is <count clause>]" —
            # the reverse order of the "gets...and has" branch above
            # (Runechanter's Pike). Strip the pump tail off before feeding
            # the remainder to _parse_keyword_grants, which otherwise had no
            # way to recognize "gets +x/+0" or "where x is ..." as anything
            # but a (bogus) keyword and produced garbage UNKNOWN_KEYWORD
            # fragments for both.
            pump_lines = []
            pump_m = re.search(
                r'\s+and gets ([+\-]\d+|[+\-]x)/([+\-]\d+|[+\-]x)(?:,?\s+where x is (?:the number of )?(.+))?$',
                grants_text)
            if pump_m:
                pw, tg, count_clause = pump_m.groups()
                grants_text = grants_text[:pump_m.start()]
                if count_clause is None:
                    pump_lines = [f"        POWER: {pw.upper()}", f"        TOUGHNESS: {tg.upper()}"]
                elif ' and ' in count_clause or ' or ' in count_clause:
                    # A compound zone+type clause ("instant and sorcery cards
                    # in your graveyard") — _per_clause_to_filter can't
                    # express a zone AND a type filter together and would
                    # silently drop one of them, so gap this honestly rather
                    # than emit a count expression that's quietly wrong.
                    pump_lines = [f"        # SPEC_GAP: dynamic pump count: {count_clause}"]
                else:
                    count_filter = _per_clause_to_filter(count_clause)
                    pw_line = f"+{count_filter}" if pw.lower() == '+x' else (
                        f"-{count_filter}" if pw.lower() == '-x' else pw.upper())
                    tg_line = f"+{count_filter}" if tg.lower() == '+x' else (
                        f"-{count_filter}" if tg.lower() == '-x' else tg.upper())
                    pump_lines = [f"        POWER: {pw_line}", f"        TOUGHNESS: {tg_line}"]
            condition = None
            cond_m = re.search(r'\s+(?:as long as|if) you control an? (\w+)$', grants_text)
            if cond_m:
                remainder = grants_text[:cond_m.start()]
                # Only treat this as a single CONDITION over the whole grant
                # when there's exactly one condition clause in the sentence
                # — a per-item conditional list ("lifelink if you control a
                # cleric, deathtouch if you control a rogue, ...") would
                # otherwise get one item's condition silently applied to
                # every other item too. Caught by testing against Multiclass
                # Baldric, which has exactly this shape.
                if not re.search(r'\bif\b|\bas long as\b', remainder):
                    type_word = _singularize(cond_m.group(1)).title()
                    condition = f'COUNT(BATTLEFIELD FILTER {{ TYPE: "{type_word}" CONTROLLER: YOU }}) GTE 1'
                    grants_text = remainder
            grants_text, extra, sibling = self._extract_equipped_grant_extras(grants_text, binding)
            result += pump_lines
            result += [f"        {e}" for e in extra]
            grants = self._parse_keyword_grants(grants_text)
            for g in grants:
                result.append(f"        {g}")
            result += ["      }"] + [f"      {s}" for s in sibling] + ["    ]", "  }"]
            if condition:
                result.insert(1, f"    CONDITION: {condition}")
            return result

        # fallback
        result[-1] = f"      # SPEC_GAP: equipped grant: {line}"
        result += ["    ]", "  }"]
        return result

    # ── Replacement effects ───────────────────────────────────────────────────

    def match_replacement(self, line: str, card: dict) -> list[str]:
        """
        Handle replacement effects (rule 614):
          614.1a — "instead"
          614.1b — "skip"
          614.1c — "As this [permanent] enters"
          614.1d — continuous "[Objects] enter tapped"
        """
        ll = line.lower().strip()
        card_name = card.get("name", "").lower()
        short_name = card_name.split(",", 1)[0].strip() if "," in card_name else card_name

        # "As <name> enters, choose a creature type." (Morophon) — CHOOSE
        # with OPTIONS: CREATURE_TYPE, card-scoped via DECLARE for reference
        # by the STATIC grants that key off the chosen type. The entry
        # itself is unmodified (MOVE FROM $event inherits all fields),
        # mirroring the Festercreep "enters with a counter" composition.
        m = re.match(r'^as (?:' + re.escape(card_name) + '|' + re.escape(short_name) +
                     r') enters,? choose a creature type\.?$', ll)
        if m:
            return [
                "  DECLARE: $chosen_type",
                "",
                "  REPLACE {",
                "    EVENT: AS_ETB { FILTER { SELF } } AS $event",
                "    WITH: [",
                "      MOVE FROM $event { }",
                "      CHOOSE AS $chosen_type { OPTIONS: CREATURE_TYPE COUNT: 1 }",
                "    ]",
                "  }",
            ]

        # ── "twice that many" counter doubling ───────────────────────────────
        if "twice that many" in ll and "+1/+1 counter" in ll:
            return [
                "  REPLACE {",
                '    EVENT: AS_COUNTER_ADDED { FILTER { TYPE: "Creature" CONTROLLER: YOU } } AS $event',
                '    CONDITION: $event.counter_type EQ "+1/+1"',
                "    WITH: [",
                '      ADD_COUNTER { $event.target NAME: "+1/+1" COUNT: $event.amount * 2 }',
                "    ]",
                "  }",
            ]

        # ── "twice that many tokens" ──────────────────────────────────────────
        if "twice that many" in ll and "token" in ll:
            return [
                "  REPLACE {",
                "    EVENT: AS_CREATE_TOKEN { FILTER { CONTROLLER: YOU } } AS $event",
                "    WITH: [",
                "      CREATE_TOKEN FROM $event { COUNT: $event.count * 2 }",
                "    ]",
                "  }",
            ]

        # ── "If you would gain life, you gain twice that much life instead" ───
        if "would gain" in ll and "life" in ll and ("twice" in ll or "instead" in ll):
            return [
                "  REPLACE {",
                "    EVENT: AS_GAIN_LIFE { FILTER { YOU } } AS $event",
                "    WITH: [",
                "      GAIN_LIFE { PLAYER: YOU AMOUNT: $event.amount * 2 }",
                "    ]",
                "  }",
            ]

        # ── "This [creature/permanent] enters with a counter on it" (614.1d) ──
        # The entry itself is unmodified (MOVE FROM $event inherits all
        # fields); ADD_COUNTER is the augmentation, mirroring how the
        # existing "twice that many counter doubling" REPLACE above modifies
        # an inherited event rather than replacing it outright.
        m = re.match(r'^this (?:creature|permanent) enters with an? (\+1/\+1|-1/-1|\w+) counters? on it\.?$', ll)
        if m:
            counter_name = m.group(1)
            return [
                "  REPLACE {",
                "    EVENT: AS_ETB { FILTER { SELF } } AS $event",
                "    WITH: [",
                "      MOVE FROM $event { }",
                f'      ADD_COUNTER {{ SELF NAME: "{counter_name}" COUNT: 1 }}',
                "    ]",
                "  }",
            ]

        # ── ETB replacement: "As this [permanent] enters, choose..." ─────────
        if re.match(r'^as (?:this|it)\b', ll):
            return [f"  # SPEC_GAP: ETB replacement: {line}"]

        # ── "If this artifact would enter, discard a land card instead" ──────
        if "would enter" in ll and "instead" in ll:
            return [f"  # SPEC_GAP: zone-entry replacement: {line}"]

        # ── Continuous "[Objects] enter tapped" ──────────────────────────────
        if re.search(r'\benters?(?: the battlefield)? tapped\b', ll) and not ll.startswith("this"):
            return [f"  # SPEC_GAP: continuous ETB replacement: {line}"]

        # ── Generic "instead" fallback ────────────────────────────────────────
        if "instead" in ll:
            return [f"  # SPEC_GAP: replacement (instead): {line}"]

        return [f"  # SPEC_GAP: replacement: {line}"]

    # ── Effect matcher (shared by activated/triggered/spell) ─────────────────

    def match_effect(self, text: str, card: dict, _depth: int = 0) -> list[str]:
        """
        Map a single effect clause to CDL effect block(s).
        Returns list of CDL lines (without outer indentation).
        _depth guards against recursive compound splitting.
        """
        ll = text.lower().strip().rstrip('.')
        card_name = card.get("name", "").lower()

        if not ll:
            return []

        # "Activate only as a sorcery"/"Activate only during your turn" —
        # already captured as DURING on the enclosing ACTIVATED block by
        # _infer_during; a bare restriction clause reaching match_effect on
        # its own (e.g. as a CREATE_TOKEN remainder) is redundant, not an
        # unhandled effect, so it's dropped rather than gapped.
        if re.match(r'^activate only (?:as a sorcery|during your turn)$', ll):
            return []

        # "Investigate" (keyword action, rule 701.30) = "create a Clue
        # token." Reuses the @Clue token definition (Appendix B) already
        # wired for explicit "create a Clue token" phrasing in
        # _match_create_token — this handles the far more common bare
        # keyword-action form instead ("Investigate.", "...investigate.").
        if re.match(r'^investigates?$', ll):
            return ["CREATE_TOKEN { TOKEN: @Clue PLAYER: YOU COUNT: 1 }"]

        # "Return this card from your graveyard to your hand/the battlefield
        # [tapped][ with a/two <counter> counter(s) on it][ attached to that
        # creature]" — a self-reference MOVE (Section 15.6) out of the
        # graveyard; the Unearth-less reanimation-ability idiom shared by
        # many activated/triggered "return this card" abilities. "tapped and
        # attacking" (needs two simultaneous STATE values) and the delayed
        # "...at the beginning of the next end step" variants aren't handled
        # — gap honestly rather than guess at unprecedented STATE/DELAYED
        # composition.
        m = re.match(
            r'^return this card from your graveyard to (your hand|the battlefield)'
            r'( tapped)?'
            r'((?:,? then attach it to that creature)|(?: attached to that creature))?'
            r'(?: with (a|two) ([\w+/\-]+) counters? on it)?$',
            ll, re.IGNORECASE)
        if m:
            dest, tapped, attach_clause, count_word, counter_name = m.groups()
            lines = ["MOVE {", "  SELF", "  ZONE: YOU.graveyard"]
            if dest.lower() == "your hand":
                lines.append("  TO: YOU.hand")
            else:
                lines.append("  TO: BATTLEFIELD")
                if tapped:
                    lines.append("  STATE: TAPPED")
            lines.append("}")
            if attach_clause:
                lines.append("ATTACH { SELF TO: $event }")
            if counter_name:
                n = 2 if count_word == "two" else 1
                lines.append(f'ADD_COUNTER {{ SELF NAME: "{counter_name}" COUNT: {n} }}')
            return lines

        # Guard: don't recurse deeper than 1 level for compound splitting
        def _sub(t: str) -> list[str]:
            return self.match_effect(t, card, _depth + 1) if _depth < 1 else [f"# SPEC_GAP: effect not matched: {t}"]

        # ── CHOICE ────────────────────────────────────────────────────────
        if re.match(r'^choose (one|two|one or more|one or both)', ll):
            return self._match_choice(text, card)

        # ── PREVENT (Section 15.0) ──────────────────────────────────────────
        # Fully specified since before this session but never wired to a
        # single parser pattern (grep for "PREVENT" in this file found
        # nothing before this fix) — 145 lines across the full Scryfall pool
        # were falling into the generic "effect not matched" gap.
        if ll.startswith("prevent "):
            prevented = self._match_prevent(text, card)
            if prevented is not None:
                return prevented

        # "Draw a card if it was attacking. Otherwise, each opponent loses N
        # life." (Zurgo Stormrender) — checked before the generic DRAW
        # patterns below, which would otherwise match just the "draw a
        # card" prefix and silently drop the rest. "it" is the permanent
        # that just left the battlefield ($event, bound directly by the
        # leaves-battlefield event matcher), and its attacking state at the
        # moment of leaving is exactly what CASE/$event.source.attacking
        # captures.
        m = re.match(
            r'^draw a card if it was attacking\.\s*otherwise,\s*each opponent loses (\d+) life\.?$', ll)
        if m:
            return [
                "CASE {",
                "  WHEN: $event.source.attacking",
                "  THEN: [ DRAW { PLAYER: YOU COUNT: 1 } ]",
                f"  ELSE: [ LOSE_LIFE {{ PLAYER: OPPONENTS AMOUNT: {m.group(1)} }} ]",
                "}",
            ]

        # ── DRAW ("that many") ──────────────────────────────────────────────
        # "You may draw that many cards. Do this only once each turn."
        # (Terrasymbiosis) — "that many" is the number of +1/+1 counters
        # just placed, already exposed by this exact event's own CONDITION
        # field ($event.counters."+1/+1"). The "only once each turn" tail
        # is a card-level LIMIT/REPLENISH concern injected elsewhere
        # (assemble_cdl scans the full trigger line for it) — here it's
        # just noise to strip, not a second effect.
        if re.match(r'^draw that many cards\.?\s*(?:do this only once each turn\.?)?$', ll):
            return [f'DRAW {{ PLAYER: YOU COUNT: $event.counters."+1/+1" }}']

        # "Draw that many cards, then you may put a permanent card from
        # your hand onto the battlefield." (The Ur-Dragon) — "that many" is
        # the number of matching attackers from the "one or more <Type> you
        # control attack" event; computed directly via COUNT rather than
        # assumed to be an $event property, since WHEN_ATTACKS's own event
        # accessors (Section 5) don't document one for this shape.
        m = re.match(
            r'^draw that many cards,\s*then you may put a permanent card from your hand onto the battlefield\.?$',
            ll)
        if m:
            return [
                'DRAW { PLAYER: YOU COUNT: COUNT(BATTLEFIELD FILTER { TYPE: "Dragon" CONTROLLER: YOU ATTACKING: TRUE }) }',
                "CHOOSE {",
                "  FROM: YOU.hand",
                "  COUNT: RANGE 0..1",
                "  FILTER { CATEGORY: PERMANENT }",
                "} AS $chosen",
                "MOVE { $chosen TO: BATTLEFIELD }",
            ]

        # "You and that player each draw that many cards." (Xyris) — "that
        # player"/"that many" both resolve to the already-bound damage
        # event ($event.recipient, $event.amount).
        if re.match(r'^you and that player each draw that many cards\.?$', ll):
            return [
                "DRAW { PLAYER: YOU COUNT: $event.amount }",
                "DRAW { PLAYER: $event.recipient COUNT: $event.amount }",
            ]

        m = re.match(r'^(?:you )?draw(s)? (?:a card|(\d+) cards?)(?:\s+and\s+you\s+lose\s+1\s+life)?', ll)
        if m:
            n = int(m.group(2)) if m.group(2) else 1
            lines = [f"DRAW {{ PLAYER: YOU COUNT: {n} }}"]
            if "lose 1 life" in ll:
                lines.append("LOSE_LIFE { PLAYER: YOU AMOUNT: 1 }")
            # "...and each opponent loses N life" (Oloro) — a trailing
            # compound tail the regex above doesn't capture (unanchored,
            # so it was silently dropped with no gap marker).
            opp_lose = re.search(r'and each opponent loses (\d+) life$', ll)
            if opp_lose:
                lines.append(f"LOSE_LIFE {{ PLAYER: OPPONENTS AMOUNT: {opp_lose.group(1)} }}")
            # "..., then discard a/N card(s)" (Sidar Jabari's own draw-then-
            # discard, the classic "loot" idiom) — another trailing
            # compound tail this same unanchored regex was silently
            # dropping.
            discard_m = re.search(r'then discard (?:a card|(\d+|two|three) cards?)$', ll)
            if discard_m:
                dn = _word_to_num(discard_m.group(1)) if discard_m.group(1) else 1
                lines.append(f"DISCARD {{ PLAYER: YOU COUNT: {dn} }}")
            return lines

        # "Draw two cards. You may play an additional land this turn." — compound
        # Catch "draw N cards" as standalone (sub-sentence from compound splitter)
        m = re.match(r'^draw (?:a card|(\w+) cards?)$', ll)
        if m:
            n = _word_to_num(m.group(1)) if m.group(1) else 1
            return [f"DRAW {{ PLAYER: YOU COUNT: {n} }}"]

        # "Target player draws N cards[, then <this card> deals damage to
        # that player equal to the number of cards they've drawn this
        # turn]." (Cerebral Vortex) — the trailing damage clause used to be
        # silently dropped since the regex wasn't anchored past "cards", and
        # $target was referenced with no TARGET {} declaring it anywhere.
        m = re.match(
            r'^target player draws? (\d+|a|two|three) cards?'
            r'(?:,\s*then .+? deals damage to that player equal to the number'
            r"(?:'s worth)? of cards (?:they've|they have) drawn this turn)?\.?$", ll)
        if m:
            n = _word_to_num(m.group(1))
            lines = [
                "TARGET { TARGET_CLASSES: [PLAYER] } AS $target",
                f"DRAW {{ PLAYER: $target COUNT: {n} }}",
            ]
            if "deals damage" in ll:
                lines.append("DAMAGE { $target SOURCE: SELF AMOUNT: $target.cards_drawn_this_turn }")
            return lines

        m = re.match(r'^(?:each|all) opponents? draws? (\d+|a|two|three) cards?', ll)
        if m:
            n = _word_to_num(m.group(1))
            return [f"DRAW {{ PLAYER: OPPONENTS COUNT: {n} }}"]

        # Draw additional card each draw step
        if "draws an additional card" in ll:
            return ["DRAW { PLAYER: SELF.player COUNT: 1 }"]

        # ── DISCARD ───────────────────────────────────────────────────────
        m = re.match(r'^discard (?:your hand|all cards in your hand)', ll)
        if m:
            return ["DISCARD { PLAYER: YOU COUNT: ALL }"]

        m = re.match(r'^then discard (?:a card|(\d+|two|three) cards?)', ll)
        if m:
            n = _word_to_num(m.group(1)) if m.group(1) else 1
            return [f"DISCARD {{ PLAYER: YOU COUNT: {n} }}"]

        m = re.match(r'^discard (?:a card|(\d+|two|three) cards?)(?:s)? at random', ll)
        if m:
            n = _word_to_num(m.group(1)) if m.group(1) else 1
            return [f"DISCARD {{ PLAYER: YOU COUNT: {n} MODE: RANDOM }}"]

        m = re.match(r'^discard (?:a card|(\d+|two|three) cards?)', ll)
        if m:
            n = _word_to_num(m.group(1)) if m.group(1) else 1
            return [f"DISCARD {{ PLAYER: YOU COUNT: {n} }}"]

        m = re.match(r'^target player discards? (?:a card|(\d+|two|three) cards?)', ll)
        if m:
            n = _word_to_num(m.group(1)) if m.group(1) else 1
            return [
                "TARGET { TARGET_CLASSES: [PLAYER] } AS $target",
                f"DISCARD {{ PLAYER: $target COUNT: {n} }}",
            ]

        # ── ADD MANA (spell/side-effect form, Section 15.24) ──────────────
        # "Add {R} for each card in target opponent's hand" — variable count
        m = re.match(r"^add (\{[^}]+\}) for each card in target opponent'?s hand$", ll)
        if m:
            sym = re.search(r'\{[^}]+\}', text.strip().rstrip('.')).group(0)
            return [
                "TARGET { TARGET_CLASSES: [PLAYER] FILTER { OPPONENT } } AS $target",
                f'ADD_MANA {{ PLAYER: YOU AMOUNT: {{ "{sym}" COUNT: COUNT($target.hand) }} }}',
            ]

        # "Add {B}{B}{B}" / "Add {G}{G}{G}" — repeated or mixed symbol literals
        m = re.match(r'^add ((?:\{[^}]+\})+)$', ll)
        if m:
            orig = text.strip().rstrip('.')
            orig_m = re.match(r'^add ((?:\{[^}]+\})+)$', orig, re.IGNORECASE)
            syms = re.findall(r'\{[^}]+\}', orig_m.group(1) if orig_m else m.group(1))
            if len(set(syms)) == 1 and len(syms) > 1:
                amount = f'{{ "{syms[0]}" COUNT: {len(syms)} }}'
            elif len(syms) == 1:
                amount = f'{{ "{syms[0]}" }}'
            else:
                amount = "{ [" + ", ".join(f'"{s}"' for s in syms) + "] }"
            return [f"ADD_MANA {{ PLAYER: YOU AMOUNT: {amount} }}"]

        # ── "half ... rounded down" (Lord Xander pattern, Section 15.30a) ──
        m = re.match(r"^target opponent discards? half the cards in their hand,? rounded down$", ll)
        if m:
            return [
                "TARGET { TARGET_CLASSES: [PLAYER] FILTER { OPPONENT } } AS $target",
                "DISCARD { PLAYER: $target COUNT: FLOOR(COUNT($target.hand) / 2) }",
            ]
        m = re.match(r"^defending player mills half (?:their|its) library,? rounded down$", ll)
        if m:
            return ["MILL { PLAYER: $event.defending COUNT: FLOOR(COUNT($event.defending.library) / 2) }"]
        m = re.match(
            r"^target opponent sacrifices half the nonland permanents they control of their choice,? rounded down$", ll)
        if m:
            return [
                "TARGET { TARGET_CLASSES: [PLAYER] FILTER { OPPONENT } } AS $target",
                "SACRIFICE {",
                "  PLAYER: $target",
                "  FILTER {",
                "    CATEGORY: NONLAND",
                "    CONTROLLER: $target",
                "    SELECTION: FLOOR(COUNT(BATTLEFIELD FILTER { CATEGORY: NONLAND CONTROLLER: $target }) / 2)",
                "  }",
                "}",
            ]

        # ── GOAD (Section 15.35a, v0.56) ────────────────────────────────────
        m = re.match(r'^goad target creature(?: that player controls)?$', ll)
        if m:
            ctrl = " CONTROLLER: $event.recipient" if "that player" in ll else ""
            return [
                f'TARGET {{ TARGET_CLASSES: [PERMANENT] FILTER {{ TYPE: "Creature"{ctrl} }} }} AS $target',
                "GOAD { $target }",
            ]

        # ── SACRIFICE (effect form, Section 15.30a) ────────────────────────
        # Bare "sacrifice it"/"sacrifice self" and "sacrifice a/another
        # <type>[ you control]" — SACRIFICE as an effect primitive (distinct
        # from the ADDL_COST/cost-block form) had zero coverage in this
        # matcher before now, despite being a very common idiom.
        if ll in ("sacrifice it", "sacrifice this creature", "sacrifice self"):
            return ["SACRIFICE { SELF }"]
        m = re.match(r'^sacrifice (another|a|an) (\w+)(?: you control)?$', ll)
        if m:
            article, type_word = m.groups()
            excl = " EXCEPT: SELF" if article == "another" else ""
            if type_word == "permanent":
                type_filter = "CONTROLLER: YOU"
            else:
                type_filter = f'TYPE: "{type_word.title()}" CONTROLLER: YOU'
            return [f"SACRIFICE {{ FILTER {{ {type_filter} SELECTION: 1 }}{excl} }}"]

        # ── COUNTER ───────────────────────────────────────────────────────
        if re.match(r'^counter target (?:noncreature )?(?:spell|instant|sorcery|enchantment|artifact)', ll):
            filter_clause = _build_counter_filter(ll)
            return [
                f"COUNTER {{\n  TARGET {{\n    TARGET_CLASSES: [SPELL]\n    {filter_clause}\n  }} AS $countered\n}}"]

        if re.match(r'^counter target spell', ll):
            return ["COUNTER {\n  TARGET { TARGET_CLASSES: [SPELL] } AS $countered\n}"]

        # ── DESTROY ───────────────────────────────────────────────────────
        m = re.match(
            r"^destroy target ((?:nonland |noncreature |artifact or |"
            r"white |blue |black |red |green )?(?:permanent|creature|artifact|enchantment|planeswalker))",
            ll)
        if m:
            filter_str = _build_destroy_filter(m.group(0), ll)
            # Append MV constraint if present: "with mana value N or greater/less"
            mv = _extract_mv_constraint(ll)
            if mv:
                filter_str += f" {mv}"
            result = []
            if "can't be regenerated" in ll:
                result.append("CANT_REGENERATE { $target DURATION: END_OF_TURN }")
            result.append(
                f"DESTROY {{\n  TARGET {{\n    TARGET_CLASSES: [PERMANENT]\n    FILTER {{ {filter_str} }}\n  }} AS $target\n}}")
            # "That player may search their library for a land card with a
            # basic land type, put it onto the battlefield, then shuffle."
            # (Boseiju's Channel ability) — was silently dropped entirely
            # (no gap marker); this regex only ever matched the DESTROY
            # clause and returned, ignoring anything after the period.
            # "That player" = the destroyed permanent's controller.
            if re.search(r"that player may search (?:their|his or her) library for a land card"
                          r" with a basic land type, put it onto the battlefield, then shuffle", ll):
                result += [
                    "SEARCH {",
                    "  PLAYER: $target.controller",
                    "  ZONE: $target.controller.library",
                    '  FILTER { AND { TYPE: "Basic" TYPE: "Land" } }',
                    "  MAY: TRUE",
                    "} AS $land",
                    "MOVE { $land TO: BATTLEFIELD }",
                ]
            return result

        # "Destroy all artifacts/enchantments/creatures[/permanents]
        #  [with mana value N or greater/less]" — mass, untargeted.
        # NOTE: must extract the MV qualifier even for plain "destroy all
        # creatures" — Austere Command's two creature modes ("mana value 3
        # or less" / "4 or greater") previously both matched the bare
        # "destroy all creatures" prefix check and silently lost the MV
        # split, making both modes emit identical output.
        m = re.match(
            r'^destroy all (artifacts|enchantments|creatures|permanents)'
            r'(?: with mana value (\d+) or (greater|less))?$', ll)
        if m:
            type_word, mv_num, mv_dir = m.groups()
            parts = [] if type_word == "permanents" else [f'TYPE: "{type_word[:-1].title()}"']
            parts.append("SELECTION: ALL")
            if mv_num:
                op = "GTE" if mv_dir == "greater" else "LTE"
                parts.append(f"MV: {op} {mv_num}")
            return [f"DESTROY {{ FILTER {{ {' '.join(parts)} }} }}"]

        if re.match(r'^destroy all nonland permanents your opponents control', ll):
            return ["DESTROY { FILTER { CATEGORY: NONLAND CONTROLLER: OPPONENT SELECTION: ALL } }"]

        # ── EXILE ─────────────────────────────────────────────────────────
        if re.match(r"^exile target player'?s graveyard", ll):
            return [
                "EXILE {",
                "  TARGET { TARGET_CLASSES: [PLAYER] } AS $target",
                "  FILTER { SELECTION: ALL }",
                "  FROM: $target.graveyard",
                "}",
            ]

        # "Exile all artifacts/creatures/enchantments/permanents" — mass, untargeted
        m = re.match(r'^exile all (artifacts|creatures|enchantments|permanents)$', ll)
        if m:
            type_word = m.group(1)
            parts = [] if type_word == "permanents" else [f'TYPE: "{type_word[:-1].title()}"']
            parts.append("SELECTION: ALL")
            return [f"EXILE {{ FILTER {{ {' '.join(parts)} }} }}"]

        if re.match(r'^exile all graveyards$', ll):
            return ["EXILE { FILTER { SELECTION: ALL } FROM: ALL.graveyard }"]

        m = re.match(
            r'^exile target ((?:nonland |noncreature )?(?:permanent|creature|artifact|enchantment|planeswalker))(?: (?:an opponent|you don\'t) controls?)?',
            ll)
        if m:
            filter_str = _build_exile_filter(ll)
            return [
                f"EXILE {{\n  TARGET {{\n    TARGET_CLASSES: [PERMANENT]\n    FILTER {{ {filter_str} }}\n  }} AS $exiled\n}}"]

        if re.match(r'^exile target (?:permanent )?with mana value', ll):
            m2 = re.search(r'mana value (\d+) or greater', ll)
            n = m2.group(1) if m2 else "4"
            return [
                f"EXILE {{\n  TARGET {{\n    TARGET_CLASSES: [PERMANENT]\n    FILTER {{ MV: GTE {n} }}\n  }} AS $exiled\n}}"]

        if re.match(r'^exile target creature card from a graveyard', ll):
            return [
                "EXILE {\n  TARGET {\n    TARGET_CLASSES: [CARD]\n    ZONE: ALL.graveyard\n    FILTER { TYPE: \"Creature\" }\n  } AS $exiled\n  FROM: ALL.graveyard\n}"]

        # ── RETURN TO HAND ────────────────────────────────────────────────
        # Guard on "hand" appearing in the clause — the bare prefix also
        # matches graveyard-reanimation clauses like "return target creature
        # card ... from your graveyard to the battlefield tapped" (Terra,
        # Herald of Hope), which is not a bounce and must not fall through
        # to this MOVE-to-hand pattern.
        m = re.match(r'^return target (nonland )?(permanent|creature)', ll)
        if m and "hand" in ll:
            type_filter = "CATEGORY: NONLAND" if m.group(1) else 'TYPE: "Creature"'
            ctrl = "CONTROLLER: OPPONENT" if "you don't control" in ll or "opponent controls" in ll else ""
            filter_parts = " ".join(filter(None, [type_filter, ctrl]))
            return [
                f"MOVE {{",
                f"  TARGET {{",
                f"    TARGET_CLASSES: [PERMANENT]",
                f"    FILTER {{ {filter_parts} }}",
                f"  }} AS $target",
                f"  TO: $target.owner.hand",
                f"}}",
            ]

        m = re.match(r'^return target (nonland )?permanent (?:you control )?to its owner\'s hand', ll)
        if m:
            ctrl = "CONTROLLER: YOU" if "you control" in ll else ""
            type_f = "CATEGORY: NONLAND" if m.group(1) else "CATEGORY: PERMANENT"
            filter_parts = " ".join(filter(None, [type_f, ctrl]))
            return [
                "MOVE {",
                "  TARGET {",
                "    TARGET_CLASSES: [PERMANENT]",
                f"    FILTER {{ {filter_parts} }}",
                "  } AS $target",
                "  TO: $target.owner.hand",
                "}",
            ]

        # ── MONARCH ──────────────────────────────────────────────────────
        if ll == "you become the monarch":
            return ["BECOME_MONARCH { PLAYER: YOU }"]

        # ── GAIN/LOSE LIFE ────────────────────────────────────────────────
        m = re.match(r'^you gain (\d+|\$\w+) life', ll)
        if m:
            return [f"GAIN_LIFE {{ PLAYER: YOU AMOUNT: {m.group(1)} }}"]

        m = re.match(r'^(?:its controller|target player|that player)(?:\'s controller)? (?:gains|gain) life equal to',
                     ll)
        if m:
            if "power" in ll:
                return ["GAIN_LIFE { PLAYER: $target.controller AMOUNT: $target.power }"]
            return [f"# SPEC_GAP: life gain amount unknown: {text}"]

        m = re.match(r'^you lose (\d+) life', ll)
        if m:
            return [f"LOSE_LIFE {{ PLAYER: YOU AMOUNT: {m.group(1)} }}"]

        m = re.match(r'^(?:each )?opponents? (?:lose|loses) (\d+) life', ll)
        if m:
            return [f"LOSE_LIFE {{ PLAYER: OPPONENTS AMOUNT: {m.group(1)} }}"]

        m = re.match(r'^you lose life equal to', ll)
        if m:
            if "mana value" in ll:
                return ["LOSE_LIFE { PLAYER: YOU AMOUNT: $target.mv }"]
            return [f"# SPEC_GAP: life loss amount: {text}"]

        # ── DAMAGE ────────────────────────────────────────────────────────
        # "Each creature deals N damage to its controller" — mass, per-object source and recipient
        m = re.match(r'^each creature deals (\d+) damage to its controller', ll)
        if m:
            amount = m.group(1)
            return [
                "DAMAGE {",
                '  EACH AS $creature { FILTER { TYPE: "Creature" } }',
                "  $creature.controller",
                "  SOURCE: $creature",
                f"  AMOUNT: {amount}",
                "}",
            ]

        m = re.match(
            r'^(?:[\w\s,-]+ )?deals? (?:(\d+|\$\w+|that much) )?damage (?:to|equal to) (?:any target|each creature|each opponent|each player|target|that permanent|that player|you|the number|' + re.escape(
                card_name) + r')', ll)
        if m:
            return self._match_damage(text, card)

        # ── ADD COUNTER ───────────────────────────────────────────────────
        # Handle compound: "Put counters on X. Those creatures gain Y until EOT."
        # Also handles: "Put N counters on each [type] you control"
        # And: "Put N counters on [Cardname]. It gains X until EOT."
        m = re.match(r'^put (?:a |an? )?(\+1/\+1|-1/-1|charge|lore|page|age|depletion|loyalty|energy) counter on', ll)
        if m:
            counter = m.group(1)
            subj = _infer_counter_subject(ll)
            results = []
            if subj == "$target":
                results.append("TARGET { TARGET_CLASSES: [CREATURE] } AS $target")
            results.append(f'ADD_COUNTER {{ {subj} NAME: "{counter}" }}')
            trailing = re.search(r'\.\s+those creatures gain (.+?) until (end of turn|your next turn)', ll)
            if trailing:
                grant_text = trailing.group(1).strip()
                duration = "END_OF_TURN" if "end of turn" in trailing.group(2) else "YOUR_NEXT_TURN"
                grants = self._parse_keyword_grants(grant_text)
                results += self._render_grant(subj, grants, duration)
            else:
                # "... and draw a card" — a bare compound tail this pattern
                # otherwise silently dropped (only "those creatures gain X"
                # was handled). Confirmed via Korvold, Fae-Cursed King: "put
                # a +1/+1 counter on Korvold and draw a card" was emitting
                # only the ADD_COUNTER with zero gap marker for the DRAW half.
                draw_m = re.search(r'\band (?:you )?draws? (?:a card|(\d+|two|three) cards?)$', ll)
                if draw_m:
                    n = _word_to_num(draw_m.group(1)) if draw_m.group(1) else 1
                    results.append(f"DRAW {{ PLAYER: YOU COUNT: {n} }}")
                # "That creature can't block this turn." (Merciless
                # Javelineer) — another bare compound tail this pattern
                # otherwise silently dropped.
                elif subj == "$target" and re.search(r"\.\s*that creature can't block this turn", ll):
                    results.append("CANT { $target EFFECTS: [BLOCK] DURATION: END_OF_TURN }")
            return results

        # "Put N +1/+1 counters on each [type] you control"
        m = re.match(r'^put (\w+) (\+1/\+1|-1/-1) counters on each (\w+) you control', ll)
        if m:
            n = _word_to_num(m.group(1))
            counter = m.group(2)
            type_word = m.group(3).title()
            return [
                f'ADD_COUNTER {{ FILTER {{ TYPE: "{type_word}" CONTROLLER: YOU SELECTION: ALL }} NAME: "{counter}" COUNT: {n} }}']

        # "Put N +1/+1 counters on [Cardname/this creature]. It gains X until EOT."
        # Match on full card name OR first word of card name (Baylen, etc.)
        card_first_word = re.escape(card_name.split(',')[0].split(' ')[0])
        m = re.match(r'^put (\w+) (\+1/\+1) counters on (?:' + re.escape(
            card_name) + r'|' + card_first_word + r'|this creature|this permanent)', ll)
        if m:
            n = _word_to_num(m.group(1))
            counter = m.group(2)
            results = [f'ADD_COUNTER {{ SELF NAME: "{counter}" COUNT: {n} }}']
            gain_m = re.search(r'\.\s+it gains (.+?) until end of turn', ll)
            if gain_m:
                grants = self._parse_keyword_grants(gain_m.group(1))
                results += self._render_grant("SELF", grants, "END_OF_TURN")
            return results

        m = re.match(r'^put (\d+) (\+1/\+1) counters on', ll)
        if m:
            n, counter = m.group(1), m.group(2)
            subj = _infer_counter_subject(ll)
            results = []
            if subj == "$target":
                results.append("TARGET { TARGET_CLASSES: [CREATURE] } AS $target")
            results.append(f'ADD_COUNTER {{ {subj} NAME: "{counter}" COUNT: {n} }}')
            return results

        # ── SEARCH ────────────────────────────────────────────────────────
        # Note: the descriptor capture allows commas so multi-type lists
        # ("a Plains, Island, Swamp, or Mountain card") reach _match_search
        # instead of silently failing to match at all.
        m = re.match(r'^search your library for(?: up to (\w+))? ([\w\s,]+?) card', ll)
        if m:
            return self._match_search(text, card)

        # ── CREATE TOKEN ──────────────────────────────────────────────────
        m = re.match(r'^create (?:a |an? )?(?:(\d+) )?(.+?) (?:creature )?token', ll)
        if m:
            return self._match_create_token(text, card)

        # ── MILL ──────────────────────────────────────────────────────────
        # Word-form counts ("mill two cards", Terra) weren't accepted —
        # only digits. Trailing text (e.g. "<Name> gains <keyword> until
        # end of turn") is now composed via the same self-reference-gain
        # idiom used elsewhere, or gapped explicitly rather than silently
        # dropped.
        m = re.match(r'^mill (?:a card|(\w+) cards?)\.?\s*(.*)$', ll)
        if m:
            n = _word_to_num(m.group(1)) if m.group(1) else 1
            lines = [f"MILL {{ PLAYER: YOU COUNT: {n} }}"]
            tail = m.group(2)
            if tail:
                card_first = card_name.split(',')[0].split(' ')[0]
                gain_m = re.match(re.escape(card_first) + r' gains (.+?) until end of turn\.?$', tail)
                return_m = re.match(
                    r'then you may return a land card from your graveyard to the battlefield( tapped)?\.?$', tail)
                if gain_m:
                    grants = self._parse_keyword_grants(gain_m.group(1))
                    lines += self._render_grant("SELF", grants, "END_OF_TURN")
                elif return_m:
                    state = " STATE: TAPPED" if return_m.group(1) else ""
                    lines += [
                        "MOVE {",
                        "  TARGET {",
                        "    TARGET_CLASSES: [CARD]",
                        "    ZONE: YOU.graveyard",
                        '    FILTER { TYPE: "Land" }',
                        "  } AS $target",
                        f"  TO: BATTLEFIELD{state}",
                        "  MAY: TRUE",
                        "}",
                    ]
                else:
                    lines.append(f"# SPEC_GAP: effect not matched: {tail}")
            return lines

        # ── UNTAP ─────────────────────────────────────────────────────────
        m = re.match(
            r'^untap (?:up to (\w+) (?:target )?lands?|this (?:artifact|creature|permanent)|each myr you control)', ll)
        if m:
            if "myr" in ll:
                return ['UNTAP { FILTER { TYPE: "Myr" CONTROLLER: YOU SELECTION: ALL } }']
            if "this" in ll:
                return ["UNTAP { SELF }"]
            n_word = m.group(1) or "two"
            n = _word_to_num(n_word)
            return [
                "UNTAP {",
                "  CHOOSE {",
                "    FROM: BATTLEFIELD",
                f"    COUNT: RANGE 0..{n}",
                '    FILTER { TYPE: "Land" CONTROLLER: YOU }',
                "  }",
                "}",
            ]

        # ── PUMP — gets +N/+N [and gains X] until end of turn ───────────────
        # Handles fixed (+2/+2), variable (+X/-X), and "where X is N" forms
        m = re.match(
            r'^(this creature|it|target (?:creature|permanent)|all (?:other )?creatures?(?:[^,]+)?|creatures you control|other creatures you control)\s+gets?\s+([+\-]\d+|[+\-]x)/([+\-]\d+|[+\-]x)',
            ll)
        if m:
            subj_text = m.group(1).strip()
            pw, tg = m.group(2), m.group(3)
            subj = _resolve_pump_subject(subj_text)
            duration = "END_OF_TURN"
            results = []
            if subj == "$target":
                tc = "PERMANENT" if "permanent" in subj_text else "CREATURE"
                results.append(f"TARGET {{ TARGET_CLASSES: [{tc}] }} AS $target")
            results.append(f"GRANT {{ {subj} POWER: {pw.upper()} TOUGHNESS: {tg.upper()} DURATION: {duration} }}")
            gain_m = re.search(r'and (?:gains?|has) (.+?) until end of turn', ll)
            if gain_m:
                grant_clause = gain_m.group(1)
                # "gains your choice of A, B, C, or D" — a choose-one-of-N
                # keyword grant, distinct from a plain comma/and-joined
                # keyword list (which _parse_keyword_grants treats as ALL of
                # them being granted together, not a choice among them).
                choice_m = re.match(r'^your choice of (.+)$', grant_clause)
                if choice_m:
                    raw_opts = [o.strip() for o in re.split(r',\s*(?:or\s+)?|\s+or\s+', choice_m.group(1)) if o.strip()]
                    results.append("CHOICE {")
                    results.append("  PLAYER: YOU")
                    results.append("  TIMING: RESOLUTION")
                    results.append("  COUNT: 1")
                    for opt in raw_opts:
                        kw = self._parse_keyword_grants(opt)
                        if any(g.lstrip().startswith('#') for g in kw):
                            results.append("  OPTION {")
                            results.append("    EFFECT: [")
                            results += self._render_grant(subj, kw, duration, indent="      ")
                            results.append("    ]")
                            results.append("  }")
                        else:
                            results.append(f"  OPTION {{ EFFECT: [ GRANT {{ {subj} {' '.join(kw)} DURATION: {duration} }} ] }}")
                    results.append("}")
                else:
                    grants = self._parse_keyword_grants(grant_clause)
                    results += self._render_grant(subj, grants, duration)
            return results

        # "+X/+X where X is the number of Y" — variable pump
        m = re.match(
            r'^(?:other )?creatures? you control gets? \+x/\+x until end of turn,? where x is (?:the number of )?(.+)',
            ll)
        if m:
            count_clause = m.group(1).strip().rstrip('.')
            count_filter = _per_clause_to_filter(count_clause)
            return [
                f"GRANT {{ FILTER {{ TYPE: \"Creature\" CONTROLLER: YOU SELECTION: ALL }} POWER: +$X TOUGHNESS: +$X DURATION: END_OF_TURN WHERE: $X EQ {count_filter} }}"]

        # ── Player-level counters (ADD_COUNTER applies to a player too) ────
        m = re.match(r'^you get an? (\w+) counter$', ll)
        if m:
            return [f'ADD_COUNTER {{ YOU NAME: "{m.group(1)}" COUNT: 1 }}']
        m = re.match(r'^each player gets an? (\w+) counter$', ll)
        if m:
            return [f'ADD_COUNTER {{ FILTER {{ TARGET_CLASSES: [PLAYER] SELECTION: ALL }} NAME: "{m.group(1)}" COUNT: 1 }}']

        # ── ATTACH (Section 15.30b) ─────────────────────────────────────────
        # "attach up to one target Equipment you control to it"
        m = re.match(r'^attach up to one target equipment you control to (it|self)$', ll)
        if m:
            return [
                "TARGET { TARGET_CLASSES: [PERMANENT] FILTER { TYPE: \"Equipment\" CONTROLLER: YOU } SELECTION: RANGE 0..1 } AS $target",
                "ATTACH { $target TO: SELF }",
            ]
        # "then attach this Equipment to it" — self-attaches to a token
        # created earlier in the same ability (e.g. Ancestral Blade's ETB);
        # $token is bound by the preceding CREATE_TOKEN when this remainder
        # references it.
        if ll in ("attach this equipment to it", "then attach this equipment to it"):
            return ["ATTACH { SELF TO: $token }"]

        # Named subject pump: "Put three +1/+1 counters on [Cardname]. It gains trample until end of turn."
        m = re.match(r'^put (\w+) (\+1/\+1) counters on (?:' + re.escape(card_name) + r'|this creature)', ll)
        if m:
            n = _word_to_num(m.group(1))
            counter = m.group(2)
            results = [f'ADD_COUNTER {{ SELF NAME: "{counter}" COUNT: {n} }}']
            gain_m = re.search(r'\.\s+it gains (.+?) until end of turn', ll)
            if gain_m:
                grants = self._parse_keyword_grants(gain_m.group(1))
                results += self._render_grant("SELF", grants, "END_OF_TURN")
            return results

        # ── GRANT KEYWORDS UNTIL EOT ──────────────────────────────────────
        # "Creatures you control gain deathtouch and lifelink until end of turn"
        # "Target creature gains flying until end of turn"
        # "Until end of turn, target creature gains trample and..."
        m = re.match(
            r'^(?:until end of turn, )?(?:(creatures you control|permanents you control|target (?:creature|permanent)|all creatures|each creature you control|that creature|it))\s+(?:gains?|have)\s+(.+?)\s+until end of turn',
            ll)
        if not m:
            m = re.match(
                r'^until end of turn,?\s+(?:(target (?:creature|permanent)|creatures you control|that creature))\s+(?:gains?|have)\s+(.+)',
                ll)
        if m:
            subj_text = m.group(1).strip()
            grant_text = m.group(2).strip().rstrip('.')
            subj = _resolve_pump_subject(subj_text)
            grants = self._parse_keyword_grants(grant_text)
            results = []
            if subj == "$target":
                tc = "PERMANENT" if "permanent" in subj_text else "CREATURE"
                results.append(f"TARGET {{ TARGET_CLASSES: [{tc}] }} AS $target")
            results += self._render_grant(subj, grants, "END_OF_TURN")
            return results

        # "Gain control of target creature ... until end of turn"
        # ── CHANGE_CONTROL ────────────────────────────────────────────────
        m = re.match(r'^gain control of target (creature|permanent)', ll)
        if m:
            type_str = m.group(1).title()
            ctrl = "CONTROLLER: OPPONENT" if "opponent controls" in ll else ""
            mv = _extract_mv_constraint(ll)
            filter_parts = " ".join(filter(None, [f'TYPE: "{type_str}"', ctrl, mv]))
            duration = "DURATION: END_OF_TURN" if "until end of turn" in ll else ""
            lines = [
                "CHANGE_CONTROL {",
                "  TARGET {",
                "    TARGET_CLASSES: [PERMANENT]",
                f"    FILTER {{ {filter_parts} }}",
                "  } AS $target",
                "  CONTROLLER: YOU",
            ]
            if duration:
                lines.append(f"  {duration}")
            lines.append("}")
            # Untap + haste riders common with gain control
            results = lines
            if "untap that" in ll:
                results = results + ["UNTAP { $target }"]
            if "gains haste" in ll:
                results = results + ["GRANT { $target @HASTE DURATION: END_OF_TURN }"]
            return results

        # "Until end of turn, target creature you control with power N or
        # greater gains <keyword> and 'Whenever this creature deals combat
        # damage to a player, draw a card.'" (Herd Heirloom) — a temporary
        # GRANT carrying a full nested TRIGGERED sub-block, per Section
        # 15.26's own documented capability ("GRANT bodies may contain
        # complete TRIGGERED/ACTIVATED/STATIC blocks").
        m = re.match(
            r'^until end of turn, target creature you control with power (\d+) or greater gains (\w+) and '
            r'"whenever this creature deals combat damage to a player, draw a card\."$', ll)
        if m:
            power_min, keyword = m.groups()
            return [
                f'TARGET {{ TARGET_CLASSES: [PERMANENT] FILTER {{ TYPE: "Creature" CONTROLLER: YOU POWER: GTE {power_min} }} }} AS $target',
                "GRANT {",
                "  $target",
                f"  @{keyword.upper()}",
                "  TRIGGERED {",
                "    WHEN_DAMAGE_DEALT { FILTER { SELF } COMBAT: TRUE TARGET_TYPE: PLAYER }",
                "    EFFECT: [ DRAW { PLAYER: YOU COUNT: 1 } ]",
                "  }",
                "  DURATION: END_OF_TURN",
                "}",
            ]

        # ── REGENERATE ────────────────────────────────────────────────────
        m = re.match(r'^regenerate (this creature|this permanent|target creature|target permanent|\w[\w\s]+)', ll)
        if m:
            subj_text = m.group(1).strip()
            if "this" in subj_text:
                subj = "SELF"
            elif "target" in subj_text:
                subj = "$target"
            else:
                subj = "SELF"
            return [f"REGENERATE {{ {subj} }}"]

        # ── TAP TARGET ────────────────────────────────────────────────────
        m = re.match(r'^tap target (land|creature|permanent|artifact)', ll)
        if m:
            type_str = m.group(1).title()
            ctrl = " CONTROLLER: OPPONENT" if "opponent controls" in ll else ""
            return [
                f'TAP {{ TARGET {{ TARGET_CLASSES: [PERMANENT] FILTER {{ TYPE: "{type_str}"{ctrl} }} }} AS $tapped }}']

        # ── SCRY ──────────────────────────────────────────────────────────
        m = re.match(r'^scry (\d+|x)', ll)
        if m:
            n = m.group(1).upper()
            return [f"@SURVEIL({n})"]

        # ── SURVEIL ───────────────────────────────────────────────────────
        m = re.match(r'^surveil (\d+)', ll)
        if m:
            return [f"@SURVEIL({m.group(1)})"]

        # ── PROLIFERATE ───────────────────────────────────────────────────
        if re.match(r'^proliferate', ll):
            return ["@PROLIFERATE"]

        # ── AMASS (v0.61) ───────────────────────────────────────────────────
        # "amass <Type> N" (Sauron's "amass Orcs 1") — @AMASS is built
        # entirely from existing primitives (Appendix A.2), not a new core
        # primitive, per explicit user direction.
        m = re.match(r'^amass (\w+?)s? (\d+)$', ll)
        if m:
            subtype, n = m.groups()
            return [f'@AMASS({n}, "{subtype.title()}")']

        # ── CANT BE BLOCKED ───────────────────────────────────────────────
        m = re.match(r"^target (\w+) can't be blocked this turn", ll)
        if m:
            type_str = m.group(1).title()
            return [
                "CANT {",
                "  TARGET {",
                f"    TARGET_CLASSES: [PERMANENT]",
                f'    FILTER {{ TYPE: "{type_str}" }}',
                "  } AS $target",
                "  EFFECTS: [BLOCK { $target }]",
                "  DURATION: END_OF_TURN",
                "}",
            ]

        # ── DISTRIBUTE COUNTERS ───────────────────────────────────────────
        # "Distribute N +1/+1 counters among one or two target creatures you control"
        m = re.match(r'^distribute (\w+) (\+1/\+1|-1/-1|\w+) counters? among (one or two|one or more|up to \w+|target)',
                     ll)
        if m:
            count_word, counter, among = m.group(1), m.group(2), m.group(3)
            count = _word_to_num(count_word)
            max_targets = 2 if "two" in among else _word_to_num(among.split()[-1]) if "up to" in among else 1
            ctrl = " CONTROLLER: YOU" if "you control" in ll else ""
            return [
                "DISTRIBUTE {",
                f'  COUNTER: "{counter}"',
                f"  TOTAL: {count}",
                "  TARGETS {",
                "    TARGET_CLASSES: [PERMANENT]",
                f'    FILTER {{ TYPE: "Creature"{ctrl} }}',
                f"    COUNT: RANGE 1..{max_targets}",
                "  }",
                "}",
            ]

        # ── RETURN FROM GRAVEYARD ─────────────────────────────────────────
        # "Return this card from your graveyard to your hand"
        if re.match(r'^return this card from your graveyard to your hand', ll):
            return ["MOVE { SELF FROM: YOU.graveyard TO: YOU.hand }"]

        # "Return target card from your graveyard to your hand" (Eternal Witness)
        if re.match(r'^return target card from your graveyard to your hand', ll):
            return [
                "MOVE {",
                "  TARGET {",
                "    TARGET_CLASSES: [CARD]",
                "    ZONE: YOU.graveyard",
                "  } AS $target",
                "  TO: YOU.hand",
                "}",
            ]

        # "Choose target creature card in your graveyard. If that card's MV
        # is LTE the number of experience counters you have, return it to
        # the battlefield. Otherwise, put it into your hand." (Meren) — a
        # targeted MOVE whose destination is conditional, composed via
        # CASE's own THEN/ELSE effect-list branches (Section 16). Player-
        # level experience counters use the same .counters."<name>"
        # accessor already documented for objects (Appendix D).
        m = re.match(
            r"^choose target creature card in your graveyard\.\s*if that card'?s mana value is"
            r" less than or equal to the number of experience counters you have,\s*"
            r"return it to the battlefield\.\s*otherwise,\s*put it into your hand\.?$", ll)
        if m:
            return [
                'TARGET { TARGET_CLASSES: [CARD] ZONE: YOU.graveyard FILTER { TYPE: "Creature" } } AS $target',
                "CASE {",
                '  WHEN: $target.mv LTE YOU.counters."experience"',
                "  THEN: [ MOVE { $target TO: BATTLEFIELD } ]",
                "  ELSE: [ MOVE { $target TO: YOU.hand } ]",
                "}",
            ]

        # "Return all <Type> creature cards from your graveyard to the
        # battlefield [tapped][, then destroy all <Type>s]." (Zombie
        # Apocalypse) — mass reanimation via MOVE's FILTER field (v0.59,
        # mirrors DESTROY/EXILE's existing mass-effect support).
        m = re.match(
            r'^return all (\w+) creature cards from your graveyard to the battlefield( tapped)?'
            r'(?:,?\s*then destroy all (\w+)s?)?\.?$', ll)
        if m:
            type_word, tapped, destroy_type = m.groups()
            state = " STATE: TAPPED" if tapped else ""
            lines = [
                "MOVE {",
                f'  FILTER {{ AND {{ TYPE: "{type_word.title()}" TYPE: "Creature" }} SELECTION: ALL }}',
                "  ZONE: YOU.graveyard",
                f"  TO: BATTLEFIELD{state}",
                "}",
            ]
            if destroy_type:
                lines.append(f'DESTROY {{ FILTER {{ TYPE: "{_singularize(destroy_type).title()}" SELECTION: ALL }} }}')
            return lines

        # "Return target [creature-type] creature card from a graveyard to
        # the battlefield" — the optional leading word captures a creature-
        # type qualifier ("Knight creature card", Sidar Jabari) alongside
        # the plain "creature card"/"permanent card"/etc. shape.
        m = re.match(
            r'^return target (?:(\w+) )?(creature|permanent|artifact|enchantment) card (?:with .+? )?from (?:a |your )?graveyard to the battlefield',
            ll)
        if m:
            subtype_word, base_type = m.groups()
            type_str = (f'AND {{ TYPE: "{subtype_word.title()}" TYPE: "{base_type.title()}" }}'
                        if subtype_word else f'TYPE: "{base_type.title()}"')
            mv = _extract_mv_constraint(ll)
            # "power X or less" (Ruthless Technomancer) — X here isn't a
            # {X} mana-cost variable, it's however many objects were paid
            # via this same ability's "Sacrifice X <type>s" cost, which
            # binds that collection as $sacrificed (see _parse_cost).
            pw = re.search(r'with power (\d+|x) or (greater|less)', ll)
            if pw:
                amount = "COUNT($sacrificed)" if pw.group(1) == "x" else pw.group(1)
                power = f"POWER: {'GTE' if pw.group(2) == 'greater' else 'LTE'} {amount}"
            else:
                power = ""
            filter_parts = " ".join(filter(None, [type_str, mv, power]))
            ctrl_dest = "$target.owner.control" if "owner's control" in ll else "YOU.control"
            lines = [
                "MOVE {",
                "  TARGET {",
                "    TARGET_CLASSES: [CARD]",
                "    ZONE: ALL.graveyard",
                f"    FILTER {{ {filter_parts} }}",
                "  } AS $target",
                "  TO: BATTLEFIELD",
                f"  CONTROLLER: YOU",
            ]
            if "tapped" in ll:
                lines.append("  STATE: TAPPED")
            lines.append("}")
            return lines

        # "Put target creature card from a graveyard onto the battlefield"
        m = re.match(r'^put target (creature|permanent) card from (?:a |your )?graveyard onto the battlefield', ll)
        if m:
            type_str = m.group(1).title()
            owner = "$target.owner" if "under its owner" in ll else "YOU"
            return [
                "MOVE {",
                "  TARGET {",
                "    TARGET_CLASSES: [CARD]",
                "    ZONE: ALL.graveyard",
                f'    FILTER {{ TYPE: "{type_str}" }}',
                "  } AS $target",
                "  TO: BATTLEFIELD",
                f"  CONTROLLER: {owner}",
                "}",
            ]

        # ── LOOK/REVEAL AT LIBRARY ─────────────────────────────────────────
        # "Look at/Reveal the top N cards of your library. You may put a
        #  [type] card from among them onto the battlefield/into your hand.
        #  Put the rest on the bottom/into your graveyard..."
        m = re.match(r'^(look at|reveal) the top (\w+) cards? of your library', ll)
        if m:
            verb, n_word = m.groups()
            n = _word_to_num(n_word)
            prim = "LOOK" if verb == "look at" else "REVEAL"
            # Only "from among them" (a single group, one optional pick) is
            # handled here — "for each card type, you may put a card of that
            # type from among the revealed cards" (Atraxa) is a distinct
            # multi-select-per-category shape this pattern doesn't attempt to
            # guess at; if the destination clause isn't recognized, fall
            # through rather than emit a MOVE referencing an unbound $chosen.
            dest_m = re.search(
                r'you may (?:put|reveal) (?:a |an? )?([\w\-]+ )?(?:non-human )?(\w+) cards? from among them '
                r'(?:and put it )?(onto the battlefield|into your hand)( tapped)?',
                ll)
            if dest_m:
                type_str = (dest_m.group(2) or "creature").title()
                dest = "BATTLEFIELD" if dest_m.group(3) == "onto the battlefield" else "YOU.hand"
                lines = [
                    f"{prim} {{ FROM: YOU.library COUNT: {n} }} AS $top",
                    "CHOOSE {",
                    "  FROM: $top",
                    "  COUNT: RANGE 0..1",
                    f'  FILTER {{ TYPE: "{type_str}" }}',
                    "} AS $chosen",
                    f"MOVE {{ $chosen TO: {dest} }}",
                ]
                if dest_m.group(4):
                    lines[-1] = f"MOVE {{ $chosen TO: {dest} STATE: TAPPED }}"
                if "bottom of your library" in ll:
                    lines.append("MOVE { $top EXCEPT: $chosen TO: YOU.library LOCATION: BOTTOM ORDER: RANDOM }")
                elif "into your graveyard" in ll and "put the rest" in ll:
                    lines.append("MOVE { $top EXCEPT: $chosen TO: YOU.graveyard }")
                return lines

            # "Look at the top N cards of your library. Put N2 of them/those
            # cards into your hand and the rest/other [cards] on the bottom
            # of your library [in a random/any order]/into your graveyard."
            # — a free pick of N2 cards (no type filter), distinct from the
            # single type-filtered "may put/reveal a <type> card" shape above.
            pick_m = re.search(
                r'put (\w+) of (?:them|those cards) into your hand and the '
                r'(?:rest|other)(?: cards?)? '
                r'(on the bottom of your library(?: in (?:a random|any) order)?|into your graveyard)',
                ll)
            if pick_m:
                n2 = _word_to_num(pick_m.group(1))
                lines = [
                    f"{prim} {{ FROM: YOU.library COUNT: {n} }} AS $top",
                    "CHOOSE {",
                    "  FROM: $top",
                    f"  COUNT: {n2}",
                    "} AS $chosen",
                    "MOVE { $chosen TO: YOU.hand }",
                ]
                if pick_m.group(2).startswith("on the bottom"):
                    lines.append("MOVE { $top EXCEPT: $chosen TO: YOU.library LOCATION: BOTTOM ORDER: RANDOM }")
                else:
                    lines.append("MOVE { $top EXCEPT: $chosen TO: YOU.graveyard }")
                return lines

        # "Reveal/look at that many cards from the top of your library.
        #  Put any number of <Type> cards from among them onto the
        #  battlefield and the rest on the bottom of your library in a
        #  random order." — "that many" is $event.amount (the triggering
        #  event's own count, e.g. combat damage dealt); match_triggered
        #  binds $event when it sees "that many" in the effect text.
        # Anchored end-to-end (not re.search) — Choco's superficially similar
        # "look at that many cards ... You may put one of them into your
        # hand. Then put any number of land cards from among them onto the
        # battlefield tapped and the rest into your graveyard" has an extra
        # pick-one-first step, a tapped destination state, and a different
        # disposal zone, none of which this simpler shape represents; a
        # loose match against it previously silently dropped all three.
        m = re.match(
            r'^(look at|reveal) that many cards from the top of your library\.\s*'
            r'put any number of ([\w\-]+ )?(\w+) cards from among them onto the battlefield'
            r' and the rest on the bottom of your library in a random order$', ll)
        if m:
            verb, qualifier, bare_type = m.groups()
            prim = "LOOK" if verb == "look at" else "REVEAL"
            # Prefer the more specific qualifier when both are present
            # ("Dinosaur creature cards" — the subtype, not the bare
            # "creature" card-type word right before "cards").
            type_str = (qualifier or bare_type or "creature").strip().title()
            return [
                f"{prim} {{ FROM: YOU.library COUNT: $event.amount }} AS $top",
                "CHOOSE {",
                "  FROM: $top",
                "  COUNT: RANGE 0..ALL",
                f'  FILTER {{ TYPE: "{type_str}" }}',
                "} AS $chosen",
                "MOVE { $chosen TO: BATTLEFIELD }",
                "MOVE { $top EXCEPT: $chosen TO: YOU.library LOCATION: BOTTOM ORDER: RANDOM }",
            ]

        # "Reveal cards from the top of your library until you reveal a
        #  <type> card. Put that card onto the battlefield [tapped and
        #  attacking] and the rest on the bottom of your library in a
        #  random order." — REVEAL COUNT: UNTIL (Section 15.18a, v0.54).
        m = re.match(
            r'^reveal cards from the top of your library until you reveal a (\w+) card\.\s*'
            r'put that card onto the battlefield( tapped and attacking)?'
            r' and the rest on the bottom of your library in a random order',
            ll)
        if m:
            type_word, tapped_attacking = m.groups()
            type_str = type_word.title()
            state = " STATE: ATTACKING" if tapped_attacking else ""
            return [
                f'REVEAL {{ FROM: YOU.library COUNT: UNTIL {{ FILTER {{ TYPE: "{type_str}" }} AS $match }} }} AS $revealed',
                f"MOVE {{ $match TO: BATTLEFIELD{state} }}",
                "MOVE { $revealed EXCEPT: $match TO: YOU.library LOCATION: BOTTOM ORDER: RANDOM }",
            ]

        # "Exile the top [N] card(s) of your library. [Until the end of your
        # next turn,] you may play {it|that card|them} [this turn]." —
        # impulse draw. Section 15.34a's own worked example is exactly this
        # shape (EXILE + GRANT { PLAY { } } + DURATION), just never wired to
        # a parser pattern (Jeska's Will, Stella Lee).
        m = re.match(
            r'^exile the top(?: (\w+))? cards? of your library\.\s*'
            r'(?:until the end of your next turn,\s*)?'
            r'you may play (?:it|that card|them)(?: this turn)?\.?$', ll)
        if m:
            count = _word_to_num(m.group(1)) if m.group(1) else 1
            if "until the end of your next turn" in ll:
                duration = "UNTIL { WHEN_END_STEP_BEGIN { FILTER { YOU } } }"
            else:
                duration = "END_OF_TURN"
            return [
                f"EXILE {{ TOP({count}, YOU.library) }} AS $exiled",
                "GRANT {",
                "  $exiled",
                "  PLAY { $exiled }",
                f"  DURATION: {duration}",
                "}",
            ]

        # "You may choose new targets for target spell or ability" (Deflecting
        # Swat) — Section 15.43 ALTER worked example. An empty PROPERTIES
        # TARGET {} means the controller may choose any currently legal
        # targets (including the existing ones), so no separate MAY needed.
        if ll == "you may choose new targets for target spell or ability":
            return [
                "ALTER {",
                "  TARGET { TARGET_CLASSES: [SPELL, ABILITY] } AS $obj",
                "  PROPERTIES {",
                "    TARGET { }",
                "  }",
                "}",
            ]

        # ── COPY SPELL (bare subject, v0.60) ────────────────────────────────
        # "Copy that spell. You may choose new targets for the copy." (Fire
        # Lord Azula) — "that spell" is the already-cast spell that
        # triggered this WHEN_CAST ability, so no new TARGET { } occurs;
        # copies the already-bound $event directly (see the "that spell"
        # auto-bind trigger in match_triggered).
        if re.match(r"^copy that spell\.?\s*(?:you may choose new targets for the copy\.?)?$", ll):
            lines = ["COPY_SPELL {", "  $event"]
            if "you may choose new targets" in ll:
                lines.append("  RETARGET: OPTIONAL")
            lines.append("}")
            return lines

        # ── COPY SPELL ────────────────────────────────────────────────────
        # "Copy target instant or sorcery spell you control. You may choose new targets."
        m = re.match(r'^copy target (instant or sorcery|instant|sorcery|spell) (?:spell )?you control', ll)
        if m:
            type_str = m.group(1)
            if "or" in type_str:
                type_filter = 'TYPE: ["Instant", "Sorcery"]'
            else:
                type_filter = f'TYPE: "{type_str.title()}"'
            new_targets = "  RETARGET: OPTIONAL" if "you may choose new targets" in ll else ""
            lines = [
                "COPY_SPELL {",
                "  TARGET {",
                "    TARGET_CLASSES: [SPELL]",
                f"    FILTER {{ {type_filter} CONTROLLER: YOU }}",
                "  } AS $original",
            ]
            if new_targets:
                lines.append(new_targets)
            lines.append("}")
            return lines

        # ── CREATE COPY TOKEN ─────────────────────────────────────────────
        # "Create a token that's a copy of target creature you control."
        m = re.match(r"^create a token that'?s? a copy of target (creature|permanent)", ll)
        if m:
            type_str = m.group(1).title()
            ctrl = " CONTROLLER: YOU" if "you control" in ll else ""
            lines = [
                "CREATE_TOKEN {",
                f"  COPY_OF: TARGET {{",
                "    TARGET_CLASSES: [PERMANENT]",
                f'    FILTER {{ TYPE: "{type_str}"{ctrl} }}',
                "  } AS $original",
                "}",
            ]
            # "Sacrifice it at the beginning of the next end step"
            if "sacrifice it at the beginning of the next end step" in ll:
                lines += [
                    "DELAYED_TRIGGER {",
                    "  WHEN_END_STEP_BEGIN { ONCE: TRUE }",
                    "  EFFECT: [ SACRIFICE { $token } ]",
                    "}",
                ]
            return lines

        # ── VARIABLE TOKEN CREATE (X = count of type) ─────────────────────
        # "Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control"
        m = re.match(
            r'^create x (\d+)/(\d+) (\w+) (?:\w+ )?creature tokens?,? where x is the number of (\w+) you control', ll)
        if m:
            pw, tg = m.group(1), m.group(2)
            color_word = m.group(3)
            count_type = m.group(4).title()
            color = _color_word_to_symbol(color_word)
            return [
                "CREATE_TOKEN {",
                f"  COLOR: {color}",
                '  TYPES: ["Creature"]',
                f'  SUBTYPES: ["{count_type}"]',
                f"  POWER: {pw}",
                f"  TOUGHNESS: {tg}",
                f'  COUNT: COUNT(BATTLEFIELD FILTER {{ TYPE: "{count_type}" CONTROLLER: YOU }})',
                "}",
            ]

        # ── GAIN LIFE — target player ──────────────────────────────────────
        m = re.match(r'^target player gains (\d+) life', ll)
        if m:
            return [f"GAIN_LIFE {{ PLAYER: $target AMOUNT: {m.group(1)} }}"]

        # ── DRAIN ("deals damage to each opponent and you gain life") ──────
        # "[Cardname/it] deals N damage to each opponent and you gain N
        # life." (Y'shtola — literal digits) or "...deals X damage to each
        # opponent and you gain X life, where X is <count clause>."
        # (Arabella — a shared variable bound to a COUNT expression).
        # Checked before the narrower single-DAMAGE patterns below so the
        # "and you gain life" tail isn't silently dropped.
        short_card_name = re.escape(card_name.split(',')[0].strip())
        m = re.match(
            r'^(?:' + short_card_name + r'|it|this creature|this permanent) deals (\d+) damage to each '
            r'opponent and you gain (\d+) life\.?$', ll)
        if m:
            dmg, life = m.groups()
            return [
                f"DAMAGE {{ OPPONENTS SOURCE: SELF AMOUNT: {dmg} }}",
                f"GAIN_LIFE {{ PLAYER: YOU AMOUNT: {life} }}",
            ]
        m = re.match(
            r'^(?:' + short_card_name + r'|it|this creature|this permanent) deals x damage to each '
            r'opponent and you gain x life, where x is the number of creatures you control with power '
            r'(\d+) or less\.?$', ll)
        if m:
            count_expr = f'COUNT(BATTLEFIELD FILTER {{ TYPE: "Creature" CONTROLLER: YOU POWER: LTE {m.group(1)} }})'
            return [
                f"DAMAGE {{ OPPONENTS SOURCE: SELF AMOUNT: {count_expr} }}",
                f"GAIN_LIFE {{ PLAYER: YOU AMOUNT: {count_expr} }}",
            ]

        # ── DAMAGE — named card or "it" ───────────────────────────────────
        # "[Cardname] deals N damage to each opponent"
        # Also catches "Ragost deals 3 damage" where card_name matches
        m = re.match(r'^(?:' + re.escape(
            card_name) + r'|it|this creature|this permanent|this spell) deals (\d+) damage to (each opponent|each player|target .+)',
                     ll)
        if m:
            amount = m.group(1)
            target_text = m.group(2)
            if "each opponent" in target_text:
                return [f"DAMAGE {{ OPPONENTS SOURCE: SELF AMOUNT: {amount} }}"]
            elif "each player" in target_text:
                return [f"DAMAGE {{ ALL_PLAYERS SOURCE: SELF AMOUNT: {amount} }}"]
            else:
                tc = _target_class_for(target_text)
                return [
                    "DAMAGE {",
                    "  TARGET {",
                    f"    TARGET_CLASSES: [{tc}]",
                    "  } AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]

        # Named card not matching card_name — try loose match on first word of line
        m = re.match(r'^(\w[\w\s,]+?) deals (\d+) damage to (each opponent|each player|any target|target .+)', ll)
        if m:
            amount = m.group(2)
            target_text = m.group(3)
            if "each opponent" in target_text:
                return [f"DAMAGE {{ OPPONENTS SOURCE: SELF AMOUNT: {amount} }}"]
            elif "any target" in target_text:
                return [
                    "DAMAGE {",
                    "  TARGET { TARGET_CLASSES: [CREATURE, PLANESWALKER, PLAYER] } AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]
            elif "attacking or blocking" in target_text:
                return [
                    "DAMAGE {",
                    "  TARGET {",
                    "    TARGET_CLASSES: [CREATURE]",
                    "    FILTER { STATUS: [ATTACKING, BLOCKING] }",
                    "  } AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]
            else:
                tc = _target_class_for(target_text)
                return [
                    "DAMAGE {",
                    "  TARGET { TARGET_CLASSES: [" + tc + "] } AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]

        # "Rin and Seri deals damage to any target equal to the number of Dogs you control.
        #  You gain life equal to the number of Cats you control."
        # Handle compound by splitting sentences
        if re.search(r'deals damage to (?:any target|target .+) equal to the number of', ll):
            sentences = [s.strip() for s in re.split(r'\.\s+', text.strip().rstrip('.')) if s.strip()]
            if len(sentences) > 1:
                results = []
                for s in sentences:
                    results += _sub(s)
                return results
            # Single sentence
            m = re.match(r'^.+ deals damage to (any target|target .+?) equal to the number of (\w+) you control', ll)
            if m:
                target_text = m.group(1)
                count_type = m.group(2).title()
                tc = "CREATURE, PLANESWALKER, PLAYER" if "any target" in target_text else _target_class_for(target_text)
                return [
                    "DAMAGE {",
                    f"  TARGET {{ TARGET_CLASSES: [{tc}] }} AS $dmg_target",
                    "  SOURCE: SELF",
                    f'  AMOUNT: COUNT(BATTLEFIELD FILTER {{ TYPE: "{count_type}" CONTROLLER: YOU }})',
                    "}",
                ]

        # "you gain life equal to the number of [type] you control"
        m = re.match(r'^you gain life equal to the number of (\w+) you control', ll)
        if m:
            count_type = m.group(1).title()
            return [
                f'GAIN_LIFE {{ PLAYER: YOU AMOUNT: COUNT(BATTLEFIELD FILTER {{ TYPE: "{count_type}" CONTROLLER: YOU }}) }}']
        if m:
            count_clause = m.group(1).strip()
            target_clause = m.group(2).strip().rstrip('.')
            count_filter = _per_clause_to_filter(count_clause)
            tc = _target_class_for(f"target {target_clause}")
            return [
                "DAMAGE {",
                "  TARGET {",
                f"    TARGET_CLASSES: [{tc}]",
                "  } AS $dmg_target",
                "  SOURCE: SELF",
                f"  AMOUNT: {count_filter}",
                "}",
            ]

        # "Rin and Seri deals damage to any target equal to the number of Dogs"
        m = re.match(
            r'^(?:' + re.escape(card_name) + r') deals damage to any target equal to the number of (.+?) you control',
            ll)
        if m:
            count_type = m.group(1).strip().title()
            return [
                "DAMAGE {",
                "  TARGET { TARGET_CLASSES: [CREATURE, PLANESWALKER, PLAYER] } AS $dmg_target",
                "  SOURCE: SELF",
                f'  AMOUNT: COUNT(BATTLEFIELD FILTER {{ TYPE: "{count_type}" CONTROLLER: YOU }})',
                "}",
            ]

        # ── COMPOUND: DRAW + DISCARD + CREATE TOKEN ────────────────────────
        # "Draw two cards, then discard a card. Create a 1/1 colorless Pilot token..."
        # Also handles "Draw N cards, then discard a card" as single sentence
        if re.match(r'^draw (?:two|three|\d+) cards?,? (?:then|and) discard', ll) or \
                re.match(r'^draw (?:two|three|\d+) cards?,? then discard', ll):
            sentences = [s.strip() for s in re.split(r'\.\s+', text.strip().rstrip('.')) if s.strip()]
            if len(sentences) > 1:
                results = []
                for s in sentences:
                    results += _sub(s)
                return results
            # Single sentence: "draw N, then discard a card"
            m2 = re.match(r'^draw (\w+) cards?,? (?:then|and) discard (?:a card|(\w+) cards?)', ll)
            if m2:
                draw_n = _word_to_num(m2.group(1))
                disc_n = _word_to_num(m2.group(2)) if m2.group(2) else 1
                return [
                    f"DRAW {{ PLAYER: YOU COUNT: {draw_n} }}",
                    f"DISCARD {{ PLAYER: YOU COUNT: {disc_n} }}",
                ]

        # ── COMPOUND: EXILE + EFFECT ───────────────────────────────────────
        # "Exile target X from a graveyard. Each opponent loses N life."
        if re.match(r'^exile target .+ from (?:a |your )?graveyard', ll):
            sentences = [s.strip() for s in re.split(r'\.\s+', text.strip().rstrip('.')) if s.strip()]
            if len(sentences) > 1:
                results = []
                for s in sentences:
                    results += _sub(s)
                return results
            # Single exile sentence — parse type and zone
            zone = "ALL.graveyard"
            if "land card" in ll:
                type_filter = 'TYPE: "Land"'
            elif "instant or sorcery card" in ll:
                type_filter = 'TYPE: ["Instant", "Sorcery"]'
            elif "creature card" in ll:
                type_filter = 'TYPE: "Creature"'
            else:
                type_filter = 'CATEGORY: CARD'
            return [
                "EXILE {",
                "  TARGET {",
                "    TARGET_CLASSES: [CARD]",
                f"    ZONE: {zone}",
                f"    FILTER {{ {type_filter} }}",
                "  } AS $exiled",
                "}",
            ]

        # ── COMPOUND: DRAW + PUT BACK (Brainstorm) ─────────────────────────
        # "Draw N cards, then put M cards from your hand on top of your library in any order"
        m = re.match(
            r'^draw (\w+) cards?,? then put (\w+) cards? from your hand on top of your library in any order', ll)
        if m:
            draw_n = _word_to_num(m.group(1))
            put_n = _word_to_num(m.group(2))
            return [
                f"DRAW {{ PLAYER: YOU COUNT: {draw_n} }}",
                "CHOOSE AS $chosen {",
                "  FROM: YOU.hand",
                f"  COUNT: {put_n}",
                "}",
                "MOVE {",
                "  $chosen",
                "  TO: YOU.library",
                "  LOCATION: TOP_ANY_ORDER",
                "}",
            ]

        # ── COMPOUND: DRAW + LAND PLAY ─────────────────────────────────────
        # "Draw two cards. You may play an additional land this turn."
        if "draw" in ll and "play an additional land" in ll:
            sentences = [s.strip() for s in re.split(r'\.\s+', text.strip().rstrip('.')) if s.strip()]
            results = []
            for s in sentences:
                sl = s.lower()
                if re.match(r'^draw', sl):
                    results += _sub(s)
                elif "additional land" in sl:
                    results.append("ADDITIONAL_LAND_PLAY { PLAYER: YOU DURATION: END_OF_TURN }")
                else:
                    results += _sub(s)
            return results

        # ── ROLL DIE ──────────────────────────────────────────────────────
        if re.search(r'roll a (?:six-sided |\d+-sided )?die', ll):
            return [f"# SPEC_GAP: ROLL_DIE — no primitive in spec v0.53: {text}"]

        # ── COMPOUND: DRAW + DISCARD (simple) ─────────────────────────────
        # "Draw two cards, then discard a card"  (already handled above but
        #  catch here if it slips through as a single sentence)
        m = re.match(r'^draw (\w+) cards?,?\s+(?:then )?discard (\w+) cards?', ll)
        if m:
            draw_n = _word_to_num(m.group(1))
            disc_n = _word_to_num(m.group(2))
            return [
                f"DRAW {{ PLAYER: YOU COUNT: {draw_n} }}",
                f"DISCARD {{ PLAYER: YOU COUNT: {disc_n} }}",
            ]

        # ── DRAW EQUAL TO TOUGHNESS ───────────────────────────────────────
        # "Draw cards equal to the sacrificed creature's toughness, then discard equal to its power"
        if "draw cards equal to" in ll and "toughness" in ll:
            results = ["DRAW { PLAYER: YOU COUNT: $sacrificed.toughness }"]
            if "discard" in ll and "power" in ll:
                results.append("DISCARD { PLAYER: YOU COUNT: $sacrificed.power }")
            return results

        # ── EACH OPPONENT LOSES / YOU GAIN (paired) ────────────────────────
        # "Each opponent loses 10 life and you gain 10 life"
        m = re.match(r'^each opponent loses (\d+) life and you gain (\d+) life', ll)
        if m:
            return [
                f"LOSE_LIFE {{ PLAYER: OPPONENTS AMOUNT: {m.group(1)} }}",
                f"GAIN_LIFE {{ PLAYER: YOU AMOUNT: {m.group(2)} }}",
            ]

        # ── CHOICES WITHIN ACTIVATED (Breya, Urza's Avenger) ──────────────
        if re.match(r'^choose one', ll):
            return self._match_choice(text, card)

        # ── SURVIVE UNMATCHED ─────────────────────────────────────────────
        return [f"# SPEC_GAP: effect not matched: {text}"]

    def _prevent_subject_filter(self, obj: str, card: dict) -> Optional[str]:
        """A PREVENT TARGET/SOURCE value for a bare (non-"target X") subject
        phrase. PREVENT's own field type (Section 15.0) is a single object
        reference or player descriptor — it has no documented FILTER/
        collection support (unlike DESTROY/EXILE/MOVE) — so collection
        phrases ("creatures you control", "players") return None and the
        caller gaps honestly rather than guess at unprecedented syntax."""
        obj = obj.strip().lower()
        if obj in ("this creature", "this permanent"):
            return "SELF"
        if obj == "enchanted creature":
            subtypes = card.get("subtypes", "").lower()
            return "$enchanted" if "aura" in subtypes else None
        if obj == "you":
            return "YOU"
        return None

    def _match_prevent(self, text: str, card: dict) -> Optional[list[str]]:
        """'Prevent the next N/X damage...'/'Prevent all [combat] damage...'
        -> PREVENT { } (Section 15.0). Returns None for unrecognized shapes
        (multi-target division, "noncombat", collection subjects, compound
        multi-sentence riders) so the caller falls back to an honest gap."""
        ll = text.strip().rstrip('.')

        # "Prevent the next N/X damage that would be dealt to <target> this
        # turn." — a fresh targeting decision, so declare TARGET separately
        # (PREVENT's own TARGET field is a reference, not a sub-block) and
        # reference it, mirroring how other effects in this file bind then
        # reuse $target (e.g. Cerebral Vortex's TARGET-then-DRAW-then-DAMAGE).
        m = re.match(
            r'^prevent the next (\d+|x) damage that would be dealt to (any target|target .+?)(?: this turn)?$',
            ll, re.IGNORECASE)
        if m:
            amount, target_text = m.groups()
            amount = amount.upper() if amount.lower() == 'x' else amount
            tc = ("CREATURE, PLANESWALKER, PLAYER" if "any target" in target_text.lower()
                  else _target_class_for(target_text.lower()))
            return [
                f"TARGET {{ TARGET_CLASSES: [{tc}] }} AS $prevent_target",
                f"PREVENT {{ TARGET: $prevent_target AMOUNT: {amount} }}",
            ]

        # "Prevent all combat damage that would be dealt this turn." — the
        # spec's own worked "Fog pattern" example, verbatim.
        if re.match(r'^prevent all combat damage that would be dealt this turn$', ll, re.IGNORECASE):
            return ["PREVENT { AMOUNT: ALL DURATION: END_OF_COMBAT }"]

        # "Prevent all [combat] damage that would be dealt to and dealt by
        # <subject>[ this turn]." — the oracle-text phrasing elides the
        # repeated subject ("to [X] and dealt by X"), it never actually
        # repeats the noun, so this is matched as its own literal-connector
        # pattern rather than a backreference. Checked before the plainer
        # "to <subject>" pattern below so it isn't shadowed. "combat"
        # narrows via DURATION: END_OF_COMBAT (the shield expires before any
        # later noncombat damage this turn, same trick as the bare Fog
        # pattern above) — "noncombat" has no equivalent trick and is left
        # unmatched.
        m = re.match(
            r'^prevent all (combat |noncombat )?damage that would be dealt to and dealt by '
            r'(.+?)(?: this turn)?$',
            ll, re.IGNORECASE)
        if m:
            combat_qual, obj = m.groups()
            if combat_qual and combat_qual.strip() == "noncombat":
                return None
            subject = self._prevent_subject_filter(obj, card)
            if subject is None:
                return None
            lines = ["PREVENT {", f"  TARGET: {subject}", f"  SOURCE: {subject}", "  AMOUNT: ALL"]
            if combat_qual and combat_qual.strip() == "combat":
                lines.append("  DURATION: END_OF_COMBAT")
            lines.append("}")
            return lines

        # "Prevent all [combat] damage that would be dealt to <subject>[
        # this turn]." — target only, no source restriction.
        m = re.match(
            r'^prevent all (combat |noncombat )?damage that would be dealt to '
            r'(.+?)(?: this turn)?$',
            ll, re.IGNORECASE)
        if m:
            combat_qual, obj = m.groups()
            if combat_qual and combat_qual.strip() == "noncombat":
                return None
            subject = self._prevent_subject_filter(obj, card)
            if subject is None:
                return None
            lines = ["PREVENT {", f"  TARGET: {subject}", "  AMOUNT: ALL"]
            if combat_qual and combat_qual.strip() == "combat":
                lines.append("  DURATION: END_OF_COMBAT")
            lines.append("}")
            return lines

        # "Prevent all [combat] damage that would be dealt by <subject>[
        # this turn]." — no target restriction, only a source restriction.
        m = re.match(
            r'^prevent all (combat |noncombat )?damage that would be dealt by '
            r'(.+?)(?: this turn)?$',
            ll, re.IGNORECASE)
        if m:
            combat_qual, obj = m.groups()
            if combat_qual and combat_qual.strip() == "noncombat":
                return None
            subject = self._prevent_subject_filter(obj, card)
            if subject is None:
                return None
            lines = ["PREVENT {", f"  SOURCE: {subject}", "  AMOUNT: ALL"]
            if combat_qual and combat_qual.strip() == "combat":
                lines.append("  DURATION: END_OF_COMBAT")
            lines.append("}")
            return lines

        return None

    def _match_damage(self, text: str, card: dict) -> list[str]:
        ll = text.lower().strip()
        card_name = card.get("name", "").lower()
        # Short self-reference (oracle text uses the pre-comma name, e.g.
        # "Niv-Mizzet deals..." for "Niv-Mizzet, Parun") — re.escape so a
        # hyphenated name's "-" is treated literally, not as a regex range.
        short_name = re.escape(card_name.split(",")[0].strip())

        # "deals damage to target X equal to..."
        m = re.match(
            r'(?:it )?deals? (?:that much|(\d+)) damage to (target (?:planeswalker|player|creature|opponent)|that player|each opponent|each creature|you)',
            ll)
        if m:
            amount = m.group(1) if m.group(1) else "$event.amount"
            target_text = m.group(2)
            if target_text.startswith("target"):
                return [
                    "DAMAGE {",
                    "  TARGET {",
                    f"    TARGET_CLASSES: [{_target_class_for(target_text)}]",
                    "  } AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]
            elif "each opponent" in target_text:
                return [f"DAMAGE {{ OPPONENTS SOURCE: SELF AMOUNT: {amount} }}"]
            elif "each creature" in target_text:
                return [f'DAMAGE {{ FILTER {{ TYPE: "Creature" SELECTION: ALL }} SOURCE: SELF AMOUNT: {amount} }}']
            else:
                return [f"DAMAGE {{ $target SOURCE: SELF AMOUNT: {amount} }}"]

        # Any named card "deals N damage to each opponent/player/target"
        # (covers Ragost, Rin and Seri, and any named card damage). Matches
        # on the short (pre-comma) name — legendary creatures' oracle text
        # almost always self-references by first name only, not the full
        # "Name, Epithet" string.
        m = re.match(r'^(?:' + short_name +
                     r'|this (?:spell|creature|permanent)) deals (\d+) damage to '
                     r'(each opponent|each player|that permanent or player|that player|target .+|any target)',
                     ll)
        if m:
            amount, target_text = m.group(1), m.group(2)
            if "each opponent" in target_text:
                return [f"DAMAGE {{ OPPONENTS SOURCE: SELF AMOUNT: {amount} }}"]
            elif "each player" in target_text:
                return [f"DAMAGE {{ ALL_PLAYERS SOURCE: SELF AMOUNT: {amount} }}"]
            elif target_text == "that player":
                # Non-targeted — "that player" refers to the already-bound
                # triggering event (e.g. the opponent who drew a card).
                return [f"DAMAGE {{ $event SOURCE: SELF AMOUNT: {amount} }}"]
            elif target_text == "that permanent or player":
                # Non-targeted — the damage-event's own recipient (Ghyrson
                # Starn), distinct from $event itself (the damage source).
                return [f"DAMAGE {{ $event.recipient SOURCE: SELF AMOUNT: {amount} }}"]
            elif "any target" in target_text:
                return [
                    "DAMAGE {",
                    "  TARGET { TARGET_CLASSES: [CREATURE, PLANESWALKER, PLAYER] } AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]
            else:
                tc = _target_class_for(target_text)
                return [
                    "DAMAGE {",
                    f"  TARGET {{ TARGET_CLASSES: [{tc}] }} AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]

        # Loose named card match (any other self-reference not caught
        # above) — "[\w-]" so a hyphenated name ("Niv-Mizzet") is captured
        # as one word instead of stopping at the hyphen.
        m = re.match(r'^([\w-]+) deals (\d+) damage to (each opponent|each player|that player|any target|target .+)', ll)
        if m:
            amount, target_text = m.group(2), m.group(3)
            if "each opponent" in target_text:
                return [f"DAMAGE {{ OPPONENTS SOURCE: SELF AMOUNT: {amount} }}"]
            elif target_text == "that player":
                return [f"DAMAGE {{ $event SOURCE: SELF AMOUNT: {amount} }}"]
            elif "any target" in target_text:
                return [
                    "DAMAGE {",
                    "  TARGET { TARGET_CLASSES: [CREATURE, PLANESWALKER, PLAYER] } AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]
            else:
                tc = _target_class_for(target_text)
                return [
                    "DAMAGE {",
                    f"  TARGET {{ TARGET_CLASSES: [{tc}] }} AS $dmg_target",
                    "  SOURCE: SELF",
                    f"  AMOUNT: {amount}",
                    "}",
                ]

        # "It deals damage equal to the number of <Type> you control to
        # target <Type>." (Stadium Headliner) — same idea as the Rin and
        # Seri COUNT-scaled pattern below, but with the amount clause
        # before the target clause instead of after.
        m = re.match(
            r'^it deals damage equal to the number of (\w+) you control to target (\w+)', ll)
        if m:
            count_type, target_word = m.groups()
            tc = _target_class_for(target_word)
            return [
                "DAMAGE {",
                f"  TARGET {{ TARGET_CLASSES: [{tc}] }} AS $dmg_target",
                "  SOURCE: SELF",
                f'  AMOUNT: COUNT(BATTLEFIELD FILTER {{ TYPE: "{_singularize(count_type).title()}" CONTROLLER: YOU }})',
                "}",
            ]

        # "Rin and Seri deals damage to any target equal to the number of Dogs you control"
        m = re.match(r'^.+ deals damage to (any target|target .+) equal to the number of (\w+) you control', ll)
        if m:
            target_text = m.group(1)
            count_type = m.group(2).title()
            tc = "CREATURE, PLANESWALKER, PLAYER" if "any target" in target_text else _target_class_for(target_text)
            return [
                "DAMAGE {",
                f"  TARGET {{ TARGET_CLASSES: [{tc}] }} AS $dmg_target",
                "  SOURCE: SELF",
                f'  AMOUNT: COUNT(BATTLEFIELD FILTER {{ TYPE: "{count_type}" CONTROLLER: YOU }})',
                "}",
            ]

        # "attacking or blocking creature" target
        if "attacking or blocking creature" in ll:
            m2 = re.search(r'deals (\d+) damage', ll)
            amount = m2.group(1) if m2 else "1"
            return [
                "DAMAGE {",
                "  TARGET {",
                "    TARGET_CLASSES: [CREATURE]",
                "    FILTER { STATUS: [ATTACKING, BLOCKING] }",
                "  } AS $dmg_target",
                "  SOURCE: SELF",
                f"  AMOUNT: {amount}",
                "}",
            ]

        # "deals 13 damage to each creature" (Blasphemous Act)
        m = re.match(r'(?:\w[\w\s]+ )?deals (\d+) damage to each creature', ll)
        if m:
            return [f'DAMAGE {{ FILTER {{ TYPE: "Creature" SELECTION: ALL }} SOURCE: SELF AMOUNT: {m.group(1)} }}']

        return [f"# SPEC_GAP: damage pattern unmatched: {text}"]

    def _match_search(self, text: str, card: dict) -> list[str]:
        ll = text.lower()
        lines = []

        # up to N cards
        up_to = re.search(r'up to (\w+)', ll)
        count_word = up_to.group(1) if up_to else "1"
        count = _word_to_num(count_word)
        count_expr = f"RANGE 0..{count}" if up_to else str(count)

        # determine filter
        filter_parts = []
        if "basic land" in ll:
            filter_parts = ['AND { TYPE: "Basic" TYPE: "Land" }']
        elif "forest" in ll and "plains" not in ll:
            filter_parts = ['TYPE: "Forest"']
        elif "plains, island, swamp, or mountain" in ll:
            filter_parts = ['TYPE: ["Plains", "Island", "Swamp", "Mountain"]']
        elif "land" in ll and "basic" not in ll:
            filter_parts = ['TYPE: "Land"']
        elif "creature" in ll:
            mv_match = re.search(r'mana value (?:x|(\d+)) or less', ll)
            if mv_match:
                mv = "$X" if "x or less" in ll else mv_match.group(1)
                filter_parts = [f'TYPE: "Creature" MV: LTE {mv}']
            else:
                filter_parts = ['TYPE: "Creature"']
        elif "enchantment" in ll:
            mv_match = re.search(r'mana value (\d+) or less', ll)
            if mv_match:
                filter_parts = [f'TYPE: "Enchantment" MV: LTE {mv_match.group(1)}']
            else:
                filter_parts = ['TYPE: "Enchantment"']
        elif "instant or sorcery" in ll:
            filter_parts = ['TYPE: ["Instant", "Sorcery"]']
        elif "artifact or enchantment" in ll:
            filter_parts = ['TYPE: ["Artifact", "Enchantment"]']
        elif re.search(r'artifact or (\w+) card', ll):
            other = re.search(r'artifact or (\w+) card', ll).group(1)
            filter_parts = [f'TYPE: ["Artifact", "{other.title()}"]']
        elif "artifact" in ll:
            filter_parts = ['TYPE: "Artifact"']
        else:
            filter_parts = []

        # destination
        if ("put it onto the battlefield" in ll or "put them onto the battlefield" in ll
                or "put that card onto the battlefield" in ll):
            dest = "BATTLEFIELD"
            state = " STATE: TAPPED" if "tapped" in ll else ""
        elif "put that card into your hand" in ll or "into your hand" in ll:
            dest = "YOU.hand"
            state = ""
        elif "on top" in ll or "put that card on top" in ll:
            dest = "YOU.library"
            state = " LOCATION: TOP"
        elif "put one onto the battlefield" in ll:
            # Split destination (Cultivate pattern — handled as two moves)
            dest = "SPLIT"
            state = ""
        else:
            dest = "YOU.hand"
            state = ""

        reveal = "REVEAL: TRUE" if "reveal" in ll else ""

        filter_str = " ".join(filter_parts)
        search_block = ["SEARCH {"]
        search_block.append(f"  ZONE: YOU.library")
        if count_expr != "1":
            search_block.append(f"  COUNT: {count_expr}")
        if filter_str:
            search_block.append(f"  FILTER {{ {filter_str} }}")
        # Share-a-type constraint (Myriad Landscape pattern)
        if "share a land type" in ll:
            search_block.append("  ACTIVE_SELECTION AS $selections")
            search_block.append("  CONSTRAIN: SHARES_ANY($selections.SUBTYPES)")
        if reveal:
            search_block.append(f"  {reveal}")
        search_block.append("} AS $found")

        lines += search_block

        if dest == "SPLIT":
            # Cultivate: one onto battlefield tapped, one to hand
            lines += [
                "MOVE { $found[0] TO: BATTLEFIELD STATE: TAPPED }",
                "MOVE { $found[1] TO: YOU.hand }",
            ]
        elif dest == "BATTLEFIELD":
            lines.append(f"MOVE {{ $found TO: {dest}{state} }}")
        elif dest == "YOU.library":
            lines.append(f"MOVE {{ $found TO: {dest}{state} }}")
        elif dest == "YOU.hand":
            lines.append(f"MOVE {{ $found TO: YOU.hand }}")

        return lines

    def _match_create_token(self, text: str, card: dict) -> list[str]:
        ll = text.lower()

        # ── Named token types ──────────────────────────────────────────────
        pm = re.match(r"create a number of treasure tokens equal to (\$\w+)'?s power$", ll)
        if pm:
            return [f"CREATE_TOKEN {{ TOKEN: @Treasure PLAYER: YOU COUNT: {pm.group(1)}.power }}"]
        if "treasure token" in ll:
            n = _extract_token_count(ll)
            return [f"CREATE_TOKEN {{ TOKEN: @Treasure PLAYER: YOU COUNT: {n} }}"]
        if "food token" in ll:
            n = _extract_token_count(ll)
            return [f"CREATE_TOKEN {{ TOKEN: @Food PLAYER: YOU COUNT: {n} }}"]
        if "clue token" in ll:
            n = _extract_token_count(ll)
            return [f"CREATE_TOKEN {{ TOKEN: @Clue PLAYER: YOU COUNT: {n} }}"]

        # ── Copy token: "...copy of up to one other target X you control.
        # You get {E}{E}..." (Satya, Aetherflux Genius) — TARGET's own
        # SELECTION: RANGE 0..1 expresses the "up to one" optionality; the
        # energy grant reuses Section 15's own worked "ADD_COUNTER { YOU
        # NAME: "energy" COUNT: 2 }" example verbatim. The trailing delayed
        # sacrifice-unless-pay-energy clause has no established "pay
        # counters as a cost" idiom anywhere in this corpus — gapped
        # explicitly rather than guessed at.
        m = re.match(
            r"create a tapped and attacking token that'?s? a copy of up to one other target nontoken (\w+)"
            r" you control\.?\s*you get \{e\}\{e\} \(two energy counters\)\.?\s*(.*)$", ll)
        if m:
            type_word, tail = m.groups()
            lines = [
                "CREATE_TOKEN {",
                "  COPY_OF: TARGET {",
                "    TARGET_CLASSES: [PERMANENT]",
                f'    FILTER {{ TYPE: "{type_word.title()}" CONTROLLER: YOU CATEGORY: NONTOKEN EXCEPT: SELF }}',
                "    SELECTION: RANGE 0..1",
                "  } AS $original",
                "  STATE: TAPPED_AND_ATTACKING",
                "} AS $token",
                'ADD_COUNTER { YOU NAME: "energy" COUNT: 2 }',
            ]
            if tail:
                lines.append(f"# SPEC_GAP: effect not matched: {tail}")
            return lines

        # ── Copy token: "...copy of it. The token gains haste until end of
        # turn. At the beginning of your next end step, sacrifice it unless
        # you pay <cost>." (Ashling, the Limitless) — "it" = $event.source,
        # the sacrificed permanent (see the "copy of it" auto-bind trigger
        # in match_triggered). The delayed sacrifice-unless-pay reuses the
        # same two-OPTION CHOICE shape as _match_pay_or_effect's "unless
        # that player pays" idiom — one costed OPTION that averts the
        # effect, one uncosted OPTION carrying the fallback SACRIFICE.
        m = re.match(
            r"create a token that'?s? a copy of it\.\s*the token gains haste until end of turn\.\s*"
            r"at the beginning of your next end step,\s*sacrifice it unless you pay "
            r'((?:\{[^}]+\})+)\.?$', ll)
        if m:
            cost = m.group(1).upper()
            return [
                "CREATE_TOKEN { COPY_OF: $event.source } AS $token",
                "GRANT { $token @HASTE DURATION: END_OF_TURN }",
                "DELAYED_TRIGGER {",
                "  WHEN_END_STEP_BEGIN { FILTER { YOU } }",
                "  EFFECT: [",
                "    CHOICE {",
                "      PLAYER: YOU",
                "      TIMING: RESOLUTION",
                "      COUNT: 1",
                f'      OPTION {{ COST: MANA: "{cost}" }}',
                "      OPTION { EFFECT: [ SACRIFICE { $token } ] }",
                "    }",
                "  ]",
                "}",
            ]

        # ── Copy token: "Create a token that's a copy of it[, except ...]" ──
        # (Miirym, Sentinel Wyrm) — "it" is the entering permanent that
        # caused this ETB trigger, i.e. $event.source (see the "copy of it"
        # auto-bind trigger in match_triggered). COPY_OF's own override
        # semantics (Section 15.33 — "any additional field overrides the
        # corresponding copied value") handle "except the token isn't
        # legendary" directly: SUPERTYPES here fully replaces the copied
        # supertype list rather than merging with it.
        m = re.match(r"create a token that'?s? a copy of it(?:, except (.+))?$", ll)
        if m:
            lines = ["CREATE_TOKEN {", "  COPY_OF: $event.source"]
            exc = m.group(1)
            if exc and "isn't legendary" in exc:
                lines.append('  SUPERTYPES: []')
            lines.append("} AS $token")
            return lines

        # ── Copy token: "Create a token that's a copy of enchanted X" ──────
        # (Mechanized Production) — $enchanted is bound by the card's own
        # "Enchant <object descriptor>" declaration line.
        m = re.match(r"create a token that'?s? a copy of enchanted \w+\.?\s*(then .+)?$", ll)
        if m:
            lines = ["CREATE_TOKEN { COPY_OF: $enchanted } AS $token"]
            # "Then if you control N or more artifacts with the same name as
            # one another, you win the game." — no WIN_GAME primitive
            # exists in the spec (alternate win conditions aren't modeled
            # yet); gap it explicitly rather than silently drop it.
            if m.group(1):
                lines.append(f"# SPEC_GAP: effect not matched: {m.group(1)}")
            return lines

        # ── Copy token: "Create a token that's a copy of target creature" ──
        m = re.match(r"create a token that'?s? a copy of target (\w+)", ll)
        if m:
            type_str = m.group(1).title()
            ctrl = " CONTROLLER: YOU" if "you control" in ll else ""
            lines = [
                "CREATE_TOKEN {",
                "  COPY_OF: TARGET {",
                "    TARGET_CLASSES: [PERMANENT]",
                f'    FILTER {{ TYPE: "{type_str}"{ctrl} }}',
                "  } AS $original",
                "}",
            ]
            if "sacrifice it at the beginning of the next end step" in ll:
                lines += [
                    "DELAYED_TRIGGER {",
                    "  WHEN_END_STEP_BEGIN { ONCE: TRUE }",
                    "  EFFECT: [ SACRIFICE { $token } ]",
                    "}",
                ]
            return lines

        # ── X-variable token: "Create X 1/1 red Goblin tokens, where X is..." ──
        m = re.match(r'^create x (\d+)/(\d+) (\w+) (?:\w+ )?(?:creature )?tokens?,? where x is (?:the number of )?(.+)',
                     ll)
        if m:
            pw, tg = m.group(1), m.group(2)
            color_word = m.group(3)
            count_clause = m.group(4).strip().rstrip('.')
            # Subtype from count clause (last word before "you control")
            subtype_m = re.match(r'(\w+)s? you control', count_clause)
            subtype = subtype_m.group(1).title() if subtype_m else ""
            color = _color_word_to_symbol(color_word)
            count_filter = _per_clause_to_filter(count_clause)
            token_lines = [
                "CREATE_TOKEN {",
                f"  COLOR: {color}",
                '  TYPES: ["Creature"]',
            ]
            if subtype:
                token_lines.append(f'  SUBTYPES: ["{subtype}"]')
            token_lines += [
                f"  POWER: {pw}",
                f"  TOUGHNESS: {tg}",
                f"  COUNT: {count_filter}",
                "}",
            ]
            return token_lines

        # ── Standard inline token: "a 1/1 blue Faerie creature token with flying" ──
        m = re.match(
            r'create (?:a |an? )?(?:(\w+) )?'  # count word
            r'(?:(tapped and attacking) )?'  # state
            r'(\d+)/(\d+) '  # P/T
            r'(white|blue|black|red|green|colorless|white and blue)? ?'  # color
            r'([\w\s]+?) '  # subtype(s) — greedy but stops before token
            r'(?:artifact )?creature tokens?'
            r'(?: with ("[^"]*"|[^.]+))?',  # abilities — a quoted ability
            # description ("This token gets +1/+1...") is tried first so an
            # internal period inside the quotes doesn't end the match early;
            # otherwise stop at the first sentence boundary, since a
            # trailing period starts an unrelated clause (e.g. Slime Against
            # Humanity's "...with trample. Put X +1/+1 counters on it,
            # where X is..." is NOT part of the "with" ability list — a
            # greedy .+ here previously swallowed the whole rest of the
            # line as bogus keyword fragments).
            ll
        )
        if m:
            count_word = m.group(1) or "1"
            state = m.group(2) or ""
            power, toughness = m.group(3), m.group(4)
            color_word = (m.group(5) or "").strip()
            subtype_raw = (m.group(6) or "").strip()
            abilities = m.group(7) or ""

            count = _word_to_num(count_word) if count_word.isalpha() else int(count_word) if count_word.isdigit() else 1
            color_sym = _color_word_to_symbol(color_word) if color_word else "COLORLESS"
            # Subtype may be multi-word: "Astartes Warrior"
            subtype = subtype_raw.title() if subtype_raw else ""

            token_lines = [
                "CREATE_TOKEN {",
                f"  COLOR: {color_sym}",
                '  TYPES: ["Creature"]',
            ]
            if subtype:
                token_lines.append(f'  SUBTYPES: ["{subtype}"]')
            token_lines += [
                f"  POWER: {power}",
                f"  TOUGHNESS: {toughness}",
            ]
            if count != 1:
                token_lines.append(f"  COUNT: {count}")
            if state:
                token_lines.append(f"  STATE: {state.upper().replace(' AND ', '_AND_')}")
            post_lines = []
            if abilities:
                # A quoted clause is a full granted ability description, not
                # a keyword list — _parse_keyword_grants would otherwise
                # emit a bogus UNKNOWN_KEYWORD for the entire sentence.
                quoted_m = re.match(r'^"(.+)"$', abilities.strip())
                if quoted_m:
                    inner = quoted_m.group(1)
                    pump_m = re.match(r'^this token gets ([+\-]\d+)/([+\-]\d+) for each (\w+) you control\.?$', inner)
                    if pump_m:
                        pw, tg, type_word = pump_m.groups()
                        # Bare GRANT with no DURATION defaults to permanent
                        # (Section 15 preamble) — correct here since the
                        # bonus must keep recalculating continuously, not
                        # apply once at creation. Kept as a sibling EFFECT
                        # entry, not merged into CREATE_TOKEN's own base-stat
                        # fields (POWER/TOUGHNESS there are the token's fixed
                        # definition, not a delta).
                        count_expr = f'COUNT(BATTLEFIELD FILTER {{ TYPE: "{type_word.title()}" CONTROLLER: YOU }})'
                        grant_parts = []
                        if pw != "+0":
                            grant_parts.append(f"POWER: +{count_expr}")
                        if tg != "+0":
                            grant_parts.append(f"TOUGHNESS: +{count_expr}")
                        if grant_parts:
                            post_lines.append(f"GRANT {{ $token {' '.join(grant_parts)} }}")
                    else:
                        token_lines.append(f"  # SPEC_GAP: token ability not matched: {inner}")
                else:
                    ab = abilities.strip().rstrip('.')
                    # Trailing token NAME clause: "with flying named
                    # Butterfly" — the token's flavor name (CREATE_TOKEN's
                    # own documented NAME field), not part of its ability
                    # list. Previously swallowed whole into the keyword
                    # capture, producing "flying named butterfly" as one
                    # bogus UNKNOWN_KEYWORD fragment.
                    name_m = re.search(r'\s+named (\w+)$', ab, re.IGNORECASE)
                    token_name = None
                    if name_m:
                        token_name = name_m.group(1).title()
                        ab = ab[:name_m.start()]
                    # Trailing entry-state clause: "with flying that's
                    # tapped and attacking that player" / "...that's
                    # attacking" / "...that's blocking that creature" —
                    # describes how the token enters the battlefield, same
                    # STATE field the prefix "tapped and attacking" form
                    # above already uses, not part of the ability list.
                    state_m = re.search(
                        r"\s+that'?s? (tapped and attacking|tapped|attacking|blocking)"
                        r"(?: (?:that |the )?(?:player|creature))?$",
                        ab, re.IGNORECASE)
                    trailing_state = None
                    if state_m:
                        trailing_state = state_m.group(1).upper().replace(' AND ', '_AND_')
                        ab = ab[:state_m.start()]
                    if trailing_state and not state:
                        token_lines.append(f"  STATE: {trailing_state}")
                    if token_name:
                        token_lines.append(f'  NAME: "{token_name}"')
                    grants = self._parse_keyword_grants(ab) if ab.strip() else []
                    for g in grants:
                        token_lines.append(f"  {g}")
            token_lines.append("} AS $token" if post_lines else "}")
            token_lines += post_lines
            # A trailing sentence after the token clause is a separate
            # effect (e.g. Slime Against Humanity's "...with trample. Put X
            # +1/+1 counters on it, where X is...") — recurse into it rather
            # than silently dropping it now that the "with" capture above no
            # longer swallows it by accident.
            remainder = text[m.end():].strip()
            remainder = re.sub(r'^[.,]\s*(?:then\s+)?', '', remainder).strip()
            if remainder:
                # Bind the created token as $token so the remainder can
                # reference it (e.g. "then attach this Equipment to it").
                if re.search(r'\bit\b|\bthem\b', remainder.lower()):
                    token_lines[-1] = "} AS $token"
                return token_lines + self.match_effect(remainder, card, 1)
            return token_lines

        return [f"# SPEC_GAP: create token pattern unmatched: {text}"]

    def _match_choice(self, text: str, card: dict) -> list[str]:
        """Generate a CHOICE block stub — full option parsing is complex."""
        return [f"# SPEC_GAP: CHOICE block needed: {text}"]


# ── ETB / ADDL_COST / special header handlers ─────────────────────────────────

def match_etb_choice(line: str, card: dict) -> list[str]:
    """Handle 'As this X enters, choose Y or Z' patterns."""
    ll = line.lower()

    # "As this enchantment enters, choose Sultai or Abzan."
    m = re.match(r'^as this \w+ enters,?\s+choose (.+)', ll)
    if m:
        options_text = m.group(1).rstrip('.')
        options = [o.strip() for o in re.split(r'\s+or\s+', options_text)]
        return [f"# ETB_CHOICE: choose from {options} — requires REPLACE with AS_ETB and CHOICE block"]

    # "As this land enters, you may reveal an X card from your hand. If you don't, this land enters tapped."
    if "you may reveal" in ll:
        return ["# ETB_CHOICE: conditional ETB tapped — requires REPLACE with AS_ETB"]

    # "As this Equipment becomes attached to a creature, choose..."
    if "becomes attached" in ll:
        return ["# ETB_CHOICE: AS_EQUIP choice — requires AS_EQUIP block"]

    return [f"# SPEC_GAP: ETB choice pattern: {line}"]


def match_addl_cost(line: str, card: dict) -> tuple[str, list[str]]:
    """Returns (card-level ADDL_COST field, [any extra lines])."""
    ll = line.lower()

    if "sacrifice an artifact or creature" in ll:
        return (
            '  ADDL_COST: SACRIFICE {\n    FILTER { TYPE: ["Artifact", "Creature"] CONTROLLER: YOU }\n    COUNT: 1\n  }',
            [])
    if "pay x life" in ll:
        return ("  ADDL_COST: PAY_LIFE: $X", [])
    if "sacrifice a creature" in ll:
        return ('  ADDL_COST: SACRIFICE {\n    FILTER { TYPE: "Creature" CONTROLLER: YOU }\n    COUNT: 1\n  }', [])

    return (f"  # SPEC_GAP: ADDL_COST: {line}", [])


# ── Cost parser ───────────────────────────────────────────────────────────────

def _parse_mana_cost(cost_raw: str) -> list[str]:
    """
    Parse the cost portion of a mana ability into CDL cost field(s).

    Cost shapes:
      TAP              {T}                       → ["TAP"]
      MANA+TAP         {1}, {T}                  → ["MANA: \"{1}\"", "TAP"]
      MANA+TAP+SAC     {B}, {T}, Sacrifice ...   → ["MANA: \"{B}\"", "TAP", "SACRIFICE: ..."]
      TAP+REMOVE       {T}, Remove a counter     → ["TAP", "REMOVE_COUNTER: SELF"]
      TAP_CREATURES    Tap N untapped X you ctrl → ["TAP_CREATURES: { COUNT: N ... }"]
      ZERO             {0}                       → ["MANA: \"{0}\""]
    """
    cl = cost_raw.lower().strip()

    # Pure tap
    if cl == '{t}':
        return ['TAP']

    # {T}, Remove a depletion/charge/etc counter
    m = re.match(r'^\{t\},\s*remove (?:a |an? )?(\w+) counter from this (\w+)', cl)
    if m:
        counter = m.group(1)
        return ['TAP', f'REMOVE_COUNTER: SELF NAME: "{counter}"']

    # Tap N untapped X you control (Urza / Baylen pattern)
    m = re.match(r'^tap (?:an? )?(?:(\w+) )?untapped (.+?) you control', cl)
    if m:
        count_word = m.group(1) or "one"
        count = _word_to_num(count_word)
        type_text = m.group(2).strip().title()
        return [f'TAP_CREATURES: {{ COUNT: {count} FILTER {{ TYPE: "{type_text}" CONTROLLER: YOU }} }}']

    # {0}
    if cl == '{0}':
        return ['MANA: "{0}"']

    # Everything else: split on commas and classify each token
    tokens = [t.strip() for t in cost_raw.split(',') if t.strip()]
    lines = []
    for token in tokens:
        tl = token.lower()
        if tl == '{t}':
            lines.append('TAP')
        elif re.match(r'^\{[^}]+\}$', token):
            lines.append(f'MANA: "{token}"')
        elif re.match(r'^sacrifice', tl):
            if 'this' in tl:
                lines.append('SACRIFICE: SELF')
            else:
                m2 = re.match(r'^sacrifice (a|an|one|two|three|four|five|six|\d+) (\w+?)s?\b', tl)
                if m2:
                    count = _word_to_num(m2.group(1))
                    subj = m2.group(2).title()
                else:
                    m2 = re.match(r'^sacrifice (?:a |an? )?(.+)', tl)
                    count = 1
                    subj = m2.group(1).strip().title() if m2 else "Permanent"
                lines.append(
                    f'SACRIFICE: CHOOSE {{ FROM: BATTLEFIELD COUNT: {count} FILTER {{ TYPE: "{subj}" CONTROLLER: YOU }} }}')
        elif re.match(r'^remove', tl):
            m2 = re.match(r'^remove (?:a |an? )?(\w+) counter', tl)
            counter = m2.group(1) if m2 else "charge"
            lines.append(f'REMOVE_COUNTER: SELF NAME: "{counter}"')
        else:
            lines.append(f'# COST_UNKNOWN: {token}')
    return lines if lines else ['# COST: (empty)']


def _parse_mana_output(effect_raw: str) -> tuple[str, list[str], str]:
    """
    Parse the effect portion of a mana ability.
    Returns (add_mana_cdl, side_effects_list, limit_str).

    Output shapes (13):
      SINGLE_SYMBOL         Add {G}.
      MULTI_SYMBOL_FIXED    Add {C}{C}. / Add {B}{R}.
      TWO_COLOR_CHOICE      Add {U} or {B}.
      THREE_COLOR_CHOICE    Add {G}, {W}, or {U}.
      ANY_COLOR             Add one mana of any color.
      ANY_COLOR+RESTRICTION Add one mana of any color. Spend this mana only to...
      ANY_COLOR+SCOPE       Add one mana of any color in [scope].
      ANY_ONE_COLOR         Add N mana of any one color.
      VARIABLE_X            Add X mana in any combination of {U} and/or {R}.
      PER_COUNT             Add {G} for each creature you control.
      SYMBOL_OR_CHOSEN      Add {U} or one mana of the chosen color.
      EXILED_CARD_COLOR     Add one mana of any of the exiled card's colors.
      OPPONENT_LAND_COLOR   Add one mana of any color that a land an opponent controls could produce.
    """
    el = effect_raw.lower().strip()

    # Split into sentences for side-effect extraction
    sentences = [s.strip() for s in re.split(r'\.\s+', effect_raw.strip().rstrip('.')) if s.strip()]
    # Find the Add sentence (first one starting with "Add" or containing "add")
    add_sentence = ""
    other_sentences = []
    for s in sentences:
        if re.match(r'^add\b', s.strip(), re.IGNORECASE):
            add_sentence = s.strip()
        else:
            other_sentences.append(s.strip())

    if not add_sentence:
        # Deathrite pattern: side effect first, Add last
        # "{T}: Exile target land card from a graveyard. Add one mana of any color."
        for s in sentences:
            if 'add' in s.lower() and re.search(r'add\b.+mana', s.lower()):
                add_sentence = s.strip()
                other_sentences = [x for x in sentences if x != s]
                break

    if not add_sentence:
        return (f'"# SPEC_GAP: no Add clause in: {effect_raw}"', [], "")

    al = add_sentence.lower()

    # ── Parse ADD_MANA value ─────────────────────────────────────────────────

    # MULTI_SYMBOL_FIXED: Add {C}{C} / Add {B}{R}{G}
    m = re.match(r'^add\s+(\{[^}]+\}(?:\{[^}]+\})+)', add_sentence, re.IGNORECASE)
    if m:
        syms_str = m.group(1)  # e.g. "{C}{C}" or "{B}{R}"
        add_mana = f'{{ "{syms_str}" }}'

    # SINGLE_SYMBOL: Add {G}
    elif re.match(r'^add\s+\{[^}]+\}\s*$', al):
        sym = re.search(r'\{[^}]+\}', add_sentence).group(0)
        add_mana = f'{{ "{sym}" }}'

    # TWO_COLOR_CHOICE: Add {U} or {B}
    elif re.match(r'^add\s+\{[^}]+\}\s+or\s+\{[^}]+\}', al):
        syms = re.findall(r'\{[^}]+\}', add_sentence)
        add_mana = '{ CHOICE [' + ', '.join(f'"{s}"' for s in syms) + '] }'

    # THREE_COLOR_CHOICE: Add {G}, {W}, or {U}
    elif re.match(r'^add\s+\{[^}]+\},\s*\{[^}]+\},?\s+or\s+\{[^}]+\}', al):
        syms = re.findall(r'\{[^}]+\}', add_sentence)
        add_mana = '{ CHOICE [' + ', '.join(f'"{s}"' for s in syms) + '] }'

    # SYMBOL_OR_CHOSEN: Add {U} or one mana of the chosen color
    elif re.match(r'^add\s+\{[^}]+\}\s+or one mana of the chosen color', al):
        sym = re.search(r'\{[^}]+\}', add_sentence).group(0)
        add_mana = f'{{ CHOICE ["{sym}", $chosen_color] }}'

    # ANY_ONE_COLOR with count: Add two mana of any one color
    elif re.match(r'^add\s+(one|two|three|\d+) mana of any one color', al):
        m2 = re.match(r'^add\s+(\w+) mana of any one color', al)
        count = _word_to_num(m2.group(1)) if m2 else 1
        add_mana = f'{{ ANY_ONE COUNT: {count} }}'

    # VARIABLE_X: Add X mana in any combination of {U} and/or {R}
    elif re.match(r'^add x mana', al):
        syms = re.findall(r'\{[^}]+\}', add_sentence)
        if syms:
            add_mana = '{ X_OF CHOICE [' + ', '.join(f'"{s}"' for s in syms) + '] }'
        else:
            add_mana = '{ X_OF ANY }'

    # PER_COUNT: Add {G} for each X you control
    elif re.match(r'^add\s+\{[^}]+\}\s+for each', al):
        sym = re.search(r'\{[^}]+\}', add_sentence).group(0)
        m2 = re.search(r'for each (.+)', al)
        per_clause = m2.group(1).strip().rstrip('.') if m2 else "permanent you control"
        per_filter = _per_clause_to_filter(per_clause)
        add_mana = f'{{ "{sym}" PER: {per_filter} }}'

    # EXILED_CARD_COLOR: Add one mana of any of the exiled card's colors
    elif 'exiled card' in al:
        add_mana = '{ ANY_OF $exiled.colors }'

    # OPPONENT_LAND_COLOR: Add one mana of any color that a land an opponent controls could produce
    elif 'land an opponent controls' in al:
        add_mana = '{ ANY_OF OPPONENT.lands.producible_colors }'

    # ANY_COLOR+SCOPE: Add one mana of any color in your commander's color identity
    elif re.match(r'^add one mana of any color in', al):
        m2 = re.search(r'in (.+)', al)
        scope = m2.group(1).strip().rstrip('.') if m2 else "any"
        add_mana = f'{{ ANY SCOPE: "{scope}" }}'

    # ANY_COLOR (base — handles both plain and +RESTRICTION cases)
    elif re.match(r'^add one mana of any color', al):
        add_mana = '{ ANY }'

    else:
        add_mana = f'"# SPEC_GAP: unmatched Add clause: {add_sentence}"'

    # ── Parse side effects ───────────────────────────────────────────────────
    side_effects = []
    limit = ""

    for s in other_sentences:
        sl = s.lower().strip()

        # Spending restriction: "Spend this mana only to cast X"
        if sl.startswith('spend this mana only to'):
            m2 = re.match(r'^spend this mana only to (.+)', sl)
            restriction = m2.group(1).strip().rstrip('.') if m2 else s
            # Fold restriction into ADD_MANA annotation rather than side effect
            add_mana = add_mana.rstrip('}').rstrip() + f' RESTRICTION: "{restriction}" }}'
            continue

        # Ability rider: "and that spell can't be countered"
        if "can't be countered" in sl:
            add_mana = add_mana.rstrip('}').rstrip() + ' RIDER: CANT_COUNTER }'
            continue

        # Damage self: "This [permanent] deals N damage to you"
        m2 = re.match(r'^this (?:artifact|land|permanent) deals (\d+) damage to you', sl)
        if m2:
            side_effects.append(f'DAMAGE {{ YOU SOURCE: SELF AMOUNT: {m2.group(1)} }}')
            continue

        # Conditional sacrifice: "If there are no [counter] counters on this [permanent], sacrifice it"
        if re.match(r'^if there are no .+ counters on this', sl):
            m2 = re.search(r'no (.+?) counters', sl)
            counter = m2.group(1) if m2 else "depletion"
            side_effects.append(
                f'SACRIFICE_IF {{ SELF CONDITION: COUNT(SELF.counters."{counter}") EQ 0 }}'
            )
            continue

        # Activation restrictions: "Activate only during your turn and only once each turn"
        if 'activate only' in sl:
            if 'only once each turn' in sl or 'once each turn' in sl:
                limit = "YOUR_TURN LIMIT: 1 REPLENISH: EACH_TURN"
            elif 'during your turn' in sl:
                limit = "YOUR_TURN"
            continue

        # Unhandled sentence — check if it's an action before the mana (Deathrite pattern)
        if re.match(r'^exile target', sl):
            # Parse the exile target type and zone from the sentence directly
            zone = "ALL.graveyard" if "graveyard" in sl else "BATTLEFIELD"
            type_filter = ""
            if re.search(r'land card', sl):
                type_filter = 'TYPE: "Land"'
            elif re.search(r'instant or sorcery card', sl):
                type_filter = 'TYPE: ["Instant", "Sorcery"]'
            elif re.search(r'creature card', sl):
                type_filter = 'TYPE: "Creature"'
            else:
                type_filter = 'CATEGORY: CARD'
            side_effects.append(
                f'EXILE {{\n  TARGET {{\n    TARGET_CLASSES: [CARD]\n    ZONE: {zone}\n    FILTER {{ {type_filter} }}\n  }} AS $exiled\n}}'
            )
            continue

        # Unhandled sentence
        side_effects.append(f'# SPEC_GAP: mana side effect: {s}')

    return (add_mana, side_effects, limit)


def _per_clause_to_filter(clause: str) -> str:
    """
    Convert a 'for each X you control' clause to a CDL COUNT filter expression.
    Examples:
      "creature you control"       → COUNT(BATTLEFIELD FILTER { TYPE: "Creature" CONTROLLER: YOU })
      "artifact you control"       → COUNT(BATTLEFIELD FILTER { TYPE: "Artifact" CONTROLLER: YOU })
      "land you control"           → COUNT(BATTLEFIELD FILTER { TYPE: "Land" CONTROLLER: YOU })
      "permanent you control"      → COUNT(BATTLEFIELD FILTER { CONTROLLER: YOU })
      "card in your graveyard"     → COUNT(YOU.graveyard)
    """
    cl = clause.lower().strip().rstrip('.')

    # Zone-based counts
    if 'in your graveyard' in cl:
        return 'COUNT(YOU.graveyard)'
    if 'in your hand' in cl:
        return 'COUNT(YOU.hand)'
    if 'in exile' in cl:
        return 'COUNT(YOU.exile)'

    # Battlefield type counts
    type_map = {
        'creature': 'TYPE: "Creature"',
        'artifact': 'TYPE: "Artifact"',
        'enchantment': 'TYPE: "Enchantment"',
        'land': 'TYPE: "Land"',
        'planeswalker': 'TYPE: "Planeswalker"',
        'permanent': None,
        'myr': 'TYPE: "Myr"',
    }
    for word, filter_clause in type_map.items():
        if word in cl:
            if filter_clause:
                return f'COUNT(BATTLEFIELD FILTER {{ {filter_clause} CONTROLLER: YOU }})'
            else:
                return 'COUNT(BATTLEFIELD FILTER { CONTROLLER: YOU })'

    # Fallback
    return f'COUNT(BATTLEFIELD FILTER {{ CONTROLLER: YOU }})  # per: {clause}'


def _parse_cost(cost_raw: str) -> list[str]:
    """Convert cost string like '{2}, {T}, Sacrifice this land' to cost block lines."""
    parts = [p.strip() for p in cost_raw.split(',')]
    lines = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.upper() == "{T}" or part.lower() == "tap" or part == "t":
            lines.append("TAP: SELF")
        elif re.match(r'^\{[^}]+(?:\}\{[^}]+)*\}$', part):
            lines.append(f'MANA: "{part}"')
        elif re.match(r'^sacrifice this', part, re.IGNORECASE):
            lines.append("SACRIFICE: SELF")
        elif re.match(r'^[Ss]acrifice X (\w+)', part):
            # "Sacrifice X artifacts" (Ruthless Technomancer) — a variable
            # count, not a literal number/word; the prior branch's count
            # regex doesn't include "X" so this used to fall through to the
            # bare-fallback branch below, which took the whole ".+" after
            # "sacrifice " (no article to strip) as a literal type name —
            # "X Artifacts" — instead of recognizing "X" as a variable.
            # Bind the sacrificed collection as $sacrificed so the ability's
            # own effect can reference COUNT($sacrificed) wherever its text
            # says "X"; RANGE 1..ALL also captures "X can't be 0" directly,
            # since the engine can't select zero objects for a minimum of 1.
            m = re.match(r'^[Ss]acrifice X (\w+?)s?\b', part)
            type_name = m.group(1).title()
            lines.append(
                f'SACRIFICE: CHOOSE {{ FROM: BATTLEFIELD COUNT: RANGE 1..ALL FILTER {{ TYPE: "{type_name}" CONTROLLER: YOU }} }} AS $sacrificed')
        elif re.match(r'^sacrifice (a|an|one|two|three|four|five|six|\d+) (\w+)', part, re.IGNORECASE):
            # "sacrifice a creature", "sacrifice two artifacts",
            # "sacrifice six creatures named X" ...
            m = re.match(r'^sacrifice (a|an|one|two|three|four|five|six|\d+) (\w+?)s?\b', part, re.IGNORECASE)
            count = _word_to_num(m.group(1))
            type_name = m.group(2).title()
            lines.append(
                f'SACRIFICE: CHOOSE {{ FROM: BATTLEFIELD COUNT: {count} FILTER {{ TYPE: "{type_name}" CONTROLLER: YOU }} }}')
        elif re.match(r'^sacrifice (?:a |an |another )?(\w+)', part, re.IGNORECASE):
            # "another" wasn't stripped alongside "a"/"an" — silently
            # became a literal (nonexistent) "Another Creature" type
            # instead of TYPE: "Creature" ... EXCEPT: SELF (Felothar).
            another = bool(re.match(r'^sacrifice another\b', part, re.IGNORECASE))
            m = re.match(r'^sacrifice (?:a |an |another )?(.+)', part, re.IGNORECASE)
            type_name = m.group(1).title() if m else "Permanent"
            excl = " EXCEPT: SELF" if another else ""
            lines.append(
                f'SACRIFICE: CHOOSE {{ FROM: BATTLEFIELD COUNT: 1 FILTER {{ TYPE: "{type_name}" CONTROLLER: YOU }}{excl} }}')
        elif re.match(r'^discard', part, re.IGNORECASE):
            lines.append("DISCARD: SELF")
        elif re.match(r'^pay \d+ life', part, re.IGNORECASE):
            n = re.search(r'\d+', part).group()
            lines.append(f"PAY_LIFE: {n}")
        elif re.match(r'^remove', part, re.IGNORECASE):
            lines.append(f"# COST: {part}")
        else:
            lines.append(f"# COST_UNKNOWN: {part}")
    return lines if lines else ["# COST: (empty)"]


def _infer_during(effect_raw: str, full_line: str) -> Optional[str]:
    """Check if 'Activate only as a sorcery' or 'only during your turn' modifiers exist."""
    ll = full_line.lower()
    if "activate only as a sorcery" in ll:
        return "SORCERY_SPEED"
    if "activate only during your turn" in ll:
        return "YOUR_TURN"
    return None


# ── Trigger splitting ─────────────────────────────────────────────────────────

def _split_trigger(line: str) -> tuple[str, str, str]:
    """Split 'Whenever X, [if condition,] effect' into (trigger, condition_maybe, effect)."""
    ll = line.lower()

    # Find the trigger boundary — first comma after the trigger condition
    # Trigger conditions end at ", you may", ", create", ", draw", ", exile", etc.
    # Use a heuristic: find first comma not inside parens
    depth = 0
    for i, ch in enumerate(line):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ',' and depth == 0:
            trigger = line[:i].strip()
            rest = line[i + 1:].strip()
            return (trigger, "", rest)

    return (line, "", "")


def _extract_condition(text: str, card_name: str = "") -> Optional[str]:
    """Extract 'if X' guard condition from beginning of effect text."""
    ll = text.lower()
    m = re.match(r'^if (an opponent is the monarch|you control a commander|you control a creature|there is no monarch)', ll)
    if m:
        cond_text = m.group(1)
        if cond_text == "an opponent is the monarch":
            return "OPPONENT IS MONARCH"
        if cond_text == "you control a commander":
            return "COUNT(YOU.commanders) GTE 1"
        if cond_text == "you control a creature":
            return 'COUNT(BATTLEFIELD FILTER { TYPE: "Creature" CONTROLLER: YOU }) GTE 1'
        if cond_text == "there is no monarch":
            return "COUNT(ALL FILTER { is_monarch: TRUE }) EQ 0"

    # "if a player lost N or more life this turn" (Y'shtola) — reuses the
    # already-documented $player.life_lost_this_turn accessor (Section 13)
    # and the ALL-as-player-collection idiom already used for the
    # no-monarch check above.
    lm = re.match(r'^if a player lost (\d+) or more life this turn\b', ll)
    if lm:
        return f'COUNT(ALL FILTER {{ life_lost_this_turn: GTE {lm.group(1)} }}) GTE 1'

    # "if <Name> is in the command zone or on the battlefield" / "if <Name>
    # is in the command zone" (Eminence idiom) — self-reference by short
    # name, per the v0.56 .zone accessor + CONDITION OR() (user-approved).
    cl = card_name.lower()
    if cl:
        # Self-reference is some prefix of the full name ("Sidar Jabari" for
        # "Sidar Jabari of Zhalfir", "Edgar" for "Edgar Markov") — rather
        # than guess which prefix length is "the" short name, extract
        # whatever name-like phrase appears before "is in the command
        # zone" and verify it's actually a prefix of this card's name.
        cz_m = re.match(r'^if (.+?) is in the command zone( or on the battlefield)?\b', ll)
        if cz_m and cl.startswith(cz_m.group(1).strip()):
            if cz_m.group(2):
                return "OR(SELF.zone EQ COMMAND_ZONE, SELF.zone EQ BATTLEFIELD)"
            return "SELF.zone EQ COMMAND_ZONE"
    return None


def _strip_condition(text: str) -> str:
    ll = text.lower()
    m = re.match(r'^if [^,]+,\s*', ll)
    if m:
        return text[m.end():].strip()
    return text


def _extract_may(text: str) -> tuple[bool, str]:
    """Check if effect starts with 'you may' and strip it."""
    ll = text.lower().strip()
    if ll.startswith("you may "):
        return (True, text[len("you may "):].strip())
    return (False, text)


def _splice_activation_reduction(ability_lines: list[str], reduction_expr: str) -> bool:
    """Find the most recently emitted ACTIVATED block's own COST { } sub-
    block in `ability_lines` and insert REDUCTION: <expr> before its
    closing brace, mutating `ability_lines` in place. Returns False (no
    mutation) if the trailing modifier sentence this came from has no
    ACTIVATED block to attach to — e.g. the ability it modifies is a
    KEYWORD-templated cost (Disguise, Equip) rather than a plain COST { }
    block; callers should fall back to an honest gap in that case rather
    than silently dropping the reduction."""
    act_idx = None
    for i in range(len(ability_lines) - 1, -1, -1):
        if ability_lines[i].strip() == "ACTIVATED {":
            act_idx = i
            break
    if act_idx is None:
        return False
    cost_idx = None
    for i in range(act_idx + 1, len(ability_lines)):
        stripped = ability_lines[i].strip()
        if stripped == "COST {":
            cost_idx = i
            break
        if stripped == "}":
            break  # hit ACTIVATED's own close without finding a COST block
    if cost_idx is None:
        return False
    local_depth = 1
    for i in range(cost_idx + 1, len(ability_lines)):
        local_depth += ability_lines[i].count("{") - ability_lines[i].count("}")
        if local_depth == 0:
            indent = ability_lines[i][:len(ability_lines[i]) - len(ability_lines[i].lstrip())]
            ability_lines.insert(i, f"{indent}  REDUCTION: {reduction_expr}")
            return True
    return False


def _splice_keyword_reduction(ability_lines: list[str], reduction_expr: str) -> bool:
    """Find the most recently emitted parameterized keyword macro line
    (e.g. '  @EQUIP("{4}") AS $equipped', '  @DISGUISE("{5}{R}")') and
    insert REDUCTION: <expr> as a trailing field inside its parens
    (Section 23, v0.58 — REDUCTION on activation-cost-bearing keyword
    macros), mutating `ability_lines` in place. Returns False if no such
    line is found (the counterpart to `_splice_activation_reduction`,
    which handles the plain ACTIVATED { COST { } } case instead)."""
    pattern = re.compile(r'^(\s*)@(\w+)\("([^"]*)"\)(.*)$')
    for i in range(len(ability_lines) - 1, -1, -1):
        m = pattern.match(ability_lines[i])
        if m:
            indent, kw, cost, trailing = m.groups()
            ability_lines[i] = f'{indent}@{kw}("{cost}" REDUCTION: {reduction_expr}){trailing}'
            return True
    return False


def _inject_may_true(effect_lines: list[str]) -> list[str]:
    """
    Insert MAY: TRUE as a field inside the first effect block's braces
    (Section 22: "MAY: TRUE" belongs inside the block, not as a bare
    trailing line — e.g. DRAW { PLAYER: YOU COUNT: 1 MAY: TRUE }).
    Handles both single-line blocks and multi-line blocks by tracking
    brace depth to find the block's own closing line.
    """
    if not effect_lines:
        return effect_lines

    first = effect_lines[0].rstrip()
    if first.endswith("}"):
        body = first[:-1].rstrip()
        return [f"{body} MAY: TRUE }}"] + effect_lines[1:]

    depth = first.count("{") - first.count("}")
    for i in range(1, len(effect_lines)):
        depth += effect_lines[i].count("{") - effect_lines[i].count("}")
        if depth == 0:
            indent = effect_lines[i][:len(effect_lines[i]) - len(effect_lines[i].lstrip())]
            return effect_lines[:i] + [f"{indent}  MAY: TRUE"] + effect_lines[i:]

    return effect_lines + ["MAY: TRUE"]


def _bind_event_block(event_block: list[str], var_name: str) -> list[str]:
    """Append 'AS $name' to the closing brace of an event block so later
    effect lines can reference the bound event (e.g. $event.source.controller).
    No-op if the block is already bound to that name, or if the "event block"
    is actually an unmatched-event gap marker (nothing to bind to — appending
    would corrupt the gap message). An EVENTS: [...] compound block (v0.68)
    binds each sub-event individually — $event must resolve correctly
    regardless of which one actually fired, not just the last."""
    if event_block and event_block[0].strip() == "EVENTS: [":
        bound = [event_block[0]]
        for line in event_block[1:-1]:
            bound.append(line if line.rstrip().endswith(f"AS {var_name}") else f"{line} AS {var_name}")
        bound.append(event_block[-1])
        return bound
    last = event_block[-1]
    if "# SPEC_GAP:" in last:
        return event_block
    if last.rstrip().endswith(f"AS {var_name}"):
        return event_block
    return event_block[:-1] + [last + f" AS {var_name}"]


# ── Filter builders ───────────────────────────────────────────────────────────

def _resolve_pump_subject(subj_text: str) -> str:
    """Convert natural language subject to CDL reference."""
    sl = subj_text.lower().strip()
    if sl in ("this creature", "this permanent", "it"):
        return "SELF"
    if sl.startswith("target"):
        return "$target"
    if sl in ("all creatures", "all other creatures"):
        excl = " EXCEPT: SELF" if "other" in sl else ""
        return f'FILTER {{ TYPE: "Creature" SELECTION: ALL{excl} }}'
    if "creatures you control" in sl:
        return 'FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }'
    if "each creature you control" in sl:
        return 'FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }'
    if "permanents you control" in sl:
        return 'FILTER { CONTROLLER: YOU SELECTION: ALL }'
    return "SELF"


def _extract_mv_constraint(ll: str) -> str:
    """Extract 'with mana value N or greater/less/equal' as a CDL MV filter field."""
    m = re.search(r'with mana value (\d+) or greater', ll)
    if m:
        return f"MV: GTE {m.group(1)}"
    m = re.search(r'with mana value (\d+) or less', ll)
    if m:
        return f"MV: LTE {m.group(1)}"
    m = re.search(r'with mana value (\d+)', ll)
    if m:
        return f"MV: EQ {m.group(1)}"
    return ""


def _build_counter_filter(ll: str) -> str:
    parts = []
    if "noncreature" in ll:
        parts.append('NOT { TYPE: "Creature" }')
    elif "enchantment" in ll and "instant" in ll and "sorcery" in ll:
        parts.append('TYPE: ["Enchantment", "Instant", "Sorcery"]')
    elif "instant or sorcery" in ll:
        parts.append('TYPE: ["Instant", "Sorcery"]')
    return " ".join(parts)


def _build_destroy_filter(matched: str, ll: str) -> str:
    parts = []
    if "artifact, enchantment, or nonbasic land" in ll:
        # N-way compound with a per-member qualifier ("nonbasic") — a flat
        # TYPE: [list] can't express the "Land but not Basic" branch, so use
        # filter-level OR { } with a nested AND/NOT for that one member
        # (Section 14.1's own @PROTECTION worked example precedents OR {}
        # combining heterogeneous criteria this way).
        parts.append('OR { TYPE: "Artifact" TYPE: "Enchantment" AND { TYPE: "Land" NOT { TYPE: "Basic" } } }')
    elif "artifact or creature" in ll:
        parts.append('TYPE: ["Artifact", "Creature"]')
    elif "artifact or enchantment" in ll:
        parts.append('TYPE: ["Artifact", "Enchantment"]')
    elif "nonland permanent" in ll:
        parts.append("CATEGORY: NONLAND")
    elif "creature" in ll:
        parts.append('TYPE: "Creature"')
    elif "artifact" in ll:
        parts.append('TYPE: "Artifact"')
    elif "enchantment" in ll:
        parts.append('TYPE: "Enchantment"')
    else:
        parts.append("CATEGORY: PERMANENT")
    cm = re.search(r'\btarget (white|blue|black|red|green) (?:creature|permanent|artifact|enchantment|planeswalker)\b', ll)
    if cm:
        parts.append(f"COLOR: {_color_word_to_symbol(cm.group(1))}")
    if "opponent controls" in ll or "an opponent" in ll:
        parts.append("CONTROLLER: OPPONENT")
    return " ".join(parts)


def _build_exile_filter(ll: str) -> str:
    parts = []
    if "nonland permanent" in ll:
        parts.append("CATEGORY: NONLAND")
    elif "creature" in ll:
        parts.append('TYPE: "Creature"')
    elif "artifact" in ll:
        parts.append('TYPE: "Artifact"')
    elif "enchantment" in ll:
        parts.append('TYPE: "Enchantment"')
    else:
        parts.append("CATEGORY: PERMANENT")
    if "opponent controls" in ll or "an opponent" in ll or "you don't control" in ll:
        parts.append("CONTROLLER: OPPONENT")
    return " ".join(parts)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _word_to_num(word: str) -> int:
    m = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1, "an": 1}
    if isinstance(word, str) and word.lower() in m:
        return m[word.lower()]
    try:
        return int(word)
    except (ValueError, TypeError):
        return 1


def _affinity_cost_reduction_filter(descriptor: str) -> str:
    """'artifacts' -> TYPE: "Artifact" CONTROLLER: YOU; 'snow lands' ->
    AND { TYPE: "Snow" TYPE: "Land" } CONTROLLER: YOU. Rule 702.41's object
    descriptor is always plural and always implicitly "you control" (the
    reminder text omits "you control" but the rule requires it)."""
    words = [_singularize(w).title() for w in descriptor.strip().rstrip('.').split()]
    type_clause = (f'TYPE: "{words[0]}"' if len(words) == 1
                   else "AND { " + " ".join(f'TYPE: "{w}"' for w in words) + " }")
    return f'{type_clause} CONTROLLER: YOU'


_PROTECTION_COLORS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}


def _protection_source_to_criterion(item: str) -> Optional[str]:
    """One clause of a 'protection from <source>' list -> a FILTER criterion
    string, or None if unrecognized (caller gaps honestly rather than guess).
    "everything" -> "" (empty FILTER matches any source, same convention as
    _filter_for_subject_phrase's bare "permanent"). Bare type-line words
    (card types, creature types/subtypes, supertypes like "snow") all use
    TYPE — FILTER's TYPE field matches any type-line word uniformly, per the
    existing "legendary <type>" REDUCTION precedent elsewhere in this file.
    """
    item = item.strip().rstrip('.')
    low = item.lower()
    if low in _PROTECTION_COLORS:
        return f'COLOR: {_PROTECTION_COLORS[low]}'
    if low == "colorless":
        return "COLOR: COLORLESS"
    if low == "everything":
        return ""
    if low in ("multicolored", "monocolored"):
        return None  # a color-count criterion, not a type/color match — no FILTER primitive for this
    m = re.match(r'^non-(\w+) creatures?$', item, re.IGNORECASE)
    if m:
        return f'AND {{ TYPE: "Creature" NOT {{ TYPE: "{_singularize(m.group(1)).title()}" }} }}'
    if re.match(r'^[A-Za-z][A-Za-z\-]*$', item):
        return f'TYPE: "{_singularize(item).title()}"'
    return None


def _fold_protection_parts(raw_parts: list[str]) -> list[str]:
    """Given comma/'and'-split fragments, re-merge any 'from <source>' or
    'and from <source>' continuation fragment back onto a preceding
    'protection from ...' fragment — 'protection from X, from Y, and from
    Z' is one keyword with a compound source list, not three keywords."""
    parts = []
    for p in raw_parts:
        p = p.strip()
        if not p:
            continue
        # A leading "and " survives on the last item of an Oxford-comma list
        # ("daunt, deathtouch, and poisonous 2") because the comma-or-"and"
        # split above matches the comma before it gets a chance to match
        # " and " — the comma (plus trailing space) consumes the delimiter,
        # leaving no leading whitespace for "\s+and\s+" to match against.
        # Stripping it here (shared by every caller of this helper) doesn't
        # disturb the "and from <source>" protection-continuation check
        # below, which only cares whether the *rest* starts with "from ".
        p = re.sub(r'^and\s+', '', p, flags=re.IGNORECASE)
        if parts and re.match(r'^(and\s+)?from\s+', p, re.IGNORECASE) and \
                re.match(r'^protection from ', parts[-1], re.IGNORECASE):
            parts[-1] = f"{parts[-1]}, {p}"
        else:
            parts.append(p)
    return parts


def _match_protection_keyword(clean_line: str) -> Optional[list[str]]:
    """'Protection from <source>[, from <source>...][, and from <source>]'
    -> one @PROTECTION(FILTER {...}) line per source (Section 14.1's own
    @PROTECTION worked example; the multi-source convention is precedented
    by the keyword_store 'Protection from white and from black' entry this
    generalizes and replaces). Returns None if any clause doesn't parse
    (e.g. "protection from multicolored", "...from permanents with X
    counters on them") so the caller falls back to an honest gap rather
    than guessing at unprecedented syntax.
    """
    m = re.match(r'^protection from (.+)$', clean_line, re.IGNORECASE)
    if not m:
        return None
    normalized = re.sub(r'\s+and\s+', ', ', m.group(1), flags=re.IGNORECASE)
    items = [re.sub(r'^from\s+', '', i.strip(), flags=re.IGNORECASE)
             for i in normalized.split(',') if i.strip()]
    if not items:
        return None
    criteria = [_protection_source_to_criterion(i) for i in items]
    if any(c is None for c in criteria):
        return None
    return [f'  @PROTECTION(FILTER {{ {c} }})' if c else '  @PROTECTION(FILTER {  })'
            for c in criteria]


def _filter_for_subject_phrase(phrase: str) -> str:
    """
    Convert a bare subject phrase like "a legendary creature you control",
    "a permanent you control", or "a Ninja creature you control" into a
    FILTER {...} clause. "permanent" alone carries no type-line constraint
    (any permanent) since "Permanent" isn't a real type word.
    """
    p = phrase.strip().rstrip('.')
    ctrl = "CONTROLLER: YOU" if "you control" in p else ""
    p = re.sub(r'\s*you control\s*$', '', p).strip()
    p = re.sub(r'^(a|an)\s+', '', p, flags=re.IGNORECASE)
    words = [w.title() for w in p.split() if w.lower() != "permanent"]

    parts = []
    if len(words) == 1:
        parts.append(f'TYPE: "{words[0]}"')
    elif len(words) > 1:
        parts.append("AND { " + " ".join(f'TYPE: "{w}"' for w in words) + " }")
    if ctrl:
        parts.append(ctrl)
    return "FILTER { " + " ".join(parts) + " }"


_IE_PLURAL_EXCEPTIONS = {"zombies": "zombie", "faeries": "faerie", "genies": "genie", "pixies": "pixie"}


def _singularize(word: str) -> str:
    """Best-effort singular form of an English creature-type plural
    ("Dwarves" -> "Dwarf", "Slivers" -> "Sliver", "Horrors" -> "Horror").
    A plain "ies" -> "y" suffix swap is ambiguous: it's correct for a
    consonant+y pluralization ("Harpies" -> "Harpy") but wrong for a
    handful of real MTG creature types that already end in "ie" and
    pluralize with a bare "s" ("Zombies" -> "Zombie", not "Zomby") —
    checked against a small exception list first."""
    if word.lower() in _IE_PLURAL_EXCEPTIONS:
        singular = _IE_PLURAL_EXCEPTIONS[word.lower()]
        return singular.capitalize() if word[0].isupper() else singular
    if word.endswith("ves"):
        return word[:-3] + "f"
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _color_word_to_symbol(word: str) -> str:
    m = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G",
         "colorless": "COLORLESS", "white and blue": "W U"}
    return m.get(word.lower(), word.upper()[:1] if word else "COLORLESS")


def _mana_sym(mana: str) -> str:
    """Format a mana string like '{G}' for ADD_MANA block."""
    # Returns quoted symbol list
    syms = re.findall(r'\{[^}]+\}', mana)
    if len(syms) == 1:
        return f'"{syms[0]}"'
    return "[" + ", ".join(f'"{s}"' for s in syms) + "]"


def _mana_ability_block(cost: str, mana: str) -> list[str]:
    if mana == "{Any}":
        mana_expr = '"{Any}"'
    else:
        syms = re.findall(r'\{[^}]+\}', mana)
        if len(syms) == 1:
            mana_expr = f'{{ "{syms[0]}" }}'
        else:
            mana_expr = "{ [" + ", ".join(f'"{s}"' for s in syms) + "] }"
    return [
        "  MANA_ABILITY {",
        f"    COST: {cost}",
        f"    ADD_MANA: {mana_expr}",
        "  }",
    ]


def _extract_token_count(ll: str) -> int:
    m = re.search(r'create (?:a |an? )?(?:(\w+) )?(?:tapped and attacking )?(?:\d+/\d+ )?(?:\w+ )?\w+ token', ll)
    if m and m.group(1):
        return _word_to_num(m.group(1))
    # "create two treasure tokens"
    m2 = re.search(r'create (\w+)', ll)
    if m2:
        return _word_to_num(m2.group(1))
    return 1


def _infer_counter_subject(ll: str) -> str:
    if "this creature" in ll or "on it" in ll:
        return "SELF"
    if "target creature" in ll:
        return "$target"
    if "each creature you control" in ll:
        return 'FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }'
    return "SELF"


def _target_class_for(text: str) -> str:
    """Return the TARGET_CLASSES value(s) mentioned in text — handles
    compound phrasing like "target player or planeswalker" (Boros Charm,
    Breya), not just a single noun."""
    classes = []
    if "creature" in text:
        classes.append("CREATURE")
    if "planeswalker" in text:
        classes.append("PLANESWALKER")
    if "player" in text:
        classes.append("PLAYER")
    if "battle" in text:
        classes.append("BATTLE")
    return ", ".join(classes) if classes else "PLAYER"


# ── CDL assembler ─────────────────────────────────────────────────────────────

def assemble_cdl(card: dict, keyword_store: KeywordStore) -> str:
    matcher = PatternMatcher(keyword_store)
    oracle = card.get("oracle_text", "")
    card_types = card.get("types", "").lower()
    is_spell = any(t in card_types for t in ["instant", "sorcery"])
    is_land = "land" in card_types

    # ── 1. Header ─────────────────────────────────────────────────────────
    header = build_header(card)

    # ── 2. Tokenize oracle ────────────────────────────────────────────────
    lines = tokenize_oracle(oracle)

    # ── 3. Pre-scan for header-level constructs ───────────────────────────
    header_extras = []
    addl_costs = []
    alt_costs = []
    cant_be_countered = False
    enters_tapped = False
    cost_reductions = []
    constructed_overrides = []
    affinity_filter = None
    affinity_line = None

    for line in lines:
        ll = line.lower()
        if "can't be countered" in ll and "this spell" in ll:
            cant_be_countered = True
        if re.match(r'^this (?:\w+ )?enters tapped', ll) or ll == "this land enters tapped.":
            enters_tapped = True
        if ll.startswith("as an additional cost"):
            field, extras = match_addl_cost(line, card)
            addl_costs.append(field)
        if ("rather than pay this spell's mana cost" in ll or
                "without paying its mana cost" in ll or "without paying their mana cost" in ll):
            alt_costs.append(line)
        if re.match(r'^this spell costs? .+ less', ll):
            m = re.search(r'(\{[^}]+\}) less', ll)
            if m:
                cost_reductions.append(m.group(1))
        # "A deck can have any number of cards named X." — Section 2.5,
        # already documented (v0.48, Rat Colony worked example) but never
        # wired up to a parser pattern.
        if re.match(r'^a deck can have any number of cards named ', ll):
            constructed_overrides.append("COPY_LIMIT: ANY_NUMBER")
        # NOTE: "Companion — Your starting deck contains at least N cards
        # more than the minimum deck size" (Yorion) also maps to
        # CONSTRUCTED_OVERRIDE { COMPANION: { CONDITION: ... } } per Section
        # 2.5, but there's no worked example or documented "minimum deck
        # size" constant/accessor for the CONDITION expression itself —
        # left as an honest gap rather than guessing at that syntax.
        # "Affinity for X" (rule 702.41) always means exactly "this spell
        # costs {1} less to cast for each X you control" — a concrete,
        # always-identical reduction, unlike a pure evasion-style keyword
        # tag (Fear, Shadow, ...) with no computable effect of its own.
        # Handled at header level like the other cost-affecting keyword
        # lines above, and excluded from normal ability-line rendering
        # below so it isn't also emitted as a separate bare keyword line.
        am = re.match(r'^affinity for (.+)$', ll.rstrip('.'))
        if am:
            affinity_filter = _affinity_cost_reduction_filter(am.group(1))
            affinity_line = line

    if affinity_line:
        lines = [l for l in lines if l != affinity_line]

    # ── 4. Inject header-level fields ─────────────────────────────────────
    if enters_tapped:
        header.append("  ENTERS: TAPPED")
    for ar in addl_costs:
        header.append(ar)
    if cost_reductions:
        # Use COUNT-scaled if "for each" pattern
        header.append(f'  COST_REDUCTION: "{cost_reductions[0]}" * COUNT(BATTLEFIELD FILTER {{ TYPE: "Creature" }})')
    if affinity_filter:
        header.append(f'  COST_REDUCTION: "{{1}}" * COUNT(BATTLEFIELD FILTER {{ {affinity_filter} }})')
    for ac in alt_costs:
        alt_block = _build_alt_cost(ac, card)
        header += alt_block
    if constructed_overrides:
        header.append("  CONSTRUCTED_OVERRIDE {")
        header += [f"    {co}" for co in constructed_overrides]
        header.append("  }")

    # ── 5. Separate CARD header close from ability section ─────────────────
    # We'll append abilities then close the block

    ability_lines = []

    if cant_be_countered:
        ability_lines += [
            "",
            "  STATIC {",
            "    EFFECT: [",
            "      CANT { SELF EFFECTS: [COUNTER] }",
            "    ]",
            "  }",
        ]

    # ── 6. For spells: build SPELL block ──────────────────────────────────
    if is_spell:
        # Keyword lines on a spell (Kicker, Overload, Flashback, Buyback,
        # Madness, ...) render as their own ability line, same as
        # @EQUIP/@DISGUISE already do for permanents. Previously these fell
        # into match_spell_body's generic line loop: most produced a
        # confusing "effect not matched" gap (Kicker), while three
        # (Overload/Flashback/Harmonize) were silently skipped with no gap
        # marker at all — the ability was dropped from the output entirely.
        spell_kw_lines = [l for l in lines
                           if _is_keyword_line(re.sub(r'\s*\([^)]+\)', '', l).strip().rstrip('.'))]
        spell_body_lines = [l for l in lines if l not in spell_kw_lines]
        for kw_line in spell_kw_lines:
            ability_lines.append("")
            ability_lines += matcher.match_keyword_line(kw_line)

        spell_body_text = "\n".join(spell_body_lines)
        spell_effects = matcher.match_spell_body(spell_body_text, card)
        if any("CHOICE_OPTION:" in e for e in spell_effects):
            # Reconstruct as CHOICE block. Pass the same keyword-filtered
            # text used above — _build_choice_block's own preamble scan
            # would otherwise re-process an already-emitted keyword line
            # (e.g. "Kicker {2}{U}{U}") as a bogus second effect.
            ability_lines += _build_choice_block(spell_body_text, card, matcher)
        else:
            ability_lines.append("")
            ability_lines.append("  SPELL {")
            ability_lines.append("    EFFECT: [")
            for e in spell_effects:
                ability_lines.append(f"      {e}")
            ability_lines.append("    ]")
            ability_lines.append("  }")

    else:
        # ── 7. Non-spells: classify each oracle line ─────────────────────
        idx = 0
        n_lines = len(lines)
        while idx < n_lines:
            line = lines[idx]
            idx += 1
            ll = line.lower()
            # Discard pure reminder-text lines (entire line is parenthetical)
            if re.match(r'^\([^)]+\)$', line.strip()): continue

            atype = classify_line(line, card)

            # ── Header-level: already handled in pre-scan ─────────────────
            if atype in (AbilityType.ENTERS_TAPPED,
                         AbilityType.ADDL_COST,
                         AbilityType.ALT_COST,
                         AbilityType.COST_REDUCTION,
                         AbilityType.CANT_BE_COUNTERED,
                         AbilityType.CONSTRUCTED_OVERRIDE):
                continue

            # ── Keyword ability ───────────────────────────────────────────
            elif atype == AbilityType.KEYWORD:
                ability_lines.append("")
                ability_lines += matcher.match_keyword_line(line)

            # ── Mana ability ──────────────────────────────────────────────
            elif atype == AbilityType.MANA_ABILITY:
                ability_lines.append("")
                ability_lines += matcher.match_mana_ability(line, card)

            # ── Activated ability ─────────────────────────────────────────
            elif atype == AbilityType.ACTIVATED:
                ability_lines.append("")
                bullets = []
                if _ends_with_choose_marker(ll):
                    while idx < n_lines and lines[idx].strip().startswith('•'):
                        bullets.append(lines[idx])
                        idx += 1
                block = matcher.match_activated(line, card)
                if bullets:
                    block = _splice_choice_block(block, line, bullets, card, matcher)
                ability_lines += block

            # ── Triggered ability ─────────────────────────────────────────
            elif atype == AbilityType.TRIGGERED:
                ability_lines.append("")
                bullets = []
                if _ends_with_choose_marker(ll):
                    while idx < n_lines and lines[idx].strip().startswith('•'):
                        bullets.append(lines[idx])
                        idx += 1
                triggered_block = matcher.match_triggered(line, card)
                if bullets:
                    triggered_block = _splice_choice_block(triggered_block, line, bullets, card, matcher)
                # Inject per-turn limit if flagged in the line
                if "only once each turn" in ll or "this ability triggers only once" in ll:
                    for i, tl in enumerate(triggered_block):
                        if tl.strip().startswith("EFFECT:"):
                            triggered_block.insert(i, "    LIMIT: 1\n    REPLENISH: EACH_TURN")
                            break
                ability_lines += triggered_block

            # ── Loyalty ability ───────────────────────────────────────────
            elif atype == AbilityType.LOYALTY:
                ability_lines.append("")
                ability_lines += _build_loyalty_ability(line, card, matcher)

            # ── Saga chapter ──────────────────────────────────────────────
            elif atype == AbilityType.SAGA_CHAPTER:
                ability_lines.append("")
                ability_lines += _build_saga_chapter(line, card, matcher)

            # ── Choice option (bullet) ────────────────────────────────────
            # These are collected by the enclosing triggered/static context.
            # If we encounter a lone bullet here it means the "Choose one —"
            # header wasn't on a prior line — emit as gap.
            elif atype == AbilityType.CHOICE_OPTION:
                ability_lines.append("")
                ability_lines.append(f"  # SPEC_GAP: orphan choice option: {line}")

            # ── Equipped/enchanted grant (static sub-type) ────────────────
            elif atype == AbilityType.EQUIPPED_GRANT:
                ability_lines.append("")
                ability_lines += matcher.match_equipped_grant(line, card)

            # ── Replacement effect ────────────────────────────────────────
            elif atype == AbilityType.REPLACEMENT:
                ability_lines.append("")
                ability_lines += matcher.match_replacement(line, card)

            # ── Static ability ────────────────────────────────────────────
            elif atype == AbilityType.STATIC:
                # "Enchant <object descriptor>" — the Aura target-
                # declaration line (Section 23's @ENCHANT macro), same
                # shape as @EQUIP's own "AS $equipped" binding convention.
                em = re.match(r"^enchant (.+)$", ll)
                if em:
                    filter_clause = _filter_for_subject_phrase(em.group(1))
                    ability_lines.append("")
                    ability_lines.append(f"  @ENCHANT({filter_clause}) AS $enchanted")
                    continue

                # "This ability costs {N} less to activate for each
                # <Type> you control." — a trailing modifier sentence the
                # tokenizer splits off from its own ACTIVATED ability line
                # (e.g. Boseiju's Channel). Splice REDUCTION into that
                # block's COST rather than emitting a separate STATIC.
                rm = re.match(
                    r"this (?:ability|cost) (?:costs?|is reduced by) (\{[^}]+\})"
                    r" less(?: to activate)? for each (legendary )?(\w+) you control\.?$", ll)
                if rm:
                    cost, legendary, type_word = rm.groups()
                    type_clause = (f'AND {{ TYPE: "Legendary" TYPE: "{type_word.title()}" }}'
                                   if legendary else f'TYPE: "{type_word.title()}"')
                    reduction = f'"{cost}" * COUNT(BATTLEFIELD FILTER {{ {type_clause} CONTROLLER: YOU }})'
                    if _splice_activation_reduction(ability_lines, reduction) or \
                            _splice_keyword_reduction(ability_lines, reduction):
                        continue

                # "This cost is reduced by {N} for each <type1> and
                # <type2> card in your graveyard." (Fugitive Codebreaker's
                # Disguise cost) — same COUNT-scaling idiom as above,
                # scoped to the graveyard instead of the battlefield.
                rm2 = re.match(
                    r"this cost is reduced by (\{[^}]+\}) for each (\w+) and (\w+) card"
                    r" in your graveyard\.?$", ll)
                if rm2:
                    cost, type1, type2 = rm2.groups()
                    reduction = (f'"{cost}" * COUNT(YOU.graveyard FILTER '
                                 f'{{ TYPE: ["{type1.title()}", "{type2.title()}"] }})')
                    if _splice_activation_reduction(ability_lines, reduction) or \
                            _splice_keyword_reduction(ability_lines, reduction):
                        continue

                # "This ability costs {N} less to activate if you're the
                # monarch." (Crown of Gondor's Equip cost) — a CONDITION-
                # gated reduction rather than a COUNT-scaled one; CASE
                # selects the applicable mana expression (Section 16),
                # composed with REDUCTION's existing mana-expression slot.
                rm3 = re.match(
                    r"this ability costs (\{[^}]+\}) less to activate if you're the monarch\.?$", ll)
                if rm3:
                    cost = rm3.group(1)
                    reduction = f'CASE {{ WHEN: YOU.is_monarch THEN: "{cost}" ELSE: "{{0}}" }}'
                    if _splice_activation_reduction(ability_lines, reduction) or \
                            _splice_keyword_reduction(ability_lines, reduction):
                        continue

                ability_lines.append("")
                ability_lines += matcher.match_static(line, card)

            else:
                ability_lines.append("")
                ability_lines.append(f"  # SPEC_GAP: unclassified: {line}")

    # ── 8. Assemble ────────────────────────────────────────────────────────
    result = header + ability_lines + ["}", ""]
    return "\n".join(result)


def _build_alt_cost(line: str, card: dict) -> list[str]:
    ll = line.lower()
    # Force of Will pattern: "pay 1 life and exile a blue card"
    m = re.search(r'pay (\d+) life and exile a (\w+) card from your hand', ll)
    if m:
        life = m.group(1)
        color = _color_word_to_symbol(m.group(2))
        return [
            "  ALT_COST {",
            f"    PAY_LIFE: {life}",
            "    EXILE: CHOOSE {",
            "      FROM: YOU.hand",
            "      COUNT: 1",
            f"      FILTER {{ COLOR: {color} }}",
            "    }",
            "  }",
        ]
    # "without paying its mana cost" — free cast
    if "without paying" in ll:
        # Check for condition
        m2 = re.search(r'if you control a commander', ll)
        if m2:
            return [
                "  ALT_COST {",
                "    COST: \"{0}\"",
                "    CONDITION: COUNT(YOU.commanders) GTE 1",
                "  }",
            ]
        return [
            "  ALT_COST {",
            "    COST: \"{0}\"",
            "  }",
        ]
    return [f"  # SPEC_GAP: ALT_COST: {line}"]


def _build_saga_chapter(line: str, card: dict, matcher: 'PatternMatcher') -> list[str]:
    """
    Handle Saga chapter lines encoded as "N+ | [text]" in our card data.
    Per CR 107.15: chapter symbols are triggered abilities that fire when
    the corresponding lore counter count is reached.

    N+ means "when lore counter count reaches N or more".
    """
    m = re.match(r'^(\d+)\+\s*\|\s*(.+)', line.strip())
    if not m:
        return [f"  # SPEC_GAP: saga chapter parse failed: {line}"]

    threshold = m.group(1)
    chapter_text = m.group(2).strip()

    # Chapter text may be a keyword grant or an effect
    # Check if it's a keyword line
    from_keyword = _is_keyword_line(_strip_reminder(chapter_text).rstrip('.'))
    effect_lines: list[str]
    if from_keyword:
        # Keywords granted by the chapter — this is a static conditional,
        # not a triggered effect per se. Treat as GRANT.
        kw_parts = [p.strip() for p in re.sub(r'\s*\([^)]+\)', '', chapter_text).rstrip('.').split(',') if p.strip()]
        grants = []
        ks = KeywordStore(os.path.join(os.path.dirname(__file__), "keyword_store.json"))
        for part in kw_parts:
            cdl = ks.match(part)
            grants.append(cdl if cdl else f"# UNKNOWN_KEYWORD: {part}")
        effect_lines = matcher._render_grant("SELF", grants)
    else:
        effect_lines = matcher.match_effect(chapter_text, card)

    result = [
        "  TRIGGERED {",
        f"    WHEN_LORE_COUNTER_ADDED {{ FILTER {{ SELF }} THRESHOLD: GTE {threshold} }}",
        "    EFFECT: [",
    ]
    for e in effect_lines:
        result.append(f"      {e}")
    result += ["    ]", "  }"]
    return result

    ll = line.lower()
    # Force of Will pattern: "pay 1 life and exile a blue card"
    m = re.search(r'pay (\d+) life and exile a (\w+) card from your hand', ll)
    if m:
        life = m.group(1)
        color = _color_word_to_symbol(m.group(2))
        return [
            "  ALT_COST {",
            f"    PAY_LIFE: {life}",
            "    EXILE: CHOOSE {",
            "      FROM: YOU.hand",
            "      COUNT: 1",
            f"      FILTER {{ COLOR: {color} }}",
            "    }",
            "  }",
        ]
    return [f"  # SPEC_GAP: ALT_COST: {line}"]


def _build_loyalty_ability(line: str, card: dict, matcher: PatternMatcher) -> list[str]:
    """Parse '+1: ...', '0: ...', '−3: ...' loyalty ability lines."""
    m = re.match(r'^([+\u2212\-]?\d+)\s*:\s*(.+)', line)
    if not m:
        return [f"  # SPEC_GAP: loyalty parse failed: {line}"]
    cost_raw, effect_raw = m.group(1), m.group(2).strip()
    # Normalize minus sign
    cost = cost_raw.replace('\u2212', '-')

    effect_lines = matcher.match_effect(effect_raw, card)

    result = [
        "  LOYALTY_ABILITY {",
        f"    COST: {cost}",
        "    EFFECT: [",
    ]
    for e in effect_lines:
        result.append(f"      {e}")
    result += ["    ]", "  }"]
    return result


def _ends_with_choose_marker(ll: str) -> bool:
    """Does this ACTIVATED/TRIGGERED line's text end in a modal choice
    header ("choose one -", "choose two -", "choose one that hasn't been
    chosen this turn -") whose options follow as bullet lines?"""
    return bool(re.search(
        r"choose (one|two|one or more|one or both)(?: that hasn't been chosen this turn)?\s*[—-]\s*$",
        ll.strip()))


def _assemble_choice_block(header_line: str, bullets: list[str], card: dict, matcher: PatternMatcher) -> list[str]:
    """
    Build a CHOICE block's CDL lines from a 'Choose N [that hasn't been
    chosen this turn] -' header and its bullet options — the ACTIVATED/
    TRIGGERED counterpart to _build_choice_block, which is SPELL-body
    specific (hardcodes PLAYER: YOU / SPELL wrapper, no DEPLETE support).
    """
    hl = header_line.lower()
    count_map = {"one": "1", "two": "2", "one or more": "RANGE 1..ALL", "one or both": "RANGE 1..2"}
    m = re.search(r"choose (\w[\w ]*?)(?: that hasn't been chosen this turn)?\s*[—-]\s*$", hl.strip())
    count_word = m.group(1).strip() if m else "one"
    count = count_map.get(count_word, "1")
    depletes = "hasn't been chosen this turn" in hl

    lines = ["CHOICE {", "  PLAYER: YOU", f"  COUNT: {count}"]
    if depletes:
        lines += ["  DEPLETE: TRUE", "  REPLENISH: EACH_TURN"]
    for bullet in bullets:
        opt_text = bullet.strip()[1:].strip()  # strip leading bullet char
        # Strip an optional "Name — effect" flavor prefix (e.g. "Standoff — ...")
        opt_text = re.sub(r'^[\w \'-]+ — ', '', opt_text)
        opt_effects = matcher.match_effect(opt_text, card)
        lines.append("  OPTION {")
        lines.append("    EFFECT: [")
        for e in opt_effects:
            lines.append(f"      {e}")
        lines.append("    ]")
        lines.append("  }")
    lines.append("}")
    return lines


def _splice_choice_block(block: list[str], header_line: str, bullets: list[str],
                          card: dict, matcher: PatternMatcher) -> list[str]:
    """Replace the '# SPEC_GAP: CHOICE block needed: ...' placeholder that
    match_effect's _match_choice stub emits with a fully assembled CHOICE
    block, preserving the placeholder line's indentation.

    The placeholder is only present when the ability's own text (after
    cost/condition stripping) happened to start with "choose N" itself.
    When something upstream (an unrecognized "if"/"when you do" clause)
    keeps that from happening, there's no placeholder to replace — in
    that case, fall back to re-emitting the bullets as their original
    per-line gaps rather than silently dropping the consumed lines.
    """
    choice_lines = _assemble_choice_block(header_line, bullets, card, matcher)
    out = []
    spliced = False
    for l in block:
        if "SPEC_GAP: CHOICE block needed" in l:
            indent = l[:len(l) - len(l.lstrip())]
            out += [f"{indent}{cl}" for cl in choice_lines]
            spliced = True
        else:
            out.append(l)
    if not spliced:
        out += [f"  # SPEC_GAP: orphan choice option: {b}" for b in bullets]
    return out


def _build_choice_block(oracle: str, card: dict, matcher: PatternMatcher) -> list[str]:
    """Build a CHOICE block from '• option' bullet lines."""
    lines_in = oracle.split('\n')
    header_line = ""
    options = []
    preamble = []

    for line in lines_in:
        line = line.strip()
        if not line: continue
        if line.startswith('•'):
            options.append(line[1:].strip())
        elif re.match(r'^choose (one|two|one or more|one or both)', line.lower()):
            header_line = line
        else:
            if not options:
                preamble.append(line)

    # Count
    count_map = {"one": "1", "two": "2", "one or more": "RANGE 1..ALL", "one or both": "RANGE 1..2"}
    m = re.match(r'choose (\w[\w ]*)', header_line.lower()) if header_line else None
    count_word = m.group(1).strip() if m else "one"
    count = count_map.get(count_word, "1")

    # Check conditional all-choices
    extra_cond = ""
    if "if you control a commander" in oracle.lower() and "both" in oracle.lower():
        count = f"RANGE 1..CASE {{ WHEN: COUNT(YOU.commanders) GTE 1 THEN: 2 ELSE: 1 }}"

    result = []
    # Preamble effects first
    for p in preamble:
        if not re.match(r'^this spell costs|^as an additional', p.lower()):
            result += [f"      {e}" for e in matcher.match_effect(p, card)]

    result += [
        "  SPELL {",
        "    EFFECT: [",
        "      CHOICE {",
        f"        PLAYER: YOU",
        f"        COUNT: {count}",
    ]
    for opt in options:
        opt_effects = matcher.match_effect(opt, card)
        result.append("        OPTION {")
        result.append("          EFFECT: [")
        for e in opt_effects:
            result.append(f"            {e}")
        result.append("          ]")
        result.append("        }")
    result += ["      }", "    ]", "  }"]
    return result


# ── Bootstrap: LLM keyword expansion ──────────────────────────────────────────

def bootstrap_keyword(keyword_text: str, spec_text: str, store: KeywordStore) -> Optional[str]:
    """
    Call the Anthropic API to generate a CDL expansion for an unknown keyword.
    Stores result in keyword_store on success.
    Returns CDL template string or None on failure.
    """
    try:
        import urllib.request, json as _json

        prompt = f"""You are a CDL (Card Description Language) keyword expert.

Given this unknown MTG keyword: "{keyword_text}"

Generate a CDL keyword store entry for it. The entry must:
1. Include a "pattern" regex field matching the keyword in oracle text
2. Include a "cdl_template" field with the CDL output
3. Include "form": one of "simple", "parameterized", "block_parameterized", "complex"
4. Use only constructs defined in the CDL spec v0.53

Respond with ONLY a JSON object (no markdown, no prose):
{{
  "form": "...",
  "pattern": "...",
  "cdl_template": "...",
  "source": "llm",
  "reviewed": false
}}

CDL spec excerpt relevant to keywords:
{spec_text[:8000]}
"""
        payload = _json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req) as resp:
            data = _json.loads(resp.read())
            text = data["content"][0]["text"].strip()
            defn = _json.loads(text)
            name = keyword_text.split()[0].title()  # use first word as key
            store.add_entry(name, defn)
            print(f"  [bootstrap] stored definition for '{name}'", file=sys.stderr)
            return defn["cdl_template"]

    except Exception as e:
        print(f"  [bootstrap] failed for '{keyword_text}': {e}", file=sys.stderr)
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

def encode_card(card: dict, store: KeywordStore, bootstrap: bool = False) -> str:
    result = assemble_cdl(card, store)

    # If bootstrap enabled and there are UNKNOWN_KEYWORD gaps, try to resolve them
    if bootstrap:
        unknown = re.findall(r'# UNKNOWN_KEYWORD: (.+)', result)
        if unknown:
            spec_path = os.path.join(PARSER_DIR, "lang_spec_v0.50.md")
            spec_text = ""
            if os.path.exists(spec_path):
                with open(spec_path, encoding="utf-8") as f:
                    spec_text = f.read()
            for kw in set(unknown):
                cdl = bootstrap_keyword(kw.strip(), spec_text, store)
                if cdl:
                    result = result.replace(f"# UNKNOWN_KEYWORD: {kw}", cdl)

    return result


def encode_card_by_name(name: str, cards: list[dict], store: KeywordStore, bootstrap: bool = False) -> str:
    name_lower = name.lower()
    for card in cards:
        if card.get("name", "").lower() == name_lower:
            return encode_card(card, store, bootstrap)
    return f"# ERROR: card '{name}' not found"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="CDL Parser — encode MTG cards to CDL v0.53")
    ap.add_argument("card", nargs="?", help="Card name to encode")
    ap.add_argument("--cards", default=os.path.join(PARSER_DIR, "cards.json"), help="Path to cards.json")
    ap.add_argument("--store", default=KEYWORD_STORE_PATH, help="Path to keyword_store.json")
    ap.add_argument("--bootstrap", action="store_true", help="Use LLM to resolve unknown keywords")
    ap.add_argument("--all", action="store_true", help="Encode all cards and report gap summary")
    ap.add_argument("--gaps-only", action="store_true", help="Only print gap lines")
    args = ap.parse_args()

    with open(args.cards, encoding="utf-8") as f:
        cards = json.load(f)

    store = KeywordStore(args.store)

    if args.all:
        gaps = []
        unknown_kw = []
        for card in cards:
            output = encode_card(card, store, args.bootstrap)
            for line in output.split('\n'):
                if '# SPEC_GAP:' in line:
                    gaps.append(f"[{card['name']}] {line.strip()}")
                if '# UNKNOWN_KEYWORD:' in line:
                    unknown_kw.append(f"[{card['name']}] {line.strip()}")
        print(f"\n=== GAP REPORT: {len(gaps)} gaps across {len(cards)} cards ===")
        for g in gaps[:50]:
            print(f"  {g}")
        print(f"\n=== UNKNOWN KEYWORDS: {len(unknown_kw)} ===")
        for k in set(unknown_kw):
            print(f"  {k}")
    elif args.card:
        output = encode_card_by_name(args.card, cards, store, args.bootstrap)
        if args.gaps_only:
            for line in output.split('\n'):
                if 'SPEC_GAP' in line or 'UNKNOWN_KEYWORD' in line:
                    print(line)
        else:
            print(output)
    else:
        ap.print_help()