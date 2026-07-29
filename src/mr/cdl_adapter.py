"""Thin adapter over the external CDL parser (github.com/Persing/PileOfCardsParser).

The parser is `parser.encode_card(card, store) -> str`, emitting CDL **source text**, not an AST.
Three things about its actual behaviour drive this module's shape:

  1. **Failure is in-band.** A card can encode "successfully" while the output carries
     `# SPEC_GAP:` or `# UNKNOWN_KEYWORD:` comment lines. So coverage is not binary — a card is
     CLEAN, GAP, CRASH, or EXCLUDED, and collapsing those four into ok/not-ok throws away the
     distinction between "the parser can't express this" and "the parser fell over".

  2. **It excludes cards before parsing.** `scryfall_import.should_include` drops multi-faced,
     digital, and non-playable layouts. Multi-faced cards are real Commander cards, so they are
     reported as their own EXCLUDED stratum rather than being quietly dropped from the denominator
     (which would inflate coverage) or lumped in with parse failures (which would misattribute it).

  3. **Its input schema is not Scryfall's.** `scryfall_import.convert_card` does the translation;
     we import it rather than reimplementing so this stays correct as that repo changes.

`canonical()` is the correctness-critical piece of T1. Its policy decisions are all explicit
constants below, because each biases the divergence number in a knowable direction.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

STATUS_CLEAN = "clean"
STATUS_GAP = "gap"
STATUS_CRASH = "crash"
STATUS_EXCLUDED = "excluded"

GAP_MARKERS = ("# SPEC_GAP:", "# UNKNOWN_KEYWORD:")

# ── canonicalization policy ───────────────────────────────────────────────────
#
# Blocks whose children are an unordered set of clauses. Everything else keeps its order, because
# sequence is load-bearing in CDL (an EFFECT list is a sequence of effects, not a set).
#
# Over-sorting understates divergence, under-sorting overstates it, so T1 reports the strict
# (`sort_all=True`) and loose readings side by side and treats the pair as a bound.
ORDER_INSENSITIVE_HEADS = ("CARD", "AND", "OR", "FILTER")

# Header fields dropped under scope="rules". Two functionally-equivalent cards necessarily differ
# in name, and usually in mana cost and colour (Wrath of God vs Damnation, Rampant Growth vs
# Three Visits). Including the header would score every functional pair as divergent by
# construction, which measures nothing.
HEADER_FIELDS = ("NAME:", "MANA_COST:", "TYPES:", "SUPERTYPES:", "SUBTYPES:", "STATS:",
                 "POWER:", "TOUGHNESS:", "LOYALTY:", "DEFENSE:", "COLOR:", "COLOR_IDENTITY:")
HEADER_FIELD_NAMES = {f.rstrip(":") for f in HEADER_FIELDS}

_WS = re.compile(r"\s+")
_BINDING = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


class ParserUnavailable(RuntimeError):
    """Raised when T0/T1 are asked to run without the CDL parser importable."""


@dataclass
class ParseResult:
    status: str
    cdl: str | None = None
    gaps: list[str] = field(default_factory=list)
    unknown_keywords: list[str] = field(default_factory=list)
    error: str | None = None
    exclusion_reason: str | None = None

    @property
    def ok(self) -> bool:
        """Fully clean: encoded with no gap markers. This is T0's 'fully parses'."""
        return self.status == STATUS_CLEAN

    @property
    def encoded(self) -> bool:
        """Produced CDL at all, gaps or not. The looser reading, reported alongside `ok`."""
        return self.status in (STATUS_CLEAN, STATUS_GAP)


# ── parser discovery ──────────────────────────────────────────────────────────

_state: dict[str, Any] = {"parser": None, "store": None, "convert": None,
                          "should_include": None, "desc": None}


def _discover() -> None:
    root_str = config.CDL_PARSER_PATH
    if not root_str:
        raise ParserUnavailable(
            "No complete CDL parser + clause pipeline found. T2 does not need it and runs "
            f"without it; T0/T1/T4 do. {config.parser_gaps()}"
        )
    root = Path(root_str).expanduser().resolve()
    if not root.exists():
        raise ParserUnavailable(f"MR_CDL_PARSER={root} does not exist.")

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # parser.py resolves keyword_store.json relative to its own file, so no cwd juggling needed.
    try:
        parser_mod = importlib.import_module("parser")
        import_mod = importlib.import_module("scryfall_import")
    except ImportError as exc:
        raise ParserUnavailable(f"Could not import the parser from {root}: {exc}") from exc

    for attr in ("encode_card", "KeywordStore", "KEYWORD_STORE_PATH"):
        if not hasattr(parser_mod, attr):
            raise ParserUnavailable(f"{root}/parser.py has no {attr!r} — interface changed?")

    _state["parser"] = parser_mod
    _state["store"] = parser_mod.KeywordStore(parser_mod.KEYWORD_STORE_PATH)
    _state["convert"] = import_mod.convert_card
    _state["should_include"] = import_mod.should_include
    _state["desc"] = f"{root.name}@{root}"


def ensure() -> None:
    if _state["parser"] is None:
        _discover()


def available() -> bool:
    try:
        ensure()
        return True
    except ParserUnavailable:
        return False


def describe() -> str:
    ensure()
    return str(_state["desc"])


# ── parse ─────────────────────────────────────────────────────────────────────

def parse(scryfall_card: dict) -> ParseResult:
    """Encode one Scryfall card dict to CDL. Never raises on parser failure — that IS the data."""
    ensure()
    reason = _state["should_include"](scryfall_card)
    if reason:
        return ParseResult(status=STATUS_EXCLUDED, exclusion_reason=reason)

    try:
        converted = _state["convert"](scryfall_card)
        cdl = _state["parser"].encode_card(converted, _state["store"], False)
    except Exception as exc:  # noqa: BLE001 - any failure mode is a measurement
        return ParseResult(status=STATUS_CRASH, error=f"{type(exc).__name__}: {exc}")

    gaps, unknown = [], []
    for line in (cdl or "").splitlines():
        s = line.strip()
        if s.startswith("# SPEC_GAP:"):
            gaps.append(s)
        elif s.startswith("# UNKNOWN_KEYWORD:"):
            unknown.append(s)

    status = STATUS_GAP if (gaps or unknown) else STATUS_CLEAN
    return ParseResult(status=status, cdl=cdl, gaps=gaps, unknown_keywords=unknown)


def gap_category(gap_line: str) -> str:
    """'# SPEC_GAP: <category>: <detail>' -> '<category>'. '# UNKNOWN_KEYWORD: ...' has no
    sub-category (matches analyze_full_pool.py's own split_gap_message convention), so it is
    reported as its own bucket rather than falling through to '(uncategorized)'."""
    s = gap_line.strip()
    if "UNKNOWN_KEYWORD:" in s:
        return "UNKNOWN_KEYWORD"
    body = s.split("SPEC_GAP:", 1)[1].strip() if "SPEC_GAP:" in s else s
    return body.split(":", 1)[0].strip() if ":" in body else "(uncategorized)"


# ── canonicalization ──────────────────────────────────────────────────────────
#
# CDL is parsed as a real nested structure, not line-by-line: a bracketed group can open and close
# entirely within one line (`FILTER { AND { TYPE: "Basic" TYPE: "Land" } }` is one line in the
# parser's actual output), so depth has to be tracked per-character across the whole text, with
# quoted strings consumed whole so braces inside a mana cost string never enter the bracket count.

# Parentheses are structural, not text. The parser emits keywords in an at-form —
# `@KICKER( "{B}" )`, `@WARD( "{4}" )`, `@PROTECTION(FILTER { COLOR: W })` — and leaving `(`/`)`
# out of the delimiter set made `@KICKER(` a single atom while the matching `)` became a bare
# node of its own. That node then sorted to the front under ORDER_INSENSITIVE_HEADS, producing
# canonical forms like `CARD{););@PROTECTION(FILTER{...}`. It affected 1,277 of 10,637
# clean-parse cards (12.0%), concentrated in exactly the keyword-carrying cards, and it is a
# T4 blocker because arms C/D feed this string to a WordPiece tokenizer.
_TOKEN = re.compile(r'"[^"]*"|[{}\[\](),;]|[^\s{}\[\](),;"]+')
_OPENERS = {"{": "}", "[": "]", "(": ")"}
_CLOSERS = frozenset(_OPENERS.values())


def _strip_comments(cdl: str) -> str:
    """Drop `#`-prefixed comment lines, joined back into one string with an explicit `;` at each
    line break.

    Gap markers are comments, so a GAP encoding canonicalizes to whatever it *did* manage to
    express. T1 scores clean-only pairs as its primary number precisely because comparing two
    gap-laden encodings measures the gaps, not the representation.

    The parser's actual output relies on the newline itself as the boundary between adjacent
    fields (`MANA_COST: "{1}{W}"` followed on the next line by `MANA_ABILITY {`, with no other
    delimiter). Joining lines with a bare space loses that boundary and lets the two merge into one
    leaf; joining with `;` reproduces the same statement-separator role a literal `;` already plays
    mid-line, so both are handled identically by the tokenizer.
    """
    kept = [ln for ln in cdl.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return " ; ".join(kept)


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _parse_block(tokens: list[str], pos: int) -> tuple[list[Any], int]:
    """Recursive-descent parse of one bracketed scope. Each node is either a leaf string
    (a flushed run of atoms, e.g. 'TYPE: "Basic"') or (head, children, opener) for a nested block.

    A key token (ends with ':') flushes the current buffer before starting a new one — that is
    what separates `TYPE: "Basic" TYPE: "Land"` into two leaves rather than one run-on string,
    since there is no other delimiter between two key:value pairs on the same line.

    The opener is carried on the node so `FOO(x)` and `FOO{x}` cannot render to the same string.
    """
    nodes: list[Any] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            nodes.append(" ".join(buffer))
            buffer.clear()

    while pos < len(tokens):
        tok = tokens[pos]
        if tok in _CLOSERS:
            flush()
            return nodes, pos
        if tok in _OPENERS:
            head = " ".join(buffer)
            buffer.clear()
            children, pos = _parse_block(tokens, pos + 1)
            pos += 1  # consume the matching close bracket
            nodes.append((head, children, tok))
            continue
        if tok in (",", ";"):
            flush()
            pos += 1
            continue
        if tok.endswith(":") and buffer:
            flush()
        buffer.append(tok)
        pos += 1
    flush()
    return nodes, pos


def _head(head: str) -> str:
    return head.rstrip(":").strip().upper() if head else ""


def _drop_header(nodes: list[Any]) -> list[Any]:
    """Drop header fields whether the parser expressed them as a flushed leaf string
    (`NAME: "Foo"`) or as a bracketed block/list (`TYPES: ["Creature"]` parses to a tuple node).
    Only checking leaves missed every list-valued header field — TYPES, SUBTYPES, SUPERTYPES —
    which are exactly the ones two functionally-equivalent-but-differently-typed cards disagree on.
    """
    out = []
    for n in nodes:
        if isinstance(n, tuple):
            head, children, opener = n
            h = _head(head)
            if h == "CARD":
                out.append((head, _drop_header(children), opener))
            elif h not in HEADER_FIELD_NAMES:
                out.append(n)
        elif not n.upper().startswith(HEADER_FIELDS):
            out.append(n)
    return out


def _render(nodes: list[Any], sort_all: bool, parent_head: str = "") -> str:
    parts = []
    for n in nodes:
        if isinstance(n, tuple):
            head, children, opener = n
            parts.append(head + opener + _render(children, sort_all, _head(head)) + _OPENERS[opener])
        else:
            parts.append(n)
    if sort_all or parent_head in ORDER_INSENSITIVE_HEADS:
        parts = sorted(parts)
    return ";".join(parts)


_NUM = re.compile(r"\b\d+\b")
# Matches {W}, {R/W}, {2/W}, {W/P} — hybrid and Phyrexian forms included. Matching only the
# single-letter form would leave locket/hybrid cycles looking structurally divergent when the only
# difference is which colours fill the slot.
_MANA_SYM = re.compile(r"\{(?:[WUBRGCP0-9]+/)*[WUBRGC]\}")
_COLOR_WORD = re.compile(r"\b(white|blue|black|red|green|colorless)\b", re.I)


def canonical(cdl: str, scope: str = "rules", sort_all: bool = False,
              card_name: str | None = None,
              mask_numbers: bool = False, mask_symbols: bool = False) -> str:
    """Order-stable normal form of a CDL encoding.

    scope="rules" (default) drops header fields — name, cost, types, stats. Functionally
    equivalent cards differ in all of those by definition, so including them would score every
    functional pair as divergent and measure nothing. scope="full" keeps them; T1 reports both.

    `card_name`, when given, is replaced by `~` wherever it appears, so a card's self-reference
    doesn't make two otherwise-identical encodings differ.

    `mask_numbers` / `mask_symbols` replace numeric literals and mana symbols/colour words with
    placeholders. This is how "did the parser take a different branch?" is separated from "did it
    fill the same slot with a different value?" — two signets producing {U}{B} and {U}{R} have
    identical structure, and scoring them as divergent measures nothing about the representation.
    """
    text = _strip_comments(cdl)
    if card_name:
        text = text.replace(f'"{card_name}"', '"~"').replace(card_name, "~")

    # Alpha-rename bindings to order of first appearance: $found and $x are the same shape.
    seen: dict[str, str] = {}
    for m in _BINDING.findall(text):
        seen.setdefault(m, f"${len(seen)}")
    if seen:
        pattern = re.compile("|".join(re.escape(k) for k in sorted(seen, key=len, reverse=True)))
        text = pattern.sub(lambda m: seen[m.group(0)], text)

    # Masking separates "took a different branch" from "filled a slot differently". Two signets
    # differ only in which mana symbol lands in ADD_MANA; that is the parser working, not a
    # divergence. With symbols masked, a remaining difference is structural.
    if mask_numbers:
        text = _NUM.sub("N", text)
    if mask_symbols:
        text = _COLOR_WORD.sub("COLOR", _MANA_SYM.sub("{M}", text))

    nodes, _ = _parse_block(_tokenize(text), 0)
    if scope == "rules":
        nodes = _drop_header(nodes)
    elif scope != "full":
        raise ValueError(f"scope must be 'rules' or 'full', got {scope!r}")
    return _render(nodes, sort_all, parent_head="CARD")


def constructs(cdl: str) -> set[str]:
    """Every CDL construct keyword used. Feeds T0 failure triage and T3's rare-construct stratum."""
    return set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", _strip_comments(cdl)))
