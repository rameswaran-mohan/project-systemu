"""R-A13a Stage 1 — source #0 path-descent (nested / array / oneOf over-ask fix).

All fixtures use a REAL Tool with a REAL nested parameters_schema and drive the
REAL binder (compute_requirements / build_requirement_report) — never a synthetic
Requirement dict or a schema-less tool (the blind spot that hid all 5 R-A12c defects)."""
from __future__ import annotations

from systemu.core.models import Objective, Tool
from systemu.runtime.requirement_binder import (
    build_requirement_report, compute_requirements,
)


class _Ctx:
    def __init__(self):
        self._situation_report = {"services": [], "capabilities": [], "roots": [],
                                  "credentials": [], "profile": {}, "declared_intents": []}
        self._granted_roots = None
        self.files_produced = []
        self.vault = None


def _tool(name, schema, *, effect_tags=None):
    return Tool(id="tool_" + name, name=name, description="t",
                tool_type="python_function", parameters_schema=schema,
                effect_tags=list(effect_tags or []))


def _obj():
    return Objective(id=1, goal="do it", success_criteria="done")


_NESTED = {
    "type": "object",
    "properties": {
        "recipient": {"type": "string"},
        "message": {
            "type": "object",
            "properties": {"body": {"type": "string"}, "subject": {"type": "string"}},
            "required": ["body", "subject"],
        },
    },
    "required": ["recipient", "message"],
}


def _bundle_paths(report):
    return sorted(r.schema_path for r in report.ask_bundle)


def test_nested_provided_leaf_binds_not_asked():
    """LLM provides {recipient, message:{body}} (omits message.subject). The card must
    show ONLY 'message/subject' — NO false 'message/body' gap (defect #2)."""
    ctx, cap = _Ctx(), _tool("send", _NESTED, effect_tags=["net_mutate"])
    provided = {"recipient": "a@b", "message": {"body": "hi"}}
    report = build_requirement_report([_obj()], cap, ctx._situation_report, ctx,
                                      provided_params=provided)
    assert _bundle_paths(report) == ["message/subject"]


def test_fully_provided_nested_no_gap():
    """AC6 shape at the binder level: a fully-provided nested call ⇒ empty ask_bundle."""
    ctx, cap = _Ctx(), _tool("send", _NESTED, effect_tags=["net_mutate"])
    provided = {"recipient": "a@b", "message": {"body": "hi", "subject": "yo"}}
    report = build_requirement_report([_obj()], cap, ctx._situation_report, ctx,
                                      provided_params=provided)
    assert report.ask_bundle == []


def test_array_of_objects_fully_provided_no_gap():
    """Array-of-object items fully provided ⇒ no false gap (defect #3)."""
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
        "required": ["items"]}
    ctx, cap = _Ctx(), _tool("bulk", schema, effect_tags=["net_mutate"])
    report = build_requirement_report([_obj()], cap, ctx._situation_report, ctx,
                                      provided_params={"items": [{"id": "x"}, {"id": "y"}]})
    assert report.ask_bundle == []


def test_array_of_objects_missing_leaf_still_gap():
    """An array element that OMITS a required sub-leaf is still a real gap."""
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
        "required": ["items"]}
    ctx, cap = _Ctx(), _tool("bulk", schema, effect_tags=["net_mutate"])
    report = build_requirement_report([_obj()], cap, ctx._situation_report, ctx,
                                      provided_params={"items": [{"id": "x"}, {}]})
    assert _bundle_paths(report) == ["items/[]/id"]


def test_oneof_branch_provided_no_gap():
    """A oneOf object branch fully provided binds (defect #3 — walk path is target/repo)."""
    schema = {"type": "object", "properties": {
        "target": {"oneOf": [{"type": "object", "properties": {"repo": {"type": "string"}},
                              "required": ["repo"]}]}}, "required": ["target"]}
    ctx, cap = _Ctx(), _tool("gh", schema, effect_tags=["net_mutate"])
    report = build_requirement_report([_obj()], cap, ctx._situation_report, ctx,
                                      provided_params={"target": {"repo": "octocat/hello"}})
    assert report.ask_bundle == []


def test_top_level_flat_still_binds():
    """Non-regression: a flat top-level provided param still binds (foundation Part-A)."""
    ctx, cap = _Ctx(), _tool("push", {"repo": {"type": "string"}}, effect_tags=["net_mutate"])
    reqs = compute_requirements(_obj(), cap, ctx._situation_report, ctx,
                                provided_params={"repo": "octocat/hello"})
    repo = [r for r in reqs if r.schema_path == "repo"]
    assert repo and repo[0].state == "have" and repo[0].source == "provided"
