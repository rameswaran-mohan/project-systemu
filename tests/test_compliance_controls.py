"""DEC-23 seed S-2: the compliance control map is valid + honest (Phase C0)."""
from __future__ import annotations

from pathlib import Path

import yaml

_CONTROLS = Path(__file__).resolve().parent.parent / "compliance" / "controls.yaml"
_VALID_STATUS = {"enforced", "partial", "planned"}


def test_controls_yaml_parses_and_is_well_formed():
    data = yaml.safe_load(_CONTROLS.read_text(encoding="utf-8"))
    controls = data["controls"]
    assert len(controls) == 15, "the seed maps 15 control families"
    ids = set()
    for c in controls:
        assert {"id", "name", "intent", "mechanism", "status"} <= set(c), c
        assert c["status"] in _VALID_STATUS, f"{c['id']} has an out-of-vocab status {c['status']!r}"
        assert isinstance(c["mechanism"], str) and c["mechanism"].strip()
        ids.add(c["id"])
    assert len(ids) == 15, "control ids are unique"


def test_controls_yaml_is_honest_not_all_enforced():
    """Honesty guard: the seed must NOT claim every control is done — R-SEC1 auth
    and the tamper-evident ledger are planned, capture-consent is partial."""
    data = yaml.safe_load(_CONTROLS.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in data["controls"]}
    assert by_id["CTL-13"]["status"] == "planned"   # R-SEC1 dashboard auth
    assert by_id["CTL-14"]["status"] == "planned"   # tamper-evident ledger (no model yet)
    assert data.get("generated_as") == "seed"
