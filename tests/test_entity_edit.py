"""Phase 5 Slice 3c — shared tool/skill edit dialogs (entity_edit).

The Workshop's ``_open_tool_edit`` / ``_open_skill_edit`` dialog bodies were
lifted VERBATIM-in-behaviour into ``systemu.interface.components.entity_edit``
so the Build registry rows can open them in-page (edit-in-place) instead of
deep-linking to the dissolving Workshop.

The NiceGUI dialog shell can't run headless, so — same split-the-data-from-the-
paint discipline as ``entity_rows`` — these tests exercise:

  * the pure change-detection helpers (``tool_edit_changes`` /
    ``skill_edit_changes``) that decide which fields actually changed;
  * the save appliers (``apply_tool_edit`` / ``apply_skill_edit``) — that they
    mutate the entity to the edited values, call ``vault.save_*`` AND
    ``record_workshop_edit`` with the edited fields, and fire ``on_saved``.

The SAVE CONTRACT (vault round-trip + change-detection) that
``test_v042b_workshop_toggle.py`` guards is preserved unchanged — these are the
same comparisons, just relocated.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ── pure change-detection: tool ──────────────────────────────────────────────

def _tool(**over):
    base = dict(
        id="tool_a", name="fetch_json", description="Fetch JSON",
        implementation_notes="notes", dependencies=["requests"],
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_tool_edit_changes_detects_edited_fields():
    from systemu.interface.components.entity_edit import tool_edit_changes
    changed, prev = tool_edit_changes(
        _tool(),
        name="fetch_json2", description="Fetch JSON",
        implementation_notes="new notes", dependencies=["requests", "httpx"],
    )
    assert set(changed) == {"name", "implementation_notes", "dependencies"}
    assert changed["name"] == "fetch_json2"
    assert changed["dependencies"] == ["requests", "httpx"]
    # description unchanged → not in the diff
    assert "description" not in changed
    # previous snapshot captures the pre-edit values
    assert prev["name"] == "fetch_json"
    assert prev["dependencies"] == ["requests"]


def test_tool_edit_changes_empty_when_identical():
    from systemu.interface.components.entity_edit import tool_edit_changes
    changed, _prev = tool_edit_changes(
        _tool(),
        name="fetch_json", description="Fetch JSON",
        implementation_notes="notes", dependencies=["requests"],
    )
    assert changed == {}


# ── pure change-detection: skill ─────────────────────────────────────────────

def _skill(**over):
    base = dict(
        id="skill_a", name="email_summary", description="Summarize threads",
        proficiency_level="intermediate", category="communication",
        instructions_md="# do it",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_skill_edit_changes_detects_edited_fields():
    from systemu.interface.components.entity_edit import skill_edit_changes
    changed, prev = skill_edit_changes(
        _skill(),
        name="email_summary", description="Summarize email threads",
        proficiency_level="expert", category="communication",
        instructions_md="# do it",
    )
    assert set(changed) == {"description", "proficiency_level"}
    assert changed["proficiency_level"] == "expert"
    assert prev["proficiency_level"] == "intermediate"


def test_skill_edit_changes_empty_when_identical():
    from systemu.interface.components.entity_edit import skill_edit_changes
    changed, _prev = skill_edit_changes(
        _skill(),
        name="email_summary", description="Summarize threads",
        proficiency_level="intermediate", category="communication",
        instructions_md="# do it",
    )
    assert changed == {}


# ── save appliers: the preserved save path ───────────────────────────────────

def test_apply_tool_edit_saves_and_records():
    """apply_tool_edit mutates the tool, calls vault.save_tool +
    record_workshop_edit (artifact_type='tool') with the edited fields, and
    fires on_saved."""
    import systemu.interface.components.entity_edit as ee

    tool = _tool()
    vault = MagicMock()
    fired = []

    with patch.object(ee, "record_workshop_edit") as rec, \
         patch.object(ee, "log_event") as log:
        ok = ee.apply_tool_edit(
            tool, vault,
            name="fetch_json2", description="Fetch JSON",
            implementation_notes="new notes", dependencies=["requests", "httpx"],
            on_saved=lambda: fired.append(True),
        )

    assert ok is True
    # entity mutated to the edited values
    assert tool.name == "fetch_json2"
    assert tool.implementation_notes == "new notes"
    assert tool.dependencies == ["requests", "httpx"]
    # save path preserved: save_tool + record_workshop_edit + log_event
    vault.save_tool.assert_called_once_with(tool)
    rec.assert_called_once()
    kw = rec.call_args.kwargs
    assert kw["artifact_type"] == "tool"
    assert kw["artifact_id"] == "tool_a"
    assert set(kw["fields_changed"]) == {"name", "implementation_notes", "dependencies"}
    assert kw["vault"] is vault
    log.assert_called_once()
    assert fired == [True]


def test_apply_tool_edit_noop_when_unchanged():
    """No edits → no save, no record, returns False (caller closes the dialog)."""
    import systemu.interface.components.entity_edit as ee
    tool = _tool()
    vault = MagicMock()
    with patch.object(ee, "record_workshop_edit") as rec, \
         patch.object(ee, "log_event"):
        ok = ee.apply_tool_edit(
            tool, vault,
            name="fetch_json", description="Fetch JSON",
            implementation_notes="notes", dependencies=["requests"],
        )
    assert ok is False
    vault.save_tool.assert_not_called()
    rec.assert_not_called()


def test_apply_skill_edit_saves_and_records():
    import systemu.interface.components.entity_edit as ee

    skill = _skill()
    vault = MagicMock()
    fired = []

    with patch.object(ee, "record_workshop_edit") as rec, \
         patch.object(ee, "log_event") as log:
        ok = ee.apply_skill_edit(
            skill, vault,
            name="email_summary", description="Summarize email threads",
            proficiency_level="expert", category="comms",
            instructions_md="# do it better",
            on_saved=lambda: fired.append(True),
        )

    assert ok is True
    assert skill.description == "Summarize email threads"
    assert skill.proficiency_level == "expert"
    assert skill.category == "comms"
    assert skill.instructions_md == "# do it better"
    vault.save_skill.assert_called_once_with(skill)
    rec.assert_called_once()
    kw = rec.call_args.kwargs
    assert kw["artifact_type"] == "skill"
    assert kw["artifact_id"] == "skill_a"
    assert set(kw["fields_changed"]) == {
        "description", "proficiency_level", "category", "instructions_md"
    }
    log.assert_called_once()
    assert fired == [True]


def test_apply_skill_edit_noop_when_unchanged():
    import systemu.interface.components.entity_edit as ee
    skill = _skill()
    vault = MagicMock()
    with patch.object(ee, "record_workshop_edit") as rec, \
         patch.object(ee, "log_event"):
        ok = ee.apply_skill_edit(
            skill, vault,
            name="email_summary", description="Summarize threads",
            proficiency_level="intermediate", category="communication",
            instructions_md="# do it",
        )
    assert ok is False
    vault.save_skill.assert_not_called()
    rec.assert_not_called()


# ── dialog openers importable with the documented signatures ─────────────────

def test_dialog_openers_importable():
    import inspect
    from systemu.interface.components import entity_edit

    for fn_name in ("open_tool_edit_dialog", "open_skill_edit_dialog"):
        fn = getattr(entity_edit, fn_name)
        sig = inspect.signature(fn)
        assert "on_saved" in sig.parameters
        assert sig.parameters["on_saved"].default is None
