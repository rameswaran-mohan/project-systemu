"""Wave 2.4 — piercing the safety floor must be visible.

The gate-mode dial deliberately lets a per-type override beat the floor
(operator escape hatch) and ``no_floor`` disable it entirely — but nothing
in the UI said so.  ``floor_pierces`` is the pure detector; the Inbox and
Settings pages render a persistent warn banner when it's non-empty.
"""
from systemu.interface.command.gate_mode import (
    GateMode,
    GateModePolicy,
    floor_pierces,
)


class TestFloorPierces:
    def test_default_policy_is_clean(self):
        assert floor_pierces(GateModePolicy()) == []

    def test_bypass_mode_alone_is_clean(self):
        # Bypass still respects the floor — that's the floor's whole job.
        assert floor_pierces(GateModePolicy(mode=GateMode.BYPASS)) == []

    def test_no_floor_is_flagged(self):
        out = floor_pierces(GateModePolicy(no_floor=True))
        assert len(out) == 1 and "no_floor" in out[0]

    def test_floor_type_override_allow_is_flagged(self):
        out = floor_pierces(GateModePolicy(overrides={"dep": "allow"}))
        assert len(out) == 1 and "dep" in out[0]

    def test_recovery_override_allow_is_flagged(self):
        out = floor_pierces(GateModePolicy(overrides={"recovery": "allow"}))
        assert len(out) == 1 and "recovery" in out[0]

    def test_floor_override_ask_is_clean(self):
        # "ask" matches what the floor would do anyway.
        assert floor_pierces(GateModePolicy(overrides={"dep": "ask"})) == []

    def test_non_floor_override_is_clean(self):
        assert floor_pierces(GateModePolicy(overrides={"scroll": "allow"})) == []

    def test_combined_pierces_all_reported(self):
        out = floor_pierces(GateModePolicy(
            no_floor=True, overrides={"dep": "allow", "recovery": "allow"},
        ))
        assert len(out) == 3


class TestBannersWired:
    def test_inbox_page_renders_banner(self):
        import inspect
        from systemu.interface.pages import inbox_page
        assert "floor_pierces" in inspect.getsource(inbox_page)

    def test_settings_page_renders_banner(self):
        import inspect
        from systemu.interface.pages import settings
        assert "floor_pierces" in inspect.getsource(settings)
