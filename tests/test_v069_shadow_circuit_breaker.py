"""shadow iteration loop bails after N=3 consecutive same-tool
same-reason failures."""
from systemu.runtime.shadow_runtime import ShadowRuntime


def test_circuit_breaker_threshold_default_is_3():
    assert ShadowRuntime.CIRCUIT_BREAKER_FAILURES == 3


def test_circuit_breaker_helper_returns_true_after_threshold():
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt._consecutive_failures = []
    tripped = False
    for _ in range(3):
        tripped = rt._record_tool_failure("fetch_json", "DEP_PENDING")
    assert tripped is True


def test_circuit_breaker_not_tripped_below_threshold():
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt._consecutive_failures = []
    rt._record_tool_failure("fetch_json", "DEP_PENDING")
    tripped = rt._record_tool_failure("fetch_json", "DEP_PENDING")
    assert tripped is False  # only 2 occurrences


def test_circuit_breaker_resets_on_different_tool():
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt._consecutive_failures = []
    rt._record_tool_failure("fetch_json", "DEP_PENDING")
    rt._record_tool_failure("fetch_json", "DEP_PENDING")
    # Different tool — should reset the streak
    tripped = rt._record_tool_failure("create_word_doc", "FS_PERMISSION")
    assert tripped is False
    assert len(rt._consecutive_failures) == 1


def test_circuit_breaker_resets_on_different_reason():
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt._consecutive_failures = []
    rt._record_tool_failure("fetch_json", "DEP_PENDING")
    rt._record_tool_failure("fetch_json", "DEP_PENDING")
    tripped = rt._record_tool_failure("fetch_json", "FS_PERMISSION")
    assert tripped is False


def test_circuit_breaker_lazy_init_when_attribute_missing():
    """Helper should work even if _consecutive_failures wasn't initialized
    by __init__ (defensive against bypass-init test patterns)."""
    rt = ShadowRuntime.__new__(ShadowRuntime)
    # Note: deliberately NOT setting rt._consecutive_failures
    tripped = rt._record_tool_failure("x", "y")
    assert tripped is False
    assert hasattr(rt, "_consecutive_failures")
