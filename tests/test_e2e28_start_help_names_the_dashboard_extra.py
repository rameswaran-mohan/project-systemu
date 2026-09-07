"""N4 -- `systemu start --help` names the extra the command cannot run without.

WITNESSED DEFECT (v0.10.28, a scratch `pip install systemu` into a clean venv)
    `systemu start` is the advertised first command: "Start Systemu and open the
    dashboard."  On a default install it refuses in under a second, because the
    dashboard ships as an OPTIONAL extra and nicegui is not there.  The refusal
    itself is good -- it names the remedy.  `--help`, which is where an operator
    looks BEFORE running the command, said nothing: it described a dashboard the
    install does not contain, and the first way to learn otherwise was to run it.

THE PROPERTY PINNED HERE
    `start --help` states that the dashboard is an extra and carries the install
    command, and carries it UNWRAPPED on one line.  The unwrapped part is not
    cosmetic: click rewraps help paragraphs to the console width, and a remedy
    broken across a line break cannot be copied -- the same defect
    ``optional_deps.unavailable_reason_parts`` exists to keep off the wrap path
    (F29), arriving here from the help side.

    The quoted form is load-bearing too: zsh, the macOS default shell, refuses
    an unquoted ``systemu[dashboard]`` with "no matches found".  The help must
    print the form that runs everywhere, which is the form `optional_deps` mints.
"""
from __future__ import annotations

from click.testing import CliRunner

from systemu.interface import cli_commands as cc
from systemu.runtime import optional_deps as od


#: The console width a default terminal has, and the width the defect needs.
_ENV = {"COLUMNS": "80"}

#: The exact remedy an operator must be able to copy.
_REMEDY = 'pip install "systemu[dashboard]"'


def _help() -> str:
    result = CliRunner().invoke(cc.start_cmd, ["--help"], obj={}, env=_ENV)
    assert result.exit_code == 0, result.output
    return result.output


def test_start_help_names_the_dashboard_extra():
    """The fact itself: the dashboard is not in the default install."""
    text = _help().lower()
    assert "extra" in text, f"`start --help` does not say the dashboard is an extra:\n{_help()}"
    assert "dashboard" in text


def test_start_help_carries_the_install_command():
    assert _REMEDY in _help(), (
        f"`start --help` does not carry {_REMEDY!r}:\n{_help()}")


def test_the_install_command_is_on_one_unwrapped_line():
    """A remedy split by click's rewrap is a remedy that cannot be copied."""
    text = _help()
    carriers = [ln for ln in text.splitlines() if _REMEDY in ln]
    assert carriers, f"the remedy is folded across lines:\n{text}"


def test_the_help_prints_the_command_optional_deps_itself_mints():
    """Two wordings of one remedy is two things to get wrong.  If the extra is
    ever renamed, the mint moves and this pin drags the help with it."""
    minted = od.install_command(("nicegui",))
    assert minted == _REMEDY, (
        f"`optional_deps` now mints {minted!r}; the help text in "
        f"`start_cmd` must say the same thing")
    assert minted in _help()
