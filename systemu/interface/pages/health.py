"""R-UX1 — the /health page (SPEC §15-UX UX-4/UX-6).

A dedicated self-diagnosis page (distinct from the passive per-page
``health_banner``): it renders the ONE deterministic platform capability profile
plus live provider / keyring / daemon status and the DEP-10 host-capability
honesty rows, topped by a load/status chip.

The render is split into a pure data helper ``health_view() -> dict`` (unit
tested without a NiceGUI runtime) and a thin ``build_health_page()`` renderer
that composes design-token classes only (no inline f-string styles / raw hex —
the UI-style lint gate stays green).
"""
from __future__ import annotations

from systemu.runtime import platform_profile as pp

# chip → design-token status-pill class (success/warn/danger are token colours)
_CHIP_PILL = {
    "ok": "s-pill s-pill--success",
    "warn": "s-pill s-pill--warn",
    "danger": "s-pill s-pill--danger",
}
_CHIP_LABEL = {"ok": "HEALTHY", "warn": "WARNINGS", "danger": "PROBLEM"}

# R-UX2 / UX-9(f) — the load chip is a SEPARATE axis from the status chip: a
# loaded box is still a healthy install, so "busy" must never read as "broken".
_LOAD_PILL = {
    "normal": "s-pill s-pill--success",
    "busy": "s-pill s-pill--warn",
    "unknown": "s-pill s-pill--muted",
}
_LOAD_LABEL = {
    "normal": "RESPONSIVE",
    "busy": "UNDER LOAD",
    "unknown": "LOAD NOT MEASURED",
}


def _cpu_bit(snap: dict) -> str:
    """The CPU clause of the chip's justification — ALWAYS a clause.

    Silence is not a safe default for an input to a load verdict: omitting the
    CPU bit made a half-observed chip look fully observed. An unmeasured CPU is
    stated, and the window it covers is the MEASURED span from the snapshot, not
    a nominal figure.
    """
    cpu = snap.get("cpu_percent")
    if cpu is None:
        return "CPU not measured"
    window = snap.get("cpu_window_s")
    readings = int(snap.get("cpu_readings") or 0)
    if window:
        return f"CPU {cpu:.0f}% (mean of {readings} readings over {window:.0f}s)"
    return f"CPU {cpu:.0f}% ({readings} reading(s))"


def _recent_numbers(snap: dict) -> str:
    """The ``recent_*`` justification line, or "" when that window is unmeasured.

    Read ONLY from ``recent_*`` — the one window ``load_state`` is computed
    from. This is the rule the three-window chip broke.
    """
    if snap.get("recent_p95_ms") is None:
        return ""
    breach_ms = float(snap.get("breach_ms") or 0.0)
    recent_breaches = int(snap.get("recent_breaches") or 0)
    bits = [
        f"UI lag p95 {snap['recent_p95_ms']:.0f}ms",
        _cpu_bit(snap),
        (f"{recent_breaches} stall(s) over {breach_ms:.0f}ms"
         if recent_breaches else f"no stalls over {breach_ms:.0f}ms"),
    ]
    return (f"Last {float(snap['recent_window_s']):.0f}s "
            f"({snap['recent_samples']} samples): " + " · ".join(bits) + ".")


def _load_chip(snap: dict) -> dict:
    """Render-data for the UX-9(f) "system under load" chip.

    The chip's job is to EXPLAIN residual slowness, so the numbers behind it are
    part of the output, not just a colour. That is exactly where the first cut of
    this function went wrong, and the rule it now follows is narrow and literal:

        **every number printed as the justification for the announced state is
        read from ``recent_*``** — the one window ``load_state`` is computed
        from — and any figure from a different window is printed only under an
        explicit label that names that window ("Since start: …").

        The chip previously read ``p95_ms`` (the whole ~2-minute ring) and
        ``breaches`` (an all-time counter) while announcing a state derived from
        the last ~5 seconds, and so rendered ``RESPONSIVE :: UI lag p95 900ms ·
        200 stall(s)``. There is no longer a ``p95_ms`` key to read.

    ``load_state == "unknown"`` is its own state and never renders as
    "responsive" — with no live data we do not know the UI is responsive, and
    this page is reachable precisely when the install is broken.

    An input we did not measure is DECLARED, never dropped. The CPU bit used to
    be appended only ``if cpu is not None``, so an unmeasured CPU vanished from
    the justification and the chip read as though load had been assessed on
    everything it names. Now it says so — and because the state itself already
    withholds "responsive" when CPU is unmeasured, the two agree.
    """
    state = snap.get("load_state")
    if state not in _LOAD_PILL:
        state = "unknown"
    if state == "unknown":
        reason = snap.get("load_reason") or "the watchdog has not reported"
        detail = f"UI responsiveness is unknown — {reason}."
        if snap.get("stale"):
            detail += (" The meter has stopped, or the loop is blocked right "
                       "now. Either way this is not a claim that the UI is "
                       "fine.")
        # The lag half may be genuinely measured even when the composite is
        # not (CPU unmeasured). Those numbers are real, so print them — under
        # a state that still refuses to claim the box is fine.
        numbers = _recent_numbers(snap)
        if numbers:
            detail += " " + numbers
        return {"state": "unknown", "label": _LOAD_LABEL["unknown"],
                "detail": detail}

    # ONE builder for the justification line, shared with the "unknown" branch
    # above — two constructions kept in step by hand is how the CPU clause came
    # to differ between them in the first place.
    numbers = _recent_numbers(snap)
    since = (f" Since start: {int(snap.get('session_breaches') or 0)} stall(s) "
             f"in {int(snap.get('session_samples') or 0)} sample(s).")
    if state == "busy":
        return {
            "state": "busy",
            "label": _LOAD_LABEL["busy"],
            "detail": ("Heavy work is running, so the dashboard may feel slow. "
                       "Your actions are still being accepted. " + numbers + since),
        }
    return {"state": "normal", "label": _LOAD_LABEL["normal"],
            "detail": numbers + since}


def health_view(*, load: "dict | None" = None, **overrides) -> dict:
    """Pure render-DATA for /health. Accepts the same injectable states as
    ``build_doctor_report`` so it is deterministic + hermetic in tests.

    Returns the platform profile, provider/keyring/daemon status, the DEP-10
    honesty rows, a ``status_chip`` (ok | warn | danger) and — R-UX2 / UX-9(f) —
    the loop-lag ``load`` snapshot with its ``load_chip``.

    ``load`` defaults to the live loop-lag watchdog's snapshot; tests inject it.
    The watchdog read is in-memory only (a bounded ring buffer), so rendering
    /health does no disk I/O for it — this page must not itself be the thing
    that blocks the loop it is reporting on.
    """
    from systemu.runtime.loop_lag import get_watchdog

    report = pp.build_doctor_report(**overrides)
    prof = report["profile"]
    if not report["ok"]:
        chip = "danger"
    elif any(p["severity"] == "warning" for p in report["problems"]):
        chip = "warn"
    else:
        chip = "ok"
    snap = load if load is not None else get_watchdog().snapshot()
    return {
        "profile": prof,
        "provider": report["provider"],
        "keyring": report["keyring"],
        "daemon": report["daemon"],
        "versions": report["versions"],
        "problems": report["problems"],
        "honesty_rows": prof["host_capabilities"],
        # F21: the same rows `doctor` prints, from the same mint. A dashboard
        # operator can be missing [browser] while [dashboard] is present, so
        # "which of my capabilities are not installed" is a live question on
        # this page too — and F19's lesson was that two surfaces answering the
        # same question from two derivations eventually disagree.
        "optional_groups": report["optional_groups"],
        "last_error": report.get("last_error"),
        "status_chip": chip,
        "load": snap,
        "load_chip": _load_chip(snap),
        "ok": report["ok"],
    }


def secrets_at_rest_text(prof) -> str:
    """S5 / R-A3 -- the capability table's "Secrets at rest" value.

    The ``secrets_at_rest`` row's own label, read from the profile in the view
    this page was built from (never a probe on render). A row this function
    cannot read is "unknown", never a claim. ASCII only.
    """
    row = prof.get("secrets_at_rest") if type(prof) is dict else None
    label = row.get("label") if type(row) is dict else None
    if type(label) is not str or not label:
        return "unknown - the platform profile did not report it"
    return label.encode("ascii", "backslashreplace").decode("ascii")


def _provider_text(provider: dict) -> str:
    if not provider["configured"]:
        return "not configured"
    if provider["reachable"] is False:
        return "unreachable"
    return "configured"


def _daemon_build_text(build: dict) -> str:
    """F13 — WHICH systemu build the daemon is executing.

    UNVERIFIED is its own answer. A daemon that recorded no build is an older or
    other build, which is the skew itself — collapsing that into silence is what
    let a stale daemon pass for twenty minutes.
    """
    if type(build) is not dict:
        return "unknown"
    if not build.get("observed") and build.get("match") is None:
        return "— (no daemon observed)"
    if build.get("match") is False:
        return (f"MISMATCH — daemon {build.get('daemon_version')} from "
                f"{build.get('daemon_path')}")
    if build.get("match") is True:
        return f"{build.get('daemon_version')} (same build as this process)"
    return "UNVERIFIED — the daemon did not record which build it loaded"


def _needs_you_section():
    """The R-B4 "Needs you" breakdown for /health, or ``None`` when it cannot be
    determined.

    ``None`` (⇒ the section is not rendered) is deliberately distinct from a zero
    breakdown (⇒ "Nothing is waiting on you"). With no vault, or an unreadable
    decision store, we do not KNOW that nothing is waiting — and /health is
    reachable precisely when the install is broken, which is exactly when a
    confident "nothing needs you" would be both wrong and reassuring. This page
    has already shipped that failure class once; it does not get to ship it again.
    """
    try:
        from systemu.interface.dashboard_state import AppState
        vault = getattr(AppState.get(), "vault", None)
        if vault is None:
            return None
        from systemu.interface.components.attention import needs_you_breakdown
        return needs_you_breakdown(vault)
    except Exception:
        return None


#: How many broken items /health lists one line each before the count alone has
#: to do (the /table page shows them all).
_BROKEN_LINES_CAP = 5


def table_status_text(needs) -> str:
    """R-B1 — the /health "Table" row: ``"N broken"``, from the Needs-you breakdown.

    Never a reassuring number it cannot back: no breakdown (no vault open, or the
    accounting failed) is "unknown", a table the reconciler has not projected yet is
    "not checked yet", and an unreadable snapshot is "unknown" — not "0 broken".
    ASCII only."""
    if type(needs) is not dict:
        return "unknown - the table could not be checked from this page"
    if not needs.get("table_checked"):
        if needs.get("table_why") == "absent":
            return "not checked yet - the table has not been projected"
        return "unknown - the table snapshot could not be read"
    n = needs.get("table_broken")
    n = n if type(n) is int and n >= 0 else 0
    return f"{n} broken"


# ── PLAN-11 / DEC-11: the approval fatigue the system records ───────────────
#
# The counters in ``runtime/metrics_store.py`` were written on every gate
# decision from S1b on and shown NOWHERE: ``MetricsStore.snapshot()`` had no
# reader outside tests. This section is that reader. It renders from a
# PUBLISHED render snapshot (``render_snapshots``) -- the store is a file, and
# no page reads a file on the event loop -- and every measure whose denominator
# is 0 says NOT MEASURED. A "0%" with nothing behind it would be a reassuring
# claim the store cannot back.

_NOT_MEASURED = "NOT MEASURED"

#: How often an OPEN /health checks whether a fresher approval snapshot landed.
#: The tick reads one timestamp under a lock -- it never reads the store.
APPROVALS_POLL_S = 2.0


def _fmt_duration(ms: float) -> str:
    """A time-to-decision, in the unit a person reads it in. ASCII only."""
    s = float(ms) / 1000.0
    if s < 60.0:
        return f"{s:.1f}s"
    if s < 3600.0:
        return f"{s / 60.0:.1f} min"
    return f"{s / 3600.0:.1f} h"


def _median(xs: list) -> float:
    ordered = sorted(xs)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _p90(xs: list) -> float:
    """Nearest-rank 90th percentile: a value that was actually observed."""
    import math
    ordered = sorted(xs)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def approvals_view(metrics, *, unread_reason: str = "") -> dict:
    """Pure render-DATA for the /health "Approvals" section.

    ``metrics`` is a ``MetricsStore.snapshot()`` dict (as published in an
    ``ApprovalMetricsSnapshot``), or None when no snapshot has been read --
    in which case every row says NOT MEASURED and a note says why.

    Returns ``{"measured", "rows": [(label, value)], "notes": [str]}``; every
    string is ASCII. The denominator of each row:

      * cards resolved, Always-allow grants, bulk decisions -> resolved cards;
      * median / 90th percentile -> the timed decisions in the bounded window;
      * approved without reading -> approvals that were timed;
      * all-time line -> every timed decision on record.

    A row whose denominator is 0 reads NOT MEASURED, never 0 / 0%.
    """
    from systemu.runtime import metrics_store as _ms

    fast = f"{_ms.FAST_APPROVAL_S:g}s"
    bulk = f"{_ms._BULK_WINDOW_S:g}s"
    labels = (
        "Gate cards resolved",
        "Median time to decision",
        "90th percentile time to decision",
        f"Approved without reading (decided in under {fast})",
        "Always allow grants",
        f"Bulk decisions (within {bulk} of the previous decision)",
        "All-time time to decision",
    )

    def _int(key: str) -> int:
        x = metrics.get(key)
        return x if type(x) is int and x >= 0 else 0

    def _num(key: str):
        x = metrics.get(key)
        return float(x) if type(x) in (int, float) else None

    if type(metrics) is not dict:
        reason = unread_reason or "the approval counters have not been read"
        return {
            "measured": False,
            "rows": [(lbl, f"{_NOT_MEASURED} - not read yet") for lbl in labels],
            "notes": [f"Approval counters not read yet: {reason}."],
        }

    resolved = _int("gate_cards_resolved")
    if resolved == 0:
        return {
            "measured": False,
            "rows": [(lbl, f"{_NOT_MEASURED} - no resolved gate card is on record")
                     for lbl in labels],
            "notes": ["Nothing to measure yet: these fill in as gate cards are "
                      "decided."],
        }

    series = metrics.get("resolution_latency_ms")
    window = ([float(x) for x in series if type(x) in (int, float)]
              if type(series) is list else [])
    no_timed = f"{_NOT_MEASURED} - no timed decision is on record"
    if window:
        last = f"(last {_plural(len(window), 'decision')})"
        median = f"{_fmt_duration(_median(window))} {last}"
        p90 = f"{_fmt_duration(_p90(window))} {last}"
    else:
        median = p90 = no_timed

    timed = _int("approvals_timed")
    unread = min(_int("approvals_unread"), timed)
    if timed:
        share = (f"{100.0 * unread / timed:.0f}% ({unread} of "
                 f"{_plural(timed, 'approval')})")
    else:
        share = f"{_NOT_MEASURED} - no timed approval is on record"

    count = _int("resolution_latency_count")
    total, lo, hi = (_num("resolution_latency_sum_ms"),
                     _num("resolution_latency_min_ms"),
                     _num("resolution_latency_max_ms"))
    if count and total is not None and lo is not None and hi is not None:
        all_time = (f"avg {_fmt_duration(total / count)}, fastest "
                    f"{_fmt_duration(lo)}, slowest {_fmt_duration(hi)} over "
                    f"{_plural(count, 'decision')}")
    else:
        all_time = no_timed

    rows = list(zip(labels, (
        str(resolved), median, p90, share,
        str(_int("always_allow_grants")), str(_int("bulk_approve_events")),
        all_time,
    )))
    return {
        "measured": True,
        "rows": rows,
        "notes": [
            (f"Median and 90th percentile cover at most the {_ms.LATENCY_WINDOW} "
             "most recent decisions; the all-time line covers every decision "
             "on record."),
            ("An approval is Approve, Approve once, Always allow, or a "
             "trust/install choice; a Deny is never counted as one."),
            ("Cards a gate-mode policy resolved automatically are included, "
             "recorded as decided in 0s."),
        ],
    }


def _approvals_store_dir():
    """The metrics directory of THIS process's open vault, or None. No I/O."""
    try:
        from pathlib import Path

        from systemu.interface.dashboard_state import AppState
        root = getattr(getattr(AppState.get(), "vault", None), "root", None)
        if root is None:
            return None
        return str(Path(root) / "metrics")
    except Exception:
        return None


def _published_approvals(store_dir: str):
    """The PUBLISHED approval-metrics snapshot for ``store_dir``, or None.

    Reads the render-snapshot slot; never produces, never touches the file.
    Type-pinned in this frame (DEC-36), and a snapshot of a DIFFERENT store
    (the slot is process-wide) is not an answer about this vault."""
    try:
        from systemu.runtime import metrics_store as _ms
        from systemu.runtime import render_snapshots as _rs
        snap = _rs.current(_ms.APPROVAL_METRICS_SNAPSHOT)
    except Exception:
        return None
    if type(snap) is not _ms.ApprovalMetricsSnapshot:
        return None
    if type(snap.store_dir) is not str or snap.store_dir != store_dir:
        return None
    if type(snap.metrics) is not dict:
        return None
    return snap


def _approvals_snapshot():
    """``(snapshot | None, why)`` for the Approvals section. NEVER reads the file.

    Registers the producer for this vault (idempotent; re-points it if the
    vault changed) and nudges the shared refresher when there is no snapshot
    or it is stale -- the D-3 shape: a render reads, it never produces."""
    store_dir = _approvals_store_dir()
    if store_dir is None:
        return None, "no vault is open in this process"
    try:
        from systemu.runtime import metrics_store as _ms
        from systemu.runtime import render_snapshots as _rs
        _rs.register(_ms.APPROVAL_METRICS_SNAPSHOT,
                     lambda: _ms.publish_approval_metrics(store_dir))
        _rs.start_refresher()
    except Exception:
        _rs = None
    snap = _published_approvals(store_dir)
    if (snap is None or snap.stale) and _rs is not None:
        try:
            _rs.request_refresh()
        except Exception:
            pass
    if snap is None:
        return None, ("they are read off the page thread, and this section "
                      "fills in when they arrive")
    return snap, ""


def _paint_approvals() -> None:
    """The /health "Approvals" section, kept current on an open page."""
    from nicegui import ui

    ui.label("Approvals").classes("s-section-head")
    holder = ui.column().classes("w-full").style("gap: 4px;")
    seen = {"key": None}

    def _paint() -> None:
        snap, why = _approvals_snapshot()
        seen["key"] = None if snap is None else snap.taken_at
        view = approvals_view(None if snap is None else snap.metrics,
                              unread_reason=why)
        with holder:
            for label, value in view["rows"]:
                with ui.row().classes("w-full items-center").style("gap: 12px;"):
                    ui.label(label).classes("s-muted").style("min-width: 260px;")
                    ui.label(value).classes("s-cell")
            for note in view["notes"]:
                ui.label(note).classes("s-muted")

    _paint()

    # Repaint ONLY when a new observation has landed (D-11): the first visit
    # after a restart finds no snapshot, nudges the refresher, and fills in a
    # moment later without a reload.
    def _tick() -> None:
        store_dir = _approvals_store_dir()
        snap = None if store_dir is None else _published_approvals(store_dir)
        key = None if snap is None else snap.taken_at
        if key == seen["key"]:
            return
        holder.clear()
        _paint()

    try:
        from systemu.interface.ui_helpers import safe_timer
        safe_timer(APPROVALS_POLL_S, _tick)
    except Exception:
        pass


def build_health_page() -> None:
    """Render /health. Token classes only — no inline f-string styles / raw hex."""
    from nicegui import ui

    view = health_view()
    prof = view["profile"]

    with ui.column().classes("w-full items-center"):
        with ui.column().classes("s-card").style("max-width: 860px; width: 100%;"):
            with ui.row().classes("items-center").style("gap: 12px;"):
                ui.label("System health").classes("s-page-title")
                chip = view["status_chip"]
                ui.html(f'<span class="{_CHIP_PILL[chip]}">{_CHIP_LABEL[chip]}</span>')
                # R-UX2 / UX-9(f): a SECOND, independent chip — "busy" is not
                # "broken", so it never recolours the health chip beside it.
                # These four lines are the ONLY thing that puts the chip on the
                # page, and deleting them once left 45 tests green — so
                # tests/test_rux2_health_load_chip.py now parses this function's
                # AST and fails if the _LOAD_PILL read below disappears.
                _load = view["load_chip"]
                ui.html(f'<span class="{_LOAD_PILL[_load["state"]]}">'
                        f'{_load["label"]}</span>')
            ui.label(view["load_chip"]["detail"]).classes("s-muted")
            ui.label(
                "Why is nothing happening? This page reports the live provider, "
                "keyring and daemon status and the one cross-OS capability "
                "profile every OS-divergence renders from."
            ).classes("s-muted")

            # ── problems (only when present) ──────────────────────────────
            if view["problems"]:
                ui.label("Diagnosis").classes("s-section-head")
                for p in view["problems"]:
                    variant = "s-banner--danger" if p["blocking"] else "s-banner--warn"
                    ui.label(p["message"]).classes(f"s-banner {variant} w-full")
                    if p.get("cta"):
                        ui.label(p["cta"]).classes("s-muted")

            # ── R-B4: "Needs you" — what is WAITING, by surface ────────────
            # §5.10's honest-risks list names "Needs-you surfacing" as one of the
            # three load-bearing defences against a stale table: a suggestion
            # nobody is told about is wallpaper. /health is the "why is nothing
            # happening?" page, and "three things are waiting on you" is one of
            # the true answers to that question.
            #
            # It reads AppState rather than health_view() so health_view stays
            # hermetic + vault-free (it is unit-tested with injected overrides).
            # No vault ⇒ the section is omitted entirely rather than rendered as
            # a reassuring "nothing waiting" we cannot actually vouch for.
            _needs = _needs_you_section()
            if _needs is not None:
                ui.label("Needs you").classes("s-section-head")
                if _needs["total"] == 0:
                    ui.label("Nothing is waiting on you.").classes("s-muted")
                else:
                    for _lbl, _n, _href in (
                        ("Approvals in your Inbox", _needs["gates"], "/inbox"),
                        ("Questions waiting for an answer", _needs["asks"], "/inbox"),
                        ("Suggestions on your table",
                         _needs["table_suggestions"], "/table"),
                        # R-B1: something the operator relied on went away
                        ("Broken on your table",
                         _needs.get("table_broken", 0), "/table"),
                    ):
                        if not _n:
                            continue
                        with ui.row().classes("w-full items-center").style("gap: 12px;"):
                            ui.label(f"{_lbl}: {_n}")
                            ui.link("open →", _href).classes("s-muted")
                    # R-B1: each broken item's one-line reason, linking to /table
                    # where its card leads with the same reason and a fix action.
                    for _row in _needs.get("table_broken_rows", [])[:_BROKEN_LINES_CAP]:
                        ui.link(_row["line"], _row["href"]).classes("s-muted")

            # ── PLAN-11 / DEC-11: approval fatigue, from the published snapshot.
            # Pinned by tests/test_plan11_approval_fatigue_surface.py (a stub
            # render that must show the published numbers, plus an AST pin).
            _paint_approvals()

            # ── live status ───────────────────────────────────────────────
            ui.label("Status").classes("s-section-head")
            kr = view["keyring"]
            kr_txt = kr["backend"] + (" (LOCKED)" if kr["locked"] else "")
            daemon = view["daemon"]["running"]
            daemon_txt = ("running" if daemon else
                          ("not running" if daemon is False else "unknown"))
            # F13 / GATE-7a: this page is served BY the daemon, so its bare
            # "systemu version" row was the daemon's own — but the operator
            # reads it as the answer to "which build is everything running?".
            # Both builds are named, and each says whose it is.
            _build = view["daemon"].get("build") or {}
            for label, value in (
                ("LLM provider", _provider_text(view["provider"])),
                ("Keyring backend", kr_txt),
                ("Daemon", daemon_txt),
                # R-B1: how many things on the operator's table are broken. From
                # the same breakdown as "Needs you" above -- no second read.
                ("Table", table_status_text(_needs)),
                ("systemu version (this process)",
                 view["versions"].get("systemu", "?")),
                ("systemu path (this process)", _build.get("cli_path") or "?"),
                ("Daemon build", _daemon_build_text(_build)),
                ("python version", view["versions"].get("python", "?")),
            ):
                with ui.row().classes("w-full items-center").style("gap: 12px;"):
                    ui.label(label).classes("s-muted").style("min-width: 160px;")
                    ui.label(str(value)).classes("s-cell")
            if view.get("last_error"):
                with ui.row().classes("w-full items-center").style("gap: 12px;"):
                    ui.label("Last error").classes("s-muted").style("min-width: 160px;")
                    ui.label(str(view["last_error"])).classes("s-cell")

            # ── platform capability profile ───────────────────────────────
            ui.label("Platform capability profile").classes("s-section-head")
            for label, value in (
                ("OS / arch", f"{prof['os']} ({prof['os_family']}) / {prof['arch']}"),
                ("Docker mode", "yes" if prof["docker_mode"] else "no"),
                ("Capture available", "yes" if prof["capture_available"] else "no"),
                ("Keyring backend", prof["keyring_backend"]),
                # S5 / R-A3: what actually protects the secrets, from the
                # snapshot's truthful row -- no probe on render.
                ("Secrets at rest", secrets_at_rest_text(prof)),
                ("Forged-network jail", prof["forged_net_jail"]),
                ("Provider configured", "yes" if prof["provider_configured"] else "no"),
            ):
                with ui.row().classes("w-full items-center").style("gap: 12px;"):
                    ui.label(label).classes("s-muted").style("min-width: 160px;")
                    ui.label(str(value)).classes("s-cell")

            # ── F21 optional capability groups ────────────────────────────
            ui.label("Optional capability groups").classes("s-section-head")
            ui.label(
                "Shipped as pip extras to keep the default install small. A "
                "group that is not installed makes its tools UNAVAILABLE — "
                "never silently broken."
            ).classes("s-muted")
            for grp in view["optional_groups"]:
                state = "installed" if grp["installed"] else "UNAVAILABLE"
                detail = grp["covers"] if grp["installed"] else grp["remedy"]
                with ui.row().classes("w-full items-center").style("gap: 12px;"):
                    ui.label(grp["label"]).classes("s-cell").style("min-width: 200px;")
                    ui.label(state).classes("s-muted").style("min-width: 96px;")
                    ui.label(detail).classes("s-muted")

            # ── DEP-10 honesty rows ───────────────────────────────────────
            ui.label("Host capabilities").classes("s-section-head")
            ui.label(
                "Host-only capabilities are never faked in a container — inside "
                "one they read as offered via the Host Companion (flagged)."
            ).classes("s-muted")
            for row in view["honesty_rows"]:
                avail = "yes" if row["available"] else "no"
                detail = row["via"] + (f" — {row['note']}" if row["note"] else "")
                with ui.row().classes("w-full items-center").style("gap: 12px;"):
                    ui.label(row["label"]).classes("s-cell").style("min-width: 200px;")
                    ui.label(avail).classes("s-muted").style("min-width: 48px;")
                    ui.label(detail).classes("s-muted")
