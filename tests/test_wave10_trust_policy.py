"""W10.2 — trust policies leave an audit trail.

The gate-mode dial (allow/ask/deny with a floor) shipped in W2.4-era work,
and the DENY path records an inspectable resolved row — but the ALLOW path
executed a synthetic decision and recorded NOTHING: auto-grants were
invisible to the audit trail (the office/compliance buying requirement).

Now every auto-approval saves a BORN-RESOLVED decision row
(``resolved_by: "auto_policy"``): visible in /inbox History, exportable,
never pending — so it cannot ping the needs-you badge or push "Needs you"
to the operator's phone for something that needed no attention.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from systemu.interface.command.gate import GateDescriptor
from systemu.interface.command.gate_mode import GateMode, GateModePolicy
from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    (tmp_path / "decisions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "decisions" / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _descriptor(risk="low") -> GateDescriptor:
    return GateDescriptor(
        title="Enable tool 'parse_json'?", risk=risk,
        inspect="A pure utility tool.", options=["Skip", "Enable"],
        safe_default="Skip", dedup="tools_blocked:act_42",
    )


class TestAutoAllowAudit:
    def test_allow_writes_born_resolved_audit_row(self, vault, monkeypatch):
        from systemu.interface.command import inbox as inbox_mod
        # The executor must still run — stub it to observe the call.
        executed = {}
        monkeypatch.setattr(
            inbox_mod, "resolve_gate",
            lambda decision, vault: executed.update(ok=True) or
            __import__("types").SimpleNamespace(status=None, summary="done"))

        queue = inbox_mod.InboxQueue(vault)
        policy = GateModePolicy(mode=GateMode.BYPASS)
        queue.enqueue(_descriptor(), gate_type="tools_blocked",
                      policy=policy, vault=vault)

        assert executed.get("ok") is True, "auto-grant must still execute"
        rows = vault.load_index("decisions")
        assert len(rows) == 1
        assert rows[0]["status"] == "resolved", \
            "the audit row must be born resolved — never pending"
        full = vault.get_decision(rows[0]["id"])
        assert (full.context or {}).get("resolved_by") == "auto_policy"
        assert full.choice == "Enable"          # the affirmative option
        assert (full.context or {}).get("kind") == "gate"

    def test_auto_allow_never_pings_needs_you(self, vault, monkeypatch):
        from systemu.interface.command import inbox as inbox_mod
        from systemu.interface.components.attention import needs_you_total
        monkeypatch.setattr(
            inbox_mod, "resolve_gate",
            lambda decision, vault: __import__("types").SimpleNamespace(
                status=None, summary="done"))
        queue = inbox_mod.InboxQueue(vault)
        queue.enqueue(_descriptor(), gate_type="tools_blocked",
                      policy=GateModePolicy(mode=GateMode.BYPASS), vault=vault)
        assert needs_you_total(vault) == 0

    def test_auto_allow_emits_no_posted_event(self, vault, monkeypatch):
        """Posting fires operator_decision_posted → Telegram 'Needs you'
        pushes + badge flashes. An auto-grant must stay silent."""
        from systemu.interface.command import inbox as inbox_mod
        from systemu.interface.event_bus import EventBus
        monkeypatch.setattr(
            inbox_mod, "resolve_gate",
            lambda decision, vault: __import__("types").SimpleNamespace(
                status=None, summary="done"))
        captured = []
        unsub = EventBus.get().subscribe(captured.append, replay=False)
        try:
            inbox_mod.InboxQueue(vault).enqueue(
                _descriptor(), gate_type="tools_blocked",
                policy=GateModePolicy(mode=GateMode.BYPASS), vault=vault)
        finally:
            unsub()
        assert not any(e.get("category") == "operator_decision_posted"
                       for e in captured)


class TestDialBehaviourUnchanged:
    def test_floor_still_posts_under_bypass(self, vault):
        from systemu.interface.command.inbox import InboxQueue
        dec_id = InboxQueue(vault).enqueue(
            _descriptor(), gate_type="dep",        # floor type
            policy=GateModePolicy(mode=GateMode.BYPASS), vault=vault)
        assert isinstance(dec_id, str)
        rows = vault.load_index("decisions")
        assert rows and rows[0]["status"] == "pending"

    def test_ask_posts_pending(self, vault):
        from systemu.interface.command.inbox import InboxQueue
        InboxQueue(vault).enqueue(
            _descriptor(risk="high"), gate_type="tools_blocked",
            policy=GateModePolicy(mode=GateMode.RISK_TIERED), vault=vault)
        rows = vault.load_index("decisions")
        assert rows and rows[0]["status"] == "pending"


class TestHistoryShowsAuto:
    def test_history_card_marks_auto_policy(self):
        import inspect
        from systemu.interface.pages import inbox_page
        src = inspect.getsource(inbox_page._render_history_card)
        assert "auto_policy" in src, \
            "/inbox History must visibly mark policy auto-approvals"