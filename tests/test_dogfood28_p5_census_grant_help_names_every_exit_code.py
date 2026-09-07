"""P5 -- `census grant --help` documents every exit code the command returns.

THE WITNESS (0.10.30 dogfood)
-----------------------------
`systemu census grant --help` documents 0, 1 and 2, and closes with the shape
of a complete contract:

    Exit codes:
      0  granted
      1  you were asked and declined (a bare Enter counts as no)
      2  there was no terminal to ask on -- ...

Both of these exit 3:

    systemu census grant not-a-category      -> 3
    systemu census grant installed_apps      -> 3   (declared, not yet grantable)

A script that reads that help and branches on 0/1/2 has no case for the answer
it will actually get from a typo -- the most likely wrong invocation there is.
The command was RIGHT to refuse (`_census_category_or_error` refuses in both
directions rather than no-opping, which is the whole point of the code); the
help was wrong about what refusing looks like from outside.

DEC-34: a surface that states a contract it does not keep is a defect of that
class, and README copy and `--help` copy are held to it exactly as a docstring
is. The fix is the documentation, not the code -- 3 is the right answer.

THE PROPERTY
    The help names 3, and says what it means; the codes it names are the
    constants the command actually returns.

No vault is opened here: `_census_category_or_error` refuses before
`run_census_grant` reaches the store, which is what lets these run with
``vault=None``.
"""
from __future__ import annotations

from click.testing import CliRunner

from sharing_on.cli import census_grant_cmd
from systemu.interface import cli_commands as cc


def _help() -> str:
    res = CliRunner().invoke(census_grant_cmd, ["--help"])
    assert res.exit_code == 0, res.output
    return res.output


# --------------------------------------------------------------------------- #
# the help names 3
# --------------------------------------------------------------------------- #

def test_the_help_documents_exit_code_3():
    out = _help()
    lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("3 ")]
    assert lines, (
        "`census grant --help` still documents 0/1/2 only, while the command "
        "answers 3 for a typo'd or a not-yet-grantable category:\n" + out)


def test_the_exit_code_3_line_names_both_ways_of_reaching_it():
    """One code, two causes, and the operator's next move differs: a typo is
    fixed by retyping, a declared-but-not-grantable category cannot be granted
    from this at all."""
    line = [ln.strip() for ln in _help().splitlines()
            if ln.strip().startswith("3 ")][0]
    lowered = line.lower()
    assert "unknown" in lowered, line
    assert "grantable" in lowered, line


def test_the_help_still_documents_the_three_codes_it_already_had():
    """The addition must not cost the codes that were already right."""
    out = _help()
    starts = {ln.strip()[0] for ln in out.splitlines()
              if ln.strip()[:2] in ("0 ", "1 ", "2 ", "3 ")}
    assert starts == {"0", "1", "2", "3"}, out


def test_the_documented_codes_are_ascii():
    """DEC-32c -- this block is read on the console that runs the command."""
    _help().encode("ascii")


# --------------------------------------------------------------------------- #
# the codes documented are the codes returned
# --------------------------------------------------------------------------- #

def test_an_unknown_category_really_answers_3():
    """The witness, so the help is pinned against BEHAVIOUR and not against a
    number someone typed into a docstring."""
    assert cc.run_census_grant(None, "definitely-not-a-category") == 3


def test_a_declared_but_not_grantable_category_really_answers_3():
    from systemu.runtime.census_consent import CATEGORIES, SURFACED_CATEGORIES

    not_yet = sorted(set(CATEGORIES) - set(SURFACED_CATEGORIES))
    if not not_yet:
        import pytest
        pytest.skip("every declared category is grantable in this build")

    assert cc.run_census_grant(None, not_yet[0]) == 3


def test_the_documented_number_is_the_constant_the_command_returns():
    """DEC-43: one fact, one place. The help and `CENSUS_EXIT_BAD_CATEGORY`
    must not be two independent claims about the same number."""
    assert cc.CENSUS_EXIT_BAD_CATEGORY == 3
    assert [ln for ln in _help().splitlines()
            if ln.strip().startswith(str(cc.CENSUS_EXIT_BAD_CATEGORY) + " ")]
