# tests/test_stuck_force_finalize.py
from systemu.runtime.shadow_runtime import _should_force_finalize_stuck


def test_force_finalize_after_coach_budget_and_rounds():
    # coach budget spent (2/2) AND stuck round >= ceiling → force-finalize
    assert _should_force_finalize_stuck(coach_steers_used=2, max_steers=2,
                                        stuck_round=2, finalize_after_rounds=2) is True


def test_no_finalize_while_coach_budget_remains():
    assert _should_force_finalize_stuck(coach_steers_used=0, max_steers=2,
                                        stuck_round=5, finalize_after_rounds=2) is False
    assert _should_force_finalize_stuck(coach_steers_used=1, max_steers=2,
                                        stuck_round=5, finalize_after_rounds=2) is False


def test_no_finalize_before_round_ceiling():
    assert _should_force_finalize_stuck(coach_steers_used=2, max_steers=2,
                                        stuck_round=1, finalize_after_rounds=2) is False


def test_disabled_when_ceiling_zero():
    # finalize_after_rounds=0 disables the force-finalize (back-compat escape hatch)
    assert _should_force_finalize_stuck(coach_steers_used=2, max_steers=2,
                                        stuck_round=9, finalize_after_rounds=0) is False
