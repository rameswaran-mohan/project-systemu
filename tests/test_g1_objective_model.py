"""G1 (R-A2): Objective gains origin + requires_external_verification (SPEC §5.2)."""
from systemu.core.models import Objective


def test_new_fields_default_backward_compatible():
    obj = Objective.model_validate({"id": 1, "goal": "g", "success_criteria": "sc"})
    assert obj.origin == "planner"
    assert obj.requires_external_verification is False


def test_new_fields_round_trip():
    obj = Objective(
        id=2, goal="g", success_criteria="sc",
        origin="backchain", requires_external_verification=True,
        depends_on=[1],
    )
    dumped = obj.model_dump(mode="json")
    assert dumped["origin"] == "backchain"
    assert dumped["requires_external_verification"] is True
    assert dumped["depends_on"] == [1]
    restored = Objective.model_validate(dumped)
    assert restored == obj


def test_origin_rejects_unknown_value():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Objective(id=3, goal="g", success_criteria="sc", origin="bogus")
