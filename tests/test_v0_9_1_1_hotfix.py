"""v0.9.1.1 hotfix tests — Tool.timeout_seconds + Config knob + output_dir precedence."""
import inspect
import os
from unittest.mock import patch
import pytest

from sharing_on.config import Config
from systemu.core.models import Tool, ToolType


class TestToolTimeoutSeconds:
    def test_default_is_none(self):
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        assert tool.timeout_seconds is None

    def test_set_to_integer(self):
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION, timeout_seconds=90)
        assert tool.timeout_seconds == 90

    def test_round_trip_json(self):
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION, timeout_seconds=120)
        rebuilt = Tool.model_validate_json(tool.model_dump_json())
        assert rebuilt.timeout_seconds == 120


class TestConfigToolDefaultTimeoutSeconds:
    _ENV_KEY = "SYSTEMU_TOOL_DEFAULT_TIMEOUT_SECONDS"

    def test_default_is_60(self, monkeypatch):
        monkeypatch.delenv(self._ENV_KEY, raising=False)
        cfg = Config()
        assert cfg.tool_default_timeout_seconds == 60

    def test_env_override(self):
        with patch.dict(os.environ, {self._ENV_KEY: "120"}, clear=False):
            cfg = Config.from_env()
        assert cfg.tool_default_timeout_seconds == 120


import asyncio
from systemu.runtime.tool_registry import ToolRegistry


class TestToolRegistryTimeoutResolution:
    def test_resolved_timeout_uses_tool_field_when_set(self):
        """When Tool.timeout_seconds is set, use it (highest precedence)."""
        from systemu.runtime.tool_registry import _resolve_timeout
        cfg = Config()
        cfg.tool_default_timeout_seconds = 60
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION, timeout_seconds=120)
        assert _resolve_timeout(tool, cfg, explicit=None) == 120

    def test_resolved_timeout_falls_back_to_config_default(self):
        """When Tool.timeout_seconds is None, use config.tool_default_timeout_seconds."""
        from systemu.runtime.tool_registry import _resolve_timeout
        cfg = Config()
        cfg.tool_default_timeout_seconds = 75
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        assert _resolve_timeout(tool, cfg, explicit=None) == 75

    def test_resolved_timeout_explicit_arg_wins_over_all(self):
        """Explicit timeout= arg to execute() is the ultimate override."""
        from systemu.runtime.tool_registry import _resolve_timeout
        cfg = Config()
        cfg.tool_default_timeout_seconds = 60
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION, timeout_seconds=120)
        assert _resolve_timeout(tool, cfg, explicit=200) == 200

    def test_resolved_timeout_handles_missing_config_field(self):
        """If config doesn't have the field (older callers), use 30s legacy default."""
        from systemu.runtime.tool_registry import _resolve_timeout
        class _MinimalConfig: pass
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        assert _resolve_timeout(tool, _MinimalConfig(), explicit=None) == 30


from systemu.runtime import shadow_runtime as sr


class TestOutputDirPrecedence:
    """Hotfix: when user_profile.default_output_dir is set, it must beat
    config.output_dir for verifier state-delta capture. v0.9.1 used
    config.output_dir unconditionally."""

    def test_resolve_prefers_user_profile_when_set(self):
        """user_profile.default_output_dir non-empty -> use it."""
        from systemu.runtime.shadow_runtime import _resolve_verifier_output_dir
        class _Cfg: output_dir = "/should/be/ignored"; vault_dir = "/v"
        class _Prof: default_output_dir = "/from/profile"
        assert _resolve_verifier_output_dir(_Cfg(), _Prof()) == "/from/profile"

    def test_resolve_falls_back_to_config_when_profile_missing(self):
        from systemu.runtime.shadow_runtime import _resolve_verifier_output_dir
        class _Cfg: output_dir = "/from/config"; vault_dir = "/v"
        assert _resolve_verifier_output_dir(_Cfg(), None) == "/from/config"

    def test_resolve_falls_back_to_config_when_profile_field_empty(self):
        from systemu.runtime.shadow_runtime import _resolve_verifier_output_dir
        class _Cfg: output_dir = "/from/config"; vault_dir = "/v"
        class _Prof: default_output_dir = ""
        assert _resolve_verifier_output_dir(_Cfg(), _Prof()) == "/from/config"

    def test_resolve_vault_outputs_when_both_missing(self):
        from systemu.runtime.shadow_runtime import _resolve_verifier_output_dir
        import os
        class _Cfg: vault_dir = "/v"   # no output_dir
        result = _resolve_verifier_output_dir(_Cfg(), None)
        # Use os.sep-agnostic check: result must contain vault_dir and "outputs"
        assert "v" in result and "outputs" in result


class TestWebExtractBrowserHeaders:
    @pytest.fixture(autouse=True)
    def _legacy_web(self, monkeypatch):  # v0.9.8: legacy raw-fetch header path
        monkeypatch.setenv("SYSTEMU_WEB_STACK_V2", "false")

    """v0.9.1.1: web_extract must look like a real browser so sites that
    block scrapers (Yelp, Reddit, Google, TripAdvisor) actually serve
    content. The old systemu/0.8 UA was being 403'd everywhere."""

    def test_default_ua_looks_like_browser(self):
        from systemu.vault.tools.implementations.web_extract import _DEFAULT_UA
        # Real browsers always start with "Mozilla/5.0"
        assert _DEFAULT_UA.startswith("Mozilla/5.0"), (
            f"UA must look like a browser, got: {_DEFAULT_UA[:60]}"
        )
        # The old UA had "systemu/" in it — must be gone
        assert "systemu/" not in _DEFAULT_UA
        assert "github.com" not in _DEFAULT_UA

    def test_fetch_sets_browser_headers_by_default(self, monkeypatch):
        from systemu.vault.tools.implementations import web_extract as wx
        captured = {}

        class _FakeResp:
            status_code = 200
            text = "<html><body>" + "x" * 200 + "</body></html>"

        def _fake_get(url, headers=None, params=None, timeout=None):
            captured["headers"] = dict(headers or {})
            return _FakeResp()

        import requests
        monkeypatch.setattr(requests, "get", _fake_get)
        wx._fetch("https://example.com", {}, {}, 30)
        # Must include browser-like headers
        h = captured["headers"]
        assert h.get("User-Agent", "").startswith("Mozilla/5.0")
        assert "Accept-Language" in h
        assert h.get("DNT") == "1"
        assert h.get("Connection") == "keep-alive"
        assert h.get("Upgrade-Insecure-Requests") == "1"

    def test_caller_headers_win_over_defaults(self, monkeypatch):
        """Caller-supplied User-Agent must override the default."""
        from systemu.vault.tools.implementations import web_extract as wx
        captured = {}

        class _FakeResp:
            status_code = 200
            text = "<html><body>" + "x" * 200 + "</body></html>"

        def _fake_get(url, headers=None, params=None, timeout=None):
            captured["headers"] = dict(headers or {})
            return _FakeResp()

        import requests
        monkeypatch.setattr(requests, "get", _fake_get)
        wx._fetch("https://example.com",
                  {"User-Agent": "custom-agent/1.0"}, {}, 30)
        assert captured["headers"]["User-Agent"] == "custom-agent/1.0"


class TestWebExtractAntiBotHint:
    # v0.9.8: legacy raw-fetch anti-bot mapping. The v0.9.8 web stack (Jina first)
    # is default-ON, so pin the legacy path here.
    @pytest.fixture(autouse=True)
    def _legacy_web(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_WEB_STACK_V2", "false")

    """v0.9.1.1: when a site returns 401/403/406/429/451 (anti-bot
    detection), web_extract returns error_type='anti_bot_blocked' with a
    concrete hint pointing the LLM at search engine URLs. Without this,
    the LLM kept calling the same blocked URL until the stuck guard fired."""

    def _patch_status(self, monkeypatch, status_code):
        from systemu.vault.tools.implementations import web_extract as wx

        class _FakeResp:
            def __init__(self, sc):
                self.status_code = sc
                self.text = ""

        def _fake_get(url, headers=None, params=None, timeout=None):
            return _FakeResp(status_code)

        import requests
        monkeypatch.setattr(requests, "get", _fake_get)
        return wx

    def test_403_returns_anti_bot_blocked_with_hint(self, monkeypatch):
        wx = self._patch_status(monkeypatch, 403)
        out = wx.run(url="https://example.com")
        assert out["success"] is False
        assert out["error_type"] == "anti_bot_blocked"
        assert "duckduckgo" in out["error"].lower() or "search" in out["error"].lower()
        assert out["status_code"] == 403

    def test_429_returns_anti_bot_blocked(self, monkeypatch):
        wx = self._patch_status(monkeypatch, 429)
        out = wx.run(url="https://example.com")
        assert out["error_type"] == "anti_bot_blocked"
        assert out["status_code"] == 429

    def test_401_returns_anti_bot_blocked(self, monkeypatch):
        wx = self._patch_status(monkeypatch, 401)
        out = wx.run(url="https://example.com")
        assert out["error_type"] == "anti_bot_blocked"

    def test_500_keeps_generic_http_error(self, monkeypatch):
        """500-class errors are server issues, not bot detection — preserve
        legacy http_error behavior."""
        wx = self._patch_status(monkeypatch, 500)
        out = wx.run(url="https://example.com")
        assert out["error_type"] == "http_error"
        assert "duckduckgo" not in out["error"].lower()

    def test_404_keeps_generic_http_error(self, monkeypatch):
        """404 is a real missing page — don't suggest the user is being blocked."""
        wx = self._patch_status(monkeypatch, 404)
        out = wx.run(url="https://example.com")
        assert out["error_type"] == "http_error"

    def test_406_returns_anti_bot_blocked(self, monkeypatch):
        wx = self._patch_status(monkeypatch, 406)
        out = wx.run(url="https://example.com")
        assert out["error_type"] == "anti_bot_blocked"

    def test_451_returns_anti_bot_blocked(self, monkeypatch):
        wx = self._patch_status(monkeypatch, 451)
        out = wx.run(url="https://example.com")
        assert out["error_type"] == "anti_bot_blocked"


# ─── Guard tests: source-inspection pins for the two critical silent-failure ──
# fixes in v0.9.1.1 review pass 2.  These catch the specific wiring mistakes
# that caused _resolve_verifier_output_dir and _resolve_timeout to silently
# no-op in production despite passing all unit tests.


class TestShadowRuntimeUserProfileInit:
    """Critical Bug 1 guard: ShadowRuntime.__init__ must assign self.user_profile
    so the existing getattr(self, 'user_profile', None) calls at the two
    _resolve_verifier_output_dir sites actually resolve to the vault profile
    instead of always returning None.

    Uses inspect.getsource (same pattern as v0.9.1's _after_successful_call
    guard) so this test fails immediately if the assignment is removed.
    """

    def test_user_profile_assigned_in_init(self):
        from systemu.runtime import shadow_runtime as _sr
        src = inspect.getsource(_sr.ShadowRuntime.__init__)
        assert "self.user_profile =" in src, (
            "ShadowRuntime.__init__ must assign self.user_profile so that "
            "_resolve_verifier_output_dir receives the real vault profile "
            "instead of None. Add the v0.9.1.1 fix block after 'self.vault = vault'."
        )


class TestToolSandboxTimeoutWiring:
    """Critical Bug 2 integration guard: a Tool with timeout_seconds=120
    invoked through ToolSandbox.execute_tool must see 120s in the registry's
    execute path, NOT the hardcoded 30s sandbox default.

    This catches the regression where effective_timeout = timeout or self.timeout
    collapsed caller timeout=None to 30, then passed explicit=30.0 to
    _resolve_timeout — making the per-tool and config-default precedence
    unreachable from production code.
    """

    def test_tool_timeout_seconds_reaches_registry(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from systemu.runtime.tool_sandbox import ToolSandbox
        from systemu.core.models import Tool, ToolType
        from pathlib import Path
        import tempfile, os

        # Build a minimal implementation file so impl_path.exists() is True
        # and the fast-path branch fires (not subprocess fallback).
        with tempfile.TemporaryDirectory() as tmpdir:
            impl_file = Path(tmpdir) / "my_tool.py"
            impl_file.write_text("def run(**kw): return {'success': True}\n")

            sandbox = ToolSandbox(vault_root=tmpdir, default_timeout=30)

            # Track what timeout the registry receives.
            received_timeout = {}

            async def _fake_execute(name, params, timeout=None):
                received_timeout["value"] = timeout
                return {"success": True}

            mock_registry = MagicMock()
            mock_registry.execute = _fake_execute

            # Provide a vault that returns a Tool with timeout_seconds=120.
            tool_120 = Tool(
                id="my_tool", name="my_tool", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, timeout_seconds=120,
            )
            mock_vault = MagicMock()
            mock_vault.find_tool_by_name.return_value = tool_120
            mock_registry._vault = mock_vault

            sandbox.attach_registry(mock_registry)

            # asyncio.run (not get_event_loop) — order-independent: a prior
            # test can leave the thread's current loop closed, which made this
            # fail in the full suite while passing alone.
            asyncio.run(sandbox.execute_tool(str(impl_file), {}))

        # The registry must receive timeout=None so _resolve_timeout can pick
        # up tool.timeout_seconds=120.  If it receives 30.0 the fix is broken.
        assert received_timeout["value"] is None, (
            f"ToolSandbox.execute_tool must pass timeout=None to registry.execute() "
            f"when no explicit timeout override is given, so _resolve_timeout can "
            f"prefer Tool.timeout_seconds over the sandbox default. "
            f"Got timeout={received_timeout['value']!r} (expected None)."
        )
