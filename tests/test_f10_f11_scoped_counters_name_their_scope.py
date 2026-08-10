"""F10/F11 — a counter scoped to a subset must SAY which subset, on screen.

Both defects came out of a human walkthrough of the dashboard, and neither is a
counting bug — every number was correct. The bug is that the number is rendered
without the scope that makes it true, so it reads as contradicting a different,
correctly-scoped number somewhere else.

F10  Shadows renders three bare cards "Queued (0) / Running (0) / Completed (0)".
     They count JobManager Execute jobs — work started FROM THE DASHBOARD. A task
     run via `chat submit` never enters JobManager, so "Completed (0)" sat next to
     a Work page showing that same run as COMPLETED. The panel's scope existed
     only in a Python docstring, which no user can read.

F11  Insights shows "Last run: never" (the consolidation SCHEDULER's own metadata)
     directly above a table column "LAST CONSOLIDATED: 2026-08-06 21:10" (parsed
     per-shadow from ELDER_MEMORY.md). Different facts, adjacent, both reading as
     "when did consolidation last happen".

The fence is source-level on purpose: these are render-time labels, and asserting
on the committed render code is what fails if someone deletes the heading again.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_ARMY = _REPO / "systemu" / "interface" / "pages" / "army.py"
_MEMPAGE = _REPO / "systemu" / "interface" / "pages" / "memory_consolidation_page.py"


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name}: function {name!r} not found")


def _rendered_strings(fn: ast.FunctionDef) -> list[str]:
    """String literals the function renders, EXCLUDING its docstring.

    The docstring is exactly what F10 relied on, so it must not count.
    """
    doc = ast.get_docstring(fn, clean=False)
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if doc is not None and node.value == doc:
                continue
            out.append(node.value)
    return out


def test_f10_execute_jobs_counters_render_their_scope():
    fn = _function_node(_ARMY, "_render_execute_jobs_panel")
    strings = _rendered_strings(fn)

    counters = [s for s in strings if s in ("Queued", "Running", "✓ Completed")]
    assert counters, "fixture premise: this panel renders the three counter labels"

    scoped = [s for s in strings if re.search(r"execute\s+job", s, re.I)]
    assert scoped, (
        "the Queued/Running/Completed counters on Shadows count ONLY dashboard-"
        "initiated Execute jobs, but the panel renders no visible heading saying so "
        "— the scope lives in the docstring, which a user cannot read. A CLI-run "
        "task then shows as Completed on Work and 0 here, reading as a contradiction. "
        f"Rendered strings were: {sorted(set(strings))[:20]}"
    )


def test_f11_consolidation_scheduler_stat_distinguishes_itself_from_per_shadow():
    """'Last run' (the scheduler job) sits above 'LAST CONSOLIDATED' (per shadow).
    At least one of the two must name what it is scoped to."""
    src = _MEMPAGE.read_text(encoding="utf-8")

    # Key on the VARIABLE, not the label — the label is exactly what this test
    # constrains, so matching on its text would make the test vacuous the moment
    # it is renamed (which is the fix).
    m = re.search(r'_sched_stat\(\s*"([^"]+)"\s*,\s*last_run\s*\)', src)
    assert m, (
        "fixture premise: the scheduler panel renders a stat fed by `last_run`"
    )
    label = m.group(1)

    assert re.search(r"sweep|job|scheduler|pass", label, re.I), (
        "'Last run' is the consolidation SCHEDULER's own metadata, but it renders "
        "directly above a per-shadow 'LAST CONSOLIDATED' column that reads as the "
        "same fact — I saw 'Last run: never' above 'LAST CONSOLIDATED 2026-08-06 "
        "21:10' on one screen. The label must name its scope. "
        f"Got: {label!r}"
    )


@pytest.mark.parametrize("path,fn_name", [(_ARMY, "_render_execute_jobs_panel")])
def test_scope_heading_is_not_merely_a_comment(path, fn_name):
    """A comment is not a fence either — it must be a rendered string."""
    fn = _function_node(path, fn_name)
    assert any(re.search(r"execute\s+job", s, re.I) for s in _rendered_strings(fn)), (
        "the scope must be a rendered string literal, not a comment or docstring"
    )
