"""v0.9.35 Phase 0 — capture-sources filter (single vs all) at record time.

Renamed from v0.9.34.1 Feature D's CaptureScope (broad/narrow) so that
broad/narrow are free for the v0.9.35 generalization toggle. Behaviour is
byte-identical to CaptureScope; only the names and value tokens change
(broad->all, narrow->single).
"""
from __future__ import annotations

from datetime import datetime, timezone

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


class TestCaptureSourcesFilter:
    def test_all_mode_keeps_everything(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="all")
        assert s.keep(_win(app="notepad.exe")) is True
        assert s.keep(_win(app="chrome.exe")) is True

    def test_single_keeps_only_source_app_case_insensitive(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="Chrome.exe")
        assert s.keep(_win(app="chrome.exe")) is True
        assert s.keep(_win(app="notepad.exe")) is False

    def test_single_matches_on_process_name_when_application_absent(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="code.exe")
        ev = _win(app=None, title="main.py", proc="code.exe")
        assert s.keep(ev) is True

    def test_single_substring_match_in_window_title(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="chrome.exe",
                           source_title="GitHub")
        assert s.keep(_win(app="chrome.exe", title="GitHub - Chrome")) is True
        assert s.keep(_win(app="chrome.exe", title="Gmail - Chrome")) is False

    def test_single_with_no_source_app_keeps_everything(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="")
        assert s.keep(_win(app="anything.exe")) is True

    def test_session_and_marker_events_always_kept_in_single(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="chrome.exe")
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

    def test_metadata_less_event_kept_in_single(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="chrome.exe")
        click = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert s.keep(click) is True

    def test_introspector_interaction_event_kept_in_single(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="chrome.exe")
        ev = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
            application="GitHub - Google Chrome",
            process_name=None,
        )
        assert s.keep(ev) is True

    def test_exe_suffix_target_matches_friendly_app_name(self):
        from sharing_on.collectors.sources import CaptureSources
        s = CaptureSources(mode="single", source_app="chrome.exe")
        assert s.keep(_win(app="Google Chrome", proc="Google Chrome")) is True
        assert s.keep(_win(app="Notepad", proc="Notepad")) is False

    def test_is_single_flag(self):
        from sharing_on.collectors.sources import CaptureSources
        assert CaptureSources(mode="single", source_app="x").is_single is True
        assert CaptureSources(mode="all").is_single is False
        assert CaptureSources(mode="single", source_app="").is_single is False


class TestBaseCollectorSourcesHook:
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
        c._sources = None
        return c, stored

    def test_emit_with_no_sources_stores_everything(self):
        c, stored = self._collector()
        c.emit(_win(app="notepad.exe"))
        assert len(stored) == 1

    def test_emit_drops_event_failing_sources(self):
        from sharing_on.collectors.sources import CaptureSources
        c, stored = self._collector()
        c.set_sources(CaptureSources(mode="single", source_app="chrome.exe"))
        c.emit(_win(app="notepad.exe"))      # off-source -> dropped
        c.emit(_win(app="chrome.exe"))       # on-source -> kept
        assert len(stored) == 1
        assert stored[0].application == "chrome.exe"

    def test_set_sources_is_noop_for_all(self):
        from sharing_on.collectors.sources import CaptureSources
        c, stored = self._collector()
        c.set_sources(CaptureSources(mode="all"))
        c.emit(_win(app="notepad.exe"))
        assert len(stored) == 1


class TestCaptureSourcesConfig:
    def _clear(self, mp):
        for v in ("SHARING_ON_CAPTURE_SOURCES", "SHARING_ON_CAPTURE_SOURCE_APP",
                  "SHARING_ON_CAPTURE_SOURCE_TITLE", "SHARING_ON_CAPTURE_SCOPE",
                  "SHARING_ON_CAPTURE_APP", "SHARING_ON_CAPTURE_TITLE"):
            mp.delenv(v, raising=False)

    def test_defaults_are_all_with_empty_sources(self, monkeypatch):
        self._clear(monkeypatch)
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_sources_mode == "all"
        assert c.capture_source_app == ""
        assert c.capture_source_title == ""

    def test_from_env_parses_new_vars(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("SHARING_ON_CAPTURE_SOURCES", "Single")
        monkeypatch.setenv("SHARING_ON_CAPTURE_SOURCE_APP", "chrome.exe")
        monkeypatch.setenv("SHARING_ON_CAPTURE_SOURCE_TITLE", "GitHub")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_sources_mode == "single"   # lower-cased
        assert c.capture_source_app == "chrome.exe"
        assert c.capture_source_title == "GitHub"

    def test_invalid_mode_falls_back_to_all(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("SHARING_ON_CAPTURE_SOURCES", "everything")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_sources_mode == "all"

    def test_legacy_env_vars_still_read_with_token_remap(self, monkeypatch):
        # BACK-COMPAT: v0.9.34.1 shipped SHARING_ON_CAPTURE_SCOPE=narrow etc.
        # publicly; from_env must still honour them, remapping narrow->single.
        self._clear(monkeypatch)
        monkeypatch.setenv("SHARING_ON_CAPTURE_SCOPE", "narrow")
        monkeypatch.setenv("SHARING_ON_CAPTURE_APP", "code.exe")
        monkeypatch.setenv("SHARING_ON_CAPTURE_TITLE", "main.py")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_sources_mode == "single"
        assert c.capture_source_app == "code.exe"
        assert c.capture_source_title == "main.py"

    def test_new_env_var_wins_over_legacy(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("SHARING_ON_CAPTURE_SCOPE", "narrow")
        monkeypatch.setenv("SHARING_ON_CAPTURE_SOURCES", "all")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.capture_sources_mode == "all"


class TestSessionSourcesWiring:
    def _session(self, tmp_path, mode="all", app="", title=""):
        from sharing_on.config import Config
        from sharing_on.session import CaptureSession
        cfg = Config.from_env()
        cfg.capture_sources_mode = mode
        cfg.capture_source_app = app
        cfg.capture_source_title = title
        return CaptureSession(name="t", config=cfg, output_dir=tmp_path)

    def test_build_sources_returns_single_when_configured(self, tmp_path):
        from sharing_on.collectors.sources import CaptureSources
        s = self._session(tmp_path, mode="single", app="chrome.exe", title="X")
        srcs = s._build_sources()
        assert isinstance(srcs, CaptureSources)
        assert srcs.is_single is True
        assert srcs.source_app == "chrome.exe"
        assert srcs.source_title == "X"

    def test_build_sources_all_by_default(self, tmp_path):
        s = self._session(tmp_path)
        assert s._build_sources().is_single is False

    def test_collectors_receive_sources(self, tmp_path):
        s = self._session(tmp_path, mode="single", app="chrome.exe")
        s._build_collectors()
        assert s._collectors, "expected at least one collector"
        for c in s._collectors:
            assert c._sources is not None
            assert c._sources.source_app == "chrome.exe"

    def test_session_json_records_sources(self, tmp_path):
        import json
        s = self._session(tmp_path, mode="single", app="chrome.exe", title="GitHub")
        s._save_metadata()
        meta = json.loads((tmp_path / "session.json").read_text())
        assert meta["capture_sources_mode"] == "single"
        assert meta["capture_source_app"] == "chrome.exe"
        assert meta["capture_source_title"] == "GitHub"

    def test_session_json_back_compat_keys_present(self, tmp_path):
        # BACK-COMPAT: keep the v0.9.34.1 keys (with broad/narrow tokens) so a
        # reader pinned to the old schema still finds them.
        import json
        s = self._session(tmp_path, mode="single", app="chrome.exe", title="GitHub")
        s._save_metadata()
        meta = json.loads((tmp_path / "session.json").read_text())
        assert meta["capture_scope"] == "narrow"   # single -> narrow
        assert meta["capture_target_app"] == "chrome.exe"
        assert meta["capture_target_title"] == "GitHub"

    def test_session_json_all_default(self, tmp_path):
        import json
        s = self._session(tmp_path)
        s._save_metadata()
        meta = json.loads((tmp_path / "session.json").read_text())
        assert meta["capture_sources_mode"] == "all"
        assert meta["capture_scope"] == "broad"    # back-compat token
        assert meta["capture_source_app"] == ""


class TestWebExtensionSources:
    def _make_collector(self, sources=None):
        from sharing_on.collectors.web_extension import WebExtensionCollector
        col = WebExtensionCollector.__new__(WebExtensionCollector)
        emitted = []
        col.emit = emitted.append  # type: ignore[attr-defined]
        col._sources = sources
        return col, emitted

    def test_all_keeps_all_origins(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        col, emitted = self._make_collector(sources=None)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/x"})
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://gmail.com/y"})
        assert len(emitted) == 2

    def test_single_keeps_only_allowed_origin(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        from sharing_on.collectors.sources import CaptureSources
        sources = CaptureSources(mode="single", source_app="https://github.com")
        col, emitted = self._make_collector(sources=sources)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/foo/bar"})
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://gmail.com/inbox"})
        assert len(emitted) == 1
        assert emitted[0].data["url"] == "https://github.com/foo/bar"

    def test_single_browser_app_target_keeps_all_web(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        from sharing_on.collectors.sources import CaptureSources
        sources = CaptureSources(mode="single", source_app="chrome.exe")
        col, emitted = self._make_collector(sources=sources)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/x"})
        assert len(emitted) == 1

    def test_dashboard_origin_still_dropped_under_single(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://127.0.0.1:8765")
        from sharing_on.collectors.sources import CaptureSources
        sources = CaptureSources(mode="single", source_app="http://127.0.0.1:8765")
        col, emitted = self._make_collector(sources=sources)
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "http://127.0.0.1:8765/inbox"})
        assert emitted == []

    def test_real_emit_single_origin_via_should_capture(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        from sharing_on.collectors.web_extension import WebExtensionCollector
        from sharing_on.collectors.sources import CaptureSources
        stored = []
        class _Store:
            def put(self, ev): stored.append(ev)
        col = WebExtensionCollector.__new__(WebExtensionCollector)
        col._store = _Store()
        col._sources = CaptureSources(mode="single", source_app="https://github.com")
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://github.com/x"})
        col.handle_extension_event({"action": "mouse_click",
                                    "url": "https://gmail.com/y"})
        assert len(stored) == 1
        assert stored[0].data["url"] == "https://github.com/x"


class TestCliSourcesOptions:
    def _run(self, monkeypatch, args):
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
        result = runner.invoke(cli_mod.cli, ["record", "--no-analyze", *args])
        assert result.exit_code == 0, result.output
        return captured["config"]

    def test_default_is_all(self, monkeypatch):
        cfg = self._run(monkeypatch, ["--name", "t"])
        assert cfg.capture_sources_mode == "all"
        assert cfg.capture_source_app == ""

    def test_sources_single_with_source(self, monkeypatch):
        cfg = self._run(monkeypatch,
                        ["--name", "t", "--sources", "single",
                         "--source", "chrome.exe"])
        assert cfg.capture_sources_mode == "single"
        assert cfg.capture_source_app == "chrome.exe"

    def test_source_alone_implies_single(self, monkeypatch):
        cfg = self._run(monkeypatch, ["--name", "t", "--source", "code.exe"])
        assert cfg.capture_sources_mode == "single"
        assert cfg.capture_source_app == "code.exe"

    def test_legacy_scope_app_aliases_still_work(self, monkeypatch):
        # BACK-COMPAT: v0.9.34.1 docs/scripts used --scope narrow --app X.
        cfg = self._run(monkeypatch,
                        ["--name", "t", "--scope", "narrow", "--app", "chrome.exe"])
        assert cfg.capture_sources_mode == "single"
        assert cfg.capture_source_app == "chrome.exe"

    def test_legacy_app_alias_alone_implies_single(self, monkeypatch):
        cfg = self._run(monkeypatch, ["--name", "t", "--app", "code.exe"])
        assert cfg.capture_sources_mode == "single"
        assert cfg.capture_source_app == "code.exe"


class TestDashboardSourcesArgs:
    def test_all_args_have_no_source_flags(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("Deploy app", mode="all", source="")
        assert args == ["--name", "Deploy app", "--no-analyze"]

    def test_single_args_include_sources_and_source(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("Deploy app", mode="single",
                                     source="chrome.exe")
        assert args == ["--name", "Deploy app", "--no-analyze",
                        "--sources", "single", "--source", "chrome.exe"]

    def test_single_without_source_falls_back_to_all(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("t", mode="single", source="")
        assert "--sources" not in args
        assert args == ["--name", "t", "--no-analyze"]


class TestSingleEndToEnd:
    def _store(self):
        stored = []
        class _Store:
            def put(self, ev): stored.append(ev)
        return _Store(), stored

    def test_window_collector_drops_offsource_keeps_source(self):
        from sharing_on.collectors.window import WindowCollector
        from sharing_on.collectors.sources import CaptureSources
        store, stored = self._store()
        col = WindowCollector.__new__(WindowCollector)
        col._store = store
        col._sources = CaptureSources(mode="single", source_app="chrome.exe")
        col.emit(_win(app="notepad.exe", title="Untitled"))   # dropped
        col.emit(_win(app="chrome.exe", title="GitHub"))      # kept
        assert [e.application for e in stored] == ["chrome.exe"]

    def test_process_collector_respects_sources(self):
        from sharing_on.collectors.process import ProcessCollector
        from sharing_on.collectors.sources import CaptureSources
        store, stored = self._store()
        col = ProcessCollector.__new__(ProcessCollector)
        col._store = store
        col._sources = CaptureSources(mode="single", source_app="chrome.exe")
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

    def test_all_collector_keeps_both(self):
        from sharing_on.collectors.window import WindowCollector
        store, stored = self._store()
        col = WindowCollector.__new__(WindowCollector)
        col._store = store
        col._sources = None   # all
        col.emit(_win(app="notepad.exe"))
        col.emit(_win(app="chrome.exe"))
        assert len(stored) == 2
