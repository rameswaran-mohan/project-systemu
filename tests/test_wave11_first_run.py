"""W11.3 — setup enforcement + the auto-setup engine.

Operator requirement (2026-06-12): "enforce the model details and keys to
setup and very important settings questions to be done at the time of
installation itself. systemu should be able to ensure it automatically
sets things up correctly for the user."

Two halves:
  * runtime truth — ``systemu/runtime/first_run.py``: ``setup_status``
    (is this install actually ready?) + ``auto_setup`` (fix what is safe
    to fix silently: directories — never keys, never model choices).
  * install-time enforcement — ``install.py``: the OpenRouter key prompt
    no longer accepts a casual blank; preset + output folder are asked.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(str(tmp_path / "vault"))


def _config(**kw):
    base = {"openrouter_api_key": "", "output_dir": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _by_id(checks, check_id):
    return next(c for c in checks if c["id"] == check_id)


class TestSetupStatus:
    def test_key_check_reads_config_and_env(self, vault, monkeypatch):
        from systemu.runtime.first_run import setup_status
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert _by_id(setup_status(_config(), vault), "key_present")["ok"] is False
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
        assert _by_id(setup_status(_config(), vault), "key_present")["ok"] is True

    def test_profile_check_flips_after_onboarding(self, vault, monkeypatch):
        from systemu.runtime.first_run import setup_status
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert _by_id(setup_status(_config(), vault), "profile_present")["ok"] is False
        from systemu.interface.pages.welcome import save_onboarding
        save_onboarding(vault, name="R", location="X", timezone="UTC",
                        output_dir="C:/x")
        assert _by_id(setup_status(_config(), vault), "profile_present")["ok"] is True

    def test_vault_seeded_check(self, vault, tmp_path):
        from systemu.runtime.first_run import setup_status
        assert _by_id(setup_status(_config(), vault), "vault_seeded")["ok"] is False
        idx = tmp_path / "vault" / "tools" / "index.json"
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text('[{"id": "tool_x", "name": "x"}]', encoding="utf-8")
        assert _by_id(setup_status(_config(), vault), "vault_seeded")["ok"] is True

    def test_models_check_is_informational(self, vault, monkeypatch):
        """No preset/tiers is OK (defaults) — reported, never blocking."""
        from systemu.runtime.first_run import setup_status
        monkeypatch.delenv("SYSTEMU_MODEL_PRESET", raising=False)
        c = _by_id(setup_status(_config(), vault), "models_configured")
        assert c["ok"] is True and c["required"] is False
        monkeypatch.setenv("SYSTEMU_MODEL_PRESET", "balanced")
        c = _by_id(setup_status(_config(), vault), "models_configured")
        assert "balanced" in c["detail"]

    def test_required_flags(self, vault):
        """The W11.4 gate may block only on key / profile / tour."""
        from systemu.runtime.first_run import setup_status
        checks = {c["id"]: c["required"] for c in setup_status(_config(), vault)}
        assert checks["key_present"] is True
        assert checks["profile_present"] is True
        assert checks["tour_completed"] is True
        assert checks["models_configured"] is False
        assert checks["output_dir_ok"] is False
        assert checks["vault_seeded"] is False

    def test_never_raises_on_broken_inputs(self):
        from systemu.runtime.first_run import setup_status
        checks = setup_status(object(), object())
        assert isinstance(checks, list) and checks


class TestAutoSetup:
    def test_creates_missing_output_dir(self, vault, tmp_path):
        from systemu.runtime.first_run import auto_setup
        target = tmp_path / "out" / "deep"
        fixed = auto_setup(_config(output_dir=str(target)), vault)
        assert target.is_dir()
        assert any("output" in f.lower() for f in fixed)

    def test_idempotent_second_run_fixes_nothing(self, vault, tmp_path):
        from systemu.runtime.first_run import auto_setup
        target = tmp_path / "out"
        cfg = _config(output_dir=str(target))
        auto_setup(cfg, vault)
        assert auto_setup(cfg, vault) == []

    def test_defaults_to_vault_output_when_unset(self, vault):
        from systemu.runtime.first_run import auto_setup
        auto_setup(_config(output_dir=""), vault)
        assert (Path(vault.root) / "output").is_dir()

    def test_never_touches_keys_or_models(self):
        from systemu.runtime import first_run
        src = inspect.getsource(first_run.auto_setup)
        for forbidden in ("OPENROUTER_API_KEY", "SYSTEMU_TIER", "SYSTEMU_MODEL_PRESET"):
            assert forbidden not in src, \
                "auto_setup must never write keys or change model choices"

    def test_never_raises(self):
        from systemu.runtime.first_run import auto_setup
        assert auto_setup(object(), object()) == []


class TestDaemonBootHook:
    def test_daemon_runs_auto_setup_best_effort(self):
        from systemu.scheduler import daemon
        src = inspect.getsource(daemon)
        assert "auto_setup(" in src, \
            "the daemon must ensure the install is set up at every boot"


class TestInstallEnforcement:
    """install.py: a blank key is no longer a silent shrug."""

    def _args(self, **kw):
        base = {"non_interactive": False, "openrouter_key": None,
                "google_key": None, "no_key": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_blank_key_is_re_asked(self, monkeypatch):
        import install
        answers = iter(["", "", "sk-or-valid", ""])  # 2 blanks, key, google
        monkeypatch.setattr(install, "prompt", lambda *a, **k: next(answers))
        monkeypatch.setattr(install, "validate_openrouter_key",
                            lambda key, proxies: (True, "ok"))
        monkeypatch.setattr(install, "detect_proxy_config", lambda: {})
        out = install.collect_api_keys(self._args())
        assert out["OPENROUTER_API_KEY"] == "sk-or-valid"

    def test_three_blanks_proceed_with_loud_warning(self, monkeypatch):
        import install
        answers = iter(["", "", "", ""])
        warnings = []
        monkeypatch.setattr(install, "prompt", lambda *a, **k: next(answers))
        monkeypatch.setattr(install, "warn", lambda msg: warnings.append(msg))
        monkeypatch.setattr(install, "detect_proxy_config", lambda: {})
        out = install.collect_api_keys(self._args())
        assert out["OPENROUTER_API_KEY"] == ""
        assert any("dashboard" in w.lower() or "first launch" in w.lower()
                   for w in warnings), "the operator must know enforcement follows"

    def test_no_key_flag_skips_quietly(self, monkeypatch):
        import install
        answers = iter(["", ""])
        monkeypatch.setattr(install, "prompt", lambda *a, **k: next(answers))
        monkeypatch.setattr(install, "detect_proxy_config", lambda: {})
        out = install.collect_api_keys(self._args(no_key=True))
        assert out["OPENROUTER_API_KEY"] == ""

    def test_setup_prefs_collects_preset_and_output_dir(self, monkeypatch, tmp_path):
        import install
        target = tmp_path / "outdir"
        monkeypatch.setattr(install, "prompt_choice", lambda *a, **k: "balanced")
        monkeypatch.setattr(install, "prompt", lambda *a, **k: str(target))
        out = install.collect_setup_prefs(self._args())
        assert out["SYSTEMU_MODEL_PRESET"] == "balanced"
        assert out["SYSTEMU_OUTPUT_DIR"] == str(target)
        assert target.is_dir(), "the chosen output folder is created immediately"

    def test_setup_prefs_decide_later_writes_no_preset(self, monkeypatch, tmp_path):
        import install
        monkeypatch.setattr(install, "prompt_choice", lambda *a, **k: "later")
        monkeypatch.setattr(install, "prompt", lambda *a, **k: str(tmp_path / "o"))
        out = install.collect_setup_prefs(self._args())
        assert "SYSTEMU_MODEL_PRESET" not in out, \
            "'decide later' must keep today's defaults byte-for-byte"

    def test_setup_prefs_non_interactive_is_empty(self):
        import install
        assert install.collect_setup_prefs(self._args(non_interactive=True)) == {}

    def test_all_three_mode_flows_collect_prefs(self):
        import install
        src = inspect.getsource(install)
        assert src.count("collect_setup_prefs(args)") >= 3, \
            "local, docker-local and docker-enterprise must all ask"
