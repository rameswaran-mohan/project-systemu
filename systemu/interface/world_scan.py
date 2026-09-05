"""P2c - the consent-first world scan behind the Table page's "Scan a folder".

The card beside this code makes exactly two claims:

    "Reads file names and extensions only - never file contents.
     Nothing leaves this machine."

Both are properties of THIS module, and both are pinned by source-purity tests
in tests/test_world_scan_model.py, so the copy cannot drift away from the code
it describes:

  * NAMES ONLY.  The scan walks directory entries and looks at `entry.name` and
    a `stat`-level "is this a folder?".  It never asks for a file body - there
    is no call in this module that could produce one, which is why a file the
    process could not even have been allowed to look inside is still counted.
  * NOTHING LEAVES.  There is no network reach here at all, of any kind.

Two more properties matter as much and are pinned the same way:

  * SESSION-TRANSIENT.  A scan result is a value.  It is rendered and dropped
    when the operator leaves the page; nothing here writes a sidecar, a cache,
    a user fact or an OnTheTable item (`table_reconciler.project()` keeps its
    sole-writer invariant, DEC-10).  Because the scan is not kept, neither is a
    dismissal of a proposal derived from it: the decline-forever machinery on
    Home's proposals has nothing to attach to here, so this surface simply does
    not use it.
  * REFUSALS ARE VALUES (DEC-32).  A missing path, a file, an unlistable
    folder, and an entry whose real location escapes the named folder are all
    handled by RETURNING a verdict, never by raising one.  A raise would be no
    fence: every caller between here and the page is defensive, and one of them
    would have turned a refusal into an empty-looking success.

CONTAINMENT is per entry.  The operator named one folder; that path is resolved
ONCE and every entry is measured against it.  An entry whose resolved location
is not under that root is skipped and counted - so a symlink cannot walk the
scan out of the folder consent was given for, and the operator is told how many
entries the fence held back rather than being shown a quietly smaller number.

DEPTH is the named folder plus one level, and the walk is capped at
``ENTRY_CAP`` entries examined.  The cap rides in every result and is stated in
``summary_line`` whether or not it was hit, because a truncated count that
reads as a total is the kind of number an operator would act on.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: the hard ceiling on how many directory entries one scan will examine.
#: Stated as a constant, carried in every result, and rendered - a cap the
#: operator cannot see is indistinguishable from an empty folder.
ENTRY_CAP = 2000

#: the label a file with no extension renders under
NO_EXTENSION_LABEL = "(no extension)"

# --- refusal codes (DEC-32: the verdict is the returned value) ---------------
NO_PATH = "no_path"
NO_SUCH_PATH = "no_such_path"
NOT_A_DIRECTORY = "not_a_directory"
UNREADABLE = "unreadable"

#: code -> the sentence the operator sees.  Minted here, once, so the page
#: renders a refusal rather than inventing one - and so a code this table does
#: not carry degrades to a generic sentence instead of leaking the code itself
#: into the UI as if it were English.
_REFUSAL_SENTENCES = {
    NO_PATH: "Type the full path to a folder first.",
    NO_SUCH_PATH: "There is no folder at that path on this machine.",
    NOT_A_DIRECTORY: "That path is a file, not a folder.",
    UNREADABLE: "That folder could not be listed on this machine.",
}
_UNKNOWN_REFUSAL = "That folder could not be scanned."


@dataclass(frozen=True)
class ScanResult:
    """One scan, as a value.

    ``ok`` is the verdict; ``refusal`` names why not.  Everything else is what
    was counted: ``counts`` maps a lowercased extension (``""`` for a file with
    none) to how many files carried it, ``entries`` is how many directory
    entries were examined, ``outside`` how many the containment fence held
    back, and ``cap``/``capped`` disclose the ceiling and whether it was hit.
    """

    ok: bool = False
    refusal: str = ""
    folder: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    files: int = 0
    entries: int = 0
    cap: int = ENTRY_CAP
    capped: bool = False
    outside: int = 0


def _refuse(code: str) -> ScanResult:
    return ScanResult(ok=False, refusal=code, cap=ENTRY_CAP)


def _contained(real: Path, root: Path) -> bool:
    """True only when ``real`` is the root itself or lies under it.

    Both sides are already resolved, so this compares final locations - the
    check a symlink chain cannot talk its way past.
    """
    return real == root or root in real.parents


def _resolved(entry_path: str) -> Optional[Path]:
    """The entry's real location, or None when it cannot be established.

    Fail-closed: an entry we cannot place is treated exactly like one we placed
    outside the folder.  Being unable to prove an entry is inside is not a
    reason to look at it.
    """
    try:
        return Path(entry_path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _listing(folder: str) -> Optional[List]:
    """The folder's entries, name-sorted, or None if it cannot be listed.

    Sorting is what makes a capped scan deterministic: the same folder must
    yield the same partial answer every time, or the derived proposals flicker.
    """
    try:
        with os.scandir(folder) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return None


def _is_folder(entry) -> bool:
    try:
        return bool(entry.is_dir())
    except OSError:
        return False


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def scan_folder(path) -> ScanResult:
    """Count file extensions in ``path`` - names only, one folder deep.

    Never raises: every failure comes back as a ``ScanResult`` whose ``ok`` is
    False and whose ``refusal`` names the reason.  ``path`` must be a real
    ``str`` (DEC-36: the concrete type is pinned before any operation, so a
    value with a clever ``__fspath__`` cannot dispatch itself into the walk).
    """
    if type(path) is not str:
        return _refuse(NO_PATH)
    typed = path.strip()
    if not typed:
        return _refuse(NO_PATH)

    root = _resolved(typed)
    if root is None:
        return _refuse(NO_SUCH_PATH)
    try:
        if not root.exists():
            return _refuse(NO_SUCH_PATH)
        if not root.is_dir():
            return _refuse(NOT_A_DIRECTORY)
    except OSError:
        return _refuse(UNREADABLE)

    top = _listing(str(root))
    if top is None:
        return _refuse(UNREADABLE)

    cap = ENTRY_CAP                     # read once, per call
    counts: Dict[str, int] = {}
    entries = files = outside = 0
    capped = False
    descend: List[str] = []

    for entry in top:
        if entries >= cap:
            capped = True
            break
        entries += 1
        real = _resolved(entry.path)
        if real is None or not _contained(real, root):
            outside += 1
            continue
        if _is_folder(entry):
            descend.append(entry.path)
            continue
        ext = _extension(entry.name)
        counts[ext] = counts.get(ext, 0) + 1
        files += 1

    for folder in descend:
        if capped:
            break
        children = _listing(folder)
        if children is None:
            continue
        for entry in children:
            if entries >= cap:
                capped = True
                break
            entries += 1
            real = _resolved(entry.path)
            if real is None or not _contained(real, root):
                outside += 1
                continue
            if _is_folder(entry):
                continue                # depth 2 - deliberately out of scope
            ext = _extension(entry.name)
            counts[ext] = counts.get(ext, 0) + 1
            files += 1

    return ScanResult(ok=True, refusal="", folder=str(root), counts=counts,
                      files=files, entries=entries, cap=cap, capped=capped,
                      outside=outside)


# --- the minted sentences ----------------------------------------------------

def refusal_message(result) -> str:
    """The operator sentence for a refused scan; ``""`` when it succeeded.

    An unrecognised refusal code degrades to a generic sentence rather than
    being printed: a code is a value for this module, not copy for a human.
    """
    if type(result) is not ScanResult:
        return _UNKNOWN_REFUSAL
    if result.ok is True:
        return ""
    return _REFUSAL_SENTENCES.get(result.refusal, _UNKNOWN_REFUSAL)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many}"


def summary_line(result) -> str:
    """What the scan actually looked at, in one line; ``""`` when it refused.

    The cap is named whether or not it was reached, the depth is named, and a
    truncated walk says so - the three facts that stop a partial count from
    being read as a total.
    """
    if type(result) is not ScanResult or result.ok is not True:
        return ""
    kinds = len([e for e in result.counts if e])
    line = (f"{_plural(result.files, 'file', 'files')}, "
            f"{_plural(kinds, 'extension', 'extensions')} - top level plus "
            f"one folder deep, up to {result.cap} entries.")
    if result.capped:
        line += f" Stopped at the {result.cap}-entry cap - this is a partial count."
    if result.outside:
        line += (f" {_plural(result.outside, 'entry', 'entries')} skipped - "
                 "not inside the folder you named.")
    return line


def count_rows(result) -> List[Tuple[str, int]]:
    """The extension counts as display rows, commonest first.

    Ties break on the extension itself so the card is stable between renders,
    and a file with no extension gets a label rather than an empty cell.
    """
    if type(result) is not ScanResult or result.ok is not True:
        return []
    ordered = sorted(result.counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(ext or NO_EXTENSION_LABEL, n) for ext, n in ordered]
