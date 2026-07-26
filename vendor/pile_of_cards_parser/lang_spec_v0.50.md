# CDL Specification — v0.50

-----

## 1. Overview

The Card Description Language (CDL) is a structured, machine-readable format for encoding the rules text of Magic: The Gathering cards. CDL captures game actions, conditions, and effects in a way that is unambiguous, consistently structured, and suitable for downstream interpretation by a rules engine.

**Syntax conventions:**

- Blocks are delimited with `{` and `}`
- Mana expressions always appear as quoted strings: `"{2}{W}{U}"`. Bare `{` and `}` are always structural delimiters. Hybrid mana symbols use slash notation inside the quotes: `"{B/G}"`, `"{W/U}"`. Phyrexian mana uses a `P` suffix: `"{W/P}"`. Multi-symbol expressions concatenate normally: `"{B/G}{B/G}"`. Repeated colorless mana is written with repeated `{C}` symbols: `"{C}{C}"`.
- Fields marked `?` are optional
- `<angle brackets>` denote variable slots
- `#` introduces a comment to end of line
- `@` prefix denotes a keyword macro (e.g. `@FLYING`, `@WARD("{2}")`). Block-form keyword macros (e.g. `@EQUIP { }`) may establish named variable bindings (e.g. `$equipped`) that are visible to all sibling ability blocks on the same card
- Event descriptors use `WHEN_` prefix for triggered abilities (go on stack, can be responded to) and `AS_` prefix for simultaneous/replacement events (no stack, cannot be responded to). See Section 5.
- `$` prefix denotes a variable binding (e.g. `$X`, `$chosen`). `$X`, `$Y`, `$Z` match the variable names on the card. When `{X}`, `{Y}`, or `{Z}` appear in the mana cost, they are auto-bound at cast time from the mana paid. When no such symbol appears in the mana cost, `$X`/`$Y`/`$Z` must be explicitly bound using `AS` or an inline definition. All other `$name` variables must also be explicitly bound.
- `AS $name` binds the result of a `TARGET`, `CHOOSE`, or scalar expression to a named variable for use downstream
- **Variable scoping:** Variables are ability-scoped by default — declared and used within one ability block, discarded after resolution. When a variable is referenced across ability blocks on the same card, the engine infers card-scope and persists the binding for the card’s lifetime on the battlefield. Use `DECLARE:` at the card level for explicit forward declarations where inference is insufficient (see Section 2)
- `.` accesses a property of a bound variable (e.g. `$target.power`, `$target.controller`). Dot notation chains are valid — e.g. `$target.controller.library`. Dot notation on a **collection** maps over members returning a list; on a **leaf property** returns the value directly (integer, string, boolean).
- `."name"` quoted dot notation accesses properties whose names contain special characters: `SELF.counters."+1/+1"`, `SELF.counters."depletion"`
- `!` prefix denotes an **immutable snapshot** variable, capturing a value at cast time. Unlike `$` reference variables which are evaluated at resolution time, `!` variables are permanently bound at cast — `!target.controller` is bound to whoever controlled the target when the spell was cast, regardless of what happens before resolution. `!` and `$` are mutually exclusive sigils — `!$var` is never valid
- `.*` wildcard in dot notation flattens all children of a collection into a single collection: `SELF.counters.*` returns all counter instances
- Type values (`TYPES`, `SUBTYPES`, `SUPERTYPES`) are always string literals in lists: `["Creature"]`
- Mana expressions support arithmetic operators (`+`, `-`) in computed contexts — see Section 13 for mana expression arithmetic. The engine floors results at `"{0}"` per rule 601.2b.
- `OUTSIDE_GAME` is a special zone value for cards outside the game. Distinct from `ANY`.

-----

## 2. Top-Level Card Structure

```
CARD {
  NAME: "<card name>"
  MANA_COST?: "<mana expression>"     # omitted for lands
  SUPERTYPES?: ["<supertype>", ...]   # e.g. ["Legendary"]
  TYPES: ["<type>", ...]              # e.g. ["Artifact", "Creature"]
  SUBTYPES?: ["<subtype>", ...]       # e.g. ["Human", "Artificer"]
  POWER?: <N | STAR | STAR+N | STAR-N | $X>   # creatures only; STAR = defined by STAT block
  TOUGHNESS?: <N | STAR | STAR+N | STAR-N | $X>   # creatures only; STAR = defined by STAT block
  LOYALTY?: <N>                       # planeswalkers only
  DEFENSE?: <N>                       # battles only
  ENTERS?: TAPPED                     # omit if untapped (default)
  ZONE?: <zone>                       # zone this card may be cast from; default: YOU.hand (omit for standard hand casting)
  COST_REDUCTION?: <cost expression>  # optional cost modification; see Section 2.1
  # CANT_BE_COUNTERED retired — use STATIC { EFFECT: [ CANT { SELF EFFECTS: [COUNTER] } ] }
  ADDL_COST?: <cost expression>       # additional cost paid on top of mana cost; see Section 2.3
  DECLARE?: <variable>               # explicit card-scoped variable declaration; use when cross-block inference is insufficient. Multiple DECLARE fields permitted.
  CONSTRUCTED_OVERRIDE?: { ... }     # card-level deck construction overrides; see Section 2.5

  # keyword macros — expand to ability blocks at engine level
  @<keyword>
  @<keyword> { EFFECT: [ ... ] }      # block-parameterized keyword (e.g. @LANDFALL)
  ...

  # ability blocks — appear directly in card body, no wrapper list
  ACTIVATED { ... }
  TRIGGERED { ... }
  STATIC { ... }
  STAT { ... }              # continuous base stat definition; required when POWER/TOUGHNESS uses STAR
  SPELL { ... }
  MANA_ABILITY { ... }
  KEYWORD("<name>") { ... }           # named keyword block; see Section 2.2
  ...
}
```

`POWER`, `TOUGHNESS`, `LOYALTY`, and `DEFENSE` only appear on card types where they are valid. The engine is responsible for enforcing this. Keywords and ability blocks may appear in any order after the header fields, though convention is keywords first, then ability blocks in oracle text order.

### 2.1 COST_REDUCTION

`COST_REDUCTION` is an optional top-level card field that expresses a continuous cost modification applied while the spell is being cast. It accepts a static mana expression or a COUNT-scaled expression:

```
COST_REDUCTION: "{1}"
COST_REDUCTION: "{1}" * COUNT(BATTLEFIELD FILTER { TYPE: "Creature" })
```

The floor — that cost reduction cannot reduce a spell below its minimum castable cost — is an engine and rules responsibility and is not expressed in CDL.

Affinity (rule 702.41, "Affinity for X") is not a symbolic keyword tag — it fully expands into this COUNT-scaled form (always `"{1}"`, always `CONTROLLER: YOU`, unlike an evasion-style keyword such as `@FEAR`/`@SHADOW` with no computable effect): `COST_REDUCTION: "{1}" * COUNT(BATTLEFIELD FILTER { TYPE: "Artifact" CONTROLLER: YOU })` for "Affinity for artifacts".

### 2.2 ALT_COST Block Form

`ALT_COST` declares an alternative cost that may be paid instead of the mana cost when casting. It accepts the same fields as the `COST` block directly — no double wrapping needed:

```
ALT_COST {
  CONDITION?: <condition expression>   # must be true at cast time for this alt cost to be available
  MANA?: "<mana expression>"
  TAP?: SELF | $<variable>
  UNTAP?: SELF | $<variable>
  SACRIFICE?: <object descriptor | CHOOSE { ... }>
  DISCARD?: <object descriptor>
  PAY_LIFE?: <N>
  REMOVE_COUNTER?: { NAME: "<counter name>" }
  EXILE?: <object descriptor>      # exile as cost — Force of Will pattern
}
```

`EXILE` as a cost field is distinct from `EXILE { }` as an effect block — it expresses exiling an object as payment. `CONDITION` guards whether the alternate cost is available — if false, the player may not choose this alt cost.

```
# Force of Will pattern
ALT_COST {
  PAY_LIFE: 1
  EXILE: CHOOSE {
    FROM: YOU.hand
    COUNT: 1
    FILTER { COLOR: U }
  }
}
```

### 2.3 ADDL_COST

`ADDL_COST` declares an additional cost paid on top of the mana cost when casting.

```
ADDL_COST: SACRIFICE {
  FILTER { TYPE: ["Artifact", "Creature"] CONTROLLER: YOU }
  COUNT: 1
}
ADDL_COST: PAY_LIFE: $X
ADDL_COST: DISCARD { COUNT: 1 }
```

When both `ADDL_COST` and `ALT_COST` are present, the engine handles simultaneous payment.

### 2.4 KEYWORD Blocks

`KEYWORD("<n>")` blocks encode keywords that provide an alternate way to cast a card, using an `ALT_CAST` block. Use `EFFECT: [ CAST { SELF } ]` to run the card’s normal spell effect unchanged, or define a `SPELL` block to replace it entirely.

**Parameter syntax:** Named slots declared with `(PARAM: <slot>)` in the definition. Single-parameter call sites may omit the name (positional); multi-parameter requires named arguments.

```
KEYWORD("Flashback")(MANA: "<mana>") {
  ALT_CAST {
    MANA: "<mana>"
    CAST_FROM: YOU.graveyard
    ON_RESOLVE: [ EXILE { SELF } ]
    EFFECT: [ CAST { SELF } ]
  }
}

KEYWORD("Harmonize")(MANA: "<mana>") {
  ALT_CAST {
    MANA: "<mana>"
    CAST_FROM: YOU.graveyard
    TAP {
      COUNT: RANGE 0..ALL
      FILTER { TYPE: "Creature" CONTROLLER: YOU NOT { SELF } }
    } AS $tapped
    COST_REDUCTION: SUM($tapped.power)
    ON_RESOLVE: [ EXILE { SELF } ]
    EFFECT: [ CAST { SELF } ]
  }
}

KEYWORD("Overload")(MANA: "<mana>") {
  ALT_CAST {
    MANA: "<mana>"
    SPELL {
      EFFECT: [
        MOVE {
          EACH AS $permanent {
            FILTER { CATEGORY: NONLAND CONTROLLER: OPPONENT }
          }
          TO: $permanent.owner.hand
        }
      ]
    }
  }
}
```

`ALT_CAST` fields: `MANA` — cost for this alternate cast; `CAST_FROM` — source zone (default `YOU.hand`); `TAP` — optional tap-creatures block binding a collection; `COST_REDUCTION` — reduction applied to this cast; `ON_RESOLVE` — effects on resolution; `EFFECT: [ CAST { SELF } ]` — run the card’s normal spell effect.
**Example:**

```
CARD {
  NAME: "Birds of Paradise"
  MANA_COST: "{G}"
  TYPES: ["Creature"]
  SUBTYPES: ["Bird"]
  POWER: 0
  TOUGHNESS: 1

  @FLYING

  MANA_ABILITY {
    COST: TAP
    ADD_MANA: "{Any}"
  }
}
```

### 2.5 CONSTRUCTED_OVERRIDE

`CONSTRUCTED_OVERRIDE` encodes card-level deck construction rules.

```
CONSTRUCTED_OVERRIDE {
  COPY_LIMIT?: ANY_NUMBER | <N>
  COMPANION?: { CONDITION: <condition expression> }
  PARTNER?: TRUE | WITH: "<card name>" | FRIENDS_FOREVER | DOCTORS_COMPANION
  BACKGROUND?: TRUE
}
```

```
CONSTRUCTED_OVERRIDE { COPY_LIMIT: ANY_NUMBER }  # Rat Colony pattern
```

-----

## 3. Ability Block Types

Ability blocks appear directly in the card body after header fields. Each block is identified by its type keyword.

```
ACTIVATED { ... }    # activated ability
TRIGGERED { ... }    # triggered ability
STATIC { ... }       # static/continuous ability
SPELL { ... }        # one-time effect (instants, sorceries)
MANA_ABILITY { ... }     # mana ability (see Section 7.5)
LOYALTY_ABILITY { ... }  # planeswalker loyalty ability (see Section 7.6)
```

-----

## 4. Activated Abilities

```
ACTIVATED {
  ZONE?: <zone>          # zone this ability may be activated from; default: BATTLEFIELD (omit for standard)
  COST: "<mana expression>" | COST { ... }
  DURING?: <during expression>   # window during which this ability may be activated; see Section 19.2
  CONDITION?: <condition expression>   # must be true at activation time for ability to be legally activated
  DEPLETE?: <CUMULATIVE>               # rate-limiting — CUMULATIVE counts total activations across all instances this turn
  LIMIT?: <N>                          # max activations before depleted; default 1
  REPLENISH?: <EACH_TURN>              # when depletion resets
  EFFECT: [ <effect block> ... ]
}
```

**Cost expressions — shorthand:**

```
COST: "{1}{G}"           # pure mana cost shorthand — valid for simple cases
```

**Cost block — for complex costs:**

```
COST {
  MANA?: "<mana expression>"
  TAP?: SELF | $<variable>                          # tapping action as cost — single object
  TAP { COUNT: <N | RANGE N..M | ALL> FILTER?: <FILTER { ... }> } AS $<n>?
                                                    # tapping action block — always battlefield-scoped, binds tapped collection
  THRESHOLD?: <scalar expression> <comparison>      # post-selection validity check on aggregate (e.g. THRESHOLD: SUM($crew.power) GTE <N>)
  UNTAP?: SELF | $<variable>                        # untapping action as cost
  SACRIFICE?: <object descriptor | CHOOSE { ... }>
  DISCARD?: <object descriptor>
  PAY_LIFE?: <N>
  REMOVE_COUNTER?: { NAME: "<counter name>" }
  ADD_COUNTER?: { NAME: "<counter name>" COUNT?: <N> TARGET?: <object descriptor | CHOOSE { ... }> }
                                                    # counters as a cost — the reverse of REMOVE_COUNTER;
                                                    # TARGET defaults to SELF when omitted
  REDUCTION?: "<mana expression>" | "<symbol>" * COUNT(<collection>) | SUM(<collection>.<property>)   # cost reduction scoped to this activation
}
```

`TAP` and `UNTAP` in a cost block express the action of tapping or untapping as payment — distinct from `TAPPED: TRUE` in filters (state) and `TAP { }` / `UNTAP { }` in effect lists (mass effect). `REDUCTION` inside `COST` scopes the reduction to this specific activation; use the card-level `COST_REDUCTION` for spell casting reductions.

`ZONE` declares where the ability may be activated from. Omit for the default (battlefield). Common values:

```
ZONE: YOU.hand           # Channel pattern
ZONE: YOU.graveyard      # Unearth, some black abilities
ZONE: ANY                # activatable from any zone
```

`DURING` constrains when the ability may be activated. Omit if the ability follows default timing rules (instant speed). See Section 19.2 for valid values — `DURING: SORCERY_SPEED` replaces the former `TIMING_RESTRICTION: SORCERY`; `DURING: YOUR_TURN` replaces `TIMING_RESTRICTION: YOUR_TURN`.

`CONDITION` guards whether the ability may legally be activated — evaluated at activation time. Distinct from `DURING` which expresses phase/turn scope: `DURING` restricts *when*, `CONDITION` restricts *whether* based on game state. Uses the same condition expression vocabulary as `TRIGGERED` and `STATIC`.

`DEPLETE`, `LIMIT`, and `REPLENISH` rate-limit activations — same semantics as on `TRIGGERED`. `DEPLETE: CUMULATIVE LIMIT: 1 REPLENISH: EACH_TURN` expresses “activate only once each turn.” Also valid on `MANA_ABILITY`.

```
# Activate only if you've cast three or more spells this turn
ACTIVATED {
  COST: TAP
  CONDITION: COUNT(YOU.spells_this_turn) GTE 3
  EFFECT: [ ... ]
}

# Activate only once each turn (Vivi Ornitier pattern)
MANA_ABILITY {
  COST: "{0}"
  DURING: YOUR_TURN
  DEPLETE: CUMULATIVE
  LIMIT: 1
  REPLENISH: EACH_TURN
  ADD_MANA: { ["{U}", "{R}"] COUNT: SELF.power }
}
```

-----

## 5. Triggered Abilities

```
TRIGGERED {
  <event block> | EVENTS: [ <event block>, <event block>, ... ]
  DURING?: <during expression>       # restricts which turn/phase this trigger is eligible to fire; see Section 19.2
  CONDITION?: <condition expression>
  DEPLETE?: <CUMULATIVE>             # rate-limiting — CUMULATIVE counts total firings across all instances this turn
  LIMIT?: <N>                        # max firings before depleted; default 1
  REPLENISH?: <EACH_TURN>            # when depletion resets
  EFFECT: [ <effect block> ... ]
}
```

**Simultaneous event blocks** (`AS_` prefix) — fire as part of the event, not after it. No stack, cannot be responded to. Per rule 614.12a choices in `AS_ETB` happen before the permanent enters:

```
AS_ETB { EFFECT: [ <effect block> ... ] }    # simultaneous with ETB
AS_EQUIP { EFFECT: [ <effect block> ... ] }  # simultaneous with equip
AS_DAMAGE { EFFECT: [ <effect block> ... ] } # used in REPLACE for damage modification
# AS_ form available for any event type
```

Each triggered ability contains exactly one event block identifying what triggers it, **or** an `EVENTS` list naming more than one — the ability fires when *any* listed event occurs, running the same `CONDITION`/`EFFECT`. This is the "X or Y" compound-trigger idiom ("Whenever this creature enters or attacks, ...", "Whenever this creature enters or dies, ..."), one ability with two possible causes rather than two separate abilities that happen to share text. `$event` inside `EFFECT` binds to whichever event actually fired, same as the single-event form. The event block contains a `FILTER` specifying the subject (the object or player whose action triggers the ability). `CONDITION` and `EFFECT` remain flat at the `TRIGGERED` level.

```
# "Whenever this creature enters or attacks, it deals 3 damage..." (Inferno Titan)
TRIGGERED {
  EVENTS: [
    @WHEN_ETB { FILTER { SELF } }
    WHEN_ATTACKS { FILTER { SELF } }
  ]
  EFFECT: [ ... ]
}
```

**Event blocks:**

```
# — Action events — WHEN_ = triggered (stack), AS_ = simultaneous (no stack) —
@WHEN_ETB             { FILTER { <filter criteria> } }  # macro: WHEN_ZONE_CHANGE { TO: BATTLEFIELD }
AS_ETB                { FILTER { <filter criteria> } }  # simultaneous with ETB — choices before entry per rule 614.12a
WHEN_DIES             { FILTER { <filter criteria> } }  # permanent put into graveyard from battlefield
AS_DIES               { FILTER { <filter criteria> } }  # simultaneous with death event
WHEN_ATTACKS          { FILTER { <filter criteria> } }  # a creature attacks; $event.defending = defending player/planeswalker/battle
WHEN_BLOCKS           { FILTER { <filter criteria> } }  # a creature blocks
WHEN_CAST             { FILTER { <filter criteria> } }  # a spell is cast
AS_CAST               { FILTER { <filter criteria> } }  # simultaneous with cast event
@WHEN_DRAW            { FILTER { <player descriptor> } } # macro: WHEN_ZONE_CHANGE { FROM: <owner>.library TO: <owner>.hand }
AS_DRAW               { FILTER { <player descriptor> } } # simultaneous with draw event
WHEN_DAMAGE_DEALT     { FILTER { <filter criteria> } COMBAT?: <BOOLEAN> TARGET_TYPE?: <PLAYER | CREATURE | PLANESWALKER | BATTLE> }  # a permanent deals damage
AS_DAMAGE             { FILTER { <filter criteria> } COMBAT?: <BOOLEAN> }  # simultaneous with damage — use in REPLACE
WHEN_CYCLE            { FILTER { <filter criteria> } }  # a card is cycled
WHEN_DISCARD          { FILTER { <filter criteria> } }  # a card is discarded
AS_DISCARD            { FILTER { <filter criteria> } }  # simultaneous with discard
WHEN_COUNTER_REMOVED  { FILTER { <filter criteria> } }  # a counter is removed from an object
WHEN_COUNTER_ADDED    { FILTER { <filter criteria> } }  # a counter is placed on an object
AS_COUNTER_ADDED      { FILTER { <filter criteria> } }  # simultaneous with counter placement
WHEN_LEAVES_BATTLEFIELD { FILTER { <filter criteria> } } # a permanent leaves the battlefield
AS_LEAVES_BATTLEFIELD   { FILTER { <filter criteria> } } # simultaneous with leaves-battlefield
WHEN_SACRIFICE        { FILTER { <filter criteria> } }  # a permanent is sacrificed — distinct from WHEN_DIES,
                                                         # which fires for any battlefield-to-graveyard move
                                                         # regardless of cause (destroy, lethal damage, etc.)
WHEN_CREATE_TOKEN     { FILTER { <filter criteria> } }  # one or more tokens are created
AS_CREATE_TOKEN       { FILTER { <filter criteria> } }  # simultaneous with token creation — use in REPLACE
WHEN_EQUIP            { FILTER { <filter criteria> } }  # equipment becomes attached (triggered)
AS_EQUIP              { FILTER { <filter criteria> } }  # simultaneous with equip — choices as part of attach
WHEN_GAIN_LIFE        { FILTER { <player descriptor> } }  # a player gains life
AS_GAIN_LIFE          { FILTER { <player descriptor> } }  # simultaneous with life gain — use in REPLACE
WHEN_TAPPED           { FILTER { <filter criteria> } }  # a permanent becomes tapped, regardless of cause
                                                         # (attacking, activation cost, effect, ...)

# — Zone change primitive —
WHEN_ZONE_CHANGE {
  FILTER { <filter criteria> }
  FROM?: <zone>
  TO?: <zone>
}  # @WHEN_ETB and @WHEN_DRAW expand to this

# — Phase/step events — all triggered (WHEN_), go on stack —
# FILTER is optional — omitting it defaults to ANY (any player's occurrence of this step)
# e.g. WHEN_END_STEP_BEGIN { } = any player's end step; WHEN_UPKEEP_BEGIN { FILTER { YOU } } = your upkeep only
WHEN_UNTAP_STEP_BEGIN      { FILTER { <player descriptor> } }  # beginning of untap step
WHEN_UPKEEP_BEGIN          { FILTER { <player descriptor> } }  # beginning of upkeep step
WHEN_DRAW_STEP_BEGIN       { FILTER { <player descriptor> } }  # beginning of draw step
WHEN_PRECOMBAT_MAIN_BEGIN  { FILTER { <player descriptor> } }  # beginning of precombat main phase
WHEN_COMBAT_BEGIN          { FILTER { <player descriptor> } }  # beginning of combat step
WHEN_DECLARE_ATTACKERS     { FILTER { <player descriptor> } }  # declare attackers step
WHEN_DECLARE_BLOCKERS      { FILTER { <player descriptor> } }  # declare blockers step
WHEN_COMBAT_DAMAGE_RESOLVE { FILTER { <player descriptor> } }  # combat damage step
WHEN_COMBAT_END            { FILTER { <player descriptor> } }  # end of combat step
WHEN_POSTCOMBAT_MAIN_BEGIN { FILTER { <player descriptor> } }  # beginning of postcombat main phase
WHEN_END_STEP_BEGIN        { FILTER { <player descriptor> } }  # beginning of end step
WHEN_CLEANUP_BEGIN         { FILTER { <player descriptor> } }  # beginning of cleanup step
```

`SELF` is contextual in CDL:

- In the **card body** (outside event blocks), `SELF` refers to the card itself.
- In **event blocks**, `SELF` refers to the triggering event. `SELF.player` binds the player who caused the event (e.g. the player who drew a card, cast a spell, or took an action).

```
@WHEN_ETB { FILTER { SELF } }       # SELF = the card itself (card body context)
WHEN_ATTACKS { FILTER { SELF } }   # SELF = the card itself (card body context)
```

**Subject binding** — event blocks may bind a subject expression to a variable for downstream reference using `AS`. This is separate from the filter and placed before it:

```
WHEN_CAST {
  SELF.source.controller AS $caster   # SELF = triggering event; .source = spell; .controller = caster
  FILTER { CONTROLLER: OPPONENT }
}

@WHEN_DRAW {
  SELF.player AS $drawer         # SELF = triggering event; .player = drawing player
  FILTER { CONTROLLER: OPPONENTS }
}
```

The binding captures a property of the triggering event at trigger time for use in the effect list.

**Event property accessors** — available on all event blocks via `SELF`:

```
SELF.source       # the object initiating the event (damage dealer, casting player, etc.)
SELF.recipient    # the object receiving the event (damage receiver, defending player, etc.)
```

On `WHEN_CAST` events specifically, `SELF.source` is the spell object on the stack; `.controller` accesses its controller (`SELF.source.controller`).

These are neutral terms — distinct from MTG’s “target” which has specific rules meaning. `WHEN_ATTACKS` events additionally expose:

```
$event.defending    # the player, planeswalker, or battle the attacker is declared against
```

For token creation events, additional properties are available via `$event` (implicit binding in `REPLACE` `WITH` blocks — see Section 17):

```
$event.tokens          # collection of tokens being created
$event.tokens.count    # total count of tokens being created
```

For counter events (`WHEN_COUNTER_ADDED`, `WHEN_COUNTER_REMOVED`), the count of counters of a specific type placed or removed in this event is available via dot notation:

```
$event.counters."<type>"    # integer count of counters of that type added/removed in this event
                            # e.g. $event.counters."+1/+1", $event.counters."charge"
```

For life gain events (`WHEN_GAIN_LIFE`, `AS_GAIN_LIFE`), the bound event exposes:

```
$event.amount       # integer — amount of life being gained
$event.recipient    # player — who is gaining the life
```

```
# Double all life gain for you (The Wind Crystal pattern)
REPLACE {
  EVENT: AS_GAIN_LIFE { FILTER { YOU } } AS $event
  WITH: [
    GAIN_LIFE FROM $event { AMOUNT: $event.amount * 2 }
  ]
}
```

**Examples:**

```
# Triggers when this permanent enters
TRIGGERED {
  @WHEN_ETB { FILTER { SELF } }
  EFFECT: [ ... ]
}

# Triggers at the beginning of your upkeep if a condition is met
# OPPONENT in condition expressions uses indefinite-any semantics — true if any opponent satisfies the check
TRIGGERED {
  WHEN_UPKEEP_BEGIN { FILTER { YOU } }
  CONDITION: OPPONENT IS MONARCH
  EFFECT: [ ... ]
}

# Triggers when any creature an opponent controls dies
TRIGGERED {
  WHEN_DIES { FILTER { TYPE: "Creature" CONTROLLER: OPPONENT } }
  EFFECT: [ ... ]
}

# Triggers when an opponent casts a spell, binding the caster for downstream use
TRIGGERED {
  WHEN_CAST {
    SELF.source.controller AS $caster
    FILTER { CONTROLLER: OPPONENT }
  }
  EFFECT: [ ... ]
}
```

-----

## 6. Static Abilities

```
STATIC {
  DURING?: <during expression>         # temporal scope during which this effect is active; see Section 19.2
  CONDITION?: <condition expression>   # guards whether the static effect is active
  MAY?: <BOOLEAN>                      # if TRUE, the effect is optional — controller may choose not to apply it
  COST?: <cost expression>             # optional cost associated with this static effect; paired with MAY: TRUE
  FILTER?: <FILTER { ... }>            # scopes which objects the effect applies to
  EFFECT: [ <static effect declaration> ... ]
}
```

Static effects are continuous and do not use the stack. `DURING` is optional — when present, the effect is only active during the specified phase or turn scope (replaces the former `CONDITION: PLAYER_TURN:` pattern). `CONDITION` is optional — when present, the static effect is only active while the condition holds. `FILTER` is optional — when present, it scopes which objects the effect applies to. See Section 15.25 for static effect declarations and Section 15.26 for `GRANT`.

`MAY: TRUE` makes the entire static effect optional — the controller may choose not to apply it. `COST` pairs with `MAY: TRUE` to express an optional cost: if the player opts in, they pay the cost and the effect applies. When `DURING: CAST` is present, this models static abilities that function while a spell is being cast — such as optional additional costs (Offspring pattern). A `STATIC` block with `COST` is not an activated ability — it does not use the stack.

```
# Offspring pattern — optional additional cost active at cast time
STATIC {
  DURING: CAST
  MAY: TRUE
  COST: MANA: "{2}"
  EFFECT: [
    DELAYED_TRIGGER {
      WHEN_ETB { FILTER { SELF } }
      EFFECT: [
        CREATE_TOKEN {
          COPY_OF: SELF
          POWER: 1
          TOUGHNESS: 1
        }
      ]
    }
  ]
}
```

-----

## 7. Spell Blocks

```
SPELL {
  EFFECT: [ <effect block> ... ]
}
```

Used for instants and sorceries where the card itself is the source of a one-time effect on resolution.

-----

## 7.5. Mana Ability Blocks

```
MANA_ABILITY {
  COST: <cost expression>
  DURING?: <during expression>         # window during which this ability may be activated; see Section 19.2
  DEPLETE?: <CUMULATIVE>               # rate-limiting — CUMULATIVE counts total activations across all instances this turn
  LIMIT?: <N>                          # max activations before depleted; default 1
  REPLENISH?: <EACH_TURN>              # when depletion resets
  ADD_MANA: "{Any}" | { <color spec> COUNT?: <N | scalar expression> MODE?: <ALL | UNIFORM> }
  EFFECT?: [ <effect block> ... ]      # optional side effects; does not affect mana ability classification
  GRANT_ON_SPEND?: <grant block>       # grants properties to spells cast with this mana; also restricts spend
  SPEND_ONLY?: {                       # restricts what this mana may be spent on
    FILTER { <filter criteria> }       # only spells matching this filter may be paid for with this mana
  }
}
```

`MANA_ABILITY` blocks follow the same cost structure as `ACTIVATED` but resolve immediately without using the stack and cannot be responded to. Per MTG rule 605.1a, an activated ability is a mana ability if it could add mana, requires no target, and is not a loyalty ability — regardless of any side effects it may generate. `ADD_MANA` accepts two forms: **`"{Any}"`** — special constant for any single color; or **block form** `{ <color spec> COUNT?: <N | scalar> MODE?: <ALL | UNIFORM> }` where `COLORS:` label is optional for literals, required for property accessors, always permitted, and `COUNT` defaults to 1. `COLORS` accepts a quoted symbol, list of symbols, or property accessor (e.g. `YOU.commander_identity`). The former `CHOICE [ ]` and `"<symbol>" * COUNT(...)` syntaxes are retired. `SPEND_ONLY` restricts what the produced mana may be spent on — the `FILTER` inside describes the spells it *can* pay for; anything not matching is forbidden. `GRANT_ON_SPEND` grants a property to the spell the mana is spent on — the grant applies at cast time and may also imply a spend restriction (e.g. granting `CANT { EFFECTS: [COUNTER] }` makes the spell uncounterable when cast with this mana).

```
# Delighted Halfling — add any color mana, spend only on legendary spells; those spells can't be countered
MANA_ABILITY {
  COST: TAP
  ADD_MANA: "{Any}"
  SPEND_ONLY: { FILTER { SUPERTYPE: "Legendary" } }
  GRANT_ON_SPEND: CANT { EFFECTS: [COUNTER] }
}
```

`MODE` controls how multiple mana are produced when `COUNT > 1`:

- **No `MODE`** (default) — each mana is independently chosen from the color list. “Any combination” semantics — e.g. you may choose {U}, {U}, {R} from a list of [{U},{R}].
- **`MODE: ALL`** — add one mana of each color in the list. Total produced = number of colors in list, ignoring `COUNT`.
- **`MODE: UNIFORM`** — choose one color from the list once, produce `COUNT` mana all of that color. “Add N mana of any one color” pattern.

```
# Tap for green
MANA_ABILITY {
  COST: TAP
  ADD_MANA: { "{G}" }
}

# Tap for any one color
MANA_ABILITY {
  COST: TAP
  ADD_MANA: "{Any}"
}

# Tap for blue or black, deals 1 damage to you (still a mana ability per rule 605.1a)
MANA_ABILITY {
  COST: TAP
  ADD_MANA: { ["{U}", "{B}"] }
  EFFECT: [
    DAMAGE {
      YOU
      SOURCE: SELF
      AMOUNT: 1
    }
  ]
}

# Add {G} for each creature you control (Gaea's Cradle pattern)
MANA_ABILITY {
  COST: TAP
  ADD_MANA: { "{G}" COUNT: COUNT(BATTLEFIELD FILTER { TYPE: "Creature" CONTROLLER: YOU }) }
}

# Any color in your commander's color identity
MANA_ABILITY {
  COST: TAP
  ADD_MANA: { COLORS: YOU.commander_identity }
}

# Add three mana all of one chosen color (MODE: UNIFORM)
MANA_ABILITY {
  COST: TAP
  ADD_MANA: { ["{W}", "{U}", "{B}", "{R}", "{G}"] COUNT: 3 MODE: UNIFORM }
}

# Add X mana in any combination of {U} and/or {R} (Vivi Ornitier pattern)
MANA_ABILITY {
  COST: "{0}"
  DURING: YOUR_TURN
  DEPLETE: CUMULATIVE
  LIMIT: 1
  REPLENISH: EACH_TURN
  ADD_MANA: { ["{U}", "{R}"] COUNT: SELF.power }
}
```

## 7.6. Loyalty Ability Blocks

```
LOYALTY_ABILITY {
  COST: <+N | 0 | -N>
  EFFECT: [ <effect block> ... ]
}
```

`LOYALTY_ABILITY` blocks encode planeswalker loyalty abilities. `COST` is a loyalty delta — positive values add loyalty counters, `0` leaves loyalty unchanged, negative values remove them. The engine enforces the “activate only on your turn, only once per turn” rule implicitly — it does not need to be expressed in CDL. Multiple `LOYALTY_ABILITY` blocks appear in oracle text order on the card.

```
# +1 ability
LOYALTY_ABILITY {
  COST: +1
  EFFECT: [
    CREATE_TOKEN {
      COLOR: W
      TYPES: ["Creature"]
      SUBTYPES: ["Soldier"]
      POWER: 1
      TOUGHNESS: 1
    }
  ]
}

# 0 ability
LOYALTY_ABILITY {
  COST: 0
  EFFECT: [
    ADD_COUNTER {
      FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
      NAME: "+1/+1"
    }
  ]
}

# -3 ability
LOYALTY_ABILITY {
  COST: -3
  EFFECT: [
    DESTROY {
      TARGET {
        TARGET_CLASSES: [PERMANENT]
        FILTER { TYPE: "Creature" CONTROLLER: OPPONENT }
      } AS $target
    }
  ]
}
```

-----

## 8. Targeting — TARGET

`TARGET` is used for on-stack targeting subject to full targeting rules (legality checked on cast, rechecked on resolution).

```
TARGET {
  TARGET_CLASSES?: [<target class>, ...]
  ZONE?: <zone>       # restrict targeting to objects in this zone; use ALL.graveyard for any player's graveyard
  FILTER?: <FILTER { <filter criteria> }>
  SELECTION?: <N | RANGE N..M | ALL | $<variable> | <scalar expression>>
              # how many targets; default 1; RANGE 0..N = optional targeting
              # engine infers legality from minimum: RANGE 1..N requires at least one valid target
} AS $<n>
```

`AS $name` binds a reference variable evaluated at resolution time. Use `AS !name` instead to capture an immutable snapshot at cast time — the binding is permanently fixed when the spell is cast regardless of what changes before resolution. This is important when downstream effects reference properties of the target that may change (e.g. controller) between cast and resolution:

```
# Snapshot the target's controller at cast time
TARGET {
  TARGET_CLASSES: [PERMANENT]
  FILTER { CONTROLLER: OPPONENT }
} AS !target
# !target.controller is the controller at cast time, even if control changes
```

**Target classes** define what category of game entity may be targeted. When omitted, the engine infers from context (e.g. a `FILTER { TYPE: "Creature" }` with no `TARGET_CLASSES` implies `PERMANENT`).

```
TARGET_CLASSES: [PLAYER]             # a player
TARGET_CLASSES: [PLANESWALKER]       # a planeswalker permanent
TARGET_CLASSES: [PERMANENT]          # any battlefield permanent
TARGET_CLASSES: [SPELL]              # a spell on the stack
TARGET_CLASSES: [ABILITY]            # an ability on the stack
TARGET_CLASSES: [CARD]               # a card in a zone (graveyard, hand, library)
TARGET_CLASSES: [PLAYER, PLANESWALKER]  # player or planeswalker
TARGET_CLASSES: [GRAVEYARD]          # a graveyard zone; exile means all contents
TARGET_CLASSES: [HAND]               # a hand zone
TARGET_CLASSES: [LIBRARY]            # a library zone
TARGET_CLASSES: [BATTLEFIELD]        # the battlefield zone
TARGET_CLASSES: [STACK]              # the stack
TARGET_CLASSES: [EXILE_ZONE]         # the exile zone (distinct from the EXILE effect)
TARGET_CLASSES: [CREATURE]           # a creature permanent — used with [PLAYER, PLANESWALKER] for "any target"
TARGET_CLASSES: [NONBASIC_LAND]      # a nonbasic land permanent
TARGET_CLASSES: [BASIC_LAND]         # a basic land permanent
```

When `TARGET_CLASSES` includes a zone class (e.g. `GRAVEYARD`), the effect operates on the zone itself.

`TARGET_CLASSES` and `FILTER` are complementary — `TARGET_CLASSES` defines what kind of entity, `FILTER` constrains within that class. `ZONE` restricts targeting to objects in a specific zone, used with `TARGET_CLASSES: [CARD]` for graveyard and library targets. The `AS $name` binding is optional but required if the target is referenced downstream. If a target becomes illegal before resolution, the spell or ability is countered by the rules unless it has other legal targets.

`SELECTION` controls the target count. Default is exactly one target (`SELECTION: 1`). Use `SELECTION: RANGE 0..N` for “up to N targets” — the spell may be cast with zero targets. The engine infers legality from the minimum: `SELECTION: RANGE 1..N` requires at least one valid target.

```
# Target player or planeswalker
TARGET {
  TARGET_CLASSES: [PLAYER, PLANESWALKER]
} AS $target

# Target noncreature spell on the stack
TARGET {
  TARGET_CLASSES: [SPELL]
  FILTER { NOT { TYPE: "Creature" } }
} AS $target

# Target card in any graveyard
TARGET {
  TARGET_CLASSES: [CARD]
  ZONE: ALL.graveyard
} AS $target

# Target creature an opponent controls
TARGET {
  TARGET_CLASSES: [PERMANENT]
  FILTER { TYPE: "Creature" CONTROLLER: OPPONENT }
} AS $target
```

```
# Example -- bind target for downstream reference
EXILE {
  TARGET {
    FILTER { TYPE: "Creature" }
  } AS $exiled
}
GAIN_LIFE {
  PLAYER: $exiled.controller
  AMOUNT: $exiled.power
}

# Example -- target any permanent
DESTROY {
  TARGET {
    TARGET_CLASSES: [PERMANENT]
  } AS $destroyed
}
```

-----

## 9. Resolution-Time Selection — CHOOSE

`CHOOSE` is used when a subject is selected at resolution time, not on the stack. Not subject to targeting rules; legality is evaluated at the moment of selection.

```
CHOOSE AS $<n> {
  FROM?: <collection>              # source collection — omit when OPTIONS is specified
  OPTIONS?: <CARD_NAME | CREATURE_TYPE | COLOR | ...>
                                   # open-ended value selection from constrained vocabulary
  EXCLUDE?: [<value>, ...]         # values to exclude from OPTIONS
  COUNT: <N | RANGE N..M | ALL>
  FILTER?: <FILTER { <filter criteria> }>
}
```

`AS $name` is optional but required if the selection is referenced downstream. `COUNT: RANGE N..M` expresses a variable quantity — use `RANGE 0..N` for “up to N”, `RANGE 1..N` for “one to N”, etc. A bare `N` means exactly N. `OPTIONS` replaces `FROM` for open-ended value selection — the player inputs any valid value of the specified category rather than selecting from game objects. `TARGET_CLASSES` inside the `FILTER` block specifies what types of objects are eligible — the engine resolves appropriate zones automatically (e.g. `TARGET_CLASSES: [PERMANENT, PLAYER]` selects from battlefield permanents and all players without requiring an explicit zone union).

```
# Example — up to two lands
UNTAP {
  CHOOSE {
    FROM: BATTLEFIELD
    COUNT: RANGE 0..2
    FILTER {
      TYPE: "Land"
      CONTROLLER: YOU
    }
  }
}
```

-----

## 10. Modal Branching — CHOICE

`CHOICE` represents a decision point between discrete modes. Default timing is cast time (mode declared on the stack). Override with `TIMING: RESOLUTION` for cards that select mode at resolution.

```
CHOICE {
  TIMING?: <CAST | RESOLUTION>      # default: CAST
  PLAYER: <player descriptor>
  COUNT?: <N | RANGE N..M | ALL>    # options to choose; default: 1
  DEPLETE?: <BOOLEAN>               # if TRUE, chosen options are consumed
  REPLENISH?: <EACH_TURN | NEVER>   # when depleted options reset
  OPTION { COST?: <cost expression>  EFFECT?: [ <effect block> ... ] }
  OPTION { COST?: <cost expression>  EFFECT?: [ <effect block> ... ] }
  ...
}
```

`OPTION` blocks appear directly inside `CHOICE` with no enclosing wrapper list. `COUNT` defaults to 1; use `COUNT: 2` for “choose two”, `COUNT: RANGE 1..4` for “one or more.” `DEPLETE: TRUE` marks options as consumed when chosen; `REPLENISH` controls when they reset. `REPLENISH: EACH_TURN` and `REPLENISH: NEVER` are the valid values — these are `REPLENISH`-only values and are not valid in `DURATION` contexts. An `OPTION` block may contain a bare literal value (mana symbol string, integer) bound with `AS` instead of an `EFFECT` list — the choice itself is the outcome. The same variable name should appear across all sibling options; whichever branch is taken sets the binding. Use `DECLARE:` at the card level when the binding is referenced by other ability blocks:

```
# Store chosen color card-scoped — Thriving Isle pattern
DECLARE: $chosen_color

REPLACE {
  EVENT: AS_ETB { FILTER { SELF } } AS $event
  WITH: [
    CHOICE {
      PLAYER: YOU
      COUNT: 1
      OPTION { "{W}" AS $chosen_color }
      OPTION { "{B}" AS $chosen_color }
      OPTION { "{R}" AS $chosen_color }
      OPTION { "{G}" AS $chosen_color }
    }
  ]
}
```

When `CHOICE` appears inside a `TRIGGERED` ability’s `EFFECT` list, `TIMING` defaults to `RESOLUTION` — cast-time selection only applies within `SPELL` or `ACTIVATED` blocks.

```
CHOICE {
  PLAYER: YOU
  COUNT: 2
  OPTION { EFFECT: [ DESTROY { FILTER { TYPE: "Artifact" SELECTION: ALL } } ] }
  OPTION { EFFECT: [ DESTROY { FILTER { TYPE: "Enchantment" SELECTION: ALL } } ] }
  OPTION { EFFECT: [ DESTROY { FILTER { TYPE: "Creature" MV: LTE 3 SELECTION: ALL } } ] }
  OPTION { EFFECT: [ DESTROY { FILTER { TYPE: "Creature" MV: GTE 4 SELECTION: ALL } } ] }
}

CHOICE {
  PLAYER: YOU
  COUNT: 1
  DEPLETE: TRUE
  REPLENISH: EACH_TURN
  OPTION { EFFECT: [ DRAW { PLAYER: YOU COUNT: 1 } ] }
  OPTION { EFFECT: [ CREATE_TOKEN { TOKEN: @Treasure PLAYER: YOU } ] }
  OPTION { EFFECT: [ LOSE_LIFE { PLAYER: OPPONENTS AMOUNT: 3 } ] }
}
```

**Optional cost pattern** — “you may pay {N}. If you do, [effect]” is expressed as a `CHOICE` with `COUNT: RANGE 0..1` and a single costed `OPTION`. Choosing zero options is the “decline” case — no empty `OPTION` needed.

```
# You may pay {4}. If you do, untap this artifact.
CHOICE {
  TIMING: RESOLUTION
  PLAYER: YOU
  COUNT: RANGE 0..1
  OPTION {
    COST: MANA: "{4}"
    EFFECT: [ UNTAP { SELF } ]
  }
}
```

-----

## 11. Variable Binding — AS

Variables are bound inline using the `AS` keyword. This replaces the former `DECLARE` block. Any `TARGET`, `CHOOSE`, or scalar expression can be bound to a `$name` variable and referenced later in the same ability.

```
# Bind a target
TARGET { FILTER { TYPE: "Creature" } } AS $target

# Bind a resolution-time selection
CHOOSE AS $chosen {
  FROM: YOU.graveyard
  COUNT: 1
  FILTER { TYPE: "Creature" }
}

# Bind a computed scalar value inline
DAMAGE {
  TARGET: OPPONENTS
  AMOUNT: COUNT(BATTLEFIELD FILTER { TYPE: "Creature" CONTROLLER: YOU }) AS $count
}
```

**Mana cost variables:** `$X`, `$Y`, `$Z` match the variable names on the card. When `{X}`, `{Y}`, or `{Z}` appear in the mana cost, they are auto-bound at cast time from the mana paid and available throughout the ability. When no such symbol appears in the mana cost, `$X`/`$Y`/`$Z` must be explicitly bound using `AS` or an inline definition — they are not reserved for mana use only.

**Property access:** Bound object variables expose properties via dot notation:

```
$var.power        # power value
$var.toughness    # toughness value
$var.mv           # mana value
$var.controller   # controlling player
$var.owner        # owning player
$var.name         # card name
```

-----

## 12. Collections

Collections are references to sets of game objects. They are used as inputs to `CHOOSE`, `FILTER`, `COUNT`, and distribution effects.

```
BATTLEFIELD
STACK
EXILE
COMMAND_ZONE
```

**Player-scoped zones** use dot notation on a player descriptor:

```
YOU.library
YOU.hand
YOU.graveyard
OPPONENT.library
OPPONENT.hand
OPPONENT.graveyard
ALL.library          # all players' libraries
ALL.hand             # all players' hands
ALL.graveyard        # all players' graveyards
OPPONENTS.hand
OPPONENTS.graveyard
YOU.opponents          # collection of all players who are your opponents
$player.opponents      # collection of opponents of the specified player
```

```
TOP(<N>, YOU.library)   # top N cards of a player's library
```

**Player collections:**

```
PLAYERS              # collection of all players
OPPONENTS            # collection of all opponents
```

**Special zone values:**

```
OUTSIDE_GAME         # outside the game
```

**Collection operators:**

```
$collection EXCEPT $object    # all members of collection minus object
```

-----

## 13. Scalar Functions and Property Access

```
COUNT(<collection>)
```

Returns the number of objects in a collection (after any filters applied). May be bound inline with `AS`:

```
COUNT(BATTLEFIELD FILTER { TYPE: "Creature" CONTROLLER: YOU }) AS $creatures
```

```
SHARES_ANY(<collection_a>, <collection_b>)
```

Returns true if the two collections share at least one element. Used for subtype, color, or type overlap checks.

**Property access** uses dot notation. Dot notation on a **collection** maps over members returning a list; on a **leaf property** returns the value directly.

```
$var.power        # power value — integer (leaf)
$var.toughness    # toughness value — integer (leaf)
$var.mv           # mana value — integer (leaf)
$var.controller   # controlling player
$var.owner        # owning player
$var.name         # card name — string (leaf)
$var.colors       # set of colors this permanent produces or has
<collection>.colors  # union of colors across all members of the collection
$var.creatures    # collection of creatures controlled by player
$var.creatures.power  # list of power values mapped over collection
$var.zone         # the zone this card/permanent currently occupies — one of the
                  # Section 12 zone constants (BATTLEFIELD, COMMAND_ZONE, etc.) (leaf)
```

**Counter property access:**

```
SELF.counters.depletion    # integer — count of depletion counters (leaf)
SELF.counters."+1/+1"      # integer — quoted name for special characters (leaf)
SELF.counters.*            # collection — all counter instances
COUNT(SELF.counters.*)     # integer — total counter instances
COUNT(SELF.counters)       # integer — number of distinct counter types
```

**Truthiness:** Non-zero integer values are truthy in boolean filter and condition contexts. Counter presence checks use dot notation directly — no special `HAS_COUNTER` field needed:

```
FILTER { counters.depletion }        # matches if depletion counter count > 0
FILTER { counters."+1/+1" GTE 2 }   # matches if two or more +1/+1 counters present
```

**Scalar functions:**

```
COUNT(<collection>)                           # number of objects in collection
MAX(<collection>.<property>)                  # maximum value of property across collection
MIN(<collection>.<property>)                  # minimum value of property across collection
SHARES_ANY(<collection_a>, <collection_b>)    # true if collections share an element
"<mana symbol>" * COUNT(<collection>)         # scales a mana expression by an integer count
FLOOR(<expression>)                           # rounds down to nearest integer
CEIL(<expression>)                            # rounds up to nearest integer
SUM(<collection>)                             # sum of all values in a collection
```

The `*` operator multiplies a mana symbol by a count, producing a scaled mana expression. Valid in `COST_REDUCTION`, `ADD_MANA`, and `COST`/`REDUCTION` (mana-amount) contexts. Example: `"{G}" * COUNT(BATTLEFIELD FILTER { TYPE: "Creature" CONTROLLER: YOU })` produces one `{G}` per creature you control. In a `COST` context (e.g. `MANA: "{1}" * SELF.counters.age`), this scales the amount actually owed rather than reducing or producing mana — used for effects like Cumulative upkeep, where the paid cost is the printed cost once per counter already on the permanent.

```
MAX($opponent.creatures.power)        # greatest power among opponent's creatures
MIN(YOU.graveyard.mv)                 # lowest mv among cards in your graveyard
```

**Mana expression arithmetic:**

Mana expressions support `+` and `-` operators when used as computed values. A mana expression is treated as a polynomial over mana symbols — e.g. `"{2}{W}{B}"` = 2 generic + 1W + 1B. Arithmetic produces a new mana expression string. The engine handles reduction ordering (generic reduced before colored) and floors the result at `"{0}"` per rule 601.2b — CDL expresses intent only.

```
$card.mana_cost              # the card's mana cost as a mana expression string
$card.mana_cost - "{4}"      # reduce by 4 generic; floor at "{0}" handled by engine
$card.mana_cost + "{2}"      # add 2 generic
$card.mana_cost - "{1}{G}"   # reduce by 1 generic and 1 green
```

`mana_cost` is a valid property accessor on any card or spell object. It is also valid as a bare property reference inside `GRANT` bodies, where the implicit subject convention applies — `mana_cost` refers to the mana cost of the object currently being granted to.

-----

### 13.1 Power/Toughness Model

Power and toughness in CDL are represented at three levels, reflecting the MTG layer system for stat calculation:

- **`power` / `toughness`** — effective (current) value accounting for all effects. This is what MTG oracle text means by “power” unqualified — e.g. “creatures with power 2 or less.” Used in filter comparisons, condition expressions, and property access by default.
- **`base_power` / `base_toughness`** — layer 7b value only: the printed stat, the result of a CDA formula (`STAT { }` block), or a substitutive setter (`STATS { }`). Used when oracle text says “base power” explicitly. `X/X` tokens have a fixed base stat set at creation. `*/*` creatures have a base stat defined by a `STAT { }` formula.
- **`mod_power` / `mod_toughness`** — net continuous modifications from layer 7c+: grants, auras, equipment, anthem effects, “gets +N/+N” effects. Written by `MODIFY` and `GRANT { POWER: +N }`. Readable as a property accessor when needed.
- **Counters** — self-accounting via `counters."+1/+1"` and `counters."-1/-1"`. Not a CDL-written mod field — the engine applies counter effects per Appendix C definitions.

Effective power = `base_power + mod_power + counter adjustments`. The engine handles this composition — CDL expresses intent at each level independently.

All three are valid property accessors in any context accepting a property — filters, conditions, scalar expressions, dot notation chains:

```
$var.power           # effective power (default — matches MTG oracle text usage)
$var.base_power      # base only (layer 7b) — use when card says "base power"
$var.mod_power       # net continuous modifications (layer 7c+)
$var.toughness       # effective toughness
$var.base_toughness  # base only
$var.mod_toughness   # net continuous modifications
```

```
# Filter creatures with effective power >= 3 (standard usage)
FILTER { TYPE: "Creature" POWER: GTE 3 }

# Filter creatures with base power 1 (Zinnia pattern)
FILTER { TYPE: "Creature" BASE_POWER: EQ 1 NOT { SELF } }

# Filter creatures that have received a continuous buff
FILTER { TYPE: "Creature" MOD_POWER: GTE 1 }
```

CDL constructs and the levels they write:

- `STAT { }` (Section 15.25) — defines `base_power`/`base_toughness` for `*` creatures
- `STATS { }` (Section 15.41) — substitutively sets `base_power`/`base_toughness`
- `MODIFY` (Section 15.15) — writes `mod_power`/`mod_toughness` via signed delta
- `GRANT { POWER: +N }` (Section 15.26) — writes `mod_power`/`mod_toughness` continuously

**Boolean permanent properties:**

```
$var.tapped      # true if the permanent is currently tapped
$var.attacking   # true if the permanent is currently attacking
$var.blocking    # true if the permanent is currently blocking
```

Boolean properties are valid as filter criteria (`TAPPED: TRUE`) and as condition expressions in a `WHEN` or `CONDITION` clause.

**Player-scoped property accessors:**

```
$player.spells_this_turn        # spells cast by this player this turn
$player.cards_drawn_this_turn   # cards drawn by this player this turn
$player.life                    # current life total
$player.hand_size               # number of cards in hand
$player.commander_identity      # list of colors in this player's commander's color identity; stable throughout the game
$player.commanders              # collection of permanents on the battlefield designated as this player's commanders
$player.is_monarch              # boolean — true if this player currently holds the monarch designation
$player.life_lost_this_turn     # life lost by this player this turn (integer, always >= 0)
$player.life_gained_this_turn   # life gained by this player this turn
$player.deck                    # full deckbuilding card pool
$player.companions              # cards declared as companions pre-game
```

`PLAYERS` and `OPPONENTS` are player collections for property mapping.

```
ACTIVATED {
  ZONE: OUTSIDE_GAME
  DURING: SORCERY_SPEED
  CONDITION: SELF IN YOU.companions
  COST: MANA: "{3}"
  EFFECT: [ MOVE { SELF TO: YOU.hand } ]
}
```

`is_monarch` is a boolean property on any player. Use `COUNT(ALL FILTER { is_monarch: TRUE })` to check global monarch state:

```
# No player is the monarch (Crown of Gondor pattern)
CONDITION: COUNT(ALL FILTER { is_monarch: TRUE }) EQ 0

# A player is the monarch
CONDITION: COUNT(ALL FILTER { is_monarch: TRUE }) GTE 1

# You are the monarch
CONDITION: YOU.is_monarch
```

`commanders` is always a collection — contains one entry for standard commanders, two for partner commanders, zero if no commander is on the battlefield. Use `COUNT(YOU.commanders) GTE 1` to check if you control a commander.

```
# Check if you control a commander
CONDITION: COUNT(YOU.commanders) GTE 1

# Reference your commander permanents
FILTER { SELF IN YOU.commanders }
```

Property accessors are valid in cost expressions (e.g. `COST: MANA: SELF.power`).

**RANGE** expresses a variable count for `CHOOSE` and similar constructs:

```
RANGE N..M    # between N and M inclusive
RANGE 0..2    # "up to two"
RANGE 1..3    # "one to three"
```

**STAR — variable stat marker:**

`STAR` appears in card header `POWER`/`TOUGHNESS` fields to indicate the stat is defined by a `STAT` block rather than a fixed integer. Offset notation applies the delta after the formula resolves:

```
POWER: STAR        # power equals the STAT block formula
TOUGHNESS: STAR+1  # toughness equals the formula plus 1
TOUGHNESS: STAR-1  # toughness equals the formula minus 1
```

The engine evaluates the `STAT` block formula continuously whenever the stat value is needed.

-----

## 14. FILTER Blocks

`FILTER` blocks are the sole mechanism for constraining collections or selections in CDL. Inline filter expressions are not permitted. A `FILTER` block may be defined inline at point of use or declared as a named block and referenced elsewhere on the card.

### 14.1 Filter Criteria

The following criteria are valid inside a `FILTER` block:

```
TYPE: "<type>" | ["<type>", ...]     # matches anywhere on the full type line — supertypes, types, and subtypes
                                     # e.g. "Creature", "Legendary", "Zombie", ["Instant", "Sorcery"], "Swamp"
CATEGORY: <PERMANENT | NONPERMANENT | SPELL | TOKEN | NONTOKEN | NONLAND | NONBASIC>   # NONLAND always implies permanent context
COLOR: <W | U | B | R | G | COLORLESS | MULTICOLOR>
CONTROLLER: <player descriptor>
OWNER: <player descriptor>
PLAYER: <player descriptor>          # scoped to event block filters — matches the player associated with the event
HAS_KEYWORD: @<keyword>
TAPPED: <TRUE | FALSE>               # e.g. TAPPED: TRUE matches tapped permanents
ATTACKING: <TRUE | FALSE>            # e.g. ATTACKING: TRUE matches attacking creatures
BLOCKING: <TRUE | FALSE>             # e.g. BLOCKING: TRUE matches blocking creatures
ENCHANTED_BY: <FILTER { ... }>       # matches permanents with an attached Aura satisfying the given filter
                                     # e.g. ENCHANTED_BY: { CONTROLLER: YOU } — "enchanted by an Aura you control"
COMBAT: <TRUE | FALSE>               # e.g. COMBAT: TRUE — matches combat damage instances (damage_dealt_this_turn)
SOURCE: <FILTER { ... }>            # matches damage instances whose source satisfies the given filter
                                     # e.g. SOURCE: { OR { TYPE: "Assassin" SELF IN YOU.commanders } }
NOT { <filter criteria> }           # e.g. NOT { CATEGORY: TOKEN }
AND { <filter criteria> ... }
OR { <filter criteria> ... }
CONDITION: <scalar expression> <comparison>  # e.g. CONDITION: COUNT($p.spells_this_turn) EQ 1
<property>: <comparison | scalar>   # any dot-notation property is a valid filter field
SELECTION: <N | RANGE N..M | ALL | $<variable> | <scalar expression>>
                                     # how many objects to select; default 1; RANGE 0..N for optional selection
```

**Type matching:** `TYPE` matches against the full type line without distinguishing supertypes, types, and subtypes — consistent with how MTG rules handle type matching. A card with type line “Legendary Snow Creature — Elf Druid” matches `TYPE: "Legendary"`, `TYPE: "Creature"`, `TYPE: "Elf"`, and `TYPE: "Druid"` equally. `SUBTYPE` and `SUPERTYPE` are not valid filter fields — use `TYPE` for all type line matching.

**Default zone:** `FILTER` without an explicit `CATEGORY`, `ZONE`, or zone-scoped collection defaults to battlefield permanents. Use `CATEGORY: PERMANENT` explicitly when clarity is important. Use zone dot-notation (`YOU.graveyard`, `ALL.hand` etc.) or `ZONE:` on the enclosing block to scope to other zones.

**Implicit subject:** Bare property access without a subject inside a `FILTER` block (e.g. `counters.rope`, `tapped`) refers to the object currently being evaluated — the same object `TAPPED: TRUE` would match. Enables concise counter-presence checks: `NOT { counters.rope }` matches objects with no rope counters.

Any property accessible via dot notation is valid as a filter criterion. Numeric properties accept comparison expressions (`GTE`, `LTE`, `EQ` etc.) or scalar functions (`MAX()`, `MIN()`) on the right-hand side. A property accessor (`SELF.<property>`) is also valid on the right-hand side — the engine evaluates it per object being filtered. `POWER`, `TOUGHNESS`, `MV`, `LOYALTY` etc. are property comparisons, not special-cased fields. `MV` is the mana value (formerly CMC) of the object.

```
# Creatures whose effective power exceeds their base power (Kutzil pattern)
FILTER { TYPE: "Creature" POWER: GT SELF.base_power }
```

```
FILTER { TYPE: "Creature" POWER: GTE 3 }
FILTER { TYPE: "Creature" POWER: MAX($opponent.creatures.power) }
FILTER { TYPE: ["Instant", "Sorcery"] CONTROLLER: YOU }
FILTER { TYPE: "Legendary" NOT { CATEGORY: TOKEN } }
FILTER { TYPE: "Swamp" }
```

**Matching semantics:** Multiple fields inside a `FILTER` block are combined with implicit AND — all criteria must match. When a field value is a list, it uses IN semantics — the object matches if it satisfies any value in the list. `NOT { }` takes filter criteria directly and negates them. `CONDITION` allows computed scalar checks.

```
FILTER { TYPE: ["Instant", "Sorcery"] CONTROLLER: YOU }
FILTER { TYPE: "Creature" NOT { CATEGORY: TOKEN } }
FILTER { CONTROLLER: OPPONENT CONDITION: COUNT($caster.spells_this_turn) EQ 1 }
```

**`TYPE` list vs. `AND { }` — do not conflate these.** `TYPE: ["Basic", "Land"]` is an *OR*: it matches any object whose type line contains "Basic" **or** "Land", which is wrong for "a basic land card" (it would also match every nonbasic land). When a single object must satisfy multiple type-line words at once — "basic land", "artifact creature", "legendary creature" — use `AND { }` with one `TYPE` criterion per required word instead:

```
# "search your library for a basic land card"
FILTER { AND { TYPE: "Basic" TYPE: "Land" } }

# "artifact creatures you control have menace"
FILTER { AND { TYPE: "Artifact" TYPE: "Creature" } CONTROLLER: YOU SELECTION: ALL }
```

Use the list form only when the oracle text itself says "or" between distinct alternatives ("instant or sorcery", "artifact or enchantment").

### 14.2 Comparison Expressions

```
EQ <N>     # equal to
NEQ <N>    # not equal to
LT <N>     # less than
LTE <N>    # less than or equal to
GT <N>     # greater than
GTE <N>    # greater than or equal to
```

### 14.3 Usage in Targeting and Selection

```
# Named filter defined at card level — referenced via inline FILTER use
# (For reusable filter patterns, prefer @macro definitions in Appendix A)
CHOOSE {
  FROM: BATTLEFIELD
  COUNT: 1
  FILTER {
    TYPE: "Artifact"
    CONTROLLER: YOU
  }
}
```

-----

## 15. Effect Vocabulary

Any effect block may include an optional `DURATION?:` field. When omitted, the effect is permanent or lasts for as long as its source is in play. The engine determines appropriate duration semantics per effect type. This means effects like `ADDITIONAL_LAND_PLAY`, `LAND_PLAY_FROM`, and any other primitive may carry a `DURATION` without requiring it to be individually documented on each schema.

`COUNT` accepts either a fixed integer `<N>` or `RANGE N..M` in all effect blocks. When a range is specified, the controlling player chooses the exact count within that range at resolution time. `RANGE 0..N` implies optionality — choosing 0 is valid.

### 15.0 PREVENT

`PREVENT` is a damage prevention primitive distinct from `REPLACE`. Per rule 615.12, prevention effects still apply to unpreventable damage but produce no preventive result.

```
PREVENT {
  SOURCE?: <object reference | FILTER { ... }>    # damage source being prevented
  TARGET?: <object reference | player descriptor> # damage recipient being protected
  AMOUNT?: <N | ALL>                              # how much to prevent; default: ALL
  DURATION?: <duration expression>
}
```

```
# Prevent all damage from a specific source
PREVENT { SOURCE: $attacker TARGET: YOU AMOUNT: ALL }

# Prevent the next 3 damage to target
PREVENT { TARGET: $target AMOUNT: 3 }

# Fog pattern
PREVENT { AMOUNT: ALL DURATION: END_OF_COMBAT }
```

### 15.1 DRAW

```
DRAW {
  PLAYER: <player descriptor>
  COUNT: <N>
}
```

### 15.2 DISCARD

```
DISCARD {
  PLAYER: <player descriptor>
  COUNT: <N | ALL>
  MODE?: <RANDOM | CHOOSE>   # default: CHOOSE
}
```

### 15.3 DESTROY

```
DESTROY {
  TARGET?: <target block>     # for on-stack targeted destroy
  <object reference>?         # bare subject — for non-targeted destroy (e.g. bound variable)
  FILTER?: <FILTER { ... }>   # for mass destroy effects — use SELECTION inside FILTER
  EXCEPT?: <object reference> # exclude a specific object from the matched set
}
```

`TARGET`, `OBJECT`, and `FILTER` are mutually exclusive. Use `TARGET` for spells/abilities that target, `OBJECT` when operating on a previously bound variable, and `FILTER` for mass effects. Use `SELECTION` inside `FILTER` to control how many objects are destroyed. `DESTROY` is always battlefield-scoped and has no `FROM` field. To prevent regeneration, precede `DESTROY` with a `CANT_REGENERATE` effect block (see Section 15.28).

### 15.4 EXILE

```
EXILE {
  TARGET?: <target block>          # for on-stack targeted exile
  <object reference>?              # bare subject — for non-targeted exile (e.g. SELF, bound variable)
  FILTER?: <FILTER { ... }>        # for mass exile effects
  EXCEPT?: <object reference>      # exclude a specific object from the matched set
  FROM?: <zone>                    # zone to exile from; default: BATTLEFIELD
}
```

`TARGET` and `OBJECT` are mutually exclusive. Use `TARGET` when exile requires on-stack targeting rules; use `OBJECT` for non-targeted exile (e.g. `SELF` or a bound `$variable`). For mass exile, use `FILTER` with `COUNT`. `FROM` specifies the source zone and should be stated explicitly — default is `BATTLEFIELD`.

### 15.5 CREATE_TOKEN

```
CREATE_TOKEN {
  TOKEN?: @<token name>           # reference a common token from Appendix B
  PLAYER?: <player descriptor>    # who creates the token; default: YOU
  NAME?: "<token name>"
  SUPERTYPES?: ["<supertype>", ...]
  TYPES: ["<type>", ...]
  SUBTYPES?: ["<subtype>", ...]
  COLOR?: <W | U | B | R | G | COLORLESS | color combination>
  POWER?: <N>
  TOUGHNESS?: <N>
  COUNT?: <N>    # default: 1
  # keyword macros and ability blocks may follow, same as CARD body
}
```

When `TOKEN: @<name>` is specified, the engine resolves the full token definition from Appendix B. `PLAYER` and `COUNT` may still be specified at the call site and supplement the appendix definition. Use inline fields for unique tokens not in the appendix.

```
# Common token from appendix
CREATE_TOKEN { TOKEN: @Treasure PLAYER: YOU COUNT: 2 }

# Unique token defined inline
CREATE_TOKEN {
  COLOR: G
  TYPES: ["Creature"]
  SUBTYPES: ["Beast"]
  POWER: 3
  TOUGHNESS: 3
}
```

### 15.6 MOVE

```
MOVE {
  <object reference | collection variable>?           # bare subject — single object or bound collection
  FILTER?: <FILTER { ... }>              # for mass moves — use SELECTION inside FILTER, mirrors DESTROY/EXILE
  EXCEPT?: <object reference>            # exclude a specific object; unbound vars treated as no exclusion
  ZONE?: <zone>                          # source zone — optional, inferred from current zone if omitted
  TO: <zone>
  STATE?: <TAPPED | ATTACKING | ATTACKING(<defender>) | BLOCKING(<attacker>) | FACE_DOWN>
                                         # entry state when moving to battlefield
                                         # ATTACKING(<defender>) — enters attacking that specific player/planeswalker/battle
                                         # BLOCKING(<attacker>) — enters blocking that specific attacker
  LOCATION?: <TOP | TOP_ANY_ORDER | BOTTOM | BOTTOM_ANY_ORDER | RANDOM>
  CONDITION?: <condition expression>     # move only occurs if true
}
```

The bare subject and `FILTER` are mutually exclusive, same convention as `DESTROY`/`EXILE` (Sections 15.3–15.4) — use the bare subject for a single object or a previously bound collection, `FILTER` with `SELECTION` for a mass move not built from a prior `CHOOSE`/`SEARCH` step (e.g. "return all Zombie creature cards from your graveyard to the battlefield").

`STATE` applies when the destination is `BATTLEFIELD`. `ATTACKING` and `BLOCKING` accept an optional argument for the combat assignment — `STATE: ATTACKING($event.defending)` enters attacking that specific defender; `STATE: BLOCKING($attacker)` enters blocking that specific attacker. Omit the argument when the assignment is unspecified. `LOCATION` applies when the destination is a library or ordered zone. When `OBJECT` is a collection variable, `TOP_ANY_ORDER` and `BOTTOM_ANY_ORDER` place all objects in any order chosen by the player. `EXCEPT` excludes a specific object from the set.

```
# Put a card onto the battlefield tapped
MOVE {
  $target
  TO: BATTLEFIELD
  STATE: TAPPED
}

# Put a card on top of its owner's library
MOVE {
  $found
  TO: YOU.library
  LOCATION: TOP
}

# Enter battlefield attacking a specific defender (Kaalia pattern)
MOVE {
  $creature
  TO: BATTLEFIELD
  STATE: ATTACKING($event.defending)
}

# Mass move — return all Zombie creature cards from graveyard to
# battlefield tapped (Zombie Apocalypse)
MOVE {
  FILTER { AND { TYPE: "Zombie" TYPE: "Creature" } SELECTION: ALL }
  ZONE: YOU.graveyard
  TO: BATTLEFIELD
  STATE: TAPPED
}
```

### 15.7 COUNTER

```
COUNTER {
  TARGET {
    TARGET_CLASSES: [SPELL] | [ABILITY] | [SPELL, ABILITY]
  } AS $<n>
}
```

Counters the target spell or ability on the stack, moving it to its owner’s graveyard and preventing its effects from resolving. This is a distinct game state change from `MOVE` — it carries specific rules meaning including interaction with “can’t be countered” checks and replacement effects. Also used as the target of `CANT` to express uncounterable permanents.

### 15.8 DAMAGE

```
DAMAGE {
  <object reference | player descriptor>?    # bare subject — non-targeted damage recipient
  TARGET? { <target block> }                 # targeted damage — full MTG targeting rules apply
  SOURCE: <object reference>
  AMOUNT: <N>
  COMBAT?: <BOOLEAN>              # TRUE = this is combat damage; default FALSE
}
```

Bare subject and `TARGET? { }` are mutually exclusive — use bare subject for non-targeted damage (combat damage, “deals N damage to you”, triggered damage), use `TARGET? { }` for abilities that target per MTG rules. When no `AS` binding is needed on the target, omit it.

### 15.9 GAIN_LIFE

```
GAIN_LIFE {
  PLAYER: <player descriptor>
  AMOUNT: <N>
}
```

### 15.10 LOSE_LIFE

```
LOSE_LIFE {
  PLAYER: <player descriptor>
  AMOUNT: <N>
}
```

### 15.10a MAX_HAND_SIZE

```
MAX_HAND_SIZE {
  PLAYER: <player descriptor>
  VALUE: <N | NONE>   # NONE = no maximum hand size
}
```

Sets a player's maximum hand size, overriding the default of seven. Used inside `STATIC` for a continuous effect ("you have no maximum hand size," Thought Vessel) — omit `DURATION` for as long as the source is in play, same as any other `STATIC` effect.

```
STATIC {
  EFFECT: [ MAX_HAND_SIZE { PLAYER: YOU VALUE: NONE } ]
}
```

### 15.11 ADD_COUNTER

```
ADD_COUNTER {
  SELF | $<variable> | <player descriptor>                   # single subject — permanent or player
  | FILTER { <filter criteria> SELECTION: <N | ALL> }        # mass effect — SELECTION inside FILTER controls count
  NAME: "<counter name>"   # e.g. "+1/+1", "energy", "charge"
  COUNT?: <N>              # counters per object; default: 1
}
```

`ADD_COUNTER` applies to either a permanent or a player. Use `SELF`, a bound `$variable`, or a player descriptor for single subjects. Use `FILTER` with `SELECTION` inside for mass effects. `COUNT` unambiguously means counters per object — object selection quantity lives in `FILTER` via `SELECTION`. `NAME` is the counter’s name as defined in Appendix C.

```
# Add a +1/+1 counter to this creature
ADD_COUNTER { SELF NAME: "+1/+1" }

# Add two energy counters to you
ADD_COUNTER { YOU NAME: "energy" COUNT: 2 }

# Add a +1/+1 counter to each creature you control
ADD_COUNTER {
  FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
  NAME: "+1/+1"
}

# Add three +1/+1 counters to each Myr you control
ADD_COUNTER {
  FILTER { TYPE: "Myr" CONTROLLER: YOU SELECTION: ALL }
  NAME: "+1/+1"
  COUNT: 3
}
```

### 15.12 REMOVE_COUNTER

```
REMOVE_COUNTER {
  SELF | $<variable> | <player descriptor>                   # single subject — permanent or player
  | FILTER { <filter criteria> SELECTION: <N | ALL> }
  NAME: "<counter name>"
  COUNT?: <N>    # counters per object to remove; default: 1
}
```

Same subject rules as `ADD_COUNTER`. When used in a cost expression (`COST: REMOVE_COUNTER: { NAME: "..." }`), removal must succeed for the cost to be paid — the engine enforces this.

### 15.13 TAP

```
TAP {
  <subject>?            # SELF, $variable, CHOOSE, or omit with FILTER
  FILTER?: <FILTER { ... }>     # for mass tap effects — use SELECTION inside FILTER
  EXCEPT?: <object reference>   # exclude a specific object from the matched set
}
```

### 15.14 UNTAP

```
UNTAP {
  <subject>?            # SELF, $variable, CHOOSE, or omit with FILTER
  FILTER?: <FILTER { ... }>     # for mass untap effects — use SELECTION inside FILTER
  EXCEPT?: <object reference>   # exclude a specific object from the matched set
}
```

### 15.15 MODIFY

```
MODIFY {
  OBJECT?: <object reference>          # for single object
  FILTER?: <FILTER { ... }>            # for mass modify effects — use SELECTION inside FILTER
  EXCEPT?: <object reference>          # exclude a specific object from the matched set
  CONDITION?: <condition expression>   # modify only occurs if true
  POWER?: <+<scalar> | -<scalar>>      # delta only — use + or - prefix; scalar = N, $variable, COUNT(...), SUM(...)
  TOUGHNESS?: <+<scalar> | -<scalar>>
}
```

`MODIFY` expresses temporary stat changes only — `POWER` and `TOUGHNESS` fields always use a signed delta. The delta slot accepts any scalar expression: a fixed integer, a bound variable (`$X`), or a scalar function (`COUNT(...)`, `SUM(...)`). For continuous base stat definition use a `STAT` block (Section 15.25). For continuous grants use `GRANT` (Section 15.26).

### 15.16 SEARCH

```
SEARCH {
  PLAYER?: <player descriptor>         # who performs the search; default: YOU
  ZONE: <player-scoped zone>
  COUNT?: <N | RANGE N..M>             # default: 1
  FILTER?: <FILTER { ... }>
  ACTIVE_SELECTION AS $<n>?            # binds in-progress selection for use in CONSTRAIN
  CONSTRAIN?: <constraint expression>  # collection-level constraint evaluated during selection
  REVEAL?: <BOOLEAN>                   # if TRUE, reveal found cards to all players
  MAY?: <BOOLEAN>                      # if TRUE, the searching player may choose not to search
  SHUFFLE?: <BOOLEAN>                  # if TRUE, shuffle zone after search; default: TRUE
} AS $<n>?
```

`SEARCH` shuffles the zone after all downstream operations that reference its bound result have completed by default — `SHUFFLE: TRUE` is the implicit default and may be omitted. Set `SHUFFLE: FALSE` to suppress the shuffle for effects that explicitly do not shuffle (e.g. Imperial Seal, which places the card on top without shuffling). The engine holds references to found cards throughout — the shuffle reorders remaining cards only and never interferes with subsequent `MOVE` operations. Use `ACTIVE_SELECTION AS $name` with `CONSTRAIN` to express constraints across the selected set such as shared subtypes. When `PLAYER` differs from `YOU`, the specified player performs the search and makes all selections.

```
# Search for two basic lands that share a land type
SEARCH {
  ZONE: YOU.library
  COUNT: 2
  FILTER {
    TYPE: ["Basic", "Land"]
  }
  ACTIVE_SELECTION AS $selections
  CONSTRAIN: SHARES_ANY($selections.SUBTYPES)
} AS $found

# Opponent may search their library (Path to Exile pattern)
SEARCH {
  PLAYER: $exiled.controller
  ZONE: $exiled.controller.library
  FILTER {
    TYPE: ["Basic", "Land"]
  }
  MAY: TRUE
} AS $land
```

### 15.17 SHUFFLE

```
SHUFFLE {
  ZONE: <player-scoped library zone>
}
```

Used when a shuffle is needed independently of a `SEARCH` (which shuffles implicitly by default). Example: `SHUFFLE { ZONE: YOU.library }`

### 15.18 LOOK

```
LOOK {
  FROM: <zone>                    # source zone (any player-scoped or global zone)
  COUNT: <N | ALL>
  LOCATION?: <TOP | BOTTOM | RANDOM>  # default: TOP
  PLAYER: <player descriptor>     # who does the looking (private)
} AS $<n>?
```

`LOOK` is private — only `PLAYER` sees the cards. `AS` binding makes the viewed cards available for downstream operations.

### 15.18a REVEAL

```
REVEAL {
  FROM: <zone>
  COUNT: <N | ALL | UNTIL { <FILTER { ... }> AS $<n>? }>
  LOCATION?: <TOP | BOTTOM | RANDOM>  # default: TOP
  PLAYER: <player descriptor>     # who performs the reveal (all players see)
} AS $<n>?
```

`REVEAL` is public — all players see the cards. Same structure as `LOOK`. `REVEAL: TRUE` on `SEARCH` remains as a field shorthand for revealing found cards.

**`COUNT: UNTIL { }`** — reveals cards one at a time from `LOCATION` (default `TOP`) until one matches `FILTER`, then stops. Distinct from `SEARCH`: the zone is not shuffled, and only the revealed cards not kept move — per the "put the rest on the bottom in a random order" idiom, they go to the bottom in random order while the rest of the zone keeps its existing order. The block-level `AS` binds the entire revealed pile (in reveal order); the optional `AS` inside `UNTIL` binds the single card that matched and stopped the reveal — the only card in the pile guaranteed to satisfy `FILTER`.

```
# "Reveal cards from the top of your library until you reveal a creature
#  card. Put that card onto the battlefield and the rest on the bottom of
#  your library in a random order." (Atla Palani, Nest Tender)
REVEAL {
  FROM: YOU.library
  COUNT: UNTIL { FILTER { TYPE: "Creature" } AS $match }
} AS $revealed
MOVE { $match TO: BATTLEFIELD }
MOVE { $revealed EXCEPT: $match TO: YOU.library LOCATION: BOTTOM ORDER: RANDOM }
```

### 15.19 CHOOSE (effect)

See Section 9. `CHOOSE` as an effect context uses the same block structure.

### 15.20 CHOICE (effect)

See Section 10. `CHOICE` as an effect context uses the same block structure.

### 15.21 DISTRIBUTE

```
DISTRIBUTE {
  TOTAL: <N | $X>                              # total amount to distribute
  MODE: <EQUAL | CHOOSE | USES_TARGETS>        # how distribution is determined
  AMONG: <FILTER { ... } | TARGET { ... }>     # recipients
  EFFECT: <effect block>                       # e.g. DAMAGE { SOURCE: SELF }
}
```

`MODE` values: `EQUAL` — total divided equally; `CHOOSE` — player freely allocates at resolution; `USES_TARGETS` — targets declared at cast time, player allocates among them at resolution.

```
# Distribute 6 +1/+1 counters equally among creatures you control
DISTRIBUTE {
  TOTAL: 6
  MODE: EQUAL
  AMONG: FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
  EFFECT: ADD_COUNTER { NAME: "+1/+1" }
}

# Distribute X damage freely among any targets
DISTRIBUTE {
  TOTAL: $X
  MODE: CHOOSE
  AMONG: TARGET { TARGET_CLASSES: [CREATURE, PLAYER, PLANESWALKER] }
  EFFECT: DAMAGE { SOURCE: SELF }
}
```

### 15.22 VOTE

```
VOTE {
  OPTION { NAME?: "<label>"  WIN_EFFECT: [ <effect block> ... ] }
  OPTION { NAME?: "<label>"  WIN_EFFECT: [ <effect block> ... ] }
  ...
  CUMULATIVE?: <BOOLEAN>   # if true, win effects scale with vote count
}
```

`OPTION` blocks appear directly inside `VOTE`. `NAME` is an optional flavour label matching the name the card uses for that option — it is not a structural identifier. `WIN_EFFECT` lists the effects that fire if that option wins. `CUMULATIVE: TRUE` means the win effect fires once per vote cast for the winning option rather than once total.

### 15.24 ADD_MANA

`ADD_MANA` appears in two forms depending on context.

**As a field on `MANA_ABILITY`** (see Section 7.5) — uses the unified block form:

```
ADD_MANA: "{Any}"
ADD_MANA: { "<color symbol>" }
ADD_MANA: { ["<symbol>", ...] }
ADD_MANA: { COLORS: <property accessor> }
ADD_MANA: { "<symbol>" COUNT: <N | scalar> }
```

**As a block in `EFFECT` lists** — used when mana is produced as part of a spell or as a side effect of an activated ability. Takes `PLAYER` and `AMOUNT` fields:

```
ADD_MANA {
  PLAYER: <player descriptor>
  AMOUNT: "{Any}" | { <color spec> COUNT?: <N | scalar> }
}
```

`PLAYER` makes explicit who receives the mana — necessary for spells like Dark Ritual where the caster is the recipient, and for effects that produce mana for another player.

Note that an `ACTIVATED` block containing `ADD_MANA` in its effect list is **not** automatically a mana ability — whether it qualifies under rule 605.1a depends on whether the ability also has a target. If it targets, it is a regular activated ability that happens to add mana.

### 15.25 Static Effect Declarations

Used inside `STATIC` blocks. These are continuous effects with no stack interaction.

```
STAT {
  SELF | $<variable>          # the permanent whose stats are being defined
  POWER?: <scalar expression>
  TOUGHNESS?: <scalar expression>
}
```

`STAT` defines the continuous base power and/or toughness of a permanent whose card header uses `STAR`. The expression is evaluated by the engine continuously — any time the stat value is needed. Both fields are optional; omit the one not being defined.

```
# Power equals number of cards in all graveyards; toughness is that plus 1
STAT {
  SELF
  POWER: COUNT(ALL.graveyard)
  TOUGHNESS: COUNT(ALL.graveyard) + 1
}

# Power equals number of lands you control
STAT {
  SELF
  POWER: COUNT(BATTLEFIELD FILTER { TYPE: "Land" CONTROLLER: YOU })
}
```

```
DAMAGE_UNPREVENTABLE
```

A static effect primitive and grantable property. When present, damage dealt by the permanent cannot be prevented — per rule 615.12, `PREVENT` effects still apply but produce no preventive result. Used directly in `STATIC` for self-application, or inside `GRANT` to apply to other objects:

```
# Self — direct in STATIC
STATIC { EFFECT: [ DAMAGE_UNPREVENTABLE ] }

# Grant to others (Questing Beast pattern)
STATIC {
  EFFECT: [
    GRANT {
      FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
      DAMAGE_UNPREVENTABLE
    }
  ]
}
```

Blocking restrictions are expressed via `CANT { FILTER { <criteria> SELECTION: ALL } EFFECTS: [ BLOCK { SELF } ] }` (Section 18). For a creature that cannot be blocked at all, use `@UNBLOCKABLE` which expands to `CANT { ALL EFFECTS: [ BLOCK { SELF } ] }`. Counterable restrictions are expressed via `CANT { SELF EFFECTS: [COUNTER] }` in a `STATIC` block.

```
ADDITIONAL_LAND_PLAY {
  PLAYER: <player descriptor>
  COUNT?: <N>    # additional lands per turn beyond the normal limit; default: 1
}

LAND_PLAY_FROM {
  PLAYER: <player descriptor>
  ZONE: <zone>   # zone from which the player may play lands
}
```

`ADDITIONAL_LAND_PLAY` grants the specified player the ability to play additional lands beyond the normal one-per-turn limit. Multiple `ADDITIONAL_LAND_PLAY` effects stack. `LAND_PLAY_FROM` grants the ability to play lands from an alternate zone (e.g. the graveyard). Both are continuous static effects active for as long as the source permanent is on the battlefield.

```
# You may play an additional land each turn
STATIC {
  EFFECT: [
    ADDITIONAL_LAND_PLAY { PLAYER: YOU }
  ]
}

# You may play lands from your graveyard
STATIC {
  EFFECT: [
    LAND_PLAY_FROM { PLAYER: YOU ZONE: YOU.graveyard }
  ]
}
```

### 15.26 GRANT

`GRANT` applies properties to one or more objects. Used in `STATIC` blocks (continuous) and `EFFECT` lists (temporary with `DURATION`). Any card-level property, keyword macro, stat modification, effect field, or ability block may be granted directly inside the block — `GRANT` is open to any grantable content. No `PROPERTIES` wrapper needed. `TRIGGERED { }`, `ACTIVATED { }`, and `STATIC { }` ability blocks are valid grantable content and behave as if written directly on the recipient for the duration of the grant.

```
GRANT {
  TARGET?: <target block> AS $<n>         # for single targeted grants
  $<variable>                             # for grants to a previously bound object
  FILTER?: <FILTER { ... }>               # for grants affecting a category — use SELECTION inside FILTER
  ZONE?: <zone>                           # zone where grant applies; default: BATTLEFIELD
  EXCEPT?: <object reference>             # exclude a specific object from the matched set
  <any card-level property or keyword>    # e.g. @FLYING, COST_REDUCTION: "{1}", POWER: +1
  LOSE?: [<keyword>, ...] | ALL_ABILITIES # structural inverse of the bare-keyword additive grant
  DURATION?: <duration expression>        # omit for permanent/continuous grants
}
```

When `ZONE` is specified, the grant is a continuous static effect over objects in that zone. The engine tracks entry and exit — the grant applies while the object is in the zone and expires when it leaves. `ZONE: YOU.hand` is the canonical form for granting properties to cards in hand.

Stat fields use signed delta syntax — `POWER: +1` grants a continuous +1 power bonus. These stack with other continuous effects per the engine’s layer system. `COST_REDUCTION` granted inside a `STATIC` block with a scoping `FILTER` applies only to spells matching that filter. `TYPES`, `SUBTYPES`, and `SUPERTYPES` inside `GRANT` are **additive** — they append to the existing type line for the duration and expire cleanly. For substitutive characteristic changes use `STATS` (Section 15.41).

`LOSE` removes keywords rather than granting them — the structural inverse of the bare-keyword form directly inside `GRANT` (same block, opposite polarity, distinguished by field name rather than a separate primitive). `LOSE: [@FLYING]` strips a specific keyword; `LOSE: ALL_ABILITIES` strips every keyword and ability the object currently has (the "loses all abilities" vanilla-izing idiom). Unlike `NULLIFY` (Section 18a), which leaves a restriction's source keyword intact for anything checking `HAS_KEYWORD`, `LOSE` actually removes the keyword — `HAS_KEYWORD` checks against the object stop seeing it for as long as the grant applies.

```
# Equipped creature loses flying
GRANT { $equipped LOSE: [@FLYING] }

# Equipped creature loses all abilities
GRANT { $equipped LOSE: ALL_ABILITIES }
```

`DAMAGE_ASSIGNMENT: POWER | TOUGHNESS` — changes which characteristic the recipient uses to assign combat damage (default `POWER`). `TOUGHNESS` is the "assigns combat damage equal to its toughness rather than its power" idiom (Assault Formation, Arcades the Strategist, Felothar the Steadfast):

```
GRANT {
  FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
  DAMAGE_ASSIGNMENT: TOUGHNESS
}
```

```
STATIC {
  EFFECT: [
    GRANT {
      FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
      @HASTE
    }
  ]
}

GRANT {
  TARGET { TARGET_CLASSES: [PERMANENT] FILTER { TYPE: "Creature" } } AS $target
  @FLYING
  @LIFELINK
  DURATION: END_OF_TURN
}

# Grant cost reduction to green spells (The Earth Crystal pattern)
STATIC {
  FILTER { COLOR: G CATEGORY: SPELL }
  EFFECT: [
    GRANT { COST_REDUCTION: "{1}" }
  ]
}

# Grant Miracle to each enchantment in hand with cost = its mana cost minus {4} (Aminatou pattern)
STATIC {
  EFFECT: [
    GRANT {
      FILTER { TYPE: "Enchantment" ZONE: YOU.hand SELECTION: ALL }
      @MIRACLE(mana_cost - "{4}")
    }
  ]
}
```

**Implicit subject in `GRANT` body:** Bare property access inside the grant body (e.g. `mana_cost`, `power`) refers to the object currently being granted to — the same implicit subject convention as `FILTER` blocks (Section 14.1). The engine evaluates the property per object, applying the grant individually to each matched object. Use `EACH AS $x` only when the bound variable is needed for downstream reference outside this implicit context.

**`WHEN_<action>` modifier blocks** — `GRANT` bodies may contain `WHEN_<action>` blocks that attach modifications to specific actions performed by the granted-to object. The granted-to object is always the implicit subject performing the action. Fields inside the block describe the other participants using the established vocabulary (`DEFENDING`, `SOURCE`, `RECIPIENT`). The engine interprets these as additional costs or modifications on the action — mechanically equivalent to how MTG rules apply action taxes — not as restrictions with bypasses.

```
WHEN_ATTACK {
  DEFENDING?: <player descriptor | object reference>   # the player, planeswalker, or battle being attacked
  ADDL_COST?: <cost expression>                        # additional cost to perform this action
}

WHEN_BLOCK {
  SOURCE?: <object reference>         # the creature being blocked
  ADDL_COST?: <cost expression>
}

WHEN_TARGET {
  SOURCE?: <filter criteria>          # what is doing the targeting
  ADDL_COST?: <cost expression>
}

WHEN_UNTAP {
  ADDL_COST?: <cost expression>
}

WHEN_CAST {
  SOURCE?: <filter criteria>          # what spell is being cast
  ADDL_COST?: <cost expression>
}

WHEN_DAMAGE_DEALT {
  RECIPIENT?: <object reference | player descriptor>
  COMBAT?: <BOOLEAN>
  ADDL_COST?: <cost expression>
}
```

```
# Propaganda pattern — creatures must pay {2} to attack you
STATIC {
  EFFECT: [
    GRANT {
      FILTER { TYPE: "Creature" SELECTION: ALL }
      WHEN_ATTACK { DEFENDING: YOU }
        ADDL_COST: MANA: "{2}"
    }
  ]
}

# Ward {2} expansion — pay {2} when targeting this permanent
GRANT {
  SELF
  WHEN_TARGET { }
    ADDL_COST: MANA: "{2}"
}

# Sphere of Safety pattern — cost scales with enchantment count
STATIC {
  EFFECT: [
    GRANT {
      FILTER { TYPE: "Creature" SELECTION: ALL }
      WHEN_ATTACK { DEFENDING: YOU }
        ADDL_COST: MANA: "{2}" + COUNT(BATTLEFIELD FILTER { TYPE: "Enchantment" CONTROLLER: YOU })
    }
  ]
}
```

### 15.27 BECOME_MONARCH

```
BECOME_MONARCH {
  PLAYER: <player descriptor>
}
```

Sets the specified player as the monarch. The monarch draws a card at the beginning of their end step and may change when a player deals combat damage to them. This effect is used for ETB and other triggers that cause a player to become the monarch.

### 15.27a CHANGE_CONTROL

```
CHANGE_CONTROL {
  <object reference>?                    # bare subject — single object
  FILTER?: <FILTER { ... }>              # mass effect — use SELECTION inside FILTER
  NEW_CONTROLLER: <player descriptor>    # who gains control
  DURATION?: <duration expression>       # omit for permanent change
}
```

Changes the controller of one or more permanents. For temporary control changes use `DURATION: END_OF_TURN`. For exchange patterns, use two sequential `CHANGE_CONTROL` effects with snapshot bindings to capture original controllers at cast time.

```
# Gain control of target creature until end of turn (Captivating Crew pattern)
CHANGE_CONTROL {
  $creature
  NEW_CONTROLLER: YOU
  DURATION: END_OF_TURN
}

# Exchange control of two permanents
CHANGE_CONTROL { !a NEW_CONTROLLER: !b.controller }
CHANGE_CONTROL { !b NEW_CONTROLLER: !a.controller }
```

### 15.28 CANT_REGENERATE

```
CANT_REGENERATE {
  <object reference>?           # bare subject — for single object; omit for mass effects
  FILTER?: <FILTER { ... }>     # for mass effects — use SELECTION inside FILTER
  FROM?: <zone>                 # source zone; default: BATTLEFIELD
}
```

Applies a state preventing regeneration. Used as a standalone effect preceding `DESTROY` for cards like Terminate and Putrefy, and as a granted triggered ability for cards like Bone Shaman. Not a field on `DESTROY` — regeneration prevention is an independent game state that the engine tracks separately.

```
# Terminate pattern — targeted
CANT_REGENERATE {
  $target
  DURATION: END_OF_TURN
}
DESTROY {
  $target
}

# Damn overload pattern — mass
CANT_REGENERATE {
  FILTER { TYPE: "Creature" SELECTION: ALL }
  FROM: BATTLEFIELD
  DURATION: END_OF_TURN
}
DESTROY {
  FILTER { TYPE: "Creature" SELECTION: ALL }
}
```

### 15.28a REGENERATE

```
REGENERATE {
  <object reference>    # bare subject — SELF or $<variable>
}
```

Places a one-shot regeneration shield on the target for the rest of the turn. Per MTG rule 701.15, the next time that permanent would be destroyed this turn, instead tap it, remove it from combat, and remove all damage from it. The shield is consumed when it prevents a destruction; if not consumed it expires at end of turn.

```
# Regenerate this creature
REGENERATE { SELF }

# Regenerate a targeted creature
REGENERATE { $target }
```

### 15.29 EACH

```
EACH AS $<name> {
  FILTER { <filter criteria> }
}
```

Used inside effect blocks when the destination or amount of an effect depends on a property of each individual object being affected. `EACH` binds each matched object to a variable for downstream reference within the same effect block. For mass effects that don’t need per-object property access, use `FILTER` directly on the effect block instead.

```
# Move each matching permanent to its owner's hand
MOVE {
  EACH AS $permanent {
    FILTER {
      CATEGORY: NONLAND
      CONTROLLER: OPPONENT
    }
  }
  TO: $permanent.owner.hand
}
```

### 15.30 MILL

```
MILL {
  PLAYER: <player descriptor>
  COUNT: <N>
}
```

Moves the top N cards of the specified player’s library to their graveyard, in order. The engine handles ordering — cards are placed in the graveyard in the order they were milled.

```
# Mill three cards from your library
MILL { PLAYER: YOU COUNT: 3 }

# Each opponent mills two cards
MILL { PLAYER: OPPONENTS COUNT: 2 }
```

### 15.30a SACRIFICE (effect)

`SACRIFICE` as an effect primitive forces a player to sacrifice permanents. Distinct from `SACRIFICE` as a cost field (Sections 2.2, 4) — the effect form operates on a player and uses `FILTER` with `SELECTION` for object selection. The specified player chooses which objects to sacrifice within the filter constraints.

```
SACRIFICE {
  <object reference>?              # bare subject — sacrifice a specific known object
  PLAYER?: <player descriptor>     # who performs the sacrifice; default: YOU
  FILTER?: <FILTER { ... }>        # constraints on what may be sacrificed; use SELECTION for count
}
```

```
# Sacrifice a specific token
SACRIFICE { $token }

# Force opponent to sacrifice two nonland permanents of their choice
SACRIFICE {
  PLAYER: OPPONENT
  FILTER { CATEGORY: NONLAND CONTROLLER: OPPONENT SELECTION: 2 }
}

# Lord Xander pattern — sacrifice half nonland permanents rounded down
SACRIFICE {
  PLAYER: $opponent
  FILTER {
    CATEGORY: NONLAND
    CONTROLLER: $opponent
    SELECTION: FLOOR(COUNT(BATTLEFIELD FILTER { CATEGORY: NONLAND CONTROLLER: $opponent }) / 2)
  }
}
```

### 15.30b ATTACH

Moves an Equipment, Aura, or Fortification per rule 701.3a.

```
ATTACH {
  <object reference>
  TO: <object reference | player descriptor>
}
```

```
CREATE_TOKEN { COLOR: W TYPES: ["Creature"] SUBTYPES: ["Soldier"] POWER: 1 TOUGHNESS: 1 } AS $token
ATTACH { SELF TO: $token }
```

### 15.34 CAST

`CAST` is a general effect primitive representing the act of casting a spell. It is used in two forms:

**Bare subject form** — used inside `EFFECT` lists (e.g. `ALT_CAST`) to cast a specific known object:

```
CAST {
  SELF                            # cast this card using its normal spell effect
}
```

**Effect form** — used inside `EFFECT` lists to cast a spell selected from a zone or collection, optionally without paying its mana cost:

```
CAST {
  FROM?: <zone | collection variable>   # zone or specific bound collection to cast from; default YOU.hand
  FILTER?: { <filter criteria> }        # constraints on what may be cast
  MANA?: "<cost>"                       # mana cost override; "{0}" means cast without paying mana cost
  COUNT?: <N | RANGE N..M | ALL>        # number of spells to cast; default 1; RANGE 0..ALL for "any number"
}
```

When `FROM` is a collection variable (e.g. `$exiled`), only cards in that specific collection may be cast — use this to scope “from among those cards” patterns rather than referencing the whole zone.

Both forms are also valid as the subject of a `CANT` block to express casting restrictions.

```
# Cast a spell from hand without paying its mana cost, MV <= damage dealt
CAST {
  FROM: YOU.hand
  FILTER { MV: LTE $event.amount }
  MANA: "{0}"
}

# Cast this card using its normal spell effect (ALT_CAST pattern)
CAST { SELF }
```

### 15.34a PLAY

`PLAY` is a superset of `CAST` that covers oracle text saying “you may play that card” — encompassing both spell casting and land playing. When the selected card is a land, the engine applies land-play rules (counts against the player’s land-play limit for the turn). When it is a nonland, the engine applies casting rules. Use `PLAY` when oracle text says “play”; use `CAST` when oracle text says “cast.”

```
PLAY {
  FROM?: <zone | collection variable>   # zone or specific bound collection; default YOU.hand
  $<variable>?                          # play a specific previously bound card
  FILTER?: { <filter criteria> }        # constraints on what may be played
  MANA?: "<cost>"                       # mana cost override; "{0}" means without paying mana cost
  COUNT?: <N | RANGE N..M | ALL>        # number of cards to play; default 1
}
```

```
# Exile top card of library; until end of your next turn you may play it
EXILE { TOP(1, YOU.library) } AS $exiled
GRANT {
  $exiled
  PLAY { $exiled }
  DURATION: UNTIL { WHEN_END_STEP_BEGIN { FILTER { YOU } } }
}
```

### 15.35 ATTACK

```
ATTACK {
  SUBJECT: $<variable> | SELF     # the creature attacking
}
```

Represents a creature declaring as an attacker.

### 15.35a GOAD

```
GOAD {
  <object reference>              # the creature being goaded
  DURATION?: <duration expression>  # default: until your next turn
}
```

Goads the specified creature: for as long as the effect lasts, its controller must attack with it each combat if able, and it can't attack you or planeswalkers you control. Rule 701.37 governs how "must attack if able" and the attack-restriction interact with other goad effects and attack requirements — not re-derived in CDL.

```
GOAD { $target }
```

### 15.36 BLOCK

```
BLOCK {
  SUBJECT: $<variable> | SELF     # the creature blocking
}
```

Represents a creature declaring as a blocker.

### 15.37 TARGET

```
TARGET {
  SUBJECT: $<variable> | SELF     # the permanent or player being targeted
}
```

Represents a permanent or player being targeted by a spell or ability.

### 15.38 ENCHANT

```
ENCHANT {
  SUBJECT: $<variable> | SELF     # the permanent being enchanted
}
```

Represents a permanent being enchanted by an Aura. When used in `CANT`, the restriction applies both to new attachments and to the ongoing attached state — the engine handles state-based removal of already-attached Auras when the restriction is active.

### 15.39 EQUIP

```
EQUIP {
  SUBJECT: $<variable> | SELF     # the permanent being equipped or fortified
}
```

Represents a permanent being equipped by Equipment or fortified by a Fortification. When used in `CANT`, the restriction applies both to new attachments and to the ongoing attached state — the engine handles state-based detachment when the restriction is active.

### 15.40 COUNTER

See Section 15.7. `COUNTER` as an effect primitive counters a spell or ability on the stack. As a `CANT` target it expresses that the subject cannot be countered:

```
# This spell can't be countered
STATIC {
  EFFECT: [
    CANT { SELF EFFECTS: [COUNTER] }
  ]
}
```

### 15.41 STATS

`STATS` sets characteristic fields on a permanent directly. Substitutive — replaces the specified fields entirely (unlike `GRANT` which is additive). Used inside `AS_` event blocks.

```
STATS {
  NAME?: <string | $variable>
  TYPES?: [<type>, ...]
  SUPERTYPES?: [<supertype>, ...]
  SUBTYPES?: [<subtype>, ...]
  POWER?: <N>
  TOUGHNESS?: <N>
}
```

```
# Psychic Paper pattern — substitute name and subtype as part of equip event
AS_EQUIP {
  CHOOSE AS $chosen_name { OPTIONS: CARD_NAME }
  CHOOSE AS $chosen_type { OPTIONS: CREATURE_TYPE }
  STATS {
    NAME: $chosen_name
    SUBTYPES: [$chosen_type]
  }
}
```

### 15.42 DELAYED_TRIGGER

Creates a floating triggered ability that fires once when its event occurs, then ceases to exist. Independent of any permanent. Per rule 603.7c, still affects referenced objects even if their characteristics change; will not fire if the object has left its expected zone.

```
DELAYED_TRIGGER {
  <event descriptor>
  EFFECT: [ <effect block> ... ]
}
```

```
# Sacrifice tokens at next end step — Mobilize pattern
CREATE_TOKEN { ... } AS $tokens
DELAYED_TRIGGER {
  WHEN_END_STEP_BEGIN { FILTER { ANY } }
  EFFECT: [
    SACRIFICE { $tokens }
  ]
}
```

### 15.31 COPY_SPELL

```
COPY_SPELL {
  TARGET { TARGET_CLASSES: [SPELL] } AS $<n>   # for "copy target spell" — a fresh targeting choice
  | <object reference>                          # bare subject — copy an already-bound spell, no new targeting
  RETARGET?: OPTIONAL   # if present, caster may choose new targets for the copy
}
```

`TARGET` and the bare subject are mutually exclusive, same convention as `MOVE`/`DESTROY`/`EXILE` (Sections 15.3, 15.4, 15.6). Use `TARGET` when the copying ability itself targets a spell on the stack ("copy target instant or sorcery"); use the bare subject when the copied spell is already known from context — most commonly `$event` on a `WHEN_CAST` trigger ("whenever you cast a spell, copy that spell"), where no new targeting decision occurs. Creates a copy of the spell on the stack. The copy is not cast and does not use the stack a second time — it is created directly. If `RETARGET: OPTIONAL` is present, the controller of the copy may choose new targets for it.

```
# Copy target instant or sorcery, may choose new targets
COPY_SPELL {
  TARGET {
    TARGET_CLASSES: [SPELL]
    FILTER { TYPE: ["Instant", "Sorcery"] }
  } AS $spell
  RETARGET: OPTIONAL
}

# "Whenever you cast a spell..., copy that spell. You may choose new
# targets for the copy." (Fire Lord Azula) — no new targeting, the spell
# is already bound from the triggering WHEN_CAST event.
COPY_SPELL {
  $event
  RETARGET: OPTIONAL
}
```

### 15.32 COPY_PERMANENT

Used inside a `TRIGGERED` ETB ability to make a permanent enter as a copy of another. The trigger fires when the permanent enters the battlefield; if the player chooses to copy, the permanent’s characteristics are replaced with those of the chosen original. `MAY: TRUE` on the `TRIGGERED` block makes the copy optional — if declined the permanent enters as itself.

```
COPY_PERMANENT {
  CHOOSE AS $<n> {
    FROM: BATTLEFIELD
    FILTER { <filter criteria> }
  }
}
```

```
# Enter as a copy of any artifact or enchantment — Mirrormade pattern
TRIGGERED {
  @WHEN_ETB { FILTER { SELF } }
  MAY: TRUE
  EFFECT: [
    COPY_PERMANENT {
      CHOOSE AS $original {
        FROM: BATTLEFIELD
        FILTER { TYPE: ["Artifact", "Enchantment"] }
      }
    }
  ]
}
```

### 15.33 COPY_OF (in CREATE_TOKEN)

`CREATE_TOKEN` accepts an optional `COPY_OF` field in place of inline type and stat definitions. The token enters as a copy of the referenced permanent. Any additional fields specified alongside `COPY_OF` override the corresponding copied values — absent fields inherit from the original. This follows the same override semantics as `REPLACE`/`FROM` (Section 17).

```
CREATE_TOKEN {
  COPY_OF: $<variable>         # token is a copy of this permanent
  PLAYER?: <player descriptor>
  COUNT?: <N>
  STATE?: <TAPPED | ATTACKING>
  # any card-level field may be specified to override the copied value
  POWER?: <N>
  TOUGHNESS?: <N>
  NAME?: "<string>"
  # etc.
}
```

```
# Create a tapped and attacking token copy of a target creature
CREATE_TOKEN {
  COPY_OF: $target
  PLAYER: YOU
  STATE: ATTACKING
}

# Create a 1/1 token copy — override power and toughness (Offspring pattern)
CREATE_TOKEN {
  COPY_OF: SELF
  POWER: 1
  TOUGHNESS: 1
}
```

> **Note:** All three COPY constructs are provisional. Structure subject to revision as copy cards are encoded. See Open Questions Q10.

### 15.43 ALTER

Mutates a property of an existing spell or ability on the stack. The subject must be a stack object. `PROPERTIES` contains the new definition of the field(s) being changed — it is not a new targeting action.

```
ALTER {
  <subject — object reference, or TARGET { } AS $var>
  PROPERTIES {
    TARGET?: { <filter criteria> }   # new target definition; omit filter for any legal targets
  }
}
```

An empty `TARGET { }` inside `PROPERTIES` means the controller may choose any currently legal targets for the object, subject to normal targeting rules. Additional properties may be added inside `PROPERTIES` in future versions as new mutation needs are identified.

```
# Deflecting Swat — choose new targets for target spell or ability
TARGET { TARGET_CLASSES: [SPELL, ABILITY] } AS $obj
EFFECT: [
  ALTER {
    $obj
    PROPERTIES {
      TARGET { }
    }
  }
]
```

### 15.44 TRIGGER_COUNT

A static effect: triggered abilities belonging to matched permanents fire a set number of times instead of once ("that ability triggers an additional time").

```
TRIGGER_COUNT {
  FILTER { <filter criteria> }     # which permanents' triggered abilities are affected
  CAUSE?: <event type>             # optional — scope to one triggering event type (WHEN_ATTACKS, WHEN_DIES, ...); omit to affect any triggered ability
  COUNT: <N>                       # total times the ability resolves; 2 = "an additional time"
}
```

`FILTER` scopes which permanents' abilities are affected, using the same criteria as any other `FILTER` block (Section 14.1). `CAUSE` is optional — when present, it restricts the effect to triggered abilities caused by that specific event type (using the same `WHEN_*`/`AS_*` vocabulary as Section 5); when absent, every triggered ability of a matched permanent is affected, regardless of what triggers it.

```
# Annie Joins Up — any triggered ability of a legendary creature you control
STATIC {
  EFFECT: [
    TRIGGER_COUNT {
      FILTER { AND { TYPE: "Legendary" TYPE: "Creature" } CONTROLLER: YOU }
      COUNT: 2
    }
  ]
}

# Isshin, Two Heavens as One — only triggers caused by a creature attacking
STATIC {
  EFFECT: [
    TRIGGER_COUNT {
      FILTER { CONTROLLER: YOU }
      CAUSE: WHEN_ATTACKS
      COUNT: 2
    }
  ]
}
```

-----

## 16. Conditional Expressions

```
CASE {
  WHEN: <condition expression>     # required — first branch condition
  THEN: [ <effect block> ... ]     # required — first branch effects
  WHEN?: <condition expression>    # optional additional branches — evaluated in order, first match wins
  THEN?: [ <effect block> ... ]
  ELSE?: [ <effect block> ... ]    # fallthrough — used for both binary and multi-branch
}
```

`CASE` handles both binary (single `WHEN`/`THEN`/`ELSE`) and multi-branch (multiple `WHEN`/`THEN` pairs) conditionals. The first `WHEN`/`THEN` pair is required; additional pairs and `ELSE` are optional. `ELSE` is always the fallthrough branch. Branches are evaluated in order — first matching `WHEN` wins. `ELSE: []` is valid and means “no effect” — distinct from omitting `ELSE` entirely, which also produces no effect but makes the intent explicit when a card’s oracle text has an explicit “otherwise nothing happens” clause.

**Field usage:** `WHEN` appears only inside `CASE` blocks — it selects a branch of logic flow. `CONDITION` is a block-level guard used on `TRIGGERED` (the ability only triggers if true), `STATIC` (the effect is only active if true), and `MOVE` (the move only occurs if true). Both accept the same condition expression vocabulary.

**`CASE` as inline scalar expression:** `CASE` may appear anywhere a scalar value is expected — it evaluates to the result of the matching `THEN` branch. This enables conditional values inline without a separate variable binding, though a `DECLARE:` binding is equally valid when clarity is preferred.

```
# Jeska's Will pattern — choose one, or both if you control a commander
CHOICE {
  COUNT: RANGE 1..CASE { WHEN: COUNT(YOU.commanders) GTE 1 THEN: 2 ELSE: 1 }
  OPTION { EFFECT: [ ... ] }
  OPTION { EFFECT: [ ... ] }
}

# Equivalent with explicit binding
DECLARE: $max_choices
CASE {
  WHEN: COUNT(YOU.commanders) GTE 1
  THEN: [ $max_choices AS 2 ]
  ELSE: [ $max_choices AS 1 ]
}
CHOICE {
  COUNT: RANGE 1..$max_choices
  OPTION { EFFECT: [ ... ] }
  OPTION { EFFECT: [ ... ] }
}
```

```
# Binary — if tapped deal damage, else gain life
CASE {
  WHEN: SELF IS TAPPED
  THEN: [ DAMAGE { YOU SOURCE: SELF AMOUNT: 1 } ]
  ELSE: [ GAIN_LIFE { PLAYER: YOU AMOUNT: 1 } ]
}

# Multi-branch — different effects per permanent type
CASE {
  WHEN: $revealed IS CREATURE
  THEN: [ MOVE { $revealed TO: BATTLEFIELD } ]
  WHEN: $revealed IS LAND
  THEN: [ MOVE { $revealed TO: BATTLEFIELD } ]
  ELSE: [ MOVE { $revealed TO: YOU.hand } ]
}
```

**The `IS` operator** checks whether an object belongs to a category, type, or state. Used in `WHEN` clauses and `CONDITION` fields:

```
$var IS PERMANENT       # object is any permanent type
$var IS CREATURE        # object is a creature
$var IS LAND            # object is a land
$var IS ARTIFACT        # object is an artifact
$var IS ENCHANTMENT     # object is an enchantment
$var IS PLANESWALKER    # object is a planeswalker
$var IS TOKEN           # object is a token
$var IS LEGENDARY       # object has the Legendary supertype
SELF IS TAPPED          # this permanent is currently tapped
$player IS MONARCH      # this player is currently the monarch
```

`IS` checks set membership — an object can satisfy multiple `IS` checks simultaneously (e.g. an artifact creature IS both ARTIFACT and CREATURE).

**Standalone condition primitives** — used in `WHEN`, `CONDITION`, and `TRIGGERED` `CONDITION` fields:

```
HAS_KEYWORD: <keyword>                              # object has this keyword
THRESHOLD: COUNT(<collection>) <comparison>         # count-based threshold check
MANA_WAS_PAID: "<mana expression>"                  # specific mana was paid for this spell
$var IN <collection>                                # true if $var is a member of the collection
```

`IN` checks whether a bound variable is a member of a collection:

```
CONDITION: $recipient IN YOU.opponents    # recipient is one of your opponents
CONDITION: $card IN YOU.hand              # card is in your hand
```

Boolean permanent properties are valid conditions directly via dot notation:

```
SELF.tapped          # true if this permanent is tapped
$target.tapped       # true if the bound target is tapped
```

**`OR()`/`AND()`** combine condition expressions with boolean logic — distinct from `FILTER`'s `OR { }`/`AND { }`, which combine filter *criteria* rather than standalone condition expressions:

```
CONDITION: OR(SELF.zone EQ BATTLEFIELD, SELF.zone EQ COMMAND_ZONE)
CONDITION: AND(YOU.is_monarch, COUNT(YOU.hand) GTE 7)
```

For temporal scoping (“only during your turn”, “only during combat” etc.) use `DURING:` on the enclosing ability block rather than `CONDITION:` — see Section 19.2.

```
# This creature has flying only during your turn
STATIC {
  DURING: YOUR_TURN
  EFFECT: [
    GRANT { SELF @FLYING }
  ]
}
```

-----

## 17. REPLACE (Replacement Effects)

```
REPLACE {
  EVENT: <event descriptor> AS $<n>   # bind the caught event for downstream reference
  WITH: [ <effect block FROM $<n>> ... ]
  CONDITION?: <condition expression>
}
```

Replacement effects substitute a different event for the original. They do not trigger; they intercept. `EVENT` uses `AS_` event descriptors (Section 5). `REPLACE` is reserved for true event substitution where `WITH` overrides specific fields of the original event.

**Event binding:** The event descriptor is bound to a variable using `AS $name`. This makes the caught event explicitly available for reference in `CONDITION` and `WITH` blocks. The conventional name is `$event` but any `$name` is valid — use distinct names when multiple replacements are in scope.

**Override semantics:** Effect blocks inside `WITH` use `FROM $name` to declare they are built on the caught event. Fields specified in the block override the corresponding fields of the original event; fields absent inherit unchanged. `FROM` is required whenever fields are being inherited — it makes the inheritance contract explicit to the engine.

```
# Double token count — all other token properties inherited from original event
REPLACE {
  EVENT: AS_CREATE_TOKEN { FILTER { CONTROLLER: YOU } } AS $event
  WITH: [
    CREATE_TOKEN FROM $event { COUNT: $event.tokens.count * 2 }
  ]
}
```

Damage *modification* — halving, redirecting, or substituting a different amount — is expressed as `REPLACE` on an `AS_DAMAGE` event. The bound event exposes `$event.source`, `$event.target`, and `$event.amount`.

Damage *prevention* uses the `PREVENT` primitive (Section 15.0). The distinction matters for `DAMAGE_UNPREVENTABLE`: `PREVENT` effects are neutralised by it per rule 615.12, but `REPLACE` effects on damage are not.

```
# Prevent all damage from a specific source (zero it out)
REPLACE {
  EVENT: AS_DAMAGE { FILTER { TARGET: SELF } } AS $event
  WITH: [
    DAMAGE FROM $event { AMOUNT: 0 }
  ]
}

# Halve damage dealt to you (round down)
REPLACE {
  EVENT: AS_DAMAGE { FILTER { TARGET: YOU } } AS $event
  WITH: [
    DAMAGE FROM $event { AMOUNT: FLOOR($event.amount / 2) }
  ]
}
```

-----

## 18. CANT — Action Restrictions

```
CANT {
  SELF | $<variable> | ALL                                    # single subject, or ALL for any object
  | FILTER { <filter criteria> SELECTION: <N | ALL> }             # mass restriction
  EFFECTS: [ <Section 15 primitive> ... ]                     # flat or expanded with inner subject
  DURATION?: <duration expression>
}
```

`CANT` expresses that its subject cannot perform the listed effects. `EFFECTS` is a collection of Section 15 primitives. Effect primitives may be expanded inline with an inner subject specifying what the effect is being restricted against — the outer subject is the thing restricted from performing the effect, the inner subject is the natural subject of that effect (the thing the effect happens to). For `ENCHANT` and `EQUIP`, the restriction applies both to new attachments and to the ongoing attached state. Damage prevention is expressed via `REPLACE` on `AS_DAMAGE` events — see Section 17.

```
# This permanent can't untap
CANT { SELF EFFECTS: [UNTAP] }

# Can't be countered
CANT { SELF EFFECTS: [COUNTER] }

# Can't block at all (defender pattern)
CANT { SELF EFFECTS: [BLOCK] }

# Unblockable — all creatures can't block SELF
CANT {
  ALL
  EFFECTS: [ BLOCK { SELF } ]
}

# Can't be blocked by creatures with power 2 or less
CANT {
  FILTER { TYPE: "Creature" POWER: LTE 2 SELECTION: ALL }
  EFFECTS: [ BLOCK { SELF } ]
}

# Creatures you control can't attack
CANT {
  FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
  EFFECTS: [ATTACK]
}

# Can't be targeted by opponents (hexproof pattern)
CANT {
  FILTER { CONTROLLER: OPPONENT SELECTION: ALL }
  EFFECTS: [ TARGET { SELF } ]
}
```

### 18a. NULLIFY — Restriction Exceptions

```
NULLIFY {
  SELF | $<variable> | ALL                                    # single subject, or ALL for any object
  | FILTER { <filter criteria> SELECTION: <N | ALL> }             # mass exception
  EFFECTS: [ <Section 15 primitive> ... ]                     # same vocabulary as CANT.EFFECTS
  DURATION?: <duration expression>
}
```

The structural inverse of `CANT` (same schema shape, opposite effect): for the listed effects, the subject is no longer prevented from performing them by any restriction that would otherwise apply — including one imposed by its own keyword. Unlike simply not applying the keyword's restriction, `NULLIFY` leaves the source restriction (and the keyword or ability granting it) fully intact; other rules or interactions that check for the restriction's *presence* (e.g. `HAS_KEYWORD: @DEFENDER`) still see it. Only the restriction's *effect* on the listed actions is suppressed for the nullified subject.

```
# Creatures you control can attack as though they didn't have defender
# (Arcades, the Strategist; Felothar the Steadfast) — Defender itself
# (@DEFENDER expands to CANT { SELF EFFECTS: [ATTACK] }) is untouched;
# these creatures still HAS_KEYWORD: @DEFENDER for any effect that checks.
NULLIFY {
  FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
  EFFECTS: [ATTACK]
}
```

### 18b. MUST — Action Requirements

```
MUST {
  SELF | $<variable> | ALL                                    # single subject, or ALL for any object
  | FILTER { <filter criteria> SELECTION: <N | ALL> }             # mass requirement
  EFFECTS: [ <Section 15 primitive> ... ]                     # same vocabulary as CANT.EFFECTS
  DURATION?: <duration expression>
}
```

The structural inverse of `CANT` (same schema shape, opposite polarity — a requirement rather than a restriction): expresses that its subject *must* perform the listed effects whenever able. Rule 508.1d ("attacking is mandatory") and rule 509.1c-style "must block if able" wording govern how this interacts with other requirements and restrictions — not re-derived in CDL. Uses the same inner-subject convention as `CANT`: the outer subject is what's required to perform the effect; an inner subject on the effect itself specifies who/what it's required against.

```
# This creature attacks each combat if able
MUST { SELF EFFECTS: [ATTACK] }

# This creature blocks each combat if able
MUST { SELF EFFECTS: [BLOCK] }

# All creatures able to block the equipped creature do so (Lure idiom,
# "<subject> must be blocked if able" — every creature that can legally
# block it is required to)
MUST {
  ALL
  EFFECTS: [ BLOCK { $equipped } ]
}

# Must be blocked by an Eldrazi if able
MUST {
  FILTER { TYPE: "Eldrazi" SELECTION: ALL }
  EFFECTS: [ BLOCK { SELF } ]
}
```

## 19. Temporal Expressions

### 19.1 DURATION — Effect Expiry

`DURATION` expresses when an effect expires. Used on effect blocks, `GRANT`, `CANT`, `MODIFY`, and `REPLACE`.

```
DURATION: END_OF_TURN
DURATION: END_OF_COMBAT
DURATION: END_OF_STEP          # expires at end of the current step
DURATION: UNTAP_STEP           # active only during the untap step (used with CANT)
DURATION: UNTIL { <condition expression> | <event descriptor> }
DURATION: PERMANENT
```

`UNTIL` accepts either a condition expression (expires when true) or an event descriptor (expires at the next occurrence of that event):

```
DURATION: UNTIL { WHEN_UPKEEP_BEGIN { FILTER { YOU } } }    # until your next upkeep
DURATION: UNTIL { WHEN_END_STEP_BEGIN { FILTER { YOU } } }  # until your next end step
```

### 19.2 DURING — Active Scope

`DURING` scopes when an ability is active or eligible to fire. Used on `ACTIVATED`, `TRIGGERED`, `STATIC`, and `CANT` blocks. Omit for default timing (instant speed for `ACTIVATED`, always-on for `STATIC`).

```
DURING: YOUR_TURN              # any time during your turn
DURING: OPPONENTS_TURN         # any time during an opponent's turn
DURING: SORCERY_SPEED          # your main phase, your turn, empty stack
DURING: MAIN_PHASE             # either main phase, any player's turn
DURING: PRECOMBAT_MAIN         # first main phase only
DURING: POSTCOMBAT_MAIN        # second main phase only
DURING: COMBAT                 # any player's combat phase
DURING: YOUR_COMBAT            # combat phase on your turn only
DURING: UNTAP_STEP             # untap step only
DURING: UPKEEP_STEP            # upkeep step only
DURING: END_STEP               # end step only
DURING: CAST                   # while a spell is being cast — for static abilities that function at cast time
```

```
# Activated ability usable only at sorcery speed
ACTIVATED {
  COST: TAP
  DURING: SORCERY_SPEED
  EFFECT: [ ... ]
}

# Static effect active only during your turn
STATIC {
  DURING: YOUR_TURN
  EFFECT: [
    GRANT { SELF @FLYING }
  ]
}

# Triggered ability that only fires during your turn
TRIGGERED {
  @WHEN_ETB { FILTER { SELF } }
  DURING: YOUR_TURN
  EFFECT: [ ... ]
}

# CANT scoped to untap step only (Mana Vault pattern)
STATIC {
  EFFECT: [
    CANT {
      SELF
      EFFECTS: [UNTAP]
      DURATION: UNTAP_STEP
    }
  ]
}
```

-----

## 20. Player Descriptors

```
YOU
OPPONENT
OPPONENTS
ALL
ACTIVE_PLAYER
NON_ACTIVE_PLAYER
CONTROLLER_OF: <object reference>
OWNER_OF: <object reference>
```

`YOU` always resolves to the current controller of the permanent at the time the ability triggers or resolves, consistent with MTG rules. It does not refer to the original owner or the player who cast the card. `ALL` refers to all players; `OPPONENTS` refers to all players except you. These descriptors are used both as player references and as the base for zone dot notation (e.g. `ALL.graveyard`, `OPPONENTS.hand`).

-----

## 21. Object Descriptors

Object descriptors identify game objects. They may appear as targets, subjects of effects, or filter inputs.

```
SELF    # the card itself
```

`SELF` is the only named object descriptor. Mass effects that operate on a set of objects use `FILTER` directly on the effect block rather than an `ALL` or `EACH` wrapper. Bound variables (`$name`) serve as object references wherever a single previously-identified object is needed.

`EXCEPT` is available on effect blocks that use `FILTER` to exclude a specific object from the matched set. `EXCEPT: SELF` is the idiomatic form for “other [permanents]” patterns — e.g. granting a bonus to all creatures you control except this one:

```
GRANT {
  FILTER { TYPE: "Creature" CONTROLLER: YOU SELECTION: ALL }
  EXCEPT: SELF
  POWER: +1
}
```

-----

## 22. MAY — Optional Effects

Any effect block may include a `MAY: TRUE` field to indicate the effect is optional. The controlling player may choose to skip it at resolution time. When `MAY: TRUE` is present on a single effect within a list, only that effect is optional. If the entire effect list is optional, `MAY: TRUE` is placed on the enclosing ability block.

```
DRAW {
  PLAYER: YOU
  COUNT: 1
  MAY: TRUE
}
```

-----

## 23. Keywords

All keywords are prefixed with `@` and appear directly in the card body. They are macro shorthands that expand to ability blocks at the engine level. Parameterized keywords use `()` for their arguments. Mana parameters are quoted strings. All keyword expansions are defined in Appendix A.

Keyword macros that expand to discrete effects (e.g. `@SURVEIL(<N>)`, `@MOBILIZE(<N>)`) are also valid inside `EFFECT` lists — they resolve as effects when the ability resolves. Keyword macros that express continuous properties (e.g. `@FLYING`, `@HASTE`) are not valid inside `EFFECT` lists — use `GRANT` to apply those temporarily.

Keywords fall into three forms:

- **Non-parameterized** — simple macros: `@FLYING`, `@HASTE`
- **Parameterized** — take a value in `()`: `@WARD("{2}")`
- **Block-parameterized** — take an `EFFECT` block: `@LANDFALL { EFFECT: [ ... ] }`

**Non-parameterized:**

```
@FLYING
@TRAMPLE
@VIGILANCE
@HASTE
@LIFELINK
@DEATHTOUCH
@FIRST_STRIKE
@DOUBLE_STRIKE
@REACH
@HEXPROOF
@SHROUD
@INDESTRUCTIBLE
@MENACE
@FLASH
@DEFENDER
@EXERT
@PROVOKE
@HORSEMANSHIP
@BANDING
@UNBLOCKABLE     # expands to CANT { SELF EFFECTS: [BLOCK] }
@FEAR
@INTIMIDATE
@SHADOW
@WITHER
@PERSIST
@UNDYING
@INFECT
@DELVE
@SWAMPWALK
@MOUNTAINWALK
@PLAINSWALK
@PHASING
@SKULK
@MELEE
@TRAINING
@MYRIAD
@IMPROVISE
@DETHRONE
@EVOLVE
@DEMONSTRATE
@EXTORT
@DECAYED
@UNLEASH
```

**Parameterized:**

```
@TOXIC(<N>)
@BUSHIDO(<N>)
@LANDWALK("<land type>")
@PROTECTION(FILTER { <filter criteria> })   # see examples below
@WARD("<cost>")
@EQUIP(MANA: "<mana>") AS $equipped
# Single parameter — name may be omitted: @EQUIP("{2}") AS $equipped
# $equipped is card-scoped — use DECLARE: $equipped at card level for explicit forward reference
# Activates at sorcery speed — DURING: SORCERY_SPEED implied
@DISGUISE("<mana>")
# Cast face down for {3} as a 2/2 creature with ward {2}; turn face up any time for this cost
@MORPH("<mana>")
# Cast face down for {3} as a 2/2 creature; turn face up any time for this cost
@MEGAMORPH("<mana>")
# Same as @MORPH; turning this face up also puts a +1/+1 counter on it

# Any parameterized keyword macro whose parameter is an activation cost (e.g.
# @EQUIP, @DISGUISE, @MORPH, @MEGAMORPH, @CYCLING and its type variants) accepts an optional
# trailing REDUCTION field, mirroring the REDUCTION field inside ACTIVATED's
# own COST block (Section 4): @EQUIP("{4}" REDUCTION: <expr>) AS $equipped.
# The reduction applies only to that macro's own activation cost, not the
# mana cost to cast the card.

@CREW(<N>)
# Tap creatures with total power GTE N to animate this Vehicle as a creature until end of turn

@MOBILIZE(<N>)
# Whenever this creature attacks, create N tapped and attacking 1/1 red Warrior tokens
# Sacrifice them at next end step via DELAYED_TRIGGER
@ENCHANT(<object descriptor>)
@KICKER("<mana expression>")
@OVERLOAD("<mana expression>")
@FLASHBACK("<mana expression>")
@MIRACLE("<mana expression>")
@ECHO("<mana expression>")
@EVOKE("<mana expression>")
@MADNESS("<mana expression>")
@BLOODTHIRST(<N>)
@SUSPEND(<N>, "<mana expression>")
# Exile with N time counters instead of casting normally; remove a counter
# each upkeep; when the last is removed, cast it without paying its mana cost
@DASH("<mana expression>")
@BUYBACK("<mana expression>")
@MODULAR(<N>)
@REPLICATE("<mana expression>")
@BLITZ("<mana expression>")
@ANNIHILATOR(<N>)
@DREDGE(<N>)
@GRAFT(<N>)
@AMPLIFY(<N>)
@RAMPAGE(<N>)
@AFFLICT(<N>)
@DEVOUR(<N>)
@AFTERLIFE(<N>)
@EMERGE("<mana expression>")
@TRANSMUTE("<mana expression>")
@PROWL("<mana expression>")
@OFFERING("<subtype>")
@READ_AHEAD
@SURVEIL(<N>)
@FLASHBACK("<mana>")        # cast from graveyard; exile on resolution
@HARMONIZE("<mana>")        # cast from graveyard; tap creatures to reduce cost by total power; exile on resolution
@CYCLING("<cost>")          # discard this card: draw a card
@SWAMPCYCLING("<cost>")     # discard this card: search for a Swamp
@PLAINSCYCLING("<cost>")    # discard this card: search for a Plains
@ISLANDCYCLING("<cost>")    # discard this card: search for an Island
@MOUNTAINCYCLING("<cost>")  # discard this card: search for a Mountain
@FORESTCYCLING("<cost>")    # discard this card: search for a Forest
@DESERTCYCLING("<cost>")    # discard this card: search for a Desert
@OFFSPRING("<cost>")        # optional additional cost at cast time; if paid, create a 1/1 token copy on ETB
@FREERUNNING("<cost>")      # may cast for this cost if you dealt combat damage this turn with an Assassin or commander
@PROLIFERATE                # choose any number of permanents/players with counters; add one of each counter kind
```

**Block-parameterized:**

```
@LANDFALL { EFFECT: [ <effect block> ... ] }   # triggers when a land you control enters
@EXALTED { EFFECT: [ <effect block> ... ] }    # triggers when a creature attacks alone
```

**PROTECTION examples:**

```
@PROTECTION(FILTER { COLOR: W })
@PROTECTION(FILTER { TYPE: "Cat" })
@PROTECTION(FILTER { TYPE: "Artifact" })
@PROTECTION(FILTER { OR { TYPE: "Artifact" TYPE: "Enchantment" } })
```

-----

## 24. Open Questions

|ID |Question                                                                                             |Status                                                                                         |
|---|-----------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------|
|Q1 |Layer system representation for overlapping continuous effects                                       |**Closed** — Engine responsibility; CDL expresses intent only                                  |
|Q2 |Split cards, double-faced cards, adventure cards — card structure extension                          |**Partially closed** — `FACE` blocks for DFC; `ADVENTURE` block for adventure cards            |
|Q3 |Token definition format — inline vs. referenced                                                      |**Closed** — Appendix B; `@TokenName` in `CREATE_TOKEN`                                        |
|Q4 |Targeting vocabulary — TARGET / CHOOSE / CHOICE / variable binding                                   |**Closed** — `AS` binding replaces `DECLARE`; see Sections 8, 9, 11                            |
|Q5 |Where do macro/keyword definitions live                                                              |**Closed** — Appendix A                                                                        |
|Q6 |Companion, Saga, Class — phased or conditional ability enabling                                      |Open                                                                                           |
|Q7 |Cost reduction and cost modification effects                                                         |**Closed** — `COST_REDUCTION` (Section 2.1); `ADDL_COST` (Section 2.3)                         |
|Q8 |Interaction between REPLACE and layered effects                                                      |**Closed** — Engine responsibility; layer resolution not in CDL                                |
|Q9 |`ADDL_COST` and `ALT_COST` — simultaneous use and resolution order                                   |**Partially closed** — `ADDL_COST` (Section 2.3); simultaneous payment is engine responsibility|
|Q10|COPY constructs — `COPY_SPELL`, `COPY_PERMANENT`, `COPY_OF` in `CREATE_TOKEN` — structure provisional|Open — pending stress testing against copy cards                                               |

-----

## Appendix A — Keyword & Macro Definitions

### A.1 Keyword Expansions

Stub entries. Full expansions to be defined in a future version.

|Keyword              |Expansion Summary                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
|---------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|FLYING               |This permanent can only be blocked by creatures with FLYING or REACH                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|TRAMPLE              |Excess combat damage assigned to blocker carries over to defending player                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
|DEATHTOUCH           |Any amount of damage this deals destroys the target                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
|LIFELINK             |Damage dealt by this also causes controller to gain that much life                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
|FIRST_STRIKE         |This creature deals combat damage in the first combat damage step                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
|DOUBLE_STRIKE        |This creature deals damage in both combat damage steps                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
|HEXPROOF             |`CANT { FILTER { CONTROLLER: OPPONENT SELECTION: ALL } EFFECTS: [ TARGET { SELF } ] }` — can’t be targeted by opponents                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
|SHROUD               |`CANT { ALL EFFECTS: [ TARGET { SELF } ] }` — can’t be targeted by any player including controller                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
|TOXIC(<N>)           |This creature deals N poison counters to players it deals combat damage to                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
|BUSHIDO(<N>)         |When this blocks or becomes blocked, it gets +N/+N until end of turn                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|LANDWALK(<type>)     |This creature is unblockable if defending player controls a land of the specified type                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
|HORSEMANSHIP         |`CANT { FILTER { TYPE: "Creature" NOT { HAS_KEYWORD: @HORSEMANSHIP } SELECTION: ALL } EFFECTS: [ BLOCK { SELF } ] }` — can only be blocked by creatures with horsemanship                                                                                                                                                                                                                                                                                                                                                                                                                               |
|UNBLOCKABLE          |`CANT { ALL EFFECTS: [ BLOCK { SELF } ] }` — this creature cannot be blocked by anything                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
|PROTECTION(FILTER{…})|Expands to five `CANT` blocks: `CANT { FILTER { <criteria> SELECTION: ALL } EFFECTS: [ TARGET { SELF } ] }`, `CANT { FILTER { <criteria> SELECTION: ALL } EFFECTS: [ ENCHANT { SELF } ] }`, `CANT { FILTER { <criteria> SELECTION: ALL } EFFECTS: [ EQUIP { SELF } ] }`, `CANT { FILTER { <criteria> SELECTION: ALL } EFFECTS: [ DAMAGE { SELF } ] }`, `CANT { FILTER { <criteria> SELECTION: ALL } EFFECTS: [ BLOCK { SELF } ] }`. `ENCHANT`/`EQUIP` cover new attachments and ongoing attached state (engine handles state-based removal). Multiple protection abilities are independent and additive.|
|SURVEIL(<N>)         |Look at top N cards of your library; put any number into your graveyard, rest on top in any order                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

### A.2 Block-Parameterized Keywords

These keywords take an `EFFECT` block and appear at card level.

|Keyword                      |Trigger Condition                                                   |
|-----------------------------|--------------------------------------------------------------------|
|`@LANDFALL { EFFECT: [...] }`|Whenever a land you control enters the battlefield                  |
|`@EXALTED { EFFECT: [...] }` |Whenever a creature you control attacks alone — full expansion below|

```
@EXALTED {
  EFFECT: [
    MODIFY {
      FILTER { TYPE: "Creature" CONTROLLER: YOU ATTACKING: TRUE SELECTION: ALL }
      CONDITION: COUNT(BATTLEFIELD FILTER { TYPE: "Creature" CONTROLLER: YOU ATTACKING: TRUE }) EQ 1
      POWER: +1
      TOUGHNESS: +1
      DURATION: END_OF_TURN
    }
  ]
}
```

Cards with exalted use `@EXALTED` with no inline `EFFECT` — the macro expands to this definition. Multiple `@EXALTED` declarations on the same card are independent and additive.|
|`@RIOT { EFFECT: [...] }`    |When this creature enters, choose +1/+1 counter or haste|
|`@HARMONIZE("<mana>")`       |`KEYWORD("Harmonize")(MANA: "<mana>")` — `ALT_CAST` from graveyard; `TAP` creatures binding `$tapped`; `COST_REDUCTION: SUM($tapped.power)`; `EFFECT: [ CAST { SELF } ]`; exile on resolution|
|`@FLASHBACK("<mana>")`       |`KEYWORD("Flashback")(MANA: "<mana>")` — `ALT_CAST` from graveyard; `EFFECT: [ CAST { SELF } ]`; exile on resolution|
|`@CREW(<N>)`                 |`ACTIVATED { DURING: SORCERY_SPEED COST { TAP { COUNT: RANGE 1..ALL FILTER { TYPE: "Creature" NOT { SELF } } } AS $crew THRESHOLD: SUM($crew.power) GTE <N> } EFFECT: [ GRANT { SELF TYPES: ["Creature"] DURATION: END_OF_TURN } ] }`|
|`@MOBILIZE(<N>)`             |`TRIGGERED { WHEN_ATTACKS { FILTER { SELF } } }` — creates N 1/1 R Warrior tokens `STATE: ATTACKING`; `DELAYED_TRIGGER { WHEN_END_STEP_BEGIN { FILTER { ANY } } EFFECT: [ SACRIFICE { $tokens } ] }`|
|`@AMASS(<N>, "<subtype>")`   |Built entirely from existing primitives, no new core primitive needed: `CASE { WHEN: COUNT(BATTLEFIELD FILTER { TYPE: "Army" CONTROLLER: YOU }) EQ 0 THEN: [ CREATE_TOKEN { COLOR: B TYPES: ["Creature"] SUBTYPES: ["<subtype>", "Army"] POWER: 0 TOUGHNESS: 0 } AS $army ADD_COUNTER { $army NAME: "+1/+1" COUNT: <N> } ] ELSE: [ CHOOSE { FROM: BATTLEFIELD FILTER { TYPE: "Army" CONTROLLER: YOU } COUNT: 1 } AS $army ADD_COUNTER { $army NAME: "+1/+1" COUNT: <N> } ] }` — rule 701.42: create a 0/0 black `<subtype>` Army token if you don't control one, else add the counters to an existing Army you control|
|`@CYCLING("<cost>")`         |`ACTIVATED { ZONE: YOU.hand COST { MANA: "<cost>" DISCARD: SELF } EFFECT: [ DRAW { COUNT: 1 } ] }`|
|`@TYPECYCLING("<cost>", "<subtype>")` |`ACTIVATED { ZONE: YOU.hand COST { MANA: "<cost>" DISCARD: SELF } EFFECT: [ SEARCH { FROM: YOU.library FILTER { TYPE: "<subtype>" } COUNT: 1 REVEAL: TRUE } AS $found MOVE { $found TO: YOU.hand } ] }` — base macro for all land-type cycling variants|
|`@PLAINSCYCLING("<cost>")`   |`@TYPECYCLING("<cost>", "Plains")`|
|`@ISLANDCYCLING("<cost>")`   |`@TYPECYCLING("<cost>", "Island")`|
|`@SWAMPCYCLING("<cost>")`    |`@TYPECYCLING("<cost>", "Swamp")`|
|`@MOUNTAINCYCLING("<cost>")` |`@TYPECYCLING("<cost>", "Mountain")`|
|`@FORESTCYCLING("<cost>")`   |`@TYPECYCLING("<cost>", "Forest")`|
|`@DESERTCYCLING("<cost>")`   |`@TYPECYCLING("<cost>", "Desert")`|
|`@OFFSPRING("<cost>")`       |`STATIC { DURING: CAST MAY: TRUE COST: MANA: "<cost>" EFFECT: [ DELAYED_TRIGGER { @WHEN_ETB { FILTER { SELF } } EFFECT: [ CREATE_TOKEN { COPY_OF: SELF POWER: 1 TOUGHNESS: 1 } ] } ] }` — optional additional cost at cast time; if paid, when creature enters create a 1/1 token copy of it|
|`@FREERUNNING("<cost>")`     |`ALT_COST { MANA: "<cost>" CONDITION: COUNT(YOU.damage_dealt_this_turn FILTER { COMBAT: TRUE SOURCE: { OR { TYPE: "Assassin" SELF IN YOU.commanders } } }) GTE 1 }` — may cast for this cost instead of mana cost if you dealt combat damage this turn with an Assassin or your commander|
|`@PROLIFERATE`              |`CHOOSE { FILTER { TARGET_CLASSES: [PERMANENT, PLAYER] } SELECTION: RANGE 0..ALL PLAYER: YOU } AS $chosen` then `EACH AS $obj IN $chosen { EACH AS $ct IN $obj.counters { ADD_COUNTER { $obj NAME: $ct COUNT: 1 } } }` — choose any permanents/players with counters; add one of each kind already present|

### A.3 General Pattern Macros

Stub entries.

|Macro         |Pattern Summary                                                       |
|--------------|----------------------------------------------------------------------|
|ETB_TRIGGER   |Shorthand for `TRIGGERED { @WHEN_ETB { FILTER { SELF } } ... }`       |
|DIES_TRIGGER  |Shorthand for `TRIGGERED { WHEN_DIES { FILTER { SELF } } ... }`       |
|UPKEEP_TRIGGER|Shorthand for `TRIGGERED { WHEN_UPKEEP_BEGIN { FILTER { YOU } } ... }`|

-----

## Appendix B — Common Token Definitions

Common tokens referenced via `@TokenName` in `CREATE_TOKEN`. The engine resolves the full definition from this appendix. Call-site fields (`PLAYER`, `COUNT`) supplement the definition.

```
@Treasure {
  TYPES: ["Artifact"]
  SUBTYPES: ["Treasure"]
  MANA_ABILITY {
    COST { TAP SACRIFICE: SELF }
    ADD_MANA: "{Any}"
  }
}

@Food {
  TYPES: ["Artifact"]
  SUBTYPES: ["Food"]
  ACTIVATED {
    COST { MANA: "{2}" TAP SACRIFICE: SELF }
    EFFECT: [ GAIN_LIFE { PLAYER: YOU AMOUNT: 3 } ]
  }
}

@Clue {
  TYPES: ["Artifact"]
  SUBTYPES: ["Clue"]
  ACTIVATED {
    COST { MANA: "{2}" SACRIFICE: SELF }
    EFFECT: [ DRAW { PLAYER: YOU COUNT: 1 } ]
  }
}
```

-----

## Appendix C — Counter Definitions

Named counter types referenced throughout CDL. The engine reads these definitions to apply effects automatically. `EFFECT` is optional — marker-only counters have an empty block. Counter effects are continuous and scale per instance.

```
@counter("+1/+1") {
  EFFECT: MODIFY { POWER: +1 TOUGHNESS: +1 }
}

@counter("-1/-1") {
  EFFECT: MODIFY { POWER: -1 TOUGHNESS: -1 }
}

@counter("depletion") { }
@counter("age")        { }
@counter("lore")       { }
@counter("page")       { }
@counter("charge")     { }
@counter("loyalty")    { }
@counter("rope")       { }   # Fraying Line — tracks whether a creature is "saved" each upkeep
```

Counter access via dot notation:

```
SELF.counters."+1/+1"      # integer count of +1/+1 counters
SELF.counters.depletion    # integer count of depletion counters
SELF.counters.*            # collection of all counter instances
COUNT(SELF.counters.*)     # total counter instances
```

-----

## Appendix D — Engine-Required Property Accessors

This appendix tracks property accessors the engine must make available at runtime. Accessors marked **Resolved** are fully defined in the spec. Accessors marked **Unresolved** are required by encoded cards but not yet fully specified — engine implementers should treat them as placeholders pending formal definition.

### D.1 Player Accessors

| Accessor | Returns | Status |
|---|---|---|
| `$player.spells_this_turn` | Integer — spells cast by this player this turn | Resolved |
| `$player.cards_drawn_this_turn` | Integer — cards drawn by this player this turn | Resolved |
| `$player.life` | Integer — current life total | Resolved |
| `$player.hand_size` | Integer — number of cards in hand | Resolved |
| `$player.commander_identity` | Color list — colors in this player's commander's color identity | Resolved |
| `$player.commanders` | Collection — commander permanents this player controls on the battlefield | Resolved |
| `$player.is_monarch` | Boolean — true if this player holds the monarch designation | Resolved |
| `$player.life_lost_this_turn` | Integer — life lost this turn (always >= 0) | Resolved |
| `$player.life_gained_this_turn` | Integer — life gained this turn | Resolved |
| `$player.deck` | Collection — full deckbuilding card pool | Resolved |
| `$player.companions` | Collection — cards declared as companions pre-game | Resolved |
| `$player.lands` | Collection — land permanents controlled by this player | Unresolved |
| `$player.damage_dealt_this_turn` | Collection of damage instances dealt by sources this player controls this turn — each exposes `.combat` (Boolean), `.source` (object reference), `.recipient` (player/object) | Resolved |

### D.2 Permanent / Object Accessors

| Accessor | Returns | Status |
|---|---|---|
| `$obj.tapped` | Boolean — true if permanent is tapped | Resolved |
| `$obj.attacking` | Boolean — true if permanent is attacking | Resolved |
| `$obj.blocking` | Boolean — true if permanent is blocking | Resolved |
| `$obj.blocked_by` | Collection — creatures currently blocking this attacker (empty if unblocked) | Resolved |
| `$obj.power` | Integer — effective power (layer 7 result) | Resolved |
| `$obj.toughness` | Integer — effective toughness (layer 7 result) | Resolved |
| `$obj.base_power` | Integer — layer 7b base power | Resolved |
| `$obj.base_toughness` | Integer — layer 7b base toughness | Resolved |
| `$obj.mod_power` | Integer — layer 7c+ net power modification | Resolved |
| `$obj.mod_toughness` | Integer — layer 7c+ net toughness modification | Resolved |
| `$obj.mana_cost` | Mana expression — printed mana cost of the object | Resolved |
| `$obj.mv` | Integer — mana value of the object | Resolved |
| `$obj.counters.<name>` | Integer — count of named counter type | Resolved |
| `$obj.counters.*` | Collection — all counter instances | Resolved |
| `$obj.controller` | Player — current controller | Resolved |
| `$obj.opponents` | Collection — opponents of this player | Resolved |

### D.3 Collection Accessors

| Accessor | Card | Returns | Status |
|---|---|---|---|
| `<collection>.producible_colors` | Fellwar Stone | Union set of mana colors the collection could produce | Unresolved |

-----

|Version     |Summary                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
|------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|v0.69       |`$obj.blocked_by` accessor added (Appendix D.2) — collection of creatures currently blocking this attacker, empty if unblocked. User-directed design: modeled as a collection rather than a bare `.blocked` boolean, so "is blocked" is `COUNT(SELF.blocked_by) GTE 1` and "isn't blocked" is `COUNT(SELF.blocked_by) EQ 0` — the same collection-truthiness convention already established for counter presence (`counters.depletion`, Section 13), and it leaves room for cards that care *which* creatures are blocking, not just whether any are. Closes the "attacks and isn't blocked" trigger-condition cluster flagged as open in the tenth pass|
|v0.68       |`EVENTS: [ <event block>, ... ]` added to `TRIGGERED` (Section 5) as an alternative to the single event block — one ability firing off any of several event types sharing one `CONDITION`/`EFFECT`, resolving the "X or Y" compound-trigger idiom ("Whenever this creature enters or attacks, ...") without duplicating the ability into two blocks with identical text. User-directed: chosen over the zero-spec-change alternative (one `TRIGGERED` block per event, duplicated `EFFECT`) as the semantically cleaner representation once the cluster's true size was known (164 cards, the majority of a newly-found 319-card class of bug — see the tenth pass)|
|v0.67       |`MUST` primitive added (Section 18b) — structural inverse of `CANT` (same schema, `EFFECTS` vocabulary, and inner-subject convention), same "structural inverse" precedent as `NULLIFY` (v0.59): expresses a subject is required to perform the listed effects whenever able (rule 508.1d "attacking is mandatory"-style requirements) rather than restricted from them. `LOSE?: [<keyword>, ...] \| ALL_ABILITIES` field added to `GRANT` (Section 15.26) — the structural inverse of the bare-keyword additive grant, distinguished by field name in the same block rather than a separate primitive; removes keywords rather than granting them (unlike `NULLIFY`, which leaves the source keyword intact for `HAS_KEYWORD` checks, `LOSE` actually strips it). Both closed via composing the equipped/enchanted-grant trailing-clause cluster's Tier 3 (forced-attack/-block, "loses `<keyword>`"/"loses all abilities"); `GOAD` embedded in a continuous `STATIC` grant uses an explicit `DURATION: PERMANENT` override of its own "until your next turn" default — already-valid syntax (Section 19.1), no primitive change, just the first continuous-goad composition in the codebase|
|v0.66       |Third keyword batch, found via a second full-Scryfall-pool pass after the round-2 keywords closed: `@PHASING`/`@SKULK`/`@MELEE`/`@TRAINING`/`@MYRIAD`/`@IMPROVISE`/`@DETHRONE`/`@EVOLVE`/`@DEMONSTRATE`/`@EXTORT`/`@DECAYED`/`@UNLEASH` (simple); `@ANNIHILATOR`/`@DREDGE`/`@GRAFT`/`@AMPLIFY`/`@RAMPAGE`/`@AFFLICT`/`@DEVOUR`/`@AFTERLIFE` (numeric); `@EMERGE`/`@TRANSMUTE`/`@PROWL` (mana-cost); the four typed-cycling variants (`@PLAINSCYCLING`/`@MOUNTAINCYCLING`/`@FORESTCYCLING`/`@ISLANDCYCLING`) had been documented since before v0.62 but had no keyword_store entry at all — same "documented, never wired" gap as Overload/Flashback/Miracle before them. `Swampcycling`'s existing entry had the same single-mana-symbol regex bug Kicker/Cycling/Equip had (fixed in v0.63/prior) — widened to match. All mirror already-established conventions, no new primitives. Also fixed two real bugs found while investigating, not just added keywords: token-creation "with `<abilities>`" text was swallowing trailing entry-state clauses ("that's tapped and attacking `<ref>`") and token NAME clauses ("named Butterfly") whole into the keyword capture — now stripped into CREATE_TOKEN's existing STATE and NAME fields; `match_equipped_grant`'s new CONDITION support for "has `<keyword>` as long as/if you control a `<Type>`" is deliberately guarded against per-item conditional lists ("lifelink if cleric, deathtouch if rogue, ...") after Multiclass Baldric showed a naive strip would silently apply only the last item's condition to the whole grant|
|v0.64       |`Affinity for <X>` (rule 702.41) wired directly to the already-documented `COST_REDUCTION` COUNT-scaled form (Section 2.1) instead of a symbolic keyword tag — always `"{1}" * COUNT(BATTLEFIELD FILTER { TYPE: "<singularized X>" CONTROLLER: YOU })`, computed at header level (like `ENTERS_TAPPED`/literal "this spell costs X less") and excluded from normal ability-line rendering so it isn't also emitted as a bare `@AFFINITY` line. Not a new primitive — `COST_REDUCTION`'s COUNT-scaled form already existed, just never driven by anything other than the one hardcoded (and incorrectly `TYPE: "Creature"`-only) literal-sentence case|
|v0.63       |Batch of missing keyword macros added (Appendix A), all declarative tags with no new primitives: `@FEAR`/`@INTIMIDATE`/`@SHADOW`/`@WITHER`/`@PERSIST`/`@UNDYING`/`@INFECT`/`@DELVE`/`@SWAMPWALK`/`@MOUNTAINWALK`/`@PLAINSWALK` (simple, mirroring `@ISLANDWALK`/`@FORESTWALK`'s existing convention); `@ECHO`/`@EVOKE`/`@MADNESS` (mana-cost parameterized, mirroring `@DISGUISE`); `@BLOODTHIRST(<N>)` (mirroring `@TOXIC`/`@BUSHIDO`); `@SUSPEND(<N>, "<mana>")` (two-parameter, new `{cost2}` capture-group usage in `KeywordStore._render`, no template mechanism change needed). Also widened `Cycling`/`Equip` keyword_store patterns to accept multi-symbol mana costs (same fix as v0.62's Kicker). Found via full-Scryfall-pool analysis: these were the next-largest `UNKNOWN_KEYWORD` clusters after Enchant/Protection/Morph/Kicker|
|v0.62       |`@MORPH("<mana>")` and `@MEGAMORPH("<mana>")` added (Appendix A) — same activation-cost-macro shape as `@DISGUISE` (cast face down for `{3}` as a 2/2, turn face up for this cost; Megamorph additionally puts a +1/+1 counter on it when turned up), not a new primitive; found via full-Scryfall-pool analysis as the single largest `UNKNOWN_KEYWORD` cluster (141 cards) after `Enchant` and `Protection` were fixed — keyword_store.json simply had no entry for either keyword|
|v0.61       |`MAX_HAND_SIZE { PLAYER: <player descriptor> VALUE: N \| NONE }` added (Section 15.10a) — sets a player's maximum hand size, no prior primitive existed for this genuinely common template ("you have no maximum hand size," Reliquary Tower idiom); `@AMASS(<N>, "<subtype>")` added (Appendix A.2) — deliberately built from *existing* primitives only (`CASE`/`CREATE_TOKEN`/`ADD_COUNTER`/`CHOOSE`, rule 701.42), not a new core primitive, per explicit user pushback against adding one when composition already covers it; encoded: Thought Vessel, Sauron the Dark Lord|
|v0.60       |`COPY_SPELL` — added a bare-subject form (`COPY_SPELL { $event ... }`) alongside the existing `TARGET { }` form, mirroring `MOVE`/`DESTROY`/`EXILE`'s established mutually-exclusive convention — closes "whenever you cast a spell, copy *that* spell" (no new targeting occurs, unlike "copy target spell"); also corrected a spec/parser mismatch — the field was documented as `NEW_TARGETS?: <BOOLEAN>` but the parser has shipped `RETARGET: OPTIONAL` since before this session (e.g. Stella Lee's activated ability) — spec renamed to match already-encoded output rather than the reverse; encoded: Fire Lord Azula, Ulalek Fused Atrocity|
|v0.59       |`NULLIFY { }` added (Section 18a) — structural inverse of `CANT`, same schema shape; suppresses a restriction's effect on the listed actions without removing the restriction's source (keyword/ability stays intact for anything checking its presence, e.g. `HAS_KEYWORD`) — closes the recurring "can attack as though it didn't have defender" idiom (user-directed: keep Defender itself intact, just nullify its attack restriction, rather than a blanket "override" that would hide the keyword too); `DAMAGE_ASSIGNMENT: POWER \| TOUGHNESS` field added to `GRANT` (Section 15.26) — "assigns combat damage equal to toughness rather than power" (Assault Formation idiom); `FILTER?`/`SELECTION` added to `MOVE` (Section 15.6), mirroring `DESTROY`/`EXILE`'s existing mass-effect support — `MOVE` previously required a pre-bound collection variable for any multi-object move, no way to move "all cards in a zone matching a filter" directly; encoded: Arcades the Strategist, Felothar the Steadfast, Zombie Apocalypse|
|v0.58       |`WHEN_TAPPED { FILTER { } }` event added (Section 5) — a matching permanent becomes tapped, regardless of cause (attacking, activation cost, effect); `REDUCTION` field added to activation-cost-bearing parameterized keyword macros (Section 23) — e.g. `@EQUIP("{4}" REDUCTION: <expr>) AS $equipped`, mirroring `ACTIVATED`'s own `COST.REDUCTION` (Section 4); applies only to the macro's own activation cost, not the card's mana cost; `@DISGUISE("<mana>")` documented (previously builtin-only, undocumented); encoded: Kilo Apogee Mind, Magda Brazen Outlaw, Fugitive Codebreaker, Crown of Gondor|
|v0.57       |`$player.damage_dealt_this_turn` accessor added (Appendix D.1) — collection of damage instances dealt by sources this player controls this turn, each exposing `.combat`/`.source`/`.recipient`; `COMBAT:`/`SOURCE: <FILTER { }>` added to FILTER criteria (Section 14.1) to query it (`SOURCE:` follows the `ENCHANTED_BY:` nested-filter idiom); `@FREERUNNING("<cost>")` macro defined (Section 23, Appendix A.2) — may cast for this cost if you dealt combat damage this turn with an Assassin or your commander; encoded: Ezio Auditore da Firenze|
|v0.56       |`GOAD` effect primitive added (Section 15.35a) — target creature must attack each combat if able and can't attack you or planeswalkers you control; `WHEN_SACRIFICE { FILTER { } }` event added (Section 5) — distinct from `WHEN_DIES`, which fires for any battlefield-to-graveyard move regardless of cause; `.zone` property accessor added (Section 13) — the zone a card/permanent currently occupies; `OR()`/`AND()` boolean combinators added for `CONDITION` expressions (Section 16) — distinct from `FILTER`'s `OR { }`/`AND { }`, which combine filter criteria rather than standalone conditions; encoded: Alela Cunning Conqueror, Korvold Fae-Cursed King, The Ur-Dragon, Edgar Markov|
|v0.55       |`ENCHANTED_BY: <FILTER { }>` added to FILTER criteria (Section 14.1) — matches permanents with an attached Aura satisfying the given filter, e.g. "enchanted by an Aura you control"; `*` scaling operator scope widened to `COST`/`REDUCTION` (mana-amount) contexts, not just `COST_REDUCTION`/`ADD_MANA` — scales the amount owed rather than reducing/producing mana, needed for per-counter scaling activation costs (Cumulative upkeep pattern); `ADD_COUNTER?:` added to `COST` schema (Section 4) — counters as a cost, the reverse of `REMOVE_COUNTER`; encoded: Eriette of the Charmed Apple, Mystic Remora, Auntie Ool Cursewretch|
|v0.54       |`REVEAL`/`LOOK` `COUNT: UNTIL { FILTER { } AS $<n>? }` added (Section 15.18a) — reveals cards one at a time from `LOCATION` until one matches `FILTER`, then stops; distinct from `SEARCH` (no shuffle — unmatched revealed cards go to the bottom in random order, the rest of the zone keeps its order); block-level `AS` binds the whole revealed pile, `UNTIL`'s own `AS` binds the single matching card; encoded: Atla Palani Nest Tender, Raph & Mikey Troublemakers, Tom Bombadil|
|v0.53       |`TRIGGER_COUNT` primitive added (Section 15.44) — static effect causing matched permanents' triggered abilities to fire N times instead of once ("triggers an additional time"), with an optional `CAUSE` field scoping it to one triggering event type; no prior primitive could express ability-trigger duplication, distinct from `REPLACE`'s event-replacement semantics (state-based events, not "an ability triggering"); encoded: Annie Joins Up, Splinter Radical Rat, Isshin Two Heavens as One, Teysa Karlov                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
|v0.52       |Section 14.1 clarified: `TYPE: [list]` is OR/IN semantics only (distinct alternatives — "instant or sorcery"); a single object requiring multiple type-line words at once ("basic land", "artifact creature", "legendary creature") must use `AND { TYPE: "X" TYPE: "Y" }` instead — `AND { }` existed in the grammar but had no worked example and was going unused, leading to three parser sites emitting `TYPE: ["Basic", "Land"]`-style lists (OR) where AND was needed; encoded: Rampant Growth, Cultivate, Kodama's Reach, Sakura-Tribe Elder, Solemn Simulacrum, Vibrant Cityscape, Path to Exile, Assassin's Trophy, Myriad Landscape, Urza Chief Artificer, Crown of Gondor                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
|v0.51       |`SELF.controller` removed as a direct event accessor — events have sources, not controllers; the caster-binding idiom is now `SELF.source.controller AS $name` (two examples in Section 5 updated); `$event.source` on `WHEN_CAST` explicitly documented as the spell object on the stack; "may [effect] unless [player] pays [cost]" idiom clarified to use a two-option `CHOICE` (one costed `OPTION` with no `EFFECT`, one uncosted `OPTION` with the fallback `EFFECT`) — no new primitive required; encoded: Rhystic Study, Smothering Tithe, Esper Sentinel                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
|v0.50       |`ALTER` primitive added (Section 15.43) — mutates properties of stack objects; `PROPERTIES` wrapper disambiguates subject from new definition; `GRANT_ON_SPEND` prose clarified with worked example (Delighted Halfling pattern — `SPEND_ONLY` + `GRANT_ON_SPEND: CANT { EFFECTS: [COUNTER] }`); Appendix D added — Engine-Required Property Accessors, tracking resolved and unresolved accessors; two unresolved accessors logged: `$player.lands` and `<collection>.producible_colors` (Fellwar Stone); encoded: Azorius Signet, Boros Signet, Dimir Signet, Despark, Deflecting Swat, Delighted Halfling, Fellwar Stone |
|v0.49       |Phase/step events: omitting `FILTER` defaults to `ANY`; `STATE: ATTACKING(<defender>)` and `STATE: BLOCKING(<attacker>)` for specific combat assignments on `MOVE` and `CREATE_TOKEN`; `TARGET_CLASSES` valid inside `CHOOSE` `FILTER`; `@PROLIFERATE` defined in Section 23 and Appendix A.2; encoded: Krenko Mob Boss, Nekusar the Mindrazer, Atraxa Praetors Voice, Kaalia of the Vast, Zurgo Stormrender                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
|v0.48       |`CONSTRUCTED_OVERRIDE`; `OUTSIDE_GAME`; `PLAYERS`/`OPPONENTS`; `OPPONENTS`→`OPPONENTS`; life/deck/companions accessors; `WHEN_ZONE_CHANGE`; `@WHEN_ETB`/`@WHEN_DRAW` macros; `ATTACH`; encoded: Festercreep, Y’shtola, Teval, Ancestral Blade, Rat Colony                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
|v0.47       |`SELECTION` replaces `AT_LEAST`/`LIMIT` in `FILTER` and `TARGET` globally — unified selection count field; engine infers legality from minimum; `CHANGE_CONTROL` added (Section 15.27a); `SACRIFICE` as effect primitive added (Section 15.30a); `$player.is_monarch` boolean property; `$event.defending` on `WHEN_ATTACKS`; encoded: Windborn Muse, Rhox Faithmender, Captivating Crew, Crown of Gondor, Lord Xander the Collector                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
|v0.46       |`MV` replaces `CMC` as mana value property accessor throughout — `$var.mv`, `MV:` in filters; property accessor valid on right-hand side of filter comparisons — `POWER: GT SELF.base_power` pattern documented (Section 14.1); `WHEN_<action>` modifier blocks added to `GRANT` bodies (Section 15.26) — attaches `ADDL_COST` and other modifications to specific actions; action vocabulary: `WHEN_ATTACK` (`DEFENDING`), `WHEN_BLOCK` (`SOURCE`), `WHEN_TARGET` (`SOURCE`), `WHEN_UNTAP`, `WHEN_CAST` (`SOURCE`), `WHEN_DAMAGE_DEALT` (`RECIPIENT`, `COMBAT`); `@WARD` expansion updated to `GRANT { SELF WHEN_TARGET { } ADDL_COST: MANA: "<cost>" }`; Propaganda and Sphere of Safety patterns documented; `CASE` as inline scalar expression documented (Section 16) — valid anywhere a scalar value is expected; stale `DAMAGE TARGET:` example in Section 16 corrected to bare subject form; encoded: Deadly Rollick, Mana Drain, Kutzil Malamet Exemplar, Propaganda, Jeska’s Will                                                                                                                                                                                                                                                                                                                                                                                                           |
|v0.45       |`VILLAINOUS_CHOICE` removed — construct was redundant with `CHOICE { PLAYER: OPPONENT }`; use `CHOICE` for all opponent-decision patterns; `AT_LEAST?` and `LIMIT?` fields added to `TARGET` schema (Section 8) for “up to N targets” patterns — `AT_LEAST: 0 LIMIT: N` expresses optional targeting; examples added; encoded: Toxic Deluge, Chaos Warp, Garruk’s Uprising, Rhystic Study, Satya Aetherflux Genius                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|v0.44       |`AS_GAIN_LIFE` and `WHEN_GAIN_LIFE` events added (Section 5) — `$event.amount` and `$event.recipient` documented; REPLACE example added; `DEPLETE`/`LIMIT`/`REPLENISH` added to `ACTIVATED` and `MANA_ABILITY` schemas for rate-limiting activations; `DURING?` added to `MANA_ABILITY` schema; `MODE: UNIFORM` added to `ADD_MANA` — choose one color, produce COUNT of it; MODE semantics clarified for no-MODE (independent), `ALL`, and `UNIFORM`; `COUNT: RANGE N..M` documented as globally valid in all effect blocks (Section 15 overview); `MODIFY` and `GRANT` delta slots expanded to accept any scalar expression — `$variable`, `COUNT(...)`, `SUM(...)`; `YOU.commanders` player property accessor added (Section 13) — collection of commander permanents on battlefield; `CONDITION?` added to `ALT_COST` schema (Section 2.2); encoded: The Wind Crystal, Vivi Ornitier, Arcane Denial, Toxic Deluge, Fierce Guardianship                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|v0.43       |`DAMAGE` schema restructured — bare subject first, `TARGET? { }` block form, `SOURCE`/`AMOUNT`/`COMBAT` follow; bare subject for non-targeted damage, `TARGET? { }` for targeted; existing non-targeted examples updated; Section 13.1 added — power/toughness model documenting `power`/`toughness` (effective), `base_power`/`base_toughness` (layer 7b), `mod_power`/`mod_toughness` (layer 7c+) as property accessors; `COUNT?` field added to `CAST` and `PLAY`; `FROM` on `CAST` and `PLAY` extended to accept collection variables; Section 23 note added — keyword macros expanding to discrete effects valid in `EFFECT` lists; `MAY?` and `COST?` fields added to `STATIC` schema with clarifying note; `DURING: CAST` added to Section 19.2 vocabulary; `COPY_OF` override semantics documented in Section 15.33; `@OFFSPRING("<cost>")` defined in Section 23 and Appendix A.2; encoded: Stadium Headliner, Stella Lee Wild Card, Aminatou Veil Piercer, Kotis the Fangkeeper, Zinnia Valley’s Voice                                                                                                                                                                                                                                                                                                                                                                                      |
|v0.42       |`CONDITION?` field added to `ACTIVATED` schema (Section 4) — state-based activation guard, evaluated at activation time, distinct from `DURING` phase/turn scope; `PLAY` primitive added (Section 15.34a) — superset of `CAST` covering oracle text “play”, handles both spell casting and land playing; `ZONE?` field added to `GRANT` schema (Section 15.26) — scopes grant to objects in specified zone (default BATTLEFIELD), engine tracks entry/exit; mana expression arithmetic documented (Section 13) — `+`/`-` operators on mana expression strings, polynomial model over mana symbols, engine floors at `"{0}"` per rule 601.2b; `mana_cost` property accessor added; implicit subject in `GRANT` body documented; syntax conventions updated (Section 1); encoded: Boseiju Who Endures, Gamble, Urtet Remnant of Memnarch                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
|v0.41.1     |`TYPE` filter field unified to match full type line (supertypes, types, subtypes) — `SUBTYPE` and `SUPERTYPE` retired as filter fields; 4 spec examples updated; `REGENERATE` primitive added (Section 15.28a) — one-shot destruction-replacement shield, complement to `CANT_REGENERATE`; `@CYCLING("<cost>")` defined (Appendix A.2); `@TYPECYCLING("<cost>", "<subtype>")` base macro defined; `@SWAMPCYCLING`, `@PLAINSCYCLING`, `@ISLANDCYCLING`, `@MOUNTAINCYCLING`, `@FORESTCYCLING`, `@DESERTCYCLING` defined as aliases; all cycling keywords added to Section 23 reference; encoded: Twisted Abomination                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|v0.41       |`CAST` (Section 15.34) expanded to general effect primitive — bare subject form and FROM/FILTER/MANA effect form both documented; `COMBAT: TRUE` boolean flag added to `WHEN_DAMAGE_DEALT`, `AS_DAMAGE`, and `DAMAGE` effect primitive; `TARGET_TYPE: PLAYER                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
|v0.40.1     |`OBJECT:` field retired globally from all effect blocks — `MOVE`, `DESTROY`, `EXILE`, `CANT_REGENERATE`, `SACRIFICE` all use bare subject syntax consistent with the v0.18 retirement; schemas and all examples updated                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
|v0.40       |Major vocabulary update — all `ON_` event prefixes renamed: `WHEN_` for triggered abilities (stack, can be responded to), `AS_` for simultaneous events (no stack, per rule 614.12a); `REPLACE` reserved for true event substitution; standalone `AS_` blocks for non-substitutive simultaneous effects; `TRIGGERED` gains `DEPLETE`/`LIMIT`/`REPLENISH` for rate-limiting (“triggers only once each turn”); Section 2.4 `KEYWORD` rewritten — `ALT_CAST` block, named parameter slots, `EFFECT: [ CAST { SELF } ]` pattern; `CHOOSE` gains `OPTIONS`/`EXCLUDE` for open-ended value selection (`CARD_NAME`, `CREATURE_TYPE`, `COLOR`); `CHOICE` `TIMING` defaults to `RESOLUTION` inside `TRIGGERED`; `SUM()` scalar function added (Section 13); `MOVE` `OBJECT` accepts collection variable, `EXCEPT` added (Section 15.6); `GRANT` `$variable` subject added, `TYPES` documented as additive (Section 15.26); `STATS` block added for substitutive characteristic setting (Section 15.41); `DELAYED_TRIGGER` added for floating one-shot triggers per rule 603.7c (Section 15.42); `@EQUIP` call site updated, `@CREW`, `@MOBILIZE`, `@FLASHBACK`, `@HARMONIZE` defined (Section 23, Appendix A.2); implicit subject in filter criteria documented (Section 14.1); `rope` counter added (Appendix C); encoded: Hollowmurk Siege, Untethered Express, Fraying Line, Psychic Paper, Voice of Victory|
|v0.31       |Version bump from v0.30.6; accumulated improvements: `TARGET { } AS $name` syntax corrected throughout; `REPLACE` explicit event binding and `FROM` copy constructor; `GRANT` fully open; `ADD_MANA` unified with `COLORS:`, `MODE: ALL`, `SUM`; `KEYWORD` blocks rewritten with `ALT_CAST` and named parameters; `COPY_PERMANENT` moved to `TRIGGERED ON_ETB MAY` pattern; `MOVE` accepts collection variables with `EXCEPT`; `SUM()` scalar function; `TAP` action block in `COST`; `@HARMONIZE` defined                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|v0.30.6     |`MOVE` schema updated — `OBJECT` accepts collection variable, `EXCEPT` added; `SUM(<collection>)` scalar function added (Section 13); `TAP` action block form added to `COST` schema for multi-object tap with collection binding; Section 2.4 `KEYWORD` blocks rewritten — `ALT_CAST` replaces `ALT_COST`, named parameter slots in definitions, single-parameter call sites may omit name, `EFFECT: [ CAST { SELF } ]` pattern for same-effect alternate casts; `COPY_PERMANENT` updated — `TRIGGERED ON_ETB MAY` pattern replaces `REPLACE` approach; `@HARMONIZE` defined in Appendix A.2; encoded: Birthing Ritual, Nature’s Rhythm, Mirrormade                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
|v0.30.5     |`REPLACE` updated — event binding via `AS $name` on event descriptor replaces implicit `$event`; `FROM $name` required on `WITH` effect blocks to declare inheritance from caught event; `$event.source`/`$event.target`/`$event.amount` documented on `ON_DAMAGE`; `GRANT` schema opened — any card-level property, keyword, or effect field is grantable; `COST_REDUCTION` valid inside `GRANT` scoped via `STATIC FILTER`; encoded: Elspeth Storm Slayer, The Earth Crystal                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
|v0.30.4     |`TARGET { } AS $name` binding syntax corrected throughout — `AS` is now consistently a suffix on the closing brace, not a prefix before the block (Sections 8, 11, 15.7, 15.26, 15.31, 23); encoded: Cerebral Vortex                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
|v0.30.3     |`ADD_MANA` block form unified — `COLORS:` required in block form, `COUNT` defaults to 1 with independent-choose semantics, `MODE: ALL` adds one of each color in list; `.colors` property accessor added (Section 13) for permanents and collections; `"{Any}"` shorthand preserved; new `ADD_MANA` examples added; literal-value `OPTION` form documented in Section 10 — `OPTION { "<value>" AS $var }` stores a literal when chosen, enabling card-scoped color/value storage with `DECLARE:`; encoded: Frostboil Snarl, Thriving Isle, Icetill Explorer, Monument to Endurance, Parapet Watchers                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
|v0.30.2     |Structural fixes: duplicate `PROTECTION` row in Appendix A.1 removed (stale `SOURCE_FILTER` reference); `### 2.3 KEYWORD Blocks` renumbered to `### 2.4`; `CONDITION?:` added as optional field to `MODIFY` schema (consistent with `MOVE`); Appendix B token `COST` syntax normalized to block form `COST { }` throughout; `ATTACKING: <TRUE                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
|v0.30.1     |`PREVENT` restored as distinct damage prevention primitive (Section 15.0) per rule 615.12; `DAMAGE_UNPREVENTABLE` added as static effect primitive and grantable property replacing `DAMAGE_CANT_BE_PREVENTED`; Section 17 clarified — `REPLACE` for damage modification, `PREVENT` for prevention shields; `DAMAGE_UNPREVENTABLE` used directly in `STATIC` or inside `GRANT`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
|v0.30       |`FILTER` gains `LIMIT`, `EXACT`, `AT_LEAST` selection fields — replaces standalone `COUNT` on mass effect primitives; `FILTER` default zone documented as battlefield permanents; `DURATION` documented as universally available on any effect block; `ADD_COUNTER` `COUNT` clarified as counters-per-object; `REMOVE_COUNTER` updated to match; `DISTRIBUTE` expanded with `TOTAL`, `MODE`, `AMONG`, `EFFECT` fields; `CANT` schema updated — expanded effect primitives with inner subject, `SOURCE_FILTER` retired, `ALL` valid as subject; Appendix A.1 expansions updated for `@HEXPROOF`, `@SHROUD`, `@UNBLOCKABLE`, `@HORSEMANSHIP`, `@PROTECTION`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
|v0.29       |Variable scoping rules documented — ability-scoped by default, card-scoped when referenced across blocks, explicit `DECLARE:` for forward declarations (Sections 1, 2); `ALT_COST` block form added with `EXILE:` cost field (Section 2.2); `ON_CREATE_TOKEN` event added with `$event.tokens` collection property (Section 5); `SELF.source`/`SELF.recipient` event property accessors added (Section 5); `YOU.opponents` collection added (Section 12); `IN` membership operator for condition expressions (Section 16); `REPLACE` override semantics documented — absent fields inherit from original event, `$event` implicit binding in `WITH` blocks (Section 17); `UNTIL` extended to accept event descriptors (Section 19); `@HORSEMANSHIP` added with `CANT` expansion (Section 23, Appendix A.1)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|v0.28       |`ZONE:` added as optional field on card header (cast-from zone, default `YOU.hand`) and `ACTIVATED` blocks (activate-from zone, default `BATTLEFIELD`); `COST` block form introduced — `MANA`, `TAP`, `UNTAP`, `SACRIFICE`, `DISCARD`, `PAY_LIFE`, `REMOVE_COUNTER`, `REDUCTION` fields; `TAP`/`UNTAP` unified across cost blocks and effect blocks — position determines role; `REDUCTION` inside `COST` block scopes cost reduction to a specific activation; `TARGET_CLASSES` extended with `NONBASIC_LAND` and `BASIC_LAND`; `@EXALTED` fully defined in Appendix A.2 with `ATTACKING: TRUE` filter and solo-attacker `CONDITION`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
|v0.27       |`ADD_MANA` unified into block form `{ <color spec> COUNT?: <N                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
|v0.26.1     |`SEARCH` gains explicit `SHUFFLE?: <BOOLEAN>` field — default `TRUE`, set `FALSE` to suppress shuffle; `TARGET AS !name` snapshot binding form documented in Section 8; `ELSE: []` documented as valid “no effect” branch in Section 16; `PLAYER:` added as a valid filter criterion in Section 14.1 scoped to event block filter contexts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|v0.26       |`TIMING_RESTRICTION` on `ACTIVATED` retired — replaced by `DURING:` field accepted on `ACTIVATED`, `TRIGGERED`, `STATIC`, and `CANT`; `PLAYER_TURN:` condition primitive retired — temporal scoping now expressed via `DURING:` on ability blocks; Section 19 restructured into 19.1 `DURATION` (effect expiry) and 19.2 `DURING` (active scope); `DURING` vocabulary: `YOUR_TURN`, `OPPONENTS_TURN`, `SORCERY_SPEED`, `MAIN_PHASE`, `PRECOMBAT_MAIN`, `POSTCOMBAT_MAIN`, `COMBAT`, `YOUR_COMBAT`, `UNTAP_STEP`, `UPKEEP_STEP`, `END_STEP`; `DURATION` extended with `END_OF_STEP` and `UNTAP_STEP`; `EFFECTS` on `CANT` formalised as a collection with single shared `SOURCE_FILTER`; `@EQUIP` noted as implying `DURING: SORCERY_SPEED`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|v0.25       |Section 18 `PREVENT` retired — replaced by `CANT` block with `EFFECTS` list referencing Section 15 primitives directly; `CANT` covers action restrictions (`UNTAP`, `ATTACK`, `BLOCK`, `TARGET`, `ENCHANT`, `EQUIP`, `COUNTER`, `DAMAGE`, `CAST`); `REPLACE` clarified as the mechanism for damage prevention and modification — `ON_DAMAGE` event with `$source`/`$target`/`$amount` implicit variables available in `WITH`; Sections 15.34–15.39 added (`CAST`, `ATTACK`, `BLOCK`, `TARGET`, `ENCHANT`, `EQUIP`); Section 15.7 `COUNTER_EFFECT` renamed to `COUNTER`, unified with `CANT EFFECTS: [COUNTER]` pattern; `CANT_BE_COUNTERED` top-level card field and `GRANT` flag retired; `CANT_BE_BLOCKED` removed from Section 15.25; `@UNBLOCKABLE` expansion updated; `@PROTECTION` expansion in Appendix A fully defined as five `CANT` blocks; `FLOOR()`/`CEIL()` added to Section 13                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
|v0.24       |Section 5 example corrected — `WHEN:` inside `TRIGGERED` changed to `CONDITION:`; clarifying note added that `OPPONENT` in condition expressions uses indefinite-any semantics; `OBJECT:` wrapper removed from `UNTAP` example in Section 9; `STATIC` schema extended with optional `CONDITION?:` and `FILTER?:` fields (Section 6); `VOTE` restructured — `OPTIONS:` list and `RESULT` wrapper removed, replaced with `OPTION` blocks containing optional `NAME:` label and `WIN_EFFECT:` list, `CUMULATIVE:` at `VOTE` level (Section 15.22); `COUNT: ALL` added to `GRANT` example using `FILTER` (Section 15.26); stale `EXILE { OBJECT: SELF }` corrected to `EXILE { SELF }` in Flashback example (Section 2.3)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
|v0.23.1     |`NOT()` prose corrected to `NOT { }` in Section 14.1; boolean properties note simplified — inline example removed; `HAS_COUNTER` filter criterion removed — use dot notation with truthiness semantics instead; truthiness rule documented in Section 13; `CASE` schema corrected — first `WHEN`/`THEN` marked required; `WHEN` vs `CONDITION` distinction documented in Section 16; version history reordered to newest-first throughout                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
|v0.23       |`IF` retired — replaced by `CASE` with `WHEN`/`THEN`/`ELSE` (Section 16); `IS` operator introduced for classification/category/state checks; `CONTROL`, `OWN`, `IS_TYPE` condition primitives retired — use `IS` operator instead; `IS_MONARCH` migrated to `$player IS MONARCH` form; `!` sigil clarified — immutable snapshot, mutually exclusive with `$`, `!$var` is never valid; `$player.cards_drawn_this_turn` added to Section 13                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
|v0.22.1     |`IS_TAPPED`/`IS_UNTAPPED` filter criteria replaced by `TAPPED: TRUE/FALSE` property comparison; `tapped`, `attacking`, `blocking` documented as boolean permanent properties (Section 13); boolean properties valid as condition expressions via dot notation (Section 16); `EXCEPT` field added to `GRANT`, `DESTROY`, `EXILE`, `TAP`, `UNTAP`, `MODIFY` for excluding specific objects from matched sets; `EXCEPT: SELF` documented as idiomatic “other permanents” pattern (Section 21); versioning convention established: `vX.Y` release, `vX.Y.Z` patch, `vX.Y-alpha` for known blocking gaps                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
|v0.22       |`CREATURE` added to `TARGET_CLASSES` (Section 8); `POWER`/`TOUGHNESS` header fields extended to accept `STAR`, `STAR+N`, `STAR-N`, `$X`; `STAT` block added as static effect (Section 15.25) for continuous base stat definitions; `MODIFY` clarified as delta-only; `GRANT` gains `POWER`/`TOUGHNESS` delta fields; `$X`/`$Y`/`$Z` reserved language relaxed — bound from mana cost when present, otherwise explicit binding required; `STAR` documented in Section 13; five step events added (`ON_UNTAP_STEP_BEGIN`, `ON_DECLARE_ATTACKERS`, `ON_DECLARE_BLOCKERS`, `ON_COMBAT_DAMAGE_RESOLVE`, `ON_CLEANUP_BEGIN`); optional cost pattern documented in Section 10 using `CHOICE COUNT: RANGE 0..1`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
|v0.21       |Version bump from v0.20-alpha; header and footer corrected to v0.21 (footer had not been updated since v0.18)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
|v0.20-alpha |`FILTER_REF` and named `FILTER` declarations removed — reusable filter patterns use `@` macros in Appendix A; Section 14 renumbered (14.1–14.3); `*` scaling operator defined in Section 13; `ADD_MANA` scaling prose standardised; stray `-----` removed between 15.27 and 15.28; `PLAYER_TURN` condition primitive added (Section 16); `EACH_TURN` scoped as `REPLENISH`-only value (Section 10); `!` sigil description sharpened — immutable snapshot semantics                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|v0.20       |`LOYALTY_ABILITY` block added (Section 7.6); phase/step event vocabulary replaced — `ON_UPKEEP`/`ON_PHASE` removed, full named set added (`ON_UPKEEP_BEGIN`, `ON_DRAW_STEP_BEGIN`, `ON_PRECOMBAT_MAIN_BEGIN`, `ON_COMBAT_BEGIN`, `ON_COMBAT_END`, `ON_POSTCOMBAT_MAIN_BEGIN`, `ON_END_STEP_BEGIN`); `ADD_COUNTER`/`REMOVE_COUNTER` formalised — `TYPE` renamed `NAME`, player descriptors valid as subject; `MILL` added (Section 15.30); COPY constructs added provisional (15.31–15.33, Q10); `ADDITIONAL_LAND_PLAY` and `LAND_PLAY_FROM` added to Section 15.25; `@SURVEIL(<N>)` added (Section 23, Appendix A.1); `!$` sigil example corrected                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|v0.19       |Section 2.2/2.3 renumbered; `OBJECT:` prefix removed from `TARGET` examples in Section 8; `NOT { }` syntax restored (reverts v0.15 `NOT()` change); `VILLAINOUS_CHOICE` `OPTIONS` wrapper removed — `OPTION` blocks appear directly; `CANT_BE_BLOCKED` restructured — `OBJECT:` removed, `FILTER` required, subject expressed directly; `@UNBLOCKABLE` keyword added (Section 23, Appendix A.1); `CANT_BE_COUNTERED` removed from Section 15.25 static effects — use card field or `GRANT` instead                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|v0.18       |`!` sigil added for cast-time snapshot binding; `OBJECT` field removed globally — subjects expressed directly; `GRANT_ON_SPEND` field added to `MANA_ABILITY`; dot notation expanded — collection mapping, leaf property values, `.*` wildcard, quoted property names; `MAX()`/`MIN()` scalar functions clarified for dot notation; Section 14.2 simplified — any dot-notation property valid as filter criterion; counter model defined — `SELF.counters.<name>` integer leaf, `SELF.counters.*` collection; `ON_COUNTER_REMOVED`, `ON_COUNTER_ADDED`, `ON_LEAVES_BATTLEFIELD` events added; `LOOK` restructured with `FROM`/`COUNT`/`LOCATION`/`AS`; `REVEAL` added as 15.18a; Appendix C added with counter definitions; encoded: Farewell, Deadly Dispute, Mystical Tutor, Geistwave, Tura Kennerüd, Avacyn’s Pilgrim, Crackling Doom, Satyr Wayfinder, Peat Bog, Ancient Den                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
|v0.17       |`ADDL_COST` top-level card field (Section 2.3); `CHOICE` `COUNT`/`DEPLETE`/`REPLENISH` fields; block-parameterized keywords in Section 23 and Appendix A.2; Q1/Q3/Q7/Q8 closed, Q2/Q9 partially closed; encoded: Supreme Verdict, Tatyova, Austere Command, Solemn Simulacrum, Mind Stone                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
|v0.16       |`COUNT` field added to mass effect blocks (`DESTROY`, `EXILE`, `GRANT`, `CANT_REGENERATE`, `TAP`, `UNTAP`, `MODIFY`) — required when using `FILTER`, accepts `N` or `ALL`; `FROM` field added to `EXILE` and `CANT_REGENERATE` for explicit zone scoping (default `BATTLEFIELD`); `DESTROY` documented as always battlefield-scoped with no `FROM` field; `FILTER` added to `CANT_REGENERATE` for mass regeneration prevention; encoded: Temur Ascendancy, Void Rend, Vampiric Tutor, Frantic Search, Damn                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|v0.15       |Section ordering corrected (15.25→15.26→15.27→15.28→15.29); `REVEAL: TRUE` added to `SEARCH`; `CAST_FROM` and `ON_RESOLVE` added to `KEYWORD` blocks; `NOT()` replaces `NOT { }` as filter operator; `CONDITION` field added to `FILTER`; `GRANT` restructured — `PROPERTIES` wrapper removed, `CANT_BE_COUNTERED: TRUE` valid as granted property; player property accessors added (`$player.spells_this_turn`, `.life`, `.hand_size`); property accessors valid in cost expressions; zone target classes added to `TARGET_CLASSES` (`GRAVEYARD`, `HAND`, `LIBRARY`, `BATTLEFIELD`, `STACK`, `EXILE_ZONE`); `CANT_BE_COUNTERED: TRUE` added as top-level card field; encoded: Elvish Mystic, Vandalblast, Faithless Looting, Enlightened Tutor, Baleful Strix, Rhythm of the Wild, Esper Sentinel, Rakdos Charm                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
|v0.14       |`SELF` in event blocks clarified as referring to the triggering event (`SELF.player` binds the acting player); `CANT_BE_COUNTERED: TRUE` added as top-level card field; `KEYWORD("<name>") { }` block added (Section 2.2) for complex keywords with `ALT_COST` and replacement ability blocks; `EACH AS $name { FILTER { } }` construct added (Section 15.29) for per-object iteration with property binding; common token system added — `TOKEN: @Name` in `CREATE_TOKEN` references Appendix B; Appendix B added with canonical definitions for `@Treasure`, `@Food`, `@Clue`; encoded: Generous Gift, Putrefy, Feed the Swarm, Reanimate, Growth Spiral, Brainstorm, Smothering Tithe, Phyrexian Arena, Dovin’s Veto, Cyclonic Rift                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
|v0.13       |`TARGET` restructured — `TYPE`/`CATEGORY` fields replaced by `TARGET_CLASSES` list with vocabulary `PLAYER`, `PLANESWALKER`, `PERMANENT`, `SPELL`, `ABILITY`, `CARD`; `FILTER` remains for within-class constraint; `DESTROY` updated to support `TARGET`, `OBJECT`, and `FILTER` forms (mass destroy uses `FILTER` directly); `ALL`/`EACH` object descriptor wrappers removed — effect blocks take `FILTER` directly; `SELF` remains as sole named object descriptor; `CANT_REGENERATE` standalone effect primitive added (Section 15.28) with `OBJECT` and `DURATION`; `CATEGORY: NONLAND` clarified as always implying permanent context; encoded: Anguished Unmaking, Boros Charm, Demonic Tutor, Sakura-Tribe Elder, Ruinous Ultimatum, Negate, Swan Song, Terminate, Eternal Witness                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|v0.12       |`ADD_MANA` promoted to block form with `PLAYER` and `AMOUNT` fields when used in `EFFECT` lists; event block subject binding added (`SELF.controller AS $name` before `FILTER`); `CHOICE` `OPTIONS` wrapper removed — `OPTION` blocks appear directly inside `CHOICE`; `OPTION` may now have `COST` only with no `EFFECT` for “pay or else” patterns; `COST_REDUCTION` top-level card field added (Section 2.1) with COUNT-scaled expression support; encoded: Rampant Growth, Farseek, Dark Ritual, Rhystic Study, Blasphemous Act                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
|v0.11       |Hybrid mana syntax documented (`{B/G}`, `{W/P}` etc.); `{C}{C}` multi-colorless clarified; `ADD_MANA` scaled production uses `"<symbol>" * COUNT(...)` multiplication syntax; `TRIGGERED` event descriptors restructured as blocks with `FILTER` subject (`ON_ETB { FILTER { SELF } }` etc.); `EACH_PLAYER`/`EACH_OPPONENT` replaced by `ALL`/`OPPONENTS` throughout; `ALL.graveyard`, `ALL.hand`, `ALL.library` zone references added; `ANY_GRAVEYARD` removed; `SEARCH` shuffle semantics clarified — always last, engine holds references, `SHUFFLE` field removed; `EXILE` restructured with `TARGET`/`OBJECT` split and `ZONE` field; `MOVE` `FROM` renamed to `ZONE`; `FILTER` list/IN semantics documented (implicit AND between fields, IN for value lists); `ADD_MANA` permitted as effect-list primitive in `ACTIVATED`; `BECOME_MONARCH` effect primitive added (Section 15.27); `IS_MONARCH` condition primitive added (Section 16); encoded: Myriad Landscape, Ancient Tomb, Gaea’s Cradle, Deathrite Shaman, Queen Marchesa                                                                                                                                                                                                                                                                                                                                                             |
|v0.10       |`CATEGORY` field added to `TARGET` and `FILTER` blocks for engine-level object classification; `COLOR` and `PLAYER` fields added to `CREATE_TOKEN`; `PLAYER` and `MAY` fields added to `SEARCH`; chained dot notation documented (`$var.controller.library`); `MANA_ABILITY` updated with `CHOICE` on `ADD_MANA` and optional `EFFECT` list for side effects; mana ability classification clarified per rule 605.1a; `ADD_MANA` valid in `ACTIVATED` blocks; encoded: Counterspell, Path to Exile, Heroic Intervention, Beast Within, Talisman of Dominance                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
|v0.9        |`TIMING_RESTRICTION` field added to `ACTIVATED`; `SACRIFICE` cost accepts `CHOOSE` block; zone dot notation (`YOU.library`, `YOU.hand` etc.) replaces `LIBRARY: <player>` syntax; `EXCEPT` collection operator added; `SEARCH` updated with implicit shuffle, `COUNT`, `ACTIVE_SELECTION`, `CONSTRAIN`; `MOVE` updated with `STATE` and `LOCATION` fields; `SHUFFLE` updated to zone dot notation; `YOU` clarified as current controller at resolution time; encoded: Vault of the Archangel, Transmogrant Altar, Vibrant Cityscape, Imperial Seal, Cultivate                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
|v0.8        |`DECLARE` removed — replaced by inline `AS` binding on `TARGET`, `CHOOSE`, and scalar expressions; dot notation for property access (`$var.power`, `$var.controller` etc.); `$X`/`$Y`/`$Z` reserved for mana cost variables; `RANGE N..M` added for variable counts; `GRANT` block added (Section 15.26); `CHOSEN_AS` removed from `CHOOSE`; Section 11 rewritten as Variable Binding; Section 13 expanded with property access and RANGE; encoded: Sol Ring, Birds of Paradise, Swords to Plowshares, Fires of Yavimaya, Snap                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
|v0.7-alpha_2|Flat card block structure (keywords and ability blocks inline, no wrapper lists); `SUPERTYPES`, `TYPES`, `SUBTYPES` as string literal lists; `@` sigil for keyword macros; `$` sigil for variables; `#` for comments; `PROTECTION` uses `FILTER` parameter; `MANA_ABILITY` simplified (`ADD_MANA` direct field); filter criteria updated to string type values; `KEYWORDS: []` list removed; Section 23 unified keyword reference rewritten                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
|v0.6        |Block delimiters `{}`, quoted mana expressions, `ABILITIES:` rename, discrete stat fields, `MANA_ABILITY` block, `COUNTER_EFFECT` rename, `MODIFY` block, `ADD_MANA` primitive, static effect declarations, `CHOICE`/`CHOOSE` split, `VOTE` with WIN/CUMULATIVE, `DISTRIBUTE` with USES_TARGETS, `VILLAINOUS_CHOICE`, scalar functions, Appendix A stubs, Q4/Q5 closed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
|v0.5        |Consolidated choice constructs; clarified cast-time vs resolution-time semantics                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
|v0.4        |Added DISTRIBUTE (preliminary), expanded object descriptors and filters                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
|v0.3        |Added CHOOSE, DECLARE, REPLACE, PREVENT, MODIFY                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
|v0.2        |Added SPELL block, TARGET, CHOICE, MAY, IF/THEN/ELSE, DURATION                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
|v0.1        |Initial structure: CARD, ACTIVATED, TRIGGERED, STATIC, core effects                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

-----

*This document is the authoritative specification as of v0.69. All prior versions are superseded.*