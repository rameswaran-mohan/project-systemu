"""Unit tests for the ToolBackend protocol + backend resolver (Phase 4).

Covers:
    * Backend-name resolution from env vars + explicit args + legacy
      boolean shim.
    * Each concrete backend implements the protocol.
    * Stubs (ssh / wsl) return a clear error when called.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from systemu.runtime.backend import (
    BackendName,
    ToolBackend,
    create_backend,
    resolve_backend_name,
)


# ── resolve_backend_name ────────────────────────────────────────────────

def test_resolve_default_is_local(monkeypatch):
    monkeypatch.delenv("SYSTEMU_TOOL_BACKEND", raising=False)
    monkeypatch.delenv("SYSTEMU_USE_DOCKER_SANDBOX", raising=False)
    assert resolve_backend_name() == "local"


def test_resolve_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("SYSTEMU_TOOL_BACKEND", "docker")
    assert resolve_backend_name(explicit="local") == "local"


@pytest.mark.parametrize("name", ["local", "docker", "ssh", "wsl"])
def test_resolve_each_known_backend(monkeypatch, name: str):
    monkeypatch.setenv("SYSTEMU_TOOL_BACKEND", name)
    assert resolve_backend_name() == name


def test_resolve_unknown_falls_back_to_local(monkeypatch, caplog):
    monkeypatch.setenv("SYSTEMU_TOOL_BACKEND", "bogus")
    with caplog.at_level("WARNING"):
        assert resolve_backend_name() == "local"
    assert any("Unknown" in r.message for r in caplog.records)


def test_legacy_use_docker_sandbox_env_is_ignored(monkeypatch):
    """Removed in v0.3 — must NOT influence the resolver.  Operators
    who still have it in their .env should see local backend, not
    docker."""
    monkeypatch.delenv("SYSTEMU_TOOL_BACKEND", raising=False)
    monkeypatch.setenv("SYSTEMU_USE_DOCKER_SANDBOX", "true")
    assert resolve_backend_name() == "local"


def test_legacy_use_docker_sandbox_kwarg_no_longer_accepted():
    """The use_docker_sandbox= kwarg was removed in v0.3.  Calling with
    it must raise TypeError."""
    with pytest.raises(TypeError):
        resolve_backend_name(use_docker_sandbox=True)  # type: ignore[call-arg]


# ── create_backend ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["local", "docker", "ssh", "wsl"])
def test_create_backend_returns_protocol(tmp_path: Path, name: BackendName):
    backend = create_backend(name, vault_root=tmp_path)
    assert isinstance(backend, ToolBackend)


def test_create_backend_raises_on_unknown(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown tool backend"):
        create_backend("bogus", vault_root=tmp_path)  # type: ignore[arg-type]


# ── Stub backends return clear errors ───────────────────────────────────

def test_ssh_stub_returns_not_implemented_error(tmp_path: Path):
    from systemu.runtime.backend.ssh import SshBackend
    backend = SshBackend(vault_root=tmp_path)
    result = asyncio.run(backend.execute(
        tmp_path / "dummy.py", "{}", timeout=1,
    ))
    assert not result.success
    assert "not yet implemented" in (result.error or "")


def test_wsl_stub_returns_not_implemented_error(tmp_path: Path):
    from systemu.runtime.backend.wsl import WslBackend
    backend = WslBackend(vault_root=tmp_path)
    result = asyncio.run(backend.execute(
        tmp_path / "dummy.py", "{}", timeout=1,
    ))
    assert not result.success
    assert "not yet implemented" in (result.error or "")


# ── LocalBackend executes real subprocess ───────────────────────────────

def test_local_backend_executes_subprocess(tmp_path: Path):
    """End-to-end smoke: write a tiny Python script that returns JSON,
    run it via LocalBackend, assert we got the right output."""
    from systemu.runtime.backend.local import LocalBackend

    impl = tmp_path / "echo_tool.py"
    impl.write_text(
        "import json, sys\n"
        "args = sys.argv[sys.argv.index('--params') + 1]\n"
        "params = json.loads(args)\n"
        "print(json.dumps({'success': True, 'echo': params}))\n",
        encoding="utf-8",
    )
    backend = LocalBackend(vault_root=tmp_path)
    result = asyncio.run(backend.execute(
        impl, '{"hello": "world"}', timeout=10,
    ))
    assert result.success
    assert result.parsed.get("echo") == {"hello": "world"}
