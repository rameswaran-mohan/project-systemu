import pytest


def test_recover_module_imports():
    from systemu.interface.pages import recover
    assert callable(recover.recover_page)


def test_diagnose_dispatches_to_right_method(monkeypatch):
    from systemu.interface.pages import recover

    captured = {}
    class FakeEngine:
        def __init__(self, vault): captured["vault"] = vault
        def diagnose_tool(self, tid): captured["call"] = ("tool", tid); return []
        def diagnose_shadow(self, sid): captured["call"] = ("shadow", sid); return []
        def diagnose_scroll(self, sid): captured["call"] = ("scroll", sid); return []
        def diagnose_activity(self, aid): captured["call"] = ("activity", aid); return []

    monkeypatch.setattr(recover, "RecoveryEngine", FakeEngine)
    monkeypatch.setattr(recover, "_get_vault", lambda: object())

    assert recover._diagnose("tool", "tool_a") == []
    assert captured["call"] == ("tool", "tool_a")

    recover._diagnose("shadow", "sh1")
    assert captured["call"] == ("shadow", "sh1")

    recover._diagnose("scroll", "scr1")
    assert captured["call"] == ("scroll", "scr1")

    recover._diagnose("activity", "act1")
    assert captured["call"] == ("activity", "act1")


def test_diagnose_unknown_scope_returns_empty(monkeypatch):
    from systemu.interface.pages import recover
    monkeypatch.setattr(recover, "_get_vault", lambda: object())
    assert recover._diagnose("widget", "w1") == []


# NOTE (Phase 5 Slice 2c): test_severity_color_mapping was removed with the
# old flat row layout (_severity_color / _render_action_row).  The panel rows
# are now the side-by-side remediation card; the severity→risk mapping is
# covered by tests/test_remediation_card.py::TestSeverityRisk.


def test_action_handler_routes_dep_pending_to_install(monkeypatch):
    from systemu.interface.pages import recover
    calls = []
    monkeypatch.setattr(recover, "_dispatch_install_dep",
                        lambda tid, pkg: calls.append((tid, pkg)))

    a = recover.RecoveryAction(
        scope_kind="tool", scope_id="tool_a", kind="DEP_PENDING",
        reason="Tool fetch_json missing package: requests",
        fix_url="x", fix_command="x", severity="blocker",
    )
    recover._handle_action(a)
    assert calls == [("tool_a", "requests")]


def test_action_handler_routes_gate_3_disabled_to_enable(monkeypatch):
    from systemu.interface.pages import recover
    enabled = []
    monkeypatch.setattr(recover, "_dispatch_enable_tool",
                        lambda tid: enabled.append(tid))

    a = recover.RecoveryAction(
        scope_kind="tool", scope_id="tool_a", kind="GATE_3_DISABLED",
        reason="...", fix_url="x", fix_command="x", severity="blocker",
    )
    recover._handle_action(a)
    assert enabled == ["tool_a"]


def test_action_handler_routes_memory_poisoned_to_reset(monkeypatch):
    from systemu.interface.pages import recover
    reset = []
    monkeypatch.setattr(recover, "_dispatch_reset_memory",
                        lambda sid, keep_successes: reset.append((sid, keep_successes)))

    a = recover.RecoveryAction(
        scope_kind="shadow", scope_id="sh1", kind="MEMORY_POISONED",
        reason="...", fix_url="x", fix_command="x", severity="warning",
    )
    recover._handle_action(a)
    assert reset == [("sh1", True)]


def test_action_handler_noop_on_gate_1_pending(monkeypatch):
    from systemu.interface.pages import recover
    # No dispatcher should fire for GATE_1_PENDING
    monkeypatch.setattr(recover, "_dispatch_install_dep",
                        lambda *a, **k: pytest.fail("should not be called"))
    monkeypatch.setattr(recover, "_dispatch_enable_tool",
                        lambda *a, **k: pytest.fail("should not be called"))
    monkeypatch.setattr(recover, "_dispatch_reset_memory",
                        lambda *a, **k: pytest.fail("should not be called"))
    a = recover.RecoveryAction(
        scope_kind="tool", scope_id="tool_a", kind="GATE_1_PENDING",
        reason="...", fix_url="x", fix_command="x", severity="blocker",
    )
    recover._handle_action(a)  # should not raise


def test_extract_missing_package():
    from systemu.interface.pages.recover import _extract_missing_package
    assert _extract_missing_package("Tool fetch_json missing package: requests") == "requests"
    assert _extract_missing_package("nothing here") == ""
