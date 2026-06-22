"""v0.8.22.1 follow-up: cross-process resume reconciler.

The EventBus is a process-wide singleton (``systemu/interface/event_bus.py``).
The v0.8.22.1 EventBus subscriber registered in the daemon only fires for
resolutions that happen in the daemon process. A CLI ``sharing_on decisions
resolve --choice ...`` runs in a SEPARATE process — its EventBus publish
never reaches the daemon subscriber, so the stuck chat task was marked
resolved but never resumed.

This test module verifies the daemon-side poll
(``scheduler.jobs.reconcile_resolved_stuck_decisions``) catches those
out-of-process resolutions and re-dispatches the parked activity exactly
once, while staying idempotent across restarts and across both trigger
paths via the persisted ``decision.context["resume_dispatched"]`` flag.
"""
from __future__ import annotations

import json

import pytest


def _make_vault(tmp_path):
    """Build a Vault instance with the dir layout the existing v0.8.22.1
    tests use (mirrors ``TestResumeOnDecisionHandler._vault``)."""
    from systemu.vault.vault import Vault
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications",
        "executions", "decisions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in [
        "scrolls", "activities", "shadow_army", "skills", "tools",
        "evolutions", "decisions",
    ]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _seed_snapshot(tmp_path, *, execution_id="exec_R", shadow_id="sh_R",
                   scroll_id="scroll_R", activity_id="act_R"):
    """Seed an ExecutionSnapshot at the data_dir the reconciler will read."""
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    data_dir = tmp_path / "data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    write_snapshot(
        ExecutionSnapshot(
            execution_id=execution_id, shadow_id=shadow_id,
            scroll_id=scroll_id, activity_id=activity_id,
            completed_objective_ids=[0],
        ),
        data_dir=data_dir,
    )
    return data_dir


class _FakeSupervisor:
    """Records submit() calls so we can assert exact dispatch counts."""
    def __init__(self):
        self.calls = []

    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append((activity_id, shadow_id, kw))
        return f"sub_{len(self.calls)}"


class TestCrossProcessReconciler:
    def test_reconciler_dispatches_cli_resolved_decision(self, tmp_path):
        """Test A: a decision resolved via the queue WITHOUT any
        EventBus subscriber (simulating the CLI's separate-process
        resolution) must be re-dispatched when the reconciler runs.
        """
        from systemu.runtime import resume_on_decision as rod
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

        # Clear the in-memory dedup so prior tests don't bleed in.
        rod._handled.clear()

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(tmp_path)

        # Simulate the CLI path: post + resolve happen in a "fresh" queue
        # against the vault.  No register() call → no EventBus subscriber
        # listens for this resolution.  This mirrors `sharing_on decisions
        # resolve` running in its own subprocess.
        queue = OperatorDecisionQueue(vlt)
        did = queue.post(
            title="Stuck on Objective 1", body="?",
            options=["Provide hint", "Accept partial", "Cancel run", "Other"],
            context={
                "kind": "structured_question",
                "chat_submission_id": "2026-06-05T10:00:00",
                "execution_id": "exec_R", "activity_id": "act_R",
                "shadow_id": "sh_R", "scroll_id": "scroll_R",
                "objective_id": 1,
            },
            dedup_key="stuck:scroll_R:obj_1:r1",
        )
        queue.resolve(did, choice=json.dumps({"action": "I'm in Bangalore"}))

        # Reality check: the resolved decision has NOT yet been marked dispatched.
        before = vlt.get_decision(did)
        assert before.status == "resolved"
        assert not (before.context or {}).get("resume_dispatched")

        # Run the reconciler — this is the daemon-side poll job.
        sup = _FakeSupervisor()
        dispatched = reconcile_resolved_stuck_decisions(
            vlt, sup, data_dir=data_dir,
        )

        # Exactly one re-dispatch with the right resume coords.
        assert dispatched == 1
        assert len(sup.calls) == 1
        aid, sid, kw = sup.calls[0]
        assert aid == "act_R"
        assert sid == "sh_R"
        assert kw["resume_from_execution_id"] == "exec_R"
        assert kw["chat_submission_id"] == "2026-06-05T10:00:00"
        assert kw.get("consult_affinity_log") is False

        # The persisted flag is now set — ready for idempotency test below.
        after = vlt.get_decision(did)
        assert after.context.get("resume_dispatched") is True

        # The operator's answer was stashed in the snapshot.
        from systemu.runtime.execution_snapshot import read_snapshot
        snap = read_snapshot("exec_R", data_dir=data_dir)
        stash = [n for n in snap.sticky_notes if n.startswith("__STUCK_ANSWER__::obj_1::")]
        assert stash and "Bangalore" in stash[0]

    def test_reconciler_is_idempotent_across_runs(self, tmp_path):
        """Test B: running the reconciler twice must NOT re-dispatch.
        The persisted ``resume_dispatched`` flag is the cross-restart
        / cross-path source of truth (the in-memory ``_handled`` set
        alone is not enough — it would be cleared on daemon restart)."""
        from systemu.runtime import resume_on_decision as rod
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

        rod._handled.clear()

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(tmp_path, execution_id="exec_B",
                                  shadow_id="sh_B", scroll_id="sc_B",
                                  activity_id="act_B")

        queue = OperatorDecisionQueue(vlt)
        did = queue.post(
            title="Stuck on Objective 1", body="?",
            options=["Provide hint", "Other"],
            context={
                "kind": "structured_question",
                "chat_submission_id": "ts-B", "execution_id": "exec_B",
                "activity_id": "act_B", "shadow_id": "sh_B",
                "scroll_id": "sc_B", "objective_id": 1,
            },
            dedup_key="stuck:sc_B:obj_1:r1",
        )
        queue.resolve(did, choice=json.dumps({"action": "hint"}))

        sup = _FakeSupervisor()

        # First pass: one real dispatch.
        n1 = reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir)
        assert n1 == 1
        assert len(sup.calls) == 1

        # Second pass: persisted flag short-circuits — no double dispatch.
        n2 = reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir)
        assert n2 == 0
        assert len(sup.calls) == 1

        # Simulate a daemon restart: clear the in-memory dedup.  The
        # persisted flag is the only thing preventing double dispatch now.
        rod._handled.clear()
        n3 = reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir)
        assert n3 == 0
        assert len(sup.calls) == 1

    def test_eventbus_dispatch_blocks_subsequent_poll(self, tmp_path):
        """Test B variant: if the EventBus path dispatched first, the
        reconciler must see the persisted flag and skip — even with a
        fresh in-memory ``_handled`` set."""
        from systemu.runtime import resume_on_decision as rod
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

        rod._handled.clear()

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(tmp_path, execution_id="exec_E",
                                  shadow_id="sh_E", scroll_id="sc_E",
                                  activity_id="act_E")

        queue = OperatorDecisionQueue(vlt)
        did = queue.post(
            title="Stuck on Objective 1", body="?",
            options=["Provide hint", "Other"],
            context={
                "kind": "structured_question",
                "chat_submission_id": "ts-E", "execution_id": "exec_E",
                "activity_id": "act_E", "shadow_id": "sh_E",
                "scroll_id": "sc_E", "objective_id": 1,
            },
            dedup_key="stuck:sc_E:obj_1:r1",
        )
        queue.resolve(did, choice=json.dumps({"action": "hint"}))

        sup = _FakeSupervisor()

        # Simulate the EventBus path firing first (in-daemon resolution).
        ev = {
            "category": "operator_decision_resolved",
            "context": {"decision_id": did, "choice": "x",
                        "chat_submission_id": "ts-E"},
        }
        rod.handle_decision_resolved(ev, vault=vlt, supervisor=sup, data_dir=data_dir)
        assert len(sup.calls) == 1

        # Now the reconciler ticks.  Even after clearing _handled (which
        # would happen on a daemon restart), the persisted flag stops it.
        rod._handled.clear()
        n = reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir)
        assert n == 0
        assert len(sup.calls) == 1

    def test_reconciler_skips_non_resumable_decisions(self, tmp_path):
        """Sanity: the reconciler must skip resolved decisions that aren't
        structured_question, don't carry chat_submission_id, or lack
        resume coordinates.  Otherwise a forge_tool decision (also
        persisted in the same index) would trigger a spurious submit."""
        from systemu.runtime import resume_on_decision as rod
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

        rod._handled.clear()

        vlt = _make_vault(tmp_path)
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)

        queue = OperatorDecisionQueue(vlt)

        # 1. Wrong kind — forge_tool decision.
        did1 = queue.post(
            title="Forge?", body="?", options=["Skip", "Forge"],
            context={"kind": "forge_tool"}, dedup_key="tool_forge:t1",
        )
        queue.resolve(did1, choice="Forge")

        # 2. structured_question but missing chat_submission_id.
        did2 = queue.post(
            title="Stuck", body="?", options=["A", "B"],
            context={
                "kind": "structured_question",
                "execution_id": "ex", "activity_id": "act",
                "shadow_id": "sh", "scroll_id": "sc", "objective_id": 1,
            },
            dedup_key="stuck:sc:obj_1:r1",
        )
        queue.resolve(did2, choice=json.dumps({"action": "x"}))

        # 3. structured_question + chat_submission_id but no resume coords.
        did3 = queue.post(
            title="Stuck", body="?", options=["A"],
            context={
                "kind": "structured_question",
                "chat_submission_id": "ts-3",
                # execution_id/activity_id/shadow_id all missing
            },
            dedup_key="stuck:nocoords:r1",
        )
        queue.resolve(did3, choice=json.dumps({"action": "x"}))

        sup = _FakeSupervisor()
        n = reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir)
        assert n == 0
        assert sup.calls == []


class TestRefactoredEventBusPath:
    """Test C: the refactored ``handle_decision_resolved`` must still
    work through the EventBus path.  This is the back-compat regression
    test — the existing ``TestResumeOnDecisionHandler`` suite in
    ``tests/test_v0_8_22_1_resumable_decisions.py`` covers the same
    surface; we add one more here that specifically asserts the
    persisted flag is now stamped from the EventBus path too."""

    def test_eventbus_path_stamps_persisted_flag(self, tmp_path):
        from systemu.runtime import resume_on_decision as rod
        from systemu.approval.decision_queue import OperatorDecisionQueue

        rod._handled.clear()

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(tmp_path, execution_id="exec_F",
                                  shadow_id="sh_F", scroll_id="sc_F",
                                  activity_id="act_F")
        queue = OperatorDecisionQueue(vlt)
        did = queue.post(
            title="Stuck on Objective 1", body="?",
            options=["Provide hint", "Other"],
            context={
                "kind": "structured_question",
                "chat_submission_id": "ts-F", "execution_id": "exec_F",
                "activity_id": "act_F", "shadow_id": "sh_F",
                "scroll_id": "sc_F", "objective_id": 1,
            },
            dedup_key="stuck:sc_F:obj_1:r1",
        )
        queue.resolve(did, choice=json.dumps({"action": "hint"}))

        sup = _FakeSupervisor()
        ev = {
            "category": "operator_decision_resolved",
            "context": {"decision_id": did, "choice": "x",
                        "chat_submission_id": "ts-F"},
        }
        rod.handle_decision_resolved(ev, vault=vlt, supervisor=sup, data_dir=data_dir)

        assert len(sup.calls) == 1
        # The refactor's headline guarantee: the persisted flag is set.
        after = vlt.get_decision(did)
        assert after.context.get("resume_dispatched") is True
