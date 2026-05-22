"""NiceGUI Dashboard — Workshop page (Phase 1: editable artifacts).

Tabs:
  Scrolls    — interactive LLM rebuild (unchanged)
  Activities — delegated to activities page (unchanged)
  Shadows    — editable: name, description, identity_block (active-lock enforced).
               accumulated_voice is shown read-only — consolidator owns the writes.
  Tools      — editable: metadata + behavior fields; contract fields locked
  Skills     — editable: all fields
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

from systemu.core.utils import utcnow

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_workshop_page() -> None:
    state = AppState.get()
    vault = state.vault

    ui.label("🛠️ Workshop").style(
        f"font-size: 22px; font-weight: 800; color: {THEME['text']}; margin-bottom: 20px;"
    )

    with ui.tabs().classes("w-full") as tabs:
        tab_scrolls    = ui.tab("Scrolls")
        tab_activities = ui.tab("Activities")
        tab_shadows    = ui.tab("Shadows")
        tab_tools      = ui.tab("Tools")
        tab_skills     = ui.tab("Skills")

    with ui.tab_panels(tabs, value=tab_scrolls).classes("w-full bg-transparent"):

        with ui.tab_panel(tab_scrolls):
            _scrolls_tab(state, vault)

        with ui.tab_panel(tab_activities):
            from systemu.interface.pages.activities import build_activities_page
            build_activities_page()

        with ui.tab_panel(tab_shadows):
            _shadows_tab(vault)

        with ui.tab_panel(tab_tools):
            _tools_tab(vault)

        with ui.tab_panel(tab_skills):
            _skills_tab(vault)


# ─────────────────────────────────────────────────────────────────────────────
#  Scrolls tab — keep existing interactive behaviour intact
# ─────────────────────────────────────────────────────────────────────────────

def _scrolls_tab(state, vault) -> None:
    scrolls = vault.load_index("scrolls")
    if not scrolls:
        ui.label("No scrolls available.").style(f"color: {THEME['text_muted']};")
        return

    ui.label("Select a Scroll to modify:").style(f"font-weight: 600; color: {THEME['text']};")
    options = {s["id"]: f"{s['name']} (ID: {s['id']})" for s in scrolls}
    selected_scroll = ui.radio(options).style(f"color: {THEME['text']}; margin-bottom: 20px;")

    def show_scroll_data():
        if not selected_scroll.value:
            ui.notify("Please select a Scroll first", type="warning")
            return
        try:
            s_data = vault.get_scroll(selected_scroll.value).model_dump(mode="json")
            with ui.dialog() as dlg, ui.card().style(
                f"background: {THEME['surface']}; max-width: 800px; width: 100%;"
            ):
                ui.label("Scroll Full Data").classes("text-lg font-bold")
                ui.code(json.dumps(s_data, indent=2), language="json").style(
                    "max-height: 500px; overflow-y: auto;"
                )
                ui.button("Close", on_click=dlg.close)
            dlg.open()
        except Exception as exc:
            ui.notify(f"Error reading scroll: {exc}", type="negative")

    ui.button("Read Selected Scroll", on_click=show_scroll_data).style(
        f"background: {THEME['surface2']}; color: {THEME['text']}; margin-bottom: 20px;"
    )

    ui.label("Modification Instructions:").style(f"font-weight: 600; color: {THEME['text']};")
    prompt_input = ui.textarea("e.g. 'Make the narrative markdown more formal...'").classes("w-full").style(
        f"background: {THEME['surface']}; color: {THEME['text']}; margin-bottom: 20px;"
    )

    async def on_rebuild():
        if not selected_scroll.value:
            ui.notify("Please select a Scroll first", type="warning")
            return
        if not prompt_input.value.strip():
            ui.notify("Please provide a modification prompt", type="warning")
            return
        ui.notify("Rebuilding Scroll... This may take a few moments.", type="info")
        from systemu.pipelines.workshop_module import rebuild_scroll
        try:
            updated = await rebuild_scroll(
                selected_scroll.value, prompt_input.value, state.config, vault
            )
            ui.notify(
                f"Scroll \"{updated.name}\" rebuilt — check Notifications to re-approve and re-run extraction.",
                type="positive",
                timeout=8000,
            )
        except ValueError as ve:
            ui.notify(str(ve), type="negative", timeout=10000)
        except Exception as exc:
            ui.notify(f"Unexpected error: {exc}", type="negative")

    ui.button("Rebuild Scroll", on_click=on_rebuild).style(
        f"background: {THEME['primary']}; color: white; padding: 10px 20px;"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Shadows tab — editable (active-shadow lock enforced)
# ─────────────────────────────────────────────────────────────────────────────

def _shadows_tab(vault) -> None:
    shadows = vault.load_index("shadow_army")
    if not shadows:
        ui.label("No shadows available.").style(f"color: {THEME['text_muted']};")
        return

    _hint("Metadata and behaviour fields are editable. Shadows currently executing (ACTIVE) are locked.")

    for s_hdr in shadows:
        with ui.card().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"margin-bottom: 10px; width: 100%;"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column():
                    ui.label(s_hdr.get("name", s_hdr["id"])).classes("font-bold text-lg").style(
                        f"color: {THEME['text']};"
                    )
                    ui.label(
                        f"Status: {s_hdr.get('status', 'unknown')}  |  "
                        f"Skills: {len(s_hdr.get('skill_ids', []))}  |  "
                        f"Tools: {len(s_hdr.get('available_tool_ids', s_hdr.get('tool_ids', [])))}"
                    ).style(f"color: {THEME['text_muted']};")

                shadow_id = s_hdr["id"]
                ui.button("Edit", on_click=lambda sid=shadow_id: _open_shadow_edit(sid, vault)).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']};"
                )


def _open_shadow_edit(shadow_id: str, vault) -> None:
    from systemu.pipelines.evolution_policy import active_shadow_lock, record_workshop_edit
    from systemu.interface.notifications import log_event

    try:
        shadow = vault.get_shadow(shadow_id)
    except KeyError:
        ui.notify(f"Shadow {shadow_id} not found.", type="negative")
        return

    try:
        active_shadow_lock(shadow_id, vault)
    except RuntimeError as exc:
        ui.notify(str(exc), type="warning", timeout=8000)
        return

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; min-width: 600px; max-width: 800px; width: 100%;"
    ):
        ui.label(f"Edit Shadow: {shadow.name}").classes("text-lg font-bold").style(
            f"color: {THEME['text']}; margin-bottom: 12px;"
        )

        _section_label("Metadata")
        f_name = ui.input("Name", value=shadow.name).classes("w-full")

        f_desc = ui.textarea("Description", value=shadow.description).classes("w-full")

        # identity split — two-field editor.  `identity_block` is the
        # operator-controlled persona contract.  `accumulated_voice` is
        # consolidator-grown (see docs/memory-model.md) and shown
        # read-only so the operator can audit what the runtime has
        # learned without being able to overwrite it from the UI.
        _section_label("Identity (you control)")
        f_identity = ui.textarea(
            "Identity Block",
            value=shadow.identity_block,
        ).classes("w-full").style("min-height: 160px;")
        ui.label(
            "Persona contract — name, role, expertise, communication style, "
            "hard constraints.  ~500 tokens max."
        ).style(f"color: {THEME['text_muted']}; font-size: 12px; margin-top: 4px;")

        _section_label("Accumulated Voice (consolidator-grown, read-only)")
        ui.textarea(
            "Accumulated Voice",
            value=shadow.accumulated_voice or "(empty — no consolidation has run for this Shadow yet)",
        ).classes("w-full").props("readonly").style(
            "min-height: 100px; opacity: 0.7;"
        )
        ui.label(
            "Traits the runtime has observed across executions.  Owned by "
            "the memory consolidator; not editable here.  See "
            "docs/memory-model.md."
        ).style(f"color: {THEME['text_muted']}; font-size: 12px; margin-top: 4px;")

        # per-shadow Intelligent Supervisor toggle.  When on,
        # the v0.4.0 supervisor activates for this specific shadow even if
        # the global SYSTEMU_INTELLIGENT_SUPERVISOR flag is off.  Lets the
        # operator A/B test the supervisor on one specialist before
        # flipping the global switch.
        _section_label("Intelligent Supervisor (per-shadow opt-in)")
        f_supervisor = ui.switch(
            "Enable Intelligent Supervisor for this shadow",
            value=bool(getattr(shadow, "supervisor_enabled", False)),
        )
        ui.label(
            "When ON, the v0.4.0 Intelligent Supervisor observes this shadow's "
            "executions and may inject reflection blocks, force rollbacks, or "
            "publish operator approval cards on failure clusters.  See "
            "docs/intelligent-supervisor.md for the full action vocabulary."
        ).style(
            f"color: {THEME['text_muted']}; font-size: 12px; margin-top: 4px;"
        )

        # operator-labelled specialty for routing preference.
        _section_label("Specialty (routing preference)")
        f_specialty = ui.input(
            "Specialty tag",
            value=str(getattr(shadow, "specialty", "") or ""),
        ).classes("w-full").props("placeholder=e.g. browser, data-pipeline, devops")
        ui.label(
            "Optional free-form tag.  When the Supervisor swaps to an alternative "
            "shadow on TERMINATE, candidates with the same specialty as the "
            "original are preferred (ties broken by skill overlap and success "
            "history).  Leave blank for no preference."
        ).style(
            f"color: {THEME['text_muted']}; font-size: 12px; margin-top: 4px;"
        )

        _section_label("Read-only info")
        ui.label(f"Status: {shadow.status.value}").style(f"color: {THEME['text_muted']};")
        ui.label(f"Assigned activities: {len(shadow.assigned_activity_ids)}").style(
            f"color: {THEME['text_muted']};"
        )

        with ui.row().classes("gap-2 mt-4"):
            def _save():
                new_name       = f_name.value.strip()
                new_desc       = f_desc.value.strip()
                new_identity   = f_identity.value
                new_supervisor = bool(f_supervisor.value)
                new_specialty  = str(f_specialty.value or "").strip()

                if not new_name:
                    ui.notify("Name cannot be empty.", type="warning")
                    return

                prev = {
                    "name": shadow.name,
                    "description": shadow.description,
                    "identity_block": shadow.identity_block,
                    "supervisor_enabled": bool(getattr(shadow, "supervisor_enabled", False)),
                    "specialty": str(getattr(shadow, "specialty", "") or ""),
                }
                changed: Dict[str, Any] = {}
                if new_name       != shadow.name:           changed["name"]           = new_name
                if new_desc       != shadow.description:    changed["description"]    = new_desc
                if new_identity   != shadow.identity_block: changed["identity_block"] = new_identity
                if new_supervisor != bool(getattr(shadow, "supervisor_enabled", False)):
                    changed["supervisor_enabled"] = new_supervisor
                if new_specialty != str(getattr(shadow, "specialty", "") or ""):
                    changed["specialty"] = new_specialty

                if not changed:
                    ui.notify("No changes detected.", type="info")
                    dlg.close()
                    return

                shadow.name           = new_name
                shadow.description    = new_desc
                shadow.identity_block = new_identity
                shadow.supervisor_enabled = new_supervisor
                shadow.specialty      = new_specialty
                # accumulated_voice is NOT touched — consolidator-owned.
                shadow.updated_at     = utcnow()

                try:
                    vault.save_shadow(shadow)
                    record_workshop_edit(
                        artifact_type="shadow",
                        artifact_id=shadow.id,
                        fields_changed=list(changed.keys()),
                        previous_values=prev,
                        new_values={k: getattr(shadow, k) for k in changed},
                        vault=vault,
                    )
                    log_event("SUCCESS", "workshop",
                              f"Shadow '{shadow.name}' updated via workshop",
                              {"shadow_id": shadow.id, "fields": list(changed.keys())})
                    ui.notify(f"Shadow '{shadow.name}' saved.", type="positive")
                    dlg.close()
                except Exception as exc:
                    ui.notify(f"Save failed: {exc}", type="negative")

            ui.button("Save", on_click=_save).style(
                f"background: {THEME['primary']}; color: white;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']};"
            )

    dlg.open()


# ─────────────────────────────────────────────────────────────────────────────
#  Tools tab — contract fields locked, behavior + metadata editable
# ─────────────────────────────────────────────────────────────────────────────

def _tools_tab(vault) -> None:
    tools = vault.load_index("tools")
    if not tools:
        ui.label("No tools available.").style(f"color: {THEME['text_muted']};")
        return

    _hint(
        "Metadata (name) and behaviour (description, implementation_notes, dependencies) "
        "are editable. Contract fields (tool_type, parameter/return schemas) are locked — "
        "changing them would break existing callers."
    )

    for t_hdr in tools:
        with ui.card().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"margin-bottom: 10px; width: 100%;"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column():
                    ui.label(t_hdr.get("name", t_hdr["id"])).classes("font-bold text-lg").style(
                        f"color: {THEME['text']};"
                    )
                    ui.label(
                        f"Type: {t_hdr.get('tool_type', '—')}  |  "
                        f"Status: {t_hdr.get('status', '—')}"
                    ).style(f"color: {THEME['text_muted']};")
                    ui.label(t_hdr.get("description", "—")).style(f"color: {THEME['text_muted']};")

                tool_id = t_hdr["id"]
                ui.button("Edit", on_click=lambda tid=tool_id: _open_tool_edit(tid, vault)).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']};"
                )


def _open_tool_edit(tool_id: str, vault) -> None:
    from systemu.pipelines.evolution_policy import record_workshop_edit
    from systemu.interface.notifications import log_event

    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        ui.notify(f"Tool {tool_id} not found.", type="negative")
        return

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; min-width: 620px; max-width: 860px; width: 100%;"
    ):
        ui.label(f"Edit Tool: {tool.name}").classes("text-lg font-bold").style(
            f"color: {THEME['text']}; margin-bottom: 12px;"
        )

        _section_label("Metadata (editable)")
        f_name = ui.input("Name", value=tool.name).classes("w-full")

        _section_label("Behaviour (editable)")
        f_desc  = ui.textarea("Description", value=tool.description).classes("w-full")
        f_notes = ui.textarea("Implementation Notes", value=tool.implementation_notes).classes("w-full")
        f_deps  = ui.input(
            "Dependencies (comma-separated)",
            value=", ".join(tool.dependencies),
        ).classes("w-full")

        _section_label("Contract (locked — cannot change without versioning)")
        _locked_field("Tool Type", tool.tool_type.value if hasattr(tool.tool_type, "value") else str(tool.tool_type))
        _locked_field("Parameters Schema", json.dumps(tool.parameters_schema, indent=2))
        _locked_field("Return Schema", json.dumps(tool.return_schema, indent=2))

        with ui.row().classes("gap-2 mt-4"):
            def _save():
                new_name  = f_name.value.strip()
                new_desc  = f_desc.value.strip()
                new_notes = f_notes.value.strip()
                new_deps  = [d.strip() for d in f_deps.value.split(",") if d.strip()]

                if not new_name:
                    ui.notify("Name cannot be empty.", type="warning")
                    return

                prev = {
                    "name": tool.name,
                    "description": tool.description,
                    "implementation_notes": tool.implementation_notes,
                    "dependencies": list(tool.dependencies),
                }
                changed: Dict[str, Any] = {}
                if new_name  != tool.name:                 changed["name"]                 = new_name
                if new_desc  != tool.description:          changed["description"]           = new_desc
                if new_notes != tool.implementation_notes: changed["implementation_notes"]  = new_notes
                if new_deps  != list(tool.dependencies):   changed["dependencies"]          = new_deps

                if not changed:
                    ui.notify("No changes detected.", type="info")
                    dlg.close()
                    return

                tool.name                 = new_name
                tool.description          = new_desc
                tool.implementation_notes = new_notes
                tool.dependencies         = new_deps
                tool.updated_at           = utcnow()

                try:
                    vault.save_tool(tool)
                    record_workshop_edit(
                        artifact_type="tool",
                        artifact_id=tool.id,
                        fields_changed=list(changed.keys()),
                        previous_values=prev,
                        new_values={k: getattr(tool, k) for k in changed},
                        vault=vault,
                    )
                    log_event("SUCCESS", "workshop",
                              f"Tool '{tool.name}' updated via workshop",
                              {"tool_id": tool.id, "fields": list(changed.keys())})
                    ui.notify(f"Tool '{tool.name}' saved.", type="positive")
                    dlg.close()
                except Exception as exc:
                    ui.notify(f"Save failed: {exc}", type="negative")

            ui.button("Save", on_click=_save).style(
                f"background: {THEME['primary']}; color: white;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']};"
            )

    dlg.open()


# ─────────────────────────────────────────────────────────────────────────────
#  Skills tab — all fields editable (no contract surface)
# ─────────────────────────────────────────────────────────────────────────────

def _skills_tab(vault) -> None:
    skills = vault.load_index("skills")
    if not skills:
        ui.label("No skills available.").style(f"color: {THEME['text_muted']};")
        return

    _hint("All skill fields are editable. Changes are recorded as evolution entries.")

    for sk_hdr in skills:
        with ui.card().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"margin-bottom: 10px; width: 100%;"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column():
                    ui.label(sk_hdr.get("name", sk_hdr["id"])).classes("font-bold text-lg").style(
                        f"color: {THEME['text']};"
                    )
                    ui.label(f"Category: {sk_hdr.get('category', '—')}").style(
                        f"color: {THEME['text_muted']};"
                    )
                    ui.label(sk_hdr.get("description", "—")).style(f"color: {THEME['text_muted']};")

                skill_id = sk_hdr["id"]
                ui.button("Edit", on_click=lambda sid=skill_id: _open_skill_edit(sid, vault)).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']};"
                )


def _open_skill_edit(skill_id: str, vault) -> None:
    from systemu.pipelines.evolution_policy import record_workshop_edit
    from systemu.interface.notifications import log_event

    try:
        skill = vault.get_skill(skill_id)
    except KeyError:
        ui.notify(f"Skill {skill_id} not found.", type="negative")
        return

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; min-width: 620px; max-width: 860px; width: 100%;"
    ):
        ui.label(f"Edit Skill: {skill.name}").classes("text-lg font-bold").style(
            f"color: {THEME['text']}; margin-bottom: 12px;"
        )

        _section_label("Metadata")
        f_name  = ui.input("Name", value=skill.name).classes("w-full")
        f_desc  = ui.textarea("Description", value=skill.description).classes("w-full")
        f_level = ui.select(
            ["beginner", "intermediate", "expert"],
            value=skill.proficiency_level or "intermediate",
            label="Proficiency Level",
        ).classes("w-full")

        _section_label("Behaviour")
        f_cat   = ui.input("Category", value=skill.category).classes("w-full")
        f_instr = ui.textarea("Instructions (Markdown)", value=skill.instructions_md).classes("w-full").style(
            "min-height: 180px;"
        )

        with ui.row().classes("gap-2 mt-4"):
            def _save():
                new_name  = f_name.value.strip()
                new_desc  = f_desc.value.strip()
                new_level = f_level.value
                new_cat   = f_cat.value.strip()
                new_instr = f_instr.value

                if not new_name:
                    ui.notify("Name cannot be empty.", type="warning")
                    return

                prev = {
                    "name": skill.name,
                    "description": skill.description,
                    "proficiency_level": skill.proficiency_level,
                    "category": skill.category,
                    "instructions_md": skill.instructions_md,
                }
                changed: Dict[str, Any] = {}
                if new_name  != skill.name:               changed["name"]               = new_name
                if new_desc  != skill.description:        changed["description"]         = new_desc
                if new_level != skill.proficiency_level:  changed["proficiency_level"]   = new_level
                if new_cat   != skill.category:           changed["category"]            = new_cat
                if new_instr != skill.instructions_md:    changed["instructions_md"]     = new_instr

                if not changed:
                    ui.notify("No changes detected.", type="info")
                    dlg.close()
                    return

                skill.name               = new_name
                skill.description        = new_desc
                skill.proficiency_level  = new_level
                skill.category           = new_cat
                skill.instructions_md    = new_instr
                skill.updated_at         = utcnow()

                try:
                    vault.save_skill(skill)
                    record_workshop_edit(
                        artifact_type="skill",
                        artifact_id=skill.id,
                        fields_changed=list(changed.keys()),
                        previous_values=prev,
                        new_values={k: getattr(skill, k) for k in changed},
                        vault=vault,
                    )
                    log_event("SUCCESS", "workshop",
                              f"Skill '{skill.name}' updated via workshop",
                              {"skill_id": skill.id, "fields": list(changed.keys())})
                    ui.notify(f"Skill '{skill.name}' saved.", type="positive")
                    dlg.close()
                except Exception as exc:
                    ui.notify(f"Save failed: {exc}", type="negative")

            ui.button("Save", on_click=_save).style(
                f"background: {THEME['primary']}; color: white;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']};"
            )

    dlg.open()


# ─────────────────────────────────────────────────────────────────────────────
#  Shared UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section_label(text: str) -> None:
    ui.label(text).style(
        f"font-weight: 700; color: {THEME['text']}; margin-top: 14px; margin-bottom: 4px;"
    )


def _hint(text: str) -> None:
    ui.label(text).style(
        f"color: {THEME['text_muted']}; font-size: 12px; margin-bottom: 12px;"
    )


def _locked_field(label: str, value: str) -> None:
    """Display a contract field as read-only with a visual lock indicator."""
    with ui.card().style(
        f"background: {THEME['surface2']}; border: 1px solid {THEME['border']}; "
        f"margin-bottom: 6px; padding: 8px; width: 100%;"
    ):
        ui.label(f"🔒 {label}").style(f"font-size: 11px; color: {THEME['text_muted']}; font-weight: 600;")
        ui.label(value).style(
            f"font-family: monospace; font-size: 11px; color: {THEME['text']}; "
            f"white-space: pre-wrap; word-break: break-all;"
        )
