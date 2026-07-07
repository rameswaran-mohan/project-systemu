"""R-A9 T5: S3 granted-roots builder — the BOUNDED, escape-re-gated survey.

The two hard requirements are the whole point of this task:
  - AC4: the scan is BOUNDED — top-N most-recent files per root, NOT a full
    recursive crawl. A huge tree must never blow the survey.
  - Confinement: every emitted file's canonical path MUST pass is_within_granted
    (defense-in-depth against a symlinked dir / `..` escaping the root). Symlinks
    are skipped outright.
"""
from __future__ import annotations

import os

import pytest

from systemu.runtime import situational_inventory
from systemu.runtime.granted_roots import GrantedRootsStore
from systemu.runtime.situational_inventory import (
    FileHandleLite,
    RootSurvey,
    build_roots,
    root_freshness_stamp,
    _MAX_SALIENT_PER_ROOT,
)


def _store(tmp_path) -> GrantedRootsStore:
    return GrantedRootsStore(base_dir=tmp_path / "vault")


# --------------------------------------------------------------------------- #
# AC4 — the scan is BOUNDED to top-N most-recent, not a full crawl
# --------------------------------------------------------------------------- #

def test_build_roots_bounds_to_top_n_newest_first(tmp_path):
    root = tmp_path / "Documents"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    # 200 files across two subdirs; stamp increasing mtimes so "newest" is knowable.
    created = []
    base = 1_000_000.0
    for i in range(200):
        sub = "a" if i % 2 == 0 else "b"
        f = root / sub / f"f{i:03d}.txt"
        f.write_text(str(i))
        mt = base + i          # later i == newer
        os.utime(f, (mt, mt))
        created.append((f, mt))

    st = _store(tmp_path)
    st.grant(str(root))

    surveys = build_roots(st)
    assert len(surveys) == 1
    surv = surveys[0]
    assert isinstance(surv, RootSurvey)

    # AC4: bounded to top-N, never the full 200.
    assert len(surv.salient) <= _MAX_SALIENT_PER_ROOT
    assert len(surv.salient) == _MAX_SALIENT_PER_ROOT

    # newest-first: mtimes descending, and they are the newest files created.
    mtimes = [fh.mtime for fh in surv.salient]
    assert mtimes == sorted(mtimes, reverse=True)
    newest_expected = sorted(m for _f, m in created)[-_MAX_SALIENT_PER_ROOT:]
    assert set(mtimes) == set(newest_expected)

    # each is a proper FileHandleLite with the untrusted taint axis.
    for fh in surv.salient:
        assert isinstance(fh, FileHandleLite)
        assert fh.origin_class == "content_derived"
        assert fh.source_kind == "file"
        assert fh.name and fh.path


def test_build_roots_survey_taint_axis(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    (root / "note.md").write_text("hi")
    st = _store(tmp_path)
    st.grant(str(root))
    surv = build_roots(st)[0]
    assert surv.origin_class == "operator"        # operator-granted container
    assert surv.source_kind == "granted_root"


def test_scan_traversal_cap_truncates(tmp_path, monkeypatch):
    """AC4 traversal-cap regression: with a tiny _MAX_SCAN_ENTRIES the walk STOPS
    early — it does NOT crawl the whole tree just to slice its output. Exercises the
    early-`return found` cap path in _scan_root_bounded."""
    monkeypatch.setattr(situational_inventory, "_MAX_SCAN_ENTRIES", 10)
    root = tmp_path / "Big"
    for s in range(5):
        sub = root / f"sub{s}"
        sub.mkdir(parents=True)
        for i in range(10):           # ~50 files across subdirs
            (sub / f"f{s}_{i}.txt").write_text("x")

    # the bounded scanner examines at most the cap; it must NOT return all ~50 files.
    candidates = situational_inventory._scan_root_bounded(str(root))
    assert len(candidates) < 50       # truncated by the traversal cap
    assert len(candidates) <= 10      # never more files than entries examined

    st = _store(tmp_path)
    st.grant(str(root))
    surv = build_roots(st)[0]
    # the survey is still bounded and does not surface all 50.
    assert len(surv.salient) < 50


def test_bfs_surfaces_recent_shallow_file(tmp_path, monkeypatch):
    """Fix 1 regression: a RECENT file in a SHALLOW sibling branch must survive even
    when a wide/deep sibling of OLD files would exhaust the traversal cap first.

    Layout (NTFS scandir is alphabetical → the root's two subdirs enqueue as
    [AAA_recent, zzz_old]):
      - a LIFO/DFS walk pops the LAST-enqueued dir (`zzz_old`) first, descends its
        huge OLD subtree and burns the cap before ever popping `AAA_recent` →
        RECENT.txt is never examined (FAILS).
      - a FIFO/BFS walk pops `AAA_recent` first → RECENT.txt is examined and kept
        before the cap trips on the OLD subtree (PASSES).
    Verified: reverting the walk to `queue.pop()` (DFS) makes this assertion fail.
    """
    monkeypatch.setattr(situational_inventory, "_MAX_SCAN_ENTRIES", 30)
    root = tmp_path / "Tree"
    root.mkdir()

    # SHALLOW sibling (alphabetically first) holding ONE genuinely-recent file.
    recent_dir = root / "AAA_recent"
    recent_dir.mkdir()
    recent = recent_dir / "RECENT.txt"
    recent.write_text("recent")
    newer = 9_000_000.0
    os.utime(recent, (newer, newer))

    # WIDE/DEEP sibling (alphabetically last) loaded with many OLD files — enough
    # to trip the cap when descended first, as a DFS would.
    old = 1_000_000.0
    deep = root / "zzz_old" / "B" / "C"
    deep.mkdir(parents=True)
    for i in range(80):
        f = deep / f"old{i:03d}.txt"
        f.write_text("old")
        os.utime(f, (old, old))

    st = _store(tmp_path)
    st.grant(str(root))
    surv = build_roots(st)[0]

    names = {fh.name for fh in surv.salient}
    # BFS examines the shallow sibling before descending the OLD subtree → RECENT kept.
    assert "RECENT.txt" in names


# --------------------------------------------------------------------------- #
# Confinement — a symlink/`..` escape is NEVER emitted (the point)
# --------------------------------------------------------------------------- #

def _symlinks_supported(tmp_path) -> bool:
    probe_t = tmp_path / "_probe_target"
    probe_t.write_text("x")
    link = tmp_path / "_probe_link"
    try:
        os.symlink(str(probe_t), str(link))
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        try:
            if link.exists() or link.is_symlink():
                os.unlink(str(link))
        except OSError:
            pass
    return True


def test_symlink_escape_never_emitted(tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlink creation not permitted on this platform")

    root = tmp_path / "Granted"
    root.mkdir()
    (root / "legit.txt").write_text("inside")

    # a secret file OUTSIDE the granted root...
    outside = tmp_path / "Secrets" / "passwords.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("TOPSECRET")
    # ...made "newest" so a naive scan would surface it first.
    newer = 9_000_000.0
    os.utime(outside, (newer, newer))

    # a symlink INSIDE the root pointing at the outside secret.
    link = root / "escape_link.txt"
    os.symlink(str(outside), str(link))

    st = _store(tmp_path)
    st.grant(str(root))
    surv = build_roots(st)[0]

    emitted = {fh.path for fh in surv.salient}
    # neither the symlink itself nor the outside target is ever emitted.
    assert str(outside) not in emitted
    assert not any("passwords.txt" in p for p in emitted)
    assert not any("escape_link.txt" in p for p in emitted)
    # the legit inside file IS surveyed.
    assert any("legit.txt" in p for p in emitted)
    # every emitted path is confined.
    for fh in surv.salient:
        assert st.is_within_granted(fh.path)


def test_symlinked_dir_not_descended(tmp_path):
    """Fix 2: a symlinked DIRECTORY inside the root pointing OUTSIDE must not be
    descended (skipped via is_symlink) and none of its contents may appear."""
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlink creation not permitted on this platform")

    root = tmp_path / "Granted"
    root.mkdir()
    (root / "legit.txt").write_text("inside")

    # an OUTSIDE directory with a "newest" secret file.
    outside_dir = tmp_path / "Outside"
    outside_dir.mkdir()
    secret = outside_dir / "secret.txt"
    secret.write_text("TOPSECRET")
    newer = 9_000_000.0
    os.utime(secret, (newer, newer))

    # a symlinked DIRECTORY inside the root pointing at the outside dir.
    link_dir = root / "linked_dir"
    try:
        os.symlink(str(outside_dir), str(link_dir), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("directory symlink creation not permitted")

    st = _store(tmp_path)
    st.grant(str(root))
    surv = build_roots(st)[0]

    emitted = {fh.path for fh in surv.salient}
    assert not any("secret.txt" in p for p in emitted)   # outside content never surfaced
    assert any("legit.txt" in p for p in emitted)        # inside file IS surveyed
    for fh in surv.salient:
        assert st.is_within_granted(fh.path)


def test_junction_dir_not_descended(tmp_path):
    """Fix 2 (Windows junction): a directory JUNCTION (`mklink /J`) reports
    is_symlink()==False but is a reparse point to an OUTSIDE target. The per-dir
    realpath confinement (commonpath vs the current root) must refuse to descend it,
    so none of the outside contents appear."""
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")

    root = tmp_path / "GrantedJ"
    root.mkdir()
    (root / "legit.txt").write_text("inside")

    outside_dir = tmp_path / "OutsideJ"
    outside_dir.mkdir()
    secret = outside_dir / "secretj.txt"
    secret.write_text("TOPSECRET")
    newer = 9_000_000.0
    os.utime(secret, (newer, newer))

    junction = root / "junc"
    rc = os.system(f'mklink /J "{junction}" "{outside_dir}" >nul 2>&1')
    if rc != 0 or not junction.exists():
        pytest.skip("could not create a directory junction")

    st = _store(tmp_path)
    st.grant(str(root))
    surv = build_roots(st)[0]

    emitted = {fh.path for fh in surv.salient}
    assert not any("secretj.txt" in p for p in emitted)  # junction target never surfaced
    assert any("legit.txt" in p for p in emitted)
    for fh in surv.salient:
        assert st.is_within_granted(fh.path)


def test_every_emitted_path_is_within_granted(tmp_path):
    root = tmp_path / "R"
    (root / "sub").mkdir(parents=True)
    for i in range(5):
        (root / "sub" / f"x{i}.txt").write_text("x")
    st = _store(tmp_path)
    st.grant(str(root))
    surv = build_roots(st)[0]
    assert surv.salient  # non-empty
    for fh in surv.salient:
        assert st.is_within_granted(fh.path)


# --------------------------------------------------------------------------- #
# Defensive
# --------------------------------------------------------------------------- #

def test_missing_root_emits_empty_salient_no_raise(tmp_path):
    # grant a path, then remove it from disk → survey row present, salient empty.
    root = tmp_path / "Gone"
    root.mkdir()
    st = _store(tmp_path)
    st.grant(str(root))
    root.rmdir()  # vanished after the grant
    surveys = build_roots(st)
    assert len(surveys) == 1
    assert surveys[0].salient == []


def test_broken_granted_roots_returns_empty_list():
    class _Boom:
        def list_roots(self):
            raise RuntimeError("store down")
    assert build_roots(_Boom()) == []


# --------------------------------------------------------------------------- #
# Freshness stamp (Task 7 cache invalidation, AC3)
# --------------------------------------------------------------------------- #

def test_root_freshness_stamp_has_mtime_and_entry_count(tmp_path):
    root = tmp_path / "F"
    root.mkdir()
    (root / "one.txt").write_text("1")
    stamp = root_freshness_stamp(str(root))
    assert isinstance(stamp, dict)
    assert "mtime" in stamp and "entry_count" in stamp
    assert stamp["entry_count"] == 1

    (root / "two.txt").write_text("2")
    stamp2 = root_freshness_stamp(str(root))
    assert stamp2["entry_count"] == 2   # adding a file changes the shallow count


def test_root_freshness_stamp_defensive_on_missing():
    assert root_freshness_stamp("/nonexistent/path/xyz") == {}
