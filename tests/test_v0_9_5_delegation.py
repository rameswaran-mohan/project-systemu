"""v0.9.5 L6 Goal-Level Orchestration — delegate + dynamic_schema tests."""
import os
from unittest.mock import patch
import pytest

from sharing_on.config import Config


class TestConfigDelegateFields:
    _KEYS = (
        "SYSTEMU_DELEGATE_MAX_DEPTH",
        "SYSTEMU_DELEGATE_MAX_CONCURRENT_CHILDREN",
        "SYSTEMU_DELEGATE_MAX_TURNS_PER_CHILD",
    )

    def test_defaults(self, monkeypatch):
        for k in self._KEYS:
            monkeypatch.delenv(k, raising=False)
        cfg = Config()
        assert cfg.delegate_max_depth == 3
        assert cfg.delegate_max_concurrent_children == 2
        assert cfg.delegate_max_turns_per_child == 20

    def test_env_overrides(self):
        env = {
            "SYSTEMU_DELEGATE_MAX_DEPTH": "5",
            "SYSTEMU_DELEGATE_MAX_CONCURRENT_CHILDREN": "4",
            "SYSTEMU_DELEGATE_MAX_TURNS_PER_CHILD": "30",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.from_env()
        assert cfg.delegate_max_depth == 5
        assert cfg.delegate_max_concurrent_children == 4
        assert cfg.delegate_max_turns_per_child == 30


class TestToolEntryDynamicSchema:
    """ToolEntry gains a `dynamic_schema_overrides` callable hook.

    Design: tools whose schema description depends on runtime config
    (like delegate_task with current max_depth limits) supply a zero-arg
    callable that returns dict overrides applied each time the LLM tool list
    is rendered."""

    def test_default_is_none(self):
        from systemu.runtime.tool_registry_v2 import ToolEntry
        e = ToolEntry(
            name="t", toolset="x",
            schema={"type": "object"},
            handler=lambda **k: None,
        )
        assert e.dynamic_schema_overrides is None

    def test_set_to_callable(self):
        from systemu.runtime.tool_registry_v2 import ToolEntry
        def overrides():
            return {"description": "fresh description"}
        e = ToolEntry(
            name="t", toolset="x",
            schema={"type": "object"},
            handler=lambda **k: None,
            dynamic_schema_overrides=overrides,
        )
        assert callable(e.dynamic_schema_overrides)
        assert e.dynamic_schema_overrides() == {"description": "fresh description"}


class TestRegisterAcceptsDynamicSchema:
    def test_register_accepts_dynamic_schema_overrides_kwarg(self):
        from systemu.runtime.tool_registry_v2 import ToolRegistry
        r = ToolRegistry()
        def overrides():
            return {"description": "live"}
        r.register(
            name="t", toolset="x",
            schema={"type": "object"},
            handler=lambda **k: None,
            dynamic_schema_overrides=overrides,
        )
        entry = r.get("t")
        assert entry.dynamic_schema_overrides is overrides
