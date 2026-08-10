"""F28 — Rich deletes `[browser]` from the remedy the operator is told to run.

Found on a clean cold-install pass. With playwright absent, `systemu doctor`
prints TWO tables that state the same remedy, and they disagree:

    Diagnosis table            pip install "systemu"              <- WRONG
    Optional capability groups pip install "systemu[browser]"     <- right

`pip install "systemu"` is a valid command that installs nothing new, so an
operator who follows the headline remedy sees the tool stay broken and concludes
the product is lying to them.

The mint is fine — `optional_deps.install_command(["playwright"])` returns
`pip install "systemu[browser]"`. The loss happens at render: square brackets are
Rich's markup syntax, so an unescaped `[browser]` is parsed as a style tag and
DELETED. The Optional-groups table calls `_esc()` on every data cell; the
Diagnosis table (cli_commands.py, `tbl.add_row(mark, p["message"], p.get("cta"))`)
does not.

The install packet documented this exact hazard when it introduced the remedies
and escaped the paths it knew about. This is the one it missed — and it is the
most prominent one, printed at the top under "Diagnosis".

Sibling of F22 (the same remedy was unquoted, so zsh rejected it). Both are
"the remedy we printed is not the remedy" — DEC-34 applied to product copy.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CLI = _REPO / "systemu" / "interface" / "cli_commands.py"


def _render(renderable) -> str:
    """Render through a real Rich console, exactly as the terminal would."""
    from rich.console import Console

    con = Console(width=200, no_color=True, highlight=False)
    with con.capture() as cap:
        con.print(renderable)
    return cap.get()


def test_a_bracketed_remedy_survives_a_rich_table_cell():
    """The mechanism, isolated: this is what the Diagnosis table was doing."""
    from rich.markup import escape
    from rich.table import Table

    remedy = 'pip install "systemu[browser]"'

    unescaped = Table(show_header=False)
    unescaped.add_row(remedy)
    assert "[browser]" not in _render(unescaped), (
        "fixture premise: Rich is expected to eat an unescaped [browser]"
    )

    escaped = Table(show_header=False)
    escaped.add_row(escape(remedy))
    assert "[browser]" in _render(escaped)


def test_the_doctor_diagnosis_table_escapes_its_data_cells():
    """Structural: the Diagnosis rows must escape message and cta.

    Asserted on the source because the failure is invisible in any output that
    happens not to contain a bracket — a behavioural-only test would pass on a
    machine where every group is installed.
    """
    # Scoped to the fields that carry OPERATOR GUIDANCE, i.e. the ones that can
    # contain an install command and therefore a literal `[extra]`. Checking
    # every cell instead would flag ids, names and timestamps that can never hold
    # a bracket - dozens of phantoms, and a fence that cries wolf gets switched
    # off. Same reasoning as F25 comparing canonical package names.
    _GUIDANCE = ("cta", "remedy", "message", "fix", "detail", "install")

    def _names_guidance(node) -> bool:
        return any(
            isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.lower() in _GUIDANCE
            for n in ast.walk(node)
        )

    tree = ast.parse(_CLI.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_row"):
            continue
        for arg in node.args:
            if not _names_guidance(arg):
                continue
            # An escaping call ANYWHERE inside the expression counts. It is not
            # always the outermost node: `_esc(g["remedy"]) or "-"` is a BoolOp,
            # and an outermost-only check reported that correct line as a defect.
            if any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ("_esc", "escape")
                for n in ast.walk(arg)
            ):
                continue
            offenders.append((node.lineno, ast.unparse(arg)))
    assert not offenders, (
        "these Rich table cells receive operator text without escaping, so any "
        "literal [...] in them — notably `pip install \"systemu[browser]\"` — is "
        "parsed as a style tag and DELETED:\n  "
        + "\n  ".join(f"{_CLI.name}:{ln}: {src}" for ln, src in offenders)
    )


def test_the_two_doctor_tables_state_the_same_remedy():
    """Behavioural: whatever the mint returns must reach the terminal intact in
    BOTH tables. Renders the real report through a real console."""
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    from systemu.runtime.optional_deps import install_command

    remedy = install_command(["playwright"])
    assert "[browser]" in remedy, f"premise: the mint emits the extra ({remedy!r})"

    # Diagnosis-shaped row (the surface that was wrong)
    diag = Table(show_header=False)
    diag.add_row("[yellow]!(/yellow]".replace("(", "["), escape(remedy))
    assert "[browser]" in _render(diag), "the Diagnosis cell must keep the extra"

    # Optional-groups-shaped row (the surface that was already right)
    grp = Table(show_header=False)
    grp.add_row(escape(remedy))
    assert "[browser]" in _render(grp)


@pytest.mark.parametrize("hostile", ['pip install "systemu[browser]"',
                                     "systemu[dashboard]",
                                     "[bold]not a style[/bold]"])
def test_escape_is_lossless_for_anything_bracket_shaped(hostile):
    from rich.markup import escape
    from rich.table import Table

    t = Table(show_header=False)
    t.add_row(escape(hostile))
    out = _render(t)
    for token in re.findall(r"\[[^\]]+\]", hostile):
        assert token in out, f"{token!r} was eaten from {hostile!r}: {out!r}"
