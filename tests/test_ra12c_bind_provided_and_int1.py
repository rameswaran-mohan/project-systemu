"""R-A12c FOUNDATION — the two isolated requirement-binder additions (spec §5.3 / §5.8).

Part A — ``_bind_provided_params`` (THE over-ask fix).
    At the tool-call seam the LLM has ALREADY supplied ``decision.parameters``. Without
    a bind source that reads those provided params, every already-supplied REQUIRED leaf
    would generate a spurious ask and every tool call would suspend. This source binds a
    required leaf from the CURRENT call's provided params (FIRST in ``_SOURCES`` — a
    provided value wins over inventory / schema default).

    IMPL-5 taint (the judgment call): raw provided params carry NO taint signal (a plain
    ``dict[str, Any]``). A value the LLM emits from a plan is systemu-authored (systemu's
    own reasoning), NOT untrusted file content — so the DEFAULT origin is
    ``systemu_authored`` (non-content_derived ⇒ binds silently, state="have", never asked).
    This is NOT laundering: we never convert a value KNOWN to be content_derived — there
    is simply no content_derived signal on raw provided params to preserve.

Part B — INT-1 gate on ``_requires_external_verification`` (spec §5.8, INTERIM).
    S3 cannot yet produce ``confirmed`` evidence for real tools, so stamping
    ``requires_external_verification=True`` on every non-read effect would park every
    non-read objective UNVERIFIED_EXTERNAL. INT-1: stamp True ONLY where the capability
    declares an S3 evidence channel (``external_verification_channel``); else False. A
    temporary BLOCKER-3 deviation removed at R-A13 when S3 evidence-production goes live.
"""
from __future__ import annotations

import os

from systemu.core.models import Objective, Tool
from systemu.runtime.requirement_binder import (
    build_requirement_report,
    compute_requirements,
    _requires_external_verification,
)


# ── tiny fakes (kept local; mirror tests/test_ra10_binder.py) ─────────────────
class _FakeGrantedRoots:
    def __init__(self, roots):
        self._roots = [os.path.normcase(os.path.abspath(r)) for r in (roots or [])]

    def is_within_granted(self, candidate: str) -> bool:
        c = os.path.normcase(os.path.abspath(str(candidate or "")))
        return any(c == r or c.startswith(r + os.sep) for r in self._roots)


class _FakeCtx:
    def __init__(self, *, situation=None, granted_roots=None, files_produced=None):
        self._situation_report = situation
        self._granted_roots = granted_roots
        self.files_produced = list(files_produced or [])
        self.vault = None


def _tool(name, schema, *, effect_tags=None, channel=None):
    return Tool(
        id="tool_" + name,
        name=name,
        description="test tool",
        tool_type="python_function",
        parameters_schema=schema,
        effect_tags=list(effect_tags or []),
        external_verification_channel=channel,
    )


def _obj(oid=1, goal="do the thing"):
    return Objective(id=oid, goal=goal, success_criteria="it is done")


def _situation(**over):
    base = {
        "services": [], "capabilities": [], "roots": [],
        "credentials": [], "profile": {}, "declared_intents": [],
    }
    base.update(over)
    return base


# ══════════════════════════════════════════════════════════════════════════════
#  Part A — _bind_provided_params (the over-ask fix)
# ══════════════════════════════════════════════════════════════════════════════
def test_provided_param_binds_not_asked():
    """A required leaf whose key IS in the provided call params binds silently
    (state="have", systemu_authored) — NOT surfaced as an ask."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("push_repo", {"repo": {"type": "string", "description": "which repository"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx,
                                provided_params={"repo": "octocat/hello"})
    repo = [r for r in reqs if r.schema_path.endswith("repo")]
    assert repo, "the required leaf should still produce a Requirement"
    r = repo[0]
    assert r.state == "have"                      # already supplied ⇒ not a gap
    assert r.source == "provided"
    assert r.value_origin == "systemu_authored"   # the judgment-call default (non-content)
    assert r.bound_value_ref and r.bound_value_ref.startswith("provided:")

    report = build_requirement_report([_obj()], cap, situation, ctx,
                                      provided_params={"repo": "octocat/hello"})
    assert not any(a.schema_path.endswith("repo") for a in report.ask_bundle), \
        "an already-provided required param must NOT be asked (the over-ask fix)"


def test_missing_provided_param_still_asked():
    """A required leaf NOT in provided_params, unbindable elsewhere, is still a real
    requirement (state != 'have') — the fix does not suppress a genuine gap."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("push_repo", {
        "repo": {"type": "string", "description": "which repository"},
        "branch": {"type": "string", "description": "which branch"},
    })

    reqs = compute_requirements(_obj(), cap, situation, ctx,
                                provided_params={"repo": "octocat/hello"})
    repo = [r for r in reqs if r.schema_path.endswith("repo")]
    branch = [r for r in reqs if r.schema_path.endswith("branch")]
    assert repo and repo[0].state == "have"        # provided ⇒ bound
    assert branch, "the un-provided required leaf must still be reported"
    assert branch[0].state != "have"               # a real gap remains an ask
    assert branch[0].state == "missing"

    report = build_requirement_report([_obj()], cap, situation, ctx,
                                      provided_params={"repo": "octocat/hello"})
    assert any(a.schema_path.endswith("branch") for a in report.ask_bundle), \
        "the genuinely-missing param must still be surfaced"
    assert not any(a.schema_path.endswith("repo") for a in report.ask_bundle)


def test_provided_none_value_not_bound():
    """provided_params={"repo": None} — a None value is NOT a supplied value; the leaf
    must fall through to a real missing requirement, never bind on None."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("push_repo", {"repo": {"type": "string", "description": "which repository"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx,
                                provided_params={"repo": None})
    repo = [r for r in reqs if r.schema_path.endswith("repo")]
    assert repo, "the required leaf should still produce a Requirement"
    r = repo[0]
    assert r.state == "missing"                    # None is not a value ⇒ not bound
    assert r.source != "provided"
    assert not r.bound_value_ref


def test_provided_wins_over_default():
    """A provided value takes precedence over a schema default (which _walk otherwise
    routes straight through as a systemu source #5 bind)."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("fmt", {"format": {"type": "string", "default": "pdf"}})

    # sanity: with NO provided value, the schema default binds (source="schema").
    base = compute_requirements(_obj(), cap, situation, ctx)
    fmt0 = [r for r in base if r.schema_path.endswith("format")]
    assert fmt0 and fmt0[0].source == "schema"

    reqs = compute_requirements(_obj(), cap, situation, ctx,
                                provided_params={"format": "docx"})
    fmt = [r for r in reqs if r.schema_path.endswith("format")]
    assert fmt, "the leaf should produce a Requirement"
    r = fmt[0]
    assert r.state == "have"
    assert r.source == "provided", "the provided value must win over the schema default"
    assert r.bound_value_ref == "provided:format"
    assert not str(r.bound_value_ref).startswith("schema_")
    assert r.value_origin == "systemu_authored"


# ══════════════════════════════════════════════════════════════════════════════
#  Part B — DEC-24 classifier + S4_STAMP write-gate (supersedes INT-1)
# ══════════════════════════════════════════════════════════════════════════════
def _cap_tags(effect_tags):
    return Tool(id="tool_c", name="c", description="d", tool_type="python_function",
                effect_tags=list(effect_tags or []))


def test_dec24_net_mutate_true():
    assert _requires_external_verification(_cap_tags(["net_mutate"])) is True


def test_dec24_local_read_false():
    assert _requires_external_verification(_cap_tags(["local_read"])) is False


def test_dec24_empty_tags_true():
    assert _requires_external_verification(_cap_tags([])) is True   # UNKNOWN-until-classified


def test_dec24_none_capability_false():
    assert _requires_external_verification(None) is False


def test_dec24_local_write_false():
    assert _requires_external_verification(_cap_tags(["local_write"])) is False


def test_stamp_written_only_under_enforce(monkeypatch):
    """End-to-end: a net_mutate cap stamps True ONLY when S4_STAMP=enforce; OFF (default)
    never writes it."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap_nm = _tool("poster", {"x": {"type": "string"}}, effect_tags=["net_mutate"])

    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)   # default OFF
    obj_off = _obj()
    compute_requirements(obj_off, cap_nm, situation, ctx)
    assert obj_off.requires_external_verification is False

    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    obj_on = _obj()
    compute_requirements(obj_on, cap_nm, situation, ctx)
    assert obj_on.requires_external_verification is True
