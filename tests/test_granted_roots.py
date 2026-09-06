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

import ast
import json
import os
import threading
from pathlib import Path

import pytest

from systemu.runtime import granted_roots as _granted_roots_mod
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
# B1b -- the write lock (docs/CONC-MAP.md: TWO interactive CLI writers)
# --------------------------------------------------------------------------- #
#
# `grant` and `revoke` are both load-modify-REPLACE over the whole file. While the
# store had no live writer at all this was academic; with `roots grant` and `roots
# revoke` it is two operator-typed commands that can be in flight at once, and the
# CONC-MAP row for the revoke half predicted exactly this ("risk goes MED the
# moment a second writer exists"). These tests are that prediction, closed.
#
# The revoke-side test is the one that matters: an ADD lost to a race costs a
# re-grant, but a REMOVAL lost to a race leaves GRANTED a folder the operator
# revoked -- a permission that outlives the decision to withdraw it.

_WORKERS = 6
_PER_WORKER = 20


def _run_in_parallel(jobs):
    """Run every job on its own thread; return the exceptions they raised."""
    errors: list = []

    def _guard(job):
        def _run():
            try:
                job()
            except Exception as exc:            # a thread's traceback is invisible
                errors.append(exc)              # to pytest unless it is carried out
        return _run

    threads = [threading.Thread(target=_guard(j)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return errors


def _make_roots(tmp_path, worker):
    out = []
    for i in range(_PER_WORKER):
        p = tmp_path / f"root_{worker}_{i:02d}"
        p.mkdir()
        out.append(str(p))
    return out


def test_concurrent_grants_never_lose_a_root(tmp_path):
    st = GrantedRootsStore(base_dir=tmp_path / "vault")
    groups = [_make_roots(tmp_path, w) for w in range(_WORKERS)]
    expected = {canonicalize(p) for g in groups for p in g}

    def job(paths):
        return lambda: [st.grant(p) for p in paths]

    errors = _run_in_parallel([job(g) for g in groups])
    assert not errors, f"writer threads raised: {errors[:3]}"
    got = set(st.list_roots())
    missing = expected - got
    assert not missing, (
        f"{len(missing)} of {len(expected)} grants were LOST to a concurrent write. "
        f"grant() is a read-modify-write over the whole file; without the store's "
        f"lock two writers that both load before either saves silently drop one "
        f"side's rows."
    )


def test_concurrent_revokes_never_leave_a_root_granted(tmp_path):
    """The unsafe direction, and the reason the lock is load-bearing rather than
    hygiene: a lost REVOKE is a permission the operator withdrew and still has."""
    st = GrantedRootsStore(base_dir=tmp_path / "vault")
    groups = [_make_roots(tmp_path, w) for w in range(_WORKERS)]
    for g in groups:                     # seed serially: the race under test is the revoke
        for p in g:
            st.grant(p)
    assert len(st.list_roots()) == _WORKERS * _PER_WORKER

    def job(paths):
        return lambda: [st.revoke(p) for p in paths]

    errors = _run_in_parallel([job(g) for g in groups])
    assert not errors, f"writer threads raised: {errors[:3]}"
    left = st.list_roots()
    assert left == [], (
        f"{len(left)} folder(s) are STILL GRANTED after every one of them was "
        f"revoked -- a concurrent revoke lost the other's removal. This is the "
        f"failure the store's write lock exists to prevent; it is not recoverable "
        f"by re-running anything, because the operator already believes it is gone."
    )


def _store_fn(name: str) -> ast.FunctionDef:
    src = Path(_granted_roots_mod.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is not defined in granted_roots.py")


@pytest.mark.parametrize("write_path", ["grant", "revoke"])
def test_every_write_path_holds_the_exclusive_lock(write_path):
    """Structural companion to the two race tests above.

    They can only fail when a race actually loses a row, which is probabilistic;
    this cannot be lucky. A `with _exclusive(...)` around the whole load-modify-
    replace is the shape, and it must cover BOTH mutators -- a lock one writer
    skips is not a lock, it is a delay."""
    fn = _store_fn(write_path)
    held = [
        item for w in ast.walk(fn) if isinstance(w, ast.With)
        for item in w.items
        if isinstance(item.context_expr, ast.Call)
        and getattr(item.context_expr.func, "id", "") == "_exclusive"
    ]
    assert held, (
        f"{write_path}() does not hold _exclusive(...) around its read-modify-write. "
        f"Both mutators must take the store's lock or concurrent operator commands "
        f"can lose each other's rows."
    )


def test_the_lock_is_a_sidecar_file_not_the_store_itself(tmp_path):
    """The lock CANNOT be an advisory lock on `granted_roots.json`.

    The write is tmp-file + `os.replace`, so the inode a writer locked is not the
    one that ends up in place -- the next writer locks a different file and the
    exclusion is imaginary. On Windows it is worse than imaginary: an open handle
    on the destination makes `os.replace` fail outright. Hence a sidecar."""
    base = tmp_path / "vault"
    root = tmp_path / "r"
    root.mkdir()
    st = GrantedRootsStore(base_dir=base)
    st.grant(str(root))
    names = {p.name for p in base.iterdir()}
    assert "granted_roots.json" in names
    assert any(n.endswith(".lock") for n in names), sorted(names)
    # the lock file is never mistaken for state
    assert st.list_roots() == [canonicalize(str(root))]


def test_a_failed_revoke_still_releases_the_lock(tmp_path):
    """A revoke that finds nothing must not strand the lock: the very next write
    would hang. Two no-op revokes then a successful grant is the cheapest witness
    that the context manager unwinds on every path."""
    base = tmp_path / "vault"
    root = tmp_path / "r"
    root.mkdir()
    st = GrantedRootsStore(base_dir=base)
    assert st.revoke(str(root)) is False
    assert st.revoke(str(root)) is False
    assert st.grant(str(root)) == canonicalize(str(root))
    assert st.revoke(str(root)) is True


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
