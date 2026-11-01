"""AST lint: every tool list a prompt payload carries comes from the ONE selection
view (R-CAP1 · CAP-4, DEC-43).

Usage:
    python -m tools.lint_tool_view     # exit 1 on drift from the baseline

**Why.** CAP-4 trims every tool-consuming prompt to the top-K full records plus a
one-line index. That only holds if no builder assembles its own tool list into a
payload: one bypass and that prompt is back to the whole catalog in full (or to a
silently short one). So the view is minted once -- ``capability_view.build_view``
-- and this lint keeps it that way.

**What it checks** (every ``.py`` under ``systemu/``), for each TOOL-LIST KEY
(:data:`TOOL_LIST_KEYS`) written as

  * a dict-literal key           ``{"tools": <value>}``
  * a subscript store            ``payload["available_tools"] = <value>``
  * a call keyword               ``build(available_tools=<value>)``
  * an in-place mutation         ``payload["tools"].append(...)``  (always a violation)

the value must be the view's own accessor -- a call to ``.full_records()`` -- or
the empty list ``[]`` (stripping every tool, e.g. the loop guard's force-finalize).
Anything else is a violation keyed ``(path, scope, key)`` with a count.

**Baseline, enforced both ways (DEC-32c).** Sites that are NOT prompt tool lists
(an MCP listing result, an entity inventory, a candidate gatherer the view then
consumes) are listed in ``tools/tool_view_baseline.json`` with a ``kind``, a
``reason`` and their exact ``count``. A new violation fails; so does a stale or
miscounted entry -- the list can only shrink.

**Mint pins.** :data:`REQUIRED_MINTS` names each production prompt builder; each
must still call ``build_view`` directly. Deleting the view from a builder fails
the lint even if the builder now writes its tools under a key this lint does not
know.

**What it cannot see** (:data:`SCOPE_NOTE`): a tool list written under a key
outside :data:`TOOL_LIST_KEYS`; a list built through ``setattr``/``dict(...)``
calls; whether a ``.full_records()`` call belongs to a view of the RIGHT
candidates. The behavioural pins in ``tests/test_cap4_selection_view.py`` cover the
last one per builder. FAIL LOUD: an unparseable file or an empty scan raises.
"""
from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

#: Payload keys that carry a tool list into a prompt.
TOOL_LIST_KEYS = frozenset({
    "tools", "available_tools", "existing_tools", "current_tool_catalog",
    "tools_available", "enabled_tools", "more_tools",
})

#: The view accessor whose value is compliant.
ACCESSORS = frozenset({"full_records"})

#: In-place list mutations that assemble a tool list outside the view.
MUTATORS = frozenset({"append", "extend", "insert"})

#: (path, function qualname) of every production prompt builder; each must call
#: ``build_view`` itself.
REQUIRED_MINTS: Tuple[Tuple[str, str], ...] = (
    ("systemu/pipelines/quick_task.py", "run_quick_task"),
    ("systemu/runtime/shadow_runtime.py", "_cap_view_for_iteration"),
    ("systemu/pipelines/activity_extractor.py", "extract_and_process"),
    ("systemu/pipelines/skill_recalibrator.py", "recalibrate_skill"),
    ("systemu/pipelines/scroll_validator.py", "validate_scroll"),
    ("systemu/pipelines/refinery.py", "process_execution_result"),
)
MINT = "build_view"

BASELINE_PATH = Path(__file__).resolve().parent / "tool_view_baseline.json"

KINDS = frozenset({
    "not-a-prompt",      # a data structure that never reaches an LLM prompt
    "inventory",         # an entity inventory in a prompt, not a capability selection
    "candidate-gatherer",  # builds the candidates the view then consumes
    "pass-through",      # forwards a parameter its caller filled from the view
    "names-roster",      # a names-only roster, already at index granularity
})

SCOPE_NOTE = (
    "scope: TOOL_LIST_KEYS dict keys / subscript stores / call keywords / in-place "
    "mutations under systemu/. Not seen: other key names, setattr/dict() "
    "construction, and whether a .full_records() view was built from the right "
    "candidates (behavioural pins cover that per builder).")


class ToolViewLintError(RuntimeError):
    pass


@dataclass(frozen=True)
class Finding:
    path: str
    scope: str
    key: str
    line: int
    what: str

    @property
    def ident(self) -> Tuple[str, str, str]:
        return (self.path, self.scope, self.key)


def _const_str(node: Optional[ast.AST]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _compliant(value: ast.AST) -> bool:
    if isinstance(value, ast.List) and not value.elts:
        return True
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and value.func.attr in ACCESSORS)


def _subscript_key(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Subscript):
        return _const_str(node.slice)
    return None


def find_violations(source: str, rel: str) -> List[Finding]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        raise ToolViewLintError(f"cannot parse {rel}: {exc}") from exc
    out: List[Finding] = []

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = node.name if scope == "<module>" else f"{scope}.{node.name}"
            for child in ast.iter_child_nodes(node):
                visit(child, inner)
            return
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                key = _const_str(k)
                if key in TOOL_LIST_KEYS and not _compliant(v):
                    out.append(Finding(rel, scope, key, getattr(k, "lineno", 0), "dict key"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                key = _subscript_key(t)
                if key in TOOL_LIST_KEYS and (
                        isinstance(node, ast.AugAssign) or node.value is None
                        or not _compliant(node.value)):
                    out.append(Finding(rel, scope, key, node.lineno, "subscript store"))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in TOOL_LIST_KEYS and not _compliant(kw.value):
                    out.append(Finding(rel, scope, kw.arg, node.lineno, "call keyword"))
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in MUTATORS:
                key = _subscript_key(f.value)
                if key in TOOL_LIST_KEYS:
                    out.append(Finding(rel, scope, key, node.lineno,
                                       f"in-place .{f.attr}()"))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, "<module>")
    return out


def _calls_mint(source: str, rel: str, qualname: str) -> Optional[bool]:
    """True/False whether ``qualname`` calls :data:`MINT`; None if not found."""
    tree = ast.parse(source, filename=rel)

    def walk(node: ast.AST, scope: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = child.name if not scope else f"{scope}.{child.name}"
                if inner == qualname:
                    return any(
                        isinstance(n, ast.Call) and (
                            (isinstance(n.func, ast.Attribute) and n.func.attr == MINT)
                            or (isinstance(n.func, ast.Name) and n.func.id == MINT))
                        for n in ast.walk(child))
                got = walk(child, inner)
                if got is not None:
                    return got
        return None

    return walk(tree, "")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def iter_scanned_files(root: Optional[Path] = None) -> Iterator[Path]:
    root = root or repo_root()
    pkg = root / "systemu"
    if not pkg.is_dir():
        raise ToolViewLintError(f"no systemu/ package under {root}")
    files = sorted(pkg.rglob("*.py"))
    if not files:
        raise ToolViewLintError(f"no .py files under {pkg}")
    yield from files


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise ToolViewLintError(f"cannot read {path}: {exc}") from exc


def scan_repo(root: Optional[Path] = None) -> List[Finding]:
    root = root or repo_root()
    out: List[Finding] = []
    for p in iter_scanned_files(root):
        rel = p.relative_to(root).as_posix()
        out.extend(find_violations(_read(p), rel))
    return out


def mint_breaks(root: Optional[Path] = None) -> List[str]:
    root = root or repo_root()
    problems = []
    for rel, qual in REQUIRED_MINTS:
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}::{qual}: builder file not found")
            continue
        got = _calls_mint(_read(path), rel, qual)
        if got is None:
            problems.append(f"{rel}::{qual}: builder not found")
        elif not got:
            problems.append(f"{rel}::{qual}: no longer calls {MINT}() -- the prompt "
                            "builder bypasses the selection view")
    return problems


def load_baseline(path: Path = BASELINE_PATH) -> Dict[Tuple[str, str, str], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolViewLintError(f"cannot read baseline {path}: {exc}") from exc
    entries = data.get("sites") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ToolViewLintError(f"baseline {path} has no 'sites' list")
    out: Dict[Tuple[str, str, str], dict] = {}
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)
                and isinstance(e.get("scope"), str) and e.get("key") in TOOL_LIST_KEYS
                and type(e.get("count")) is int and e["count"] > 0
                and e.get("kind") in KINDS
                and isinstance(e.get("reason"), str) and e["reason"].strip()):
            raise ToolViewLintError(f"malformed baseline entry: {e!r}")
        ident = (e["path"], e["scope"], e["key"])
        if ident in out:
            raise ToolViewLintError(f"duplicate baseline entry: {ident}")
        out[ident] = e
    return out


def drift(findings: List[Finding],
          baseline: Dict[Tuple[str, str, str], dict]) -> Tuple[List[str], List[str]]:
    """(new or over-count violations, stale or over-counted baseline entries)."""
    counts: Dict[Tuple[str, str, str], int] = {}
    lines: Dict[Tuple[str, str, str], List[int]] = {}
    for f in findings:
        counts[f.ident] = counts.get(f.ident, 0) + 1
        lines.setdefault(f.ident, []).append(f.line)
    new, stale = [], []
    for ident in sorted(counts):
        want = baseline.get(ident, {}).get("count", 0)
        if counts[ident] > want:
            p, s, k = ident
            new.append(f"{p}::{s} writes tool-list key {k!r} from something other "
                       f"than the selection view ({counts[ident]} site(s), baseline "
                       f"{want}; lines {lines[ident]})")
    for ident in sorted(baseline):
        have = counts.get(ident, 0)
        if have < baseline[ident]["count"]:
            p, s, k = ident
            stale.append(f"{p}::{s} key {k!r}: baseline says {baseline[ident]['count']} "
                         f"site(s), found {have} -- lower or delete the entry (the "
                         f"baseline only shrinks)")
    return new, stale


def main() -> int:
    try:
        findings = scan_repo()
        baseline = load_baseline()
        breaks = mint_breaks()
    except ToolViewLintError as exc:
        print(f"tool-view lint: CANNOT RUN -- {exc}", file=sys.stderr)
        return 2
    new, stale = drift(findings, baseline)
    for line in breaks + new + stale:
        print(line)
    if breaks or new or stale:
        print(f"\n{len(breaks)} broken mint pin(s), {len(new)} new violation(s), "
              f"{len(stale)} stale baseline entr(y/ies)")
        print(SCOPE_NOTE)
        return 1
    print(f"tool-view lint: clean -- {len(findings)} baselined site(s), "
          f"{len(REQUIRED_MINTS)} mint pin(s)")
    print(SCOPE_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
