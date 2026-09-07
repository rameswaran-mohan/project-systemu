"""P9(a) -- the reclassify option label was non-ASCII, and it was duplicated.

THE DEFECT, both halves
    ``gate.RECLASSIFY_OPTION`` ended in a real ellipsis (U+2026). It is the
    label on a DENY card AND the exact choice string the Inbox panel resolves
    with, and ``decision_queue.resolve`` validates choice-in-options -- so it
    reaches a console. On Windows the daemon's console is cp1252, where U+2026
    is a UnicodeEncodeError in place of the card (DEC-32c). It was the LAST
    non-ASCII module constant in ``gate.py``: v0.10.30 made every sibling ASCII
    and carved this one out, because at the time the literal was duplicated
    into ``cli_commands.py``'s refusal copy and changing it in one place would
    have left the CLI naming a button that no longer existed.

    That duplicate is the second half of the defect and the reason the first
    could not be fixed alone. Two literals for one label is the shape
    ``decision_queue.resolve`` punishes: a one-character drift between them
    raises instead of resolving.

THE RULING
    ``"Reclassify effect..."`` -- ASCII -- and ONE source of truth: the CLI
    IMPORTS the constant instead of re-typing it.

THE PROPERTIES PINNED HERE
    1. The label is ASCII and is exactly the ruled string.
    2. The label the card OFFERS is that constant (read off a real DENY card,
       not off the module).
    3. The CLI's refusal copy contains the constant BY IMPORT: the CLI module's
       source carries no ``Reclassify effect`` literal at all, and the module
       does bind the imported name. Stated structurally rather than by
       comparing two rendered strings, because two equal strings prove nothing
       about where the second one came from -- re-typing today's label would
       pass a value comparison and is exactly the mutation this must catch.
    4. Everything the CLI prints on that refusal is ASCII, the label included.

NOTHING IS ENQUEUED AND NO DECISION IS RESOLVED. The card is built directly and
the CLI's refusal branch is reached through its own predicate.
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest

from systemu.interface.command.gate import RECLASSIFY_OPTION, GateDescriptor


#: The ruled label. Written out ONCE, here, because this file is the pin for
#: it; every other surface must reach it by import.
_RULED_LABEL = "Reclassify effect..."

#: The prefix a re-typed duplicate would necessarily start with, whichever
#: trailing character it chose. Matching on the PREFIX is what makes the
#: structural test below survive a mutation that "fixes" the ellipsis by
#: re-typing the ASCII form.
_LABEL_PREFIX = "Reclassify effect"


def _cli_source() -> str:
    from systemu.interface import cli_commands

    return io.open(cli_commands.__file__, encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# 1 + 2. the label itself, and the label a card offers
# --------------------------------------------------------------------------- #

def test_the_reclassify_option_is_ascii():
    """The last non-ASCII constant in `gate.py`. A console that cannot encode a
    line is a console the operator reads NOTHING on."""
    bad = sorted({hex(ord(c)) for c in RECLASSIFY_OPTION if ord(c) > 127})
    assert not bad, (
        "the reclassify option label carries non-ASCII " + repr(bad)
        + " and reaches a cp1252 console: " + repr(RECLASSIFY_OPTION))


def test_the_reclassify_option_is_the_ruled_string():
    assert RECLASSIFY_OPTION == _RULED_LABEL


def test_a_deny_card_offers_exactly_that_label():
    """Read off a real DENY card. The constant is only interesting insofar as
    it is what the operator is actually offered -- and what
    ``decision_queue.resolve`` will validate the choice against."""
    card = GateDescriptor.from_tool(tool_name="mystery", sig="s",
                                    verdict="deny", effect_tags=[])
    assert card.options == ["Deny", _RULED_LABEL]
    assert card.options[-1] is RECLASSIFY_OPTION or \
        card.options[-1] == RECLASSIFY_OPTION


# --------------------------------------------------------------------------- #
# 3. ONE source of truth -- stated structurally
# --------------------------------------------------------------------------- #

def test_the_cli_imports_the_label_instead_of_re_typing_it():
    """THE MUTATION CATCHER. Two equal strings prove nothing about where the
    second came from, so this reads the CLI's SOURCE: it must bind the name by
    import and it must contain no label literal of its own. Re-typing the label
    into `cli_commands.py` -- with either trailing character -- turns this red.
    """
    source = _cli_source()
    tree = ast.parse(source)

    imported = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("command.gate")
        and any(alias.name == "RECLASSIFY_OPTION" for alias in node.names)]
    assert imported, (
        "cli_commands.py does not import RECLASSIFY_OPTION from "
        "systemu.interface.command.gate; the option label it prints is "
        "therefore a second source of truth for a string decision_queue."
        "resolve byte-matches against")

    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _LABEL_PREFIX in node.value:
                literals.append((getattr(node, "lineno", "?"), node.value))
    assert not literals, (
        "cli_commands.py still spells the reclassify label as a literal; a "
        "one-character drift from gate.RECLASSIFY_OPTION makes "
        "decision_queue.resolve raise instead of resolving: " + repr(literals))


def test_the_module_actually_binds_the_imported_name():
    """The AST above proves the import statement is written. This proves it
    RUNS -- an import inside a dead branch would satisfy the scan and bind
    nothing."""
    from systemu.interface import cli_commands

    bound = getattr(cli_commands, "RECLASSIFY_OPTION", None)
    assert bound == RECLASSIFY_OPTION, (
        "cli_commands does not bind the shared constant at import time: "
        + repr(bound))


# --------------------------------------------------------------------------- #
# 4. the refusal an operator actually reads
# --------------------------------------------------------------------------- #

def _refusal_output(monkeypatch) -> str:
    """Everything the CLI prints when it refuses a reclassify on this surface.

    The REAL command is invoked through click's own runner, so the refusal
    branch is reached exactly as an operator reaches it. Only ``console.print``
    is captured (an output sink, never product logic), and no vault, queue or
    decision is touched -- the branch exits before ``_get_vault_and_config``.
    """
    from click.testing import CliRunner

    from systemu.interface import cli_commands

    printed = []
    monkeypatch.setattr(cli_commands.console, "print",
                        lambda *a, **kw: printed.append(
                            " ".join(str(x) for x in a)))

    assert cli_commands._reclassify_needs_the_inbox(RECLASSIFY_OPTION), (
        "the CLI no longer recognises the ruled label as a reclassify choice, "
        "so the refusal below is unreachable and nothing is pinned")

    result = CliRunner().invoke(
        cli_commands.decisions_resolve,
        ["dec1", "--choice", RECLASSIFY_OPTION])
    assert result.exit_code == 2, (
        "the CLI did not take the refusal branch (exit 2); nothing was pinned. "
        "exit=" + repr(result.exit_code) + " output=" + repr(result.output))
    assert printed, "the refusal branch printed nothing"
    return "\n".join(printed)


def test_the_refusal_names_the_button_by_its_real_label(monkeypatch):
    """The whole point of the refusal is to send the operator to a control that
    exists. It must name the label the Inbox actually renders."""
    out = _refusal_output(monkeypatch)
    assert RECLASSIFY_OPTION in out, (
        "the refusal points the operator at a button whose label it does not "
        "use: " + repr(out))
    assert "Inbox" in out


def test_the_whole_refusal_is_ascii(monkeypatch):
    """DEC-32c on the surface that carried the duplicate. This string is
    printed to the same cp1252 console the card is."""
    out = _refusal_output(monkeypatch)
    bad = sorted({hex(ord(c)) for c in out if ord(c) > 127})
    assert not bad, (
        "the CLI's reclassify refusal carries non-ASCII " + repr(bad)
        + " -- use '--' for an em dash and '...' for an ellipsis:\n" + repr(out))


# --------------------------------------------------------------------------- #
# 5. the corpus carve-out this closes
# --------------------------------------------------------------------------- #

def test_gate_py_now_has_no_non_ascii_module_constant_at_all():
    """``test_dogfood28_p9_gate_card_corpus_is_ascii.py`` shipped with exactly
    one documented exemption -- this label. With the duplicate resolved the
    carve-out has nothing left to cover, so the stronger property is stated
    here: NO module-level string constant in `gate.py` is non-ASCII."""
    from systemu.interface.command import gate as gate_mod

    source = io.open(gate_mod.__file__, encoding="utf-8").read()
    tree = ast.parse(source)

    offenders = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or node.value is None:
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if any(ord(c) > 127 for c in sub.value):
                    offenders.append((names[0], sub.value))

    assert not offenders, (
        "gate.py still carries a non-ASCII module constant: " + repr(offenders))
