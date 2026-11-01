"""AST lint: every mask function under ``systemu/`` reaches the ONE shared
secret-span primitive, ``messaging.gateway.mask_secret_spans``.

Usage:
    python -m tools.lint_mask_primitive     # exit 1 on drift from the baseline

**Why.** Three masks in this tree grew their own rule sets and fell behind the
outbound push: the evidence / per-call audit / ledger mask had no ``Basic``,
``Token`` or URL-userinfo rule, and the resolve-audit row masked only values
that carried a hint word. Each was closed by calling the primitive instead of
copying its rules. This lint keeps it that way.

**What it checks.** A MASK FUNCTION is any function under ``systemu/`` whose
name contains ``mask``, ``redact`` or ``scrub`` (case-insensitive). Each nested
function is its own scope (``outer.inner``). A mask function must REACH the
primitive: call it, or call -- or pass as a value, as ``mask_values`` passes
``_text_core`` to a walker -- a function that reaches it. Reachability is a
fixed point over the whole tree, resolved by BARE NAME, and conservatively: a
name counts only if EVERY function of that name in the tree reaches the
primitive, so two same-named functions cannot launder each other.

A mask function that does not reach it must be listed in
``tools/mask_primitive_allowlist.json`` with a ``kind`` and a ``reason``.

**Baseline, enforced both ways (DEC-32c).** The test fails on a NEW mask
function that neither reaches the primitive nor is listed, and on a STALE
entry (a listed function that now reaches it, or no longer exists -- delete
the line). So the list can only shrink.

**Fenced maskers.** :data:`FENCED` names the maskers that must call the
primitive DIRECTLY, not through a chain: the push, the evidence value pass,
and the resolve-audit row. Replacing that call with a copy of the rules fails
the lint even if some other path still reaches the primitive.

**What it cannot see** (:data:`SCOPE_NOTE`): a masking function whose name has
none of the three words; whether the primitive's result is what the function
returns; a call on a path that never runs; and rules copied into a function
that ALSO calls the primitive. A clean run means "every mask-named function
this matcher can see reaches the primitive or is listed with a reason" -- NOT
"no mask in the tree has its own rules". FAIL LOUD: an unparseable file, an
empty scan or a missing primitive raises.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

PRIMITIVE_PATH = "systemu/messaging/gateway.py"
PRIMITIVE = "mask_secret_spans"

#: A function whose name matches is a mask function.
MASK_NAME_RE = re.compile(r"mask|redact|scrub", re.IGNORECASE)

#: (path, qualname) of maskers that must call :data:`PRIMITIVE` directly.
FENCED: Tuple[Tuple[str, str], ...] = (
    ("systemu/messaging/gateway.py", "mask_outbound"),
    ("systemu/runtime/external_verifier.py", "_scrub_value_shapes"),
    ("systemu/messaging/decision_bridge.py", "_mask"),
)

ALLOWLIST_PATH = Path(__file__).with_name("mask_primitive_allowlist.json")

KINDS = frozenset({
    "primitive-internal",   # part of the primitive itself
    "whole-value",          # replaces the ENTIRE value; there is no span left
    "identity",             # removes a KNOWN value by equality, not by shape
    "structural",           # drops a structural part (a URL's password/query)
    "predicate",            # returns a bool; masks nothing
    "residual",             # a known gap, listed so it is visible, not permitted
})

SCOPE_NOTE = (
    "mask-primitive lint scope: a function under systemu/ whose name contains "
    "mask/redact/scrub must reach messaging.gateway.mask_secret_spans (call it, "
    "or call/pass a function that does) or be allowlisted with a reason.\n"
    "  It CANNOT see: a masking function named otherwise; whether the "
    "primitive's result is what is returned; a call on a dead path; or rules "
    "copied into a function that also calls the primitive. A clean run means "
    "'every mask-named function reaches the primitive or is listed' -- NOT "
    "'no mask in the tree carries its own rules'."
)


class MaskPrimitiveLintError(RuntimeError):
    """The lint could not run. Never confuse this with a clean result."""


@dataclass(frozen=True)
class Finding:
    path: str
    qualname: str

    @property
    def key(self) -> Tuple[str, str]:
        return (self.path, self.qualname)


class _Scope:
    __slots__ = ("path", "qualname", "name", "refs")

    def __init__(self, path: str, qualname: str, name: str) -> None:
        self.path = path
        self.qualname = qualname
        self.name = name
        self.refs: Set[str] = set()

    @property
    def key(self) -> Tuple[str, str]:
        return (self.path, self.qualname)


def _scopes(tree: ast.AST, rel: str) -> List[_Scope]:
    """Every function scope (nested ones separate) with the names it uses."""
    out: List[_Scope] = []

    def visit(node: ast.AST, prefix: str, scope: Optional[_Scope]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            s = _Scope(rel, f"{prefix}{node.name}", node.name)
            out.append(s)
            for d in node.decorator_list:
                visit(d, prefix, scope)
            for child in node.body:
                visit(child, f"{s.qualname}.", s)
            return
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                visit(child, f"{prefix}{node.name}.", scope)
            return
        if scope is not None:
            if isinstance(node, ast.Name):
                scope.refs.add(node.id)
            elif isinstance(node, ast.Attribute):
                scope.refs.add(node.attr)
        for child in ast.iter_child_nodes(node):
            visit(child, prefix, scope)

    visit(tree, "", None)
    return out


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def iter_scanned_files(root: Optional[Path] = None) -> Iterator[Path]:
    root = root or repo_root()
    pkg = root / "systemu"
    if not pkg.is_dir():
        raise MaskPrimitiveLintError(f"no systemu/ package under {root}")
    files = sorted(pkg.rglob("*.py"))
    if not files:
        raise MaskPrimitiveLintError(f"no .py files under {pkg}")
    yield from files


def _parse(source: str, rel: str) -> ast.AST:
    try:
        return ast.parse(source, filename=rel)
    except SyntaxError as exc:
        raise MaskPrimitiveLintError(f"cannot parse {rel}: {exc}") from exc


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise MaskPrimitiveLintError(f"cannot read {path}: {exc}") from exc


def _reach(scopes: List[_Scope]) -> Dict[Tuple[str, str], bool]:
    """Which scopes reach :data:`PRIMITIVE`, as a fixed point by bare name."""
    by_name: Dict[str, List[_Scope]] = {}
    for s in scopes:
        by_name.setdefault(s.name, []).append(s)
    reach = {s.key: (s.path == PRIMITIVE_PATH and s.qualname == PRIMITIVE)
             or PRIMITIVE in s.refs for s in scopes}
    changed = True
    while changed:
        changed = False
        for s in scopes:
            if reach[s.key]:
                continue
            for ref in s.refs:
                defs = by_name.get(ref)
                if defs and all(reach[d.key] for d in defs):
                    reach[s.key] = True
                    changed = True
                    break
    return reach


def _findings(scopes: List[_Scope]) -> List[Finding]:
    reach = _reach(scopes)
    return [Finding(s.path, s.qualname) for s in scopes
            if MASK_NAME_RE.search(s.name)
            and not (s.path == PRIMITIVE_PATH and s.qualname == PRIMITIVE)
            and not reach[s.key]]


def scan_sources(sources: Dict[str, str]) -> List[Finding]:
    """Findings over ``{relpath: source}`` (for tests). The primitive's module
    need not be among them; a direct call by name still counts."""
    scopes: List[_Scope] = []
    for rel, src in sorted(sources.items()):
        scopes.extend(_scopes(_parse(src, rel), rel))
    return _findings(scopes)


def _repo_scopes(root: Optional[Path] = None) -> List[_Scope]:
    root = root or repo_root()
    scopes: List[_Scope] = []
    for p in iter_scanned_files(root):
        rel = p.relative_to(root).as_posix()
        scopes.extend(_scopes(_parse(_read(p), rel), rel))
    if not any(s.path == PRIMITIVE_PATH and s.qualname == PRIMITIVE for s in scopes):
        raise MaskPrimitiveLintError(
            f"the primitive {PRIMITIVE_PATH}::{PRIMITIVE} was not found -- a "
            f"scan with no primitive would read every mask as unreachable")
    return scopes


def scan_repo(root: Optional[Path] = None) -> List[Finding]:
    return _findings(_repo_scopes(root))


def fence_breaks(root: Optional[Path] = None) -> List[str]:
    """Every :data:`FENCED` masker that is missing or no longer calls the
    primitive directly."""
    by_key = {s.key: s for s in _repo_scopes(root)}
    problems = []
    for key in FENCED:
        s = by_key.get(key)
        if s is None:
            problems.append(f"{key[0]}::{key[1]}: fenced masker not found")
        elif PRIMITIVE not in s.refs:
            problems.append(f"{key[0]}::{key[1]}: no longer calls {PRIMITIVE}() "
                            f"directly")
    return problems


def load_allowlist(path: Path = ALLOWLIST_PATH) -> Dict[Tuple[str, str], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MaskPrimitiveLintError(f"cannot read allowlist {path}: {exc}") from exc
    entries = data.get("functions") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise MaskPrimitiveLintError(f"allowlist {path} has no 'functions' list")
    out: Dict[Tuple[str, str], dict] = {}
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)
                and isinstance(e.get("qualname"), str)
                and e.get("kind") in KINDS
                and isinstance(e.get("reason"), str) and e["reason"].strip()):
            raise MaskPrimitiveLintError(f"malformed allowlist entry: {e!r}")
        key = (e["path"], e["qualname"])
        if key in out:
            raise MaskPrimitiveLintError(f"duplicate allowlist entry: {key}")
        out[key] = e
    return out


def drift(findings: List[Finding],
          allow: Dict[Tuple[str, str], dict]) -> Tuple[List[str], List[str]]:
    """(new violations, stale allowlist entries)."""
    found = {f.key for f in findings}
    new = [f"{p}::{q} is a mask function that does not reach {PRIMITIVE}() and "
           f"is not allowlisted" for p, q in sorted(found - set(allow))]
    stale = [f"{p}::{q} is allowlisted but no longer needs to be (it reaches the "
             f"primitive, or is gone) -- delete its entry (the list only shrinks)"
             for p, q in sorted(set(allow) - found)]
    return new, stale


def main() -> int:
    try:
        findings = scan_repo()
        allow = load_allowlist()
        breaks = fence_breaks()
    except MaskPrimitiveLintError as exc:
        print(f"mask-primitive lint: CANNOT RUN -- {exc}", file=sys.stderr)
        return 2
    new, stale = drift(findings, allow)
    for line in breaks + new + stale:
        print(line)
    if breaks or new or stale:
        print(f"\n{len(breaks)} broken fence(s), {len(new)} new violation(s), "
              f"{len(stale)} stale allowlist entr(y/ies)")
        print(SCOPE_NOTE)
        return 1
    print(f"mask-primitive lint: clean -- {len(findings)} allowlisted mask "
          f"function(s), {len(FENCED)} fence pin(s)")
    print(SCOPE_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
