# tests/test_v0935_generalization_record.py
"""v0.9.35 Phase 1 — record-time generalization toggle (config/CLI/session/dashboard)."""
from __future__ import annotations

import json

import pytest


class TestGeneralizationConfig:
    def test_default_is_standard(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_GENERALIZATION", raising=False)
        from sharing_on.config import Config
        assert Config.from_env().generalization_mode == "standard"

    def test_dataclass_default_is_standard(self):
        # Bare Config() (no from_env) must also default to standard.
        from sharing_on.config import Config
        assert Config().generalization_mode == "standard"

    def test_from_env_parses_each_valid_value(self, monkeypatch):
        from sharing_on.config import Config
        for raw, expected in (("broad", "broad"), ("Standard", "standard"),
                              ("NARROW", "narrow")):
            monkeypatch.setenv("SYSTEMU_GENERALIZATION", raw)
            assert Config.from_env().generalization_mode == expected

    def test_invalid_value_falls_back_to_standard(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_GENERALIZATION", "loose")
        from sharing_on.config import Config
        assert Config.from_env().generalization_mode == "standard"

    def test_distinct_from_capture_sources(self, monkeypatch):
        # generalization_mode is a SEPARATE field from the (Phase-0 renamed)
        # capture-sources filter. broad/narrow on the generalization toggle must
        # not bleed into the sources mode and vice versa.
        monkeypatch.setenv("SYSTEMU_GENERALIZATION", "broad")
        monkeypatch.setenv("SHARING_ON_CAPTURE_SOURCES", "single")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.generalization_mode == "broad"
        assert c.capture_sources_mode == "single"


class TestGeneralizationCli:
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

    def test_default_is_standard(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_GENERALIZATION", raising=False)
        cfg = self._run(monkeypatch, ["--name", "t"])
        assert cfg.generalization_mode == "standard"

    def test_generalize_broad(self, monkeypatch):
        cfg = self._run(monkeypatch, ["--name", "t", "--generalize", "broad"])
        assert cfg.generalization_mode == "broad"

    def test_generalize_narrow(self, monkeypatch):
        cfg = self._run(monkeypatch, ["--name", "t", "--generalize", "narrow"])
        assert cfg.generalization_mode == "narrow"

    def test_invalid_generalize_rejected_by_click(self, monkeypatch):
        from click.testing import CliRunner
        from sharing_on import cli as cli_mod
        result = CliRunner().invoke(
            cli_mod.cli, ["record", "--no-analyze", "--name", "t",
                          "--generalize", "loose"])
        assert result.exit_code != 0
        assert "loose" in result.output  # click.Choice rejection mentions the bad value

    def test_cli_flag_overrides_env(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_GENERALIZATION", "broad")
        cfg = self._run(monkeypatch, ["--name", "t", "--generalize", "narrow"])
        assert cfg.generalization_mode == "narrow"


class TestSessionGeneralizationWiring:
    def _session(self, tmp_path, mode="standard"):
        from sharing_on.config import Config
        from sharing_on.session import CaptureSession
        cfg = Config.from_env()
        cfg.generalization_mode = mode
        return CaptureSession(name="t", config=cfg, output_dir=tmp_path)

    def test_session_json_records_generalization(self, tmp_path):
        s = self._session(tmp_path, mode="broad")
        s._save_metadata()
        meta = json.loads((tmp_path / "session.json").read_text())
        assert meta["generalization"] == "broad"

    def test_session_json_standard_default(self, tmp_path):
        s = self._session(tmp_path)
        s._save_metadata()
        meta = json.loads((tmp_path / "session.json").read_text())
        assert meta["generalization"] == "standard"


class TestDashboardGeneralizationArgs:
    # NOTE: Phase 0 already renamed _record_dispatch_args' capture-filter kwargs
    # to mode=/source= (was scope=/app=). The generalization toggle is a
    # SEPARATE record-time knob; "standard" (default) emits no flag so the
    # spawned argv is byte-identical to today.
    def test_standard_omits_generalize_flag(self):
        # standard == today; emit no --generalize so behaviour is byte-identical.
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("Deploy app", mode="all", source="",
                                     generalization="standard")
        assert args == ["--name", "Deploy app", "--no-analyze"]

    def test_broad_appends_generalize_flag(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("Deploy app", mode="all", source="",
                                     generalization="broad")
        assert args == ["--name", "Deploy app", "--no-analyze",
                        "--generalize", "broad"]

    def test_narrow_generalization_appends_flag(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("t", mode="all", source="",
                                     generalization="narrow")
        assert args[-2:] == ["--generalize", "narrow"]

    def test_generalization_default_param_is_standard(self):
        # Calling without the kwarg (old callers) keeps today's behaviour.
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("t", mode="all", source="")
        assert "--generalize" not in args

    def test_sources_and_generalization_compose(self):
        from systemu.interface.dashboard import _record_dispatch_args
        args = _record_dispatch_args("t", mode="single", source="chrome.exe",
                                     generalization="broad")
        assert args == ["--name", "t", "--no-analyze",
                        "--sources", "single", "--source", "chrome.exe",
                        "--generalize", "broad"]
