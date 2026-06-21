"""v0.9.34.1 Feature D — capture-scope (narrow vs broad) at record time."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sharing_on.events.models import CaptureEvent, EventAction, EventCategory


def _win(app="chrome.exe", title="GitHub - Chrome", proc=None):
    return CaptureEvent(
        category=EventCategory.WINDOW,
        action=EventAction.WINDOW_FOCUS,
        timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        application=app,
        window_title=title,
        process_name=proc if proc is not None else app,
    )


class TestCaptureScopeFilter:
    def test_broad_scope_keeps_everything(self):
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="broad")
        assert s.keep(_win(app="notepad.exe")) is True
        assert s.keep(_win(app="chrome.exe")) is True

    def test_narrow_keeps_only_target_app_case_insensitive(self):
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="Chrome.exe")
        assert s.keep(_win(app="chrome.exe")) is True
        assert s.keep(_win(app="notepad.exe")) is False

    def test_narrow_matches_on_process_name_when_application_absent(self):
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="code.exe")
        ev = _win(app=None, title="main.py", proc="code.exe")
        assert s.keep(ev) is True

    def test_narrow_substring_match_in_window_title(self):
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="chrome.exe",
                         target_title="GitHub")
        assert s.keep(_win(app="chrome.exe", title="GitHub - Chrome")) is True
        assert s.keep(_win(app="chrome.exe", title="Gmail - Chrome")) is False

    def test_narrow_with_no_target_app_keeps_everything(self):
        # Misconfigured narrow (no target) must not silently drop all events.
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="")
        assert s.keep(_win(app="anything.exe")) is True

    def test_session_and_marker_events_always_kept_in_narrow(self):
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="chrome.exe")
        sess = CaptureEvent(
            category=EventCategory.SESSION, action=EventAction.SESSION_START,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        )
        marker = CaptureEvent(
            category=EventCategory.MARKER, action=EventAction.STEP_MARKER,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert s.keep(sess) is True
        assert s.keep(marker) is True

    def test_metadata_less_event_kept_in_narrow(self):
        # v0.9.34.1 design divergence from the plan's tentative "drop": events
        # with NO app metadata (raw input-hook clicks/keystrokes, clipboard,
        # full-screen screenshots) are KEPT in narrow Phase 1 — they can't be
        # attributed to an app, and dropping them would gut the interaction
        # stream that step generation depends on. Phase 2 correlates input later.
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="chrome.exe")
        click = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        )  # no application / process_name
        assert s.keep(click) is True

    def test_introspector_interaction_event_kept_in_narrow(self):
        # Review HIGH: Windows UI-introspector enriched clicks stamp
        # application=window TITLE and no process_name; they must SURVIVE narrow
        # (the richest signal), not be dropped because the title isn't the
        # target token. (Phase 2 sets process_name → then they narrow properly.)
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="chrome.exe")
        ev = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
            application="GitHub - Google Chrome",  # window title, not a process
            process_name=None,
        )
        assert s.keep(ev) is True

    def test_exe_suffix_target_matches_friendly_app_name(self):
        # Review LOW (macOS): a "chrome.exe" target must also match a friendly
        # app name like "Google Chrome" (no .exe), so narrow works cross-platform.
        from sharing_on.collectors.scope import CaptureScope
        s = CaptureScope(scope="narrow", target_app="chrome.exe")
        assert s.keep(_win(app="Google Chrome", proc="Google Chrome")) is True
        assert s.keep(_win(app="Notepad", proc="Notepad")) is False

    def test_is_narrow_flag(self):
        from sharing_on.collectors.scope import CaptureScope
        assert CaptureScope(scope="narrow", target_app="x").is_narrow is True
        assert CaptureScope(scope="broad").is_narrow is False
        # narrow with no target degrades to broad behaviour
        assert CaptureScope(scope="narrow", target_app="").is_narrow is False


class TestBaseCollectorScopeHook:
    def _collector(self):
        from sharing_on.collectors.base import BaseCollector

        class _C(BaseCollector):
            name = "test"
            def _collect_loop(self):  # pragma: no cover - not run here
                ...

        c = _C.__new__(_C)
        stored = []
        class _Store:
            def put(self, ev): stored.append(ev)
        c._store = _Store()
        c._scope = None
        return c, stored

    def test_emit_with_no_scope_stores_everything(self):
        c, stored = self._collector()
        c.emit(_win(app="notepad.exe"))
        assert len(stored) == 1

    def test_emit_drops_event_failing_scope(self):
        from sharing_on.collectors.scope import CaptureScope
        c, stored = self._collector()
        c.set_scope(CaptureScope(scope="narrow", target_app="chrome.exe"))
        c.emit(_win(app="notepad.exe"))      # off-target → dropped
        c.emit(_win(app="chrome.exe"))       # on-target → kept
        assert len(stored) == 1
        assert stored[0].application == "chrome.exe"

    def test_set_scope_is_chainable_noop_for_broad(self):
        from sharing_on.collectors.scope import CaptureScope
        c, stored = self._collector()
        c.set_scope(CaptureScope(scope="broad"))
        c.emit(_win(app="notepad.exe"))
        assert len(stored) == 1


class TestCaptureScopeConfig:
    def test_defaults_are_broad_with_empty_targets(self, monkeypatch):
        for v in ("SHARING_ON_CAPTURE_SCOPE", "SHARING_ON_CAPTURE_APP",
                  "SHARING_ON_CAPTURE_TITLE"):
            monkeypatch.delenv(v, raising=False)
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_scope == "broad"
        assert c.capture_target_app == ""
        assert c.capture_target_title == ""

    def test_from_env_parses_scope_and_targets(self, monkeypatch):
        monkeypatch.setenv("SHARING_ON_CAPTURE_SCOPE", "Narrow")
        monkeypatch.setenv("SHARING_ON_CAPTURE_APP", "chrome.exe")
        monkeypatch.setenv("SHARING_ON_CAPTURE_TITLE", "GitHub")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_scope == "narrow"       # lower-cased
        assert c.capture_target_app == "chrome.exe"
        assert c.capture_target_title == "GitHub"

    def test_invalid_scope_falls_back_to_broad(self, monkeypatch):
        monkeypatch.setenv("SHARING_ON_CAPTURE_SCOPE", "everything")
        for v in ("SHARING_ON_CAPTURE_APP", "SHARING_ON_CAPTURE_TITLE"):
            monkeypatch.delenv(v, raising=False)
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_scope == "broad"


class TestSessionScopeWiring:
    def _session(self, tmp_path, scope="broad", app="", title=""):
        from sharing_on.config import Config
        from sharing_on.session import CaptureSession
        cfg = Config.from_env()
        cfg.capture_scope = scope
        cfg.capture_target_app = app
        cfg.capture_target_title = title
        return CaptureSession(name="t", config=cfg, output_dir=tmp_path)

    def test_build_scope_returns_narrow_when_configured(self, tmp_path):
        from sharing_on.collectors.scope import CaptureScope
        s = self._session(tmp_path, scope="narrow", app="chrome.exe", title="X")
        scope = s._build_scope()
        assert isinstance(scope, CaptureScope)
        assert scope.is_narrow is True
        assert scope.target_app == "chrome.exe"
        assert scope.target_title == "X"

    def test_build_scope_broad_by_default(self, tmp_path):
        s = self._session(tmp_path)
        assert s._build_scope().is_narrow is False

    def test_collectors_receive_scope(self, tmp_path):
        s = self._session(tmp_path, scope="narrow", app="chrome.exe")
        s._build_collectors()
        # every collector got the narrow scope installed
        assert s._collectors, "expected at least one collector"
        for c in s._collectors:
            assert c._scope is not None
            assert c._scope.target_app == "chrome.exe"

    def test_session_json_records_scope(self, tmp_path):
        import json
        s = self._session(tmp_path, scope="narrow", app="chrome.exe", title="GitHub")
        s._save_metadata()
        meta = json.loads((tmp_path / "session.json").read_text())
        assert meta["capture_scope"] == "narrow"
        assert meta["capture_target_app"] == "chrome.exe"
        assert meta["capture_target_title"] == "GitHub"

    def test_session_json_broad_default(self, tmp_path):
        import json
        s = self._session(tmp_path)
        s._save_metadata()
        meta = json.loads((tmp_path / "session.json").read_text())
        assert meta["capture_scope"] == "broad"
        assert meta["capture_target_app"] == ""


class TestWebExtensionScope:
    def _make_collector(self, scope=None):
        from sharing_on.collectors.web_extension import WebExtensionCollector
        col = WebExtensionCollector.__new__(WebExtensionCollector)
        emitted = []
        col.emit = emitted.append  # type: ignore[attr-defined]
        col._scope = scope
        return col, emitted

    def test_broad_keeps_all_origins(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        col, emitted = self._make_collector(scope=None)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/x"})
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://gmail.com/y"})
        assert len(emitted) == 2

    def test_narrow_keeps_only_allowed_origin(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        from sharing_on.collectors.scope import CaptureScope
        scope = CaptureScope(scope="narrow", target_app="https://github.com")
        col, emitted = self._make_collector(scope=scope)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/foo/bar"})
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://gmail.com/inbox"})
        assert len(emitted) == 1
        assert emitted[0].data["url"] == "https://github.com/foo/bar"

    def test_narrow_browser_app_target_keeps_all_web(self, monkeypatch):
        """If the narrow target is a browser process (chrome.exe), not a URL,
        the origin allow-list does not engage — all web events survive (the
        browser IS the target app)."""
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        from sharing_on.collectors.scope import CaptureScope
        scope = CaptureScope(scope="narrow", target_app="chrome.exe")
        col, emitted = self._make_collector(scope=scope)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/x"})
        assert len(emitted) == 1

    def test_dashboard_origin_still_dropped_under_narrow(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://127.0.0.1:8765")
        from sharing_on.collectors.scope import CaptureScope
        scope = CaptureScope(scope="narrow", target_app="http://127.0.0.1:8765")
        col, emitted = self._make_collector(scope=scope)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "http://127.0.0.1:8765/inbox"})
        assert emitted == []

    def test_real_emit_narrow_origin_via_should_capture(self, monkeypatch):
        """Exercise the REAL emit path (not stubbed): handle_extension_event →
        emit → should_capture override. Proves the base process-name match does
        NOT wrongly drop a matching-origin web event (whose application is the
        generic "Chrome/Edge Web Browser"). Without the override this stores 0."""
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        from sharing_on.collectors.web_extension import WebExtensionCollector
        from sharing_on.collectors.scope import CaptureScope
        stored = []
        class _Store:
            def put(self, ev): stored.append(ev)
        col = WebExtensionCollector.__new__(WebExtensionCollector)
        col._store = _Store()
        col._scope = CaptureScope(scope="narrow", target_app="https://github.com")
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/x"})
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://gmail.com/y"})
        assert len(stored) == 1
        assert stored[0].data["url"] == "https://github.com/x"


class TestCliScopeOptions:
    def _run(self, monkeypatch, args):
        """Invoke `record` with collectors/analysis stubbed so it returns fast,
        and capture the Config the CaptureSession was built with."""
        from click.testing import CliRunner
        from sharing_on import cli as cli_mod

        captured = {}

        class _FakeSession:
            def __init__(self, name, config, output_dir=None):
                captured["config"] = config
                self.output_dir = output_dir or "."
                self.event_count = 0
            def start(self): ...
            def stop(self): ...

        monkeypatch.setattr(cli_mod, "CaptureSession", _FakeSession)
        monkeypatch.setattr(cli_mod, "check_dependencies", lambda: [])
        monkeypatch.setattr(cli_mod, "_run_live_display", lambda *a, **k: None)
        monkeypatch.setattr(cli_mod, "_print_startup_banner", lambda *a, **k: None)

        runner = CliRunner()
        result = runner.invoke(cli_mod.cli,
                               ["record", "--no-analyze", *args])
        assert result.exit_code == 0, result.output
        return captured["config"]

    def test_default_is_broad(self, monkeypatch):
        cfg = self._run(monkeypatch, ["--name", "t"])
        assert cfg.capture_scope == "broad"
        assert cfg.capture_target_app == ""

    def test_scope_narrow_with_app(self, monkeypatch):
        cfg = self._run(monkeypatch,
                        ["--name", "t", "--scope", "narrow", "--app", "chrome.exe"])
        assert cfg.capture_scope == "narrow"
        assert cfg.capture_target_app == "chrome.exe"

    def test_app_alone_implies_narrow(self, monkeypatch):
        cfg = self._run(monkeypatch, ["--name", "t", "--app", "code.exe"])
        assert cfg.capture_scope == "narrow"
        assert cfg.capture_target_app == "code.exe"


class TestDashboardScopeArgs:
    def test_broad_args_have_no_scope_flags(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("Deploy app", scope="broad", app="")
        assert args == ["--name", "Deploy app", "--no-analyze"]

    def test_narrow_args_include_scope_and_app(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("Deploy app", scope="narrow",
                                     app="chrome.exe")
        assert args == ["--name", "Deploy app", "--no-analyze",
                        "--scope", "narrow", "--app", "chrome.exe"]

    def test_narrow_without_app_falls_back_to_broad(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("t", scope="narrow", app="")
        assert "--scope" not in args
        assert args == ["--name", "t", "--no-analyze"]


class TestNarrowEndToEnd:
    def _store(self):
        stored = []
        class _Store:
            def put(self, ev): stored.append(ev)
        return _Store(), stored

    def test_window_collector_drops_offtarget_keeps_target(self):
        from sharing_on.collectors.window import WindowCollector
        from sharing_on.collectors.scope import CaptureScope
        store, stored = self._store()
        col = WindowCollector.__new__(WindowCollector)
        col._store = store
        col._scope = CaptureScope(scope="narrow", target_app="chrome.exe")
        col.emit(_win(app="notepad.exe", title="Untitled"))   # dropped
        col.emit(_win(app="chrome.exe", title="GitHub"))      # kept
        assert [e.application for e in stored] == ["chrome.exe"]

    def test_process_collector_respects_scope(self):
        from sharing_on.collectors.process import ProcessCollector
        from sharing_on.collectors.scope import CaptureScope
        store, stored = self._store()
        col = ProcessCollector.__new__(ProcessCollector)
        col._store = store
        col._scope = CaptureScope(scope="narrow", target_app="chrome.exe")
        on = CaptureEvent(
            category=EventCategory.PROCESS, action=EventAction.PROCESS_STARTED,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
            process_name="chrome.exe",
        )
        off = CaptureEvent(
            category=EventCategory.PROCESS, action=EventAction.PROCESS_STARTED,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
            process_name="notepad.exe",
        )
        col.emit(off)
        col.emit(on)
        assert [e.process_name for e in stored] == ["chrome.exe"]

    def test_broad_collector_keeps_both(self):
        from sharing_on.collectors.window import WindowCollector
        store, stored = self._store()
        col = WindowCollector.__new__(WindowCollector)
        col._store = store
        col._scope = None   # broad
        col.emit(_win(app="notepad.exe"))
        col.emit(_win(app="chrome.exe"))
        assert len(stored) == 2
