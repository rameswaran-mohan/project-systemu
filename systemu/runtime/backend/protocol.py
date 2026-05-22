"""The ToolBackend protocol and backend-name resolution.

Backends do one thing: execute a Python script with a JSON params arg
and return a :class:`ToolResult`.  Where the script runs (host process,
Docker container, remote SSH session, WSL) is the backend's choice;
the runtime doesn't care.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from systemu.runtime.tool_sandbox import ToolResult

logger = logging.getLogger(__name__)


BackendName = Literal["local", "docker", "ssh", "wsl"]
_BACKEND_NAMES: tuple[str, ...] = ("local", "docker", "ssh", "wsl")


@runtime_checkable
class ToolBackend(Protocol):
    """One way to execute a tool implementation.

    All backends share the same shape so the runtime can swap them
    without conditionals.  Each backend is responsible for:

    * Setting up its execution environment (subprocess / container / SSH
      session / WSL invocation).
    * Stripping or whitelisting environment variables that shouldn't
      cross the trust boundary.
    * Enforcing the timeout (call ``proc.kill()`` on overrun).
    * Capturing stdout/stderr and returning a structured ``ToolResult``.

    The backend does NOT decide *whether* a tool runs — Gate 3 / approval
    checks happen upstream in ``ToolSandbox.execute_tool``.
    """

    async def execute(
        self,
        impl_path: Path,
        params_json: str,
        *,
        timeout: int,
        extra_packages: Optional[List[str]] = None,
    ) -> "ToolResult":
        """Execute *impl_path* with the given JSON params and return a result.

        Args:
            impl_path:      Absolute path to the tool's Python implementation.
            params_json:    Already-serialised JSON string for ``--params``.
            timeout:        Seconds to allow before killing the run.
            extra_packages: pip packages to install before execution
                            (Docker only; other backends should accept the
                            arg and ignore or warn).
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
#  Backend-name resolution from env vars
# ─────────────────────────────────────────────────────────────────────────────

def resolve_backend_name(
    *,
    explicit: Optional[str] = None,
) -> BackendName:
    """Pick the canonical backend name from env / explicit args.

    Resolution order (highest priority first):

    1. ``explicit`` arg, when given.  Lets callers force a specific
       backend in tests without touching env.
    2. ``SYSTEMU_TOOL_BACKEND`` env var.
    3. Default — ``local``.

    Unknown enum values fall back to ``local`` with a warning, never an
    exception — we'd rather have the runtime keep running than fail boot
    on a config typo.

    The legacy ``use_docker_sandbox`` boolean kwarg and the
    ``SYSTEMU_USE_DOCKER_SANDBOX`` env var were removed in v0.3 per the
    deprecation window declared in v0.2's MIGRATION.md.
    """
    if explicit:
        return _normalise(explicit)

    env_val = (os.environ.get("SYSTEMU_TOOL_BACKEND") or "").strip().lower()
    if env_val:
        return _normalise(env_val)

    return "local"


def _normalise(value: str) -> BackendName:
    val = value.strip().lower()
    if val in _BACKEND_NAMES:
        return val  # type: ignore[return-value]
    logger.warning(
        "[Backend] Unknown SYSTEMU_TOOL_BACKEND value %r — falling back to 'local'. "
        "Valid values: %s",
        value, ", ".join(_BACKEND_NAMES),
    )
    return "local"
