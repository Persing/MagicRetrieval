"""Multi-seed training actually varies, and the same seed reproduces.

This is the highest-value test in T4, and its absence is why the bug it guards existed.

`SentenceTransformer.fit()` takes no `seed` argument. It constructs a
`SentenceTransformerTrainingArguments` internally with the default `seed=42`, and
`transformers.Trainer.__init__` then calls `set_seed(self.args.seed)` unconditionally — which
overwrites any `torch.manual_seed()` the caller set first. Under that path all five of T4's
"seeds" produce the same run apart from GPU kernel nondeterminism, so the pooled seed standard
deviation — the quantity the entire null-result rule is measured against — would have been
reporting cuBLAS jitter rather than training variance.

These tests are slow (they really do train), so they are marked and kept tiny: 20 examples,
1 epoch, CPU-sized. They are not checking that training works; they are checking that the seed
reaches the thing that consumes it.
"""

import numpy as np
import pytest

from mr import finetune

pytestmark = pytest.mark.slow


# Must exceed one batch. `MultipleNegativesRankingLoss` draws its negatives from within the batch,
# so with n <= TRAIN_BATCH_SIZE every seed sees one identical batch and shuffling changes nothing —
# the run would look seed-invariant for a reason that has nothing to do with whether the seed is
# wired up. 160 examples at batch 32 gives 5 batches whose composition genuinely varies.
_N = 160
TEXTS = {f"oid{i}": f"Creature — Beast {{{i}}} 2/2 [SEP] Whenever this attacks, draw {i} cards."
         for i in range(_N)}
POSITIVES = [(f"oid{i}", f"oid{(i + 1) % _N}") for i in range(_N)]
ORDER = sorted(TEXTS)


def _run(seed: int) -> np.ndarray:
    return finetune.finetune(POSITIVES, None, TEXTS, ORDER, seed=seed, epochs=1)


def test_seed_is_a_required_keyword():
    """No default, so no caller can silently fall back to a fixed seed — which is exactly how
    the fit() path went unnoticed."""
    with pytest.raises(TypeError):
        finetune.finetune(POSITIVES, None, TEXTS, ORDER, epochs=1)  # type: ignore[call-arg]


def test_different_seeds_produce_different_embeddings():
    a, b = _run(42), _run(43)
    assert a.shape == b.shape
    assert not np.allclose(a, b, atol=1e-6), (
        "Two seeds produced identical embeddings — the seed is not reaching the trainer, so the "
        "pooled seed sd would be measuring GPU noise instead of training variance."
    )


def test_same_seed_reproduces():
    """Bit-exactness is not claimed — cuDNN/cuBLAS reduction order stays nondeterministic on
    purpose, because honest seed variance is the point. What must hold is that same-seed runs are
    far closer to each other than different-seed runs."""
    a, b = _run(42), _run(42)
    same_seed_delta = float(np.abs(a - b).mean())
    diff_seed_delta = float(np.abs(a - _run(43)).mean())
    assert same_seed_delta < diff_seed_delta, (
        f"same-seed delta {same_seed_delta:.2e} is not below different-seed delta "
        f"{diff_seed_delta:.2e} — seeding is not controlling data order."
    )


def test_max_seq_length_actually_truncates():
    """`max_seq_length` is a recipe parameter set once for every arm. If it silently reverted to
    the model default of 256, arms B+ and C would truncate ~1.9% of cards while A and B truncated
    none, and that asymmetry would be measured as representation. This proves the knob is live:
    the same long text encoded under a tight and a generous limit must differ."""
    long_text = {"x": "draw a card. " * 200}
    tight = finetune.zero_shot_embeddings(["x"], long_text, max_seq_length=16)
    generous = finetune.zero_shot_embeddings(["x"], long_text, max_seq_length=512)
    assert not np.allclose(tight, generous, atol=1e-5)


def test_embeddings_are_l2_normalized():
    norms = np.linalg.norm(_run(42), axis=1)
    assert np.allclose(norms, 1.0, atol=1e-4)
