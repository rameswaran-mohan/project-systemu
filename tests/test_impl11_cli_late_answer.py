"""IMPL-11 - `decisions resolve` shows the late-answer notice as itself, exit 0.

Witnessed gap: answering, from the CLI, a card whose run was cancelled printed
"Invalid choice: That run was cancelled; this answer was not needed." and exited
2 - a failure framing and a failure exit code for an answer that was simply not
needed (nothing failed, nothing was recorded; a script treats exit 2 as an error).

Ruling under test: the command catches ``AnswerNotNeeded`` specifically, prints
the notice unprefixed as an informational line, and exits 0; its --help says so.
A genuinely invalid choice keeps "Invalid choice:" and exit 2.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from systemu.approval.decision_queue import OperatorDecisionQueue

NOTICE = "That run was cancelled; this answer was not needed."


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _gate(q):
    return q.post(title="Run tool: thing", body="b",
                  options=["Deny", "Approve once", "Always allow"],
                  context={"kind": "gate", "gate_type": "tool", "tool_name": "thing",
                           "execution_id": "exec_cli", "activity_id": "act_cli"},
                  dedup_key="tool:cli")


def _invoke(vault, *args):
    from systemu.interface.cli_commands import decisions_group
    with patch("systemu.interface.cli_commands._get_vault_and_config",
               return_value=(MagicMock(), vault)):
        return CliRunner().invoke(decisions_group, list(args))


def test_a_late_answer_prints_the_notice_unprefixed_and_exits_zero(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    did = _gate(q)
    q.expire_cards_for_run(execution_id="exec_cli")

    result = _invoke(vault, "resolve", did, "--choice", "Approve once")

    assert NOTICE in result.output
    assert "Invalid choice" not in result.output, (
        "MUTATION PIN: the notice is printed behind a failure prefix")
    assert result.exit_code == 0, "nothing failed - the answer was not needed"
    assert vault.get_decision(did).status == "expired"


def test_the_help_says_a_not_needed_answer_exits_zero(tmp_path):
    result = _invoke(_vault(tmp_path), "resolve", "--help")
    text = " ".join(result.output.split())
    assert "not needed" in text and "exits 0" in text


def test_a_genuinely_invalid_choice_still_fails(tmp_path):
    vault = _vault(tmp_path)
    did = _gate(OperatorDecisionQueue(vault))
    result = _invoke(vault, "resolve", did, "--choice", "Maybe")
    assert "Invalid choice" in result.output
    assert result.exit_code == 2
