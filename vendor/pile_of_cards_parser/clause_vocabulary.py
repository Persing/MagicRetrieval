"""
The declared vocabulary of the clause-splitting schema -- single source of
truth, imported by both the gold-file checker (validate_gold_schema.py) and
the corpus-output checker (validate_corpus_invariants.py).

These were previously defined inline in validate_gold_schema.py. They moved
here so both checkers agree on what "declared" means: the whole point of the
corpus check is to detect where PIPELINE OUTPUT has drifted away from what
the GOLD SCHEMA declares, and that comparison is meaningless if the two
files keep their own copies of the enum.

Authority: clause_gold_v1.json's `_schema._tag_vocabulary` and `_decisions`.
When a value is added here, it should be added there too (and vice versa).
"""

VALID_TYPES = {"TRIGGER", "COST", "CONDITION", "ACTION", "STATIC", "REPLACEMENT", "CHOICE"}
VALID_CONF = {"RULE", "BOOTSTRAP", "UNCLASSIFIED"}
VALID_TARGET_SCOPE = {"SELF", "TARGET", "ALL", "NONE", "EACH_OPPONENT", "EACH_PLAYER"}
VALID_RESTRICTION = {"YOU", "OPPONENT", "OPPONENTS", "NOT_YOU", "ALL_PLAYERS", "DEFENDING_PLAYER", "THAT_PLAYER",
                      "TARGET_PLAYER", "TARGET_OPPONENT"}
VALID_CHOOSER = {"YOU", "OPPONENT", "OPPONENTS", "ALL_PLAYERS", "EACH_PLAYER", "DEFENDING_PLAYER",
                  "THAT_PLAYER", "RANDOM", "CONTROLLER_OF"}
VALID_MODE_KIND = {"EFFECT_MODES", "ABILITY_SELECTION", "PARAMETER_VALUES", "CONDITION_OPTIONS", "VOTE_OPTIONS"}
VALID_MODE_RECORD_KIND = {"EFFECT", "ABILITY", "PARAMETER", "VOTE_OPTION"}
VALID_VALUE_KIND = {"LITERAL", "VAR", "COUNT", "SCALED", "CHARACTERISTIC"}
VALID_SEGMENT_KIND = {"SPELL", "ACTIVATED", "TRIGGERED", "STATIC", "KEYWORD", "SAGA_CHAPTER", "REMINDER", "LOYALTY_ABILITY"}
BASE_VALUE_FIELDS = {"role", "kind", "value", "var_name", "binding", "expr_span", "bound"}
KNOWN_TAG_KEYS = {"modality", "chooser", "target_scope", "restriction", "target_type", "zone", "duration",
                   "refers_to_prior", "refers_to", "condition_kind", "is_mana_ability", "cost_components",
                   "keyword", "event", "phase", "whose_turn", "excludes", "distributive",
                   "restriction_kind", "restricted_action", "during"}
VALID_RESTRICTION_KIND = {"CANT", "ONLY"}
# ACTIVATED-ability timing restriction (CDL Section 19.2 DURING, parser.py's
# own `_infer_during`/`DURING:` field -- reused here rather than inventing a
# separate vocabulary). BEFORE_ATTACKERS is a clause-pipeline-only extension
# CDL doesn't model either (see clause_pipeline_known_issues.md).
VALID_DURING = {"SORCERY_SPEED", "YOUR_TURN", "OPPONENTS_TURN", "BEFORE_ATTACKERS"}

# ─────────────────────────────────────────── declared-but-gold-only vs pipeline

# Tag/value keys the PIPELINE emits that gold's vocabulary does not declare.
# These are legitimate pipeline-only output, not drift: they are the
# structural-linking stage's resolutions, which gold expresses as clause_id
# references (`refers_to`, `excludes`) but the pipeline expresses as the
# antecedent's own PHRASE, because the real pipeline has no clause ids at
# all (see clause_pipeline_handoff.md's stage-7 design note).
PIPELINE_ONLY_TAG_KEYS = {
    "excludes_prior",       # tag_extractor: "all other X" needs cross-clause resolution
    "resolved_excludes",    # structural_linker: which prior clause it excludes
    "resolved_reference",   # structural_linker: the antecedent phrase
}
PIPELINE_ONLY_VALUE_FIELDS = {
    "resolved_binding",     # structural_linker: the phrase a dynamic value binds to
}

# Tag keys the pipeline never emits, by design -- listed so the corpus check
# can report "declared but unreachable" without treating it as a failure.
GOLD_ONLY_TAG_KEYS = {
    "refers_to",            # pipeline resolves to a phrase, never a clause_id
}

# Values that are OBSERVED in pipeline output but not declared in gold's
# `_tag_vocabulary`. Recorded here rather than silently widening the enum,
# so the drift stays visible and gets a deliberate ruling. See
# clause_pipeline_known_issues.md.
OBSERVED_UNDECLARED = {
    "duration": {"THIS_TURN", "WHILE_SOURCE_ON_BATTLEFIELD"},
}

# Closed enums worth value-checking on pipeline output. Deliberately EXCLUDES
# target_type (declared open-ended, "multi-valued allowed"), event, phase,
# restricted_action and role (all open, text-derived).
CLOSED_TAG_ENUMS = {
    "target_scope": VALID_TARGET_SCOPE,
    "restriction": VALID_RESTRICTION,
    "chooser": VALID_CHOOSER,
    "restriction_kind": VALID_RESTRICTION_KIND,
    "condition_kind": {"GAME_STATE", "PRIOR_ACTION_SUCCEEDED"},
    "whose_turn": {"YOURS", "EACH_OPPONENT", "ANY"},
    "zone": {"BATTLEFIELD", "GRAVEYARD", "HAND", "LIBRARY", "EXILE", "COMMAND"},
    "duration": {"UNTIL_EOT", "UNTIL_YOUR_NEXT_TURN", "PERMANENT"},
    "modality": {"MAY", "MUST"},
    "during": VALID_DURING,
}

VALID_BOUND = {"EXACT", "UP_TO", "AT_LEAST"}
