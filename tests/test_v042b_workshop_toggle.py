"""Tests for v0.4.2-b — Workshop UI toggle for supervisor_enabled.

The UI itself (NiceGUI dialog) is hard to test in isolation, so these
tests validate the *contract* the UI relies on:

  * Shadow.supervisor_enabled is editable + persisted via vault.save_shadow
  * Round-trip through the file vault preserves the flag
  * Toggle from False → True → False round-trips cleanly
  * The workshop edit's change-detection logic correctly identifies a
    supervisor_enabled toggle as a real change
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from systemu.core.models import Shadow, ShadowStatus


# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


class TestRoundTrip:
    def test_toggle_on_persists(self, vault):
        sh = Shadow(
            id="sh-1", name="X", description="t",
            identity_block="ID", status=ShadowStatus.AWAKENED,
        )
        assert sh.supervisor_enabled is False
        vault.save_shadow(sh)
        reloaded = vault.get_shadow("sh-1")
        assert reloaded.supervisor_enabled is False

        # Operator toggles ON via workshop
        reloaded.supervisor_enabled = True
        vault.save_shadow(reloaded)
        again = vault.get_shadow("sh-1")
        assert again.supervisor_enabled is True

    def test_toggle_off_persists(self, vault):
        sh = Shadow(
            id="sh-2", name="X", description="t",
            identity_block="ID", supervisor_enabled=True,
            status=ShadowStatus.AWAKENED,
        )
        vault.save_shadow(sh)
        assert vault.get_shadow("sh-2").supervisor_enabled is True

        sh.supervisor_enabled = False
        vault.save_shadow(sh)
        assert vault.get_shadow("sh-2").supervisor_enabled is False


class TestWorkshopChangeDetection:
    """Mirrors the change-detection block in workshop._open_shadow_edit._save.

    The save handler compares form values to current shadow state and
    only triggers a vault write when something actually changed.  The
    v0.4.2-b addition: supervisor_enabled toggles count as changes.
    """

    def _detect(self, *, shadow_supervisor: bool, form_supervisor: bool) -> Dict[str, Any]:
        """Replicate the change-detection from workshop.py _save."""
        prev_val = bool(shadow_supervisor)
        new_val = bool(form_supervisor)
        changed: Dict[str, Any] = {}
        if new_val != prev_val:
            changed["supervisor_enabled"] = new_val
        return changed

    def test_off_to_on_is_a_change(self):
        assert "supervisor_enabled" in self._detect(
            shadow_supervisor=False, form_supervisor=True,
        )

    def test_on_to_off_is_a_change(self):
        assert "supervisor_enabled" in self._detect(
            shadow_supervisor=True, form_supervisor=False,
        )

    def test_no_change_when_same(self):
        for v in (True, False):
            assert self._detect(
                shadow_supervisor=v, form_supervisor=v,
            ) == {}


class TestModelDefaultsForLegacyShadows:
    """An existing shadow (pre-v0.4.1) loaded from JSON without the field
    must default supervisor_enabled to False so it appears OFF in the UI."""

    def test_legacy_shadow_renders_as_disabled(self, vault):
        # Manually write a shadow JSON without the supervisor_enabled field
        import json
        from pathlib import Path as _P
        legacy = {
            "id": "sh-legacy",
            "name": "OldOne",
            "description": "legacy",
            "identity_block": "ID",
            "status": "awakened",
        }
        # Write directly so we bypass the model-side default
        shadow_dir = _P(vault.root) / "shadow_army"
        shadow_dir.mkdir(exist_ok=True)
        (shadow_dir / "shadow_sh-legacy.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        # Update the index
        idx_path = shadow_dir / "index.json"
        idx_path.write_text(json.dumps([{"id": "sh-legacy", "name": "OldOne",
                                          "status": "awakened"}]))

        sh = vault.get_shadow("sh-legacy")
        # The Pydantic default kicks in
        assert sh.supervisor_enabled is False
        # And it's the value the workshop dialog initialises its switch to:
        assert bool(getattr(sh, "supervisor_enabled", False)) is False
