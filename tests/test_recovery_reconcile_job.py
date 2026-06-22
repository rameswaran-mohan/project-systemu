"""reconcile_recovery_gates: enqueue new actions, expire self-healed ones."""
from unittest.mock import MagicMock


def test_reconcile_enqueues_new_and_expires_stale():
    import systemu.scheduler.jobs as jobs
    from systemu.recovery.engine import RecoveryAction

    enqueued, expired = [], []

    class _Inbox:
        def __init__(self, v): pass
        def enqueue(self, desc, *, gate_type, **k): enqueued.append(desc.dedup)

    class _Queue:
        def __init__(self, v): pass
        def list_pending(self):
            d = MagicMock()
            d.dedup_key = "recovery:tool:gone:DEP_PENDING"
            d.context = {"kind": "gate", "gate_type": "recovery"}
            return [d]
        def expire_by_dedup_key(self, k):
            expired.append(k)
            return True

    eng = MagicMock()
    eng.scan_all.return_value = [
        RecoveryAction("tool", "new", "DEP_PENDING", "r", "", "", "blocker")
    ]
    jobs.reconcile_recovery_gates(vault=MagicMock(), engine=eng,
                                  inbox_cls=_Inbox, queue_cls=_Queue)
    assert "recovery:tool:new:DEP_PENDING" in enqueued      # new action enqueued
    assert "recovery:tool:gone:DEP_PENDING" in expired      # self-healed expired
