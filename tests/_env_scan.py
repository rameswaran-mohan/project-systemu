"""AST scan of every environment-variable access in ``systemu/`` and ``sharing_on/``.

Shared by the R-4 witnesses (``tests/test_reserved_env_names.py``). Not a test
module itself (leading underscore). Pure: parses source, imports nothing it scans.

An ACCESS is ``os.environ.get / .setdefault / .pop (X)``, ``os.environ[X]``
(load, store or delete), ``X in os.environ``, ``os.getenv(X)`` /
``os.putenv(X, ...)`` / ``os.unsetenv(X)``, ``os.environ.update(...)`` and
``os.environ.clear()``. ``X`` is resolved to a NAME when it is a string literal
or a module-level string constant; to a covered PREFIX when it is a ``+`` or
f-string whose literal head starts ``SYSTEMU_`` / ``SHARING_ON_``; otherwise the
access is DYNAMIC and is reported by site (``path::function::expression``) so a
test can pin every dynamic site and what it can name.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("systemu", "sharing_on")
COVERED_PREFIX_HEADS = ("SYSTEMU_", "SHARING_ON_")


@dataclass
class Access:
    path: str
    function: str
    expr: str
    name: str = ""          # resolved name ("" when dynamic)
    write: bool = False
    dynamic: bool = False
    prefix: str = ""        # covered prefix head, when that is all we know

    @property
    def site(self) -> str:
        return f"{self.path}::{self.function}::{self.expr}"


def _is_environ(node: ast.AST) -> bool:
    return ((isinstance(node, ast.Attribute) and node.attr == "environ")
            or (isinstance(node, ast.Name) and node.id == "environ"))


def _module_consts(tree: ast.Module) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n.value.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str) and isinstance(n.target, ast.Name):
            out[n.target.id] = n.value.value
    return out


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, consts: Dict[str, str]) -> None:
        self.path, self.consts = path, consts
        self.stack: List[str] = []
        self.out: List[Access] = []

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def _record(self, arg: ast.AST, *, write: bool) -> None:
        func = ".".join(self.stack) or "<module>"
        expr = ast.unparse(arg)
        acc = Access(self.path, func, expr, write=write)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            acc.name = arg.value
        elif isinstance(arg, ast.Name) and arg.id in self.consts:
            acc.name = self.consts[arg.id]
        else:
            head = None
            if isinstance(arg, ast.BinOp) and isinstance(arg.left, ast.Constant) \
                    and isinstance(arg.left.value, str):
                head = arg.left.value
            elif isinstance(arg, ast.JoinedStr) and arg.values \
                    and isinstance(arg.values[0], ast.Constant):
                head = str(arg.values[0].value)
            if head is not None and head.upper().startswith(COVERED_PREFIX_HEADS):
                acc.prefix = head
            else:
                acc.dynamic = True
        self.out.append(acc)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute) and _is_environ(f.value):
            if f.attr in ("get", "setdefault", "pop") and node.args:
                self._record(node.args[0], write=f.attr != "get")
            elif f.attr in ("update", "clear"):
                func = ".".join(self.stack) or "<module>"
                self.out.append(Access(self.path, func, f"environ.{f.attr}(...)",
                                       write=True, dynamic=True))
        elif (isinstance(f, ast.Attribute) and f.attr in ("getenv", "putenv", "unsetenv")) \
                or (isinstance(f, ast.Name) and f.id in ("getenv", "putenv", "unsetenv")):
            attr = f.attr if isinstance(f, ast.Attribute) else f.id
            if node.args:
                self._record(node.args[0], write=attr != "getenv")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_environ(node.value):
            self._record(node.slice, write=isinstance(node.ctx, (ast.Store, ast.Del)))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) \
                and any(_is_environ(c) for c in node.comparators):
            self._record(node.left, write=False)
        self.generic_visit(node)


def scan(root: Path = ROOT) -> List[Access]:
    """Every environment access in the scanned trees. Raises on unparseable
    source (a lint that skips a file it cannot read is a lint that passes)."""
    out: List[Access] = []
    for d in SCAN_DIRS:
        for py in sorted((root / d).rglob("*.py")):
            rel = py.relative_to(root).as_posix()
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
            v = _Visitor(rel, _module_consts(tree))
            v.visit(tree)
            out.extend(v.out)
    return out
