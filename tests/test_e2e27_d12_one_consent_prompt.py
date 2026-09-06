"""D12 -- two consent surfaces may not answer to two different stdin rules.

WITNESSED DEFECT (v0.10.27)
    Both commands create a STANDING permission and both print a disclosure and
    ask. Piped the same way, they behaved differently:

        $ echo y | systemu roots grant ~/Documents
        Granted: /home/me/Documents                       # exit 0

        $ echo y | systemu census grant cloud_sync_roots
        Refusing to record consent: there is no terminal to ask on ...
          Re-run with --yes ...                           # exit 2

    `roots grant` accepted a piped `y` as consent and, on an EOF, exited 1
    naming `--yes`; `census grant` required a terminal, refused a pipe outright,
    and exited 2. So the answer to "did my automation just grant that?" depended
    on which permission was being granted, and a wrapper written against one of
    them was wrong about the other.

THE RULING
    ONE helper, used by BOTH commands, carrying the CENSUS rule -- the stricter
    of the two, and the right one: a `y` arriving on a pipe is not a person
    reading a disclosure.

        no terminal            -> refuse, name --yes, exit 2
        terminal + y / yes     -> proceed
        terminal + Enter / n / anything else -> refused, exit 1
        --yes                  -> proceed, disclosure still printed

    The census side already conformed and consumes the helper unchanged; the
    change in behaviour is entirely on the `roots grant` side, where a piped
    `y` now refuses with exit 2 instead of granting.

WHAT THIS FILE PINS
    The rule itself, table-driven over BOTH commands through `CliRunner`, with
    the terminal state controlled at the HELPER'S seam so the two commands
    cannot be reading two different answers to "is there a human here". Plus an
    AST reachability pin: both command bodies must actually call the helper --
    a second private copy of the rule is how this defect happened the first
    time.

TEST REWRITE RECORDED (GATE-6)
    Four tests in tests/test_p4_roots_cli.py pinned the OLD `roots grant` rule
    and were rewritten with this change; they are listed in the commit message.
    Under `CliRunner`, `sys.stdin.isatty()` is always False, so their piped
    happy paths were unreachable under the new rule -- they answer consent at
    the helper's seam or via `--yes` now, and every other assertion in them
    (the on-disk store contents, the disclosure text, the survey behaviour) is
    unchanged.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from systemu.interface import cli_commands
from systemu.runtime.granted_roots import GrantedRootsStore, canonicalize

#: The two surfaces under the one rule.
ROOTS, CENSUS = "roots grant", "census grant"
BOTH = (ROOTS, CENSUS)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A vault both commands write into, and a folder `roots grant` can grant."""
    from sharing_on import cli as sharing_on_cli
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    folder = tmp_path / "Documents"
    folder.mkdir()
    vault = SimpleNamespace(root=vault_root)
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), vault))
    monkeypatch.setattr(sharing_on_cli, "_census_vault", lambda: vault)
    return SimpleNamespace(vault_root=vault_root, folder=folder)


def _at_a_terminal(monkeypatch, present: bool) -> None:
    """Control the terminal answer AT THE HELPER'S SEAM, for both commands.

    This is the point of the slice: there is one predicate now. The census
    command keeps its own `_census_stdin_is_a_terminal` wrapper (its tests
    inject there), but that wrapper DELEGATES here -- so this single patch
    steers both surfaces, and it would not if either grew a second answer.
    """
    from systemu.interface import consent_prompt
    monkeypatch.setattr(consent_prompt, "stdin_is_a_terminal", lambda: present)


def _invoke(which, machine, *, yes=False, stdin=""):
    from sharing_on import cli as sharing_on_cli
    runner = CliRunner()
    if which is ROOTS:
        args = ["grant", str(machine.folder)] + (["--yes"] if yes else [])
        return runner.invoke(cli_commands.roots_group, args, input=stdin)
    args = ["grant", "cloud_sync_roots"] + (["--yes"] if yes else [])
    return runner.invoke(sharing_on_cli.census_group, args, input=stdin)


def _was_granted(which, machine) -> bool:
    if which is ROOTS:
        return GrantedRootsStore(base_dir=machine.vault_root).list_roots() == [
            canonicalize(str(machine.folder))]
    return (machine.vault_root / "census_consent.json").exists()


# --------------------------------------------------------------------------- #
# THE RULE, over both commands
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("which", BOTH)
@pytest.mark.parametrize("stdin", ["y\n", "yes\n", "n\n", "", "\n"])
def test_no_terminal_refuses_whatever_is_on_stdin_and_names_the_flag(
        which, stdin, machine, monkeypatch):
    """A `y` on a pipe is not a person who read the disclosure.

    Parameterised over the input deliberately: the OLD `roots grant` rule turned
    exactly this table into two different answers (`y` granted, EOF refused),
    and the whole point of the ruling is that stdin's CONTENT stops mattering
    once there is nobody to ask.
    """
    _at_a_terminal(monkeypatch, False)
    res = _invoke(which, machine, stdin=stdin)
    assert res.exit_code == 2, f"{which}: {res.output}"
    assert "--yes" in res.output, (
        f"{which}: a refusal with no way forward: {res.output}")
    assert not _was_granted(which, machine), f"{which} recorded a grant anyway"


@pytest.mark.parametrize("which", BOTH)
@pytest.mark.parametrize("stdin", ["y\n", "yes\n"])
def test_a_yes_typed_at_a_real_terminal_proceeds(which, stdin, machine,
                                                 monkeypatch):
    """The positive control. Without it every assertion above is satisfied by a
    command that never grants anything at all."""
    _at_a_terminal(monkeypatch, True)
    res = _invoke(which, machine, stdin=stdin)
    assert res.exit_code == 0, f"{which}: {res.output}"
    assert _was_granted(which, machine), f"{which}: {res.output}"


@pytest.mark.parametrize("which", BOTH)
@pytest.mark.parametrize("stdin", ["n\n", "\n"])
def test_a_no_or_a_bare_enter_at_a_terminal_refuses_and_changes_nothing(
        which, stdin, machine, monkeypatch):
    """y/N, not Y/n: someone holding return through a series of prompts must not
    hand over a standing permission by momentum. Exit 1, distinct from the 2
    above so a script can tell "they said no" from "you cannot ask here"."""
    _at_a_terminal(monkeypatch, True)
    res = _invoke(which, machine, stdin=stdin)
    assert res.exit_code == 1, f"{which}: {res.output}"
    assert not _was_granted(which, machine), f"{which} recorded a grant anyway"


@pytest.mark.parametrize("which", BOTH)
def test_the_yes_flag_proceeds_with_the_disclosure_still_printed(
        which, machine, monkeypatch):
    """`--yes` is the operator asserting they read the disclosure. It is not a
    way to not be told what was agreed to, so the copy still prints -- on the
    path where NOBODY is watching, which is where dropping it is tempting."""
    _at_a_terminal(monkeypatch, False)
    res = _invoke(which, machine, yes=True, stdin="")
    assert res.exit_code == 0, f"{which}: {res.output}"
    assert _was_granted(which, machine), f"{which}: {res.output}"
    assert "model provider" in res.output.lower(), (
        f"{which} granted without printing what it discloses: {res.output}")


# --------------------------------------------------------------------------- #
# REACHABILITY -- both bodies call the ONE helper
# --------------------------------------------------------------------------- #

def _bodies_that_call(func_name: str, call_name: str) -> bool:
    """Does `func_name`'s body contain a call to `call_name`, in the shipped file?

    Source-level on purpose: the property is "there is one rule and both
    commands are ON it", which lives in the call graph. A behavioural test alone
    would stay green against a second private copy that happened to agree today.
    """
    tree = ast.parse(Path(cli_commands.__file__).read_text(
        encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            fn = inner.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == call_name:
                return True
    return False


@pytest.mark.parametrize("func", ["roots_grant", "run_census_grant"])
def test_both_command_bodies_call_the_shared_consent_helper(func):
    assert _bodies_that_call(func, "ask_for_consent"), (
        f"{func} does not call the shared consent prompt, so it is free to "
        f"answer a pipe differently from the other one -- which is the defect")


def test_the_reachability_pin_is_not_vacuous():
    """It must be able to say NO, or it pins nothing (DEC-32: an assertion the
    failure path also satisfies is not a pin)."""
    assert not _bodies_that_call("roots_grant", "no_such_helper_exists")
    assert not _bodies_that_call("no_such_function", "ask_for_consent")


# --------------------------------------------------------------------------- #
# the exit codes are DOCUMENTED where the operator meets them
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("which", BOTH)
def test_the_help_states_the_exit_codes(which, machine):
    """A script author reading `--help` has to learn 1 from 2 there, not by
    running the command twice and diffing."""
    from sharing_on import cli as sharing_on_cli
    runner = CliRunner()
    if which is ROOTS:
        out = runner.invoke(cli_commands.roots_group, ["grant", "--help"]).output
    else:
        out = runner.invoke(sharing_on_cli.census_group, ["grant", "--help"]).output
    low = " ".join(out.split()).lower()
    assert "exit codes:" in low, f"{which} --help documents no exit codes: {out}"
    block = low.split("exit codes:", 1)[1]
    for code, meaning in (("0", "granted"), ("1", "declined"), ("2", "no terminal")):
        assert code in block, f"{which} --help omits exit {code}: {out}"
        assert meaning in block, (
            f"{which} --help names exit {code} without saying what it means: {out}")


# --------------------------------------------------------------------------- #
# the helper itself
# --------------------------------------------------------------------------- #

def test_the_helper_fails_closed_when_the_terminal_probe_explodes(monkeypatch):
    """A predicate that raises is not a terminal. The safe direction is refusal,
    never "assume someone is there"."""
    from systemu.interface import consent_prompt as cp

    def _boom():
        raise RuntimeError("no console")

    assert cp.ask_for_consent("ok?", is_a_terminal=_boom) == cp.CONSENT_NO_TERMINAL


def test_the_helper_never_prompts_when_there_is_no_terminal(monkeypatch):
    """Prompting into a closed stdin surfaces as a bare abort with no way
    forward -- the shape both commands' refusal copy exists to avoid."""
    from systemu.interface import consent_prompt as cp

    def _must_not_prompt(*a, **kw):     # pragma: no cover - the assertion is it
        raise AssertionError("prompted with no terminal instead of refusing")

    monkeypatch.setattr(cp.click, "confirm", _must_not_prompt)
    assert cp.ask_for_consent("ok?", is_a_terminal=lambda: False) \
        == cp.CONSENT_NO_TERMINAL


def test_the_helper_asks_with_the_default_set_to_no(monkeypatch):
    """The default is the answer a distracted person gives."""
    from systemu.interface import consent_prompt as cp
    seen = []

    monkeypatch.setattr(cp.click, "confirm",
                        lambda text, **kw: (seen.append(kw.get("default")), False)[1])
    assert cp.ask_for_consent("ok?", is_a_terminal=lambda: True) == cp.CONSENT_DECLINED
    assert seen == [False], f"the confirm must default to N, got {seen}"


def test_the_yes_flag_short_circuits_before_the_terminal_is_even_consulted():
    """`--yes` is for the box that has no terminal. Consulting one first would
    make the flag useless exactly where it exists to be used."""
    from systemu.interface import consent_prompt as cp

    def _boom():                        # pragma: no cover - the assertion is it
        raise AssertionError("consulted the terminal despite --yes")

    assert cp.ask_for_consent("ok?", assume_yes=True, is_a_terminal=_boom) \
        == cp.CONSENT_GRANTED


def test_the_census_seam_delegates_to_the_shared_predicate(monkeypatch):
    """The census keeps its own named predicate (its tests inject there), but it
    must be a DELEGATE, not a second answer -- otherwise the two commands can
    disagree about whether a human is present."""
    from systemu.interface import consent_prompt as cp
    monkeypatch.setattr(cp, "stdin_is_a_terminal", lambda: True)
    assert cli_commands._census_stdin_is_a_terminal() is True
    monkeypatch.setattr(cp, "stdin_is_a_terminal", lambda: False)
    assert cli_commands._census_stdin_is_a_terminal() is False
