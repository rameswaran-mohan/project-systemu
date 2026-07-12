"""R-UX1 — the ONE deterministic cross-OS capability profile + self-diagnosis.

Two jobs, one module:

  * ``platform_profile()`` — a single deterministic capability map with a
    STABLE schema across win32/darwin/linux (SPEC §15-UX UX-6 / §15-DEP
    DEP-1/6/10). Every OS-divergence in the product renders from THIS profile,
    so behaviour is experience-parity, never a silent OS-conditional branch
    scattered through the code. The profile is honest by construction:
      - ``forged_net_jail`` is ``"absent"`` because no OS egress jail exists yet
        (IMPL-13's forged-network hard-DENY is active precisely because of this);
      - inside a container, host-only capabilities (record/capture, COM/UIA,
        hotkey, host-browser) are reported as deferred to the Host Companion
        (flagged) — the container NEVER pretends a host capability is present
        (DEP-10).

  * ``build_doctor_report()`` / ``report_exit_code()`` — the self-diagnosis a
    user runs when "nothing is happening" (SPEC §15-UX UX-4). It answers WHY:
    a killed/absent LLM provider, a locked keyring, a dead daemon — each is a
    named, actionable problem. Killed/absent provider and a locked keyring are
    BLOCKING (``doctor`` exits nonzero); a dead daemon or a plaintext-keyring
    fallback are surfaced as non-blocking warnings.

Deterministic + hermetic: ``sys.platform`` and the container / keyring / provider
probes are all injectable, so tests assert the SAME schema on any host OS and
drive killed/locked states without touching the machine. Leaf module — imports
stdlib plus a lazy keyring probe and the leaf ``at_rest`` helper; no import cycle.
"""
from __future__ import annotations

import os
import platform as _platform
import sys
from typing import Callable, Optional

# ── keyring-backend enum (the STABLE cross-OS vocabulary) ────────────────────
KEYRING_DPAPI = "dpapi"
KEYRING_KEYCHAIN = "keychain"
KEYRING_SECRETSERVICE = "secretservice"
KEYRING_PLAINTEXT = "plaintext_fallback"

FORGED_NET_JAIL_ABSENT = "absent"

# Host-only capabilities (DEP-10). Each row is honest per OS + container state.
_HOST_CAPS = (
    ("record_capture", "Screen / input capture"),
    ("com_uia", "Windows COM / UIA automation"),
    ("hotkey", "Global hotkey"),
    ("host_browser", "Host browser control"),
)


# ── platform helpers ─────────────────────────────────────────────────────────

def _os_family(platform_str: str) -> str:
    if platform_str.startswith("win"):
        return "windows"
    if platform_str == "darwin":
        return "macos"
    if platform_str.startswith("linux"):
        return "linux"
    return "other"


def _in_container() -> bool:
    """Best-effort: are we running inside a container? Mirrors
    ``interpreter_check._is_in_container`` plus an explicit env override."""
    try:
        if os.path.exists("/.dockerenv"):
            return True
        mode = (os.environ.get("SYSTEMU_MODE", "") or "").lower()
        if mode.startswith("docker"):
            return True
        if (os.environ.get("SYSTEMU_CONTAINER", "") or "").strip().lower() in ("1", "true", "yes"):
            return True
    except Exception:
        pass
    return False


def _provider_configured() -> bool:
    """Is the LLM provider configured? (env-driven so the profile stays
    deterministic + hermetic — never prints the key, only its presence)."""
    try:
        return bool((os.environ.get("OPENROUTER_API_KEY", "") or "").strip())
    except Exception:
        return False


def _usable_keyring():
    """The single keyring-usability probe (delegates to the secrets store, which
    owns the fail/null-sentinel rejection). Returns a keyring-like object or
    None; never raises."""
    try:
        from systemu.runtime.credentials.store import usable_keyring
        return usable_keyring()
    except Exception:
        return None


def _dpapi_available() -> bool:
    """Is the Windows DPAPI at-rest envelope usable here?"""
    try:
        from systemu.runtime.credentials.at_rest import is_encrypted_at_rest
        return bool(is_encrypted_at_rest())
    except Exception:
        return False


def keyring_backend(platform_str: Optional[str] = None, *,
                    usable: Optional[Callable[[], object]] = None,
                    dpapi: Optional[Callable[[], bool]] = None) -> str:
    """Resolve the effective secret-at-rest backend to the STABLE enum.

    Windows reports ``dpapi`` whenever the OS keyring (Credential Manager) OR
    the DPAPI at-rest envelope is available (both bind the secret to the user).
    macOS → ``keychain``, Linux/other POSIX → ``secretservice`` when a real
    backend exists. With no backend at all it degrades to ``plaintext_fallback``
    (a flagged 0600 file) — the honest, reported last-resort.
    """
    platform_str = platform_str or sys.platform
    usable_fn = usable if usable is not None else _usable_keyring
    dpapi_fn = dpapi if dpapi is not None else _dpapi_available
    has_backend = usable_fn() is not None

    if platform_str.startswith("win"):
        if has_backend or dpapi_fn():
            return KEYRING_DPAPI
        return KEYRING_PLAINTEXT
    if not has_backend:
        return KEYRING_PLAINTEXT
    if platform_str == "darwin":
        return KEYRING_KEYCHAIN
    return KEYRING_SECRETSERVICE


def _host_capabilities(platform_str: str, in_container: bool) -> list:
    """DEP-10 honesty rows for host-only capabilities.

    In a container: EVERY host capability defers to the Host Companion (flagged)
    — the container never claims a host capability as present. On a native host:
    the row reports its real per-OS availability (COM/UIA is Windows-only)."""
    is_win = platform_str.startswith("win")
    rows = []
    for cap_id, label in _HOST_CAPS:
        if in_container:
            rows.append({
                "id": cap_id, "label": label, "available": False,
                "via": "host_companion",
                "note": "available via Host Companion (flagged)",
            })
            continue
        if cap_id == "com_uia":
            available = is_win
            note = "" if is_win else "Windows-only (COM / UIA)"
        else:
            available = True
            note = ""
        rows.append({
            "id": cap_id, "label": label, "available": available,
            "via": "native", "note": note,
        })
    return rows


def platform_profile(*, platform_str: Optional[str] = None,
                     in_container: Optional[bool] = None,
                     provider_configured: Optional[bool] = None) -> dict:
    """The one deterministic capability map. STABLE schema across every OS."""
    platform_str = platform_str if platform_str is not None else sys.platform
    if in_container is None:
        in_container = _in_container()
    if provider_configured is None:
        provider_configured = _provider_configured()

    try:
        arch = _platform.machine() or "unknown"
    except Exception:
        arch = "unknown"
    try:
        py_version = _platform.python_version()
    except Exception:
        py_version = ".".join(str(x) for x in sys.version_info[:3])

    return {
        "os": platform_str,
        "os_family": _os_family(platform_str),
        "arch": arch,
        "python_version": py_version,
        # No host desktop inside a container -> capture is not directly available
        # (it is offered via the Host Companion honesty row instead).
        "capture_available": not in_container,
        "keyring_backend": keyring_backend(platform_str),
        # IMPL-13: no OS egress jail exists yet -> the forged-network hard-DENY
        # stands in for it. The profile reports this honestly.
        "forged_net_jail": FORGED_NET_JAIL_ABSENT,
        "docker_mode": bool(in_container),
        "provider_configured": bool(provider_configured),
        "host_capabilities": _host_capabilities(platform_str, in_container),
    }


# ── self-diagnosis (`doctor` / `/health`) ───────────────────────────────────

def _pkg_version() -> str:
    try:
        import systemu
        return str(getattr(systemu, "__version__", "unknown"))
    except Exception:
        return "unknown"


def _versions() -> dict:
    try:
        py = _platform.python_version()
    except Exception:
        py = ".".join(str(x) for x in sys.version_info[:3])
    return {"systemu": _pkg_version(), "python": py}


# -- probes (best-effort, injectable; monkeypatched by tests) -----------------

def _probe_provider_reachable() -> Optional[bool]:
    """Reachability of the LLM provider. Returns None by default — a real
    network probe is intentionally NOT done here (it would hang/slow `doctor`);
    callers/tests inject ``provider_reachable=False`` to represent a killed
    provider. ``None`` = not probed (a configured provider is assumed fine)."""
    return None


def _probe_keyring_locked() -> bool:
    """Best-effort: is the OS keyring present-but-LOCKED? A locked keychain
    raises on access. Never prompts on a benign read of a nonexistent key on
    Windows; injectable for hermetic tests."""
    kr = _usable_keyring()
    if kr is None:
        return False   # no backend -> not "locked", it's absent (plaintext fallback)
    try:
        kr.get_password("systemu", "__systemu_doctor_probe__")
        return False
    except Exception:
        return True


def _probe_daemon_running(vault_dir: Optional[str] = None) -> Optional[bool]:
    """Best-effort daemon liveness. Returns None if it can't be determined."""
    try:
        if vault_dir is None:
            from sharing_on.config import Config
            vault_dir = Config.from_env().vault_dir
        from systemu.scheduler.daemon import get_status
        return bool(get_status(vault_dir).get("running"))
    except Exception:
        return None


def _last_error() -> Optional[str]:
    """The most recent operator-visible degradation, if any (never raises)."""
    try:
        from systemu.interface.dashboard_state import AppState
        deg = getattr(AppState.get(), "storage_degraded", None)
        if deg:
            return f"storage degraded: {deg.get('reason', 'unknown')}"
    except Exception:
        pass
    return None


def build_doctor_report(*, provider_configured: Optional[bool] = None,
                        provider_reachable: Optional[bool] = None,
                        keyring_locked: Optional[bool] = None,
                        daemon_running: Optional[bool] = None,
                        last_error: Optional[str] = None,
                        vault_dir: Optional[str] = None,
                        platform_str: Optional[str] = None,
                        in_container: Optional[bool] = None) -> dict:
    """The self-diagnosis report. Pure given its inputs; every probe is
    injectable so tests drive killed/locked states deterministically."""
    if provider_configured is None:
        provider_configured = _provider_configured()
    if provider_reachable is None:
        provider_reachable = _probe_provider_reachable()
    if keyring_locked is None:
        keyring_locked = _probe_keyring_locked()
    if daemon_running is None:
        daemon_running = _probe_daemon_running(vault_dir)
    if last_error is None:
        last_error = _last_error()

    prof = platform_profile(platform_str=platform_str, in_container=in_container,
                            provider_configured=provider_configured)
    problems = []

    # -- LLM provider (BLOCKING) ------------------------------------------
    if not provider_configured:
        problems.append({
            "id": "provider_absent", "severity": "danger", "blocking": True,
            "message": "LLM provider is not configured (OPENROUTER_API_KEY is "
                       "missing) — nothing can run.",
            "cta": "Add OPENROUTER_API_KEY=… to .env and restart the daemon.",
        })
    elif provider_reachable is False:
        problems.append({
            "id": "provider_unreachable", "severity": "danger", "blocking": True,
            "message": "LLM provider is configured but not reachable "
                       "(killed / unreachable) — runs stall with no output.",
            "cta": "Check the network / provider status, then retry.",
        })

    # -- keyring (BLOCKING when locked; a plaintext fallback is a warning) --
    if keyring_locked:
        problems.append({
            "id": "keyring_locked", "severity": "danger", "blocking": True,
            "message": "The OS keyring is locked — secrets cannot be read, so "
                       "credentialed steps fail.",
            "cta": "Unlock the OS keyring / keychain, then re-run.",
        })
    elif prof["keyring_backend"] == KEYRING_PLAINTEXT:
        problems.append({
            "id": "keyring_plaintext_fallback", "severity": "warning", "blocking": False,
            "message": "No OS keyring backend — secrets use a flagged plaintext "
                       "file fallback (0600).",
            "cta": "Enable an OS keyring (Keychain / SecretService) for at-rest "
                   "protection.",
        })

    # -- daemon (non-blocking warning) ------------------------------------
    if daemon_running is False:
        problems.append({
            "id": "daemon_down", "severity": "warning", "blocking": False,
            "message": "The Systemu daemon is not running — recordings and tasks "
                       "will not be picked up.",
            "cta": "Start it: sharing_on daemon start",
        })

    report = {
        "profile": prof,
        "provider": {"configured": bool(provider_configured),
                     "reachable": provider_reachable},
        "keyring": {"backend": prof["keyring_backend"], "locked": bool(keyring_locked)},
        "daemon": {"running": daemon_running},
        "versions": _versions(),
        "last_error": last_error,
        "problems": problems,
    }
    report["ok"] = not any(p["blocking"] for p in problems)
    return report


def report_exit_code(report: dict) -> int:
    """Nonzero iff any BLOCKING problem is present (AC-U4)."""
    return 1 if any(p.get("blocking") for p in report.get("problems", [])) else 0
