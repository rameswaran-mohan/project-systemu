"""Phase 5 Slice 2c — the side-by-side remediation card.

Kills the self-referential fix_url loop: ``RecoveryAction.fix_url`` is ALWAYS
``/recover/<scope>/<id>`` for EVERY kind (engine._make → links.recover_url),
so the old "Open Gate Review" button on the recovery panel navigated to a page
rendering the same row with the same button.  The remediation card model maps
each kind to exactly ONE of:

  apply — {DEP_PENDING, GATE_3_DISABLED, MEMORY_POISONED} → the existing
          recover._handle_action apply path (unchanged);
  gate  — {GATE_1_PENDING, GATE_2_PENDING} → enqueue-on-demand recovery gate
          (GateDescriptor.from_recovery_action, policy=None) reviewed in the
          unified Inbox card;
  none  — {SKILL_MISSING, FS_PERMISSION, DRY_RUN_FAILED_BUG} + unknown kinds
          → guidance text, no button.

NO ``ui.navigate.to(fix_url)`` anywhere.
"""
from __future__ import annotations

import pytest

from systemu.recovery.engine import RecoveryAction


def _action(**kw) -> RecoveryAction:
    base = dict(
        scope_kind="tool",
        scope_id="tool_a",
        kind="DEP_PENDING",
        reason="Tool fetch_json missing package: requests",
        fix_url="http://localhost:8765/recover/tool/tool_a",
        fix_command="sharing_on doctor --apply",
        severity="blocker",
    )
    base.update(kw)
    return RecoveryAction(**base)


# ── per-kind button mapping ──────────────────────────────────────────────────

class TestButtonMapping:
    @pytest.mark.parametrize("kind", ["DEP_PENDING", "GATE_3_DISABLED",
                                      "MEMORY_POISONED"])
    def test_apply_kinds_get_apply_button(self, kind):
        from systemu.interface.components.remediation_card import remediation_card_model
        model = remediation_card_model(_action(kind=kind))
        assert model["fix"]["button"] == "apply"

    @pytest.mark.parametrize("kind", ["GATE_1_PENDING", "GATE_2_PENDING"])
    def test_gate_kinds_get_gate_button(self, kind):
        from systemu.interface.components.remediation_card import remediation_card_model
        model = remediation_card_model(_action(kind=kind))
        assert model["fix"]["button"] == "gate"

    @pytest.mark.parametrize("kind", ["SKILL_MISSING", "FS_PERMISSION",
                                      "DRY_RUN_FAILED_BUG"])
    def test_unactionable_kinds_get_no_button(self, kind):
        from systemu.interface.components.remediation_card import remediation_card_model
        model = remediation_card_model(_action(kind=kind))
        assert model["fix"]["button"] == "none"

    def test_unknown_kind_gets_no_button_and_guidance(self):
        from systemu.interface.components.remediation_card import remediation_card_model
        model = remediation_card_model(
            _action(kind="SOME_FUTURE_KIND", fix_command=None))
        assert model["fix"]["button"] == "none"
        assert model["fix"]["text"]          # never empty — guidance fallback


# ── severity → risk (via the ONE map: gate._SEVERITY_TO_RISK) ────────────────

class TestSeverityRisk:
    @pytest.mark.parametrize("severity,risk", [
        ("blocker", "high"), ("warning", "medium"), ("info", "low"),
        ("nonsense", "low"),                 # unknown → low (from_recovery_action parity)
    ])
    def test_severity_maps_to_risk(self, severity, risk):
        from systemu.interface.components.remediation_card import remediation_card_model
        model = remediation_card_model(_action(severity=severity))
        assert model["risk"] == risk
        assert model["severity"] == severity


# ── fix text: fix_command else guidance ──────────────────────────────────────

class TestFixText:
    def test_fix_text_prefers_fix_command(self):
        from systemu.interface.components.remediation_card import remediation_card_model
        model = remediation_card_model(_action(fix_command="sharing_on tools review tool_a"))
        assert model["fix"]["text"] == "sharing_on tools review tool_a"

    @pytest.mark.parametrize("kind", ["SKILL_MISSING", "FS_PERMISSION",
                                      "DRY_RUN_FAILED_BUG"])
    def test_fix_text_falls_back_to_guidance(self, kind):
        from systemu.interface.components.remediation_card import remediation_card_model
        model = remediation_card_model(_action(kind=kind, fix_command=None))
        assert model["fix"]["text"]          # non-empty guidance per kind


# ── problem column + the loop-kill pin ───────────────────────────────────────

class TestModelShape:
    def test_problem_carries_kind_and_reason(self):
        from systemu.interface.components.remediation_card import remediation_card_model
        a = _action()
        model = remediation_card_model(a)
        assert model["kind"] == a.kind
        assert model["problem"]["title"] == a.kind
        assert model["problem"]["reason"] == a.reason

    def test_model_never_carries_fix_url(self):
        """The self-referential fix_url must not survive into the view model."""
        from systemu.interface.components.remediation_card import remediation_card_model
        a = _action(fix_url="http://localhost:8765/recover/tool/tool_a")
        model = remediation_card_model(a)
        assert "fix_url" not in model
        assert a.fix_url not in repr(model)


# ── ensure_recovery_gate: enqueue-on-demand, never auto-exec ─────────────────
# Mirrors tests/test_ensure_scroll_gate.py (the Slice 2b precedent): real
# FileVault(Vault(tmp)) end-to-end, no queue mocks.

def _file_vault(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    return FileVault(Vault(tmp_path / "vault"))


def _recovery_descriptors(vault, action):
    """All PENDING gate descriptors for this action (recovery-scoped dedup)."""
    from systemu.interface.command.inbox import InboxQueue
    dedup = f"recovery:{action.scope_kind}:{action.scope_id}:{action.kind}"
    return [(dec_id, d) for dec_id, d in InboxQueue(vault).list_descriptors()
            if d.dedup == dedup]


class TestEnsureRecoveryGate:
    def test_no_existing_gate_enqueues_exactly_one(self, tmp_path):
        from systemu.interface.components.remediation_card import ensure_recovery_gate
        vault = _file_vault(tmp_path)
        action = _action(kind="GATE_1_PENDING")

        dec_id = ensure_recovery_gate(vault, action)

        rows = _recovery_descriptors(vault, action)
        assert len(rows) == 1
        assert rows[0][0] == dec_id
        desc = rows[0][1]
        # The from_recovery_action contract: Dismiss-first safe default,
        # recovery-scoped dedup, severity→risk.
        assert desc.options == ["Dismiss", "Approve & Apply"]
        assert desc.safe_default == "Dismiss"
        assert desc.dedup == f"recovery:tool:tool_a:GATE_1_PENDING"
        assert desc.risk == "high"            # blocker → high

    def test_second_call_returns_same_id_no_duplicate(self, tmp_path):
        from systemu.interface.components.remediation_card import ensure_recovery_gate
        vault = _file_vault(tmp_path)
        action = _action(kind="GATE_2_PENDING")

        first = ensure_recovery_gate(vault, action)
        second = ensure_recovery_gate(vault, action)

        assert first == second
        assert len(_recovery_descriptors(vault, action)) == 1

    def test_bypass_mode_posts_pending_and_never_executes(self, tmp_path, monkeypatch):
        """With the dial at Bypass, a policy'd enqueue would auto-grant the
        gate and run the executor (resolve_gate → doctor_apply) without ever
        posting.  ensure_recovery_gate must do the OPPOSITE: policy=None posts
        a pending row for inspection and doctor_apply NEVER runs."""
        monkeypatch.setenv("SYSTEMU_GATE_MODE", "bypass")
        calls = []
        monkeypatch.setattr(
            "systemu.interface.command.verbs.doctor_apply",
            lambda *a, **k: calls.append((a, k)))

        from systemu.interface.components.remediation_card import ensure_recovery_gate
        vault = _file_vault(tmp_path)
        action = _action(kind="GATE_1_PENDING")

        dec_id = ensure_recovery_gate(vault, action)

        assert calls == []                                   # executor NEVER ran
        rows = _recovery_descriptors(vault, action)
        assert len(rows) == 1 and rows[0][0] == dec_id       # pending row posted
        decision = vault.get_decision(dec_id)
        assert decision.status == "pending"                  # not auto-resolved


# ── Slice 2c wiring: the panel delegates; the fix_url loop is dead ───────────

class TestWiring:
    """Source pins (precedent: test_ensure_scroll_gate.TestWiring) — the
    recovery panel rows delegate to the remediation card and NOTHING in the
    recovery surface navigates to fix_url any more."""

    def test_recover_panel_delegates_to_remediation_card(self):
        import inspect
        from systemu.interface.pages import recover
        src = inspect.getsource(recover)
        assert "render_remediation_card" in src
        assert "Open Gate Review" not in src   # the loop button is gone
        assert "navigate" not in src           # NO fix_url navigation remains
        assert "resolve_name" in src           # v0_8_12 names pin stays true

    def test_remediation_card_never_navigates(self):
        import inspect
        from systemu.interface.components import remediation_card
        src = inspect.getsource(remediation_card)
        assert "navigate" not in src           # the loop cannot re-enter here
