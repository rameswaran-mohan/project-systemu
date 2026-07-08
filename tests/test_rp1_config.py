"""R-P1: messaging config fields for Telegram decision-resolution.

These knobs drive R-P1 (resolve parked decisions from Telegram) — see the
messaging/privacy fields in sharing_on/config.py. They flow through from_env()
via default_factory (from_env does not override default_factory fields).
"""


def test_decision_resolution_default_on(monkeypatch):
    monkeypatch.delenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", raising=False)
    from sharing_on.config import Config
    assert Config.from_env().messaging_decision_resolution is True


def test_decision_resolution_off(monkeypatch):
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "off")
    from sharing_on.config import Config
    assert Config.from_env().messaging_decision_resolution is False


def test_push_detail_default_summary(monkeypatch):
    monkeypatch.delenv("SHARING_ON_MESSAGING_PUSH_DETAIL", raising=False)
    from sharing_on.config import Config
    assert Config.from_env().messaging_push_detail == "summary"


def test_push_detail_env(monkeypatch):
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "full")
    from sharing_on.config import Config
    assert Config.from_env().messaging_push_detail == "full"


def test_dashboard_base_url_default_empty(monkeypatch):
    monkeypatch.delenv("SHARING_ON_DASHBOARD_BASE_URL", raising=False)
    from sharing_on.config import Config
    assert Config.from_env().dashboard_base_url == ""
