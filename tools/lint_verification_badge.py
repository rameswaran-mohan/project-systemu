"""AST lint: every DEC-13 badge surface consumes the ONE mint (DEC-43).

Usage:
    python -m tools.lint_verification_badge     # exit 1 on any finding

**Why.** The verified / claimed / measured badge was decided in four places --
the receipt store's badge list, the ledger's effect-row join, the ledger's
committed-effect filter and the IMPL-7 committed-effects render -- each reading
a receipt's confirmation bits with its own recipe. They agreed by coincidence.
``runtime.verification_badge`` is now the one mint; this lint keeps it that way.

**What it checks.** Every scope in ``verification_badge.SURFACES`` (a file, or
``file::function``) must:

  1. never spell a badge state itself -- a string constant equal to one of
     ``VERDICT_TOKENS`` (any case), or the filled-check icon ``VERIFIED_ICON``;
  2. never read the mint's private inputs -- ``x["confirmed"]``,
     ``x.get("shadow")``, ``x.confirmed`` and so on (``INPUT_KEYS``);
  3. never read the receipt store behind the mint's back
     (``read_receipts`` / ``read_receipts_witnessed``);
  4. reference the mint at least once (one of ``CONSUMER_NAMES``) -- a surface
     that quietly stops rendering the badge fails here.

The pattern set is generated from the mint's own registry, not copied. Enforcing
from day one: there is no baseline, because the tree has no finding (DEC-43(3)).

**What it cannot see** (:data:`SCOPE_NOTE`): ``failed`` and ``not_recorded`` are
also a run status and the ledger's DEC-27 cell, so spelling cannot tell a
re-derivation from a status check for those two; a proxy computed from some
other field (a status string, a log line) without touching the receipt keys. The
agreement test (``tests/test_dec13_badge_surface_agreement.py``) checks
BEHAVIOUR over the fixture matrix and catches those: this lint checks STRUCTURE.
FAIL LOUD: an unparseable file, a missing surface file or function, or an empty
registry raises :class:`BadgeLintError` -- never a clean result.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

SCOPE_NOTE = (
    "verification-badge lint scope: each surface in verification_badge.SURFACES "
    "must not spell a verdict token or the filled-check icon, must not read a "
    "receipt's confirmation keys or the receipt store, and must reference the mint.\n"
    "  It CANNOT see: 'failed' / 'not_recorded' (shared with run statuses and the "
    "ledger's DEC-27 cell), or a proxy derived from some other field. The "
    "agreement test covers behaviour; this lint covers structure."
)

#: Store readers a surface may not call: the receipts reach a surface minted.
FORBIDDEN_READERS = frozenset({"read_receipts", "read_receipts_witnessed"})

#: The shared component a surface may consume instead of the mint directly; it is
#: itself a surface in the registry, so it is held to the same rules.
COMPONENT_NAMES = frozenset({"badge_model_for", "render_badge", "receipt_panel_model",
                             "render_receipt_panel", "open_receipt_dialog",
                             "render_committed_effects_list"})


class BadgeLintError(RuntimeError):
    """The lint could not run. Never confuse this with a clean result."""


@dataclass(frozen=True)
class Finding:
    path: str
    scope: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}::{self.scope}:{self.line}: {self.message}"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _mint():
    from systemu.runtime import verification_badge as vb
    return vb


#: The mint's entry points: referencing one of these (or importing the mint) is
#: consuming it. ``view`` is deliberately absent -- the name is too common to
#: count as evidence on its own.
MINT_NAMES = frozenset({"run_badge", "effect_record", "mint_effect", "mint_effects",
                        "badge_text", "effect_lines", "badge_from_record"})


def consumer_names() -> Set[str]:
    return set(MINT_NAMES) | set(COMPONENT_NAMES) | {"verification_badge"}


def registry() -> List[Tuple[str, str, Optional[str]]]:
    """``(surface id, path, function or None)`` for every registered surface."""
    vb = _mint()
    out = []
    for sid, spec in vb.SURFACES:
        path, _, fn = spec.partition("::")
        out.append((sid, path, fn or None))
    if not out:
        raise BadgeLintError("verification_badge.SURFACES is empty: nothing to check")
    return out


def _docstring_ids(tree: ast.AST) -> Set[int]:
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _scope_node(tree: ast.Module, function: Optional[str], path: str) -> ast.AST:
    if function is None:
        return tree
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == function:
            return node
    raise BadgeLintError(f"{path}: surface function {function!r} not found")


def _key_of(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def find_violations(source: str, path: str, *,
                    function: Optional[str] = None) -> List[Finding]:
    """Every finding in one surface scope. Raises on an unparseable source."""
    vb = _mint()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise BadgeLintError(f"{path}: cannot parse ({exc})") from exc
    scope = _scope_node(tree, function, path)
    scope_name = function or "<module>"
    docs = _docstring_ids(tree)
    # A field NAME is not a verdict: ``{"verified": <minted bool>}`` (the receipt
    # badge list's long-standing key) and ``row["verified"]`` name a field; the
    # value is what matters, and rule 2 catches a value read off the receipt.
    docs |= {id(k) for n in ast.walk(scope) if isinstance(n, ast.Dict)
             for k in n.keys if k is not None}
    docs |= {id(n.slice) for n in ast.walk(scope) if isinstance(n, ast.Subscript)}
    tokens = {t.lower() for t in vb.VERDICT_TOKENS}
    inputs = set(vb.INPUT_KEYS)
    consumers = consumer_names()
    out: List[Finding] = []
    consumed = False

    def add(node: ast.AST, msg: str) -> None:
        out.append(Finding(path, scope_name, getattr(node, "lineno", 0), msg))

    for node in ast.walk(scope):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            v = node.value.strip()
            if v.lower() in tokens:
                add(node, f"spells the badge state {v!r} itself; render "
                          "verification_badge.view(<minted value>) instead")
            elif v == vb.VERIFIED_ICON:
                add(node, f"uses the verified badge's filled check {v!r} directly")
        elif isinstance(node, ast.Subscript):
            key = _key_of(node.slice)
            if key in inputs:
                add(node, f"reads the mint's private input [{key!r}]")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and node.args:
            key = _key_of(node.args[0])
            if key in inputs:
                add(node, f"reads the mint's private input .get({key!r})")
        if isinstance(node, ast.Attribute):
            if node.attr in inputs:
                add(node, f"reads the mint's private input .{node.attr}")
            if node.attr in FORBIDDEN_READERS:
                add(node, f"reads the receipt store ({node.attr}) behind the mint")
            if node.attr in consumers:
                consumed = True
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_READERS:
                add(node, f"reads the receipt store ({node.id}) behind the mint")
            if node.id in consumers:
                consumed = True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            mod = getattr(node, "module", None) or ""
            if "verification_badge" in mod or any("verification_badge" in n for n in names) \
                    or any(n in consumers for n in names):
                consumed = True
            for n in names:
                if n in FORBIDDEN_READERS:
                    add(node, f"imports the receipt store reader {n} behind the mint")
    if not consumed:
        out.append(Finding(path, scope_name, getattr(scope, "lineno", 1),
                           "never references the verification-badge mint: this "
                           "surface no longer renders the minted badge"))
    return out


def iter_scopes(root: Optional[Path] = None) -> Iterable[Tuple[str, Path, Optional[str]]]:
    base = root or repo_root()
    for sid, path, fn in registry():
        p = base / path
        if not p.is_file():
            raise BadgeLintError(f"surface {sid}: {path} does not exist")
        yield sid, p, fn


def scan_repo(root: Optional[Path] = None) -> List[Finding]:
    base = root or repo_root()
    found: List[Finding] = []
    scanned = 0
    for _sid, p, fn in iter_scopes(base):
        rel = p.relative_to(base).as_posix()
        found.extend(find_violations(p.read_text(encoding="utf-8"), rel, function=fn))
        scanned += 1
    if not scanned:
        raise BadgeLintError("no surface was scanned")
    return found


def main(argv: Optional[List[str]] = None) -> int:
    try:
        findings = scan_repo()
    except BadgeLintError as exc:
        print(f"verification-badge lint could not run: {exc}", file=sys.stderr)
        return 2
    for f in findings:
        print(str(f).encode("ascii", "backslashreplace").decode("ascii"))
    print(SCOPE_NOTE)
    if findings:
        print(f"{len(findings)} finding(s): a surface computes the badge itself.")
        return 1
    print(f"clean: {len(registry())} surfaces consume the one mint.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
