"""G2 — the GrantedRoots grant + realpath-confinement store (spec UNIFIED-v2 §5.4 / §13, HIGH-3).

A NET-NEW filesystem confinement primitive: the operator grants directories, and
every resolved path the agent touches must be confined WITHIN a granted root
(after canonicalization) — a path outside is rejected even if the request names
it absolutely. This is the substrate the `access` requirement type (§5.3), the
Reference Resolver confinement (§5.4), and the Situational-Inventory root survey
(§5.1) all rest on.

Confinement canonicalizes the FINAL path (IMPL-9): `..`/symlink/junction chains
followed to the end, case-folded on NTFS, 8.3 short-name aliases expanded — so a
prefix check on the raw string is never the boundary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from systemu.runtime.granted_roots import GrantedRootsStore, canonicalize


def _store(tmp_path) -> GrantedRootsStore:
    return GrantedRootsStore(base_dir=tmp_path / "vault")


# --------------------------------------------------------------------------- #
# grant / confinement
# --------------------------------------------------------------------------- #

def test_grant_and_confinement(tmp_path):
    root = tmp_path / "Documents"
    (root / "sub").mkdir(parents=True)
    inside = root / "sub" / "bills.pdf"
    inside.write_text("x")
    outside = tmp_path / "Secrets" / "passwords.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("x")

    st = _store(tmp_path)
    st.grant(str(root))
    assert st.is_within_granted(str(inside)) is True
    assert st.is_within_granted(str(outside)) is False


def test_ungranted_is_rejected(tmp_path):
    st = _store(tmp_path)
    # nothing granted → everything is outside
    assert st.is_within_granted(str(tmp_path / "anything.txt")) is False


def test_root_itself_is_within(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    st = _store(tmp_path)
    st.grant(str(root))
    assert st.is_within_granted(str(root)) is True


def test_prefix_collision_not_confined(tmp_path):
    # `/granted-evil` must NOT count as within `/granted` (string-prefix trap)
    granted = tmp_path / "granted"
    evil = tmp_path / "granted-evil"
    granted.mkdir(); evil.mkdir()
    st = _store(tmp_path)
    st.grant(str(granted))
    assert st.is_within_granted(str(evil / "x.txt")) is False


def test_dotdot_escape_rejected(tmp_path):
    # a path that escapes the root via .. is resolved and rejected (canonicalization,
    # not the raw string, is the boundary)
    root = tmp_path / "root"
    (root).mkdir()
    st = _store(tmp_path)
    st.grant(str(root))
    escape = str(root / ".." / "elsewhere" / "x.txt")
    assert st.is_within_granted(escape) is False


def test_absolute_outside_path_rejected_even_if_named(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    st = _store(tmp_path)
    st.grant(str(root))
    # a fully-qualified absolute path outside the grant is rejected
    assert st.is_within_granted(r"C:\Windows\System32\drivers\etc\hosts"
                                if os.name == "nt" else "/etc/passwd") is False


@pytest.mark.skipif(os.name != "nt", reason="NTFS case-fold semantics")
def test_case_insensitive_on_windows(tmp_path):
    root = tmp_path / "Documents"; root.mkdir()
    st = _store(tmp_path)
    st.grant(str(root))
    # a differently-cased path to the same location is confined (case-fold)
    weird = str(root).upper() + "\\Bills.PDF"
    assert st.is_within_granted(weird) is True


def test_symlink_escape_rejected_if_supported(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    target = tmp_path / "outside"; target.mkdir()
    link = root / "escape"
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")
    st = _store(tmp_path)
    st.grant(str(root))
    # a file reached THROUGH a symlink that points outside is rejected (final-path)
    assert st.is_within_granted(str(link / "x.txt")) is False


# --------------------------------------------------------------------------- #
# grant lifecycle + persistence
# --------------------------------------------------------------------------- #

def test_revoke(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    st = _store(tmp_path)
    st.grant(str(root))
    assert st.is_within_granted(str(root / "x")) is True
    assert st.revoke(str(root)) is True
    assert st.is_within_granted(str(root / "x")) is False
    assert st.revoke(str(root)) is False  # idempotent


def test_persistence_across_instances(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    _store(tmp_path).grant(str(root))
    # a fresh store instance reads the persisted grant
    st2 = _store(tmp_path)
    assert st2.is_within_granted(str(root / "x")) is True
    assert len(st2.list_roots()) == 1


def test_grant_is_idempotent_and_canonical(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    st = _store(tmp_path)
    st.grant(str(root))
    st.grant(str(root) + os.sep)      # trailing sep — same root
    st.grant(str(root / "." ))        # dot — same root
    assert len(st.list_roots()) == 1


def test_defensive_on_broken_store(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir(parents=True)
    (vault / "granted_roots.json").write_text("not json {", encoding="utf-8")
    st = GrantedRootsStore(base_dir=vault)
    assert st.list_roots() == []               # never raises
    assert st.is_within_granted(str(tmp_path)) is False


# --------------------------------------------------------------------------- #
# canonicalize() helper
# --------------------------------------------------------------------------- #

def test_canonicalize_idempotent_and_dotdot(tmp_path):
    p = tmp_path / "a" / "b"
    p.mkdir(parents=True)
    c1 = canonicalize(str(p / ".." / "b"))
    c2 = canonicalize(c1)
    assert c1 == c2
    assert canonicalize(str(p)) == c1          # .. resolved to the same place


def test_canonicalize_handles_short_name_pattern_gracefully(tmp_path):
    # a PROGRA~1-style component must not crash canonicalization (expanded where it
    # exists, passed through where it doesn't) — no exception is the contract here
    weird = str(tmp_path / "PROGRA~1" / "x")
    out = canonicalize(weird)
    assert isinstance(out, str) and out
