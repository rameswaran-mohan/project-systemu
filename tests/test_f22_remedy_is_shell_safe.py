"""F22 — a printed remedy must be runnable in the shell the operator is using.

Found on a cold install of the v0.10.23 wheel. `systemu daemon start` without
the dashboard extra refuses honestly and prints:

    pip install systemu[dashboard]

Square brackets are glob metacharacters. In bash that happens to survive when no
file matches; in **zsh — the default shell on macOS since Catalina — it does
not**:

    zsh: no matches found: systemu[dashboard]

So the one line we hand a blocked user does nothing on a large fraction of
target machines. The README already gets this right (`pip install
"systemu[dashboard]"`); only the runtime strings were unquoted.

This is the same defect class the packet that introduced these messages already
hit once from the other direction: Rich parsed `[browser]` as a style tag and
deleted it outright. Both are "the remedy we printed is not the remedy" — DEC-34
applied to product copy, which the project rules is a defect in its own right.

The fence is quantified over every install command in every user-facing string,
so a new group or a new message cannot reintroduce it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PACKAGES = ("systemu", "sharing_on")

# `pip install <token>[<extras>]` where <token>[ is NOT preceded by a quote.
_UNQUOTED_EXTRA = re.compile(r'pip install\s+(?!["\'])([A-Za-z0-9_.-]+)\[')


def _iter_strings():
    """Every string literal that could reach an operator, with file and line.

    DOCSTRINGS ARE EXCLUDED. They are developer-facing prose — this module's own
    docstrings discuss ``pip install systemu[...]`` while explaining the design,
    and flagging those would be noise that trains the next reader to silence the
    fence. Only strings the program can print are in scope.
    """
    for pkg in _PACKAGES:
        for path in (_REPO / pkg).rglob("*.py"):
            if "test" in path.name:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:  # pragma: no cover
                continue
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None) or []
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings):
                    yield path, node.lineno, node.value


def test_no_user_facing_string_prints_an_unquoted_extras_install():
    offenders = []
    for path, lineno, text in _iter_strings():
        for m in _UNQUOTED_EXTRA.finditer(text):
            offenders.append(
                f"{path.relative_to(_REPO)}:{lineno}: {m.group(0)}… "
                f"-> zsh: no matches found: {m.group(1)}[…]"
            )
    assert not offenders, (
        "these remedies print an install command with UNQUOTED extras. Square "
        "brackets are glob metacharacters; zsh (the macOS default shell) refuses "
        'the line outright. Quote the argument: pip install "systemu[dashboard]".\n  '
        + "\n  ".join(offenders)
    )


# install_command takes PACKAGE names and resolves them to extras — not extra
# names. Passing "dashboard" silently returns "" (no such package), which would
# make every assertion below vacuously true, so the expected extra is asserted.
_PKG_TO_EXTRA = [(["nicegui"], "dashboard"),
                 (["playwright"], "browser"),
                 (["nicegui", "playwright"], "dashboard")]


@pytest.mark.parametrize("packages,expected_extra", _PKG_TO_EXTRA)
def test_the_builder_quotes_what_it_returns(packages, expected_extra):
    """The single place that composes the remedy must produce a safe line."""
    from systemu.runtime.optional_deps import install_command

    cmd = install_command(packages)
    assert cmd, f"no remedy produced for {packages!r} — the test would be vacuous"
    assert expected_extra in cmd, f"wrong extra for {packages!r}: {cmd!r}"
    assert not _UNQUOTED_EXTRA.search(cmd), f"unquoted: {cmd!r}"


@pytest.mark.parametrize("bracket", ["[", "]"])
def test_the_quoting_actually_survives_a_shell_split(bracket):
    """Not just 'has quotes' — the token must round-trip through shlex as ONE
    argument still containing the bracket, which is what pip receives."""
    import shlex

    from systemu.runtime.optional_deps import install_command

    cmd = install_command(["nicegui"])
    assert cmd, "no remedy produced — the test would be vacuous"
    parts = shlex.split(cmd)
    target = [p for p in parts if "dashboard" in p]
    assert len(target) == 1, f"extras token did not survive shlex: {parts}"
    assert bracket in target[0], (
        f"shlex stripped {bracket!r} from the package token: {target[0]!r}"
    )
    assert target[0].startswith("systemu["), target[0]
