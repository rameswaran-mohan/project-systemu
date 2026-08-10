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


def provider_statuses(config=None, *, probe=None,
                      cache_ttl_s: Optional[float] = None) -> dict:
    """Every provider's MINTED verdict, for the profile / doctor / health page.

    F19 / DEC-43. ``_provider_configured`` used to be
    ``bool(os.environ["OPENROUTER_API_KEY"])`` — a fifth private copy of the
    recipe, and the one behind ``doctor`` telling an operator with a working
    Google key that "LLM provider is not configured (OPENROUTER_API_KEY is
    missing) — nothing can run."

    ``config`` defaults to ``Config.from_env()``, which reads the same
    environment the old predicate did (dotenv already applied at import), so the
    profile stays deterministic and hermetic — it just now scores all five
    providers through the one mint instead of one env var through a proxy.
    Never raises: a mint that cannot run yields no statuses, and every consumer
    below treats that as "not configured", which is fail-closed.
    """
    from systemu.runtime import provider_status as _ps
    try:
        if config is None:
            from sharing_on.config import Config
            config = Config.from_env()
        ttl = _ps.PROBE_CACHE_TTL_S if cache_ttl_s is None else cache_ttl_s
        return _ps.all_provider_statuses(config, probe=probe, cache_ttl_s=ttl)
    except Exception:
        return {}


def _provider_configured(config=None, *, probe=None) -> bool:
    """Is ANY provider usable? Derived from the mint, never from an env var.

    Kept under this name and this zero-arg-callable shape because
    ``tests/test_onthetable_consult.py`` and ``systemu/runtime/table_consult.py``
    both address it as ``platform_profile._provider_configured``.
    """
    from systemu.runtime import provider_status as _ps
    return _ps.any_satisfied(provider_statuses(config, probe=probe))


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


def _pkg_path() -> str:
    """The resolved directory of the systemu package THIS process imported.

    Two installs can share a version and be different code — the live F13
    incident was a stale editable install pointing at a different worktree — so
    the location is part of the build identity, not decoration.
    """
    try:
        import systemu
        f = getattr(systemu, "__file__", None)
        if type(f) is not str:
            return "unknown"
        from pathlib import Path as _P
        return str(_P(f).resolve().parent)
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


def _probe_daemon_state(vault_dir: Optional[str] = None) -> Optional[dict]:
    """THE daemon probe for `doctor` and the dashboard health page — the whole
    projection of the readiness mint, not just its boolean.

    DEC-43: consumes the single readiness mint via ``daemon.get_status`` — whose
    ``running`` key is an alias of ``ready``, i.e. a TCP connection to the
    dashboard port was OBSERVED to succeed. It is deliberately not a second,
    cheaper derivation from the pidfile: doctor used to answer "daemon: running"
    off process liveness alone, and so agreed with `daemon status` that a daemon
    still 12 s away from binding its port was up.

    F13 made the *build* fact ride the same mint, so this returns the dict
    rather than a bool: deriving "is it up?" and "which build is it?" from two
    separate probes is how the two would come to disagree. ``None`` when the
    probe is undeterminable at all.
    """
    try:
        if vault_dir is None:
            from sharing_on.config import Config
            vault_dir = Config.from_env().vault_dir
        from systemu.scheduler.daemon import get_status
        st = get_status(vault_dir)
        return st if type(st) is dict else None
    except Exception:
        return None


def _probe_daemon_running(vault_dir: Optional[str] = None) -> Optional[bool]:
    """Best-effort daemon READINESS. A thin projection of ``_probe_daemon_state``
    (never a second derivation). Returns None if undeterminable."""
    st = _probe_daemon_state(vault_dir)
    return None if type(st) is not dict else bool(st.get("running"))


def _daemon_build_view(state: Optional[dict]) -> dict:
    """F13 render-DATA: which systemu build the daemon is executing.

    ``match`` is TRI-STATE and this function NEVER invents agreement:
      * ``True``  — the daemon recorded exactly the build this process imported
      * ``False`` — SKEW; the daemon is serving different code
      * ``None``  — UNVERIFIED (no probe, or the daemon recorded nothing)

    ``observed`` says whether a tracked daemon process was actually seen, so an
    unverified build is only ever flagged as a problem when there IS a daemon.
    """
    mine_v, mine_p = _pkg_version(), _pkg_path()
    if type(state) is not dict:
        return {"observed": False, "match": None, "note": "",
                "daemon_version": None, "daemon_path": None,
                "cli_version": mine_v, "cli_path": mine_p}
    m = state.get("build_match")
    note = state.get("build_note")
    dv = state.get("daemon_version")
    dp = state.get("daemon_path")
    cv = state.get("cli_version")
    cp = state.get("cli_path")
    return {
        "observed": bool(state.get("process_alive")),
        "match": m if type(m) is bool else None,
        "note": note if type(note) is str else "",
        "daemon_version": dv if type(dv) is str else None,
        "daemon_path": dp if type(dp) is str else None,
        "cli_version": cv if type(cv) is str else mine_v,
        "cli_path": cp if type(cp) is str else mine_p,
    }


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
                        daemon_state: Optional[dict] = None,
                        last_error: Optional[str] = None,
                        vault_dir: Optional[str] = None,
                        platform_str: Optional[str] = None,
                        in_container: Optional[bool] = None,
                        config=None, provider_probe=None) -> dict:
    """The self-diagnosis report. Pure given its inputs; every probe is
    injectable so tests drive killed/locked states deterministically.

    F19: ``report["providers"]`` carries EVERY provider's minted verdict, and
    ``report["provider"]["configured"]`` is derived from those same values — so
    ``doctor``'s summary row and its per-provider table cannot disagree, and
    neither can disagree with the dashboard.
    """
    from systemu.runtime import provider_status as _ps

    statuses = provider_statuses(config, probe=provider_probe)
    if provider_configured is None:
        provider_configured = _ps.any_satisfied(statuses)
    if provider_reachable is None:
        provider_reachable = _probe_provider_reachable()
    if keyring_locked is None:
        keyring_locked = _probe_keyring_locked()
    # ONE daemon probe feeds BOTH "is it up?" and "which build is it?" (DEC-43).
    # Skipped entirely when a caller/test has already injected the verdict, so
    # the hermetic doctor/health tests stay free of real socket I/O.
    if daemon_state is None and daemon_running is None:
        daemon_state = _probe_daemon_state(vault_dir)
    if daemon_running is None:
        daemon_running = (bool(daemon_state.get("running"))
                          if type(daemon_state) is dict else None)
    daemon_build = _daemon_build_view(daemon_state)
    if last_error is None:
        last_error = _last_error()

    prof = platform_profile(platform_str=platform_str, in_container=in_container,
                            provider_configured=provider_configured)
    problems = []

    # -- LLM provider (BLOCKING) ------------------------------------------
    # F19: the message names every provider the operator could configure, not
    # only the one this check used to read. Generated from PROVIDER_SPECS.
    if not provider_configured:
        problems.append({
            "id": "provider_absent", "severity": "danger", "blocking": True,
            "message": "No LLM provider is usable — nothing can run.",
            "cta": _ps.configure_hint(statuses) + " Then restart the daemon.",
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
            "cta": "Start it: systemu daemon start",
        })

    # -- F13 daemon BUILD SKEW (non-blocking warning) ----------------------
    # Loud, but never blocking: a user mid-upgrade must still be able to run
    # `daemon stop`, and `doctor` exiting nonzero is a blocking signal.
    if daemon_build["match"] is False:
        problems.append({
            "id": "daemon_build_skew", "severity": "warning", "blocking": False,
            "message": (daemon_build["note"]
                        or "The daemon is executing a different systemu build "
                           "than this CLI."),
            "cta": "Restart the daemon: systemu daemon stop, then "
                   "systemu daemon start.",
        })
    elif daemon_build["observed"] and daemon_build["match"] is None:
        problems.append({
            "id": "daemon_build_unverified", "severity": "warning",
            "blocking": False,
            "message": (daemon_build["note"]
                        or "The daemon did not record which systemu build it "
                           "loaded, so it cannot be compared with this CLI."),
            "cta": "Restart the daemon: systemu daemon stop, then "
                   "systemu daemon start.",
        })

    # -- F21 optional capability groups (non-blocking warning) -------------
    # A pure-CLI operator who never wanted the dashboard must not see `doctor`
    # exit nonzero, so these are warnings by construction. They are reported at
    # all because "the dashboard URL does nothing" and "web_read says it cannot
    # run" are the two questions a slimmer default install creates, and the
    # answer to both is one line the operator can copy.
    from systemu.runtime import optional_deps as _od
    optional_groups = _od.group_status()
    for _g in optional_groups:
        if _g["installed"]:
            continue
        problems.append({
            "id": f"optional_group_missing:{_g['extra']}",
            "severity": "warning", "blocking": False,
            "message": (f"{_g['label']} is not installed — {_g['covers']} "
                        f"cannot run. This is optional; nothing else is affected."),
            "cta": _g["remedy"],
        })

    report = {
        "profile": prof,
        "provider": {"configured": bool(provider_configured),
                     "reachable": provider_reachable},
        # F21: the same rows the dashboard health page and /health read, minted
        # once here so no surface can disagree about which extras are present.
        "optional_groups": optional_groups,
        # F19: the per-provider table `doctor` renders. Plain dicts, so the
        # report stays JSON-serialisable for /health; `satisfied` is copied from
        # the minted value, never recomputed from `state` by a consumer.
        "providers": [{"provider": s.provider, "display": s.display,
                       "env": s.env, "rule": s.rule, "state": s.state,
                       "detail": s.detail, "satisfied": s.satisfied}
                      for s in (statuses.get(spec.provider)
                                for spec in _ps.PROVIDER_SPECS)
                      if s is not None],
        "keyring": {"backend": prof["keyring_backend"], "locked": bool(keyring_locked)},
        "daemon": {"running": daemon_running, "build": daemon_build},
        "versions": _versions(),
        "last_error": last_error,
        "problems": problems,
    }
    report["ok"] = not any(p["blocking"] for p in problems)
    return report


def report_exit_code(report: dict) -> int:
    """Nonzero iff any BLOCKING problem is present (AC-U4)."""
    return 1 if any(p.get("blocking") for p in report.get("problems", [])) else 0
