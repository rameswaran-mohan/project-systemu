"""W12 — A2-audit pipeline findings F7/F9/F10.

F7: a freshly refined scroll must ring needs-you immediately (gates were
enqueue-on-render only — invisible until the operator visited /work).
F9: an objective claim consumed by a FAILED tool call must not be lost —
when the same tool later succeeds without a claim, the model is nudged to
re-state it (the A2 run finished its deliverable but was never credited;
the watchdog cancelled a finished run and the retry re-did paid work).
F10: the watchdog's no-heartbeat window is env-tunable for slow models.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.vault.vault import Vault


class TestEagerScrollGate:
    def test_refiner_ensures_the_gate_at_save(self):
        from systemu.pipelines import scroll_refiner
        src = inspect.getsource(scroll_refiner)
        assert "ensure_scroll_gate(vault, scroll)" in src, \
            "a fresh proposal must surface itself — not wait for a /work visit"

    def test_gate_lands_in_pending_decisions(self, tmp_path: Path):
        """ensure_scroll_gate on a pending scroll puts a row in the queue
        (the needs-you badge/rail/Inbox all read from it)."""
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.interface.command.inbox import InboxQueue
        from systemu.interface.scroll_gate import ensure_scroll_gate
        vault = Vault(str(tmp_path / "vault"))
        scroll = Scroll(
            id="scroll_f7test", name="F7 scroll",
            source_session_id="sess_f7",
            raw_instructions_path="captures/sess_f7/instructions.md",
            narrative_md="Do the thing.", intent="F7 verification",
            status=ScrollStatus.PENDING_APPROVAL)
        vault.save_scroll(scroll)
        ensure_scroll_gate(vault, scroll)
        rows = list(InboxQueue(vault).list_descriptors())
        descriptors = [r[1] if isinstance(r, tuple) else r for r in rows]
        assert any(getattr(d, "dedup", "") == "scroll:scroll_f7test"
                   for d in descriptors)


class TestFailedClaimNudge:
    def test_runtime_remembers_failed_claims(self):
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        assert "_failed_objective_claims" in src
        assert "credit_nudge" in src, \
            "a success after a failed claim must nudge the re-claim"

    def test_nudge_message_names_the_objective(self):
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        assert "completes_objective={_missed}" in src


class TestStuckThresholdTunable:
    def test_default_is_300(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_STUCK_THRESHOLD_S", raising=False)
        from systemu.runtime.supervisor import _resolve_stuck_threshold
        assert _resolve_stuck_threshold() == 300

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_STUCK_THRESHOLD_S", "900")
        from systemu.runtime.supervisor import _resolve_stuck_threshold
        assert _resolve_stuck_threshold() == 900

    def test_invalid_and_too_short_fall_back(self, monkeypatch):
        from systemu.runtime.supervisor import _resolve_stuck_threshold
        monkeypatch.setenv("SYSTEMU_STUCK_THRESHOLD_S", "banana")
        assert _resolve_stuck_threshold() == 300
        monkeypatch.setenv("SYSTEMU_STUCK_THRESHOLD_S", "5")
        assert _resolve_stuck_threshold() == 300
