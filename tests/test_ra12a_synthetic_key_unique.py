"""R-A12a (concurrency fix 2) — the no-execution_id retry-arm key must be RUN-UNIQUE.

On the worker-thread exception path the result dict carries NO ``execution_id``
(and, verified, NO ``root_execution_id`` — supervisor.py ~:1148 builds
``{"status":"failure","error":...,"final_summary":...}`` only). ``_arm_durable_retry``
falls back to a synthetic key. The OLD key ``f"retryarm-{activity_id}-{attempt}"``
depends ONLY on activity + attempt, so two *distinct* runs of the same activity that
both fail at the same attempt on this path collide on one ``exec_retryarm-A-0``
snapshot and one ``wait_id``.

The review-confirmed LOST-RETRY (HIGH): Run 1 arms ``retryarm-A-0`` then its wait is
later dropped stale/exhausted (stamped ``dispatched``, snapshot NOT deleted). A LATER
Run 2 of A that also fails at attempt 0 with no execution_id reads the SURVIVING
``exec_retryarm-A-0`` snapshot; ``arm_wait`` sees the same ``wait_id`` already present
→ no-ops → **Run 2's retry is silently never armed.**

Fix: fold the run-stable-but-run-unique ``shadow_id`` into the key —
``f"retryarm-{activity_id}-{shadow_id}-{attempt}"`` — so distinct runs land in
distinct snapshot dirs / ``wait_id``s, while a re-arm of the SAME run + attempt keeps
the SAME key (idempotent — no uuid4/now, which would break dedupe).

Drives ``Supervisor._arm_durable_retry`` directly via the bare-supervisor harness
from ``tests/test_ra12a_supervisor_durable_retry.py``.
"""
from __future__ import annotations

from systemu.runtime.execution_snapshot import read_snapshot

from test_ra12a_supervisor_durable_retry import _bare_supervisor


def _arm(sup, *, shadow_id, activity_id="act", attempt=0, now=1_000_000.0):
    """Arm a durable retry on the no-execution_id (synthetic-key) path."""
    return sup._arm_durable_retry(
        execution_id=None,          # worker-thread exception path — no execution_id
        activity_id=activity_id,
        shadow_id=shadow_id,
        root_execution_id=None,     # also absent on this path (verified)
        scroll_id="scr",
        delay_s=5.0,
        attempt=attempt,
        max_attempts=5,
        now=now,
    )


def test_distinct_runs_get_distinct_keys(tmp_path):
    """Two distinct runs (different shadow_id) of the SAME activity + attempt on the
    no-execution_id path must land in DIFFERENT snapshot dirs with DIFFERENT wait_ids,
    so the second run's retry is actually armed (not deduped away against the first
    run's surviving snapshot)."""
    sup = _bare_supervisor(tmp_path)

    rec1 = _arm(sup, shadow_id="sh_A", activity_id="act", attempt=0)
    rec2 = _arm(sup, shadow_id="sh_B", activity_id="act", attempt=0)

    assert rec1 is not None and rec2 is not None
    # Run-unique key: distinct wait_ids AND distinct snapshot homes (eid).
    assert rec1["wait_id"] != rec2["wait_id"]
    assert rec1["execution_id"] != rec2["execution_id"]

    # BOTH runs' retries are armed — each in its own snapshot, each with its own wait.
    snap1 = read_snapshot(rec1["execution_id"], data_dir=tmp_path)
    snap2 = read_snapshot(rec2["execution_id"], data_dir=tmp_path)
    assert snap1 is not None and snap2 is not None
    assert [w["wait_id"] for w in snap1.pending_waits] == [rec1["wait_id"]]
    assert [w["wait_id"] for w in snap2.pending_waits] == [rec2["wait_id"]]


def test_surviving_dropped_wait_does_not_swallow_second_runs_retry(tmp_path):
    """The exact LOST-RETRY shape: Run 1's wait is dropped (stamped dispatched, its
    snapshot survives on disk). A later Run 2 (different shadow_id) at the same
    activity + attempt must STILL arm its own retry — not read Run 1's surviving
    snapshot and dedupe itself into oblivion."""
    from systemu.runtime.execution_snapshot import write_snapshot

    sup = _bare_supervisor(tmp_path)

    rec1 = _arm(sup, shadow_id="sh_1", activity_id="act", attempt=0)
    # Simulate the reconciler dropping Run 1's wait: stamp dispatched, keep the file.
    snap1 = read_snapshot(rec1["execution_id"], data_dir=tmp_path)
    snap1.pending_waits[0]["dispatched"] = True
    write_snapshot(snap1, data_dir=tmp_path)

    rec2 = _arm(sup, shadow_id="sh_2", activity_id="act", attempt=0)
    assert rec2 is not None
    assert rec2["wait_id"] != rec1["wait_id"]
    snap2 = read_snapshot(rec2["execution_id"], data_dir=tmp_path)
    assert snap2 is not None
    # Run 2's retry is present AND undispatched (it will actually fire).
    match = [w for w in snap2.pending_waits if w["wait_id"] == rec2["wait_id"]]
    assert len(match) == 1
    assert match[0]["dispatched"] is False


def test_same_run_attempt_rearm_is_idempotent(tmp_path):
    """A re-arm of the SAME run (same shadow_id) + same attempt keeps the SAME
    wait_id and does NOT accumulate twin waits — the key stays stable (no uuid4/now)."""
    sup = _bare_supervisor(tmp_path)

    rec1 = _arm(sup, shadow_id="sh_X", activity_id="act", attempt=0)
    rec2 = _arm(sup, shadow_id="sh_X", activity_id="act", attempt=0)

    assert rec1["wait_id"] == rec2["wait_id"]
    assert rec1["execution_id"] == rec2["execution_id"]
    snap = read_snapshot(rec1["execution_id"], data_dir=tmp_path)
    assert snap is not None
    assert len(snap.pending_waits) == 1   # deduped — one wait, not twins


def test_submission_id_is_the_preferred_provably_unique_key(tmp_path):
    """When a submission_id reaches the arm (the REAL result path threads
    payload['submission_id']), it is preferred over shadow_id — so two runs sharing
    activity+shadow+attempt but with DISTINCT sub_<uuid> submission_ids get DISTINCT
    keys (shadow_id is run-distinguishing but not provably unique). Same submission
    stays idempotent."""
    sup = _bare_supervisor(tmp_path)

    def _arm_sub(sub):
        return sup._arm_durable_retry(
            execution_id=None, activity_id="act", shadow_id="sh", root_execution_id=None,
            scroll_id="scr", delay_s=5.0, attempt=0, max_attempts=5, now=1_000_000.0,
            submission_id=sub)

    r1 = _arm_sub("sub_aaaa")
    r2 = _arm_sub("sub_bbbb")
    assert r1["execution_id"] == "retryarm-sub_aaaa-0"          # submission_id, not shadow_id
    assert r1["execution_id"] != r2["execution_id"]             # distinct submissions → distinct keys
    assert r1["wait_id"] != r2["wait_id"]

    r1b = _arm_sub("sub_aaaa")                                  # re-arm same submission → idempotent
    assert r1b["wait_id"] == r1["wait_id"]
    snap = read_snapshot(r1["execution_id"], data_dir=tmp_path)
    assert snap is not None and len(snap.pending_waits) == 1
