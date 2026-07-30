"""Findings output.

Every result is written twice: a `.json` for downstream code and a `.md` for the record. Both carry
the pinned input manifest and the frozen thresholds, so a number can never be read without the rule
it was judged against — that is the whole point of pre-committing thresholds.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import config, manifest, thresholds


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """Write via a sibling temp file and `os.replace`, which is atomic on POSIX and Windows.

    Matters as soon as the grid is sharded across parallel workers. `build_layer0` rewrites
    `meta.json` on every call including cache hits, so six workers reading one layer-0 directory
    are also six writers of the same small file — and a half-written `meta.json` is not a crash, it
    is a `JSONDecodeError` on the *next* run, or worse a truncated-but-parseable object missing the
    keys that carry the basis and the query fingerprint.
    """
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)
    return path


def atomic_write_parquet(df, path: Path, **kwargs) -> Path:
    """Same guarantee for the parquet caches. A torn arm-text parquet is the dangerous one: it
    reads back as a *shorter card list*, so the arm silently trains on fewer examples and nothing
    anywhere reports an error."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    df.to_parquet(tmp, **kwargs)
    os.replace(tmp, path)
    return path


def _git_rev() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(config.REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _git_dirty() -> bool | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(config.REPO_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def envelope(inputs: list[Path], run_args: dict | None = None) -> dict:
    return {
        "git_rev": _git_rev(),
        "git_dirty": _git_dirty(),
        "thresholds": thresholds.as_dict(),
        "inputs": manifest.pin(inputs),
        "run_args": run_args or {},
    }


def write(name: str, payload: dict, markdown: str, findings_dir: Path | None = None) -> tuple[Path, Path]:
    d = findings_dir or config.FINDINGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    json_path = d / f"{name}.json"
    md_path = d / f"{name}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path


def confound_header() -> str:
    return (
        "> **Standing confound — recorded, not solved.**\n> "
        + thresholds.CONFOUND_NOTE.replace(". ", ".\n> ")
        + "\n"
    )


def pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"
