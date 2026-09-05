"""Documentation self-containment -- every citation must resolve inside this
checkout.

A docstring, comment or markdown page in this repository must stand on its own:
a reader holding only this tree must be able to act on what it says. A citation
pointing at a planning document that does not ship here fails that bar twice
over. The reader cannot follow it, and -- the expensive half -- the sentence
wrapped around such a citation almost always OMITS the requirement it was
standing in for, so the pointer is carrying meaning the tree never states.

The repair is never deletion: the requirement gets written down inline, where
the reader already is.

This file is the control, not the convention. The rule was already the practice
and twelve files had drifted back across earlier syncs anyway.

Two design notes worth keeping:

* The forbidden spellings are ASSEMBLED at import time instead of written out
  as literals, so this module sits INSIDE its own fence rather than being an
  exception to it. A lint that must skip its own file cannot pin its own file.
* The scan matches on BYTES, and enumerates through ``git ls-files`` rather
  than a directory walk. Tracked-ness is the question being asked, and it also
  makes ``.git`` and every build directory unreachable by construction rather
  than by a blocklist someone has to remember to extend.

Every "clean" this module can emit is falsifiable: a scan that reached nothing,
a root that is not a checkout, and a tracked file that cannot be read all RAISE
instead of returning the empty list that means "nothing wrong here".
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple

import pytest


class HygieneScanError(RuntimeError):
    """The scan could not RUN.

    Deliberately distinct from "the scan ran and found nothing": collapsing the
    two lets a broken checkout render as a passing gate.
    """


# --- what may not appear ----------------------------------------------------
# Assembled, never spelled. See the module docstring.
_STEM = "MASTER"
_JOINERS = ("-", "_")
_TAILS = ("SPEC", "PLAN")

FORBIDDEN: Tuple[bytes, ...] = tuple(
    (_STEM + joiner + tail).encode("ascii")
    for joiner in _JOINERS
    for tail in _TAILS
)

# The text formats a citation can hide in.
TEXT_SUFFIXES = frozenset({".py", ".md", ".txt", ".toml", ".yml", ".yaml"})

# Belt-and-braces only: `git ls-files` already cannot report anything under
# `.git`, and build output is untracked. This catches the day someone commits a
# vendored tree.
SKIP_DIR_NAMES = frozenset({
    ".git", "build", "dist", "node_modules", ".venv", "venv",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
})

# UTF-16 text would encode an ASCII needle as interleaved NUL bytes and slip
# past a byte search. `test_no_scanned_file_is_utf16` witnesses that this tree
# contains no such file, so the byte match is COMPLETE here rather than assumed.
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def repo_root() -> Path:
    """The checkout this file lives in -- derived from ``__file__``, never from
    the cwd. A cwd-derived root silently scans an empty tree from anywhere else
    and reports clean."""
    return Path(__file__).resolve().parent.parent


def _in_scope(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in SKIP_DIR_NAMES for part in parts[:-1]):
        return False
    return Path(rel).suffix.lower() in TEXT_SUFFIXES


def iter_scanned_files(root: Path = None) -> List[Path]:
    """Every tracked in-scope text file, as absolute paths."""
    base = repo_root() if root is None else Path(root)
    if not (base / ".git").exists():
        raise HygieneScanError(f"{base} is not a git checkout -- refusing to "
                               f"report a scan of it as clean")
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(base), capture_output=True, check=True,
            encoding="utf-8", errors="backslashreplace",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HygieneScanError(f"could not enumerate {base}: {exc}") from exc

    found = []
    for rel in out.split("\0"):
        if rel and _in_scope(rel):
            path = base / rel
            if not path.exists():
                raise HygieneScanError(
                    f"tracked file {rel} is missing from disk -- a scan that "
                    f"skipped it would under-report")
            found.append(path)
    return found


def scan_repo(root: Path = None) -> List[str]:
    """``[]`` iff no tracked text file names an out-of-tree planning document.

    Findings come back as ``path:line  text`` so a red gate says exactly what to
    edit.
    """
    base = repo_root() if root is None else Path(root)
    files = iter_scanned_files(base)
    if not files:
        raise HygieneScanError(
            f"no tracked text files under {base} -- refusing to report "
            f"'never scanned' as clean")

    findings: List[str] = []
    for path in files:
        raw = path.read_bytes()
        if not any(needle in raw for needle in FORBIDDEN):
            continue
        rel = str(path.relative_to(base)).replace("\\", "/")
        text = raw.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            hits = [n.decode("ascii") for n in FORBIDDEN
                    if n.decode("ascii") in line]
            if hits:
                findings.append(f"{rel}:{lineno}  {line.strip()}")
    return findings


# --- the matcher does what it claims ----------------------------------------

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "hygiene-tests"],
                   cwd=str(path), check=True)


def _track(path: Path, *rel_files: str) -> None:
    subprocess.run(["git", "add", "--", *rel_files], cwd=str(path), check=True)


def test_all_four_spellings_are_caught(tmp_path):
    """Both joiners times both tails. Catching three of four is a fence with a
    gate left open in it."""
    _init_git_repo(tmp_path)
    for i, needle in enumerate(n.decode("ascii") for n in FORBIDDEN):
        name = f"doc{i}.md"
        (tmp_path / name).write_text(f"see {needle} section 4\n",
                                     encoding="utf-8")
        _track(tmp_path, name)
    found = scan_repo(tmp_path)
    assert len(found) == len(FORBIDDEN), found


def test_every_in_scope_suffix_is_actually_read(tmp_path):
    """A suffix listed in TEXT_SUFFIXES but never opened is coverage on paper."""
    _init_git_repo(tmp_path)
    needle = FORBIDDEN[0].decode("ascii")
    for suffix in sorted(TEXT_SUFFIXES):
        name = f"f{suffix}"
        (tmp_path / name).write_text(f"# cites {needle}\n", encoding="utf-8")
        _track(tmp_path, name)
    found = scan_repo(tmp_path)
    assert len(found) == len(TEXT_SUFFIXES), found


def test_an_untracked_violation_is_not_this_gates_business(tmp_path):
    """Scope, stated: the fence covers what the repository PUBLISHES. A scratch
    file nobody committed is not published."""
    _init_git_repo(tmp_path)
    (tmp_path / "kept.md").write_text("clean\n", encoding="utf-8")
    _track(tmp_path, "kept.md")
    (tmp_path / "scratch.md").write_text(
        f"{FORBIDDEN[0].decode('ascii')}\n", encoding="utf-8")
    assert scan_repo(tmp_path) == []


def test_an_unrelated_use_of_the_word_master_is_not_flagged(tmp_path):
    """The needle is the compound, not the stem -- 'master branch' and
    'mastering' must stay writable."""
    _init_git_repo(tmp_path)
    (tmp_path / "a.md").write_text(
        "the master branch, a master copy, mastering the plan and the spec\n",
        encoding="utf-8")
    _track(tmp_path, "a.md")
    assert scan_repo(tmp_path) == []


def test_the_finding_names_the_line_not_merely_the_file(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "a.md").write_text(
        "line one\nline two\nsee " + FORBIDDEN[0].decode("ascii") + "\n",
        encoding="utf-8")
    _track(tmp_path, "a.md")
    found = scan_repo(tmp_path)
    assert found == ["a.md:3  see " + FORBIDDEN[0].decode("ascii")], found


# --- fail loud, never fail open ---------------------------------------------

def test_a_root_that_is_not_a_checkout_raises(tmp_path):
    with pytest.raises(HygieneScanError, match="not a git checkout"):
        scan_repo(tmp_path)
    with pytest.raises(HygieneScanError, match="not a git checkout"):
        scan_repo(tmp_path / "definitely-not-here")


def test_a_checkout_with_no_tracked_text_files_raises(tmp_path):
    """``[]`` is the value that means "clean". A scan that opened nothing must
    not be able to produce it."""
    _init_git_repo(tmp_path)
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01")
    _track(tmp_path, "binary.bin")
    with pytest.raises(HygieneScanError, match="never scanned"):
        scan_repo(tmp_path)


def test_a_tracked_file_missing_from_disk_raises(tmp_path):
    """Skipping it would quietly shrink the scanned set."""
    _init_git_repo(tmp_path)
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    _track(tmp_path, "a.md")
    (tmp_path / "a.md").unlink()
    with pytest.raises(HygieneScanError, match="missing from disk"):
        scan_repo(tmp_path)


# --- the scan reaches the real tree -----------------------------------------

def test_the_scan_reaches_the_real_tree():
    """Guards the vacuous pass: a gate over an empty or wrong directory is
    green unconditionally."""
    files = iter_scanned_files()
    assert len(files) > 500, len(files)
    names = {f.name for f in files}
    assert "README.md" in names
    assert "pyproject.toml" in names
    assert "model_matrix.py" in names


def test_the_scan_reaches_docs_and_the_package_and_the_tests():
    """One in-scope file is not coverage of a tree with several roots."""
    rels = {str(f.relative_to(repo_root())).replace("\\", "/")
            for f in iter_scanned_files()}
    for prefix in ("docs/", "systemu/", "tests/", "sharing_on/"):
        assert any(r.startswith(prefix) for r in rels), prefix
    assert any("/" not in r for r in rels), "no repo-root text file reached"


def test_no_scanned_file_is_utf16():
    """Completeness witness for the byte match: UTF-16 would hide an ASCII
    needle behind interleaved NULs. Witnessed over the tree, not assumed."""
    offenders = [str(p) for p in iter_scanned_files()
                 if p.read_bytes()[:2] in _UTF16_BOMS]
    assert offenders == [], offenders


# --- the gate ---------------------------------------------------------------

def test_no_tracked_text_file_cites_an_out_of_tree_planning_document():
    findings = scan_repo()
    assert findings == [], (
        "These lines cite a planning document that does not ship in this "
        "checkout. Rewrite each so it states the requirement inline:\n"
        + "\n".join(findings))
