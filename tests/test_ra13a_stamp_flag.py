"""R-A13a Stage 1 — DEC-24 classifier + the 3-state S4_STAMP write-gate.

The classifier truth table is tested on _requires_external_verification directly
(value, flag-independent). The write-gate is tested through compute_requirements on a
REAL Tool, per S4_STAMP mode."""
from __future__ import annotations

import pytest

from systemu.core.models import Objective, Tool
from systemu.runtime.requirement_binder import (
    compute_requirements, _requires_external_verification,
)


class _Ctx:
    def __init__(self):
        self._situation_report = {"services": [], "capabilities": [], "roots": [],
                                  "credentials": [], "profile": {}, "declared_intents": []}
        self._granted_roots = None
        self.files_produced = []
        self.vault = None


def _cap(tags):
    return Tool(id="tool_c", name="c", description="d", tool_type="python_function",
                parameters_schema={"x": {"type": "string"}}, effect_tags=list(tags))


def _obj():
    return Objective(id=1, goal="do it", success_criteria="done")


# ── DEC-24 classifier truth table (channel-agnostic; value only) ──────────────
@pytest.mark.parametrize("tags,expected", [
    (["net_mutate"], True), (["money_move"], True), (["send_message"], True),
    (["oauth_call"], True), (["unknown"], True), ([], True),
    (["local_read"], False), (["local_write"], False), (["local_delete"], False),
    (["shell_exec"], False), (["net_read"], False),
    (["local_read", "net_mutate"], True),   # any stamp-effect ⇒ True
])
def test_dec24_classifier(tags, expected):
    assert _requires_external_verification(_cap(tags)) is expected


def test_none_capability_false():
    assert _requires_external_verification(None) is False


# ── the 3-state write-gate (through compute_requirements) ─────────────────────
def test_stamp_off_never_writes(monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "off")
    obj = _obj()
    compute_requirements(obj, _cap(["net_mutate"]), _Ctx()._situation_report, _Ctx())
    assert obj.requires_external_verification is False   # OFF ⇒ never written True


def test_stamp_off_is_the_default(monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)
    obj = _obj()
    compute_requirements(obj, _cap(["net_mutate"]), _Ctx()._situation_report, _Ctx())
    assert obj.requires_external_verification is False   # default OFF


def test_stamp_enforce_writes_the_dec24_value(monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    obj = _obj()
    compute_requirements(obj, _cap(["net_mutate"]), _Ctx()._situation_report, _Ctx())
    assert obj.requires_external_verification is True
    obj2 = _obj()
    compute_requirements(obj2, _cap(["local_read"]), _Ctx()._situation_report, _Ctx())
    assert obj2.requires_external_verification is False


def test_stamp_shadow_does_not_write_live_field(monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    obj = _obj()
    compute_requirements(obj, _cap(["net_mutate"]), _Ctx()._situation_report, _Ctx())
    assert obj.requires_external_verification is False   # SHADOW records, never writes live
