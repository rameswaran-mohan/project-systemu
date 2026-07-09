"""R-A12a: pure record model + helpers for durable retry timers.

These tests pin the shape of the ``pending_waits`` records stored on
``ExecutionSnapshot`` and the pure helpers that operate over them. No
snapshot/reconciler wiring here (later tasks) — this is the model layer only.
"""

from systemu.runtime import pending_waits as pw


def test_make_retry_wait_shape():
    r = pw.make_retry_wait(execution_id="e1", activity_id="a1", shadow_id="s1",
                           root_execution_id="e0", delay_s=5.0, attempt=1, max_attempts=3, now=100.0)
    assert r["wait_kind"] == "retry"
    assert r["execution_id"] == "e1" and r["activity_id"] == "a1" and r["shadow_id"] == "s1"
    assert r["root_execution_id"] == "e0"
    assert r["fire_at"] == 105.0            # now + delay_s
    assert r["attempt"] == 1 and r["max_attempts"] == 3
    assert r["dispatched"] is False
    assert r["created_at"] == 100.0
    assert isinstance(r["wait_id"], str) and r["wait_id"]


def test_wait_id_is_stable_for_same_run_attempt():
    a = pw.make_retry_wait(execution_id="e1", activity_id="a1", shadow_id="s1", root_execution_id="e0",
                           delay_s=5, attempt=2, max_attempts=3, now=1.0)
    b = pw.make_retry_wait(execution_id="e1", activity_id="a1", shadow_id="s1", root_execution_id="e0",
                           delay_s=5, attempt=2, max_attempts=3, now=999.0)
    assert a["wait_id"] == b["wait_id"]     # stable → re-arming the same attempt is idempotent (dedupe key)


def test_due_waits_returns_only_undispatched_and_due():
    waits = [
        {"wait_id": "w1", "fire_at": 100.0, "dispatched": False},
        {"wait_id": "w2", "fire_at": 200.0, "dispatched": False},   # not due
        {"wait_id": "w3", "fire_at": 50.0, "dispatched": True},     # already dispatched
    ]
    due = pw.due_waits(waits, now=150.0)
    assert [w["wait_id"] for w in due] == ["w1"]


def test_mark_dispatched_flips_one():
    waits = [{"wait_id": "w1", "dispatched": False}, {"wait_id": "w2", "dispatched": False}]
    out = pw.mark_dispatched(waits, "w1")
    assert [w["dispatched"] for w in out] == [True, False]


def test_arm_wait_appends_and_dedupes(monkeypatch):
    class C:
        pass
    c = C()
    r = pw.make_retry_wait(execution_id="e1", activity_id="a1", shadow_id="s1", root_execution_id="e0",
                           delay_s=5, attempt=1, max_attempts=3, now=1.0)
    pw.arm_wait(c, r)
    pw.arm_wait(c, r)     # same wait_id → NOT duplicated
    assert len(c._pending_waits) == 1


def test_expire_all_marks_every_wait_dispatched():
    waits = [{"wait_id": "w1", "dispatched": False}, {"wait_id": "w2", "dispatched": False}]
    assert all(w["dispatched"] for w in pw.expire_all(waits))


def test_is_exhausted():
    assert pw.is_exhausted({"attempt": 3, "max_attempts": 3}) is True
    assert pw.is_exhausted({"attempt": 1, "max_attempts": 3}) is False
