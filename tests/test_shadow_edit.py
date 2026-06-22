"""Phase 5 Slice 4c — shadow edit-in-place (entity_edit).

The Workshop's ``_open_shadow_edit`` dialog body was lifted into
``systemu.interface.components.entity_edit`` (alongside the tool/skill variants
from Slice 3c) so the Build/Shadows registry rows can open it in-page instead of
deep-linking to the dissolving Workshop Shadows tab.

Same split-the-data-from-the-paint discipline as the tool/skill variants — the
NiceGUI dialog shell can't run headless, so these tests exercise:

  * the pure change-detection helper (``shadow_edit_changes``) over the FIVE
    editable fields (name / description / identity_block / supervisor_enabled /
    specialty);
  * the save applier (``apply_shadow_edit``) — that it mutates the entity to the
    edited values, calls ``vault.save_shadow`` AND
    ``record_workshop_edit(artifact_type="shadow")`` with the edited fields,
    fires ``log_event`` + ``on_saved``, sets ``updated_at``, and LEAVES
    ``accumulated_voice`` untouched (consolidator-owned);
  * THE DIFFERENCE from tool/skill — the ``active_shadow_lock`` gate: an ACTIVE
    shadow refuses to save (no mutation, no vault write).

The save contract (the same comparisons + the single audit call-site) is
preserved unchanged — these are the Workshop ``_save`` comparisons, relocated.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _shadow(**over):
    base = dict(
        id="shadow_a", name="Aria", description="Research specialist",
        identity_block="You are Aria.", supervisor_enabled=False,
        specialty="research",
        accumulated_voice="learned trait: concise",
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── pure change-detection ─────────────────────────────────────────────────────

def test_shadow_edit_changes_detects_edited_fields():
    from systemu.interface.components.entity_edit import shadow_edit_changes
    changed, prev = shadow_edit_changes(
        _shadow(),
        name="Aria-2", description="Research specialist",
        identity_block="You are Aria, reborn.", supervisor_enabled=True,
        specialty="research",
    )
    assert set(changed) == {"name", "identity_block", "supervisor_enabled"}
    assert changed["name"] == "Aria-2"
    assert changed["supervisor_enabled"] is True
    # description / specialty unchanged → not in the diff
    assert "description" not in changed
    assert "specialty" not in changed
    # previous snapshot captures the pre-edit values
    assert prev["name"] == "Aria"
    assert prev["supervisor_enabled"] is False
    # accumulated_voice is NOT part of the editable contract / diff
    assert "accumulated_voice" not in changed
    assert "accumulated_voice" not in prev


def test_shadow_edit_changes_empty_when_identical():
    from systemu.interface.components.entity_edit import shadow_edit_changes
    changed, _prev = shadow_edit_changes(
        _shadow(),
        name="Aria", description="Research specialist",
        identity_block="You are Aria.", supervisor_enabled=False,
        specialty="research",
    )
    assert changed == {}


# ── save applier: the preserved save path ────────────────────────────────────

def test_apply_shadow_edit_saves_and_records():
    """apply_shadow_edit mutates the shadow, calls vault.save_shadow +
    record_workshop_edit (artifact_type='shadow') with the edited fields, fires
    log_event + on_saved, sets updated_at, and leaves accumulated_voice
    untouched."""
    import systemu.interface.components.entity_edit as ee

    shadow = _shadow()
    vault = MagicMock()
    fired = []

    with patch.object(ee, "record_workshop_edit") as rec, \
         patch.object(ee, "log_event") as log, \
         patch.object(ee, "active_shadow_lock") as lock:
        ok = ee.apply_shadow_edit(
            shadow, vault,
            name="Aria-2", description="Research specialist",
            identity_block="You are Aria, reborn.", supervisor_enabled=True,
            specialty="research",
            on_saved=lambda: fired.append(True),
        )

    assert ok is True
    # the lock gate ran (and did not raise) before the save
    lock.assert_called_once_with(shadow.id, vault)
    # entity mutated to the edited values
    assert shadow.name == "Aria-2"
    assert shadow.identity_block == "You are Aria, reborn."
    assert shadow.supervisor_enabled is True
    # updated_at stamped
    assert getattr(shadow, "updated_at", None) is not None
    # accumulated_voice untouched — consolidator-owned
    assert shadow.accumulated_voice == "learned trait: concise"
    # save path preserved: save_shadow + record_workshop_edit + log_event
    vault.save_shadow.assert_called_once_with(shadow)
    rec.assert_called_once()
    kw = rec.call_args.kwargs
    assert kw["artifact_type"] == "shadow"
    assert kw["artifact_id"] == "shadow_a"
    assert set(kw["fields_changed"]) == {"name", "identity_block", "supervisor_enabled"}
    assert kw["vault"] is vault
    log.assert_called_once()
    assert fired == [True]


def test_apply_shadow_edit_noop_when_unchanged():
    """No edits → no save, no record, returns False (caller closes the dialog).

    The lock still runs first (it gates the whole operation)."""
    import systemu.interface.components.entity_edit as ee
    shadow = _shadow()
    vault = MagicMock()
    with patch.object(ee, "record_workshop_edit") as rec, \
         patch.object(ee, "log_event"), \
         patch.object(ee, "active_shadow_lock"):
        ok = ee.apply_shadow_edit(
            shadow, vault,
            name="Aria", description="Research specialist",
            identity_block="You are Aria.", supervisor_enabled=False,
            specialty="research",
        )
    assert ok is False
    vault.save_shadow.assert_not_called()
    rec.assert_not_called()


def test_apply_shadow_edit_refuses_when_active():
    """An ACTIVE shadow (active_shadow_lock raises) → apply_shadow_edit raises and
    performs NO mutation / NO vault write — the contract is enforced in the
    testable layer, not just the dialog shell."""
    import systemu.interface.components.entity_edit as ee
    import pytest

    shadow = _shadow()
    vault = MagicMock()

    with patch.object(ee, "active_shadow_lock", side_effect=RuntimeError("ACTIVE")), \
         patch.object(ee, "record_workshop_edit") as rec, \
         patch.object(ee, "log_event"):
        with pytest.raises(RuntimeError):
            ee.apply_shadow_edit(
                shadow, vault,
                name="Aria-2", description="changed",
                identity_block="changed", supervisor_enabled=True,
                specialty="changed",
            )

    # no save, no audit record, entity not mutated
    vault.save_shadow.assert_not_called()
    rec.assert_not_called()
    assert shadow.name == "Aria"


# ── dialog opener importable with the documented signature ───────────────────

def test_shadow_dialog_opener_importable():
    import inspect
    from systemu.interface.components import entity_edit

    fn = getattr(entity_edit, "open_shadow_edit_dialog")
    sig = inspect.signature(fn)
    assert "on_saved" in sig.parameters
    assert sig.parameters["on_saved"].default is None
