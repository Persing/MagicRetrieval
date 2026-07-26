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

from . import config


def card_text(oracle_text: str | None, type_line: str | None) -> str:
    parts = [p for p in [str(type_line or ""), str(oracle_text or "")] if p]
    return config.TEXT_SEP.join(parts)


def text_lookup(cards_df: pd.DataFrame) -> dict[str, str]:
    return {
        row.oracle_id: card_text(row.oracle_text, row.type_line)
        for row in cards_df.itertuples(index=False)
    }


def zero_shot_embeddings(oracle_ids: list[str], texts_by_oid: dict[str, str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(config.BASE_MODEL)
    texts = [texts_by_oid.get(oid, "") for oid in oracle_ids]
    return model.encode(
        texts, batch_size=config.BATCH_SIZE, show_progress_bar=False,
        normalize_embeddings=True, convert_to_numpy=True,
    )


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
    if not negatives:
        return [
            (ta, tp) for a, p in positives
            if (ta := texts_by_oid.get(a, "")) and (tp := texts_by_oid.get(p, ""))
        ]

    neg_by_anchor: dict[str, str] = {}
    for na, nb in negatives:
        neg_by_anchor.setdefault(na, nb)
    fallback_pool = list(negatives)

    examples = []
    for i, (a, p) in enumerate(positives):
        ta, tp = texts_by_oid.get(a, ""), texts_by_oid.get(p, "")
        if not (ta and tp):
            continue
        neg_partner = neg_by_anchor.get(a)
        if neg_partner is None:
            # No mined negative for this specific anchor — fall back to cycling the pool rather
            # than dropping the example; still same-colour/non-co-occurring overall, just not
            # guaranteed hard for THIS anchor.
            neg_partner = fallback_pool[i % len(fallback_pool)][1]
        tn = texts_by_oid.get(neg_partner, "")
        if tn:
            examples.append((ta, tp, tn))
    return examples


def finetune(
    positives: list[tuple[str, str]],
    negatives: list[tuple[str, str]] | None,
    texts_by_oid: dict[str, str],
    oracle_ids: list[str],
    output_dir: Path | None = None,
    epochs: int = config.FINETUNE_EPOCHS,
) -> np.ndarray:
    """Fine-tune and return embeddings in `oracle_ids` order.

    `negatives`, when given, is wired to the loss as an explicit hard negative per anchor (see
    `build_examples`) — the wiring MagicSpike's implementation computed and then never used.
    `negatives=None` reproduces the original in-batch-only training, used for arms 2a/2b.
    """
    from sentence_transformers import SentenceTransformer, InputExample, losses
    from torch.utils.data import DataLoader

    model = SentenceTransformer(config.BASE_MODEL)

    examples = [InputExample(texts=list(t)) for t in build_examples(positives, negatives, texts_by_oid)]
    if not examples:
        raise ValueError("No usable training examples — check text coverage for mined pairs.")

    loader = DataLoader(examples, shuffle=True, batch_size=config.TRAIN_BATCH_SIZE)
    loss = losses.MultipleNegativesRankingLoss(model)
    model.fit(
        train_objectives=[(loader, loss)],
        epochs=epochs,
        warmup_steps=max(1, len(loader) // 10),
        show_progress_bar=False,
        output_path=str(output_dir) if output_dir else None,
    )

    texts = [texts_by_oid.get(oid, "") for oid in oracle_ids]
    return model.encode(
        texts, batch_size=config.BATCH_SIZE, show_progress_bar=False,
        normalize_embeddings=True, convert_to_numpy=True,
    )
