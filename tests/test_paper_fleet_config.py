"""Plan 0 Build 3 (Task 3.1) — paper-fleet config: delegate_use_parallel.

The parallel-children flag is OFF by default and overridable via
SYSTEMU_DELEGATE_USE_PARALLEL. The pre-existing delegate caps
(max_concurrent_children, max_depth) must remain intact.
"""
import os
from unittest.mock import patch

from sharing_on.config import Config


class TestDelegateUseParallel:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_DELEGATE_USE_PARALLEL", raising=False)
        cfg = Config()
        assert cfg.delegate_use_parallel is False

    def test_env_override_true(self):
        with patch.dict(
            os.environ, {"SYSTEMU_DELEGATE_USE_PARALLEL": "true"}, clear=False
        ):
            cfg = Config()
        assert cfg.delegate_use_parallel is True

    def test_env_override_true_from_env(self):
        with patch.dict(
            os.environ, {"SYSTEMU_DELEGATE_USE_PARALLEL": "true"}, clear=False
        ):
            cfg = Config.from_env()
        assert cfg.delegate_use_parallel is True

    def test_preexisting_caps_intact(self, monkeypatch):
        for k in (
            "SYSTEMU_DELEGATE_MAX_DEPTH",
            "SYSTEMU_DELEGATE_MAX_CONCURRENT_CHILDREN",
        ):
            monkeypatch.delenv(k, raising=False)
        cfg = Config()
        # These already existed before Build 3 — Task 3.1 must not duplicate
        # or disturb them.
        assert cfg.delegate_max_depth == 3
        assert cfg.delegate_max_concurrent_children == 2
