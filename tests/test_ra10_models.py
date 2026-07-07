from systemu.core.models import Requirement, RequirementReport, Objective


def test_requirement_shape_and_defaults():
    r = Requirement(kind="decision", schema_path="config.repo", state="missing", source="schema")
    assert r.value_origin is None and r.confidence == 0.0 and r.bound_value_ref is None
    assert Requirement.model_validate(r.model_dump()) == r


def test_requirement_value_origin_canonical():
    r = Requirement(kind="input", schema_path="jobs[].src", state="have",
                    source="situation", value_origin="content_derived", bound_value_ref="root:/g/a.pdf", confidence=0.9)
    assert r.value_origin == "content_derived"
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Requirement(kind="bogus", schema_path="x", state="missing", source="schema")


def test_requirement_report_shape():
    rep = RequirementReport(per_objective={1: [Requirement(kind="credential", schema_path="auth", state="missing", source="runtime_error")]},
                            ask_bundle=[])
    assert 1 in rep.per_objective and rep.ask_bundle == []
    assert RequirementReport.model_validate(rep.model_dump()) == rep


def test_objective_requirements_backward_compatible():
    o = Objective.model_validate({"id": 1, "goal": "g", "success_criteria": "sc"})
    assert o.requirements == []
    o2 = Objective(id=2, goal="g", success_criteria="sc",
                   requirements=[Requirement(kind="decision", schema_path="repo", state="missing", source="schema")])
    assert len(o2.requirements) == 1 and Objective.model_validate(o2.model_dump()) == o2
