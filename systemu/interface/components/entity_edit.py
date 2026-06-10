"""Shared tool/skill edit dialogs (Phase 5 Slice 3c — edit-in-place).

Lifted from the dissolving Workshop's ``_open_tool_edit`` / ``_open_skill_edit``
(``workshop.py``) so the Build registry rows (``entity_rows.render_tool_row`` /
``render_skill_row``) can open them IN-PAGE instead of deep-linking to the
Workshop tab.  Behaviour is identical to the Workshop dialogs:

  * Tool — editable name / description / implementation_notes / dependencies
    (comma-split); locked tool_type / parameters_schema / return_schema.
  * Skill — editable name / description / proficiency_level / category /
    instructions_md.

The SAVE PATH is preserved verbatim: ``vault.save_*`` →
``record_workshop_edit(artifact_type=…, …)`` (the single audit-trail call-site)
→ ``log_event``.  ``on_saved`` fires after a successful save so the caller can
refresh its row/table.

The data-from-the-paint split keeps the save contract unit-testable:
``tool_edit_changes`` / ``skill_edit_changes`` are pure change-detection (same
comparisons the Workshop ``_save`` used); ``apply_tool_edit`` /
``apply_skill_edit`` mutate + persist; the ``open_*_edit_dialog`` functions are
the thin NiceGUI shell.

Styling is token-class / plain-string only (no inline f-string ``.style`` and no
raw hex), so this file adds ZERO entries to the UI-style lint baseline.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from systemu.core.utils import utcnow

# Module-level imports so tests can patch them on this module and the save path
# stays the single audit call-site.
from systemu.pipelines.evolution_policy import active_shadow_lock, record_workshop_edit
from systemu.interface.notifications import log_event


# ─────────────────────────────────────────────────────────────────────────────
#  Pure change-detection (mirrors workshop.py _save — headless-testable)
# ─────────────────────────────────────────────────────────────────────────────

def tool_edit_changes(
    tool,
    *,
    name: str,
    description: str,
    implementation_notes: str,
    dependencies: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Diff the edited tool fields against the current entity.

    Returns ``(changed, previous)`` where ``changed`` maps only the genuinely
    changed field names to their NEW values and ``previous`` is the full
    pre-edit snapshot (the ``before_snapshot`` for the audit record).  Same
    four-field comparison the Workshop dialog used.
    """
    prev: Dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "implementation_notes": tool.implementation_notes,
        "dependencies": list(tool.dependencies),
    }
    changed: Dict[str, Any] = {}
    if name != tool.name:
        changed["name"] = name
    if description != tool.description:
        changed["description"] = description
    if implementation_notes != tool.implementation_notes:
        changed["implementation_notes"] = implementation_notes
    if dependencies != list(tool.dependencies):
        changed["dependencies"] = dependencies
    return changed, prev


def skill_edit_changes(
    skill,
    *,
    name: str,
    description: str,
    proficiency_level: str,
    category: str,
    instructions_md: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Diff the edited skill fields against the current entity.

    Returns ``(changed, previous)`` — same five-field comparison the Workshop
    skill dialog used.
    """
    prev: Dict[str, Any] = {
        "name": skill.name,
        "description": skill.description,
        "proficiency_level": skill.proficiency_level,
        "category": skill.category,
        "instructions_md": skill.instructions_md,
    }
    changed: Dict[str, Any] = {}
    if name != skill.name:
        changed["name"] = name
    if description != skill.description:
        changed["description"] = description
    if proficiency_level != skill.proficiency_level:
        changed["proficiency_level"] = proficiency_level
    if category != skill.category:
        changed["category"] = category
    if instructions_md != skill.instructions_md:
        changed["instructions_md"] = instructions_md
    return changed, prev


def shadow_edit_changes(
    shadow,
    *,
    name: str,
    description: str,
    identity_block: str,
    supervisor_enabled: bool,
    specialty: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Diff the FIVE editable shadow fields against the current entity.

    Returns ``(changed, previous)`` — the same five-field comparison the
    Workshop shadow dialog used.  ``accumulated_voice`` is consolidator-owned and
    deliberately NOT part of this contract; ``status`` and
    ``assigned_activity_ids`` are read-only.
    """
    prev: Dict[str, Any] = {
        "name": shadow.name,
        "description": shadow.description,
        "identity_block": shadow.identity_block,
        "supervisor_enabled": bool(getattr(shadow, "supervisor_enabled", False)),
        "specialty": str(getattr(shadow, "specialty", "") or ""),
    }
    changed: Dict[str, Any] = {}
    if name != shadow.name:
        changed["name"] = name
    if description != shadow.description:
        changed["description"] = description
    if identity_block != shadow.identity_block:
        changed["identity_block"] = identity_block
    if supervisor_enabled != bool(getattr(shadow, "supervisor_enabled", False)):
        changed["supervisor_enabled"] = supervisor_enabled
    if specialty != str(getattr(shadow, "specialty", "") or ""):
        changed["specialty"] = specialty
    return changed, prev


# ─────────────────────────────────────────────────────────────────────────────
#  Save appliers — the preserved save path (save_* + record_workshop_edit + log)
# ─────────────────────────────────────────────────────────────────────────────

def apply_tool_edit(
    tool,
    vault,
    *,
    name: str,
    description: str,
    implementation_notes: str,
    dependencies: List[str],
    on_saved: Optional[Callable[[], None]] = None,
) -> bool:
    """Persist edited tool fields. Returns True iff something was saved.

    No-op (returns False) when nothing changed — the dialog just closes. On a
    real change: mutate the entity, ``vault.save_tool`` →
    ``record_workshop_edit(artifact_type="tool", …)`` → ``log_event`` →
    ``on_saved()``.
    """
    changed, prev = tool_edit_changes(
        tool, name=name, description=description,
        implementation_notes=implementation_notes, dependencies=dependencies,
    )
    if not changed:
        return False

    tool.name = name
    tool.description = description
    tool.implementation_notes = implementation_notes
    tool.dependencies = dependencies
    tool.updated_at = utcnow()

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
    if on_saved is not None:
        on_saved()
    return True


def apply_skill_edit(
    skill,
    vault,
    *,
    name: str,
    description: str,
    proficiency_level: str,
    category: str,
    instructions_md: str,
    on_saved: Optional[Callable[[], None]] = None,
) -> bool:
    """Persist edited skill fields. Returns True iff something was saved.

    Same save path as :func:`apply_tool_edit` with
    ``artifact_type="skill"``.
    """
    changed, prev = skill_edit_changes(
        skill, name=name, description=description,
        proficiency_level=proficiency_level, category=category,
        instructions_md=instructions_md,
    )
    if not changed:
        return False

    skill.name = name
    skill.description = description
    skill.proficiency_level = proficiency_level
    skill.category = category
    skill.instructions_md = instructions_md
    skill.updated_at = utcnow()

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
    if on_saved is not None:
        on_saved()
    return True


def apply_shadow_edit(
    shadow,
    vault,
    *,
    name: str,
    description: str,
    identity_block: str,
    supervisor_enabled: bool,
    specialty: str,
    on_saved: Optional[Callable[[], None]] = None,
) -> bool:
    """Persist edited shadow fields. Returns True iff something was saved.

    THE DIFFERENCE from :func:`apply_tool_edit` / :func:`apply_skill_edit`: a
    pre-save gate.  ``active_shadow_lock(shadow.id, vault)`` raises
    ``RuntimeError`` if the shadow is currently ACTIVE (mid-execution) — the lock
    is enforced HERE, in the testable layer, so the contract holds regardless of
    which UI shell calls it.  On a locked shadow this raises BEFORE any mutation
    or vault write.

    On an editable shadow: no-op (returns False) when nothing changed; otherwise
    mutate the five fields, stamp ``updated_at`` (leaving ``accumulated_voice``
    consolidator-owned and untouched), ``vault.save_shadow`` →
    ``record_workshop_edit(artifact_type="shadow", …)`` → ``log_event`` →
    ``on_saved()``.
    """
    # Pre-save gate — raises RuntimeError if the shadow is ACTIVE.  Runs first,
    # so a locked shadow is refused before any state change.
    active_shadow_lock(shadow.id, vault)

    changed, prev = shadow_edit_changes(
        shadow, name=name, description=description,
        identity_block=identity_block, supervisor_enabled=supervisor_enabled,
        specialty=specialty,
    )
    if not changed:
        return False

    shadow.name = name
    shadow.description = description
    shadow.identity_block = identity_block
    shadow.supervisor_enabled = supervisor_enabled
    shadow.specialty = specialty
    # accumulated_voice is NOT touched — consolidator-owned.
    shadow.updated_at = utcnow()

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
    if on_saved is not None:
        on_saved()
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  NiceGUI dialog shells (thin — delegate to the appliers above)
# ─────────────────────────────────────────────────────────────────────────────

def open_tool_edit_dialog(tool, vault, *, on_saved: Optional[Callable[[], None]] = None) -> None:
    """Open the in-page Tool edit dialog (lifted from workshop._open_tool_edit).

    Editable: name, description, implementation_notes, dependencies.
    Locked (contract): tool_type, parameters_schema, return_schema.
    """
    from nicegui import ui

    with ui.dialog() as dlg, ui.card().classes("s-card s-dialog"):
        ui.label(f"Edit Tool: {tool.name}").classes("s-dialog-title").style("margin-bottom: 12px;")

        _section_label("Metadata (editable)")
        f_name = ui.input("Name", value=tool.name).classes("w-full")

        _section_label("Behaviour (editable)")
        f_desc = ui.textarea("Description", value=tool.description).classes("w-full")
        f_notes = ui.textarea("Implementation Notes", value=tool.implementation_notes).classes("w-full")
        f_deps = ui.input(
            "Dependencies (comma-separated)",
            value=", ".join(tool.dependencies),
        ).classes("w-full")

        _section_label("Contract (locked — cannot change without versioning)")
        _locked_field("Tool Type", tool.tool_type.value if hasattr(tool.tool_type, "value") else str(tool.tool_type))
        _locked_field("Parameters Schema", json.dumps(tool.parameters_schema, indent=2))
        _locked_field("Return Schema", json.dumps(tool.return_schema, indent=2))

        with ui.row().classes("gap-2 mt-4"):
            def _save():
                new_name = f_name.value.strip()
                if not new_name:
                    ui.notify("Name cannot be empty.", type="warning")
                    return
                try:
                    saved = apply_tool_edit(
                        tool, vault,
                        name=new_name,
                        description=f_desc.value.strip(),
                        implementation_notes=f_notes.value.strip(),
                        dependencies=[d.strip() for d in f_deps.value.split(",") if d.strip()],
                        on_saved=on_saved,
                    )
                except Exception as exc:
                    ui.notify(f"Save failed: {exc}", type="negative")
                    return
                if saved:
                    ui.notify(f"Tool '{tool.name}' saved.", type="positive")
                else:
                    ui.notify("No changes detected.", type="info")
                dlg.close()

            ui.button("Save", on_click=_save).props("no-caps").classes("s-btn s-btn--primary")
            ui.button("Cancel", on_click=dlg.close).props("no-caps").classes("s-btn s-btn--ghost")

    dlg.open()


def open_skill_edit_dialog(skill, vault, *, on_saved: Optional[Callable[[], None]] = None) -> None:
    """Open the in-page Skill edit dialog (lifted from workshop._open_skill_edit).

    Editable: name, description, proficiency_level, category, instructions_md.
    """
    from nicegui import ui

    with ui.dialog() as dlg, ui.card().classes("s-card s-dialog"):
        ui.label(f"Edit Skill: {skill.name}").classes("s-dialog-title").style("margin-bottom: 12px;")

        _section_label("Metadata")
        f_name = ui.input("Name", value=skill.name).classes("w-full")
        f_desc = ui.textarea("Description", value=skill.description).classes("w-full")
        f_level = ui.select(
            ["beginner", "intermediate", "expert"],
            value=skill.proficiency_level or "intermediate",
            label="Proficiency Level",
        ).classes("w-full")

        _section_label("Behaviour")
        f_cat = ui.input("Category", value=skill.category).classes("w-full")
        f_instr = ui.textarea("Instructions (Markdown)", value=skill.instructions_md).classes("w-full").style(
            "min-height: 180px;"
        )

        with ui.row().classes("gap-2 mt-4"):
            def _save():
                new_name = f_name.value.strip()
                if not new_name:
                    ui.notify("Name cannot be empty.", type="warning")
                    return
                try:
                    saved = apply_skill_edit(
                        skill, vault,
                        name=new_name,
                        description=f_desc.value.strip(),
                        proficiency_level=f_level.value,
                        category=f_cat.value.strip(),
                        instructions_md=f_instr.value,
                        on_saved=on_saved,
                    )
                except Exception as exc:
                    ui.notify(f"Save failed: {exc}", type="negative")
                    return
                if saved:
                    ui.notify(f"Skill '{skill.name}' saved.", type="positive")
                else:
                    ui.notify("No changes detected.", type="info")
                dlg.close()

            ui.button("Save", on_click=_save).props("no-caps").classes("s-btn s-btn--primary")
            ui.button("Cancel", on_click=dlg.close).props("no-caps").classes("s-btn s-btn--ghost")

    dlg.open()


def open_shadow_edit_dialog(shadow, vault, *, on_saved: Optional[Callable[[], None]] = None) -> None:
    """Open the in-page Shadow edit dialog (lifted from workshop._open_shadow_edit).

    Editable: name, description, identity_block, supervisor_enabled, specialty.
    Read-only (do NOT touch): accumulated_voice (consolidator-owned), status,
    assigned_activity_ids.

    The ``active_shadow_lock`` gate is checked here so a locked (ACTIVE) shadow
    surfaces a clear notify instead of opening an editor whose Save would fail.
    The same gate is ALSO enforced inside :func:`apply_shadow_edit` — the dialog
    check is a UX shortcut, the applier check is the contract.
    """
    from nicegui import ui

    # Surface the lock as a clear notify before opening the editor.
    try:
        active_shadow_lock(shadow.id, vault)
    except RuntimeError as exc:
        ui.notify(str(exc), type="warning", timeout=8000)
        return

    with ui.dialog() as dlg, ui.card().classes("s-card s-dialog"):
        ui.label(f"Edit Shadow: {shadow.name}").classes("s-dialog-title").style("margin-bottom: 12px;")

        _section_label("Metadata")
        f_name = ui.input("Name", value=shadow.name).classes("w-full")
        f_desc = ui.textarea("Description", value=shadow.description).classes("w-full")

        # v0.3 identity split — `identity_block` is the operator-controlled
        # persona contract.  `accumulated_voice` is consolidator-grown (see
        # docs/memory-model.md) and shown read-only so the operator can audit
        # what the runtime has learned without overwriting it from the UI.
        _section_label("Identity (you control)")
        f_identity = ui.textarea(
            "Identity Block",
            value=shadow.identity_block,
        ).classes("w-full").style("min-height: 160px;")
        ui.label(
            "Persona contract — name, role, expertise, communication style, "
            "hard constraints.  ~500 tokens max."
        ).classes("s-field-label").style("margin-top: 4px;")

        _section_label("Accumulated Voice (consolidator-grown, read-only)")
        ui.textarea(
            "Accumulated Voice",
            value=shadow.accumulated_voice or "(empty — no consolidation has run for this Shadow yet)",
        ).classes("w-full").props("readonly").style("min-height: 100px; opacity: 0.7;")
        ui.label(
            "Traits the runtime has observed across executions.  Owned by the "
            "memory consolidator; not editable here.  See docs/memory-model.md."
        ).classes("s-field-label").style("margin-top: 4px;")

        # v0.4.2-b: per-shadow Intelligent Supervisor toggle.
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
        ).classes("s-field-label").style("margin-top: 4px;")

        # v0.4.3-b: operator-labelled specialty for routing preference.
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
        ).classes("s-field-label").style("margin-top: 4px;")

        _section_label("Read-only info")
        ui.label(
            f"Status: {shadow.status.value if hasattr(shadow.status, 'value') else shadow.status}"
        ).classes("s-cell s-cell--muted")
        ui.label(f"Assigned activities: {len(shadow.assigned_activity_ids)}").classes("s-cell s-cell--muted")

        with ui.row().classes("gap-2 mt-4"):
            def _save():
                new_name = f_name.value.strip()
                if not new_name:
                    ui.notify("Name cannot be empty.", type="warning")
                    return
                try:
                    saved = apply_shadow_edit(
                        shadow, vault,
                        name=new_name,
                        description=f_desc.value.strip(),
                        identity_block=f_identity.value,
                        supervisor_enabled=bool(f_supervisor.value),
                        specialty=str(f_specialty.value or "").strip(),
                        on_saved=on_saved,
                    )
                except RuntimeError as exc:
                    # active_shadow_lock fired between open and save.
                    ui.notify(str(exc), type="warning", timeout=8000)
                    return
                except Exception as exc:
                    ui.notify(f"Save failed: {exc}", type="negative")
                    return
                if saved:
                    ui.notify(f"Shadow '{shadow.name}' saved.", type="positive")
                else:
                    ui.notify("No changes detected.", type="info")
                dlg.close()

            ui.button("Save", on_click=_save).props("no-caps").classes("s-btn s-btn--primary")
            ui.button("Cancel", on_click=dlg.close).props("no-caps").classes("s-btn s-btn--ghost")

    dlg.open()


# ─────────────────────────────────────────────────────────────────────────────
#  Shared UI helpers (token-class / plain-string only)
# ─────────────────────────────────────────────────────────────────────────────

def _section_label(text: str) -> None:
    from nicegui import ui
    ui.label(text).classes("s-cell s-cell--bold").style("margin-top: 14px; margin-bottom: 4px;")


def _locked_field(label: str, value: str) -> None:
    """Display a contract field as read-only with a visual lock indicator."""
    from nicegui import ui
    with ui.card().classes("s-card").style(
        "background: var(--color-surface2); margin-bottom: 6px; padding: 8px; width: 100%;"
    ):
        ui.label(f"🔒 {label}").classes("s-field-label")
        ui.label(value).classes("s-mono").style(
            "white-space: pre-wrap; word-break: break-all;"
        )
