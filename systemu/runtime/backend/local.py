"""LocalBackend — in-process subprocess on the host.

This is the default backend and the one ToolSandbox has always used
internally.  The implementation is identical to
``ToolSandbox._execute_subprocess`` — extracted here so the protocol
has a real implementation to point at and so future backends can be
added without changing the sandbox.

v0.3.3 — Honors the tool manifest's pip ``dependencies`` (passed in via
``extra_packages``) by delegating to
:func:`systemu.runtime.dependency_installer.ensure_satisfied` BEFORE the
subprocess is launched.  The subprocess re-invokes ``sys.executable``,
so any package installed here is visible to the tool script.  The
installer caches per-package, so this path is free on the second call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Env vars that ARE allowed to cross the trust boundary into the tool subprocess.
# Matches ToolSandbox._SAFE_ENV_KEYS for behavioural parity.
_SAFE_ENV_KEYS = {
    "PATH",
    "LANG",
    "OS",
    "SystemDrive",
    "SystemRoot",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "LC_ALL",
    "PYTHONPATH",
    "PLAYWRIGHT_BROWSERS_PATH",
}


class LocalBackend:
    """Run the tool as a subprocess on the host machine.

    Args:
        vault_root:        Tool-implementation root; subprocess CWD.
        max_output_bytes:  stdout/stderr capture cap.
        install_mode:      Optional explicit :class:`InstallMode` for the
                           self-heal path.  When omitted the backend falls
                           back to PROMPT — the safe default for local
                           mode.  Pass an explicit value when the parent
                           ToolSandbox has already resolved it from config.
        approvals:         Operator-managed allow-list, required for
                           PROMPT mode to make forward progress.
    """

    def __init__(
        self,
        *,
        vault_root: Path,
        max_output_bytes: int = 65_536,
        install_mode=None,
        approvals=None,
    ) -> None:
        self.vault_root = Path(vault_root)
        self.max_output = max_output_bytes
        # Imported lazily so backends remain optional consumers of the installer.
        from systemu.runtime.dependency_installer import InstallMode
        self._install_mode = install_mode or InstallMode.PROMPT
        self._approvals    = approvals

    async def execute(
        self,
        impl_path: Path,
        params_json: str,
        *,
        timeout: int,
        extra_packages: Optional[List[str]] = None,
    ):
        """Run *impl_path* with stripped env.

        When ``extra_packages`` is non-empty, the dependency installer runs
        before the subprocess so the tool's pip-declared deps are present
        in ``sys.executable``.  An install failure short-circuits the
        subprocess and returns a structured :class:`ToolResult`.
        """
        # Import lazily to avoid circular import (sandbox imports backend).
        from systemu.runtime.tool_sandbox import ToolResult, _parse_execution_stdout

        if extra_packages:
            from systemu.runtime.dependency_installer import (
                InstallStatus,
                ensure_satisfied,
            )
            tool_name = impl_path.stem
            install_result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ensure_satisfied(
                    extra_packages,
                    mode=self._install_mode,
                    approvals=self._approvals,
                    tool_name=tool_name,
                ),
            )
            if not install_result.ok:
                # Convert installer outcome into a ToolResult with the same
                # structured ``parsed`` shape the registry path produces, so
                # the shadow runtime can treat the two backends uniformly.
                error_type = {
                    InstallStatus.BLOCKED_DISABLED:         "dependency_install_blocked",
                    InstallStatus.BLOCKED_PENDING_APPROVAL: "dependency_install_pending_approval",
                    InstallStatus.FAILED:                   "dependency_install_failed",
                }.get(install_result.status, "dependency_install_failed")
                logger.info(
                    "[LocalBackend] Dep install for '%s' did not proceed: %s (%s)",
                    tool_name, install_result.status.value, install_result.error,
                )
                return ToolResult(
                    success=False,
                    error=install_result.error or "dependency install did not proceed",
                    parsed={
                        "success":          False,
                        "error":            install_result.error or "dependency install did not proceed",
                        "error_type":       error_type,
                        "missing_packages": list(extra_packages),
                        "pip_stderr_tail":  install_result.pip_stderr_tail,
                    },
                    exit_code=-1,
                )

        cmd = [sys.executable, str(impl_path), "--params", params_json]
        # BUG-4 fix: run the child via a thread + blocking subprocess.run rather
        # than asyncio.create_subprocess_exec. The asyncio child-watcher is
        # NOT implemented on the Windows SelectorEventLoop (which NiceGUI/uvicorn
        # may serve the dashboard on) — it raised NotImplementedError that this
        # block swallowed as a silent empty-error failure. Once W2.2 routed
        # forged tools through this path, every forged-tool call on the
        # dashboard loop failed silently → "task stuck after activities".
        # subprocess.run in a worker thread is loop- and platform-agnostic.
        import subprocess

        def _run_sync():
            return subprocess.run(
                cmd,
                capture_output=True,
                cwd=str(self.vault_root),
                env=self._build_restricted_env(),
                timeout=timeout,
            )

        try:
            completed = await asyncio.to_thread(_run_sync)
            stdout    = completed.stdout.decode(errors="replace")[: self.max_output]
            stderr    = completed.stderr.decode(errors="replace")[: self.max_output]
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            logger.warning("[LocalBackend] Subprocess timed out")
            return ToolResult(
                success=False, error="Subprocess execution timed out",
                timed_out=True, exit_code=-1,
            )
        except Exception as exc:
            logger.error("[LocalBackend] Subprocess failed: %s", exc)
            return ToolResult(success=False, error=str(exc))

        success, parsed, parse_error = _parse_execution_stdout(
            stdout, exit_code, impl_path.name,
        )
        return ToolResult(
            success=success, stdout=stdout, stderr=stderr, parsed=parsed,
            exit_code=exit_code,
            error=parse_error or (parsed.get("error") if not success else None),
        )

    def _build_restricted_env(self) -> dict:
        env = {}
        for key in _SAFE_ENV_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
        return env
