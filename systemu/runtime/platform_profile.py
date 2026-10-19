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
#: Windows Credential Manager. The Windows keyring row reads ``dpapi`` only off a
#: WORKING envelope (S5 / R-A3); without one, a usable keyring is named for what
#: it is -- on a Windows host, this.
KEYRING_WINCRED = "wincred"

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


def _envelope_status():
    """The at-rest envelope's WORKING status: a real protect/unprotect round trip
    through the seam secrets use (``at_rest.envelope_status``), never an import
    check. Returns ``(working, reason)``; never raises."""
    try:
        from systemu.runtime.credentials.at_rest import envelope_status
        st = envelope_status()
        working = st.working is True
        reason = st.reason if type(st.reason) is str else ""
        return working, ("" if working else (reason or "the envelope probe failed"))
    except Exception as exc:
        return False, f"the envelope probe could not run ({type(exc).__name__})"


# ── the secrets row (2d) ─────────────────────────────────────────────────────
SECRETS_DPAPI = "dpapi"
SECRETS_KEYRING = "keyring"
SECRETS_PLAINTEXT = "plaintext"


def _secrets_witness(platform_str: str, usable_fn: Callable[[], object],
                     envelope_fn: Callable[[], tuple]) -> tuple:
    """ONE reading of each seam both secrets rows derive from (S5 / R-A3).

    Returns ``(keyring, working, why)``: the usable keyring object (or None), and
    whether the at-rest envelope round trip WORKS, with the reason when it does
    not. The envelope is probed only on a Windows leg -- there is no DPAPI
    elsewhere -- and exactly once, so ``secrets_at_rest`` and ``keyring_backend``
    cannot come from two probes that disagree. Never raises.
    """
    try:
        kr = usable_fn()
    except Exception:
        kr = None
    if not platform_str.startswith("win"):
        return kr, False, ""
    try:
        working, why = envelope_fn()
    except Exception as exc:
        return kr, False, f"the envelope probe could not run ({type(exc).__name__})"
    return kr, working is True, (why if type(why) is str else "")


def secrets_at_rest(platform_str: Optional[str] = None, *,
                    usable: Optional[Callable[[], object]] = None,
                    envelope: Optional[Callable[[], tuple]] = None) -> dict:
    """What actually protects the secrets this product persists.

    ``{"backend", "label", "reason"}`` where ``backend`` is one of
    ``dpapi`` / ``keyring`` / ``plaintext`` and ``label`` is what a surface
    prints (``plaintext (<reason>)`` for the last). A claim only a working
    envelope can earn: ``dpapi`` requires Windows AND a DPAPI round trip that
    succeeded just now. Without one, a usable OS keyring still holds the
    credential store's secrets (``keyring``, with the reason naming what stays
    plaintext); with neither, it is ``plaintext`` with the reason. Never raises.
    """
    platform_str = platform_str or sys.platform
    return _secrets_row(platform_str, _secrets_witness(
        platform_str,
        usable if usable is not None else _usable_keyring,
        envelope if envelope is not None else _envelope_status))


def _secrets_row(platform_str: str, witness: tuple) -> dict:
    """The ``secrets_at_rest`` row from one :func:`_secrets_witness` reading."""
    kr, working, why = witness
    has_keyring = kr is not None

    if platform_str.startswith("win"):
        if working is True:
            return {"backend": SECRETS_DPAPI, "label": SECRETS_DPAPI, "reason": ""}
        why = why or "the DPAPI envelope is not working"
        if has_keyring:
            reason = (f"DPAPI envelope unavailable: {why}; credentials are in the "
                      "OS keyring, but OAuth tokens and dashboard secrets are "
                      "stored as plaintext files")
            return {"backend": SECRETS_KEYRING, "label": SECRETS_KEYRING,
                    "reason": _ascii(reason)}
        reason = _ascii(f"DPAPI envelope unavailable: {why}; no OS keyring backend")
        return {"backend": SECRETS_PLAINTEXT,
                "label": f"{SECRETS_PLAINTEXT} ({reason})", "reason": reason}

    # POSIX: there is no DPAPI; the 0600 file mode is the control for files.
    if has_keyring:
        return {"backend": SECRETS_KEYRING, "label": SECRETS_KEYRING, "reason": ""}
    reason = "no OS keyring backend; secret files are protected only by 0600 mode"
    return {"backend": SECRETS_PLAINTEXT,
            "label": f"{SECRETS_PLAINTEXT} ({reason})", "reason": reason}


def _ascii(text: str) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


#: keyring's own backend layout (``keyring.backends.<name>``) -> the enum. Used
#: only to NAME a usable keyring on a Windows leg whose envelope does not work.
_KEYRING_BY_MODULE = (
    ("keyring.backends.windows", KEYRING_WINCRED),
    ("keyring.backends.macos", KEYRING_KEYCHAIN),
    ("keyring.backends.secretservice", KEYRING_SECRETSERVICE),
    ("keyring.backends.libsecret", KEYRING_SECRETSERVICE),
    ("keyring.backends.kwallet", KEYRING_SECRETSERVICE),
)


def _keyring_named_by_itself(kr) -> Optional[str]:
    """The enum for the backend ``kr`` actually is, read off its backend class's
    module; None when it does not say. ``kr`` is the ``keyring`` module the
    usability probe returns (its ``get_keyring()`` is the backend) or a backend.
    Never raises."""
    try:
        get = getattr(kr, "get_keyring", None)
        backend = get() if callable(get) else kr
        module = type(backend).__module__
    except Exception:
        return None
    if type(module) is not str:
        return None
    module = module.lower()
    for prefix, name in _KEYRING_BY_MODULE:
        if module == prefix or module.startswith(prefix + "."):
            return name
    return None


def _keyring_row(platform_str: str, witness: tuple, secrets_row: dict) -> str:
    """The ``keyring_backend`` row from the SAME witness as ``secrets_row``.

    On Windows ``dpapi`` is read off the secrets row -- the one place a WORKING
    envelope earns it -- so the two rows cannot disagree about it.
    """
    kr = witness[0]
    if platform_str.startswith("win"):
        if secrets_row.get("backend") == SECRETS_DPAPI:
            return KEYRING_DPAPI
        if kr is None:
            return KEYRING_PLAINTEXT
        # Named by the keyring in hand, so a simulated Windows leg on another
        # host never names a backend that is not there; a Windows host's
        # keyring is Credential Manager.
        return _keyring_named_by_itself(kr) or KEYRING_WINCRED
    if kr is None:
        return KEYRING_PLAINTEXT
    if platform_str == "darwin":
        return KEYRING_KEYCHAIN
    return KEYRING_SECRETSERVICE


def keyring_backend(platform_str: Optional[str] = None, *,
                    usable: Optional[Callable[[], object]] = None,
                    dpapi: Optional[Callable[[], bool]] = None,
                    envelope: Optional[Callable[[], tuple]] = None) -> str:
    """Resolve the effective secret-at-rest backend to the STABLE enum.

    S5 / R-A3 -- truthful. Windows reports ``dpapi`` ONLY when the DPAPI at-rest
    envelope round trip works -- the same witness, and the same derivation, as
    the ``secrets_at_rest`` row (a usable Credential Manager no longer stands in
    for it). Without a working envelope a usable keyring is named for what it is
    (``wincred`` on a Windows host); with neither it is ``plaintext_fallback``.
    macOS -> ``keychain``, Linux/other POSIX -> ``secretservice`` when a real
    backend exists. With no backend at all it degrades to ``plaintext_fallback``
    (a flagged 0600 file) -- the honest, reported last-resort; the reason lives
    on the ``secrets_at_rest`` row.

    ``envelope`` returns ``(working, reason)``; ``dpapi`` is the older bool form
    of the same witness, kept for existing callers.
    """
    platform_str = platform_str or sys.platform
    if envelope is None and dpapi is not None:
        def envelope():
            return ((True, "") if dpapi() is True
                    else (False, "the DPAPI envelope is not working"))
    witness = _secrets_witness(
        platform_str,
        usable if usable is not None else _usable_keyring,
        envelope if envelope is not None else _envelope_status)
    return _keyring_row(platform_str, witness, _secrets_row(platform_str, witness))


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

    # S5 / R-A3: ONE reading of the keyring and envelope seams feeds BOTH
    # secrets rows, so `keyring_backend` and `secrets_at_rest` cannot disagree.
    witness = _secrets_witness(platform_str, _usable_keyring, _envelope_status)
    secrets_row = _secrets_row(platform_str, witness)

    return {
        "os": platform_str,
        "os_family": _os_family(platform_str),
        "arch": arch,
        "python_version": py_version,
        # No host desktop inside a container -> capture is not directly available
        # (it is offered via the Host Companion honesty row instead).
        "capture_available": not in_container,
        "keyring_backend": _keyring_row(platform_str, witness, secrets_row),
        # 2d: what protects the secrets this product persists, derived from a
        # WORKING envelope (a real DPAPI round trip), never from an import.
        "secrets_at_rest": secrets_row,
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


def _probe_secret_backups(vault_dir: Optional[str] = None) -> Optional[list]:
    """S5 / R-A3: the plaintext ``<secret>.pre-encrypt.bak`` copies in the
    operating vault (``at_rest.plaintext_backups``).

    With no ``vault_dir`` the root is the ONE mint the daemon-start
    re-encryption pass itself uses (``resolve_vault_root``), so the doctor looks
    exactly where the backups are written. A cheap local glob; never decrypts.
    None when undeterminable; never raises.
    """
    try:
        from systemu.runtime.credentials import at_rest
        if vault_dir is None:
            from systemu.runtime.vault_root import resolve_vault_root
            vault_dir = resolve_vault_root().root
        return at_rest.plaintext_backups(vault_dir)
    except Exception:
        return None


def _secret_backup_problems(backups) -> list:
    """One non-blocking warning per plaintext backup, naming its path and what
    to do. ASCII; a path, never a value."""
    from systemu.runtime.credentials import at_rest
    rows = []
    for b in backups or ():
        if type(b) is not dict:
            continue
        backup, secret = b.get("backup"), b.get("secret_file")
        relative, state = b.get("relative"), b.get("secret_state")
        if not all(type(x) is str for x in (backup, secret, relative, state)):
            continue
        if state == at_rest.SECRET_ENCRYPTED:
            message = (f"A plaintext copy of a secret is on disk: {backup}. The "
                       f"secret file it copies ({secret}) is encrypted; this copy "
                       "is not.")
            cta = ("Check that systemu still works with the encrypted file, then "
                   f"delete {backup}.")
        elif state == at_rest.SECRET_MISSING:
            message = (f"A plaintext copy of a secret is on disk: {backup}. The "
                       f"secret file it copied ({secret}) no longer exists.")
            cta = f"If you no longer need that secret, delete {backup}."
        else:
            message = (f"A secret file was NOT re-encrypted and is still plaintext: "
                       f"{secret}. Its plaintext backup is kept at {backup}.")
            cta = ("Restart the daemon to retry: it re-encrypts the file and checks "
                   "the result before replacing it (the daemon log's [S5] line says "
                   "why a try failed). Keep the backup until the file is encrypted, "
                   f"then delete {backup}.")
        rows.append({
            "id": f"secret_plaintext_backup:{relative}", "severity": "warning",
            "blocking": False, "message": _ascii(message), "cta": _ascii(cta),
        })
    return rows


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


def _provider_sdk_groups() -> dict:
    """extra -> the provider ids whose SDK that optional group ships.

    e2e31. DERIVED, never declared: the providers registry's ``sdk_package``
    joined to ``optional_deps``' group registry. A hand-written "anthropic is a
    provider group" list here would be a second table of which provider needs
    which package -- the split the welcome page's retired clause was.

    A group absent from the answer is a CAPABILITY group and keeps the
    unconditional rule, which is also where every failure lands: if the
    providers registry cannot be imported, or ``sdk_package`` raises or hands
    back a non-``str`` for ANY provider, this answers ``{}``. An unread row
    could map to any group, so no group can be gated on the rows that did read.
    """
    from systemu.runtime import optional_deps as _od
    from systemu.runtime import provider_status as _ps
    out: dict = {}
    try:
        from systemu.llm import providers as _providers
        for spec in _ps.PROVIDER_SPECS:
            package = _providers.sdk_package(spec.provider)
            if type(package) is not str:
                return {}
            if not package:
                continue                 # needs no optional SDK
            group = _od.group_for_package(package)
            if group is None:
                continue                 # no extra ships it: nothing to gate
            out.setdefault(group.extra, []).append(spec.provider)
    except Exception:
        return {}
    return {extra: tuple(pids) for extra, pids in out.items()}


def _provider_unconfigured(statuses, provider: str) -> bool:
    """Did the mint OBSERVE that nobody configured ``provider``?

    True only for the mint's own verdict that the credential is absent: state
    ``missing`` with an empty blocker (a set key whose SDK is absent is
    ``missing`` WITH a blocker -- that operator did choose the provider).
    Anything else -- no status, a status that is not the mint's type, a field
    of the wrong concrete type -- is not that observation, and answers False so
    the warning stays: suppressing it is the "looks fine" direction.
    """
    from systemu.runtime import provider_status as _ps
    st = statuses.get(provider) if type(statuses) is dict else None
    if type(st) is not _ps.ProviderStatus:
        return False
    state, blocker = st.state, st.blocker
    return (type(state) is str and state == _ps.STATE_MISSING
            and type(blocker) is str and blocker == "")


def build_doctor_report(*, provider_configured: Optional[bool] = None,
                        provider_reachable: Optional[bool] = None,
                        keyring_locked: Optional[bool] = None,
                        daemon_running: Optional[bool] = None,
                        daemon_state: Optional[dict] = None,
                        last_error: Optional[str] = None,
                        vault_dir: Optional[str] = None,
                        platform_str: Optional[str] = None,
                        in_container: Optional[bool] = None,
                        config=None, provider_probe=None,
                        secret_backups: Optional[list] = None) -> dict:
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

    # -- 2d: a Windows envelope that does not WORK (non-blocking warning) --
    # The keyring row above can read `dpapi` (Credential Manager) while the
    # file-held secrets -- OAuth tokens, dashboard secrets -- sit in plaintext
    # because the DPAPI envelope is absent or broken. Never silent.
    _sec = prof.get("secrets_at_rest") or {}
    if prof["os_family"] == "windows" and _sec.get("backend") != SECRETS_DPAPI:
        problems.append({
            "id": "secrets_envelope_unavailable", "severity": "warning",
            "blocking": False,
            "message": _ascii("Secret files are not encrypted at rest: "
                              + str(_sec.get("reason") or "the DPAPI envelope "
                                    "is not working") + "."),
            "cta": "Reinstall systemu so its pywin32 dependency is present "
                   "(pip install --force-reinstall systemu), then restart the "
                   "daemon; plaintext secrets are re-encrypted at daemon start.",
        })

    # -- S5 / R-A3: plaintext backups of secrets (non-blocking warning) ----
    # The daemon-start pass copies a plaintext secret to `<file>.pre-encrypt.bak`
    # before encrypting it, and KEEPS the copy when the new envelope does not
    # verify. Each copy is a plaintext secret on disk; never silent.
    if secret_backups is None:
        secret_backups = _probe_secret_backups(vault_dir)
    problems.extend(_secret_backup_problems(secret_backups))

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
    #
    # e2e31: a group that ships a PROVIDER SDK is a problem only for the
    # operator who chose that provider. Reported unconditionally, `[anthropic]`
    # put a WARNINGS pill on /health for every install running on OpenRouter or
    # Google. The group stays LISTED in `optional_groups` either way
    # (never-subtract); only the problem row is gated. An operator who set the
    # key is also told by the provider row itself, which carries the remedy.
    from systemu.runtime import optional_deps as _od
    optional_groups = _od.group_status()
    provider_groups = _provider_sdk_groups()
    for _g in optional_groups:
        if _g["installed"]:
            continue
        served = provider_groups.get(_g["extra"], ())
        if served and all(_provider_unconfigured(statuses, p) for p in served):
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
