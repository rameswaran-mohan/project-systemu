"""AST lint: no truncation INSIDE a redaction call's arguments (DEC-31).

DEC-31 — "redaction is the FIRST transformation on any value crossing a trust
boundary: before truncation, slicing, escaping, ``%r``, ``str()``." The rule
already existed and was already written down verbatim in ``outbox._esc``
("*Order matters*"), and the leak shipped twice anyway. A rule reviewers must
remember is not a control; this is the control.

Usage:
    python -m tools.lint_redaction_order      # exit 1 if any site exists

**What it forbids.** A call to a redaction function whose argument expression
contains a PREFIX slice — starting at index 0, spelled either with no lower
bound or with an explicit literal ``0``::

    redact(str(value)[:64])          # ← the shipped defect
    redact(str(value)[0:64])         # ← the SAME slice, byte-identical result
    mask_outbound(body[:MAX])        # ← same shape, different spelling

and the fix is to let the redactor do the capping, so masking runs first::

    redact(value, max_len=64)

**Why the leak is real rather than theoretical.** Measured on this tree: a
230-char JWT through ``outbox.redact`` returns
``[redacted - looked like a credential]``; the same token sliced to 64 chars
first returns 64 CLEAR characters — and ``jwt[:64] == jwt[0:64]``, so both
spellings of the slice leak identically. Every shape these redactors know
(JWT, ``Bearer …``, ``sk-…``, ``ghp_…``, long hex) spans more characters than
a typical log-line cap, so the slice does not shorten the secret — it hides it
from the matcher.

**PREFIX slices only, and that is a measured choice.** ``x[:n]`` and the
explicit-zero spelling ``x[0:n]`` are the SAME slice — both start at index 0,
so both are truncation. ``x[pos:]`` and ``x[pos:m.start()]``, where ``pos`` is
anything OTHER than the literal ``0``, are *segmentation* —
``credentials.known_values.redact_known_secrets`` tokenises with exactly those
two forms (a variable that is never a literal ``0`` in source) and feeds each
token to ``_redact_token``, which is correct and must stay green. Flagging
every ``[`` would fire on that file on day one, and a lint nobody can keep
green gets switched off.

**The two-line form is deliberately NOT flagged.** ``s = v[:n]`` followed by
``redact(s)`` is the same defect, but measured over this tree the candidate set
is 9 sites and 8 of them are test fixtures building a value OF a given length
(``h = ('deadbeef' * 10)[:n]``, then ``mask_outbound(h)``) — legitimate, and
noise. So this lint enforces the shape it can judge, and says so rather than
implying coverage it does not have. See :data:`SCOPE_NOTE`.

**Callees are matched by BARE NAME.** Redaction helpers in this tree are
imported directly (``from systemu.runtime.outbox import redact``) and called
unqualified, so there is usually no module prefix to resolve. The name set is
enumerated from the tree rather than pattern-matched: a substring heuristic on
"esc"/"mask"/"scrub" was tried first and matched ``_descriptor``, ``_escalate``,
``executescript`` and ``GateDescriptor``. ``html.escape`` is deliberately absent
— escaping is the step that must come AFTER redaction, not a redactor.

**FAIL LOUD, NEVER FAIL OPEN.** A prior lint in this repo shipped with exactly
two fail-open bugs: an unparseable file returned ``[]`` (the value that means
"clean"), and ``main()`` printed "clean" and exited 0 from any working directory
that was not the repo root. Both are closed here and both are pinned by tests:
unparseable input raises :class:`RedactionLintError`, the scan root must be a
real git checkout with at least one tracked ``.py`` file, and the root is
resolved from ``__file__`` so the cwd is irrelevant. Exit 2 (“could not run”)
is distinct from exit 1 (“violations”).

**What gets scanned (DEC-34c AC-5).** Every ``*.py`` file ``git`` TRACKS in
this checkout, via ``git ls-files`` — not a fixed directory list. This lint
originally scanned only ``("systemu", "sharing_on", "tools", "tests")``,
which let every OTHER tracked ``.py`` file (root-level scripts, ``alembic/``,
``plugins/``, ``scripts/`` — 27 files, measured) escape the scan by
construction, silently, with nothing ever failing when a new top-level
package or a tracked repo-root script appeared. ``git ls-files`` is the same
source of truth the release process already trusts for "is this file part of
the tree", so there is no list to remember to update.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

_ALLOW_MARKER = "redaction-lint: ok"


class RedactionLintError(RuntimeError):
    """The lint could not run. Never confuse this with a clean result."""


#: Functions that REDACT. Enumerated from this tree's call sites, not guessed.
_REDACTORS = frozenset({
    # outbox (the DEC-31 reference implementation)
    "redact", "_esc",
    # messaging / gateway
    "mask_outbound", "mask_secret", "mask_key", "mask_and_digest_params",
    "_mask", "_mask_message", "_mask_url", "_mask_query", "_mask_proxy_url",
    # credentials
    "redact_known_secrets", "_redact_token", "_redact_secrets", "redact_dict",
    # evidence / verifier
    "_mask_evidence", "_scrub_value_shapes", "_scrub",
    # description + payload sanitisers
    "sanitize_description", "sanitize_card_payload", "_sanitize_html",
})

SCOPE_NOTE = (
    "redaction-order lint scope: a PREFIX slice — x[:n] OR the byte-identical "
    "x[0:n] — appearing inside the ARGUMENTS of a call to a known redaction "
    "function (see _REDACTORS).\n"
    "  It CANNOT see: the two-line form (s = v[:n] then redact(s)); truncation "
    "behind a helper (shorten(v), clip(v), f'{v:.64}'); truncation applied in a "
    "DIFFERENT module before the value reaches a redactor; a redactor whose "
    "name is not in the enumerated set; or a lower bound that is zero only at "
    "RUNTIME through a variable (pos = 0; v[pos:n]) — only the literal ``0`` "
    "is recognised, because a static matcher cannot know a Name's value.\n"
    "  A clean run therefore means 'no in-argument prefix slice reaches a "
    "redactor this matcher knows' — NOT 'nothing is truncated before it is "
    "redacted'."
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def key(self) -> str:
        return f"{self.path}:{self.message}"


def _callee_name(node: ast.AST) -> Optional[str]:
    """The bare name a call resolves to: ``redact`` for both ``redact(x)`` and
    ``outbox.redact(x)``. ``None`` for anything not rooted in a name/attribute
    (a call returning a callable, a subscripted table of functions)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_zero_lower(node: Optional[ast.expr]) -> bool:
    """True for a slice lower bound that starts the slice at index 0: absent
    (``x[:n]``) or the literal integer ``0`` (``x[0:n]``) — byte-identical
    slices, so both are the SAME truncation.

    Regression guard: the first version of this matcher checked only
    ``lower is None``, so ``redact(str(v)[0:64])`` — the exact shape of the
    shipped defect, one character different — passed clean while leaking the
    same 64 clear characters as ``redact(str(v)[:64])``.

    Only a bare ``ast.Constant`` is recognised. ``bool`` is excluded even
    though ``False == 0`` — nobody spells a slice bound that way, and treating
    it as int-like would be a guess this matcher does not need to make. A
    NAME that merely holds ``0`` at runtime (``pos = 0; v[pos:n]``) is NOT
    recognised: a static matcher cannot see a variable's value, and this is a
    documented residual blind spot (see :data:`SCOPE_NOTE`), not a silent gap.
    """
    if node is None:
        return True
    return (isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
            and node.value == 0)


def _prefix_slices(node: ast.AST) -> List[ast.Subscript]:
    """Every ``x[:n]`` — and its byte-identical spelling ``x[0:n]`` — in
    ``node``'s subtree.

    Requires a zero-or-absent lower bound (see :func:`_is_zero_lower`) AND
    ``upper is not None``: that is truncation. ``x[pos:]`` / ``x[a:b]`` with
    ``a`` anything other than the literal ``0`` are segmentation and
    ``x[::2]`` is a stride — none of them hide a secret from a shape matcher
    by cutting its tail off.
    """
    out: List[ast.Subscript] = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Subscript)
                and isinstance(n.slice, ast.Slice)
                and _is_zero_lower(n.slice.lower)
                and n.slice.upper is not None):
            out.append(n)
    return out


_MAX_COMMENT_SCAN = 10


def _marked(lines: List[str], lineno: int) -> bool:
    """True if the call carries a JUSTIFIED ``redaction-lint: ok`` marker.

    Accepted on the call's own line or anywhere in the contiguous comment block
    directly above it, and the marker must be FOLLOWED BY TEXT — a bare
    ``# redaction-lint: ok`` does not suppress. The escape hatch exists so a
    genuine case (slicing a list of already-safe items, or a deliberate
    characterisation of the defect in a test) can stay in the tree; requiring a
    reason is what stops it from becoming the way the finding gets silenced.

    The scan stops at the first non-comment line, so a marker attached to an
    earlier statement cannot leak down onto an unrelated call.
    """
    def _justified(line: str) -> bool:
        _, _, rest = line.partition(_ALLOW_MARKER)
        return len(rest.strip(" \t-—:#")) >= 3

    own = lineno - 1
    if 0 <= own < len(lines) and _ALLOW_MARKER in lines[own]:
        return _justified(lines[own])
    idx = own - 1
    scanned = 0
    while idx >= 0 and scanned < _MAX_COMMENT_SCAN:
        stripped = lines[idx].strip()
        if not stripped.startswith("#"):
            return False
        if _ALLOW_MARKER in stripped:
            return _justified(stripped)
        idx -= 1
        scanned += 1
    return False


def find_violations(source: str, path: str) -> List[Violation]:
    """Truncation inside a redaction call's arguments, in ``source``.

    Raises :class:`RedactionLintError` if ``source`` does not parse. It must not
    return ``[]`` for input it never examined — ``[]`` is the value that means
    "this file is clean", and handing that back for a file the lint could not
    read is the fail-open this lint exists to prevent.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RedactionLintError(
            f"{path}: could not parse (line {exc.lineno}): {exc.msg}") from exc
    lines = source.splitlines()
    out: List[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name not in _REDACTORS:
            continue
        # Arguments ONLY. `redact(x)[:64]` slices the RESULT, which is the
        # correct order and must never fire.
        args = list(node.args) + [k.value for k in node.keywords]
        hits = [s for a in args for s in _prefix_slices(a)]
        if not hits:
            continue
        if _marked(lines, node.lineno):
            continue
        out.append(Violation(
            path, node.lineno,
            f"DEC-31: {name}(...) receives a TRUNCATED value — redaction must "
            f"run before truncation, or the shape matcher never sees the "
            f"secret. Use the redactor's own cap (e.g. redact(v, max_len=N)) "
            f"or annotate: # {_ALLOW_MARKER} — <why this slice is safe>",
        ))
    return out


def repo_root() -> Path:
    """The checkout this file lives in — NOT the cwd.

    A prior lint scanned ``Path.cwd() / "systemu/interface"``, so running it from
    anywhere else reported a clean tree it had never looked at.
    """
    return Path(__file__).resolve().parent.parent


def _tracked_python_files(root: Path) -> List[Path]:
    """Every ``*.py`` file ``git`` TRACKS under ``root``, via ``git ls-files``.

    DEC-34c AC-5. NOT a fixed directory list (``_SCAN_DIRS`` before this fix —
    ``("systemu", "sharing_on", "tools", "tests")``, four names that let every
    OTHER tracked file — root-level scripts (``install.py``,
    ``benchmark.py``, …), ``alembic/``, ``plugins/``, ``scripts/`` — escape
    the scan by construction, silently, forever, because nothing failed when
    a new top-level package appeared). ``git ls-files`` is the same source of
    truth the release process already trusts for "is this file part of the
    tree", so a new tracked file anywhere is scanned automatically, with no
    matching list to remember to update.

    ``-z`` (NUL-separated, no quoting) so a path is read byte-exact even if
    it contains a space or another shell-special character.

    FAILS LOUD, never ``[]`` (the value this module treats as "clean"):
    ``root`` missing, ``git`` unavailable/erroring (not a checkout, or a
    worktree with no commits — ``ls-files`` on a truly empty index also
    returns nothing, which the zero-files check below catches too), or a
    checkout with zero tracked ``.py`` files all raise
    :class:`RedactionLintError`.
    """
    root = Path(root)
    if not root.is_dir():
        raise RedactionLintError(
            f"scan root {root} does not exist or is not a directory")
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RedactionLintError(
            f"could not run `git ls-files` under {root}: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise RedactionLintError(
            f"`git ls-files` failed under {root} (exit {proc.returncode}): "
            f"{stderr or '(no output)'} — {root} is not a git checkout. "
            f"Refusing to report a tree that was never scanned as clean.")
    raw = proc.stdout.decode("utf-8", "surrogateescape")
    names = [n for n in raw.split("\0") if n]
    if not names:
        raise RedactionLintError(
            f"`git ls-files` found no tracked .py files under {root} — "
            f"refusing to report a tree that was never scanned as clean")
    return sorted(root / n for n in names)


def iter_scanned_files(root: Optional[Path] = None) -> Iterator[Path]:
    base_root = repo_root() if root is None else Path(root)
    yield from _tracked_python_files(base_root)


def scan_repo(root: Optional[Path] = None) -> List[Violation]:
    """Every unannotated truncate-then-redact site under every tracked
    ``.py`` file.

    Raises :class:`RedactionLintError` when ``root`` is missing/invalid, not
    a git checkout, or has zero tracked ``.py`` files (all surfaced by
    :func:`iter_scanned_files` -> :func:`_tracked_python_files`, which never
    returns ``[]``). An unparseable file is reported as a Violation rather
    than aborting — still loud (it prints with its path and forces a
    non-zero exit) without discarding the other findings.
    """
    files = list(iter_scanned_files(root))
    base_root = repo_root() if root is None else Path(root)
    found: List[Violation] = []
    for py in files:
        try:
            rel = str(py.relative_to(base_root)).replace("\\", "/")
        except ValueError:
            rel = str(py).replace("\\", "/")
        try:
            found.extend(find_violations(py.read_text(encoding="utf-8"), rel))
        except RedactionLintError as exc:
            found.append(Violation(rel, 0, f"UNPARSEABLE — {exc}"))
        except OSError as exc:
            found.append(Violation(rel, 0, f"UNREADABLE — {exc}"))
    return found


def main() -> int:
    try:
        violations = scan_repo()
        scanned = len(list(iter_scanned_files()))
    except RedactionLintError as exc:
        print(f"redaction-order lint: CANNOT RUN — {exc}", file=sys.stderr)
        return 2
    for v in violations:
        print(f"{v.path}:{v.line}: {v.message}")
    if violations:
        print(f"\n{len(violations)} truncate-then-redact site(s) under "
              f"{repo_root()} (every git-tracked *.py file)")
        print(SCOPE_NOTE, file=sys.stderr)
        return 1
    print(f"redaction-order lint: clean — {scanned} file(s) scanned under "
          f"{repo_root()} (every git-tracked *.py file)")
    print(SCOPE_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
