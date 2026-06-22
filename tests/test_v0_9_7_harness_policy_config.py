"""v0.9.7 Phase 1.2 — HarnessPolicy.from_config must accept the runtime Config
OBJECT (not just a dict). Regression guard for the dict-only bug."""
from systemu.runtime.harness_policy import HarnessPolicy


def test_from_config_accepts_config_object():
    from sharing_on.config import Config
    pol = HarnessPolicy.from_config(Config.from_env())
    # defaults hold (no harness_* fields on Config, no env overrides set)
    assert pol.auto_grant_tool is False
    assert pol.max_requests_per_run >= 1


def test_from_config_accepts_none():
    pol = HarnessPolicy.from_config(None)
    assert pol.auto_grant_tool is False


def test_from_config_accepts_dict():
    pol = HarnessPolicy.from_config({"auto_grant_tool": True, "max_requests_per_run": 3})
    assert pol.auto_grant_tool is True
    assert pol.max_requests_per_run == 3


def test_from_config_object_with_harness_attr(monkeypatch):
    class _Cfg:
        harness_auto_grant_subagent = True
    pol = HarnessPolicy.from_config(_Cfg())
    assert pol.auto_grant_subagent is True
