"""The provider verdict, taken OFF the render path (D4).

WHY THIS MODULE EXISTS
----------------------
Dogfooding the shipped 0.10.28 wheel produced this stack, captured while the
dashboard renderer was frozen::

    _build_layout -> render_health_banner -> build_health_state
                  -> provider_status -> probe_ollama -> urllib ... sock.connect

``_build_layout`` runs on EVERY dashboard route, so every page paint waited on a
loopback TCP connect. ``provider_status`` measures that cost in its own
docstring: 1.688 s on the shipped dual-stack ``localhost`` default with Ollama
genuinely RUNNING, ~2 s when nothing is listening (Windows silently DROPS a
closed loopback port rather than refusing it). It correlated with repeated
renderer freezes -- 30 s screenshot timeouts, a "Connection lost" flash, and
clicks that were eaten.

The 20 s memo inside ``provider_status`` bounds how OFTEN that is paid, not
WHERE: the first render after every expiry still pays it, in the render.

THE PROPERTY
------------
    NO NETWORK PROBE RUNS ON THE RENDER PATH.

    ``provider_status.all_provider_statuses`` remains THE ONE MINT (DEC-43).
    This module does not re-derive a single verdict; it decides only WHEN the
    mint is called and WHO waits for it. A background refresher spends the
    witness on its own thread and publishes a :class:`HealthSnapshot`; the
    render READS that snapshot.

A VERDICT IS ABOUT A CONFIG
---------------------------
A snapshot carries a ``config_key`` -- a fingerprint of the provider-relevant
config it was minted from -- and a reader that asks for a different config gets
nothing back. Without that, the Settings page rendering for the config it was
handed would answer for the daemon's, which is DEC-43 form (ii): a claim
derived from a proxy for the thing actually asked about. The fingerprint
records SET/UNSET per credential and never the credential itself (DEC-31).

WHAT A MISSING SNAPSHOT MEANS
-----------------------------
"We have not asked yet" -- a third answer, distinct from both "it is down" and
"it is configured", exactly as ``provider_status`` keeps ``unknown`` distinct
from ``unreachable``. The banner renders it as "Checking providers..." and
schedules a refresh. It NEVER decays to green: nothing here can mint a
satisfied verdict, because nothing here mints at all.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: How often the background refresher spends the witness. 30 s is far longer
#: than the probe costs and far shorter than an operator's patience with a
#: stale verdict; the banner discloses the age past this bound either way.
DEFAULT_REFRESH_INTERVAL_S = 30.0

#: Beyond this a snapshot is not replayed at all. A dashboard left open
#: overnight beside a refresher that died must not answer from yesterday.
MAX_REPLAY_AGE_S = 600.0

#: The env override, for an operator on a slow box or a test that wants a
#: tighter loop. An unparseable value keeps the default rather than raising.
REFRESH_INTERVAL_ENV = "SYSTEMU_PROVIDER_REFRESH_S"


def refresh_interval_s() -> float:
    """The configured interval, or the default. Never raises."""
    raw = os.environ.get(REFRESH_INTERVAL_ENV, "")
    if type(raw) is not str or not raw.strip():
        return DEFAULT_REFRESH_INTERVAL_S
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_INTERVAL_S
    return value if 0.05 <= value <= 3600.0 else DEFAULT_REFRESH_INTERVAL_S


@dataclass(frozen=True)
class HealthSnapshot:
    """One OBSERVED round of provider verdicts, and when it was taken.

    ``statuses`` holds ``provider_status.ProviderStatus`` objects exactly as the
    mint produced them -- not a re-derivation, not a summary. ``satisfied`` is
    still the mint's own derived property, so nothing downstream can assert a
    capability the state does not support.
    """

    taken_at: float                       # time.monotonic() at the mint
    statuses: Dict[str, object] = field(default_factory=dict)
    interval_s: float = DEFAULT_REFRESH_INTERVAL_S
    config_key: str = ""

    def age_s(self, now: Optional[float] = None) -> float:
        """Seconds since the witness was spent. Never negative."""
        current = time.monotonic() if now is None else now
        return max(0.0, float(current) - float(self.taken_at))

    @property
    def stale(self) -> bool:
        """Older than the interval it was supposed to be refreshed within."""
        return self.age_s() > float(self.interval_s)


# ── the config fingerprint ──────────────────────────────────────────────────

def config_key(config) -> str:
    """A fingerprint of the PROVIDER-RELEVANT config. Holds no credential.

    Per credential provider it records only SET or UNSET; the keyless provider
    contributes its base URL, which is not a secret (the Settings page prints
    it) and IS the thing whose reachability the verdict is about. DEC-31: a
    secret must never reach a cache key, a log line, or an error message.

    Never raises: a config that cannot be read fingerprints as unreadable, and
    an unreadable config can only ever FAIL to match a stored snapshot.
    """
    from systemu.runtime import provider_status as _ps
    parts = []
    for spec in _ps.PROVIDER_SPECS:
        try:
            raw = getattr(config, spec.attr, "")
        except Exception:
            raw = ""
        value = raw.strip() if type(raw) is str else ""
        if spec.rule == _ps.RULE_CREDENTIAL:
            parts.append(spec.provider + "=" + ("set" if value else "unset"))
        else:
            parts.append(spec.provider + "=" + value)
    return "|".join(parts)


def resolve_config(config=None):
    """The config the daemon's provider verdict is about, env overlay applied.

    Lifted verbatim in behaviour from ``health_banner._provider_statuses`` so
    the refresher scores exactly what the render used to. THE DAEMON'S
    ENVIRONMENT is what this is about: a long-lived AppState snapshot can be
    older than the .env the operator just edited.
    """
    from systemu.runtime import provider_status as _ps
    if config is None:
        # Its OWN try: an AppState that is not up yet (early boot, a unit call)
        # must not abort the lookup and report "no provider" for a machine that
        # plainly has one.
        try:
            from systemu.interface.dashboard_state import AppState
            config = getattr(AppState.get(), "config", None)
        except Exception:
            config = None
    if config is None:
        try:
            from sharing_on.config import Config
            config = Config.from_env()
        except Exception:
            return None
    try:
        return _ps.env_overlay(config)
    except Exception:
        return config


# ── the store ───────────────────────────────────────────────────────────────

_lock = threading.RLock()
_snapshot: Optional[HealthSnapshot] = None
_refresher: dict = {"thread": None, "stop": None, "wake": None}


def current_snapshot(key: Optional[str] = None, *,
                     _now: Optional[float] = None) -> Optional[HealthSnapshot]:
    """The published snapshot, or None -- "we have not asked yet".

    ``key`` is a :func:`config_key`. A snapshot minted about a DIFFERENT config
    is not an answer about this one and is withheld, as is one past
    ``MAX_REPLAY_AGE_S``. Both refusals can only WITHHOLD a claim.
    """
    with _lock:
        snap = _snapshot
    if snap is None:
        return None
    if snap.age_s(_now) > MAX_REPLAY_AGE_S:
        return None
    if key is not None and snap.config_key != key:
        return None
    return snap


def clear() -> None:
    """Drop the published snapshot (tests; also after a config edit)."""
    global _snapshot
    with _lock:
        _snapshot = None


def refresh_now(config, *, probe=None, timeout: Optional[float] = None,
                interval_s: Optional[float] = None, cache_ttl_s: float = 0.0,
                _now: Optional[float] = None) -> HealthSnapshot:
    """Spend the witness NOW and publish the result. NEVER call from a render.

    THE ONE PLACE the banner's verdict reaches ``all_provider_statuses`` with a
    real witness. Returns the snapshot it published; on a mint failure it
    returns an EMPTY snapshot and publishes nothing, so a blown-up probe leaves
    the reader at "we have not asked yet" rather than at a manufactured "no
    provider".

    ``config`` is scored exactly as handed over -- no overlay is applied here.
    The caller decides which config the verdict is about (see
    :func:`resolve_config` for the daemon's own).

    ``cache_ttl_s`` is the mint's own short memo, passed straight through. The
    background refresher leaves it at 0 -- its whole job is to spend a FRESH
    observation -- while a caller that is itself on a render (the /welcome
    wizard's step 1) keeps the memo it has always had.
    """
    global _snapshot
    from systemu.runtime import provider_status as _ps
    taken_at = time.monotonic() if _now is None else float(_now)
    every = refresh_interval_s() if interval_s is None else float(interval_s)
    key = config_key(config)
    try:
        kwargs = {} if timeout is None else {"timeout": float(timeout)}
        statuses = _ps.all_provider_statuses(config, probe=probe,
                                             cache_ttl_s=cache_ttl_s, **kwargs)
    except Exception:
        logger.debug("[ProviderSnapshot] mint failed", exc_info=True)
        return HealthSnapshot(taken_at=taken_at, statuses={},
                              interval_s=every, config_key=key)
    if type(statuses) is not dict or not statuses:
        return HealthSnapshot(taken_at=taken_at, statuses={},
                              interval_s=every, config_key=key)
    snap = HealthSnapshot(taken_at=taken_at, statuses=statuses,
                          interval_s=every, config_key=key)
    with _lock:
        _snapshot = snap
    return snap


# ── the background refresher ────────────────────────────────────────────────

def _default_config_fn():
    return resolve_config(None)


def _loop(config_fn: Callable, interval_s: float,
          stop_ev: threading.Event, wake_ev: threading.Event) -> None:
    """Refresh, then wait for the interval OR an on-demand nudge. Never raises
    out: this thread dying silently would strand the banner at "checking".

    The nudge is CLEARED BEFORE the refresh, not after. Clearing it afterwards
    swallows every nudge that arrived while the probe was in flight -- and a
    probe is seconds long, which is exactly when a Re-check is most likely to
    be clicked. Clearing first costs at most one extra round.
    """
    while not stop_ev.is_set():
        wake_ev.clear()
        try:
            config = config_fn()
        except Exception:
            config = None
        if config is not None:
            try:
                refresh_now(config, interval_s=interval_s)
            except Exception:
                logger.debug("[ProviderSnapshot] refresh failed", exc_info=True)
        wake_ev.wait(interval_s)


def start_refresher(config_fn: Optional[Callable] = None,
                    interval_s: Optional[float] = None) -> bool:
    """Start the once-per-process refresher thread. True if THIS call started it.

    Idempotent and cheap enough to call from a render: an already-running
    refresher costs a lock and a flag read. The thread is a daemon thread, so
    it can never hold the process open.
    """
    global _refresher
    every = refresh_interval_s() if interval_s is None else float(interval_s)
    fn = _default_config_fn if config_fn is None else config_fn
    with _lock:
        thread = _refresher.get("thread")
        if thread is not None and thread.is_alive():
            return False
        stop_ev, wake_ev = threading.Event(), threading.Event()
        thread = threading.Thread(
            target=_loop, args=(fn, every, stop_ev, wake_ev),
            name="systemu-provider-refresh", daemon=True)
        _refresher = {"thread": thread, "stop": stop_ev, "wake": wake_ev}
        thread.start()
        return True


def request_refresh() -> bool:
    """Ask the refresher to take a fresh snapshot. NEVER blocks, never probes.

    This is what an on-demand trigger (the /welcome Re-check button) and a
    render that found no snapshot both use. False means no refresher is
    running, so nobody was nudged -- the caller has not been told a refresh is
    coming when it is not.
    """
    with _lock:
        thread = _refresher.get("thread")
        wake_ev = _refresher.get("wake")
    if thread is None or wake_ev is None or not thread.is_alive():
        return False
    wake_ev.set()
    return True


def stop_refresher(timeout: float = 5.0) -> None:
    """Stop the refresher thread and wait for it (tests, shutdown)."""
    global _refresher
    with _lock:
        thread = _refresher.get("thread")
        stop_ev = _refresher.get("stop")
        wake_ev = _refresher.get("wake")
        _refresher = {"thread": None, "stop": None, "wake": None}
    if stop_ev is not None:
        stop_ev.set()
    if wake_ev is not None:
        wake_ev.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout)
