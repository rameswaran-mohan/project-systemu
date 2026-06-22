"""v0.6.3 — install-time blocker fixes: OS-specific Python upgrade guidance,
proxy detection, OpenRouter key validation, macOS permissions guide."""
from __future__ import annotations

import sys
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# sys.version_info is a special type that can't be re-instantiated, so build
# a namedtuple with the same attribute surface for monkeypatching.
_FakeVersionInfo = namedtuple(
    "_FakeVersionInfo", ["major", "minor", "micro", "releaselevel", "serial"]
)

# install.py lives at repo root, not in a package.  Match the pattern used
# by tests/test_installer.py + tests/test_v062_cross_platform_hardening.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # noqa: E402


class TestPythonVersionGuidance:
    """v0.6.3-a — pre-3.10 Python bails with OS-specific upgrade hint."""

    def _old_version_info(self):
        return _FakeVersionInfo(3, 9, 7, "final", 0)

    def test_old_python_on_linux_prints_apt_and_dnf_hints(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "version_info", self._old_version_info())
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SystemExit) as exc:
            install.check_python_version()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "3.10" in out
        assert "apt install" in out
        assert "dnf install" in out

    def test_old_python_on_macos_prints_brew_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "version_info", self._old_version_info())
        monkeypatch.setattr(sys, "platform", "darwin")
        with pytest.raises(SystemExit):
            install.check_python_version()
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "brew install" in out

    def test_old_python_on_windows_prints_python_org_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "version_info", self._old_version_info())
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(SystemExit):
            install.check_python_version()
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "python.org" in out.lower()

    def test_current_python_does_not_exit(self):
        # Whatever interpreter is running, it's >= 3.10 (project requirement)
        install.check_python_version()  # must not raise


class TestProxyDetection:
    """v0.6.3-b — detect_proxy_config reads HTTP_PROXY / HTTPS_PROXY."""

    _PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "no_proxy")

    def _clear_proxy_env(self, monkeypatch):
        for k in self._PROXY_VARS:
            monkeypatch.delenv(k, raising=False)

    def test_no_proxy_returns_empty_dict(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        assert install.detect_proxy_config() == {}

    def test_http_proxy_detected(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.example:3128")
        assert install.detect_proxy_config()["http"] == "http://proxy.corp.example:3128"

    def test_https_proxy_detected_separately(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setenv("HTTPS_PROXY", "https://proxy.corp.example:3128")
        assert install.detect_proxy_config().get("https") == "https://proxy.corp.example:3128"

    def test_lowercase_env_var_honored(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setenv("http_proxy", "http://lower.example:8080")
        assert install.detect_proxy_config().get("http") == "http://lower.example:8080"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows env vars are case-insensitive — HTTP_PROXY and http_proxy alias",
    )
    def test_uppercase_takes_precedence_over_lowercase(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setenv("HTTP_PROXY", "http://upper.example:8080")
        monkeypatch.setenv("http_proxy", "http://lower.example:8080")
        assert install.detect_proxy_config().get("http") == "http://upper.example:8080"

    def test_credentials_masked_in_url(self):
        masked = install._mask_proxy_url("http://user:secret123@proxy.corp:3128")
        assert "secret123" not in masked
        assert "user" in masked  # username preserved for debugging
        assert "proxy.corp:3128" in masked

    def test_mask_no_credentials_passthrough(self):
        url = "http://proxy.corp:3128"
        assert install._mask_proxy_url(url) == url


class TestOpenRouterValidation:
    """v0.6.3-c — validate_openrouter_key probes /api/v1/models endpoint."""

    def _mock_response(self, status: int, body: bytes = b"{}"):
        m = MagicMock()
        m.status = status
        m.read.return_value = body
        m.__enter__ = lambda self: self
        m.__exit__ = lambda self, *a: None
        return m

    def test_empty_key_returns_false_immediately(self):
        # No HTTP call when key is empty
        with patch("urllib.request.build_opener",
                   side_effect=AssertionError("must not be called")):
            ok, msg = install.validate_openrouter_key("", proxies={})
        assert ok is False
        assert "empty" in msg.lower()

    def test_valid_key_returns_true(self):
        opener = MagicMock()
        opener.open.return_value = self._mock_response(
            200, b'{"data": [{"id": "model"}]}',
        )
        with patch("urllib.request.build_opener", return_value=opener):
            ok, msg = install.validate_openrouter_key("sk-or-valid", proxies={})
        assert ok is True
        assert msg == ""

    def test_401_returns_false_with_invalid_key_message(self):
        import urllib.error
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/models", 401, "Unauthorized",
            {}, None,
        )
        with patch("urllib.request.build_opener", return_value=opener):
            ok, msg = install.validate_openrouter_key("sk-bad", proxies={})
        assert ok is False
        assert "401" in msg or "invalid" in msg.lower()

    def test_network_error_returns_false_warning(self):
        import urllib.error
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("connection refused")
        with patch("urllib.request.build_opener", return_value=opener):
            ok, msg = install.validate_openrouter_key("sk-x", proxies={})
        assert ok is False
        assert "connection" in msg.lower()

    def test_proxy_dict_passed_to_handler(self):
        opener = MagicMock()
        opener.open.return_value = self._mock_response(200)
        proxies = {"https": "https://proxy.corp:3128"}
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("urllib.request.ProxyHandler") as mock_handler:
            install.validate_openrouter_key("sk-or-valid", proxies=proxies)
            mock_handler.assert_called_once_with(proxies)


class TestMacOSPermissionsGuide:
    """v0.6.3-d — print_macos_permissions_guide echoes System Settings paths."""

    def test_non_macos_prints_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        install.print_macos_permissions_guide()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_non_macos_windows_prints_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        install.print_macos_permissions_guide()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_macos_prints_accessibility_path(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        install.print_macos_permissions_guide()
        out = capsys.readouterr().out
        assert "Accessibility" in out
        assert "Privacy" in out

    def test_macos_prints_screen_recording_path(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        install.print_macos_permissions_guide()
        out = capsys.readouterr().out
        assert "Screen Recording" in out

    def test_macos_prints_restart_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        install.print_macos_permissions_guide()
        out = capsys.readouterr().out
        # After granting permissions, the daemon must restart to pick up
        # the new TCC entitlements.
        assert "stop.sh" in out or "restart" in out.lower()


class TestPyatspiCheck:
    """v0.6.4-b — check_linux_pyatspi detects missing UI introspection bindings."""

    def test_non_linux_returns_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert install.check_linux_pyatspi() is True

    def test_non_linux_windows_returns_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert install.check_linux_pyatspi() is True

    def test_linux_with_pyatspi_returns_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        # importlib.util.find_spec returns non-None when the module exists
        fake_spec = MagicMock()
        with patch("importlib.util.find_spec", return_value=fake_spec):
            assert install.check_linux_pyatspi() is True

    def test_linux_without_pyatspi_returns_false_and_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("importlib.util.find_spec", return_value=None):
            result = install.check_linux_pyatspi()
        assert result is False
        out = capsys.readouterr().out + capsys.readouterr().err
        # apt hint visible
        # capsys was drained — combined above

    def test_linux_missing_prints_apt_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("importlib.util.find_spec", return_value=None):
            install.check_linux_pyatspi()
        captured = capsys.readouterr()
        text = captured.out + captured.err
        assert "pyatspi" in text.lower() or "at-spi" in text.lower()
        assert "apt install" in text or "dnf install" in text


class TestAppleSilicon:
    """v0.6.4-c — is_apple_silicon detects ARM64 Mac."""

    def test_intel_mac_returns_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("platform.machine", return_value="x86_64"):
            assert install.is_apple_silicon() is False

    def test_arm64_mac_returns_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("platform.machine", return_value="arm64"):
            assert install.is_apple_silicon() is True

    def test_linux_arm64_returns_false(self, monkeypatch):
        # Linux ARM (e.g. Raspberry Pi) is not "Apple Silicon"
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("platform.machine", return_value="aarch64"):
            assert install.is_apple_silicon() is False

    def test_windows_returns_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("platform.machine", return_value="AMD64"):
            assert install.is_apple_silicon() is False

    def test_banner_prints_on_apple_silicon(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("platform.machine", return_value="arm64"):
            install.print_apple_silicon_banner()
        out = capsys.readouterr().out
        assert "Apple Silicon" in out or "ARM64" in out or "arm64" in out

    def test_banner_silent_on_intel(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("platform.machine", return_value="x86_64"):
            install.print_apple_silicon_banner()
        captured = capsys.readouterr()
        assert captured.out == ""
