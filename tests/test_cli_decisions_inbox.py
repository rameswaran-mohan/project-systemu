from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from click.testing import CliRunner


def test_decisions_list_shows_risk_and_what_approve_does():
    from systemu.interface.cli_commands import decisions_group
    from systemu.approval.decision_queue import OperatorDecision

    d = OperatorDecision(
        id="dec_g1", title="Approve scroll: Burrito", body="x",
        options=["Reject", "Approve"], dedup_key="scroll:scr_abc",
        status="pending", created_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        context={"kind": "gate", "gate_type": "scroll", "risk": "medium",
                 "what_approve_does": "Runs extraction and creates the activity."},
    )
    fake_queue = MagicMock(); fake_queue.list_pending.return_value = [d]
    runner = CliRunner()
    with patch("systemu.interface.cli_commands._get_vault_and_config",
               return_value=(MagicMock(), MagicMock())), \
         patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue):
        result = runner.invoke(decisions_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "MEDIUM" in result.output or "medium" in result.output
    assert "Runs extraction" in result.output


def test_decisions_resolve_approve_executes_gate():
    """`decisions resolve <gate> --choice Approve` runs the authorized action
    (Approve EXECUTES, spec §4.3): the command must invoke resolve_gate, which
    for a scroll gate calls the real approve_pending_scroll executor."""
    from systemu.interface.cli_commands import decisions_group
    from systemu.approval.decision_queue import OperatorDecision

    gate = OperatorDecision(
        id="dec_g1", title="Approve scroll: Burrito", body="x",
        options=["Reject", "Approve"], dedup_key="scroll:scr_abc",
        status="pending", created_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        context={"kind": "gate", "gate_type": "scroll"},
    )
    fake_queue = MagicMock()
    # queue.resolve() marks the row resolved AND returns the decision with
    # .choice set — the command executes that returned object directly.
    gate.choice = "Approve"
    gate.status = "resolved"
    fake_queue.resolve.return_value = gate
    fake_vault = MagicMock()
    runner = CliRunner()
    with patch("systemu.interface.cli_commands._get_vault_and_config",
               return_value=(MagicMock(), fake_vault)), \
         patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue), \
         patch("systemu.pipelines.scroll_refiner.approve_pending_scroll") as ap:
        res = runner.invoke(decisions_group, ["resolve", "dec_g1", "--choice", "Approve"])
    assert res.exit_code == 0, res.output
    assert ap.called, "resolve_gate did not invoke the real executor"
    assert ap.call_args.args[0] == "scr_abc"
    assert ap.call_args.args[1] is fake_vault
    # The plain "Resolved" line is preserved.
    assert "Resolved dec_g1" in res.output


def test_decisions_resolve_nongate_does_not_execute():
    """A non-gate (legacy) decision resolves exactly as before — resolve_gate is
    never invoked, so no executor runs and only the Resolved line prints."""
    from systemu.interface.cli_commands import decisions_group
    from systemu.approval.decision_queue import OperatorDecision

    legacy = OperatorDecision(
        id="dec_h1", title="Harness review", body="x",
        options=["Skip", "Run"], dedup_key="harness:x",
        status="pending", created_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        context={"kind": "harness_request"},
    )
    legacy.choice = "Run"
    legacy.status = "resolved"
    fake_queue = MagicMock()
    fake_queue.resolve.return_value = legacy
    runner = CliRunner()
    with patch("systemu.interface.cli_commands._get_vault_and_config",
               return_value=(MagicMock(), MagicMock())), \
         patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue), \
         patch("systemu.pipelines.scroll_refiner.approve_pending_scroll") as ap:
        res = runner.invoke(decisions_group, ["resolve", "dec_h1", "--choice", "Run"])
    assert res.exit_code == 0, res.output
    assert not ap.called, "non-gate resolve must not invoke the gate executor"
    assert "Resolved dec_h1" in res.output
