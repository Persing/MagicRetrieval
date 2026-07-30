"""The parser pin must identify the parser, not the checkout that produced it.

Discovered the hard way: the first Linux run of this branch reported pin `174bc3a1...` against the
recorded `2f61bf8d...` and the cache-invalidation guard fired. Nothing about the parser had changed
— the vendored tree has mixed line endings and `core.autocrlf=true` on Windows, so the same commit
checks out as different bytes on the two platforms.

That is the dangerous kind of false alarm. A guard that cries wolf on a platform difference invites
someone to bypass it, and then it is not guarding the real case either.
"""

import hashlib

import pytest

from mr import config, representations


CRLF, LF = b"\r\n", b"\n"


def _write_parser_tree(root, newline: bytes):
    """A complete fake clause pipeline, so `parser_pin` resolves it as a usable parser."""
    root.mkdir(parents=True, exist_ok=True)
    for i, mod in enumerate(config.CLAUSE_MODULES):
        body = LF.join([b"# module " + mod.encode(), b"def f():", b"    return " + str(i).encode()])
        root.joinpath(mod).write_bytes(body.replace(LF, newline))
    return root


def test_pin_is_identical_for_crlf_and_lf_checkouts(tmp_path, monkeypatch):
    """The property that matters: two checkouts of the same source differing only in line endings
    are the same parser and must pin the same. Python does not care about the carriage returns, so
    neither can the identity."""
    crlf_tree = _write_parser_tree(tmp_path / "win", CRLF)
    lf_tree = _write_parser_tree(tmp_path / "nix", LF)

    # the trees really do differ on disk — otherwise this test proves nothing
    a = crlf_tree.joinpath(config.CLAUSE_MODULES[0]).read_bytes()
    b = lf_tree.joinpath(config.CLAUSE_MODULES[0]).read_bytes()
    assert a != b, "fixture is broken: the two trees are byte-identical"
    assert hashlib.sha256(a).hexdigest() != hashlib.sha256(b).hexdigest()

    monkeypatch.setattr(config, "CDL_PARSER_PATH", str(crlf_tree))
    win = representations.parser_pin()
    monkeypatch.setattr(config, "CDL_PARSER_PATH", str(lf_tree))
    nix = representations.parser_pin()

    assert win["combined_sha256"] == nix["combined_sha256"]
    assert win["module_sha256"] == nix["module_sha256"]


def test_pin_still_changes_when_the_source_actually_changes(tmp_path, monkeypatch):
    """Normalizing line endings must not blunt the guard. A one-character edit still has to move the
    pin — that is the case it exists for."""
    tree = _write_parser_tree(tmp_path / "p", LF)
    monkeypatch.setattr(config, "CDL_PARSER_PATH", str(tree))
    before = representations.parser_pin()["combined_sha256"]

    target = tree / config.CLAUSE_MODULES[0]
    target.write_bytes(target.read_bytes().replace(b"return 0", b"return 1"))
    after = representations.parser_pin()["combined_sha256"]

    assert before != after


def test_pin_covers_every_clause_module(tmp_path, monkeypatch):
    """A directory holding only `parser.py` is a usable CDL parser and a broken clause pipeline, and
    the difference is invisible until arm B dies at import. The pin has to span the whole set."""
    tree = _write_parser_tree(tmp_path / "p", LF)
    monkeypatch.setattr(config, "CDL_PARSER_PATH", str(tree))
    assert set(representations.parser_pin()["module_sha256"]) == set(config.CLAUSE_MODULES)


def test_missing_parser_is_an_error_not_an_empty_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CDL_PARSER_PATH", None)
    with pytest.raises(Exception):
        representations.parser_pin()
