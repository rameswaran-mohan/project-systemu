"""W13.x — `sharing_on setup`: safe, secure CLI key + model configuration.

Closes the pip-install gap (those users never ran install.py). Properties:
key never echoed (getpass), validated before store, .env at 0600, the
PRESET NAME is stored (not resolved model ids — the deepseek-v4 lesson),
and `daemon start` runs/points-at setup when no key exists.
"""
from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest


def _env(tmp_path):
    return tmp_path / ".env"


class TestKeyHandling:
    def test_validate_gates_before_store(self, tmp_path):
        from sharing_on.setup_flow import run_setup
        calls = []

        def fake_validate(k, **kw):
            calls.append(k)
            return (True, "")

        run_setup(interactive=False, key="sk-or-good", env_path=_env(tmp_path),
                  validate=True, validate_fn=fake_validate)
        assert calls == ["sk-or-good"], "the key must be probed before storing"

    def test_mask_never_shows_full_key(self):
        from sharing_on.setup_flow import mask_key
        m = mask_key("sk-or-v1-abcdef1234567890")
        assert "1234567890" not in m and m.endswith("7890") and "…" in m

    def test_interactive_uses_hidden_input(self):
        """getpass, not input(), for the secret — no echo to scrollback."""
        from sharing_on import setup_flow
        src = inspect.getsource(setup_flow.run_setup)
        assert "getpass_fn(" in src
        # the key prompt must NOT go through the echoing input_fn
        key_region = src.split("Step 2")[0]
        assert "input_fn(" not in key_region


class TestEnvWrite:
    def test_writes_0600_and_preserves_other_keys(self, tmp_path):
        from sharing_on.setup_flow import write_env_vars
        p = _env(tmp_path)
        p.write_text("EXISTING=keepme\nOPENROUTER_API_KEY=old\n", encoding="utf-8")
        write_env_vars({"OPENROUTER_API_KEY": "new"}, env_path=p)
        from sharing_on.setup_flow import _parse_env
        parsed = _parse_env(p.read_text(encoding="utf-8"))
        assert parsed["OPENROUTER_API_KEY"] == "new"
        assert parsed["EXISTING"] == "keepme"
        if os.name == "posix":
            mode = stat.S_IMODE(p.stat().st_mode)
            assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_key_present_reads_env_file(self, tmp_path, monkeypatch):
        from sharing_on.setup_flow import key_present
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        p = _env(tmp_path)
        assert key_present(env_path=p) is False
        p.write_text("OPENROUTER_API_KEY=sk-or-x\n", encoding="utf-8")
        assert key_present(env_path=p) is True


class TestStoresPresetNameNotIds:
    def test_preset_name_written_not_resolved_models(self, tmp_path):
        """THE deepseek-v4 lesson: store SYSTEMU_MODEL_PRESET=<name> so a
        later model-id fix applies automatically — never bake resolved ids."""
        from sharing_on.setup_flow import _parse_env, run_setup
        p = _env(tmp_path)
        run_setup(interactive=False, key="sk-x", preset="balanced",
                  env_path=p, validate=False)
        env = _parse_env(p.read_text(encoding="utf-8"))
        assert env.get("SYSTEMU_MODEL_PRESET") == "balanced"
        assert "SYSTEMU_TIER1_MODEL" not in env, \
            "must store the preset NAME, not resolved tier model ids"


class TestWizardSummary:
    def test_non_interactive_writes_only_given(self, tmp_path):
        from sharing_on.setup_flow import run_setup
        out = run_setup(interactive=False, key="sk-x", output_dir=str(tmp_path / "o"),
                        env_path=_env(tmp_path), validate=False)
        assert out["key_set"] is True
        assert out["preset"] is None  # not asked, not given
        assert Path(out["output_dir"]).is_dir()

    def test_full_interactive_flow_with_injected_io(self, tmp_path):
        from sharing_on.setup_flow import _parse_env, run_setup
        p = _env(tmp_path)
        inputs = iter(["1", str(tmp_path / "out")])  # preset choice, folder
        out = run_setup(
            interactive=True, env_path=p, validate=True,
            getpass_fn=lambda prompt: "sk-or-typed",
            input_fn=lambda prompt: next(inputs),
            print_fn=lambda s: None,
            validate_fn=lambda k, **kw: (True, ""))
        assert out["key_set"] and out["validated"]
        env = _parse_env(p.read_text(encoding="utf-8"))
        assert env["OPENROUTER_API_KEY"] == "sk-or-typed"
        assert env["SYSTEMU_MODEL_PRESET"] in ("balanced", "quality", "budget")


class TestCliWiring:
    def test_setup_command_registered(self):
        from sharing_on import cli
        src = inspect.getsource(cli)
        assert "def setup(" in src and "run_setup" in src

    def test_daemon_start_guards_on_missing_key(self):
        from systemu.interface import cli_commands
        # daemon_start is a click Command — its body is the .callback fn.
        fn = getattr(cli_commands.daemon_start, "callback",
                     cli_commands.daemon_start)
        src = inspect.getsource(fn)
        assert "key_present()" in src and "run_setup" in src, \
            "daemon start must onboard (TTY) or redirect (headless) when no key"
