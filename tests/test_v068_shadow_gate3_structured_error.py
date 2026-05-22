from unittest.mock import MagicMock


def test_shadow_gate3_returns_none_when_tool_is_clean():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    tool = MagicMock()
    tool.id = "tool_a"
    tool.name = "fetch_json"
    tool.enabled = True
    tool.dry_run_status = "passed"
    tool.dry_run_evidence = None

    rt = ShadowRuntime.__new__(ShadowRuntime)
    assert rt._gate3_check(tool) is None


def test_shadow_gate3_returns_structured_error_for_disabled():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    tool = MagicMock()
    tool.id = "tool_a"
    tool.name = "fetch_json"
    tool.enabled = False
    tool.dry_run_status = "passed"
    tool.dry_run_evidence = None

    rt = ShadowRuntime.__new__(ShadowRuntime)
    err = rt._gate3_check(tool)
    assert err is not None
    assert err["reason"] == "GATE_3_DISABLED"
    assert "/recover/tool/tool_a" in err["action_url"]


def test_shadow_gate3_returns_structured_error_for_dep_pending_tool():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    tool = MagicMock()
    tool.id = "tool_a"
    tool.name = "fetch_json"
    tool.enabled = True
    tool.dry_run_status = "failed"
    tool.dry_run_evidence = {
        "error": "ImportError: No module named 'requests'",
        "classified_reason": "DEP_PENDING",
        "missing_package": "requests",
    }

    rt = ShadowRuntime.__new__(ShadowRuntime)
    err = rt._gate3_check(tool)
    assert err is not None
    assert err["reason"] == "DEP_PENDING"
    assert err["missing_package"] == "requests"
    assert "/recover/tool/tool_a" in err["action_url"]


def test_shadow_gate3_fallback_kind_when_no_evidence():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    tool = MagicMock()
    tool.id = "tool_a"
    tool.name = "fetch_json"
    tool.enabled = True
    tool.dry_run_status = "failed"
    tool.dry_run_evidence = None

    rt = ShadowRuntime.__new__(ShadowRuntime)
    err = rt._gate3_check(tool)
    assert err is not None
    assert err["reason"] == "DRY_RUN_FAILED_BUG"


def test_shadow_gate3_message_points_to_recovery_url():
    """Gate 3 error messages must reference the recovery URL,
    NOT 'Re-forge with feedback' (the v0.6.8 message was misleading)."""
    from unittest.mock import MagicMock
    from systemu.runtime.shadow_runtime import ShadowRuntime
    tool = MagicMock()
    tool.id = "tool_a"
    tool.name = "fetch_json"
    tool.enabled = True
    tool.dry_run_status = "failed"
    tool.dry_run_evidence = {
        "error": "ImportError: No module named 'requests'",
        "classified_reason": "DEP_PENDING",
        "missing_package": "requests",
    }
    rt = ShadowRuntime.__new__(ShadowRuntime)
    err = rt._gate3_check(tool)
    assert err is not None
    assert "/recover/tool/tool_a" in err["message"]
    assert "re-forge" not in err["message"].lower()


def test_shadow_gate3_disabled_message_points_to_recovery_url():
    from unittest.mock import MagicMock
    from systemu.runtime.shadow_runtime import ShadowRuntime
    tool = MagicMock()
    tool.id = "tool_b"
    tool.name = "create_word_doc"
    tool.enabled = False
    tool.dry_run_status = "passed"
    tool.dry_run_evidence = None
    rt = ShadowRuntime.__new__(ShadowRuntime)
    err = rt._gate3_check(tool)
    assert err is not None
    assert err["reason"] == "GATE_3_DISABLED"
    assert "/recover/tool/tool_b" in err["message"]
