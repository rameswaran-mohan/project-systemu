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


def health_view(**overrides) -> dict:
    """Pure render-DATA for /health. Accepts the same injectable states as
    ``build_doctor_report`` so it is deterministic + hermetic in tests.

    Returns the platform profile, provider/keyring/daemon status, the DEP-10
    honesty rows, and a single ``status_chip`` (ok | warn | danger)."""
    report = pp.build_doctor_report(**overrides)
    prof = report["profile"]
    if not report["ok"]:
        chip = "danger"
    elif any(p["severity"] == "warning" for p in report["problems"]):
        chip = "warn"
    else:
        chip = "ok"
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
