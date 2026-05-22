"""Tool execution backends — pluggable runners for the ToolSandbox.

Today the runtime supports two backends:

* ``local``  — in-process subprocess on the host (default).
* ``docker`` — ephemeral Docker container.

Stubs are reserved for two more, planned in a follow-up PR:

* ``ssh``    — remote host over SSH.  Useful for "agent that operates my
               home server" patterns.
* ``wsl``    — explicit WSL backend for Windows users.

The backend is selected via the ``SYSTEMU_TOOL_BACKEND`` env var (enum
``local | docker | ssh | wsl``).  The legacy
``SYSTEMU_USE_DOCKER_SANDBOX`` boolean was removed in v0.3 per the
deprecation window declared in v0.2's MIGRATION.md.

The protocol :class:`ToolBackend` is intentionally minimal: a single
``execute(...)`` coroutine that returns a :class:`ToolResult`.  Backends
share their concept of "what's being run" through the implementation
path + JSON params contract that ToolSandbox already uses; backends only
differ on *where* the execution happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .protocol import ToolBackend, BackendName, resolve_backend_name


def create_backend(
    name: BackendName,
    *,
    vault_root: Path,
    max_output_bytes: int = 65_536,
    install_mode=None,
    approvals=None,
) -> ToolBackend:
    """Instantiate the concrete backend for *name*.

    ``install_mode`` + ``approvals`` are forwarded to ``LocalBackend`` so
    its subprocess fallback honours the same dependency-installer policy
    as the registry fast path.  Other backends ignore them (docker
    installs in-container; ssh/wsl are stubs).  Unknown names should never
    reach this function because :func:`resolve_backend_name` clamps to a
    known enum; this raises ``ValueError`` if someone bypasses the resolver.
    """
    if name == "local":
        from .local import LocalBackend
        return LocalBackend(
            vault_root=vault_root,
            max_output_bytes=max_output_bytes,
            install_mode=install_mode,
            approvals=approvals,
        )
    if name == "docker":
        from .docker import DockerBackend
        return DockerBackend(vault_root=vault_root, max_output_bytes=max_output_bytes)
    if name == "ssh":
        from .ssh import SshBackend
        return SshBackend(vault_root=vault_root)
    if name == "wsl":
        from .wsl import WslBackend
        return WslBackend(vault_root=vault_root)
    raise ValueError(f"unknown tool backend: {name!r}")


__all__ = [
    "ToolBackend",
    "BackendName",
    "resolve_backend_name",
    "create_backend",
]
