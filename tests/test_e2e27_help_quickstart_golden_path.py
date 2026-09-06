"""D7 -- `systemu --help` Quick start must lead with the golden path.

THE DEFECT
    The root help's Quick start block read

        1. Run:  systemu setup      (stores your OpenRouter API key)
        2. Run:  systemu record --name "My Task"

    while `start` is registered in this very module as "the one-command golden
    path ... the first command a fresh install types", and the README's own
    Quick start is `pip install "systemu[dashboard]"` then `systemu start`.
    The first surface a fresh install reads therefore pointed AWAY from the
    path the product is built and documented around: an operator who follows
    `--help` literally never sees the dashboard at all.

THE PROPERTY
    The Quick start block names `systemu start` -- before it names
    `systemu record` -- and says, in the same block, that the dashboard comes
    from the `systemu[dashboard]` install extra, because `start` opening a
    dashboard is exactly what a bare `pip install systemu` cannot do.
    `setup` and `record` keep their places behind it.

Rendered help only; nothing here starts a daemon, opens a vault or touches the
network.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from sharing_on.cli import cli


def _help_text() -> str:
    """The root help exactly as an operator sees it, at a wide terminal so
    Click's wrapper cannot be what splits a name in half."""
    res = CliRunner().invoke(cli, ["--help"], env={"COLUMNS": "200"})
    assert res.exit_code == 0, res.output
    return res.output


def _quick_start_block(text: str) -> str:
    """The Quick start paragraph: from its heading to the next blank line.

    Asserting against the BLOCK rather than the whole help is the point -- the
    defect is about which command the quick start leads with, and `systemu
    start` appears elsewhere in the help (the command list) whether or not the
    quick start ever mentions it.
    """
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if "Quick start" in l]
    assert starts, "the root help has no Quick start block at all:\n" + text
    i = starts[0]
    out = [lines[i]]
    for l in lines[i + 1:]:
        if not l.strip():
            break
        out.append(l)
    return "\n".join(out)


def test_the_quick_start_block_names_the_golden_path_command():
    block = _quick_start_block(_help_text())
    assert "systemu start" in block, (
        "the first surface a fresh install reads never names the one-command "
        "golden path:\n" + block)


def test_the_golden_path_comes_before_record_in_the_quick_start():
    block = _quick_start_block(_help_text())
    assert "systemu record" in block, block
    assert block.index("systemu start") < block.index("systemu record"), (
        "`record` is offered ahead of the command that is documented as the "
        "first thing a fresh install types:\n" + block)


def test_the_quick_start_says_where_the_dashboard_comes_from():
    """`start` opens a dashboard, and a bare `pip install systemu` has no
    dashboard to open. The block that tells an operator to run it must tell
    them that in the same breath -- the README already advertises the line."""
    block = _quick_start_block(_help_text())
    flat = " ".join(block.split())
    assert 'pip install "systemu[dashboard]"' in flat, (
        "the quick start sends the operator to `start` without saying the "
        "dashboard is an install extra:\n" + block)


def test_setup_and_record_are_still_in_the_quick_start():
    """A regression guard on the fix itself: leading with `start` must not cost
    the operator the two steps that were already there."""
    block = _quick_start_block(_help_text())
    assert "systemu setup" in block, block
    assert "systemu record" in block, block


def test_the_quick_start_block_is_ascii(): 
    """DEC-32c: a help line a cp1252 console cannot encode is a line the
    operator does not get to read. (The module docstring's em dash predates
    this fix and lives outside the block.)"""
    _quick_start_block(_help_text()).encode("ascii")
