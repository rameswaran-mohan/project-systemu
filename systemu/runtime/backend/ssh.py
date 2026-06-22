"""SshBackend — placeholder for remote-host tool execution over SSH.

Status: **stub.** The protocol is in place so callers can target
``SYSTEMU_TOOL_BACKEND=ssh`` and get a clear error today rather than
silently falling back.  The full implementation ships in a follow-up
PR.

Design intent (for that follow-up):

* Inputs come from new env vars: ``SYSTEMU_SSH_HOST``,
  ``SYSTEMU_SSH_USER``, ``SYSTEMU_SSH_KEY``, ``SYSTEMU_SSH_PORT``.
* For each call, ``rsync`` (or ``scp``) the tool file to a known
  per-execution scratch dir on the remote, then ``ssh`` to run it
  with the JSON params on stdin.
* Capture stdout/stderr exactly like the local backend.
* The trust boundary is the SSH connection — the remote host is
  treated as a less-trusted environment, and the operator opts in
  by setting the env vars.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class SshBackend:
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
            "SSH tool backend is not yet implemented (planned for v0.4).  "
            "Set SYSTEMU_TOOL_BACKEND=local or =docker for now, or open an "
            "issue on the repo if you need this backend prioritised."
        )
        logger.error("[SshBackend] %s", msg)
        return ToolResult(success=False, error=msg)
