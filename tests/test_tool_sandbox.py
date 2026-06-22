import pytest
import os
from pathlib import Path

from systemu.runtime.tool_sandbox import ToolSandbox, ToolResult
from systemu.runtime.backend.local import LocalBackend


@pytest.fixture
def sandbox():
    # Pass a dummy path for vault_root
    return ToolSandbox(vault_root="dummy_vault")


@pytest.mark.asyncio
async def test_local_backend_strips_sensitive_envs(monkeypatch):
    """The env-stripping logic moved from ToolSandbox to LocalBackend in the
    Phase-4 backend-protocol extraction.  Verify it still walls off
    sensitive vars from tool subprocesses."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "super_secret")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "secret_stripe")

    backend = LocalBackend(vault_root=Path("dummy_vault"))
    restricted_env = backend._build_restricted_env()

    assert "OPENROUTER_API_KEY" not in restricted_env
    assert "STRIPE_SECRET_KEY" not in restricted_env


def test_sandbox_default_backend_is_local(sandbox):
    """When no env / explicit backend is set, ToolSandbox picks LocalBackend."""
    assert sandbox.backend_name == "local"
    assert isinstance(sandbox._backend, LocalBackend)


def test_sandbox_legacy_use_docker_kwarg_no_longer_accepted():
    """The use_docker= kwarg was removed in v0.3 — pass backend="docker"
    instead.  Calling with the old kwarg must raise TypeError."""
    with pytest.raises(TypeError):
        ToolSandbox(vault_root="dummy_vault", use_docker=True)  # type: ignore[call-arg]


def test_sandbox_explicit_backend_kwarg_picks_docker():
    """Pass backend="docker" to select the Docker backend explicitly."""
    from systemu.runtime.backend.docker import DockerBackend
    sandbox = ToolSandbox(vault_root="dummy_vault", backend="docker")
    assert sandbox.backend_name == "docker"
    assert isinstance(sandbox._backend, DockerBackend)


def test_sandbox_env_var_picks_backend(monkeypatch):
    """SYSTEMU_TOOL_BACKEND env var wins when no explicit backend is given."""
    from systemu.runtime.backend.docker import DockerBackend
    monkeypatch.setenv("SYSTEMU_TOOL_BACKEND", "docker")
    sandbox = ToolSandbox(vault_root="dummy_vault")
    assert sandbox.backend_name == "docker"
    assert isinstance(sandbox._backend, DockerBackend)


@pytest.mark.asyncio
async def test_is_destructive_call():
    # Test our heuristics
    assert ToolSandbox.is_destructive_call("delete_file", {}) == True
    assert ToolSandbox.is_destructive_call("run_shell", {"cmd": "rm -rf /"}) == True
    assert ToolSandbox.is_destructive_call("read_file", {"path": "test.txt"}) == False
