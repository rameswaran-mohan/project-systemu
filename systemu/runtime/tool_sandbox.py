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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from systemu.runtime.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

# NOTE: The whitelist of env vars allowed to cross the trust boundary into
# tool subprocesses (_SAFE_ENV_KEYS) lives in systemu/runtime/backend/local.py
# now that the backend is pluggable.  See ARCHITECTURE.md for the model.

_SUBPROCESS_ONLY_DEPS = {"playwright", "playwright-stealth", "selenium"}

# ── v0.9.7: output-path robustness ───────────────────────────────────────────
# LLMs sometimes mangle a long absolute output path (e.g. drop a path segment),
# sending a deliverable into a sibling tree the verifier never scans. We redirect
# such writes back into the configured output_dir so the deliverable always lands
# where it's expected — and reads are left alone unless their parent is missing.
_PATH_PARAM_KEYS = (
    "path", "output_path", "file_path", "filepath", "out_path", "output",
    "save_path", "dest", "destination", "target_path", "write_path", "output_file",
)
_WRITE_NAME_TOKENS = (
    "write", "save", "create", "export", "dump", "append", "output", "generate", "render",
)

# ── W12 (audit F5): command-level shell-safety classification ────────────────
# Tools that execute arbitrary shell commands — judged by the COMMAND they
# carry, not by their name (see ToolSandbox.is_destructive_call).
_SHELL_TOOL_NAMES = {"run_command", "run_cli_command"}


def _inject_sandbox_kwargs(handler, params: Dict[str, Any], output_dir: str) -> Dict[str, Any]:
    """v0.9.33 (A): add _root / output_dir to a v2 handler's kwargs so file
    tools write into the run workspace, but only for params the handler
    actually accepts (declared param or **kwargs catch-all) — otherwise a
    handler with a fixed signature would raise TypeError.

    NOTE: _normalize_output_paths (called in execute() before this) already
    absolutizes write path-params INTO output_dir, so this _root=output_dir
    injection is intentionally redundant (defense-in-depth): output_dir is the
    intended jail root, and a handler that resolves a relative path against
    _root then lands in the same place the normalizer already steered it.
    """
    import inspect
    if not output_dir:
        return params
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return params
    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )
    out = dict(params)
    for key in ("_root", "output_dir"):
        if key in out:
            continue
        if accepts_var_kw or key in sig.parameters:
            out[key] = output_dir
    return out

# v0.9.32 (D.4 review FIX-4): the destructive ``--force`` flag, matched on a
# token boundary so it does NOT over-match longer flags that merely START with
# it. ``(?![-\w])`` rejects a trailing hyphen or word char, so
# ``--force-with-lease`` (a SAFE git flag) is not flagged, while standalone
# ``--force`` (and ``--force=...``) still is. Operates on json.dumps(params),
# which lowercases to the same text the substring denylist scans.
_FORCE_FLAG_RE = re.compile(r"--force(?![-\w])")

# Programs that only READ system/file state. Deliberately tight: anything
# not provably read-only keeps the safety gate.
_READONLY_PROGRAMS = {
    # Windows
    "ver", "dir", "type", "systeminfo", "ipconfig", "whoami", "where",
    "hostname", "tasklist", "date", "time",
    # POSIX
    "ls", "cat", "pwd", "uname", "df", "du", "ps", "head", "tail", "wc",
    "which", "env",
}
_READONLY_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "remote", "describe", "blame",
}
# Chaining/redirection makes ANY command non-read-only: `>` writes, `&&`/
# `|`/`;` smuggle a second command, backticks/`$()` substitute.
_SHELL_METACHARS = set("&|;><`$\r\n")


def is_readonly_shell_command(command: str) -> bool:
    """True only when *command* is PROVABLY read-only.

    A single command (no shell metacharacters) whose program is on the
    read-only allowlist — `ver`, `dir`, `git status`, `python --version`…
    False means "keep the safety gate", never "is destructive".
    """
    cmd = (command or "").strip()
    if not cmd or any(c in _SHELL_METACHARS for c in cmd):
        return False
    tokens = cmd.split()
    prog = tokens[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if prog.endswith(".exe"):
        prog = prog[:-4]
    if prog == "git":
        return len(tokens) >= 2 and tokens[1].lower() in _READONLY_GIT_SUBCOMMANDS
    if prog in ("python", "python3"):
        return tokens[1:2] in (["--version"], ["-V"])
    if prog in ("pip", "pip3"):
        return len(tokens) >= 2 and tokens[1].lower() in ("list", "show", "freeze", "--version")
    return prog in _READONLY_PROGRAMS


def _is_write_ish(name: str, params: Dict[str, Any]) -> bool:
    """Heuristic: does this tool call write/produce a file?"""
    n = (name or "").lower()
    if any(tok in n for tok in _WRITE_NAME_TOKENS):
        return True
    try:
        return ToolSandbox.is_destructive_call(name, params)
    except Exception:
        return False


def _normalize_output_paths(name: str, params: Any, output_dir: Optional[str]) -> Any:
    """Redirect path-like params into ``output_dir`` to survive LLM path slips.

    A path param is redirected to ``output_dir/<basename>`` when EITHER:
      * its parent directory does not exist (mangled-path signature — safe for
        reads, which would fail on a missing parent anyway), OR
      * the tool is write-ish AND the path resolves outside ``output_dir``
        (enforces the deliverables-stay-in-output-dir contract).

    Reads to existing locations outside output_dir are left untouched.
    Returns a (possibly new) params dict; never raises.
    """
    if not isinstance(params, dict) or not output_dir:
        return params
    try:
        out = Path(output_dir).resolve()
    except Exception:
        return params
    write_ish = _is_write_ish(name, params)
    new_params = None
    for k, v in params.items():
        if k not in _PATH_PARAM_KEYS or not isinstance(v, str) or not v.strip():
            continue
        try:
            p = Path(v)
            rp = p.resolve()
            parent_missing = not p.parent.exists()
            try:
                outside = not rp.is_relative_to(out)
            except AttributeError:  # py<3.9 fallback
                outside = out not in rp.parents and rp.parent != out
        except Exception:
            continue
        if parent_missing or (write_ish and outside):
            redirected = str(out / p.name)
            if redirected != str(rp):
                if new_params is None:
                    new_params = dict(params)
                new_params[k] = redirected
                logger.warning(
                    "[Sandbox] %s: redirected path %r -> %r (parent_missing=%s, write_outside=%s)",
                    name, v, redirected, parent_missing, write_ish and outside,
                )
    return new_params if new_params is not None else params


def _build_empty_config():
    """Return a default Config instance (used as fallback when self._config is None)."""
    from sharing_on.config import Config
    return Config()


def requires_subprocess_isolation(tool) -> bool:
    """W2.2 isolation policy: LLM-forged code runs OUT-OF-PROCESS unless the
    operator explicitly trusted it (``trusted_inprocess``).

    The in-process fast path is a direct importlib call inside the daemon —
    full process privileges (vault, env vars, network). That is fine for
    built-in tools (repo code), but generated code only earns it explicitly.
    ``None`` (no Tool context) isolates defensively — we can't prove it's a
    built-in.
    """
    if tool is None:
        return True
    return bool(getattr(tool, "forged_by_systemu", False)) and \
        not bool(getattr(tool, "trusted_inprocess", False))


def _must_use_subprocess(tool_type, dependencies) -> bool:
    """Playwright/Selenium use a sync API that cannot run inside the asyncio
    loop the in-process fast path uses, so such tools must run in a fresh
    subprocess (the dry-run path already does this)."""
    if str(tool_type or "") == "browser_action":
        return True
    deps = {str(d).lower() for d in (dependencies or [])}
    return bool(deps & _SUBPROCESS_ONLY_DEPS)


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
    elif exit_code == 0:
        # W6.2 truth-in-results: exit 0 with NO output must not read as
        # success — that exact shape (module-style tool run as a script) was
        # reported as `success: true, parsed: {}` for every vault tool, which
        # both lied to the LLM and disarmed the stuck-loop governor's
        # same-tool-failure trigger (it only counts success=False calls).
        parse_error = (f"tool produced no output on stdout (exit 0) — "
                       f"expected a JSON result from {impl_name}")
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
        vault_root: str | Path | None = None,
        *,
        default_timeout: int = 30,  # [A.1] 30s intentional — per-call overridable via execute_tool(timeout=)
        max_output_bytes: int = 65_536,  # 64 KB
        backend: Optional[str] = None,
        registry: Optional["ToolRegistry"] = None,
        install_mode=None,
        approvals=None,
        vault=None,
        config=None,
        command_approvals=None,   # v0.9.32 D.4: per-command operator gate store
    ):
        # v0.9.3: vault_root is now optional — fall back to vault.root when available.
        if vault_root is None and vault is not None:
            vault_root = getattr(vault, "root", None)
        if vault_root is None:
            vault_root = Path(".")
        # Wave 1.3: anchor at construction.  vault_root is often relative
        # (config default "systemu/vault", or the bare "." fallback above);
        # implementation paths resolve against vault_root.parent at EXECUTION
        # time, so an unresolved root floated with whatever CWD the daemon /
        # worker happened to have — tools forged from the project root broke
        # when run from anywhere else.
        self.vault_root    = Path(vault_root).resolve()
        self.timeout       = default_timeout
        self.max_output    = max_output_bytes
        self._registry     = registry   # ToolRegistry for the fast direct-call path
        self._install_mode = install_mode
        self._approvals    = approvals
        self._vault        = vault      # v0.9.1: for action-tool audit writes
        self._config       = config     # v0.9.1: for audit_log_enabled check
        # v0.9.32 D.4: CommandApprovalStore for the per-command approval gate.
        # When None the daemon's process-wide singleton is consulted lazily at
        # gate time (init_default_store("data")), so callers that don't thread
        # it still gate correctly.
        self._command_approvals = command_approvals

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

    def _after_successful_call(
        self,
        *,
        tool,
        params,
        execution_id,
        objective_id,
        user_id=None,
    ):
        """v0.9.1: action-tool audit hook. Called from the post-call success
        path after a tool returns ToolResult(success=True). No-op for
        non-action tools or when audit_log_enabled is False.
        """
        if not getattr(tool, "is_action_tool", False):
            return
        cfg = self._config
        if cfg is not None and not getattr(cfg, "audit_log_enabled", True):
            return
        vault = self._vault
        if vault is None:
            return  # no vault → can't audit; degrade silently

        from systemu.runtime import audit_log
        try:
            audit_log.append_action(
                vault,
                execution_id=execution_id,
                objective_id=objective_id,
                action=tool.name,
                params=params or {},
                success=True,
                error=None,
                user_id=user_id,
            )
        except Exception as exc:  # pragma: no cover — best-effort
            import logging
            logging.getLogger(__name__).warning(
                "[ToolSandbox] action audit write failed for %s: %s",
                tool.name, exc,
            )

    def _record_capability_outcome(
        self,
        *,
        tool,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """v0.9.3 hook: every tool invocation (success or failure) bumps
        the capability ledger. Gated by config.capability_track_outcomes.
        Best-effort — failures degrade silently."""
        cfg = self._config
        if cfg is not None and not getattr(cfg, "capability_track_outcomes", True):
            return
        vault = self._vault
        if vault is None:
            return
        try:
            from systemu.runtime import capability_ledger
            capability_ledger.record_invocation(
                vault, tool.name, success=success, error=error, kind="tool",
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "[ToolSandbox] capability ledger write failed for %s: %s",
                getattr(tool, "name", "?"), exc,
            )

    def _record_capability_outcome_by_name(
        self,
        *,
        name: str,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """v0.9.3 v2-dispatch hook: record by name (no Tool object available
        in the v2 path). Same gating + best-effort as _record_capability_outcome."""
        cfg = self._config
        if cfg is not None and not getattr(cfg, "capability_track_outcomes", True):
            return
        vault = self._vault
        if vault is None:
            return
        try:
            from systemu.runtime import capability_ledger
            capability_ledger.record_invocation(
                vault, name, success=success, error=error, kind="tool",
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "[ToolSandbox] v2 capability ledger write failed for %s: %s",
                name, exc,
            )

    async def execute(
        self,
        name: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """v0.9.3: High-level tool dispatch by name.

        Consults the v2 (code-registered) registry FIRST; if the tool is
        registered there AND its check_fn passes, the v2 handler is called
        directly (sync handlers are run in a thread executor so the async
        loop is not blocked).

        Falls back to the v1 (vault-based) path for any tool that is not in
        the v2 registry or whose check_fn reports unavailable.

        v1 fallback: with vault=None the v1 path returns
        ``{"success": False, "error": "..."}`` for any unknown tool name.
        """
        # ── v0.9.7: keep deliverables in output_dir despite LLM path slips ──
        _od = (getattr(self._config, "output_dir", "") if self._config else "") \
            or str(self.vault_root / "output")
        params = _normalize_output_paths(name, params, _od)

        # v0.9.34 (P0 HIGH-1 follow-up): PendingOperatorDecision must be IN SCOPE
        # here. It is imported locally inside execute_tool / _maybe_gate_command,
        # NOT at module level — so without this import, evaluating the
        # `except PendingOperatorDecision:` clause below on ANY handler exception
        # throws NameError, which the outer `except Exception` then catches and
        # diverts the call to the v1 path ("not found"). Import before the try.
        from systemu.approval.exceptions import PendingOperatorDecision

        # ── v2 registry (code-registered tools) ──────────────────────────
        try:
            from systemu.runtime.tool_registry_v2 import registry as _v2_registry
            entry = _v2_registry.get(name)
            if entry is not None and _v2_registry.available(
                name, self._config or _build_empty_config()
            ):
                import asyncio as _asyncio
                loop = _asyncio.get_running_loop()
                # v0.9.33 (A): file_tools (and any sandbox-aware handler)
                # accept _root/output_dir to keep writes inside the run
                # workspace. _od is the resolved output dir computed above for
                # _normalize_output_paths. Inject ONLY the kwargs the handler
                # declares (or accepts via **kw) so handlers without those
                # params don't raise TypeError.
                _eff_params = _inject_sandbox_kwargs(entry.handler, dict(params or {}), _od)
                try:
                    if entry.is_async:
                        result = await entry.handler(**_eff_params)
                    else:
                        result = await loop.run_in_executor(
                            None, lambda: entry.handler(**_eff_params)
                        )
                except PendingOperatorDecision:
                    # v0.9.34 (P0 review HIGH-1): an action/destructive MCP tool
                    # (or any v2 handler) may raise PendingOperatorDecision via
                    # the gated chokepoint. It MUST propagate to the decision-
                    # queue resume handlers (worker/supervisor/scheduler park +
                    # resume) — EXACTLY as the v1 execute_tool path does. Swallowing
                    # it into a failure dict would orphan the Inbox decision and
                    # silently continue the run with a denied tool (no park/resume).
                    raise
                except Exception as exc:
                    # Capability ledger: record failure too (best-effort).
                    try:
                        self._record_capability_outcome_by_name(
                            name=name, success=False, error=str(exc),
                        )
                    except Exception:
                        pass
                    return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
                # Capability ledger record (best-effort).
                try:
                    self._record_capability_outcome_by_name(
                        name=name,
                        success=bool(result.get("success", True))
                        if isinstance(result, dict) else True,
                        error=None,
                    )
                except Exception:
                    pass
                # If the v2 handler returned a dict, return it as-is.
                # Otherwise wrap as {success: True, value: result}.
                if isinstance(result, dict):
                    return result
                return {"success": True, "value": result}
        except PendingOperatorDecision:
            # v0.9.34 (P0 HIGH-1): the inner handler re-raises a parked decision;
            # this OUTER try must let it propagate too (PendingOperatorDecision IS
            # an Exception, so the blanket `except Exception` below would otherwise
            # re-catch it and divert the parked call to the v1 path). Re-raise so
            # the worker/supervisor/scheduler park+resume handlers receive it.
            raise
        except Exception as exc:
            logger.debug(
                "[ToolSandbox] v2 dispatch raised, falling back to v1 for %s: %s",
                name, exc,
            )
        # ── fall through to v1 vault-based dispatch ───────────────────────
        # The v1 path operates on implementation_path + vault. Without a vault
        # or a known implementation path we return a structured failure to
        # match the API contract expected by callers.
        vault = self._vault
        if vault is None:
            return {"success": False, "error": f"Tool not found: {name!r} (not in v2 registry and no vault attached)"}
        # Try to find the tool in the vault and delegate to execute_tool().
        try:
            tool = vault.find_tool_by_name(name)
        except Exception:
            tool = None
        if tool is None:
            return {"success": False, "error": f"Tool not found: {name!r}"}
        impl_path = getattr(tool, "implementation_path", None) or getattr(tool, "impl_path", None)
        if not impl_path:
            return {"success": False, "error": f"Tool {name!r} has no implementation path"}
        tr = await self.execute_tool(
            str(impl_path),
            params or {},
            timeout=timeout,
            tool_type=getattr(tool, "tool_type", None),
            force_subprocess=requires_subprocess_isolation(tool),   # W2.2
        )
        return tr.to_dict()

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
        tool_type: Optional[str] = None,
        force_subprocess: bool = False,
        _command_gate_resolved: Optional[str] = None,
    ) -> ToolResult:
        """Execute a tool implementation script with the given parameters.

        Args:
            implementation_path: Path to the .py file, relative to vault root's parent.
            parameters:          Dict of tool parameters (will be JSON-serialised).
            timeout:             Optional per-call timeout override.
            extra_packages:      pip packages to install inside the Docker container
                                 before running (Docker mode only; ignored otherwise).
            tool_type:           Tool category (e.g. "browser_action"). Playwright/
                                 Selenium tools are forced through the subprocess path
                                 (their sync API cannot run in the in-process async
                                 fast path). See ``_must_use_subprocess``.

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

        # ── v0.9.8: keep deliverables in output_dir despite LLM path slips ──
        # The v0.9.7 redirect lived ONLY in execute(); the agentic loop dispatches
        # tools via execute_tool(), so without this every file_write to "/tmp/x.txt"
        # or a bare filename escaped output_dir → the verifier (which scans
        # output_dir) never saw the deliverable → the run looped to MAX_ITERATIONS.
        # Idempotent: a path already inside output_dir is left untouched.
        try:
            _od = (getattr(self._config, "output_dir", "") if self._config else "") \
                or str(self.vault_root / "output")
            parameters = _normalize_output_paths(tool_name, parameters, _od)
        except Exception:
            logger.debug("[Sandbox] execute_tool output-path normalise failed", exc_info=True)

        # ── v0.9.32 (D.4): per-command operator approval gate ─────────────
        # The single chokepoint for both shell tools (run_command,
        # run_cli_command) and both lanes. A destructive, non-allowlisted
        # command posts a "command" floor gate and raises
        # PendingOperatorDecision (caught + parked by the workflow lane;
        # block-polled by the chat lane). Provably read-only commands and
        # already-approved signatures fall through and run.
        self._maybe_gate_command(tool_name, parameters,
                                 resolved_dedup=_command_gate_resolved)

        # ── Fast path: ToolRegistry (direct Python function call) ─────────
        # Browser/Playwright tools MUST go through the subprocess path — their
        # sync API cannot run inside the in-process async fast path (v0.8.15).
        # W2.2: callers with Tool context pass force_subprocess=True for
        # forged-and-untrusted tools (requires_subprocess_isolation) — the
        # in-process fast path is full-daemon-privilege and generated code
        # only earns it via the operator's explicit trusted_inprocess.
        if (self._registry is not None and impl_path.exists()
                and not force_subprocess
                and not _must_use_subprocess(tool_type, extra_packages)):
            try:
                from systemu.runtime.tool_registry import ToolDependencyError, ToolNotEnabledError
                from systemu.approval.exceptions import PendingOperatorDecision
                # v0.9.1.1 fix: pass the raw caller-side `timeout` (None when
                # no explicit override was given) so _resolve_timeout in the
                # registry can prefer tool.timeout_seconds and
                # config.tool_default_timeout_seconds over the hardcoded 30s
                # sandbox default.  effective_timeout is still used for the
                # subprocess fallback path below.
                result_dict = await self._registry.execute(
                    tool_name, parameters, timeout=float(timeout) if timeout is not None else None
                )
                success = result_dict.get("success", True)
                # v0.9.1: truncate_result is called here by callers that have
                # the Tool object in scope (e.g. shadow_runtime in T12).
                # execute_tool only has the impl path, not the Tool model.
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
            except PendingOperatorDecision:
                # v0.8.18 Gate-4: an interactive credential ask (or any operator
                # decision) was raised mid-call.  This is NOT a fast-path failure —
                # it MUST propagate to the decision-queue resume handlers
                # (scheduler/jobs.py, cli_commands.py).  Falling through to the
                # subprocess backend here would run the tool UN-GATED.
                raise
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

    def _maybe_gate_command(self, tool_name: str, parameters: Dict[str, Any],
                            *, resolved_dedup: Optional[str] = None) -> None:
        """v0.9.32 (D.4): raise PendingOperatorDecision for a destructive,
        non-allowlisted shell command. No-op for non-shell tools, provably
        read-only commands, or already-approved signatures.

        ``resolved_dedup`` is the chat-lane "Approve once" one-shot bypass
        (D.6): when it matches this command's dedup key AND the decision queue
        already holds a resolved non-Deny choice for that key, the gate is
        skipped for THIS single call without persisting the signature.

        Fail-closed posture: any failure to RESOLVE the approval store leaves
        the gate active (we still post + raise) — we never silently run an
        unapproved destructive command.
        """
        if tool_name not in _SHELL_TOOL_NAMES:
            return
        if not self.is_destructive_call(tool_name, parameters):
            return  # provably read-only → run without approval

        command = str(parameters.get("command") or parameters.get("cmd") or "")
        cwd = str(parameters.get("cwd") or "")

        from systemu.runtime.command_approvals import (
            command_signature, init_default_store)
        store = self._command_approvals
        if store is None:
            try:
                store = init_default_store(Path("data"))
            except Exception:
                store = None  # fail-closed below: no store → still gate
        sig = command_signature(command, cwd=cwd)
        dedup = f"command:{sig}"
        if store is not None and store.is_approved(sig):
            return  # "Always allow" on record → run

        # v0.9.52: one-shot RESUME approval. The command-gate resume path marks
        # this signature when it re-submits a parked task that the operator
        # approved; the resumed run honors it exactly ONCE here (consumed), so it
        # proceeds past the command instead of re-asking in a loop. A LATER,
        # unrelated command re-asks normally (the mark is single-use).
        if store is not None and store.consume_resume_approved(sig):
            return

        # Chat-lane "Approve once" one-shot bypass: the operator resolved THIS
        # exact decision with a non-Deny choice; honor it once without
        # persisting (the re-attempt threads resolved_dedup=dedup).
        #
        # v0.9.32 (D.4 review FIX-2): CONSUME the decision when we honor it, so
        # the "Approve once" is genuinely SINGLE-USE. consume_resolved_choice
        # flips the resolved decision to status="consumed" — a LATER identical
        # command (same dedup_key) then finds NO resolved choice and RE-ASKS,
        # rather than replaying a stale one-shot and auto-running (fail-OPEN).
        # "Always allow" never reaches here (handled by store.is_approved above).
        if resolved_dedup and resolved_dedup == dedup:
            try:
                from systemu.approval.decision_queue import OperatorDecisionQueue
                choice = OperatorDecisionQueue(self._vault).consume_resolved_choice(dedup)
                if choice is not None and (choice or "").strip().lower() != "deny":
                    return
            except Exception:
                logger.debug("[Sandbox] Approve-once bypass check failed; "
                             "falling through to re-gate (fail-closed)",
                             exc_info=True)

        # Not approved → post the command gate and raise. The decision is
        # posted exactly once (dedup command:<sig> short-circuits dupes); the
        # lanes differ only in how they WAIT (D.5 park / D.6 block-poll).
        from systemu.approval.exceptions import PendingOperatorDecision
        from systemu.interface.command.gate import GateDescriptor
        from systemu.interface.command.inbox import InboxQueue

        descriptor = GateDescriptor.from_command(
            tool_name=tool_name, command=command, cwd=cwd)
        # v0.9.52: stamp the run's resume coords (from the contextvar carriers) so a
        # PARKED command gate is resumable — resume_on_decision re-submits the
        # activity with resume_from_execution_id and derives activity/shadow from the
        # snapshot. Without these the parked chat task hangs forever on resolution.
        _resume_extras = {"command": command, "cwd": cwd}
        try:
            from systemu.runtime.chat_submission_ctx import (
                current_chat_submission_id, current_execution_id)
            _exec_id = current_execution_id()
            _chat_sub = current_chat_submission_id()
            if _exec_id:
                _resume_extras["execution_id"] = _exec_id
            if _chat_sub:
                _resume_extras["chat_submission_id"] = _chat_sub
        except Exception:
            logger.debug("[Sandbox] could not read run coords for command gate", exc_info=True)
        dec_id = InboxQueue(self._vault).enqueue(
            descriptor,
            gate_type="command",
            policy=None,                  # floor gate — never auto-allow
            context_extras=_resume_extras,
        )
        raise PendingOperatorDecision(
            decision_id=dec_id,
            dedup_key=descriptor.dedup,
            options=descriptor.options,
            message=(f"Operator approval required to run `{command}`. "
                     "Open the dashboard Inbox and choose Deny / Approve once "
                     "/ Always allow."),
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def is_destructive_call(tool_name: str, parameters: Dict[str, Any]) -> bool:
        """Heuristic check: is this tool call potentially destructive?

        Errs on the side of caution — false positives are far safer than
        false negatives for destructive actions.

        W12 (audit F5): shell tools are judged by their COMMAND, not their
        name. The old name-only rule marked EVERY run_command destructive,
        so a read-only ``ver`` was auto-denied in every non-interactive
        context — which includes the daemon the dashboard runs in. Provably
        read-only commands pass; everything ambiguous keeps the gate.
        """
        name_lower = tool_name.lower()

        # Dangerous parameter patterns gate REGARDLESS of anything else.
        params_str = json.dumps(parameters).lower()
        # Plain substrings stay literal (rm -rf / drop table / delete from);
        # ``--force`` uses a token-boundary regex (D.4 review FIX-4) so
        # ``--force-with-lease`` is not flagged solely by the substring while
        # standalone ``--force`` still is.
        if any(p in params_str for p in ["rm -rf", "drop table", "delete from"]):
            return True
        if _FORCE_FLAG_RE.search(params_str):
            return True

        if name_lower in _SHELL_TOOL_NAMES:
            cmd = str(parameters.get("command") or parameters.get("cmd") or "")
            return not is_readonly_shell_command(cmd)

        destructive_hints = {
            "delete", "remove", "drop", "truncate", "wipe", "purge",
            "overwrite", "send", "publish", "deploy", "purchase", "pay",
            "transfer", "execute_sql", "shell",
        }
        if any(hint in name_lower for hint in destructive_hints):
            return True

        return False


def truncate_result(result: "ToolResult", tool) -> "ToolResult":
    """Cap the ToolResult's stdout to ``tool.max_result_size_chars``.

    Returns the same ToolResult (mutated in-place) when truncation fires,
    with a "[... truncated by tool.max_result_size_chars=N]" marker appended.
    None / 0 cap = passthrough (no truncation).

    Works with the actual ToolResult dataclass which stores stdout as a direct
    str field (``result.stdout``), not nested in an ``output`` dict.
    """
    cap = getattr(tool, "max_result_size_chars", None)
    if not cap or cap <= 0:
        return result
    stdout = result.stdout or ""
    if len(stdout) <= cap:
        return result
    result.stdout = stdout[:cap] + f"\n[... truncated by tool.max_result_size_chars={cap}]"
    return result
