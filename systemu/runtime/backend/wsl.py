"""WslBackend — placeholder for explicit WSL execution on Windows.

Status: **stub.** Today Windows users get the local backend which
runs in whatever Python the daemon was started under — which is
fine for most cases but doesn't let an operator deliberately run a
tool *inside* WSL.  This backend provides the explicit toggle.

Design intent (for the follow-up PR):

* Inputs come from new env vars: ``SYSTEMU_WSL_DISTRO`` (defaults to
  the user's default WSL distro), ``SYSTEMU_WSL_PYTHON`` (defaults
  to ``/usr/bin/python3``).
* Each call shells out to ``wsl.exe -d <distro> -- <python> <impl>
  --params <json>`` with a translated path for ``impl``.
* Stdout/stderr captured exactly like the local backend.
* Useful for: Linux-only tools (apt-installed binaries, system
  libraries) running under a Windows-hosted daemon.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class WslBackend:
    """Placeholder.  Raises a clear error until the v0.4 implementation lands."""

    def __init__(self, *, vault_root: Path, **_kwargs) -> None:
        self.vault_root = Path(vault_root)

    async def execute(
        self,
        impl_path: Path,
        params_json: str,
        *,
        timeout: int,
        extra_packages: Optional[List[str]] = None,
    ):
        from systemu.runtime.tool_sandbox import ToolResult

        msg = (
            "WSL tool backend is not yet implemented (planned for v0.4).  "
            "Set SYSTEMU_TOOL_BACKEND=local or =docker for now."
        )
        logger.error("[WslBackend] %s", msg)
        return ToolResult(success=False, error=msg)
