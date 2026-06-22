"""Regression tests for v0.8.0.3: project_root must resolve correctly across
all install modes so the dashboard's record→stop→refine pipeline works.

The bug being locked down here was:
  AppState._project_root = Path(systemu.__file__).parent.parent.absolute()
which silently resolved to `Lib/site-packages/` on every pip install, causing
the dashboard's `_stop_capture` to look for the captures dir at the wrong
absolute path and silently fail to dispatch the refine job.

Tests use the new `_resolve_project_root(vault, config)` helper directly,
exercising the four-tier lookup against synthetic fixtures that mimic each
install + deploy mode (local pip install, git-clone editable, docker).
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from systemu.interface.dashboard_state import _resolve_project_root


def _mk_vault_with_root(root_path: Path) -> MagicMock:
    """Build a stand-in vault object whose .root attribute is the given path.

    Uses `spec=["root"]` so that accessing `._v` (which the resolver tries first
    to peek through a FileVault wrapper) raises AttributeError instead of
    auto-mocking — that way `getattr(vault, "_v", vault)` correctly falls back
    to the vault itself.
    """
    inner = MagicMock(spec=["root"])
    inner.root = str(root_path)
    return inner


def _mk_filevault_wrapper(inner_vault) -> MagicMock:
    """Mimic the FileVault wrapper pattern (Vault stored at ._v)."""
    wrapper = MagicMock(spec=["_v"])
    wrapper._v = inner_vault
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — explicit env var wins (covers docker mode and operator overrides)
# ─────────────────────────────────────────────────────────────────────────────

def test_tier1_explicit_env_var_wins(tmp_path, monkeypatch):
    """When SYSTEMU_PROJECT_ROOT is set to a real dir, it overrides everything."""
    explicit = tmp_path / "explicit_root"
    explicit.mkdir()
    monkeypatch.setenv("SYSTEMU_PROJECT_ROOT", str(explicit))

    # Even with a misleading vault path, the env var wins
    vault = _mk_vault_with_root(tmp_path / "elsewhere" / "vault")
    config = MagicMock(vault_dir="systemu/vault")

    assert _resolve_project_root(vault, config) == str(explicit.resolve())


def test_tier1_env_var_ignored_when_path_missing(tmp_path, monkeypatch):
    """If SYSTEMU_PROJECT_ROOT points to a non-existent dir, fall through."""
    monkeypatch.setenv("SYSTEMU_PROJECT_ROOT", str(tmp_path / "does-not-exist"))

    vault_root = tmp_path / "project" / "systemu" / "vault"
    vault_root.mkdir(parents=True)
    (tmp_path / "project" / ".env").write_text("")
    vault = _mk_vault_with_root(vault_root)
    config = MagicMock(vault_dir="systemu/vault")

    # Should fall through to Tier 2 (walk-up) and find .env at project/
    assert _resolve_project_root(vault, config) == str((tmp_path / "project").resolve())


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — walk up from vault root looking for .env (pip install + git clone)
# ─────────────────────────────────────────────────────────────────────────────

def test_tier2_walks_up_to_env(tmp_path, monkeypatch):
    """Mimics a PyPI pip install: user has .env in their working dir."""
    monkeypatch.delenv("SYSTEMU_PROJECT_ROOT", raising=False)
    tryout = tmp_path / "systemu-tryout"
    vault_root = tryout / "systemu" / "vault"
    vault_root.mkdir(parents=True)
    (tryout / ".env").write_text("OPENROUTER_API_KEY=fake\n")

    vault = _mk_vault_with_root(vault_root)
    config = MagicMock(vault_dir="systemu/vault")

    assert _resolve_project_root(vault, config) == str(tryout.resolve())


def test_tier2_walks_up_through_filevault_wrapper(tmp_path, monkeypatch):
    """FileVault adapter exposes .root via ._v (the inner Vault)."""
    monkeypatch.delenv("SYSTEMU_PROJECT_ROOT", raising=False)
    project = tmp_path / "project"
    vault_root = project / "systemu" / "vault"
    vault_root.mkdir(parents=True)
    (project / ".env").write_text("")

    inner = _mk_vault_with_root(vault_root)
    wrapper = _mk_filevault_wrapper(inner)
    config = MagicMock(vault_dir="systemu/vault")

    assert _resolve_project_root(wrapper, config) == str(project.resolve())


def test_tier2_handles_vault_at_repo_root(tmp_path, monkeypatch):
    """Editable install where vault is at the same level as .env."""
    monkeypatch.delenv("SYSTEMU_PROJECT_ROOT", raising=False)
    repo = tmp_path / "code" / "systemu"
    vault_root = repo / "systemu" / "vault"
    vault_root.mkdir(parents=True)
    (repo / ".env").write_text("")

    vault = _mk_vault_with_root(vault_root)
    config = MagicMock(vault_dir="systemu/vault")

    assert _resolve_project_root(vault, config) == str(repo.resolve())


# ─────────────────────────────────────────────────────────────────────────────
# Tier 3 — vault parent fallback when no .env found anywhere up the tree
# ─────────────────────────────────────────────────────────────────────────────

def test_tier3_falls_back_to_vault_parent_when_no_env(tmp_path, monkeypatch):
    """Docker without env var or test environment: no .env exists anywhere."""
    monkeypatch.delenv("SYSTEMU_PROJECT_ROOT", raising=False)
    vault_root = tmp_path / "data" / "vault"
    vault_root.mkdir(parents=True)
    # Deliberately NOT writing any .env file anywhere up the tree

    vault = _mk_vault_with_root(vault_root)
    config = MagicMock(vault_dir="systemu/vault")

    # Should fall back to vault's parent
    assert _resolve_project_root(vault, config) == str((tmp_path / "data").resolve())


# ─────────────────────────────────────────────────────────────────────────────
# Tier 4 — config.vault_dir fallback when vault has no root attribute
# ─────────────────────────────────────────────────────────────────────────────

def test_tier4_falls_back_to_config_vault_dir(tmp_path, monkeypatch):
    """Last-ditch: vault object has no .root and no ._v.root either."""
    monkeypatch.delenv("SYSTEMU_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    vault = MagicMock(spec=[])  # no .root, no ._v
    config = MagicMock()
    config.vault_dir = "systemu/vault"

    # Should resolve config.vault_dir relative to CWD and return its parent
    expected = (tmp_path / "systemu").resolve()
    assert _resolve_project_root(vault, config) == str(expected)


# ─────────────────────────────────────────────────────────────────────────────
# Regression — the original bug (pre-v0.8.0.3 behavior)
# ─────────────────────────────────────────────────────────────────────────────

def test_does_not_return_site_packages_for_pip_install(tmp_path, monkeypatch):
    """Lock down the actual bug: for a pip-installed user, project_root must
    NOT resolve to site-packages, even when systemu.__file__ is in site-packages.
    """
    monkeypatch.delenv("SYSTEMU_PROJECT_ROOT", raising=False)
    user_dir = tmp_path / "my-project"
    vault_root = user_dir / "systemu" / "vault"
    vault_root.mkdir(parents=True)
    (user_dir / ".env").write_text("")

    vault = _mk_vault_with_root(vault_root)
    config = MagicMock(vault_dir="systemu/vault")

    result = _resolve_project_root(vault, config)
    assert "site-packages" not in result
    assert result == str(user_dir.resolve())
