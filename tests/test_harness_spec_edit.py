import pytest
from systemu.runtime.harness_spec_edit import (
    band_rank, spec_edit_view, validate_amended_spec, evaluate_amendment,
)
from systemu.core.models import RiskBand


def test_band_rank_orders_low_medium_high():
    assert band_rank(RiskBand.LOW) == 0
    assert band_rank(RiskBand.MEDIUM) == 1
    assert band_rank(RiskBand.HIGH) == 2
    # tolerates the string form too
    assert band_rank("high") == 2
    assert band_rank(RiskBand.HIGH) > band_rank(RiskBand.MEDIUM)


def test_spec_edit_view_access_exposes_resource_keys():
    view = spec_edit_view("access", {"access_type": "read", "fs_read": "/tmp/x"})
    assert "access_type" in view["allowed_keys"]
    assert "fs_write" in view["allowed_keys"]
    assert view["editable"] == {"access_type": "read", "fs_read": "/tmp/x"}


def test_spec_edit_view_subagent_requires_task():
    view = spec_edit_view("subagent", {"task": "do x", "depth": 1})
    assert "task" in view["required_keys"]
    assert "budget_fraction" in view["allowed_keys"]


def test_validate_rejects_unknown_key():
    errs = validate_amended_spec("compute", {"budget_fraction": 0.5, "bogus": 1},
                                 original_spec={"budget_fraction": 0.2})
    assert any("bogus" in e for e in errs)


def test_validate_rejects_missing_required():
    errs = validate_amended_spec("subagent", {"depth": 2},  # no task
                                 original_spec={"task": "t"})
    assert any("task" in e for e in errs)


def test_validate_accepts_clean_edit():
    assert validate_amended_spec("compute", {"budget_fraction": 0.4},
                                 original_spec={"budget_fraction": 0.2}) == []


def test_evaluate_access_read_to_write_is_band_increase():
    # ACCESS write → HIGH; ACCESS read (non-whitelisted) → MEDIUM. Edit raises band.
    res = evaluate_amendment(
        kind="access",
        original_spec={"access_type": "read", "resource": "notes"},
        edited_spec={"access_type": "write", "resource": "notes"},
        arb_context={}, config=None,
    )
    assert res["blocked"] is False
    assert res["band_increase"] is True
    assert res["to_band"] == "high"


def test_evaluate_access_write_to_read_is_not_increase():
    res = evaluate_amendment(
        kind="access",
        original_spec={"access_type": "write", "resource": "notes"},
        edited_spec={"access_type": "read", "resource": "notes"},
        arb_context={}, config=None,
    )
    assert res["blocked"] is False
    assert res["band_increase"] is False
