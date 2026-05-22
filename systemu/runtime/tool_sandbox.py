"""ToolSandbox — tool execution with ToolRegistry fast path + subprocess fallback.

Primary path  (ToolRegistry): direct Python function call via importlib, async-
              safe via ThreadPoolExecutor. No subprocess overhead (~0 ms vs 100-500 ms).
              Enforces Gate 3 (tool.enabled) before every call.

Fallback path (subprocess):   used when no registry is attached (dry-run / Docker
              mode). Each tool script accepts --params <JSON> on stdin, prints a
              JSON result dict, exits 0/1.

Security:
  - ToolRegistry raises ToolNotEnabledError → returned as a failed ToolResult
  - Subprocess path strips sensitive env vars (whitelist only)
  - A configurable timeout prevents runaway tools
  - stderr is captured and surfaced for debugging
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from systemu.runtime.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

# NOTE: The whitelist of env vars allowed to cross the trust boundary into
# tool subprocesses (_SAFE_ENV_KEYS) lives in systemu/runtime/backend/local.py
# now that the backend is pluggable.  See ARCHITECTURE.md for the model.


@dataclass
class ToolResult:
    """Result of a single tool execution."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    parsed: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    timed_out: bool = False
    exit_code: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr[:1000] if self.stderr else "",  # cap stderr for context
            "parsed": self.parsed,
            "error": self.error,
            "timed_out": self.timed_out,
            "exit_code": self.exit_code,
        }

def _parse_execution_stdout(stdout: str, exit_code: int, impl_name: str) -> (bool, Dict, Optional[str]):
    """Helper to parse stdout JSON from a tool execution."""
    parsed: Dict[str, Any] = {}
    parse_error: Optional[str] = None
    if stdout.strip():
        try:
            # Simple assumption: Last line or overall is JSON.
            # Some tools might print logs, we only parse the last line if so.
            lines = stdout.strip().split("\n")
            parsed = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            parse_error = f"stdout was not valid JSON: {exc} — raw: {stdout[-200:]}"
            logger.warning("[Sandbox] %s: %s", impl_name, parse_error)

    success = (exit_code == 0) and parse_error is None and parsed.get("success", True)
    return success, parsed, parse_error


class ToolSandbox:
    """Executes tool implementation scripts via a pluggable backend.

    Args:
        vault_root:       Root path of the vault (used to resolve implementation paths).
        default_timeout:  Default timeout in seconds per tool call.
        max_output_bytes: Maximum stdout/stderr bytes to capture.
        backend:          Explicit backend name (``local`` | ``docker`` | ``ssh`` | ``wsl``).
                          When omitted, falls back to ``resolve_backend_name()``
                          which reads ``SYSTEMU_TOOL_BACKEND`` from the env.
        registry:         Optional ToolRegistry for the in-process fast path.

    The legacy ``use_docker`` kwarg was removed in v0.3 — pass
    ``backend="docker"`` instead.
    """

    def __init__(
        self,
        vault_root: str | Path,
        *,
        default_timeout: int = 30,  # [A.1] 30s intentional — per-call overridable via execute_tool(timeout=)
        max_output_bytes: int = 65_536,  # 64 KB
        backend: Optional[str] = None,
        registry: Optional["ToolRegistry"] = None,
        install_mode=None,
        approvals=None,
    ):
        self.vault_root    = Path(vault_root)
        self.timeout       = default_timeout
        self.max_output    = max_output_bytes
        self._registry     = registry   # ToolRegistry for the fast direct-call path
        self._install_mode = install_mode
        self._approvals    = approvals

        # Resolve the canonical backend name from env or explicit kwarg.
        from systemu.runtime.backend import (
            create_backend,
            resolve_backend_name,
        )
        backend_name = resolve_backend_name(explicit=backend)
        self.backend_name  = backend_name
        self._backend = create_backend(
            backend_name,
            vault_root=self.vault_root,
            max_output_bytes=self.max_output,
            install_mode=install_mode,
            approvals=approvals,
        )

    def attach_registry(self, registry: "ToolRegistry") -> None:
        """Attach a ToolRegistry so execute_tool() uses the direct-call fast path."""
        self._registry = registry

    # ─────────────────────────────────────────────────────────────────────────

    async def execute_tool(
        self,
        implementation_path: str,
        parameters: Dict[str, Any],
        *,
        timeout: Optional[int] = None,
        extra_packages: Optional[List[str]] = None,
    ) -> ToolResult:
        """Execute a tool implementation script with the given parameters.

        Args:
            implementation_path: Path to the .py file, relative to vault root's parent.
            parameters:          Dict of tool parameters (will be JSON-serialised).
            timeout:             Optional per-call timeout override.
            extra_packages:      pip packages to install inside the Docker container
                                 before running (Docker mode only; ignored otherwise).

        Returns:
            ToolResult with parsed stdout JSON and metadata.
        """
        impl_path = Path(implementation_path)
        if not impl_path.is_absolute():
            impl_path = self.vault_root.parent / implementation_path

        # Always resolve to an absolute path so subprocess sees the real on-disk
        # location regardless of what CWD is set to on the child process.
        impl_path = impl_path.resolve()

        effective_timeout = timeout or self.timeout
        tool_name = impl_path.stem   # derive name from filename, e.g. "browser_navigate"

        # ── Fast path: ToolRegistry (direct Python function call) ─────────
        if self._registry is not None and impl_path.exists():
            try:
                from systemu.runtime.tool_registry import ToolDependencyError, ToolNotEnabledError
                result_dict = await self._registry.execute(
                    tool_name, parameters, timeout=float(effective_timeout)
                )
                success = result_dict.get("success", True)
                return ToolResult(
                    success=success,
                    parsed=result_dict,
                    error=result_dict.get("error") if not success else None,
                )
            except ToolNotEnabledError as exc:
                # Gate 3 violation — do NOT fall back to subprocess
                logger.warning("[Sandbox] Gate 3 blocked '%s': %s", tool_name, exc)
                return ToolResult(success=False, error=str(exc))
            except ToolDependencyError as exc:
                # Missing/broken import — subprocess would fail identically; skip it.
                # The registry already returns a structured result dict for this case
                # (caught inside execute()), so this branch only fires if the exception
                # somehow escapes — convert it cleanly here as well.
                from systemu.runtime.tool_registry import _dep_error_dict
                rd = _dep_error_dict(exc)
                logger.warning("[Sandbox] Dep error for '%s': %s", tool_name, exc.missing)
                return ToolResult(success=False, parsed=rd, error=rd["error"])
            except Exception as exc:
                logger.warning(
                    "[Sandbox] Registry fast-path failed for '%s' (%s) — falling back to subprocess",
                    tool_name, exc,
                )
                # Fall through to subprocess path below

        # ── Subprocess fallback (no registry, dry-run, or Docker mode) ────
        if not impl_path.exists():
            return ToolResult(
                success=False,
                error=f"Implementation not found: {impl_path}",
            )

        params_json = json.dumps(parameters)

        logger.debug(
            "[Sandbox] Backend %s executing %s with params=%s (timeout=%ds)",
            self.backend_name, impl_path.name, params_json[:120], effective_timeout,
        )

        # Delegate to the configured backend.  All transports share the
        # same protocol: see systemu/runtime/backend/protocol.py.
        return await self._backend.execute(
            impl_path,
            params_json,
            timeout=effective_timeout,
            extra_packages=extra_packages or [],
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def is_destructive_call(tool_name: str, parameters: Dict[str, Any]) -> bool:
        """Heuristic check: is this tool call potentially destructive?

        Errs on the side of caution — false positives are far safer than
        false negatives for destructive actions.
        """
        name_lower = tool_name.lower()
        destructive_hints = {
            "delete", "remove", "drop", "truncate", "wipe", "purge",
            "overwrite", "send", "publish", "deploy", "purchase", "pay",
            "transfer", "execute_sql", "run_command", "shell",
        }
        if any(hint in name_lower for hint in destructive_hints):
            return True

        # Check parameter values for dangerous patterns
        params_str = json.dumps(parameters).lower()
        if any(p in params_str for p in ["rm -rf", "drop table", "delete from", "--force"]):
            return True

        return False
