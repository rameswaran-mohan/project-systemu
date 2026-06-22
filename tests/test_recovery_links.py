import pytest
from systemu.recovery.links import recover_url, dashboard_base_url


def test_dashboard_base_url_uses_env(monkeypatch):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_URL", "http://dash.local:9000")
    assert dashboard_base_url() == "http://dash.local:9000"


def test_dashboard_base_url_default(monkeypatch):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_URL", raising=False)
    assert dashboard_base_url() == "http://localhost:8765"


def test_recover_url_shapes(monkeypatch):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_URL", raising=False)
    assert recover_url("tool", "tool_a3") == "http://localhost:8765/recover/tool/tool_a3"
    assert recover_url("shadow", "shadow_x") == "http://localhost:8765/recover/shadow/shadow_x"
    assert recover_url("scroll", "s_1") == "http://localhost:8765/recover/scroll/s_1"
    assert recover_url("activity", "a_1") == "http://localhost:8765/recover/activity/a_1"


def test_recover_url_rejects_unknown_scope():
    with pytest.raises(ValueError):
        recover_url("widget", "w_1")
