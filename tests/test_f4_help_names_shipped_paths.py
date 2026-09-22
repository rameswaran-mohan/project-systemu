"""F4 — user-facing instructions must not name files the wheel does not ship.

`.env.example` lives at the REPO ROOT, outside every package directory, and
there is no MANIFEST.in. A user who ran `pip install systemu` therefore has no
such file, so "Copy .env.example to .env" is an instruction they cannot follow.

This is the DEC-34 class applied to product copy: a false assertion about how
to use the software is a defect exactly as a false assertion in a docstring is.

The fence is class-level: it scans the shipped packages for any user-facing
string naming a repo-root-only file, so a NEW such instruction added tomorrow
fails too — not just the three sites fixed today.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SHIPPED_PACKAGES = ("sharing_on", "systemu")

# Files that exist at the repo root but are NOT inside any shipped package and
# are not declared in package-data — a wheel user never receives these.
_REPO_ONLY_FILES = (".env.example",)


def _shipped_python_files():
    for pkg in _SHIPPED_PACKAGES:
        yield from (_REPO / pkg).rglob("*.py")


def _string_constants(path: Path):
    """Every string literal in the module, with its line number."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:  # pragma: no cover - a broken file is a different test's problem
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield getattr(node, "lineno", 0), doc


def test_env_example_is_genuinely_not_shipped():
    """Guard the premise. If .env.example ever DOES ship, this test must be
    revisited rather than silently protecting a rule that no longer applies."""
    assert (_REPO / ".env.example").exists(), "fixture premise: the repo has one"
    for pkg in _SHIPPED_PACKAGES:
        assert not list((_REPO / pkg).rglob(".env.example")), (
            f"{pkg} now ships .env.example - revisit F4"
        )
    pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "env.example" not in pyproject, (
        "pyproject now references env.example - revisit F4"
    )


@pytest.mark.parametrize("needle", _REPO_ONLY_FILES)
def test_no_user_facing_string_instructs_using_a_repo_only_file(needle):
    offenders = []
    for path in _shipped_python_files():
        if "test" in path.name:
            continue
        for lineno, text in _string_constants(path):
            if needle not in text:
                continue
            # Only flag INSTRUCTIONS - an imperative aimed at the operator.
            if re.search(r"\b(copy|create|edit|rename|open|see|cp)\b", text, re.I):
                offenders.append(
                    f"{path.relative_to(_REPO)}:{lineno}: {text.strip()[:110]}"
                )
    assert not offenders, (
        f"user-facing text instructs the operator to use {needle}, which is NOT in the "
        "wheel - a pip user cannot follow it. Point at `sharing_on setup` instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_replacement_instruction_names_a_real_command():
    """Whatever we tell the user to run instead must actually exist.

    F23 note: this used to pin the literal string ``sharing_on setup``. That
    spelling was incidental -- the property is that the top-level ``--help``
    points a fresh install at the ``setup`` command using a program name the
    wheel INSTALLS. Pinning one of two equally-valid names would have made a
    coherence fix look like a regression, so the assertion now resolves the
    name against ``[project.scripts]`` instead. Strictly stronger: the old form
    would have passed for a name pip never puts on PATH.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    from sharing_on.cli import cli

    assert "setup" in cli.commands, "the `setup` command must exist to be recommended"
    help_text = cli.help or ""
    scripts = tomllib.loads(
        (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["scripts"]
    named = [p for p in scripts if f"{p} setup" in help_text]
    assert named, (
        f"top-level --help must point a fresh install at `<program> setup` using "
        f"an installed console script {sorted(scripts)}; it says:\n{help_text}"
    )
