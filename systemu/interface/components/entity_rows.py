"""Canonical entity-row renderers (Phase 5 Slice 3 Batch 1 · 3a).

ONE renderer per entity, shared by every page that lists it:

  * ``render_tool_row(tool, vault, *, editable=True)`` owns the Tool registry
    row — the table cells (Name/Type/Status/Enabled/Dry-run/Success/Deps/
    Description) AND the per-row actions (Review&Forge / Dry-Run / Enable /
    Edit).  ``tools.py`` calls it instead of inlining the per-tool ``<tr>``.

  * ``render_skill_row(skill, vault, *, editable=True)`` owns the Skills
    registry row — the collapsed header card + the expandable detail panel,
    plus the deprecate / reactivate / export affordances.  ``skills_page.py``
    calls it instead of inlining ``_skill_row`` / ``_skill_detail``.

The action *policy* (which buttons appear) and the dependency lookup live in
pure ``*_model`` / ``tool_row_deps`` functions so the behaviour is unit-testable
without a NiceGUI runtime — same split-the-data-from-the-paint discipline as
``remediation_card_model``.

Styling is token-class / plain-string only (no inline f-string ``.style`` and no
raw hex), so the new file adds ZERO entries to the UI-style lint baseline.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Skills with effectiveness_score below this are treated as deprecated
# (excluded from shadow_decision matching) — mirrors the runtime gate.
_DEPRECATED_THRESHOLD = 0.5


# ─────────────────────────────────────────────────────────────────────────────
#  Pure view-models (headless-testable)
# ─────────────────────────────────────────────────────────────────────────────

def _row_actions_for(header: dict) -> list[dict]:
    """Decide which generic action buttons a tool row gets (pure data).

    Canonical home of the v0.7.4 Pattern-2 policy: a Dry-Run action is offered
    for forged/proposed tools whose dry-run has not yet passed.  ``tools.py``
    re-exports this name so existing imports keep working.
    """
    actions: list[dict] = []
    status = (header.get("status") or "").lower()
    dryrun = (header.get("dry_run_status") or "").lower()
    if status in ("forged", "proposed") and dryrun in ("", "not_run", "failed"):
        actions.append({"label": "Dry-Run", "kind": "dryrun", "tool_id": header.get("id")})
    return actions


def tool_row_deps(header: dict, *, vault) -> list[str]:
    """Resolve a tool's declared dependencies for the inline deps cell (3d).

    Prefers the index header's ``dependencies`` list; falls back to
    ``vault.get_tool(id).dependencies`` when the header doesn't carry it (the
    ``settings.py`` pattern).  Defensive: any miss/failure → ``[]`` (the cell
    simply renders nothing), never raises.
    """
    deps = header.get("dependencies")
    if isinstance(deps, list) and deps:
        return [str(d) for d in deps]
    if vault is None:
        return []
    try:
        tool = vault.get_tool(header.get("id"))
        return [str(d) for d in (getattr(tool, "dependencies", None) or [])]
    except Exception:
        return []


_DEPS_VISIBLE_CAP = 4


def tool_deps_display(deps: list[str], *, cap: int = _DEPS_VISIBLE_CAP) -> dict:
    """Lay out a deps list for the inline cell (3d, pure/testable).

    Returns ``{"visible": [...], "overflow": N}`` — at most ``cap`` badges are
    shown, with the remainder collapsed into a ``+N more`` chip so a tool with
    many dependencies doesn't blow out the registry column.
    """
    deps = list(deps or [])
    visible = deps[:cap]
    return {"visible": visible, "overflow": max(0, len(deps) - len(visible))}


def tool_row_model(header: dict, *, vault=None) -> dict:
    """Pure view-model for ONE tool registry row.

    Encodes the exact render policy the legacy ``tools.py`` tbody used:
      * ``show_review_forge`` — PROPOSED tools get the Review&Forge button.
      * ``actions``           — generic per-row actions (Dry-Run today).
      * ``show_enable``       — the Enable shortcut button is gated on
        reviewed-but-disabled AND ``dry_run_status == 'passed'`` (Gate 3.5).
      * ``show_toggle``       — the Gate-3 enable/disable switch only renders
        for reviewed tools.
      * ``deps``              — declared dependencies for the inline cell (3d).
    """
    status = (header.get("status") or "").lower()
    enabled = bool(header.get("enabled", False))
    dry_run_status = header.get("dry_run_status") or "not_run"
    reviewed = status in ("forged", "deployed", "tested", "upgraded")
    return {
        "id": header.get("id"),
        "name": header.get("name", header.get("id", "")),
        "tool_type": header.get("tool_type", "—"),
        "status": status,
        "enabled": enabled,
        "dry_run_status": dry_run_status,
        "description": header.get("description", "") or "",
        "show_review_forge": status == "proposed",
        "actions": _row_actions_for(header),
        "show_toggle": reviewed,
        "show_enable": reviewed and not enabled and dry_run_status == "passed",
        "deps": tool_row_deps(header, vault=vault),
    }


def skill_row_model(header: dict) -> dict:
    """Pure view-model for ONE skill registry row.

    ``deprecated`` surfaces the 3b deprecated badge: a skill with
    ``effectiveness_score < 0.5`` (default 1.0) is excluded from matching and
    rendered with a muted/deprecated treatment.
    """
    try:
        score = float(header.get("effectiveness_score", 1.0))
    except (TypeError, ValueError):
        score = 1.0
    evidence = header.get("evidence_scroll_ids", []) or []
    return {
        "id": header.get("id", ""),
        "name": header.get("name", header.get("id", "")),
        "category": header.get("category", "general"),
        "description": header.get("description", "") or "",
        "evidence_count": len(evidence),
        "effectiveness_score": score,
        "deprecated": score < _DEPRECATED_THRESHOLD,
    }


def skill_effectiveness(header: dict, *, vault) -> float:
    """Resolve a skill's effectiveness_score for the deprecated badge (3b).

    The skills index header does NOT carry effectiveness_score, so prefer it
    when present (e.g. enriched fixtures) and otherwise fall back to
    ``vault.get_skill(id).effectiveness_score`` — the same header-then-vault
    pattern as ``tool_row_deps``.  Defensive: any miss/failure → 1.0
    (not deprecated), never raises.
    """
    if "effectiveness_score" in header:
        try:
            return float(header["effectiveness_score"])
        except (TypeError, ValueError):
            return 1.0
    if vault is None:
        return 1.0
    try:
        skill = vault.get_skill(header.get("id"))
        return float(getattr(skill, "effectiveness_score", 1.0))
    except Exception:
        return 1.0


# ─────────────────────────────────────────────────────────────────────────────
#  Tool row renderer (lifted from tools.py tbody — behaviour identical)
# ─────────────────────────────────────────────────────────────────────────────

def render_tool_row(tool: dict, vault, *, editable: bool = True) -> None:
    """Render ONE tool as a ``<tr>`` in the registry table.

    ``tool`` is an index header dict (``vault.load_index('tools')`` row).
    ``editable`` False drops the interactive Gate-3 toggle + action buttons
    (the read-only subset used by activity/inspection contexts).

    All action wiring (Review&Forge → ``_show_spec_review_dialog``, Dry-Run →
    ``_dispatch_dryrun``, Enable → ``_dispatch_enable``, toggle →
    ``_toggle_enabled``) is preserved exactly; the handlers are imported lazily
    from ``tools.py`` to avoid an import cycle.  Slice 3c: ``✏️ Edit`` now opens
    the edit dialog IN-PAGE (``open_tool_edit_dialog``) instead of deep-linking
    to the dissolving Workshop, refreshing the page on save.
    """
    from nicegui import ui
    from systemu.interface.dashboard_state import THEME, status_badge_html

    m = tool_row_model(tool, vault=vault)
    tid = m["id"]
    status = m["status"]
    enabled = m["enabled"]

    with ui.element("tr"):
        _td(m["name"], bold=True)
        _td(m["tool_type"])

        # Status badge — FORGED+enabled shows the green "enabled" pill.
        with ui.element("td").classes("s-cell").style("padding: 12px 16px;"):
            if status == "forged" and enabled:
                ui.html(status_badge_html("enabled"))
            else:
                ui.html(status_badge_html(status or "?"))

        # Enabled toggle (Gate 3) — only for reviewed tools, only when editable.
        with ui.element("td").classes("s-cell").style("padding: 12px 16px;"):
            if editable and m["show_toggle"]:
                sw = ui.switch("", value=enabled).props("dense")
                sw.on(
                    "update:model-value",
                    lambda e, i=tid, s=sw: _toggle_enabled(
                        i,
                        e.args if isinstance(e.args, bool)
                        else bool(e.args[0]) if isinstance(e.args, (list, tuple)) and e.args
                        else bool(e.args),
                        switch=s,
                    ),
                )
            else:
                ui.label("—").classes("s-muted").style("font-size: 12px;")

        # Dry-run status column.
        with ui.element("td").classes("s-cell").style("padding: 12px 16px;"):
            dr_status = m["dry_run_status"]
            dr_class = {
                "passed": "s-text-success", "failed": "s-text-danger",
                "skipped": "s-text-warn", "not_run": "s-muted",
            }.get(dr_status, "s-muted")
            ui.label(dr_status).classes(f"{dr_class} s-dryrun-cell")

        # Success-rate column — pulls from ToolMetrics.
        with ui.element("td").classes("s-cell").style("padding: 12px 16px;"):
            _render_success_rate(tid)

        # Dependencies column (3d) — inline badges (capped, +N more on overflow);
        # empty → em-dash.
        with ui.element("td").classes("s-cell").style("padding: 12px 16px;"):
            deps = m["deps"]
            if deps:
                layout = tool_deps_display(deps)
                with ui.row().classes("flex-wrap").style("gap: 4px;"):
                    for dep in layout["visible"]:
                        ui.html(f'<span class="s-dep-badge">{dep}</span>')
                    if layout["overflow"]:
                        full = ", ".join(deps)
                        ui.html(
                            f'<span class="s-dep-badge s-muted" title="{full}">'
                            f'+{layout["overflow"]} more</span>'
                        )
            else:
                ui.label("—").classes("s-muted").style("font-size: 12px;")

        desc = m["description"]
        _td(desc[:70] + "…" if len(desc) > 70 else desc or "—")

        # Actions column.
        with ui.element("td").classes("s-cell").style("padding: 12px 16px;"):
            with ui.row().style("gap: 6px;"):
                if not editable:
                    pass
                elif m["show_review_forge"]:
                    ui.button(
                        "Review & Forge",
                        on_click=lambda _, i=tid: _show_spec_review_dialog(i),
                    ).props("no-caps").classes("s-btn s-btn--warn")
                else:
                    for _a in m["actions"]:
                        if _a["kind"] == "dryrun":
                            ui.button(
                                _a["label"],
                                on_click=lambda _, t=_a["tool_id"]: _dispatch_dryrun(t),
                            ).props("no-caps").classes("s-btn s-btn--ghost")
                    if m["show_enable"]:
                        ui.button(
                            "Enable",
                            on_click=lambda _, i=tid: _dispatch_enable(i),
                        ).props("no-caps").classes("s-btn s-btn--success")
                if editable:
                    ui.button(
                        "✏️ Edit",
                        on_click=lambda _, i=tid: _edit_tool_in_place(i, vault),
                    ).props("no-caps").classes("s-btn s-btn--ghost")


def _render_success_rate(tid: str) -> None:
    from nicegui import ui
    try:
        from systemu.runtime.tool_metrics import get_tool_metrics
        entry = get_tool_metrics().get(tid)
        if entry.has_history and entry.attributable_calls > 0:
            rate = entry.success_rate
            rate_class = (
                "s-text-success" if rate >= 0.7
                else "s-text-warn" if rate >= 0.4
                else "s-text-danger"
            )
            ui.label(f"{rate*100:.0f}% ({entry.attributable_calls})").classes(
                f"{rate_class} s-cell--bold").style("font-size: 12px;")
        else:
            ui.label("—").classes("s-muted").style("font-size: 12px;")
    except Exception:
        ui.label("—").classes("s-muted").style("font-size: 12px;")


def _td(text: str, bold: bool = False) -> None:
    from nicegui import ui
    cls = "s-cell s-cell--bold" if bold else "s-cell"
    with ui.element("td").classes(cls).style(
        "padding: 12px 16px; border-bottom: 1px solid var(--color-border);"
    ):
        ui.label(text)


# ─────────────────────────────────────────────────────────────────────────────
#  Lazy re-exports of the tool action handlers (canonical home stays tools.py)
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch_dryrun(tool_id: str) -> None:
    from systemu.interface.pages.tools import _dispatch_dryrun as _impl
    _impl(tool_id)


def _dispatch_enable(tool_id: str) -> None:
    from systemu.interface.pages.tools import _dispatch_enable as _impl
    _impl(tool_id)


def _toggle_enabled(tool_id: str, enabled: bool, *, switch=None) -> None:
    from systemu.interface.pages.tools import _toggle_enabled as _impl
    _impl(tool_id, enabled, switch=switch)


def _show_spec_review_dialog(tool_id: str) -> None:
    from systemu.interface.pages.tools import _show_spec_review_dialog as _impl
    _impl(tool_id)


def _edit_tool_in_place(tool_id: str, vault) -> None:
    """Slice 3c: resolve the tool + open the in-page edit dialog, reloading the
    page on save so the registry row reflects the edit."""
    from nicegui import ui
    from systemu.interface.components.entity_edit import open_tool_edit_dialog

    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        ui.notify(f"Tool {tool_id} not found.", type="negative")
        return
    open_tool_edit_dialog(tool, vault, on_saved=ui.navigate.reload)


# ─────────────────────────────────────────────────────────────────────────────
#  Skill row renderer (lifted from skills_page._skill_row / _skill_detail)
# ─────────────────────────────────────────────────────────────────────────────

# Category → design-token colour NAME (resolved to var(--color-<name>) in CSS).
# No raw hex here — the palette stays single-sourced in tokens.py and the new
# file adds zero entries to the UI-style lint baseline.
_CATEGORY_TOKENS: dict[str, str] = {
    "browser": "info", "file_ops": "warn", "devops": "success",
    "data": "accent2", "productivity": "info", "communication": "accent",
    "code": "warn", "system": "muted", "finance": "success",
    "general": "muted",
}


def render_skill_row(skill: dict, vault, *, editable: bool = True) -> None:
    """Render ONE skill as a collapsible card with an expandable detail panel.

    ``skill`` is an index header dict (``vault.load_index('skills')`` row).
    ``editable`` True surfaces the deprecate / reactivate / export affordances
    and the in-page Edit dialog (Slice 3c — replaces the workshop deeplink);
    False renders the read-only subset.
    """
    from nicegui import ui
    from systemu.interface.dashboard_state import THEME

    m = skill_row_model(skill)
    skill_id = m["id"]
    name = m["name"]
    category = m["category"]
    desc = m["description"]
    cat_token = _CATEGORY_TOKENS.get(category, "muted")
    # The index header lacks effectiveness_score, so resolve it from the vault
    # (header-then-vault fallback) to drive the deprecated badge accurately.
    deprecated = skill_effectiveness(skill, vault=vault) < _DEPRECATED_THRESHOLD

    card_cls = "s-card s-skill-row"
    if deprecated:
        card_cls += " s-skill-row--deprecated"
    with ui.card().classes(card_cls).style(
        "padding: 0; margin-bottom: 10px; width: 100%; overflow: hidden;"
    ):
        detail_col = ui.column().classes("w-full").style("display: none;")

        with ui.row().classes("w-full items-center s-skill-header").on(
            "click", lambda _, dc=detail_col: _toggle_detail(dc)
        ):
            # Category pill (per-category accent driven by a design token via
            # a CSS custom property — no raw hex, no inline f-string style).
            ui.html(_category_pill_html(category, cat_token))

            # Deprecated badge (3b).
            if deprecated:
                ui.html('<span class="s-pill s-pill--danger">DEPRECATED</span>')

            with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
                ui.label(name).classes("s-cell s-cell--bold").style("line-height: 1.2;")
                if desc:
                    ui.label(desc[:120] + ("…" if len(desc) > 120 else "")).classes(
                        "s-muted").style("font-size: 12px; line-height: 1.4;")

            if m["evidence_count"]:
                n = m["evidence_count"]
                ui.html(
                    f'<span class="s-muted s-skill-evidence">📜 {n} '
                    f'scroll{"s" if n != 1 else ""}</span>'
                )

            if editable:
                _skill_lifecycle_buttons(skill_id, name, deprecated, vault)
                ui.button(
                    "✏️ Edit",
                    on_click=lambda _, i=skill_id: _edit_skill_in_place(i, vault),
                ).props("@click.stop no-caps").classes("s-btn s-btn--ghost")

            ui.label("▾").classes("s-muted").style("font-size: 14px;")

        with detail_col:
            ui.separator().classes("s-sep")
            with ui.column().style("padding: 16px 18px; gap: 12px;"):
                _skill_detail(skill_id, vault)


def _category_pill_html(category: str, cat_token: str) -> str:
    # The category accent is a design-token colour, injected through the --cat
    # custom property so .s-skill-cat (tokens.py) resolves it.
    return (
        f'<span class="s-skill-cat" style="--cat:var(--color-{cat_token});">'
        f'{category.upper()}</span>'
    )


def _skill_lifecycle_buttons(skill_id: str, name: str, deprecated: bool, vault) -> None:
    """Deprecate / Reactivate + Export buttons (3b). Click handlers stop
    propagation so they don't toggle the row's detail panel."""
    from nicegui import ui

    def _do_deprecate(_=None, reactivate=False):
        from systemu.pipelines.skill_lifecycle import deprecate_skill
        try:
            deprecate_skill(skill_id, reason="gui_codification",
                            reactivate=reactivate, vault=vault)
            verb = "reactivated" if reactivate else "deprecated"
            ui.notify(f"Skill '{name}' {verb}.", type="positive")
        except Exception as exc:
            ui.notify(f"Could not update skill: {exc}", type="negative")

    def _do_export(_=None):
        from systemu.pipelines.skill_exporter import export_skill
        target = Path("data/skill_exports")
        try:
            out = export_skill(skill_id=skill_id, target_dir=target, vault=vault)
            ui.notify(f"Exported '{name}' → {out}", type="positive")
        except FileExistsError as exc:
            ui.notify(f"Export skipped — already exists: {exc}", type="warning")
        except KeyError:
            ui.notify(f"Skill {skill_id} not found in vault.", type="negative")
        except Exception as exc:
            ui.notify(f"Export failed: {exc}", type="negative")

    if deprecated:
        ui.button("Reactivate", on_click=lambda e: _do_deprecate(reactivate=True)).props(
            "@click.stop no-caps").classes("s-btn s-btn--success")
    else:
        ui.button("Deprecate", on_click=lambda e: _do_deprecate(reactivate=False)).props(
            "@click.stop no-caps").classes("s-btn s-btn--danger")
    ui.button("Export", on_click=_do_export).props(
        "@click.stop no-caps").classes("s-btn s-btn--ghost")


def _edit_skill_in_place(skill_id: str, vault) -> None:
    """Slice 3c: resolve the skill + open the in-page edit dialog, reloading the
    page on save so the registry row reflects the edit."""
    from nicegui import ui
    from systemu.interface.components.entity_edit import open_skill_edit_dialog

    try:
        skill = vault.get_skill(skill_id)
    except KeyError:
        ui.notify(f"Skill {skill_id} not found.", type="negative")
        return
    open_skill_edit_dialog(skill, vault, on_saved=ui.navigate.reload)


def _toggle_detail(detail_col) -> None:
    current = detail_col.style or ""
    if "display: none" in current:
        detail_col.style("display: flex; flex-direction: column;")
    else:
        detail_col.style("display: none;")


def _skill_detail(skill_id: str, vault) -> None:
    """Render the expanded detail section for a skill (lifted verbatim)."""
    from nicegui import ui
    from systemu.interface.dashboard_state import THEME
    from systemu.interface.name_resolver import resolve_name, short_id

    try:
        skill = vault.get_skill(skill_id)
    except KeyError:
        ui.label("Skill not found in vault.").classes("s-text-danger").style("font-size: 12px;")
        return

    proficiency = getattr(skill, "proficiency_level", "") or ""
    tool_names = getattr(skill, "required_tool_names", []) or []
    tool_ids = getattr(skill, "required_tool_ids", []) or []
    instructions = getattr(skill, "instructions_md", "") or ""
    skill_md_path = getattr(skill, "skill_md_path", "") or ""
    evidence = getattr(skill, "evidence_scroll_ids", []) or []

    with ui.row().classes("w-full flex-wrap").style("gap: 24px;"):
        with ui.column().style("gap: 8px; min-width: 200px;"):
            if proficiency:
                with ui.row().style("gap: 6px; align-items: center;"):
                    ui.label("Proficiency:").classes("s-field-label")
                    ui.html(f'<span class="s-pill s-pill--accent">{proficiency}</span>')

            display_tools = tool_names or [
                vault.get_tool(tid).name for tid in tool_ids[:5]
                if _safe_tool_name(tid, vault)
            ]
            if display_tools:
                ui.label("Required Tools:").classes("s-field-label")
                with ui.row().classes("flex-wrap").style("gap: 4px;"):
                    for t in display_tools:
                        ui.html(f'<span class="s-tool-chip">{t}</span>')

            if evidence:
                ui.label("Evidence Scrolls:").classes("s-field-label")
                for sid in evidence:
                    with ui.row().style("gap: 6px; align-items: baseline;"):
                        ui.label(f"• {resolve_name(sid, vault)}").classes("s-cell").style(
                            "font-size: 11px;")
                        ui.label(short_id(sid)).classes("s-mono")

        if instructions or skill_md_path:
            with ui.column().style("flex: 1; min-width: 300px; gap: 6px;"):
                ui.label("Instructions:").classes("s-field-label")
                content = instructions
                if skill_md_path and Path(skill_md_path).exists():
                    try:
                        content = Path(skill_md_path).read_text(encoding="utf-8")
                    except OSError:
                        pass
                ui.code(content, language="markdown").classes("s-skill-md").style(
                    "width: 100%; max-height: 260px; overflow: auto; "
                    "border-radius: 8px; font-size: 11px;"
                )


def _safe_tool_name(tool_id: str, vault) -> str:
    try:
        return vault.get_tool(tool_id).name
    except Exception:
        return ""
