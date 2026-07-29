"""Layer-1 representation precompute: CDL parse + clause pipeline output, cached to disk.

This is the raw material every T4 arm draws from. It is deliberately **arm-independent and
corpus-independent** — computed once over the whole Commander-legal card pool, then reused
by all six arms and every seed. That is not just a speed decision: it is what makes "all arms
differ only in how a card becomes a vector" true by construction rather than by hope. An arm
cannot perturb what it never computes.

Two tables, two different join paths, for a reason:

  `cdl_table`    joins to the **Scryfall oracle snapshot**, because `scryfall_import.should_include`
                 inspects layout/digital/legality fields that `cards.parquet` does not carry. This
                 reproduces `t0_coverage.parse_all`'s join exactly, so T4's parse-status stratum and
                 T0's coverage census are the same measurement.

  `clause_table` runs straight off **cards.parquet**. `clause_pipeline.process_card` needs only
                 oracle_text, name, and is_spell, all present there. Driving it from the parser
                 repo's own `scryfall_corpus_full.json` instead — the obvious move, since that is
                 what `corpus_harness.iter_pipeline_output` does — requires a name join that lands
                 29,000/31,041 (93.4%), and the 804 missing DFC names have *no* front-face fallback
                 because that corpus contains no DFC faces at all. That would hand arms
                 B/B_type/B+/C a ~2,000-card hole arm A does not have: a straight confound in the
                 exact comparison T4 exists to make.

Cache invalidation is keyed on `parser_pin()` — the sha256 of every clause/parser module actually
resolved. Refreshing the vendored parser silently reusing a stale clause table is precisely the
failure this whole module is meant to prevent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd

from . import cdl_adapter, config, corpus

REPR_DIR = config.RUNS_DIR / "t4" / "repr"
CDL_PATH = REPR_DIR / "cdl.parquet"
CLAUSES_PATH = REPR_DIR / "clauses.jsonl.gz"
SIGFREQ_PATH = REPR_DIR / "signature_freq.parquet"
PIN_PATH = REPR_DIR / "parser_pin.json"


# ── provenance ────────────────────────────────────────────────────────────────

def parser_pin() -> dict:
    """Identity of the parser + clause pipeline actually in use.

    The per-module sha256 is the real identifier: the clause pipeline's source tree carries
    uncommitted work, so a git rev alone would name the wrong thing. The rev is recorded
    alongside as a recovery hint, with its dirty flag, never as the identity.
    """
    root = Path(config.CDL_PARSER_PATH) if config.CDL_PARSER_PATH else None
    if root is None:
        raise cdl_adapter.ParserUnavailable(config.parser_gaps())
    digests = {}
    for mod in config.CLAUSE_MODULES:
        digests[mod] = hashlib.sha256((root / mod).read_bytes()).hexdigest()
    combined = hashlib.sha256(
        "".join(f"{k}:{digests[k]}" for k in sorted(digests)).encode()
    ).hexdigest()

    # Two different repos can be involved and conflating them is actively misleading. When the
    # resolved path is the vendored copy, `git -C <path>` reports **MagicRetrieval's** rev, not
    # the parser's — so it is labelled as the containing repo, and the upstream parser checkout
    # is pinned separately when it is present on this machine.
    upstream = config.REPO_ROOT.parent / "PileOfCardsParser"
    return {
        "path": str(root),
        "module_sha256": digests,
        "combined_sha256": combined,
        "containing_repo": _git(root, "rev-parse", "--show-toplevel"),
        "containing_repo_rev": _git(root, "rev-parse", "--short", "HEAD"),
        "containing_repo_dirty": bool(_git(root, "status", "--porcelain")),
        "upstream_parser_rev": _git(upstream, "rev-parse", "--short", "HEAD") if upstream.exists() else None,
        "upstream_parser_dirty": bool(_git(upstream, "status", "--porcelain")) if upstream.exists() else None,
    }


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


# ── card-type context the pipeline cannot derive from oracle text ─────────────

def is_spell(type_line: str | None) -> bool:
    """Ported from `corpus_harness.is_spell`, retargeted from that repo's bracketed `types`
    string to `cards.parquet`'s `type_line`. Genuine external context: a bare line with no
    cost-colon/trigger/loyalty pattern is a STATIC ability on a permanent and a one-shot SPELL
    effect on an instant/sorcery, and oracle text alone cannot tell them apart."""
    t = type_line or ""
    return "Instant" in t or "Sorcery" in t


def clause_signature(clause: dict) -> tuple:
    """(type, tag-key-set, value role+kind set) — the stratification key.

    Ported verbatim in behaviour from `corpus_harness.clause_signature` rather than imported,
    so `strata.py` and the evaluator never need the parser tree on sys.path. Deliberately
    ignores tag VALUES: it groups clauses by the SHAPE of what was extracted.
    """
    tags = clause.get("tags") or {}
    values = clause.get("values") or []
    return (
        clause.get("type"),
        tuple(sorted(tags.keys())),
        tuple(sorted({(v.get("role"), v.get("kind")) for v in values})),
    )


def signature_key(sig: tuple) -> str:
    """Flat string form, for parquet keys and JSON."""
    ctype, tagkeys, rolekinds = sig
    rk = ",".join(f"{r}:{k}" for r, k in rolekinds)
    return f"{ctype}|{','.join(tagkeys)}|{rk}"


# ── layer-1 builders ──────────────────────────────────────────────────────────

def build_cdl_table(cards_df: pd.DataFrame, oracle_cards: dict[str, dict]) -> pd.DataFrame:
    """One CDL parse per distinct card. Same join and same status taxonomy as
    `t0_coverage.parse_all`, plus the CDL text itself (arms C/D need it) and the construct
    set (secondary unusualness cross-check).

    Never raises on a parse failure — that IS the data. `status` is one of
    clean / gap / crash / excluded, and all four are kept distinct: folding crash into
    excluded misattributes a parser bug as a deliberate scope decision.
    """
    rows = []
    for row in cards_df.drop_duplicates("oracle_id").itertuples(index=False):
        card = oracle_cards.get(row.oracle_id)
        if card is None:
            rows.append({
                "oracle_id": row.oracle_id, "name": row.name, "status": cdl_adapter.STATUS_EXCLUDED,
                "cdl": None, "n_gaps": 0, "gap_category": None,
                "exclusion_reason": "not in oracle snapshot", "error": None, "constructs": "[]",
            })
            continue
        res = cdl_adapter.parse(card)
        first_gap = (res.gaps or res.unknown_keywords or [None])[0]
        rows.append({
            "oracle_id": row.oracle_id,
            "name": row.name,
            "status": res.status,
            "cdl": res.cdl,
            "n_gaps": len(res.gaps) + len(res.unknown_keywords),
            "gap_category": cdl_adapter.gap_category(first_gap) if first_gap else None,
            "exclusion_reason": res.exclusion_reason,
            "error": res.error,
            "constructs": json.dumps(sorted(cdl_adapter.constructs(res.cdl))) if res.cdl else "[]",
        })
    return pd.DataFrame(rows)


def build_clause_table(cards_df: pd.DataFrame) -> tuple[dict[str, list[dict]], dict]:
    """{oracle_id: [clause dicts]} for every card with oracle text, plus coverage stats.

    A pipeline crash is recorded, never swallowed: `stats["errors"]` names the card and the
    exception. A card that crashes gets an empty clause list, which its arm serializations must
    then handle explicitly rather than silently emitting a bare head.
    """
    cdl_adapter.ensure()  # puts the resolved parser tree on sys.path
    from clause_pipeline import process_card

    out: dict[str, list[dict]] = {}
    errors: list[dict] = []
    n_no_text = 0
    for row in cards_df.drop_duplicates("oracle_id").itertuples(index=False):
        text = row.oracle_text
        if not isinstance(text, str) or not text.strip():
            n_no_text += 1
            out[row.oracle_id] = []
            continue
        try:
            out[row.oracle_id] = process_card(
                text, is_spell=is_spell(row.type_line), card_name=row.name
            )
        except Exception as exc:  # noqa: BLE001 — a crash is a finding, not a stop
            errors.append({"oracle_id": row.oracle_id, "name": row.name,
                           "error": f"{type(exc).__name__}: {exc}"})
            out[row.oracle_id] = []
    stats = {
        "n_cards": len(out),
        "n_no_oracle_text": n_no_text,
        "n_errors": len(errors),
        "errors": errors[:50],
        "n_clauses": sum(len(v) for v in out.values()),
    }
    return out, stats


def build_signature_freq(clause_table: dict[str, list[dict]]) -> pd.DataFrame:
    """Corpus frequency of every distinct clause signature. Basis for stratification 3."""
    counts: dict[str, int] = {}
    for clauses in clause_table.values():
        for cl in clauses:
            k = signature_key(clause_signature(cl))
            counts[k] = counts.get(k, 0) + 1
    return (pd.DataFrame({"signature": list(counts), "count": list(counts.values())})
            .sort_values("count", ascending=False).reset_index(drop=True))


# ── cache ─────────────────────────────────────────────────────────────────────

def _write_clauses(path: Path, table: dict[str, list[dict]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for oid, clauses in table.items():
            fh.write(json.dumps({"oracle_id": oid, "clauses": clauses}) + "\n")


def _read_clauses(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            out[rec["oracle_id"]] = rec["clauses"]
    return out


def load(cards_df: pd.DataFrame | None = None, oracle_cards: dict[str, dict] | None = None,
         force: bool = False) -> dict:
    """Build (or reuse) the layer-1 tables. Returns {cdl, clauses, signature_freq, pin, stats}.

    The cache is invalidated whenever `parser_pin()["combined_sha256"]` changes. Reusing a clause
    table built by a different parser build is the exact silent-staleness failure that motivated
    capability-based parser resolution in the first place — so it is checked, not assumed.
    """
    pin = parser_pin()
    cached_pin = json.loads(PIN_PATH.read_text(encoding="utf-8")) if PIN_PATH.exists() else None
    fresh = (
        not force
        and cached_pin is not None
        and cached_pin.get("combined_sha256") == pin["combined_sha256"]
        and CDL_PATH.exists() and CLAUSES_PATH.exists() and SIGFREQ_PATH.exists()
    )
    if fresh:
        return {
            "cdl": pd.read_parquet(CDL_PATH),
            "clauses": _read_clauses(CLAUSES_PATH),
            "signature_freq": pd.read_parquet(SIGFREQ_PATH),
            "pin": cached_pin,
            "stats": cached_pin.get("stats", {}),
            "from_cache": True,
        }

    cards_df = cards_df if cards_df is not None else corpus.load_cards_parquet()
    oracle_cards = oracle_cards if oracle_cards is not None else corpus.load_oracle_cards()

    cdl = build_cdl_table(cards_df, oracle_cards)
    clauses, clause_stats = build_clause_table(cards_df)
    sigfreq = build_signature_freq(clauses)

    REPR_DIR.mkdir(parents=True, exist_ok=True)
    cdl.to_parquet(CDL_PATH, index=False)
    _write_clauses(CLAUSES_PATH, clauses)
    sigfreq.to_parquet(SIGFREQ_PATH, index=False)

    stats = {
        "clause_pipeline": clause_stats,
        "cdl_status_counts": cdl["status"].value_counts().to_dict(),
        "n_distinct_signatures": int(len(sigfreq)),
    }
    PIN_PATH.write_text(json.dumps({**pin, "stats": stats}, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    return {"cdl": cdl, "clauses": clauses, "signature_freq": sigfreq,
            "pin": pin, "stats": stats, "from_cache": False}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="T4 layer-1 representation precompute")
    ap.add_argument("--force", action="store_true", help="rebuild even if the cache is fresh")
    args = ap.parse_args()

    res = load(force=args.force)
    print(f"parser: {res['pin']['path']}")
    print(f"  combined sha256: {res['pin']['combined_sha256'][:16]}  "
          f"upstream={res['pin']['upstream_parser_rev']} dirty={res['pin']['upstream_parser_dirty']}")
    print(f"from_cache: {res['from_cache']}")
    print(f"CDL status: {res['stats'].get('cdl_status_counts')}")
    cp = res["stats"].get("clause_pipeline", {})
    print(f"clauses: {cp.get('n_clauses'):,} over {cp.get('n_cards'):,} cards  "
          f"(no-text {cp.get('n_no_oracle_text')}, errors {cp.get('n_errors')})")
    print(f"distinct signatures: {res['stats'].get('n_distinct_signatures'):,}")
