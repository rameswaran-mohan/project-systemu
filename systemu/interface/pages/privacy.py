"""R-P3b - the /privacy "What leaves this machine" page (section 15.7 honest interim rule).

A thin renderer over ``runtime.privacy.privacy_report()`` (the pure, unit-tested
mint). ``privacy_page_model`` turns the report into the exact strings the page
shows, so a test can read the page without NiceGUI; ``build_privacy_page`` only
lays them out. The live vault + config come from ``AppState`` - the same handles
every other page renders from - so the MCP store and the web-reader setting the
report reads are the ones this install uses. Render-time reads are cheap local
reads only (no network, no probes). Token classes only - no inline colors / raw
hex (mirrors /health). Every rendered string is ASCII (DEC-32c).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

TITLE = "What leaves this machine"
FOOTER = ("This is the honest current reality, not a promise of zero egress. "
          "Local-first here means custody, verification, and the vault.")


def _ascii(text: Any) -> str:
    s = text if type(text) is str else str(text)
    return s.encode("ascii", "backslashreplace").decode("ascii")


def live_inputs() -> Tuple[Any, Any]:
    """(vault, config) of this dashboard process, or (None, None) if there is no
    AppState yet. A missing vault is reported by the mint as "not available",
    never as "no MCP servers"."""
    try:
        from systemu.interface.dashboard_state import AppState
        state = AppState.get()
    except Exception:
        return None, None
    return getattr(state, "vault", None), getattr(state, "config", None)


def privacy_page_model(*, vault: Any = None, config: Any = None,
                       report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The page, as data: title, headline, one entry per report section with the
    lines it renders, and the footer. Pure apart from the report's own reads."""
    if report is None:
        from systemu.runtime.privacy import privacy_report
        report = privacy_report(vault=vault, config=config)

    sections = []
    for s in report["sections"]:
        items = s.get("items") or []
        lines = [i["text"] for i in items] if items else [s["detail"]]
        entry = {"key": s["key"], "title": _ascii(s["title"]),
                 "severity": s.get("severity"),
                 "lines": [_ascii(line) for line in lines]}
        if s.get("route"):
            entry["route"] = s["route"]
        sections.append(entry)

    return {"title": TITLE, "headline": _ascii(report["headline"]),
            "sections": sections, "footer": FOOTER, "report": report}


def live_page_model() -> Dict[str, Any]:
    """The model for THIS process: the live vault + config fed to the mint."""
    vault, config = live_inputs()
    return privacy_page_model(vault=vault, config=config)


def page_text(model: Dict[str, Any]) -> str:
    """The page as plain text, in render order (tests + the release note)."""
    out = [model["title"], model["headline"], ""]
    for s in model["sections"]:
        out.append(s["title"])
        out.extend("  " + line for line in s["lines"])
        out.append("")
    out.append(model["footer"])
    return "\n".join(out)


def build_privacy_page() -> None:
    """Render /privacy. The data is deterministic + tested; this only lays it out."""
    from nicegui import ui

    model = live_page_model()

    with ui.column().classes("w-full items-center"):
        with ui.column().classes("s-card").style("max-width: 860px; width: 100%;"):
            ui.label(model["title"]).classes("s-page-title")
            ui.label(model["headline"]).classes("s-muted")

            for s in model["sections"]:
                ui.label(s["title"]).classes("s-section-head")
                for line in s["lines"]:
                    if s.get("severity") == "warn":
                        ui.label(line).classes("s-banner s-banner--warn w-full")
                    else:
                        ui.label(line).classes("s-cell w-full")
                if s.get("route"):
                    ui.link("Open Settings ->", s["route"]).classes("s-muted")

            ui.label(model["footer"]).classes("s-muted")
