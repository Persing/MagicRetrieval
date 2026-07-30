"""build_examples: negatives must stay keyed to the anchor they were mined for.

This is the fix for the bug MagicSpike shipped (mined negatives computed, never wired to the loss)
and a bug this repo introduced and fixed in turn (negatives cycled independently of which positive
was being trained, desyncing "hard negative for X" from X's own row).
"""

from mr.finetune import build_examples

TEXTS = {
    "sol_ring": "Artifact [SEP] Add {C}{C}.",
    "arcane_signet": "Artifact [SEP] Add one mana of any color in your commander's color identity.",
    "cultivate": "Sorcery [SEP] Search your library for a basic land card.",
    "kodama": "Sorcery [SEP] Search your library for up to two basic land cards.",
    "counterspell": "Instant [SEP] Counter target spell.",
    "no_text": "",
}


def test_no_negatives_gives_two_tuples():
    positives = [("sol_ring", "arcane_signet"), ("cultivate", "kodama")]
    examples = build_examples(positives, None, TEXTS)
    assert examples == [(TEXTS["sol_ring"], TEXTS["arcane_signet"]),
                        (TEXTS["cultivate"], TEXTS["kodama"])]


def test_negative_is_used_for_the_anchor_it_was_mined_for():
    positives = [("sol_ring", "arcane_signet"), ("cultivate", "kodama")]
    # sol_ring's mined hard negative is counterspell; cultivate's is arcane_signet.
    negatives = [("sol_ring", "counterspell"), ("cultivate", "arcane_signet")]
    examples = build_examples(positives, negatives, TEXTS)
    assert examples[0] == (TEXTS["sol_ring"], TEXTS["arcane_signet"], TEXTS["counterspell"])
    assert examples[1] == (TEXTS["cultivate"], TEXTS["kodama"], TEXTS["arcane_signet"])


def test_negative_never_leaks_across_anchors():
    """The bug this guards against: cycling negatives by position instead of by anchor identity
    would hand sol_ring's negative to cultivate's row whenever list order lined up that way."""
    positives = [("cultivate", "kodama")]
    negatives = [("sol_ring", "counterspell")]  # mined for a DIFFERENT anchor than any positive
    examples = build_examples(positives, negatives, TEXTS)
    # cultivate has no mined negative of its own, so it falls back to the pool — but the fallback
    # must be visibly a fallback (tested here via the only pool entry), never silently mislabeled
    # as a hard negative FOR cultivate.
    assert examples[0][:2] == (TEXTS["cultivate"], TEXTS["kodama"])
    assert examples[0][2] == TEXTS["counterspell"]  # only pool entry, used as fallback


def test_missing_text_drops_the_example():
    positives = [("sol_ring", "no_text"), ("cultivate", "kodama")]
    examples = build_examples(positives, None, TEXTS)
    assert len(examples) == 1
    assert examples[0] == (TEXTS["cultivate"], TEXTS["kodama"])


def test_missing_negative_text_drops_the_example():
    positives = [("sol_ring", "arcane_signet")]
    negatives = [("sol_ring", "no_text")]
    examples = build_examples(positives, negatives, TEXTS)
    assert examples == []


def test_fallback_used_when_anchor_has_no_mined_negative():
    positives = [("sol_ring", "arcane_signet"), ("cultivate", "kodama")]
    negatives = [("sol_ring", "counterspell")]  # cultivate has no entry
    examples = build_examples(positives, negatives, TEXTS)
    assert examples[0][2] == TEXTS["counterspell"]  # sol_ring's own mined negative
    assert examples[1][2] == TEXTS["counterspell"]  # cultivate falls back to the only pool entry


# ── the drop that cost arm D two thirds of its data ───────────────────────────

def test_positive_is_dropped_when_its_NEGATIVE_lacks_text():
    """The non-obvious drop, and the one that mattered.

    A positive survives only if its anchor, its partner AND its assigned negative all have text
    under this arm. Arm D has text for a third of the pool, so of 28,215 positives with text at
    both ends only 8,702 survived — 69.2% lost at the negative lookup. The grid recorded the
    both-ends figure as `n_trainable_positives` and reported it as the training-set size, so a
    published caveat understated the confound by 3.2x. Arms with full coverage lose ~0.1% here,
    which is why it stayed invisible.
    """
    from mr.finetune import build_example_oids
    positives = [("a", "b")]
    negatives = [("a", "no_text_card")]
    assert build_example_oids(positives, negatives, {"a", "b", "no_text_card"}) == [("a", "b", "no_text_card")]
    assert build_example_oids(positives, negatives, {"a", "b"}) == []   # negative has no text


def test_build_example_oids_matches_build_examples():
    """The oid walk and the text walk must not drift — D4 replays another arm's exact triples
    through the former and trains on the latter."""
    from mr.finetune import build_example_oids, build_examples
    positives = [(f"c{i}", f"c{(i + 1) % 9}") for i in range(9)]
    negatives = [("c0", "c5"), ("c3", "c7")]
    texts = {f"c{i}": f"text {i}" for i in range(9)}
    texts["c7"] = ""                                     # a negative with no text
    oids = build_example_oids(positives, negatives, {o for o, t in texts.items() if t})
    assert [tuple(texts[o] for o in row) for row in oids] == build_examples(positives, negatives, texts)


def test_filtering_positives_first_reassigns_negatives():
    """`fallback_pool[i % len]` is indexed by position in the FULL list, so pre-filtering silently
    changes which negative each surviving anchor trains against. Pinned so nobody 'simplifies'
    D4's selection into a filter."""
    from mr.finetune import build_example_oids
    positives = [("x", "y"), ("a", "b"), ("p", "q")]
    negatives = [("n1", "neg1"), ("n2", "neg2"), ("n3", "neg3")]
    everything = {"x", "y", "a", "b", "p", "q", "neg1", "neg2", "neg3"}

    full = build_example_oids(positives, negatives, everything)
    got = next(row for row in full if row[0] == "p")     # index 2 -> fallback_pool[2] -> neg3
    pre_filtered = build_example_oids([("p", "q")], negatives, everything)[0]  # index 0 -> neg1
    assert got[2] != pre_filtered[2]
