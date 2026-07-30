"""Fine-tune MiniLM on mined pairs, with negatives actually wired to the loss.

MagicSpike's `src/analysis/embeddings.py` computed hard negatives and then built training examples
as `InputExample(texts=[anchor, positive])` — anchor+positive only — under
`MultipleNegativesRankingLoss`, which falls back to random in-batch negatives. The mined negatives
were never consumed. That is fixed here: `InputExample(texts=[anchor, positive, negative])`, which
MNRL treats as an explicit hard negative for that anchor, on top of the usual in-batch negatives.

Text encoding (`_card_text`) is reproduced exactly from the original so arm 2a is a faithful
baseline: type line + oracle text, joined by `[SEP]`, no card name.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import config, thresholds


def card_text(oracle_text: str | None, type_line: str | None) -> str:
    parts = [p for p in [str(type_line or ""), str(oracle_text or "")] if p]
    return config.TEXT_SEP.join(parts)


def text_lookup(cards_df: pd.DataFrame) -> dict[str, str]:
    return {
        row.oracle_id: card_text(row.oracle_text, row.type_line)
        for row in cards_df.itertuples(index=False)
    }


def seed_everything(seed: int) -> None:
    """Seed every RNG outside the HF Trainer. The Trainer seeds itself from its own args (see
    `finetune`), so this covers mining, sampling and anything else in the run."""
    import random

    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def zero_shot_embeddings(oracle_ids: list[str], texts_by_oid: dict[str, str],
                         max_seq_length: int | None = None) -> np.ndarray:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(config.BASE_MODEL)
    if max_seq_length is not None:
        model.max_seq_length = max_seq_length
    texts = [texts_by_oid.get(oid, "") for oid in oracle_ids]
    return model.encode(
        texts, batch_size=config.BATCH_SIZE, show_progress_bar=False,
        normalize_embeddings=True, convert_to_numpy=True,
    )


def build_example_oids(
    positives: list[tuple[str, str]],
    negatives: list[tuple[str, str]] | None,
    has_text: set[str],
) -> list[tuple[str, ...]]:
    """The oid-level walk that decides which training examples exist. See `build_examples`.

    Split out from the text mapping for two reasons. It makes the *count* of surviving examples
    computable without building strings — and that count is not what the obvious predicate says
    (below). And it lets a caller replay another arm's exact example set, which is the only way to
    hold training data fixed while varying the encoding.

    **A positive is dropped when its assigned NEGATIVE has no text under this arm**, not only when
    one of its own ends does. That is easy to miss and it was: arm D has text for 10,628 of 30,958
    cards, so of its 28,215 positives with text at both ends, only **8,702** survived the negative
    lookup — 69.2% lost. The grid reported 28,215 as "trainable" and never saw the real figure.
    Arms with full text coverage lose ~0.1%, which is why this stayed invisible.

    The walk must run over the **full** positives list. `fallback_pool[i % len(fallback_pool)]` is
    indexed by position, so filtering positives before the walk silently reassigns which negative
    each surviving anchor trains against.
    """
    if not negatives:
        return [(a, p) for a, p in positives if a in has_text and p in has_text]

    neg_by_anchor: dict[str, str] = {}
    for na, nb in negatives:
        neg_by_anchor.setdefault(na, nb)
    fallback_pool = list(negatives)

    out: list[tuple[str, ...]] = []
    for i, (a, p) in enumerate(positives):
        if a not in has_text or p not in has_text:
            continue
        neg_partner = neg_by_anchor.get(a)
        if neg_partner is None:
            # No mined negative for this specific anchor — fall back to cycling the pool rather
            # than dropping the example; still same-colour/non-co-occurring overall, just not
            # guaranteed hard for THIS anchor.
            neg_partner = fallback_pool[i % len(fallback_pool)][1]
        if neg_partner in has_text:
            out.append((a, p, neg_partner))
    return out


def build_examples(
    positives: list[tuple[str, str]],
    negatives: list[tuple[str, str]] | None,
    texts_by_oid: dict[str, str],
) -> list[tuple[str, ...]]:
    """Pure text-tuple construction, kept separate from `finetune()` so it's testable without
    importing sentence-transformers/torch.

    `negatives`, when given, must be anchor-keyed: `mining.mine_hard_negatives` returns
    (anchor, negative_partner) pairs, so a negative mined for card X is only ever used as X's hard
    negative. Cycling through the negative list independently of which positive is being trained
    (an earlier version of this did that) desyncs the two — a "hard negative for Sol Ring" could
    end up training against Cultivate's row, which is not a hard negative for Cultivate at all,
    just a same-colour card picked at random from Sol Ring's perspective.

    Returns 2-tuples `(anchor_text, positive_text)` when `negatives` is None (in-batch-only
    training, arms 2a/2b), or 3-tuples `(anchor_text, positive_text, negative_text)` otherwise.
    """
    has_text = {oid for oid, t in texts_by_oid.items() if t}
    return [tuple(texts_by_oid[o] for o in row)
            for row in build_example_oids(positives, negatives, has_text)]


def finetune(
    positives: list[tuple[str, str]],
    negatives: list[tuple[str, str]] | None,
    texts_by_oid: dict[str, str],
    oracle_ids: list[str],
    *,
    seed: int,
    max_seq_length: int = thresholds.T4_MAX_SEQ_LENGTH,
    output_dir: Path | None = None,
    epochs: int = config.FINETUNE_EPOCHS,
    examples: list[tuple[str, ...]] | None = None,
) -> np.ndarray:
    """Fine-tune and return embeddings in `oracle_ids` order.

    `negatives`, when given, is wired to the loss as an explicit hard negative per anchor (see
    `build_examples`) — the wiring MagicSpike's implementation computed and then never used.
    `negatives=None` reproduces the original in-batch-only training, used for arms 2a/2b.

    ## Why this no longer calls `model.fit()`

    `fit()` is a back-compat shim that builds a `SentenceTransformerTrainingArguments` internally
    and **accepts no seed**. Its `seed` therefore defaults to 42, and `transformers.Trainer.
    __init__` runs `set_seed(self.args.seed)` unconditionally — overwriting any `torch.manual_seed`
    set beforehand. Multi-seed training through `fit()` is not merely unseeded, it is impossible:
    every "seed" produces the same run modulo GPU kernel nondeterminism, and T4's null rule (a gap
    smaller than the pooled seed sd is a null) would have been measuring cuBLAS noise.

    Everything below transcribes the shim's recipe verbatim — AdamW at lr=2e-5 with weight decay
    0.01 except on bias/LayerNorm, WarmupLinear over `steps_per_epoch * epochs`, batch 32, 2 epochs,
    max_grad_norm 1, fp16 off — and adds exactly two arguments, `seed` and `data_seed`. Under
    `MultipleNegativesRankingLoss` those are load-bearing rather than cosmetic: the loss draws its
    negatives from the batch, so batch composition *is* the negative sampling.

    `seed` is keyword-only with no default so no caller can silently omit it.
    """
    from datasets import Dataset
    from sentence_transformers import SentenceTransformer
    from sentence_transformers.sentence_transformer import losses
    from sentence_transformers.sentence_transformer.trainer import SentenceTransformerTrainer
    from sentence_transformers.sentence_transformer.training_args import (
        BatchSamplers, MultiDatasetBatchSamplers, SentenceTransformerTrainingArguments,
    )
    import torch

    seed_everything(seed)
    model = SentenceTransformer(config.BASE_MODEL)
    model.max_seq_length = max_seq_length

    # `examples` lets a caller supply a pre-built example set — used to replay another arm's exact
    # training data while swapping the encoding, which no (positives, negatives) pair can express:
    # `neg_by_anchor` is one-negative-per-anchor, while the fallback path can hand two positives
    # sharing an anchor different negatives.
    rows = examples if examples is not None else build_examples(positives, negatives, texts_by_oid)
    if not rows:
        raise ValueError("No usable training examples — check text coverage for mined pairs.")

    # `fit()` reached this shape by draining a DataLoader into a Dataset; building it directly is
    # the same data in a deterministic order, and lets `data_seed` own the shuffling instead of a
    # DataLoader generator that no caller could reach.
    dataset = Dataset.from_dict({f"sentence_{i}": list(col) for i, col in enumerate(zip(*rows))})

    batch_size = config.TRAIN_BATCH_SIZE
    steps_per_epoch = max(1, len(dataset) // batch_size)
    num_train_steps = int(steps_per_epoch * epochs)
    # Preserved exactly from the previous call site: warmup was a tenth of the DataLoader length,
    # and a DataLoader has ceil(n / batch) batches — not floor, which `steps_per_epoch` uses.
    n_loader_batches = -(-len(dataset) // batch_size)
    warmup_steps = max(1, n_loader_batches // 10)

    args = SentenceTransformerTrainingArguments(
        # Explicit: the shim's `_default_checkpoint_dir()` walks checkpoints/model, model_1, ...
        # and would leave one stray directory per run across a 30-run grid.
        output_dir=str(output_dir) if output_dir else str(config.hf_scratch_dir()),
        batch_sampler=BatchSamplers.BATCH_SAMPLER,
        multi_dataset_batch_sampler=MultiDatasetBatchSamplers.ROUND_ROBIN,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        num_train_epochs=epochs,
        max_steps=-1,
        eval_strategy="no",
        max_grad_norm=1,
        fp16=False,
        disable_tqdm=True,
        save_strategy="no",
        report_to=[],
        seed=seed,
        data_seed=seed,
    )

    no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
    named = list(model.named_parameters())
    grouped = [
        {"params": [p for n, p in named if not any(nd in n for nd in no_decay)], "weight_decay": 0.01},
        {"params": [p for n, p in named if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
    ]
    optimizer = torch.optim.AdamW(grouped, lr=2e-5)
    scheduler = model._get_scheduler(
        optimizer, scheduler="WarmupLinear", warmup_steps=warmup_steps, t_total=num_train_steps)

    SentenceTransformerTrainer(
        model=model,
        args=args,
        train_dataset=dataset,
        loss=losses.MultipleNegativesRankingLoss(model),
        optimizers=(optimizer, scheduler),
    ).train()

    if output_dir:
        model.save(str(output_dir))

    texts = [texts_by_oid.get(oid, "") for oid in oracle_ids]
    return model.encode(
        texts, batch_size=config.BATCH_SIZE, show_progress_bar=False,
        normalize_embeddings=True, convert_to_numpy=True,
    )
