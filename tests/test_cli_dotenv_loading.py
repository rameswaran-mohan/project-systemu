"""v0.8.0.2: every CLI verb loads .env from CWD (the user's working dir),
not just the install-time _PROJECT_ROOT/.env."""
import os
import pytest


def _isolated_cwd_env(tmp_path, monkeypatch, **env_vars):
    """Helper: write .env in tmp_path, chdir there, clear conflicting env."""
    env_path = tmp_path / ".env"
    lines = []
    for k, v in env_vars.items():
        lines.append(f"{k}={v}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for k in env_vars:
        monkeypatch.delenv(k, raising=False)
    return env_path


def test_config_loads_dotenv_from_cwd(tmp_path, monkeypatch):
    """Config.from_env() must read .env from CWD (the bug we're fixing)."""
    _isolated_cwd_env(
        tmp_path, monkeypatch,
        OPENROUTER_API_KEY="test-cwd-marker-12345",
        SYSTEMU_STORAGE="file",
    )
    # Force a fresh import so module-level dotenv-load picks up the new CWD
    import importlib, sharing_on.config
    importlib.reload(sharing_on.config)
    cfg = sharing_on.config.Config.from_env()
    assert cfg.openrouter_api_key == "test-cwd-marker-12345", (
        f"Config did not load .env from CWD. "
        f"openrouter_api_key={cfg.openrouter_api_key!r}"
    )


def test_cwd_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    """If OPENROUTER_API_KEY is already set in the process env, the CWD .env
    must NOT clobber it. (override=False semantics.) This protects subprocess
    callers whose parent process set the env explicitly."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-process-env-value")
    _isolated_cwd_env(
        tmp_path, monkeypatch,
        OPENROUTER_API_KEY="cwd-env-should-not-win",
        SYSTEMU_STORAGE="file",
    )
    # But re-set the real env after _isolated_cwd_env cleared it
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-process-env-value")
    import importlib, sharing_on.config
    importlib.reload(sharing_on.config)
    cfg = sharing_on.config.Config.from_env()
    assert cfg.openrouter_api_key == "real-process-env-value", (
        f"CWD .env clobbered the real process env. Got {cfg.openrouter_api_key!r}"
    )


def test_no_dotenv_in_cwd_is_safe(tmp_path, monkeypatch):
    """No .env in CWD is fine — config just reads OS env vars normally.

    The reload may load the legacy project-root .env (if one exists in the
    dev/editable install), so we clear the key again after the reload to
    simulate a clean OS-env state with no key set anywhere.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import importlib, sharing_on.config
    importlib.reload(sharing_on.config)
    # The module-level reload may have pulled OPENROUTER_API_KEY from the
    # install-dir .env (legacy load).  Clear it again so Config.from_env()
    # sees an empty env — this is the state an operator without any .env would
    # experience.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = sharing_on.config.Config.from_env()
    # No error raised, openrouter_api_key just empty
    assert cfg.openrouter_api_key == ""
