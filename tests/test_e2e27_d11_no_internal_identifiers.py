"""D11 -- the metrics report speaks to an operator, not to the build ledger.

WITNESSED DEFECT (v0.10.27, a from-scratch home)
    ``systemu debug avoidable-ask`` printed, in the OUTPUT an operator reads::

        ... a deterministic DIRECTIONAL signal ... for the S10 avoidable-ask
        rate; a DEC-7 input ...
        Answer-linked asks (S5.9, R-A16): 0 answered ...
        ... asked only for the T_high / content_derived confirm ...
        ... silence (IMPL-5). A per-class threshold delta would be dead
        machinery today -- see _threshold_sensitive_counts.
        Quick-lane asks: NOT MEASURED (0 recorded asks; DEC-7's measurement
        window opens at N=30 asks ...)

    Section numbers, roadmap item ids, decision-ledger ids and private symbol
    names. None of them mean anything outside this repository's build documents,
    and an operator who searches for one finds nothing. The report's job is to
    explain a number to the person who ran the command.

    (The identifiers stay in COMMENTS and in non-rendered docstrings, where the
    next engineer needs them. What may not carry them is text the product PRINTS
    -- which includes a click command's own help, because that IS the help.)

WHAT THIS FILE PINS
    The whole rendered surface of ``debug avoidable-ask`` -- the report and its
    ``--help`` -- against the identifier classes, plus ASCII-only, which is the
    same surface's other quoting hazard: an em dash or a section sign through
    ``click.echo`` on a cp437 console comes back as a replacement character, so
    the line an operator is meant to quote is the one that corrupts.
"""
from __future__ import annotations

import re

import pytest
from click.testing import CliRunner

from systemu.interface import cli_commands as cc
from systemu.runtime import replay_metrics as rm

#: Every class named in the ruling. Each is matched against the RENDERED text.
_INTERNAL_IDENTIFIERS = {
    "roadmap item id (R-xx<n>)": re.compile(r"R-[A-Z]+\d"),
    "decision-ledger id (DEC-<n>)": re.compile(r"\bDEC-\d"),
    "spec implementation note (IMPL-<n>)": re.compile(r"\bIMPL-\d"),
    "spec section sign": re.compile("§"),
    "internal threshold symbol (T_high)": re.compile(r"T_high"),
    "private python symbol (_leading_underscore)": re.compile(r"(?<![\w.])_[a-z][a-z0-9_]{3,}"),
}


class _Vault:
    def __init__(self, root):
        self.root = root


def _invoke(args, tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(cc, "_get_vault_and_config",
                        lambda ctx: (None, _Vault(tmp_path)))
    res = CliRunner().invoke(cc.debug_avoidable_ask, args, obj={})
    assert res.exit_code == 0, res.output
    return res.output


def _assert_clean(text: str, what: str) -> None:
    found = {name: m.group() for name, pat in _INTERNAL_IDENTIFIERS.items()
             for m in [pat.search(text)] if m}
    assert not found, (
        f"{what} carries internal identifiers an operator cannot look up: "
        f"{found}\n\n{text}")


# --------------------------------------------------------------------------- #
# THE PIN
# --------------------------------------------------------------------------- #

def test_the_report_carries_no_internal_identifiers(tmp_path, monkeypatch):
    """A fresh home -- the NOT MEASURED render of all four blocks."""
    _assert_clean(_invoke([], tmp_path, monkeypatch), "the avoidable-ask report")


def test_the_help_carries_no_internal_identifiers(tmp_path, monkeypatch):
    """A click command's docstring IS its ``--help``; it is printed text."""
    _assert_clean(_invoke(["--help"], tmp_path, monkeypatch),
                  "`debug avoidable-ask --help`")


def test_a_populated_report_carries_no_internal_identifiers(tmp_path, monkeypatch):
    """The MEASURED render of the two ask blocks is a different set of lines, and
    an identifier hiding in a branch nobody exercised is the usual way this rots."""
    vault = _Vault(tmp_path)
    rm.record_ask(vault, kind="capability", attempts_before=0, tool_attempts=0)
    rm.record_ask(vault, kind="capability", attempts_before=1, tool_attempts=2)

    _assert_clean(_invoke([], tmp_path, monkeypatch),
                  "the avoidable-ask report over a populated corpus")


@pytest.mark.parametrize("args", [[], ["--help"]])
def test_every_rendered_line_is_ascii_only(tmp_path, monkeypatch, args):
    """The sibling quick-lane block already carries this pin for its own four
    render states; the rest of the command's output was never held to it, and the
    section signs and em dashes removed above are exactly what it catches."""
    out = _invoke(args, tmp_path, monkeypatch)
    offenders = sorted({ch for ch in out if ord(ch) > 127})
    assert not offenders, (
        f"non-ASCII in operator output: {offenders!r}\n\n{out}")


# --------------------------------------------------------------------------- #
# The guard is only worth something if it would have caught the witnessed text
# --------------------------------------------------------------------------- #

def test_the_guard_catches_every_witnessed_string():
    """Not vacuous: each line quoted in this module's docstring is rejected."""
    for witnessed in (
        "a deterministic DIRECTIONAL signal for the §10 avoidable-ask rate",
        "avoidable-ask rate; a DEC-7 input.",
        "Answer-linked asks (§5.9, R-A16): 0 answered",
        "asked only for the T_high / content_derived confirm",
        "silence (IMPL-5). A per-class threshold delta",
        "dead machinery today -- see _threshold_sensitive_counts.",
        "DEC-7's measurement window opens at N=30 asks",
    ):
        with pytest.raises(AssertionError):
            _assert_clean(witnessed, "a witnessed line")


# --------------------------------------------------------------------------- #
# D11 remainder -- `census --help` (the same defect, a different printed surface)
# --------------------------------------------------------------------------- #
#
# WITNESSED: `systemu census --help` opened with
#
#     Control what systemu is allowed to notice about THIS MACHINE (R-W2, 5.11.c).
#
# A roadmap item id and a spec section number, in the first sentence of the help
# for a consent control. A click command's docstring IS its `--help`, so this is
# printed text and the ruling above covers it; the identifiers stay in the
# region comment above the group, where the next engineer needs them.

#: The ruled classes, plus the spec SECTION NUMBER that came with this one.
#: Scoped to this surface rather than added to the set above: a bare `5.11.c`
#: pattern would be free to hit a version string elsewhere.
_CENSUS_IDENTIFIERS = dict(
    _INTERNAL_IDENTIFIERS,
    **{"spec section number (5.11.c)": re.compile(r"\b\d+\.\d+\.[a-z]\b")},
)


def _census_help(args) -> str:
    from sharing_on import cli as sharing_on_cli
    res = CliRunner().invoke(sharing_on_cli.census_group, args)
    assert res.exit_code == 0, res.output
    return res.output


def _assert_census_clean(text: str, what: str) -> None:
    found = {name: m.group() for name, pat in _CENSUS_IDENTIFIERS.items()
             for m in [pat.search(text)] if m}
    assert not found, (
        f"{what} carries internal identifiers an operator cannot look up: "
        f"{found}\n\n{text}")


def test_census_help_carries_no_internal_identifiers():
    """The group's own help -- where `(R-W2, 5.11.c)` was."""
    _assert_census_clean(_census_help(["--help"]), "`systemu census --help`")


@pytest.mark.parametrize("sub", ["status", "grant", "revoke", "pause", "resume"])
def test_every_census_subcommand_help_is_clean_too(sub):
    """One clean sentence at the top of a group is worth little if the verb the
    operator actually types still speaks in ledger ids."""
    _assert_census_clean(_census_help([sub, "--help"]),
                         f"`systemu census {sub} --help`")


def test_the_census_help_still_says_what_the_command_is_for():
    """Not a deletion pin: removing the identifiers must not remove the meaning.

    The first line is the one an operator reads in the parent `--help` listing,
    so it has to still name the boundary this group exists to guard.
    """
    text = _census_help(["--help"]).lower()
    assert "this machine" in text
    assert "grant" in text and "revoke" in text


def test_the_census_guard_catches_the_witnessed_string():
    """Anti-vacuity, in the shape of the line that shipped."""
    with pytest.raises(AssertionError):
        _assert_census_clean(
            "Control what systemu is allowed to notice about THIS MACHINE "
            "(R-W2, 5.11.c).", "the witnessed line")
