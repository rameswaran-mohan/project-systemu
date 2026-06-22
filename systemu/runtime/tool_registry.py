"""ToolRegistry — dynamic importer and direct-call dispatcher for wrapper-function tools.

Each tool implementation is a Python module containing:
  TOOL_META = {"name": ..., "tool_type": ..., "dependencies": [...]}
  def run(**params) -> dict: ...

ToolRegistry imports each module on first use (cached), then calls run(**params)
in a ThreadPoolExecutor so the async ReAct loop is never blocked.

Gate 3 enforcement: execute() checks tool.enabled in the vault before every
invocation and raises ToolNotEnabledError if the user has not explicitly toggled
the tool ON in the Tools Registry page.

Self-heal for missing pip dependencies (v0.3.3+):
  When a tool module fails to import or its ``run()`` raises ``ImportError``,
  the registry consults the tool's vault manifest ``dependencies`` field and —
  subject to the operator-resolved ``InstallMode`` — attempts a single
  ``pip install`` via ``dependency_installer.ensure_satisfied()`` before
  retrying the load/call once.  Outcomes that cannot be auto-resolved (no
  manifest, no operator approval, install disabled, pip itself failed) are
  surfaced as distinct ``error_type``s so the Shadow runtime emits a precise
  event-log line and suppresses retries without learning the wrong lesson.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from systemu.core.models import Tool
    from systemu.runtime.dep_approvals import DepApprovalStore
    from systemu.runtime.dependency_installer import InstallMode
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

# Shared thread pool — tools run in threads, not the async event loop
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-")


# ─────────────────────────────────────────────────────────────────────────────

class ToolNotEnabledError(RuntimeError):
    """Raised when a Shadow tries to invoke a tool not yet enabled by the user.

    Resolution: go to the Tools Registry page in the dashboard and flip the
    toggle ON for this tool (Gate 3).
    """


class ToolDependencyError(RuntimeError):
    """Raised when a tool's Python module cannot be imported due to a missing
    (or broken) third-party package.

    Attributes:
        tool_name:   Name of the tool that failed to load.
        missing:     The import name that Python could not resolve (from exc.name).
        is_internal: True when the missing module starts with 'systemu' —
                     indicates a broken internal import rather than a missing pip
                     package.  The caller should report this as a bug, not a dep.
    """

    def __init__(self, tool_name: str, missing: str, *, is_internal: bool = False):
        self.tool_name   = tool_name
        self.missing     = missing
        self.is_internal = is_internal
        super().__init__(
            f"Tool '{tool_name}' failed to load: "
            + (f"broken internal import '{missing}'" if is_internal
               else f"missing Python package '{missing}'")
        )


def _dep_error_dict(exc: "ToolDependencyError") -> dict:
    """Convert a ToolDependencyError to the structured result dict the shadow sees."""
    if exc.is_internal:
        return {
            "success":    False,
            "error":      (f"Tool '{exc.tool_name}' has a broken internal import "
                           f"('{exc.missing}'). This is a systemu bug — report it."),
            "error_type": "internal_import_error",
        }
    return {
        "success":         False,
        "error":           (f"Tool '{exc.tool_name}' requires Python package "
                            f"'{exc.missing}' which is not installed. "
                            f"Do not retry — install the package first: "
                            f"pip install {exc.missing}"),
        "error_type":      "missing_dependency",
        "missing_packages": [exc.missing],
        "install_hint":    f"pip install {exc.missing}",
    }


def _credential_degraded(name: str, req) -> dict:
    """v0.8.18 — structured result when a required credential is unavailable.

    The Shadow runtime treats ``degraded`` results as a soft, non-retryable
    skip: the tool simply cannot run until the operator connects the secret
    on the Connections page.  Never contains the secret value.
    """
    return {
        "success": False, "degraded": True,
        "error": f"Tool '{name}' unavailable: missing {req.key}",
        "error_type": "tool_credential_missing",
        "note": (f"This tool needs {req.label}. "
                 f"Connect it at {req.signup_url or 'the Connections page (Settings -> Connections)'} to enable it."),
    }


def _install_blocked_dict(tool_name: str, packages: List[str], reason: str) -> dict:
    """Build the result dict for ``dependency_install_blocked`` (mode = OFF).

    The Shadow runtime suppresses retries and surfaces a distinct event-log
    line so the operator knows to bake the package into the base image or
    flip the install mode rather than treat this as a strategic dead-end.
    """
    return {
        "success":         False,
        "error":           reason,
        "error_type":      "dependency_install_blocked",
        "missing_packages": packages,
        "install_hint":    f"pip install {' '.join(packages)}",
    }


def _install_pending_dict(tool_name: str, packages: List[str], reason: str) -> dict:
    """Build the result dict for ``dependency_install_pending_approval``.

    Mode is PROMPT and at least one declared dep has not been approved by
    the operator yet.  ``record_pending`` has already noted the request in
    the approval store so the CLI / dashboard can show what needs action.
    """
    return {
        "success":         False,
        "error":           reason,
        "error_type":      "dependency_install_pending_approval",
        "missing_packages": packages,
        "install_hint":    (
            "Operator approval required: "
            + " && ".join(f"sharing_on tools deps approve {p}" for p in packages)
        ),
    }


def _maybe_enqueue_dep_gate(*, vault, tool_id: str, tool_name: str,
                            package: str, request_count: int = 1) -> None:
    """Best-effort: surface a pending package install as a dep gate in the
    unified Inbox (dedup dep:<package> → idempotent; OperatorDecisionQueue
    short-circuits a duplicate pending row).  Never raises into the tool run.

    ``InboxQueue``/``GateDescriptor`` are resolved from this module's globals so
    they remain monkeypatchable in tests; on first use they are lazy-imported
    and bound, keeping the import graph minimal at module-load time.
    """
    if not vault or not package:
        return
    try:
        inbox_cls = globals().get("InboxQueue")
        gate_cls  = globals().get("GateDescriptor")
        if inbox_cls is None:
            from systemu.interface.command.inbox import InboxQueue as inbox_cls
        if gate_cls is None:
            from systemu.interface.command.gate import GateDescriptor as gate_cls
        entry = {
            "package": package,
            "first_seen_tool_id": tool_id,
            "first_seen_tool": tool_name,
            "request_count": request_count,
        }
        inbox_cls(vault).enqueue(gate_cls.from_dep(entry), gate_type="dep")
    except Exception:
        logger.debug("[Registry] dep gate enqueue skipped", exc_info=True)


def _install_failed_dict(
    tool_name: str,
    packages: List[str],
    reason: str,
    pip_tail: Optional[str] = None,
) -> dict:
    """Build the result dict for ``dependency_install_failed`` (pip ran but exit≠0)."""
    detail = reason
    if pip_tail:
        detail = f"{reason}\n--- pip stderr (tail) ---\n{pip_tail}"
    return {
        "success":         False,
        "error":           detail,
        "error_type":      "dependency_install_failed",
        "missing_packages": packages,
        "install_hint":    f"pip install {' '.join(packages)}",
    }


def _discover_plugin_tools(registry, plugins_root: "Path | None" = None) -> None:
    """v0.7-f: discover + register out-of-tree tool plugins.

    Two discovery channels:

    1. **Filesystem**: ``plugins/<name>/__init__.py`` in the install root.
       Each plugin module that defines a ``register_tools(registry)``
       callable gets called once at registry init.

    2. **Entry points**: setuptools group ``systemu.tools``.  Lets
       third-party packages ship plugins via ``pip install ...`` without
       touching the Systemu install root.

    Per-plugin failures are isolated — one broken plugin must not break
    the rest of the registry.  All errors land in the logger at WARNING.
    """
    import importlib
    import importlib.util as _imputil
    import logging
    import sys
    from pathlib import Path as _Path

    log = logging.getLogger(__name__)

    if plugins_root is None:
        plugins_root = _Path("plugins")
    plugins_root = _Path(plugins_root)

    if plugins_root.exists() and plugins_root.is_dir():
        for pkg in sorted(plugins_root.iterdir()):
            init_py = pkg / "__init__.py"
            if not pkg.is_dir() or not init_py.exists():
                continue
            mod_name = f"plugins.{pkg.name}"
            try:
                # Load directly from the file path to honor the caller's
                # plugins_root rather than whatever `plugins` package may
                # already be importable from sys.path.  Critical for tests
                # that point plugins_root at a tmp dir.
                spec = _imputil.spec_from_file_location(
                    mod_name, init_py, submodule_search_locations=[str(pkg)],
                )
                if spec is None or spec.loader is None:
                    continue
                mod = _imputil.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
            except Exception:
                log.exception("[plugin_loader] failed to import %s", mod_name)
                sys.modules.pop(mod_name, None)
                continue
            fn = getattr(mod, "register_tools", None)
            if not callable(fn):
                continue
            try:
                fn(registry)
            except Exception:
                log.exception(
                    "[plugin_loader] register_tools failed in %s", mod_name,
                )

    # Entry points discovery
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="systemu.tools")
        for ep in eps:
            try:
                fn = ep.load()
                if callable(fn):
                    fn(registry)
            except Exception:
                log.exception("[plugin_loader] entry-point %s failed", ep.name)
    except Exception:
        # entry_points API differs across Python versions; absence is non-fatal
        pass


def _resolve_timeout(tool, config, *, explicit):
    """v0.9.1.1: resolve effective tool timeout.

    Precedence: explicit > tool.timeout_seconds > config.tool_default_timeout_seconds > 30s.
    """
    if explicit is not None:
        return explicit
    tool_t = getattr(tool, "timeout_seconds", None)
    if tool_t is not None:
        return tool_t
    return int(getattr(config, "tool_default_timeout_seconds", 30))


# v0.9.6 — parameter-name reconciliation.
#
# Synonym groups: parameter names that mean the same thing across naming
# conventions. LLMs use training-prior names (``path``) while a forged tool may
# declare another (``output_path``); the forge itself sometimes drifts between
# its generated schema and its generated ``run()`` code. Either way the call
# would raise ``TypeError`` at ``run(**params)`` and park the run. We reconcile
# supplied params onto the tool's REAL ``run()`` signature.
_PARAM_SYNONYMS = [
    {"path", "file_path", "filepath", "output_path", "out_path", "outpath",
     "filename", "file_name", "fname", "target_path", "target", "dest",
     "destination", "dest_path", "save_path", "save_to", "output_file",
     "output", "to"},
    {"content", "contents", "text", "data", "body", "payload", "value",
     "file_content", "file_contents", "input", "input_text", "string"},
    {"url", "uri", "link", "address", "endpoint", "href", "web_url", "page_url"},
    {"query", "q", "search", "search_query", "term", "terms", "keywords", "keyword"},
    {"dir", "directory", "folder", "dir_path", "directory_path", "folder_path", "root"},
    {"name", "title", "label", "key", "id", "identifier"},
]


def _synonym_group(param_name: str):
    for grp in _PARAM_SYNONYMS:
        if param_name in grp:
            return grp
    return None


def _reconcile_params(run_fn, params):
    """Map supplied parameter names onto ``run_fn``'s real signature.

    Returns ``(reconciled_params, notes)`` where ``notes`` is a list of
    human-readable remap descriptions (for logging). Strategy:

    1. Non-dict params or unintrospectable callables → return unchanged.
    2. ``run()`` accepts ``**kwargs`` → pass everything through.
    3. Keep params whose names already match accepted parameters.
    4. Map each unrecognised param to an unfilled accepted param that shares a
       synonym group.
    5. If exactly one unrecognised param and one unfilled slot remain, map by
       position (covers genuinely novel names).
    6. Drop any still-unmappable params — strictly better than a hard
       ``TypeError`` that parks the whole run.
    """
    import inspect

    if not isinstance(params, dict):
        return params, []
    try:
        sig = inspect.signature(run_fn)
    except (ValueError, TypeError):
        return params, []

    fn_params = sig.parameters
    if any(p.kind == p.VAR_KEYWORD for p in fn_params.values()):
        return params, []  # run(**kwargs) — accepts anything

    accepted = [
        n for n, p in fn_params.items()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    accepted_set = set(accepted)

    recognized = {k: v for k, v in params.items() if k in accepted_set}
    unknown = {k: v for k, v in params.items() if k not in accepted_set}
    if not unknown:
        return recognized, []

    notes: List[str] = []
    unfilled = [n for n in accepted if n not in recognized]

    # (4) synonym-based mapping
    for uname in list(unknown):
        grp = _synonym_group(uname)
        if not grp:
            continue
        match = next((a for a in unfilled if a in grp), None)
        if match is not None:
            recognized[match] = unknown.pop(uname)
            unfilled.remove(match)
            notes.append(f"{uname}->{match}")

    # (5) single-unknown / single-unfilled positional fallback
    if len(unknown) == 1 and len(unfilled) == 1:
        uname, uval = next(iter(unknown.items()))
        target = unfilled[0]
        recognized[target] = uval
        unknown.pop(uname)
        unfilled.pop(0)
        notes.append(f"{uname}->{target}(pos)")

    # (6) anything still unknown is dropped (logged by caller)
    if unknown:
        notes.append("dropped:" + ",".join(sorted(unknown)))

    return recognized, notes


class ToolRegistry:
    """Dynamic importer + direct-call dispatcher.

    Args:
        implementations_dir: Path to vault/tools/implementations/
        vault:               Vault instance used to check tool.enabled before execution.
        install_mode:        Effective :class:`InstallMode` for the self-heal
                             path.  Resolved by the caller from config + env
                             (see ``dependency_installer.resolve_install_mode``).
                             When omitted (e.g. older callers, tests) the
                             registry falls back to PROMPT, the safe default
                             for local mode — and without an approval store
                             that means every install request is blocked.
        approvals:           Operator-managed allow-list.  Required for
                             PROMPT mode to make forward progress; omitted
                             for ALWAYS/OFF modes.
    """

    def __init__(
        self,
        implementations_dir: Path,
        vault: "Vault",
        *,
        install_mode: Optional["InstallMode"] = None,
        approvals:    Optional["DepApprovalStore"] = None,
    ):
        self._dir     = implementations_dir
        self._vault   = vault
        self._modules: dict[str, Any] = {}
        # Imported lazily to keep import-time cost off the dashboard cold path.
        from systemu.runtime.dependency_installer import InstallMode as _InstallMode
        self._install_mode: "_InstallMode" = install_mode or _InstallMode.PROMPT
        self._approvals = approvals
        # v0.7-f: out-of-tree plugin specs (filesystem dir + entry-points).
        # Plugin authors call ``registry.register(spec_dict)``; we stash the
        # specs here for the runtime to surface alongside in-tree tools.
        self._plugin_specs: list[dict] = []

        # v0.7-f: discover out-of-tree plugins (fs `plugins/` dir + entry points).
        # Per-plugin failures are isolated; any catastrophic failure of the loader
        # itself must not block the in-tree registry from coming up.
        try:
            _discover_plugin_tools(self)
        except Exception:
            logger.exception(
                "[tool_registry] v0.7-f plugin discovery failed — continuing"
            )

    # ── Plugin registration hook (v0.7-f) ─────────────────────────────────────

    def register(self, spec: dict) -> None:
        """v0.7-f: callback invoked by plugin ``register_tools(registry)``.

        Accepts a tool spec dict (must contain at least ``name``) and stores it
        on ``self._plugin_specs``.  The dispatcher in :py:meth:`execute` is
        unchanged — plugin tools are surfaced via the vault layer once the
        operator opts them in, identical to in-tree tools.
        """
        if not isinstance(spec, dict) or "name" not in spec:
            logger.warning("[tool_registry] ignored malformed plugin spec: %r", spec)
            return
        self._plugin_specs.append(spec)

    # ── Public API ────────────────────────────────────────────────────────────

    async def execute(
        self,
        name: str,
        params: dict,
        timeout: Optional[float] = None,   # v0.9.1.1: None = resolve via _resolve_timeout
    ) -> dict:
        """Gate 3 check → import module → call run(**params) in a thread.

        Returns a result dict (always has 'success' bool and 'error' key).
        Raises ToolNotEnabledError if the tool has not been enabled by the user.
        """
        # ── Gate 3: tool must be explicitly enabled ────────────────────────
        tool = self._vault.find_tool_by_name(name)
        if tool is None:
            return {"success": False, "error": f"Tool '{name}' not found in vault"}
        # v0.9.1.1: resolve effective timeout (explicit > tool field > config default > 30s)
        from sharing_on.config import Config as _Config
        effective_timeout = _resolve_timeout(
            tool,
            getattr(self, "_config", None) or _Config(),
            explicit=timeout,
        )
        # ── Gate 2.5 (v0.8.19): validate params against the declared schema ──
        from systemu.runtime.param_validation import validate_params
        _perr = validate_params(getattr(tool, "parameters_schema", {}) or {}, params or {})
        if _perr:
            return {"success": False, "error_type": "tool_param_invalid",
                    "error": ("Invalid parameters for '" + name + "': " + "; ".join(_perr)
                              + ". Correct the parameters and call the tool again.")}
        if not tool.enabled:
            raise ToolNotEnabledError(
                f"Tool '{name}' is not enabled. "
                "Open the Tools Registry page in the dashboard and flip the toggle ON."
            )
        # ── Gate 3.5 (v0.5.0-a): dry-run must have passed ──────────────────
        # A tool whose dry-run failed cannot be called even if the operator
        # accidentally toggled it on.  ``skipped`` is allowed (destructive
        # tools the operator verified manually).  ``not_run`` is allowed
        # for backward-compat with pre-v0.5.0 tools.
        dr_status = getattr(tool, "dry_run_status", "not_run") or "not_run"
        if dr_status == "failed":
            return {
                "success":    False,
                "error":      (
                    f"Tool '{name}' has a failed dry-run on record. "
                    f"Re-forge with feedback on the Tools page before enabling."
                ),
                "error_type": "tool_dry_run_failed",
                "dry_run_evidence": getattr(tool, "dry_run_evidence", {}) or {},
            }

        # ── Gate 4 (v0.8.18): required credentials must be available ───────
        reqs = getattr(tool, "requires_credentials", None) or []
        if reqs:
            from systemu.runtime.credentials.resolver import CredentialResolver
            resolver = CredentialResolver()
            missing = resolver.missing(reqs)
            if missing:
                policy = (os.environ.get("SYSTEMU_CREDENTIAL_POLICY", "prompt") or "prompt").lower()
                interactive = (os.environ.get("SYSTEMU_DECISION_QUEUE", "").lower() == "true")
                if policy == "degrade" or not interactive:
                    return _credential_degraded(name, missing[0])
                from systemu.interface.notifications import request_credential
                if not request_credential(missing[0]):     # may raise PendingCredentialRequest
                    return _credential_degraded(name, missing[0])  # operator skipped
            resolver.promote_to_env(reqs)   # make resolved secrets visible to the tool (os.environ)

        # ── Load module (cached after first import; self-heals on ImportError) ─
        load_result = self._load_with_self_heal(name, tool)
        if isinstance(load_result, dict):
            # Self-heal produced a structured error to return to the caller.
            return load_result
        mod = load_result

        # ── Call run(**params) in thread pool ──────────────────────────────
        def _call(_mod) -> dict:
            try:
                # v0.9.6: reconcile supplied param names onto run()'s real
                # signature. LLM/forge naming drift (path vs output_path) would
                # otherwise raise TypeError here and park the whole run.
                _params, _notes = _reconcile_params(getattr(_mod, "run", None), params)
                if _notes:
                    logger.info(
                        "[Registry] reconciled params for '%s': %s",
                        name, "; ".join(_notes),
                    )
                return _mod.run(**_params)
            except ImportError as exc:
                # Lazy/conditional import inside run().  Re-raised to the
                # outer await so we can self-heal once, then retry.
                missing     = exc.name or "unknown"
                is_internal = missing.startswith("systemu")
                raise ToolDependencyError(name, missing=missing,
                                          is_internal=is_internal) from exc
            except TypeError as exc:
                return {"success": False, "error": f"Parameter mismatch calling '{name}': {exc}"}
            except Exception as exc:
                logger.exception("[Registry] Unhandled exception in tool '%s'", name)
                return {"success": False, "error": str(exc)}

        loop = asyncio.get_event_loop()
        try:
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(_executor, _call, mod),
                    timeout=effective_timeout,
                )
            except ToolDependencyError as exc:
                # Lazy ImportError surfaced from inside run().  Try self-heal
                # exactly once, then retry the call.
                heal_outcome = self._attempt_self_heal(tool, exc.missing)
                if isinstance(heal_outcome, dict):
                    return heal_outcome
                self.invalidate(name)
                reload_result = self._load_with_self_heal(name, tool, _already_healed=True)
                if isinstance(reload_result, dict):
                    return reload_result
                mod = reload_result
                result = await asyncio.wait_for(
                    loop.run_in_executor(_executor, _call, mod),
                    timeout=effective_timeout,
                )
            logger.debug("[Registry] '%s' → success=%s", name, result.get("success"))
            return result
        except asyncio.TimeoutError:
            logger.warning("[Registry] Tool '%s' timed out after %.0fs", name, effective_timeout)
            return {"success": False, "error": f"Tool '{name}' timed out after {effective_timeout:.0f}s"}
        except ToolDependencyError as exc:
            # Lazy ImportError on the retry — give up and surface the
            # current dep-error dict.  Tools that import a third package
            # the manifest didn't declare end up here.
            return _dep_error_dict(exc)

    def invalidate(self, name: str) -> None:
        """Evict a cached module so it is re-imported on the next call.

        Call this after re-forging a tool so the registry picks up the new code.
        """
        evicted = self._modules.pop(name, None)
        if evicted:
            logger.debug("[Registry] Module cache evicted for '%s'", name)

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_with_self_heal(
        self,
        name: str,
        tool: "Tool",
        *,
        _already_healed: bool = False,
    ) -> "Any | dict":
        """Load the module; on ImportError, run a single self-heal pass.

        Returns the imported module on success, or a structured error dict
        when loading still fails (no manifest dep, install blocked, install
        failed).  ``_already_healed`` short-circuits the retry to prevent
        an infinite loop when self-heal itself doesn't fix the import.
        """
        try:
            return self._load(name)
        except ToolDependencyError as exc:
            if _already_healed:
                return _dep_error_dict(exc)
            outcome = self._attempt_self_heal(tool, exc.missing)
            if isinstance(outcome, dict):
                return outcome
            # outcome is True → cache was invalidated implicitly via install;
            # retry the load once.
            try:
                return self._load(name)
            except ToolDependencyError as exc2:
                # Install succeeded but the import still fails — manifest is
                # wrong (e.g. tool imports an additional pkg it didn't list).
                return _dep_error_dict(exc2)
            except FileNotFoundError as exc2:
                return {"success": False, "error": str(exc2)}
        except FileNotFoundError as exc:
            return {"success": False, "error": str(exc)}

    def _attempt_self_heal(self, tool: "Tool", missing_hint: str) -> "bool | dict":
        """Try to install the tool's declared deps.  Returns True on success.

        Returns a structured error dict when self-heal cannot proceed (no
        manifest deps declared, mode = OFF, approval missing, pip failed).
        ``missing_hint`` is only used for logging — we install from the
        manifest, never from the ImportError's reported name.
        """
        declared: List[str] = list(tool.dependencies or [])
        if not declared:
            # Today's behaviour for un-manifested tools: surface the dep
            # error and let the operator add it to the manifest.  Refusing
            # to install from a free-form ImportError name is intentional
            # (the LLM that authored the tool could have caused the import).
            logger.info(
                "[Registry] Tool '%s' missing dep '%s' but manifest declares no "
                "dependencies — refusing to auto-install (manifest is the only "
                "trusted source).",
                tool.name, missing_hint,
            )
            return _dep_error_dict(ToolDependencyError(tool.name, missing=missing_hint))

        # Imported here to keep tool_registry import-graph minimal.
        from systemu.runtime.dependency_installer import (
            InstallMode,
            InstallStatus,
            ensure_satisfied,
        )

        result = ensure_satisfied(
            declared,
            mode=self._install_mode,
            approvals=self._approvals,
            tool_name=tool.name,
            tool_id=tool.id,
        )

        if result.ok:
            logger.info(
                "[Registry] Self-heal for tool '%s': installed %s",
                tool.name, result.installed_now or "(already satisfied)",
            )
            return True

        # Map InstallStatus → structured error dict.
        if result.status is InstallStatus.BLOCKED_DISABLED:
            return _install_blocked_dict(tool.name, declared, result.error or "")
        if result.status is InstallStatus.BLOCKED_PENDING_APPROVAL:
            _maybe_enqueue_dep_gate(
                vault=self._vault,
                tool_id=tool.id, tool_name=tool.name,
                package=str(result.pending_approval or declared),
                request_count=1,
            )
            return _install_pending_dict(
                tool.name, result.pending_approval or declared, result.error or "",
            )
        # FAILED (or any other non-ok status, defensively)
        return _install_failed_dict(
            tool.name,
            declared,
            result.error or f"pip install failed for {declared}",
            pip_tail=result.pip_stderr_tail,
        )

    def _load(self, name: str) -> Any:
        if name not in self._modules:
            path = self._dir / f"{name}.py"
            if not path.exists():
                raise FileNotFoundError(
                    f"No implementation file for tool '{name}' at {path}. "
                    "Has the tool been forged and approved?"
                )
            spec = importlib.util.spec_from_file_location(f"systemu_tools.{name}", path)
            mod  = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)       # type: ignore[union-attr]
            except ImportError as exc:
                # Module-level import failed (top-level or __init__ code).
                # Convert to ToolDependencyError so the caller can return a
                # structured result without triggering the subprocess fallback.
                missing     = exc.name or "unknown"
                is_internal = missing.startswith("systemu")
                raise ToolDependencyError(name, missing=missing,
                                          is_internal=is_internal) from exc
            if not hasattr(mod, "run"):
                raise AttributeError(
                    f"Tool module '{name}' has no run() function. "
                    "Ensure the implementation follows the wrapper function standard."
                )
            self._modules[name] = mod
            logger.debug("[Registry] Loaded tool module '%s' from %s", name, path)
        return self._modules[name]
