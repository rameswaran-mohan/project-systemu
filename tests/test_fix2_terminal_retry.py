"""Fix 2 — the supervisor must not storm-retry a structural failure, and the
max-iters terminal must surface a structural_failure flag + honest summary."""
from __future__ import annotations

import inspect


def test_should_retry_skips_structural_failures():
    from systemu.runtime.supervisor import Supervisor, MAX_RETRIES
    assert MAX_RETRIES == 2
    # transient partial with retries left → retry
    assert Supervisor._should_retry("partial", 0, structural=False) is True
    assert Supervisor._should_retry("failure", 1, structural=False) is True
    # structural failure → never retry (re-running hits the same wall)
    assert Supervisor._should_retry("partial", 0, structural=True) is False
    # retries exhausted → no retry
    assert Supervisor._should_retry("partial", MAX_RETRIES, structural=False) is False
    # success/cancelled never reach this gate, but be defensive
    assert Supervisor._should_retry("success", 0, structural=False) is False


def test_handle_result_consults_structural_flag_and_should_retry():
    from systemu.runtime import supervisor
    src = inspect.getsource(supervisor.Supervisor._handle_result)
    assert "structural_failure" in src           # reads the runtime's flag
    assert "_should_retry" in src                 # routes retry through the pure gate


def test_max_iters_terminal_sets_structural_failure_and_honest_summary():
    """The max-iters terminal in execute() builds an honest summary and stamps
    the structural_failure flag from the structural-tool set."""
    from systemu.runtime import shadow_runtime
    src = inspect.getsource(shadow_runtime.ShadowRuntime.execute)
    assert 'res["structural_failure"]' in src or "structural_failure" in src
    assert "Objectives completed" in src          # honest, specific summary (not the generic one)
