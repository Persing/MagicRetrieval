"""Paths and run-wide constants.

MagicSpike artifacts are read-only inputs. Nothing in this package writes to that tree — it is a
separate investigation with its own finding record, and it is not under version control, so a
stray write there would be unrecoverable.

**Vendored copies.** `vendor/magicspike_data/` and `vendor/pile_of_cards_parser/` hold committed
copies of the small subset of external data/code this package actually reads (~75MB total — the
full MagicSpike tree is 1.5GB and the CDL parser has its own git history, so vendoring the whole
thing isn't practical, but this repo's actual dependency footprint is small). Every path below
checks the vendored copy first and falls back to a sibling checkout — so a fresh `git clone` of
just this repo (e.g. onto another machine, for GPU access) runs T0/T1/T2 with no other setup,
while a working copy that sits next to a real MagicSpike/PileOfCardsParser checkout (this
machine, during active development) keeps reading the live sibling tree unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
FINDINGS_DIR = REPO_ROOT / "findings"
RUNS_DIR = REPO_ROOT / "runs"
VENDOR_DIR = REPO_ROOT / "vendor"


def _resolve(vendored: Path, sibling: Path) -> Path:
    """Vendored copy wins if present; otherwise the sibling checkout (may not exist either —
    callers get a clear file-not-found rather than a silent empty read either way)."""
    return vendored if vendored.exists() else sibling


# ── MagicSpike (read-only) ────────────────────────────────────────────────────
SPIKE_ROOT = Path(os.environ.get("MAGICSPIKE_ROOT", REPO_ROOT.parent / "MagicSpike"))
SPIKE_ARTIFACTS = SPIKE_ROOT / "artifacts"
_VENDOR_SPIKE = VENDOR_DIR / "magicspike_data"

CARDS_PARQUET = _resolve(_VENDOR_SPIKE / "cards.parquet", SPIKE_ARTIFACTS / "cards.parquet")
CLUSTER_ASSIGNMENTS = _resolve(
    _VENDOR_SPIKE / "cluster_assignments.parquet", SPIKE_ARTIFACTS / "cluster_assignments.parquet"
)
SCRYFALL_ORACLE = _resolve(
    _VENDOR_SPIKE / "scryfall_oracle_cards.jsonl.gz", SPIKE_ARTIFACTS / "scryfall_oracle_cards.jsonl.gz"
)
SCRYFALL_TAGS = _resolve(
    _VENDOR_SPIKE / "scryfall_oracle_tags.jsonl.gz", SPIKE_ARTIFACTS / "scryfall_oracle_tags.jsonl.gz"
)

# Corpora
CENSUS_JSONL = _resolve(
    _VENDOR_SPIKE / "archidekt_census_popular.jsonl", SPIKE_ROOT / "archidekt_census_popular.jsonl"
)  # 5,000 casual decks
DECKS_COMBINED = _resolve(
    _VENDOR_SPIKE / "decks_combined.parquet", SPIKE_ARTIFACTS / "decks_combined.parquet"
)  # cEDH + precon
PRECON_BACKUP = _resolve(_VENDOR_SPIKE / "backup_precon_only", SPIKE_ARTIFACTS / "backup_precon_only")
# ^ only decks_precon.parquet is vendored here (not the mislabeled PPMI/card_index — see
# t2_leakage.py's run_arm docstring), matching what corpus.load_precon() actually reads.

CORPORA = ("casual", "cedh", "precon")

# The prior fine-tune's text encoding, reproduced exactly so arm 2a is comparable.
# See MagicSpike src/analysis/embeddings.py::_card_text — type line + oracle text, no card name.
BASE_MODEL = "all-MiniLM-L6-v2"
TEXT_SEP = " [SEP] "
FINETUNE_EPOCHS = 2
BATCH_SIZE = 256
TRAIN_BATCH_SIZE = 32
SEED = 42

# ── CDL parser (external, required for T0/T1) ─────────────────────────────────
# Deterministic rules/pattern parser: parser.encode_card(card, store) -> CDL source text.
# Vendored copy wins; falls back to a sibling checkout; MR_CDL_PARSER overrides both.
# T2 does not use the parser at all and runs without it.
CDL_PARSER_REPO = "github.com/Persing/PileOfCardsParser"
_VENDOR_PARSER = VENDOR_DIR / "pile_of_cards_parser"
CDL_PARSER_PATH = os.environ.get(
    "MR_CDL_PARSER", str(_resolve(_VENDOR_PARSER, REPO_ROOT.parent / "PileOfCardsParser"))
)
