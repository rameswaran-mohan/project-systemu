"""Self-healing pip-dependency installer for forged tools.

Tools declare their Python dependencies in ``TOOL_META["dependencies"]`` and in
the vault ``tools.dependencies`` column.  Historically only ``DockerBackend``
honoured that manifest — ``ToolRegistry`` (the fast path used in local mode)
and ``LocalBackend`` silently dropped it, so a tool that needed
``python-docx`` would fail forever in local mode even though the system had
all the metadata required to fix itself.

This module is the single place that installs declared deps into the current
Python interpreter.  Both the registry's self-heal path and the local
backend's subprocess path call ``ensure_satisfied()``.

Design constraints honoured here:

* **Manifest is the only source of truth.**  Callers source ``packages``
  from ``tool.dependencies`` (vetted at forge time).  We never derive
  package names from free-form text like ``ImportError.name``, which a
  malicious tool author could control.
* **Defence in depth.**  ``packages`` are re-validated against a strict
  regex inside the installer regardless of the caller's discipline.
* **Operator-gated by default in local mode.**  Auto-install on the host
  Python is a real trust decision.  ``InstallMode.PROMPT`` blocks installs
  until the operator approves the package via ``sharing_on tools deps
  approve <pkg>``; ``InstallMode.ALWAYS`` installs without prompt (docker
  modes); ``InstallMode.OFF`` never installs (enterprise / air-gapped).
* **Process-local cache** so we pip-install at most once per package per
  process.  ``pip install`` of an already-satisfied package is fast but
  not free — caching keeps tool-call latency at registry-cache speeds for
  every call after the first.
* **Per-package locking.**  Concurrent shadows can ask for different deps
  in parallel.  We only serialise calls that target the same package, not
  every install.

Returned ``InstallResult.status`` distinguishes four "could not install"
cases so the Shadow runtime can surface a precise event-log line and
suppress retries without learning the wrong lesson:

  * ``satisfied``                 — nothing to do (cache hit or empty manifest)
  * ``installed``                 — at least one package was just installed
  * ``blocked_disabled``          — mode = OFF
  * ``blocked_pending_approval``  — mode = PROMPT and package is not approved yet
  * ``failed``                    — pip ran but exit ≠ 0 (or timed out)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from systemu.runtime.dep_approvals import DepApprovalStore

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration

class InstallMode(str, Enum):
    """How aggressively to auto-install declared pip dependencies.

    Resolution order at call time:
      1. ``SYSTEMU_TOOL_DEP_INSTALL_MODE`` env var (one of the values below).
      2. ``config.tool_dep_install_mode`` if set to a real value.
      3. ``AUTO`` → derive from ``config.systemu_mode``:
            - ``docker-enterprise`` → OFF (air-gapped by assumption)
            - ``docker-local``/``docker`` → ALWAYS (each call is a fresh container)
            - ``local`` → PROMPT (operator gates host installs)
    """
    OFF       = "off"         # never install — return blocked_disabled
    PROMPT    = "prompt"      # install only if the package is in the approval store
    ALWAYS    = "always"      # install on demand without approval check
    AUTO      = "auto"        # resolve from systemu_mode at call time
    ALLOWLIST = "allow-list"  # v0.6.8-e: install only if package is in tool_dep_approvals (docker-* default)


def resolve_install_mode(
    *,
    config_mode: Optional[str] = None,
    systemu_mode: Optional[str] = None,
    env: Optional[dict] = None,
) -> InstallMode:
    """Pick the effective InstallMode for this process.

    Args:
        config_mode:  ``config.tool_dep_install_mode`` (may be ``"auto"`` or empty).
        systemu_mode: ``config.systemu_mode`` (``"local"`` / ``"docker-local"`` / ``"docker-enterprise"``).
        env:          Override of ``os.environ`` for testing.
    """
    env = env if env is not None else os.environ
    explicit = (env.get("SYSTEMU_TOOL_DEP_INSTALL_MODE") or "").strip().lower()
    if explicit:
        try:
            mode = InstallMode(explicit)
            if mode is not InstallMode.AUTO:
                return mode
        except ValueError:
            logger.warning(
                "[DepInstaller] Ignoring invalid SYSTEMU_TOOL_DEP_INSTALL_MODE=%r "
                "(expected one of: off, prompt, always, auto)",
                explicit,
            )

    cfg = (config_mode or "").strip().lower()
    if cfg and cfg != InstallMode.AUTO.value:
        try:
            return InstallMode(cfg)
        except ValueError:
            logger.warning("[DepInstaller] Ignoring invalid config tool_dep_install_mode=%r", cfg)

    sys_mode = (systemu_mode or "local").strip().lower()
    if sys_mode == "docker-enterprise":
        return InstallMode.OFF
    if sys_mode in ("docker-local", "docker"):
        return InstallMode.ALWAYS
    return InstallMode.PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# Package-spec validation
#
# The accepted grammar is a deliberately small subset of PEP 508 / PEP 440:
#   name[extra,extra]<spec><version>
# We accept exactly what forge prompts realistically emit and reject anything
# that could be a shell-injection vector even though the installer never goes
# through a shell.  A tighter regex here is cheap insurance.

_PKG_RE = re.compile(
    r"""
    ^
    [A-Za-z0-9]                              # must start alphanumeric
    [A-Za-z0-9_\-\.]{0,99}                   # name body (≤100 chars)
    (?:\[[A-Za-z0-9_,\-]{1,60}\])?           # optional extras: [extra1,extra2]
    (?:\s*(?:==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9_\-\.\*\+]+){0,2}   # 0–2 version specifiers
    $
    """,
    re.VERBOSE,
)

# Hard cap on number of packages a single tool can declare.  Even very
# complex tools (e.g. ML pipelines) sit well under this.  Catches manifest
# corruption / prompt-injection that tries to flood pip.
_MAX_PACKAGES_PER_TOOL = 25


class InvalidPackageSpecError(ValueError):
    """Raised when a package spec from a tool manifest fails validation."""


class DepApprovalPending(RuntimeError):
    """v0.6.8-e: raised when ``allow-list`` mode encounters a not-yet-approved
    package.  ``.packages`` carries the list of unapproved package names so
    the caller can surface them to the operator (typically by linking to
    /recover/tool/<id>)."""

    def __init__(self, packages):
        super().__init__(f"Pending operator approval: {packages}")
        self.packages = list(packages)


def _normalise_and_validate(packages: Iterable[str]) -> List[str]:
    """Strip, dedupe, validate.  Empty inputs collapse to ``[]``."""
    seen: set[str] = set()
    out: List[str] = []
    for raw in packages:
        if not isinstance(raw, str):
            raise InvalidPackageSpecError(f"non-string package spec: {raw!r}")
        spec = raw.strip()
        if not spec:
            continue
        if spec in seen:
            continue
        if not _PKG_RE.match(spec):
            raise InvalidPackageSpecError(
                f"package spec {spec!r} does not match expected grammar "
                "(alphanumeric/.-_/extras/version specifiers only)"
            )
        seen.add(spec)
        out.append(spec)
        if len(out) > _MAX_PACKAGES_PER_TOOL:
            raise InvalidPackageSpecError(
                f"tool declared more than {_MAX_PACKAGES_PER_TOOL} packages — "
                "likely manifest corruption; refusing to proceed"
            )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Caching + per-package locks

_satisfied: set[str] = set()
_satisfied_lock = threading.Lock()

# Per-package locks so two installs of *different* packages can run in
# parallel but two installs of the *same* package serialise.
_pkg_locks: dict[str, threading.Lock] = {}
_pkg_locks_meta_lock = threading.Lock()


def _lock_for(pkg: str) -> threading.Lock:
    with _pkg_locks_meta_lock:
        lock = _pkg_locks.get(pkg)
        if lock is None:
            lock = threading.Lock()
            _pkg_locks[pkg] = lock
        return lock


def _dist_name(pkg: str) -> str:
    """Bare distribution name from a requirement string ('requests>=2' → 'requests')."""
    return re.split(r"[<>=!~\[; ]", pkg.strip(), 1)[0]


def _is_satisfied(pkg: str) -> bool:
    with _satisfied_lock:
        if pkg in _satisfied:
            return True
    # W11.7: the cache only remembers THIS process's installs — a package
    # already present in the environment is satisfied too. Without this,
    # every fresh daemon treated installed deps as missing, and PROMPT mode
    # re-gated packages the operator had installed and approved long ago
    # (field RCA 2026-06-12: requests/playwright installed + approved, yet
    # every dep-declaring tool blocked).
    try:
        from importlib import metadata as _metadata
        _metadata.version(_dist_name(pkg))
    except Exception:
        return False
    _mark_satisfied([pkg])
    return True


def _mark_satisfied(pkgs: Iterable[str]) -> None:
    with _satisfied_lock:
        _satisfied.update(pkgs)


def reset_cache_for_tests() -> None:
    """Clear in-process state.  ONLY for use in tests."""
    with _satisfied_lock:
        _satisfied.clear()
    with _pkg_locks_meta_lock:
        _pkg_locks.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Result type

class InstallStatus(str, Enum):
    SATISFIED                 = "satisfied"
    INSTALLED                 = "installed"
    BLOCKED_DISABLED          = "blocked_disabled"
    BLOCKED_PENDING_APPROVAL  = "blocked_pending_approval"
    FAILED                    = "failed"


@dataclass
class InstallResult:
    ok:               bool                          # safe to (re-)try the import
    status:           InstallStatus
    installed_now:    List[str] = field(default_factory=list)
    pending_approval: List[str] = field(default_factory=list)
    error:            Optional[str] = None
    pip_stderr_tail:  Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point

DEFAULT_TIMEOUT_SECONDS = 180


def ensure_satisfied(
    packages: Iterable[str],
    *,
    mode: InstallMode,
    approvals: Optional["DepApprovalStore"] = None,
    tool_name: str = "<unknown>",
    tool_id:   Optional[str] = None,
    timeout:   int = DEFAULT_TIMEOUT_SECONDS,
) -> InstallResult:
    """Ensure every entry in ``packages`` is installed into the current Python.

    Args:
        packages:  Package specs sourced from the tool manifest.
        mode:      Resolved InstallMode (see ``resolve_install_mode``).
                   May not be ``AUTO`` here — callers resolve first.
        approvals: Required when ``mode == PROMPT``.  When ``None`` in PROMPT
                   mode the function returns ``BLOCKED_PENDING_APPROVAL`` for
                   every non-cached package (fail-closed).
        tool_name: Used for logging + recording pending approvals.
        tool_id:   Used for recording who first requested the package.
        timeout:   Per-call timeout for the pip subprocess.

    Returns:
        InstallResult describing the outcome.  ``ok=True`` means the caller
        may retry their import; ``ok=False`` means stop and surface the
        status to the operator / Shadow.
    """
    if mode is InstallMode.AUTO:
        raise ValueError(
            "InstallMode.AUTO must be resolved by the caller via "
            "resolve_install_mode() before reaching ensure_satisfied()."
        )

    try:
        pkgs = _normalise_and_validate(packages)
    except InvalidPackageSpecError as exc:
        logger.warning(
            "[DepInstaller] Tool '%s' manifest rejected: %s",
            tool_name, exc,
        )
        return InstallResult(
            ok=False,
            status=InstallStatus.FAILED,
            error=f"invalid package spec in tool manifest: {exc}",
        )

    if not pkgs:
        return InstallResult(ok=True, status=InstallStatus.SATISFIED)

    if mode is InstallMode.OFF:
        logger.info(
            "[DepInstaller] Install mode=OFF — refusing to install %s for tool '%s'",
            pkgs, tool_name,
        )
        return InstallResult(
            ok=False,
            status=InstallStatus.BLOCKED_DISABLED,
            error=(
                "Auto-install is disabled (mode=off). Bake required packages "
                f"into the base image or set SYSTEMU_TOOL_DEP_INSTALL_MODE=prompt. "
                f"Required: {', '.join(pkgs)}"
            ),
        )

    # Drop cache hits up front.
    to_consider = [p for p in pkgs if not _is_satisfied(p)]
    if not to_consider:
        return InstallResult(ok=True, status=InstallStatus.SATISFIED)

    # PROMPT mode: filter against the approval store.
    if mode is InstallMode.PROMPT:
        if approvals is None:
            # Fail-closed: PROMPT mode with no approval store can never proceed.
            logger.warning(
                "[DepInstaller] PROMPT mode with no approval store — "
                "treating all packages as pending: %s", to_consider,
            )
            return InstallResult(
                ok=False,
                status=InstallStatus.BLOCKED_PENDING_APPROVAL,
                pending_approval=to_consider,
                error=(
                    f"Tool '{tool_name}' needs operator approval to install: "
                    f"{', '.join(to_consider)}. "
                    f"Run: sharing_on tools deps approve <package>"
                ),
            )

        approved   = [p for p in to_consider if approvals.is_approved(p)]
        unapproved = [p for p in to_consider if not approvals.is_approved(p)]
        if unapproved:
            # Record the pending request so the operator CLI can see what's outstanding.
            for p in unapproved:
                try:
                    approvals.record_pending(
                        p, tool_name=tool_name, tool_id=tool_id,
                    )
                except Exception:
                    logger.exception("[DepInstaller] record_pending failed for %s", p)

            # v0.3.6: surface the request in the Systemu Chat supervisor feed
            # so the operator sees it without having to navigate to /tools first.
            # EventBus.publish_dep_approval_request handles its own dedup so
            # 50 shadows hitting the same missing dep produce ONE card.
            try:
                from systemu.interface.event_bus import EventBus
                bus = EventBus.get()
                pending_total = len(approvals.list_pending())
                for p in unapproved:
                    # request_count is per-package across processes (tracked by store).
                    request_count = 1
                    try:
                        for entry in approvals.list_pending():
                            if entry.get("package") == p:
                                request_count = int(entry.get("request_count", 1))
                                break
                    except Exception:
                        pass
                    bus.publish_dep_approval_request(
                        p,
                        tool_name=tool_name,
                        tool_id=tool_id,
                        request_count=request_count,
                        pending_total=pending_total,
                    )
            except Exception:
                logger.debug(
                    "[DepInstaller] EventBus dep-approval publish skipped",
                    exc_info=True,
                )

            logger.info(
                "[DepInstaller] %d package(s) pending approval for tool '%s': %s",
                len(unapproved), tool_name, unapproved,
            )
            return InstallResult(
                ok=False,
                status=InstallStatus.BLOCKED_PENDING_APPROVAL,
                pending_approval=unapproved,
                error=(
                    f"Tool '{tool_name}' needs operator approval to install: "
                    f"{', '.join(unapproved)}. "
                    f"Run: sharing_on tools deps approve <package>"
                ),
            )
        to_install = approved
    elif mode is InstallMode.ALLOWLIST:
        # v0.6.8-e: docker-* default.  Allow-list lives in the
        # ``tool_dep_approvals`` SQLAlchemy table (operator-approved via
        # the dashboard recovery panel or the install wizard).  Unlike
        # PROMPT mode this raises an exception so the caller surfaces a
        # precise "dep pending" error rather than silently no-op installing.
        from systemu.runtime.dep_approvals import is_allowlisted
        unapproved = [p for p in to_consider if not is_allowlisted(p)]
        if unapproved:
            logger.info(
                "[DepInstaller] allow-list mode: %d unapproved package(s) for "
                "tool '%s': %s — raising DepApprovalPending",
                len(unapproved), tool_name, unapproved,
            )
            raise DepApprovalPending(unapproved)
        to_install = to_consider
    else:
        # ALWAYS mode
        to_install = to_consider

    if not to_install:
        return InstallResult(ok=True, status=InstallStatus.SATISFIED)

    # ── Run pip with per-package locks ──────────────────────────────────────
    # Acquire all needed locks (in sorted order, to prevent deadlock) before
    # running a single pip call that installs everything atomically.
    sorted_pkgs = sorted(to_install)
    locks = [_lock_for(p) for p in sorted_pkgs]
    for lock in locks:
        lock.acquire()
    try:
        # Re-check cache under locks: another thread may have installed these
        # in the meantime.
        still_needed = [p for p in to_install if not _is_satisfied(p)]
        if not still_needed:
            return InstallResult(ok=True, status=InstallStatus.SATISFIED)

        logger.info(
            "[DepInstaller] Installing %s for tool '%s' into %s",
            still_needed, tool_name, sys.executable,
        )
        result = _run_pip_install(still_needed, timeout=timeout)
        if not result.ok:
            return result
        _mark_satisfied(still_needed)
        logger.info("[DepInstaller] Installed %s (tool '%s')", still_needed, tool_name)
        return InstallResult(
            ok=True,
            status=InstallStatus.INSTALLED,
            installed_now=still_needed,
        )
    finally:
        for lock in locks:
            lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# pip subprocess

def _run_pip_install(packages: List[str], *, timeout: int) -> InstallResult:
    """Synchronous ``<sys.executable> -m pip install`` invocation.

    Stays sync rather than async because:
      * Callers already invoke us from a thread-pool executor
        (``ToolRegistry`` runs ``mod.run()`` via ``run_in_executor``).
      * ``subprocess.run`` is the simplest correct timeout primitive on
        Windows for native exes.
    """
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *packages]
    try:
        proc = subprocess.run(  # noqa: S603 — args are all our own / validated
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[DepInstaller] pip timed out installing %s after %ss", packages, timeout)
        return InstallResult(
            ok=False,
            status=InstallStatus.FAILED,
            error=f"pip install timed out after {timeout}s installing {packages}",
            pip_stderr_tail=(exc.stderr or b"").decode(errors="replace")[-500:] if isinstance(exc.stderr, bytes) else (exc.stderr or "")[-500:],
        )
    except FileNotFoundError as exc:
        # sys.executable itself missing / pip module not present.
        logger.error("[DepInstaller] pip not invocable: %s", exc)
        return InstallResult(
            ok=False,
            status=InstallStatus.FAILED,
            error=f"pip not available on {sys.executable}: {exc}",
        )
    except Exception as exc:
        logger.exception("[DepInstaller] unexpected pip failure")
        return InstallResult(
            ok=False,
            status=InstallStatus.FAILED,
            error=f"unexpected pip failure: {exc}",
        )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        logger.warning(
            "[DepInstaller] pip exit %d installing %s\n--- stderr tail ---\n%s",
            proc.returncode, packages, tail,
        )
        return InstallResult(
            ok=False,
            status=InstallStatus.FAILED,
            error=f"pip install failed (exit {proc.returncode}) for {packages}",
            pip_stderr_tail=tail,
        )

    return InstallResult(ok=True, status=InstallStatus.INSTALLED, installed_now=list(packages))
