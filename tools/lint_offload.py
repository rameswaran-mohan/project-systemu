"""AST lint: no unannotated BLOCKING calls under ``systemu/interface/``.

R-UX2 / SPEC Part II §15-UX **UX-9(a)** — "the event loop is never blocked
>50ms … enforced by an **offload lint** (blocking calls in ``interface/``
handlers forbidden — the style-lint pattern)".

Usage:
    python -m tools.lint_offload          # exit 1 if any unannotated site exists

**What this lint does and does not claim.** It does not prove thread affinity —
that is not decidable from the AST, and pretending otherwise would be the more
dangerous lint. It enforces something weaker and checkable: *every blocking call
it can SEE under ``systemu/interface/`` is accounted for.* A site that genuinely
runs off the event loop carries a marker naming where it runs::

    time.sleep(2)  # offload-lint: ok — runs on the refine-launcher thread

so a NEW blocking call cannot appear in a UI handler without someone writing
down why it is safe. Sites that are *not* safe get fixed instead — the canonical
fix is ``await run.io_bound(...)`` (nicegui ships ``run.io_bound``/``cpu_bound``).
``await asyncio.sleep(...)`` is the cooperative form and is deliberately absent
from ``_BLOCKING``: it is the fix, not the problem.

**Why AST and not grep.** ``interface/event_bus.py`` contains
``self._approval_requests.get(request_id)``, whose text contains
``requests.get``. A regex lint flags that line forever and is then disabled; the
AST form resolves the receiver (``self._approval_requests``) and does not fire.
The same reason drives alias resolution: the one genuine violation this lint was
built to catch was spelled ``_time.sleep(0.5)`` behind ``import time as _time``.

**FAIL LOUD, NEVER FAIL OPEN.** Three earlier paths returned the *clean* value
for an input that had not been checked at all: an unparseable file returned
``[]``; ``scan_repo(Path('C:/'))`` returned ``[]``; and ``main()`` printed
"offload lint: clean" and exited 0 from **any working directory that was not the
repo root**, because the scan base was ``Path.cwd() / "systemu/interface"`` with
no existence check. A lint whose "everything is fine" is indistinguishable from
"I looked at nothing" is worse than no lint. So: unparseable input raises
:class:`OffloadLintError`, a missing or empty scan base raises, and ``main()``
resolves the repo root from ``__file__`` rather than trusting the cwd.

**What the matcher can and cannot see** — see :data:`SCOPE_NOTE`, which is
printed on every run rather than buried here, so the limitation travels with the
result instead of with the source.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

_INTERFACE = Path("systemu/interface")
_ALLOW_MARKER = "offload-lint: ok"


class OffloadLintError(RuntimeError):
    """The lint could not run. Never confuse this with a clean result."""


# Fully-qualified callables that block the calling thread. Deliberately narrow:
# every entry here is a call that WAITS. ``subprocess.Popen`` is absent on
# purpose — it returns immediately and is how you avoid blocking.
_BLOCKING = frozenset({
    "time.sleep",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "asyncio.run",
    "urllib.request.urlopen",
    "requests.request",
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.patch",
    "requests.delete",
    "requests.head",
})

# Unqualified method names that block whatever the receiver's static type is.
#
# This exists because ``_dotted_name`` returns ``None`` for any receiver not
# rooted in a bare Name, so ``gate["event"].wait(timeout=timeout_s)`` in
# ``interface/event_bus.py`` — a real, unbounded block-poll — was invisible to
# the qualified matcher AND to ``_BLOCKING`` (``threading.Event.wait`` was never
# listed). A silent skip is a fail-open.
#
# The set is MEASURED, not guessed. Counting bound-method call sites under
# ``systemu/interface/``: ``.wait()`` 4 (all four genuine blocking waits),
# ``.acquire()`` 0, ``.join()`` 82 (essentially all ``str.join``), ``.get()``
# 893 (essentially all ``dict.get``). So ``wait``/``acquire`` are precise enough
# to enforce; ``join``/``get`` would be pure noise and are deliberately excluded
# — a lint nobody can keep green gets disabled, which is the real failure mode.
_BLOCKING_METHODS = frozenset({"wait", "acquire"})

SCOPE_NOTE = (
    "offload lint scope: module-qualified blocking calls (time.sleep, "
    "subprocess.run, requests.*, asyncio.run, urlopen) plus unqualified "
    ".wait()/.acquire() on any receiver.\n"
    "  It CANNOT see: blocking work behind a bound method whose receiver type "
    "is not knowable from the AST (q.get(), conn.execute(), sock.recv(), "
    "proc.communicate()); blocking inside a C extension; or blocking inside a "
    "function this call reaches.\n"
    "  A clean run therefore means 'no blocking call this matcher can see is "
    "unannotated' — NOT 'the event loop is never blocked'."
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def key(self) -> str:
        return f"{self.path}:{self.message}"


def _dotted_name(node: ast.AST) -> Optional[str]:
    """``a.b.c`` → "a.b.c"; anything rooted in a non-Name (a call, a subscript,
    ``self.x``) resolves to a name rooted at that Name and therefore cannot
    collide with a module-qualified entry in ``_BLOCKING``.

    Returning ``None`` here is NOT a decision that the call is safe — it is an
    admission that this matcher cannot classify it. ``_BLOCKING_METHODS`` is the
    second pass that covers the blocking cases that land here.
    """
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _alias_map(tree: ast.AST) -> Dict[str, str]:
    """local binding → fully-qualified name (covers nested/function-local imports)."""
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _resolve(dotted: str, aliases: Dict[str, str]) -> str:
    head, _, rest = dotted.partition(".")
    target = aliases.get(head)
    if target is None:
        return dotted
    return f"{target}.{rest}" if rest else target


_MAX_COMMENT_SCAN = 10


def _marked(lines: List[str], lineno: int) -> bool:
    """True if the call carries an ``offload-lint: ok`` marker.

    Accepted on the call's own line, or anywhere in the contiguous comment block
    directly above it. The block form matters: these annotations must say *where
    the call actually runs*, and a useful justification rarely fits on one line.
    The scan stops at the first non-comment line, so a marker attached to some
    earlier statement cannot leak down and silently excuse an unrelated call.
    """
    own = lineno - 1
    if 0 <= own < len(lines) and _ALLOW_MARKER in lines[own]:
        return True
    idx = own - 1
    scanned = 0
    while idx >= 0 and scanned < _MAX_COMMENT_SCAN:
        stripped = lines[idx].strip()
        if not stripped.startswith("#"):
            return False
        if _ALLOW_MARKER in stripped:
            return True
        idx -= 1
        scanned += 1
    return False


def _blocking_label(node: ast.Call, aliases: Dict[str, str]) -> Optional[str]:
    """The name to report for a blocking call, or ``None`` if it is not one."""
    dotted = _dotted_name(node.func)
    if dotted is not None:
        resolved = _resolve(dotted, aliases)
        if resolved in _BLOCKING:
            return resolved
    # Second pass: an unqualified blocking method on any receiver at all. This
    # is what catches gate["event"].wait(...), which the qualified pass cannot
    # even name.
    if isinstance(node.func, ast.Attribute) and node.func.attr in _BLOCKING_METHODS:
        return f".{node.func.attr}"
    return None


def find_violations(source: str, path: str) -> List[Violation]:
    """Unannotated blocking calls in ``source``.

    Raises :class:`OffloadLintError` if ``source`` does not parse. It must not
    return ``[]`` for input it never examined — ``[]`` is the value that means
    "this file is clean", and handing that back for a file the lint could not
    read is the fail-open this lint exists to avoid elsewhere.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise OffloadLintError(
            f"{path}: could not parse (line {exc.lineno}): {exc.msg}") from exc
    aliases = _alias_map(tree)
    lines = source.splitlines()
    out: List[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = _blocking_label(node, aliases)
        if label is None:
            continue
        if _marked(lines, node.lineno):
            continue
        out.append(Violation(
            path, node.lineno,
            f"blocking call {label}() in systemu/interface/ — offload it "
            f"(await run.io_bound(...)) or annotate: # {_ALLOW_MARKER} — <where it runs>",
        ))
    return out


def repo_root() -> Path:
    """The checkout this file lives in — NOT the cwd.

    ``main()`` used to scan ``Path.cwd() / "systemu/interface"``, so running the
    lint from anywhere else reported a clean tree it had never looked at.
    """
    return Path(__file__).resolve().parent.parent


def scan_base(root: Optional[Path] = None) -> Path:
    """The directory that will be scanned, validated.

    ``root is None`` (not ``root or ...``): an explicit falsy-ish root must not
    silently become the default.
    """
    base_root = repo_root() if root is None else Path(root)
    if not base_root.is_dir():
        raise OffloadLintError(
            f"scan root {base_root} does not exist or is not a directory")
    base = base_root / _INTERFACE
    if not base.is_dir():
        raise OffloadLintError(
            f"{base} does not exist — {base_root} is not a systemu checkout. "
            f"Refusing to report a tree that was never scanned as clean.")
    return base


def iter_scanned_files(root: Optional[Path] = None) -> Iterator[Path]:
    yield from sorted(scan_base(root).rglob("*.py"))


def scan_repo(root: Optional[Path] = None) -> List[Violation]:
    """Every unannotated blocking call under ``<root>/systemu/interface``.

    Raises :class:`OffloadLintError` when the base is missing/invalid or holds
    no Python files. An unparseable file is reported as a Violation rather than
    aborting the scan — that is still loud (it prints with its path and forces a
    non-zero exit) and it does not throw away the other findings.
    """
    files = list(iter_scanned_files(root))
    if not files:
        raise OffloadLintError(
            f"no Python files under {scan_base(root)} — nothing was scanned")
    base_root = repo_root() if root is None else Path(root)
    found: List[Violation] = []
    for py in files:
        try:
            rel = str(py.relative_to(base_root)).replace("\\", "/")
        except ValueError:
            rel = str(py).replace("\\", "/")
        try:
            found.extend(find_violations(py.read_text(encoding="utf-8"), rel))
        except OffloadLintError as exc:
            found.append(Violation(rel, 0, f"UNPARSEABLE — {exc}"))
        except OSError as exc:
            found.append(Violation(rel, 0, f"UNREADABLE — {exc}"))
    return found


def main() -> int:
    try:
        violations = scan_repo()
        scanned = len(list(iter_scanned_files()))
    except OffloadLintError as exc:
        print(f"offload lint: CANNOT RUN — {exc}", file=sys.stderr)
        return 2
    for v in violations:
        print(f"{v.path}:{v.line}: {v.message}")
    if violations:
        print(f"\n{len(violations)} unannotated blocking call(s) in {_INTERFACE}")
        print(SCOPE_NOTE, file=sys.stderr)
        return 1
    print(f"offload lint: clean — {scanned} file(s) scanned under "
          f"{repo_root() / _INTERFACE}")
    print(SCOPE_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
