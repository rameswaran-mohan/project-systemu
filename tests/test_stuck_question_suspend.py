"""v0.10.21 (fold-in) — a chat task that PAUSES on a 'Stuck on Objective N' question
must not be reported as a hard FAILURE with a bogus shell-command-approval message.

Live tryout ("why is systemu unable to finish my task?"): a burrito-search run
exhausted every data source (JustDial 403, DuckDuckGo timeout, Google 0 records,
Zomato returned biryani), correctly escalated with a structured_question
(Provide hint / Accept partial / Cancel run) via request_choice(dedup_key="stuck:..."),
which raises PendingOperatorDecision up to the Supervisor. The Supervisor's handler
was GENERIC — it stamped the command-gate result ("a shell command requires operator
approval ... re-run the task") onto EVERY PendingOperatorDecision, including this
stuck-question, and marked the activity FAILED. The user saw a scary failure with a
nonsense shell-command instruction instead of a clean "waiting for your answer".

Fix: the Supervisor classifies a PendingOperatorDecision by its dedup prefix. A
'stuck:' question is an operator QUESTION (resumed by resume_on_decision when
answered) → 'suspended_operator_question', which PARKS the activity (non-terminal,
no retry, no dead-letter, accurate message) exactly like suspended_harness_escalation.
An approval gate (command/tool/mcp) keeps the existing clean-deny behaviour.
"""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

from systemu.runtime.supervisor import Supervisor


def _supervisor_stub() -> Supervisor:
    """Minimal Supervisor that bypasses thread setup (mirrors test_harness_suspend_status)."""
    s = Supervisor.__new__(Supervisor)
    s.vault = SimpleNamespace()
    s._pending_lock = threading.Lock()
    s._pending_activity_ids = set()
    s._running_lock = threading.Lock()
    s._running = {}
    s._task_queue = None
    s._queue = queue.PriorityQueue()
    s._dl_lock = threading.Lock()
    s._dead_letters = []
    s._publish = lambda *a, **kw: None
    s._aname = lambda aid: aid
    return s


# ── classification: stuck question vs approval gate ──────────────────────────

def test_stuck_dedup_classifies_as_operator_question():
    sup = _supervisor_stub()
    pd = SimpleNamespace(decision_id="dec_q", dedup_key="stuck:scroll_1:obj_1:r1")
    result = sup._pending_decision_result(pd)
    assert result["status"] == "suspended_operator_question"
    # accurate, non-shell-command message
    assert "shell command" not in result["final_summary"].lower()
    assert "inbox" in result["final_summary"].lower()


def test_command_gate_dedup_still_clean_deny():
    sup = _supervisor_stub()
    pd = SimpleNamespace(decision_id="dec_c", dedup_key="command:abc123")
    result = sup._pending_decision_result(pd)
    assert result["status"] == "command_gate_blocked"


def test_tool_gate_dedup_still_clean_deny():
    sup = _supervisor_stub()
    pd = SimpleNamespace(decision_id="dec_t", dedup_key="tool:deadbeef")
    result = sup._pending_decision_result(pd)
    assert result["status"] == "command_gate_blocked"


# ── _handle_result: the stuck-question status PARKS, never fails ─────────────

def test_suspended_operator_question_parks_not_fails(monkeypatch):
    sup = _supervisor_stub()

    submit_calls: List[Dict[str, Any]] = []
    sup.submit = lambda *a, **kw: submit_calls.append(kw)  # type: ignore[assignment]

    timers_started: List[Any] = []
    real_timer = threading.Timer
    monkeypatch.setattr("systemu.runtime.supervisor.threading.Timer",
                        lambda *a, **kw: (timers_started.append(1), real_timer(*a, **kw))[1])

    threads_started: List[Any] = []
    real_thread = threading.Thread
    monkeypatch.setattr("systemu.runtime.supervisor.threading.Thread",
                        lambda *a, **kw: (threads_started.append(1), real_thread(*a, **kw))[1])

    # The park branch must NOT mutate the activity to a terminal state.
    save_calls: List[Any] = []
    sup.vault.save_activity = lambda act: save_calls.append(act)  # type: ignore[attr-defined]
    sup.vault.get_activity = lambda aid: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        AssertionError("get_activity must NOT be called for the operator-question park")
    )

    payload = {"activity_id": "act_q", "shadow_id": "sh_q", "retry_count": 0,
               "submission_id": "sub_q"}
    result = {
        "status": "suspended_operator_question",
        "execution_id": "exec_q",
        "final_summary": "Paused — waiting for your answer in the inbox "
                         "(Provide hint / Accept partial / Cancel run).",
    }

    sup._handle_result(payload, result)

    assert timers_started == [], "park must NOT schedule a retry timer"
    assert submit_calls == [], "park must NOT re-submit the activity"
    assert sup._dead_letters == [], "park must NOT dead-letter"
    assert threads_started == [], "park must NOT launch failure diagnosis"
    assert save_calls == [], "park must NOT mark the activity terminal (FAILED)"


# ── orphan-rerun guard: the recovery sweep must skip a parked activity ────────

def test_recovery_sweep_skips_activity_parked_on_pending_decision(monkeypatch):
    """Adversarial finding #1: leaving the paused activity ASSIGNED (non-terminal) re-
    exposes it to the startup/hourly recovery sweep (_resubmit_unexecuted_assigned),
    which re-submits ASSIGNED activities with an empty execution_log FROM SCRATCH (no
    resume_from) — resetting progress and re-executing effects every sweep, and racing
    the operator's answer-carrying resume. The sweep must SKIP an activity parked on a
    PENDING operator decision (resumed by resume_on_decision when answered). A genuinely
    orphaned ASSIGNED activity (no pending decision) is still recovered."""
    from systemu.scheduler import jobs
    from systemu.core.models import ActivityStatus

    acts = {
        "act_parked": SimpleNamespace(id="act_parked", assigned_shadow_id="sh_p",
                                      name="parked", origin="chat"),
        "act_orphan": SimpleNamespace(id="act_orphan", assigned_shadow_id="sh_o",
                                      name="orphan", origin="chat"),
    }
    shadows = {
        "sh_p": SimpleNamespace(id="sh_p", name="shp", execution_log=[]),
        "sh_o": SimpleNamespace(id="sh_o", name="sho", execution_log=[]),
    }

    class _Vault:
        def list_activities(self, status=None):
            if status == ActivityStatus.ASSIGNED:
                return [{"id": "act_parked", "status": "assigned"},
                        {"id": "act_orphan", "status": "assigned"}]
            return []
        def get_activity(self, aid):
            return acts[aid]
        def get_shadow(self, sid):
            return shadows[sid]

    submits: list = []

    class _Sup:
        def submit(self, aid, sid, **kw):
            submits.append(aid)

    monkeypatch.setattr("systemu.runtime.supervisor.Supervisor.get",
                        staticmethod(lambda: _Sup()))
    # Only the PARKED activity has a pending decision referencing it.
    monkeypatch.setattr(
        "systemu.approval.decision_queue.OperatorDecisionQueue.list_pending",
        lambda self: [SimpleNamespace(context={"activity_id": "act_parked"})],
    )

    jobs._resubmit_unexecuted_assigned(_Vault())

    assert "act_parked" not in submits, "parked-on-pending-decision activity must be SKIPPED"
    assert submits == ["act_orphan"], "a genuinely-orphaned ASSIGNED activity must still recover"
