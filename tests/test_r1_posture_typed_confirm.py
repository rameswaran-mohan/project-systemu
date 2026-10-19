"""R1 (spec section 10 / 5.10.b#5) -- loosening the approval posture in Settings is
a TYPED confirmation naming the new posture; tightening stays one click.

THE WITNESSED GAP: the Gate-Mode card saved "Bypass -- auto-grant everything
except the floor", "Disable the safety floor" and per-gate "Always allow
(auto-grant)" overrides on one click of "Save Gate Mode"; "Apply Preset" switched
on the harness auto-grant flags ("autonomous") on one click.

The check lives in pure, NiceGUI-free functions the click handlers call, so it is
server-side (the handler runs in the daemon) and testable here. Every save writes
the real ``.env`` -- redirected into tmp -- so these pins read the disk bytes the
next boot would read, not a mock's memory.
"""
from __future__ import annotations

import pytest

GATE_KEYS = ("SYSTEMU_GATE_MODE", "SYSTEMU_GATE_OVERRIDES", "SYSTEMU_GATE_NO_FLOOR")
FLAG_KEYS = ("SYSTEMU_HARNESS_AUTO_GRANT_TOOL", "SYSTEMU_HARNESS_AUTO_GRANT_SKILL",
             "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS", "SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE",
             "SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT", "SYSTEMU_AUTO_FORGE_TOOLS",
             "SYSTEMU_EXECUTION_ADHERENCE")


@pytest.fixture
def posture(monkeypatch, tmp_path):
    """A posture of risk_tiered / no overrides / floor ON, with every write
    redirected into tmp and every env key restored afterwards."""
    monkeypatch.chdir(tmp_path)                       # gate_mode_settings writes ./.env
    import systemu.interface.pages.settings as settings
    monkeypatch.setattr(settings, "_env_file_path", lambda: tmp_path / ".env")
    monkeypatch.setenv("SYSTEMU_GATE_MODE", "risk_tiered")
    monkeypatch.setenv("SYSTEMU_GATE_OVERRIDES", "{}")
    monkeypatch.setenv("SYSTEMU_GATE_NO_FLOOR", "0")
    for key in FLAG_KEYS:
        monkeypatch.setenv(key, "")                   # registers restore; "" = default
    return tmp_path / ".env"


def _env_text(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _save(**kw):
    from systemu.interface.pages.settings import save_gate_mode_confirmed
    kw.setdefault("overrides", {})
    kw.setdefault("no_floor", False)
    return save_gate_mode_confirmed(**kw)


# -- the gate-mode dial --------------------------------------------------------

def test_switching_to_bypass_needs_the_typed_phrase(posture):
    import os
    ok, msg = _save(mode="bypass", typed="")
    assert ok is False
    assert "Type bypass exactly" in msg and "Nothing was saved" in msg
    msg.encode("ascii")
    assert os.environ["SYSTEMU_GATE_MODE"] == "risk_tiered"
    assert "bypass" not in _env_text(posture)

    ok, msg = _save(mode="bypass", typed="bypass")
    assert ok is True, msg
    assert os.environ["SYSTEMU_GATE_MODE"] == "bypass"
    assert "SYSTEMU_GATE_MODE=bypass" in _env_text(posture)


@pytest.mark.parametrize("typed", ["", "Bypass", "bypas", "risk_tiered", "yes"])
def test_a_wrong_phrase_saves_nothing(posture, typed):
    import os
    ok, _ = _save(mode="bypass", typed=typed)
    assert ok is False
    assert os.environ["SYSTEMU_GATE_MODE"] == "risk_tiered"
    assert _env_text(posture) == ""


def test_a_typed_value_that_is_not_a_plain_str_is_refused(posture):
    class _Agreeable(str):
        def __eq__(self, other):
            return True

        __hash__ = str.__hash__

    ok, _ = _save(mode="bypass", typed=_Agreeable("x"))
    assert ok is False
    assert _env_text(posture) == ""


def test_disabling_the_floor_needs_the_phrase(posture):
    ok, msg = _save(mode="risk_tiered", no_floor=True, typed="")
    assert ok is False and "Type no_floor exactly" in msg
    ok, msg = _save(mode="risk_tiered", no_floor=True, typed="no_floor")
    assert ok is True, msg
    assert "SYSTEMU_GATE_NO_FLOOR=1" in _env_text(posture)


def test_an_auto_grant_override_needs_the_phrase(posture):
    ok, msg = _save(mode="risk_tiered", overrides={"dep": "allow"}, typed="")
    assert ok is False and "Type dep=allow exactly" in msg
    ok, msg = _save(mode="risk_tiered", overrides={"dep": "allow"}, typed="dep=allow")
    assert ok is True, msg


def test_several_loosenings_name_every_one_in_the_phrase(posture):
    ok, msg = _save(mode="bypass", no_floor=True, typed="bypass")
    assert ok is False and "Type bypass no_floor exactly" in msg
    ok, _ = _save(mode="bypass", no_floor=True, typed="bypass no_floor")
    assert ok is True


def test_tightening_stays_one_click(posture, monkeypatch):
    import os
    monkeypatch.setenv("SYSTEMU_GATE_MODE", "bypass")
    monkeypatch.setenv("SYSTEMU_GATE_NO_FLOOR", "1")
    monkeypatch.setenv("SYSTEMU_GATE_OVERRIDES", '{"dep": "allow"}')
    ok, msg = _save(mode="approve_only", overrides={"dep": "deny"}, no_floor=False,
                    typed="")
    assert ok is True, msg
    assert os.environ["SYSTEMU_GATE_MODE"] == "approve_only"
    assert os.environ["SYSTEMU_GATE_NO_FLOOR"] == "0"


def test_an_unchanged_posture_is_one_click(posture):
    ok, msg = _save(mode="risk_tiered", typed="")
    assert ok is True, msg


def test_the_loosening_tokens_are_derived_not_listed():
    from systemu.interface.pages.settings import posture_loosening
    cur = {"mode": "approve_only", "overrides": {"scroll": "deny"}, "no_floor": False}
    assert posture_loosening(cur, {"mode": "risk_tiered", "overrides": {"scroll": "deny"},
                                   "no_floor": False}) == ["risk_tiered"]
    # dropping a "deny" override hands that gate type to the mode default, which
    # asks (approve_only) -- an action that never ran can now run: a loosening
    assert posture_loosening(cur, {"mode": "approve_only", "overrides": {},
                                   "no_floor": False}) == ["scroll=default"]
    # pinning "deny" where the mode would have asked is a tightening
    assert posture_loosening(
        {"mode": "approve_only", "overrides": {}, "no_floor": False},
        {"mode": "approve_only", "overrides": {"scroll": "deny"}, "no_floor": False}
    ) == []
    assert posture_loosening(
        {"mode": "risk_tiered", "overrides": {"scroll": "deny"}, "no_floor": False},
        {"mode": "risk_tiered", "overrides": {}, "no_floor": False}) == ["scroll=default"]
    # an unknown override value counts as the loosest (fail-closed)
    assert posture_loosening(
        {"mode": "risk_tiered", "overrides": {}, "no_floor": False},
        {"mode": "risk_tiered", "overrides": {"dep": "maybe"}, "no_floor": False}
    ) == ["dep=maybe"]


# -- the harness auto-grant presets --------------------------------------------

def test_applying_a_loosening_preset_needs_its_name(posture):
    import os

    from systemu.interface.pages.settings import save_adherence_preset_confirmed
    ok, msg = save_adherence_preset_confirmed("autonomous", typed="")
    assert ok is False and "Type autonomous exactly" in msg
    assert os.environ["SYSTEMU_HARNESS_AUTO_GRANT_ACCESS"] == ""
    assert _env_text(posture) == ""

    ok, msg = save_adherence_preset_confirmed("autonomous", typed="autonomous")
    assert ok is True, msg
    assert os.environ["SYSTEMU_HARNESS_AUTO_GRANT_ACCESS"] == "true"
    assert "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS=true" in _env_text(posture)


def test_applying_a_tightening_preset_is_one_click(posture, monkeypatch):
    import os

    from systemu.interface.pages.settings import save_adherence_preset_confirmed
    for key in ("SYSTEMU_HARNESS_AUTO_GRANT_SKILL", "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS",
                "SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE", "SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT"):
        monkeypatch.setenv(key, "true")
    ok, msg = save_adherence_preset_confirmed("locked_sop", typed="")
    assert ok is True, msg
    assert os.environ["SYSTEMU_HARNESS_AUTO_GRANT_ACCESS"] == "false"


def test_the_assisted_preset_from_the_defaults_loosens_nothing(posture):
    """From an unset env the effective flags are the HarnessPolicy defaults
    (skill + compute on), which is exactly what "assisted" sets."""
    from systemu.interface.pages.settings import save_adherence_preset_confirmed
    ok, msg = save_adherence_preset_confirmed("assisted", typed="")
    assert ok is True, msg
