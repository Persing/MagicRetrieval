"""Pin the MagicSpike artifacts a run consumed.

MagicSpike is not version-controlled, so a findings file that just names its inputs is not
reproducible — the input can change underneath it silently. Every findings JSON embeds a manifest
so a result can be tied to the exact bytes it was computed from.

Large files get size+mtime; anything under HASH_LIMIT_BYTES also gets a sha256.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

HASH_LIMIT_BYTES = 64 * 1024 * 1024  # 64 MB


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def pin(paths: Iterable[Path]) -> dict:
    """Return {path_str: {exists, bytes, mtime, sha256?}} for each input."""
    out: dict[str, dict] = {}
    for p in paths:
        p = Path(p)
        if not p.exists():
            out[str(p)] = {"exists": False}
            continue
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.is_file())
            out[str(p)] = {
                "exists": True,
                "is_dir": True,
                "file_count": len(files),
                "bytes": sum(f.stat().st_size for f in files),
            }
            continue
        st = p.stat()
        entry: dict = {"exists": True, "bytes": st.st_size, "mtime": int(st.st_mtime)}
        if st.st_size <= HASH_LIMIT_BYTES:
            entry["sha256"] = _sha256(p)
        out[str(p)] = entry
    return out
