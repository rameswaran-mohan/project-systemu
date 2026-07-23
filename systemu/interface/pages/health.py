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
        "last_error": report.get("last_error"),
        "status_chip": chip,
        "load": snap,
        "load_chip": _load_chip(snap),
        "ok": report["ok"],
    }


def _provider_text(provider: dict) -> str:
    if not provider["configured"]:
        return "not configured"
    if provider["reachable"] is False:
        return "unreachable"
    return "configured"


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
                    ):
                        if not _n:
                            continue
                        with ui.row().classes("w-full items-center").style("gap: 12px;"):
                            ui.label(f"{_lbl}: {_n}")
                            ui.link("open →", _href).classes("s-muted")

            # ── live status ───────────────────────────────────────────────
            ui.label("Status").classes("s-section-head")
            kr = view["keyring"]
            kr_txt = kr["backend"] + (" (LOCKED)" if kr["locked"] else "")
            daemon = view["daemon"]["running"]
            daemon_txt = ("running" if daemon else
                          ("not running" if daemon is False else "unknown"))
            for label, value in (
                ("LLM provider", _provider_text(view["provider"])),
                ("Keyring backend", kr_txt),
                ("Daemon", daemon_txt),
                ("systemu version", view["versions"].get("systemu", "?")),
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
                ("Forged-network jail", prof["forged_net_jail"]),
                ("Provider configured", "yes" if prof["provider_configured"] else "no"),
            ):
                with ui.row().classes("w-full items-center").style("gap: 12px;"):
                    ui.label(label).classes("s-muted").style("min-width: 160px;")
                    ui.label(str(value)).classes("s-cell")

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
