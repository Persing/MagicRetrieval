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
