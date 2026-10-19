"""IMPL-11 ruling 3 - an answer given BEFORE the cancel never resumes the run.

Witnessed gap: the operator answers a parked run's card from the CLI (a separate
process: only the daemon's reconciler poll will ever act on it), then stops the
run before that poll. The card is RESOLVED, not pending, so the cancellation
expires nothing - and with no expired card there was no witness at all: the
next poll re-dispatched the run the operator had just stopped.

Ruling under test: every cancel records a DURABLE run marker at cancel time
(``<vault root>/cancelled_runs.json``), keyed on what the reconciler can check -
execution id, chat submission, root execution id; never the activity, which a
later re-run of the same task shares. The resume reconciler consults it.

Real FileVault under tmp_path; a fake supervisor; no daemon, no LLM.
"""
from __future__ import annotations

import json
import threading
import types
from pathlib import Path

import pytest

from systemu.approval import decision_queue as dq
from systemu.approval.decision_queue import OperatorDecisionQueue
from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

RUN = {"execution_id": "exec_mk1", "activity_id": "act_mk1", "shadow_id": "sh_mk1",
       "chat_submission_id": "2026-10-19T17:00:00.000001"}
RERUN = {"execution_id": "exec_mk2", "activity_id": "act_mk1", "shadow_id": "sh_mk1",
         "chat_submission_id": "2026-10-19T18:00:00.000002"}


class _FakeSupervisor:
    def __init__(self):
        self.calls = []
        self._running = {}
        self._running_lock = threading.Lock()
        self._pending_activity_ids = set()
        self._pending_lock = threading.Lock()

    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append((activity_id, kw.get("resume_from_execution_id")))
        return "sub"


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _question(q, coords, n=1):
    ctx = {"kind": "structured_question", "objective_id": n,
           "questions": [{"id": "answer", "prompt": "hint?", "options": [],
                          "allow_free_text": True}]}
    ctx.update(coords)
    return q.post(title=f"Stuck on objective {n}", body="b", options=["Submit"],
                  context=ctx, dedup_key=f"stuck:{coords['execution_id']}:{n}")


def _answer_then_cancel(tmp_path, cancel):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    did = _question(q, RUN)
    q.resolve(did, choice='{"answer": "use the other folder"}')   # e.g. from the CLI
    cancel(vault, q)
    assert vault.get_decision(did).status == "resolved"
    return vault, did


def _queue_cancel(vault, q):
    assert q.expire_cards_for_run(
        execution_id=RUN["execution_id"], activity_id=RUN["activity_id"],
        chat_submission_id=RUN["chat_submission_id"]) == []   # nothing was pending


def _chat_stop_cancel(vault, q):
    from systemu.runtime.chat_task_registry import cancel_chat_task

    class _ChatVault:
        def __init__(self, inner):
            self._inner = inner
            self.entries = [{"ts": RUN["chat_submission_id"], "prompt": "x",
                             "status": "pending_decision",
                             "activity_id": RUN["activity_id"]}]

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def load_chat_history(self, limit=50):
            return list(self.entries)

        def update_chat_history_entry(self, ts, updates):
            self.entries[0].update(updates)

    report = cancel_chat_task(_ChatVault(vault), RUN["chat_submission_id"], queue=q)
    assert report["stopped"] is True


def _supervisor_cancel(vault, q):
    from systemu.runtime.supervisor import Supervisor
    sup = Supervisor.__new__(Supervisor)
    sup.vault = vault
    sup._task_queue = None
    sup._publish = lambda *a, **k: None
    sup._aname = lambda a: a
    sup._snapshot_data_dir = Path(vault.root).parent / "data"
    sup._handle_result({"activity_id": RUN["activity_id"], "shadow_id": RUN["shadow_id"],
                        "chat_submission_id": RUN["chat_submission_id"]},
                       {"status": "cancelled", "execution_id": RUN["execution_id"]})


CANCEL_PATHS = [
    pytest.param(_queue_cancel, id="expire_cards_for_run"),
    pytest.param(_chat_stop_cancel, id="chat-Stop"),
    pytest.param(_supervisor_cancel, id="supervisor-CANCELLED-finalizer"),
]


@pytest.mark.parametrize("cancel", CANCEL_PATHS)
def test_an_answer_given_before_the_cancel_never_resumes_the_run(tmp_path, cancel):
    vault, did = _answer_then_cancel(tmp_path, cancel)
    sup = _FakeSupervisor()
    assert reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data") == 0
    assert sup.calls == [], (
        "MUTATION PIN: the reconciler resumed a run cancelled after its card was answered")


@pytest.mark.parametrize("cancel", CANCEL_PATHS)
def test_the_marker_is_durable_and_keyed_on_the_run(tmp_path, cancel):
    vault, _ = _answer_then_cancel(tmp_path, cancel)
    path = Path(vault.root) / "cancelled_runs.json"
    assert path.is_file(), "no durable marker was recorded at cancel time"
    json.loads(path.read_text(encoding="utf-8"))          # well-formed
    fresh = _vault(tmp_path)                              # a new process's view
    marks = dq.cancelled_run_coordinates(fresh)
    assert ("chat_submission_id", RUN["chat_submission_id"]) in marks
    assert not any(key == "activity_id" for key, _ in marks), (
        "the marker must never key on the activity a re-run shares")


def test_a_rerun_of_the_same_task_still_resumes(tmp_path):
    vault, _ = _answer_then_cancel(tmp_path, _queue_cancel)
    q = OperatorDecisionQueue(vault)
    again = _question(q, RERUN, n=2)
    q.resolve(again, choice='{"answer": "ok"}')
    sup = _FakeSupervisor()
    assert reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data") == 1
    assert sup.calls == [(RERUN["activity_id"], RERUN["execution_id"])]


def test_the_root_execution_is_part_of_the_marker(tmp_path):
    """Resumes of one run share its ROOT: a card posted by the root execution,
    answered before the cancel of a later execution, is the same run."""
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    root_card = _question(q, {**RUN, "execution_id": "exec_root"})
    q.resolve(root_card, choice='{"answer": "x"}')
    q.expire_cards_for_run(execution_id=RUN["execution_id"],
                           root_execution_id="exec_root")
    sup = _FakeSupervisor()
    assert reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data") == 0
    assert ("root_execution_id", "exec_root") in dq.cancelled_run_coordinates(vault)


def test_the_supervisor_records_the_root_from_the_runs_snapshot(tmp_path):
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    vault = _vault(tmp_path)
    data_dir = Path(vault.root).parent / "data"
    write_snapshot(ExecutionSnapshot(execution_id=RUN["execution_id"],
                                     shadow_id=RUN["shadow_id"], scroll_id="scr",
                                     activity_id=RUN["activity_id"],
                                     root_execution_id="exec_root"),
                   data_dir=data_dir)
    _supervisor_cancel(vault, OperatorDecisionQueue(vault))
    marks = dq.cancelled_run_coordinates(vault)
    assert ("execution_id", RUN["execution_id"]) in marks
    assert ("root_execution_id", "exec_root") in marks


def test_retention_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "CANCELLED_RUNS_RETENTION", 3)
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    for i in range(5):
        q.expire_cards_for_run(execution_id=f"exec_r{i}")
    marks = dq.cancelled_run_coordinates(vault)
    assert ("execution_id", "exec_r4") in marks
    assert ("execution_id", "exec_r0") not in marks
    assert len(json.loads((Path(vault.root) / "cancelled_runs.json")
                          .read_text(encoding="utf-8"))["runs"]) == 3


def test_a_damaged_marker_never_breaks_a_cancel(tmp_path):
    vault = _vault(tmp_path)
    (Path(vault.root) / "cancelled_runs.json").write_text("{not json", encoding="utf-8")
    q = OperatorDecisionQueue(vault)
    q.expire_cards_for_run(execution_id="exec_after_damage")
    assert ("execution_id", "exec_after_damage") in dq.cancelled_run_coordinates(vault)
