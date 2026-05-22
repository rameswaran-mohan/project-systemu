# tests/test_v062_cross_platform_hardening.py
"""— cross-platform hardening: install.py + config.py warnings."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# install.py lives at repo root, not in a package — add to sys.path for import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # noqa: E402


class TestLinuxCaptureDeps:
    def test_returns_empty_on_non_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert install.check_linux_capture_deps() == []

    def test_lists_missing_tools_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        # shutil.which() returns None when the tool is absent
        with patch("install.shutil.which", side_effect=lambda c: None):
            missing = install.check_linux_capture_deps()
        assert sorted(missing) == ["xclip", "xdotool"]

    def test_empty_when_all_present_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("install.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"):
            assert install.check_linux_capture_deps() == []

    def test_partial_missing(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        def fake_which(cmd):
            return "/usr/bin/xclip" if cmd == "xclip" else None
        with patch("install.shutil.which", side_effect=fake_which):
            assert install.check_linux_capture_deps() == ["xdotool"]


class TestPlaywrightInstallArgs:
    """--with-deps on Linux pulls Chromium's OS-level libraries
    (libnss3, libatk1.0-0, etc.) via sudo apt.  Without it, the browser
    binary downloads OK but fails to launch when sharing_on tries to use it."""

    def test_linux_uses_with_deps(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        args = install.playwright_install_args()
        assert args == ["install", "--with-deps", "chromium"]

    def test_macos_omits_with_deps(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        args = install.playwright_install_args()
        assert args == ["install", "chromium"]

    def test_windows_omits_with_deps(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        args = install.playwright_install_args()
        assert args == ["install", "chromium"]


class TestWaylandDetection:
    """pynput requires X11.  On Wayland sessions (Ubuntu 22+ default,
    Fedora Workstation default), capture features produce empty event streams.
    Daemon itself is unaffected, but the operator should know upfront."""

    def test_returns_false_on_non_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert install.detect_wayland_session() is False

    def test_returns_true_on_linux_wayland(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert install.detect_wayland_session() is True

    def test_returns_false_on_linux_x11(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        assert install.detect_wayland_session() is False

    def test_returns_false_when_xdg_unset(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        assert install.detect_wayland_session() is False

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "WAYLAND")
        assert install.detect_wayland_session() is True


class TestStaleEnvVarDetection:
    """SYSTEMU_AUTO_APPROVE_SCROLLS was renamed to
    SYSTEMU_NON_INTERACTIVE in v0.6.1 (hard cut, no alias).  Operators
    who git pull but don't re-run install.py keep the old key in .env
    — it gets silently ignored.  detect_stale_env_vars() returns a
    dict of {old_name: new_name} for any stale keys present."""

    def _write_env(self, tmp_path, content):
        env = tmp_path / ".env"
        env.write_text(content, encoding="utf-8")
        return env

    def test_returns_empty_when_env_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install, "ENV_PATH", tmp_path / ".env")
        assert install.detect_stale_env_vars() == {}

    def test_returns_empty_when_no_stale_keys(self, tmp_path, monkeypatch):
        self._write_env(tmp_path, "SYSTEMU_NON_INTERACTIVE=true\nSYSTEMU_MODE=local\n")
        monkeypatch.setattr(install, "ENV_PATH", tmp_path / ".env")
        assert install.detect_stale_env_vars() == {}

    def test_detects_old_auto_approve_scrolls(self, tmp_path, monkeypatch):
        self._write_env(tmp_path, "SYSTEMU_AUTO_APPROVE_SCROLLS=true\nSYSTEMU_MODE=local\n")
        monkeypatch.setattr(install, "ENV_PATH", tmp_path / ".env")
        stale = install.detect_stale_env_vars()
        assert stale == {"SYSTEMU_AUTO_APPROVE_SCROLLS": "SYSTEMU_NON_INTERACTIVE"}

    def test_ignores_old_when_new_also_present(self, tmp_path, monkeypatch):
        """Both old and new present means the operator is mid-migration —
        the new key already takes effect; old is harmless dead weight.
        Don't nag them about it."""
        self._write_env(
            tmp_path,
            "SYSTEMU_AUTO_APPROVE_SCROLLS=true\nSYSTEMU_NON_INTERACTIVE=true\n",
        )
        monkeypatch.setattr(install, "ENV_PATH", tmp_path / ".env")
        assert install.detect_stale_env_vars() == {}


class TestConfigStaleEnvWarning:
    """sharing_on.config.Config.from_env() prints a stderr warning
    when SYSTEMU_AUTO_APPROVE_SCROLLS is set.  Runtime safety net for
    operators who didn't re-run install.py."""

    def test_warns_when_old_var_set(self, monkeypatch, capsys):
        monkeypatch.setenv("SYSTEMU_AUTO_APPROVE_SCROLLS", "true")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        from sharing_on.config import Config
        Config.from_env()
        err = capsys.readouterr().err
        assert "SYSTEMU_AUTO_APPROVE_SCROLLS" in err
        assert "renamed" in err.lower() or "ignored" in err.lower()

    def test_no_warn_when_old_var_unset(self, monkeypatch, capsys):
        monkeypatch.delenv("SYSTEMU_AUTO_APPROVE_SCROLLS", raising=False)
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        from sharing_on.config import Config
        Config.from_env()
        err = capsys.readouterr().err
        assert "SYSTEMU_AUTO_APPROVE_SCROLLS" not in err
