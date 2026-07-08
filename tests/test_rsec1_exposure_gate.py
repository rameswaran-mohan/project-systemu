"""R-SEC1 — the fail-closed dashboard exposure gate, wired into run_dashboard.

The rule (see systemu.runtime.dashboard_auth.exposure_check):
  * non-loopback bind + NO passphrase configured  -> REFUSE to start (SystemExit),
    and never bind (ui.run must NOT be reached).
  * loopback bind + NO passphrase                  -> start, but log a warning.
  * any bind + passphrase configured               -> start.
  * ui.run always receives a `storage_secret` (the persisted session secret).

These tests never actually serve: `nicegui.ui.run` is monkeypatched to a stub
that records kwargs and returns immediately. run_dashboard does
`from nicegui import ui, app as ng_app` INSIDE the function, binding `ui` to the
`nicegui.ui` MODULE — so patching `nicegui.ui.run` before the call takes effect.
"""
from __future__ import annotations

import os

import pytest

from systemu.interface import dashboard
from systemu.interface.dashboard_state import AppState


class _StubUiRun:
    """Records the kwargs of the (stubbed) ui.run call; returns immediately."""

    def __init__(self):
        self.called = False
        self.kwargs = {}

    def __call__(self, *args, **kwargs):
        self.called = True
        self.kwargs = kwargs
        return None


@pytest.fixture
def stub_ui_run(monkeypatch):
    import nicegui.ui

    stub = _StubUiRun()
    monkeypatch.setattr(nicegui.ui, "run", stub)
    return stub


@pytest.fixture(autouse=True)
def _reset_appstate_singleton():
    """AppState is a process-wide singleton; reset it around each start-path
    test so a start test builds its OWN file backend on its OWN tmp vault
    instead of silently reusing a previous test's AppState."""
    AppState._instance = None
    yield
    AppState._instance = None


@pytest.fixture
def tmp_vault_config(tmp_path, monkeypatch):
    """A minimal Config pointing at a throwaway file vault.

    Config's vault dir attribute is `vault_dir` (sharing_on/config.py:186).
    Force the file backend so AppState.create() is hermetic (no DB / network).
    """
    from sharing_on.config import Config

    monkeypatch.setenv("SYSTEMU_STORAGE", "file")
    monkeypatch.setenv("SYSTEMU_NON_INTERACTIVE", "true")
    # Avoid the fresh-vault welcome funnel affecting anything during boot setup.
    monkeypatch.setenv("SYSTEMU_SKIP_ONBOARDING", "1")
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    return Config(vault_dir=str(vault_dir))


def test_nonloopback_without_passphrase_refuses(monkeypatch, tmp_vault_config, stub_ui_run):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    with pytest.raises(SystemExit):
        dashboard.run_dashboard(tmp_vault_config, host="0.0.0.0", port=0)
    assert not stub_ui_run.called          # refused BEFORE binding


def test_loopback_without_passphrase_starts_with_secret(monkeypatch, tmp_vault_config, stub_ui_run):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    dashboard.run_dashboard(tmp_vault_config, host="127.0.0.1", port=0)
    assert stub_ui_run.called
    assert stub_ui_run.kwargs.get("storage_secret")   # session secret always set


def test_nonloopback_with_passphrase_starts(monkeypatch, tmp_vault_config, stub_ui_run):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", "scrypt$14$8$1$aa$bb")
    dashboard.run_dashboard(tmp_vault_config, host="0.0.0.0", port=0)
    assert stub_ui_run.called
    assert stub_ui_run.kwargs.get("storage_secret")


def test_loopback_start_passes_storage_secret_even_with_passphrase(
    monkeypatch, tmp_vault_config, stub_ui_run
):
    """storage_secret is ALWAYS set, independent of the auth posture."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", "scrypt$14$8$1$aa$bb")
    dashboard.run_dashboard(tmp_vault_config, host="127.0.0.1", port=0)
    assert stub_ui_run.called
    assert isinstance(stub_ui_run.kwargs.get("storage_secret"), str)
    assert stub_ui_run.kwargs["storage_secret"]
