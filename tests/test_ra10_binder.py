"""R-A10 step B3 — the requirement binder core (§5.3, BIND-mode).

``requirement_binder.compute_requirements(objective, capability, situation, ctx)``
computes, per objective, a ``list[Requirement]``: it reads the chosen capability's
schema and, for each REQUIRED leaf, attempts to BIND it from the 5 spec-ordered
sources — so "what's missing" is a schema-DIFF, never a guess.

The ACs below pin each behavior the binder must satisfy:
  * AC2  — a granted-root salient (content_derived) path leaf binds but is NEVER
           silent (forced into ask_bundle, one-click confirm).
  * AC1  — a required leaf with no binding anywhere → decision/missing, no invented value.
  * AC5  — a required leaf matched from a user_facts PROFILE entry binds silently
           (operator origin, above T_high) — inventory closes the gap.
  * AC6  — the ONLY candidate being a content_derived inventory value ⇒ never silent.
  * AC3  — a nested/array required leaf is diffed at depth with the right schema_path.
  * AC4  — an unknown/net EffectTag capability ⇒ requires_external_verification=True.
  * AC7  — two accounts for a service ⇒ a decision requirement (IMPL-8) — or the
           v1 multi-service→decision path.
  * Defensive — a broken schema / missing situation returns [] (or best-effort), never raises.
"""
from __future__ import annotations

import os

import pytest

from systemu.core.models import Objective, Requirement, Tool
from systemu.runtime.requirement_binder import (
    T_HIGH,
    compute_requirements,
    build_requirement_report,
)


# ── tiny fakes ──────────────────────────────────────────────────────────────
class _FakeGrantedRoots:
    """Stand-in GrantedRootsStore: a path is within-granted iff it is under one
    of the given roots (case-insensitive prefix, component-boundary safe enough
    for the test)."""

    def __init__(self, roots):
        self._roots = [os.path.normcase(os.path.abspath(r)) for r in roots]

    def is_within_granted(self, candidate: str) -> bool:
        c = os.path.normcase(os.path.abspath(str(candidate or "")))
        return any(c == r or c.startswith(r + os.sep) for r in self._roots)


class _FakeCtx:
    """A minimal run-context: carries an optional situation report, a granted-roots
    store, files_produced, and a vault (unused here)."""

    def __init__(self, *, situation=None, granted_roots=None, files_produced=None):
        self._situation_report = situation
        self._granted_roots = granted_roots
        self.files_produced = list(files_produced or [])
        self.vault = None


def _tool(name, schema, *, effect_tags=None):
    return Tool(
        id="tool_" + name,
        name=name,
        description="test tool",
        tool_type="python_function",
        parameters_schema=schema,
        effect_tags=list(effect_tags or []),
    )


def _obj(oid=1, goal="do the thing"):
    return Objective(id=oid, goal=goal, success_criteria="it is done")


def _situation(**over):
    base = {
        "services": [],
        "capabilities": [],
        "roots": [],
        "credentials": [],
        "profile": {},
        "declared_intents": [],
    }
    base.update(over)
    return base


# ── AC2: granted-root salient path leaf → content_derived, in ask_bundle ─────
def test_ac2_granted_root_path_leaf_binds_but_content_derived_not_silent(tmp_path):
    """A required ``files`` (path) leaf resolving to a granted-root salient handle
    binds (state="have") with source="situation" and value_origin="content_derived"
    — but because it is content_derived it is FORCED into the ask_bundle (one-click
    confirm), never silent-bound."""
    root = tmp_path / "granted"
    root.mkdir()
    salient_file = root / "report.docx"
    salient_file.write_bytes(b"x")

    situation = _situation(
        roots=[{
            "path": str(root),
            "origin_class": "operator",
            "curated": False,
            "salient": [{
                "path": str(salient_file),
                "name": "report.docx",
                "ext": ".docx",
                "size": 1,
                "mtime": 0.0,
                "origin_class": "content_derived",
                "source_kind": "file",
            }],
        }],
    )
    ctx = _FakeCtx(situation=situation,
                   granted_roots=_FakeGrantedRoots([str(root)]))
    cap = _tool("open_doc", {"files": {"type": "string", "description": "path to input file"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    files = [r for r in reqs if r.schema_path.endswith("files")]
    assert files, "the required path leaf should produce a Requirement"
    r = files[0]
    assert r.kind == "input"
    assert r.state == "have"                        # a real granted-root file was bound
    assert r.source == "situation"
    assert r.value_origin == "content_derived"      # IMPL-5: copied from the FileHandle
    assert r.bound_value_ref                          # a reference to the bound path

    report = build_requirement_report([_obj()], cap, situation, ctx)
    assert any(a.schema_path.endswith("files") for a in report.ask_bundle), \
        "content_derived bind must be forced into the ask_bundle (never silent)"


# ── AC1: no binding anywhere → decision/missing, no invented value ───────────
def test_ac1_unbindable_leaf_is_decision_missing_no_value():
    """A required ``repo`` leaf with no binding in any source → a decision
    requirement, state="missing", with NO invented bound value."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("push_repo", {"repo": {"type": "string", "description": "which repository"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    repo = [r for r in reqs if r.schema_path.endswith("repo")]
    assert repo, "the required leaf should still be reported (schema-diff)"
    r = repo[0]
    assert r.state == "missing"
    assert r.kind == "decision"
    assert not r.bound_value_ref          # nothing invented
    assert r.value_origin is None


# ── AC5: user_facts PROFILE entry binds silently (operator, above T_high) ────
def test_ac5_inventory_bind_from_user_facts_closes_gap_silently():
    """A required ``account_id`` leaf that matches a ``user_facts`` profile entry
    binds: state="have"/"resolvable", source="operator_profile",
    value_origin="operator", above T_high ⇒ NOT in the ask_bundle (silent)."""
    situation = _situation(profile={
        "name": "Op", "location_text": "NYC", "timezone": "UTC",
        "default_output_dir": "/out",
        "user_facts": [{
            "id": "fact_1", "ts": "2020", "fact": "account_id is acct-42",
            "tags": ["account_id"], "source": "operator", "confidence": 1.0,
        }],
    })
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("api_call", {"account_id": {"type": "string", "description": "the account id"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    acct = [r for r in reqs if r.schema_path.endswith("account_id")]
    assert acct, "the required leaf should produce a Requirement"
    r = acct[0]
    assert r.state in ("have", "resolvable")
    assert r.state == "have"                          # operator-origin, confidence 1.0 ≥ T_high
    assert r.source == "operator_profile"
    assert r.value_origin == "operator"               # IMPL-5: copied from the profile fact
    assert r.bound_value_ref

    report = build_requirement_report([_obj()], cap, situation, ctx)
    assert not any(a.schema_path.endswith("account_id") for a in report.ask_bundle), \
        "an operator-origin bind above T_high is silent (no ask)"


# ── AC6: content_derived-only candidate is NEVER silent-bound ────────────────
def test_ac6_content_derived_only_candidate_never_silent(tmp_path):
    """When the ONLY candidate for a leaf is a content_derived inventory value, the
    taint travels (IMPL-5) and the bind is forced into the ask_bundle even at high
    confidence."""
    root = tmp_path / "g"
    root.mkdir()
    f = root / "data.csv"
    f.write_bytes(b"a,b\n")
    situation = _situation(roots=[{
        "path": str(root), "origin_class": "operator", "salient": [{
            "path": str(f), "name": "data.csv", "ext": ".csv", "size": 4, "mtime": 0.0,
            "origin_class": "content_derived", "source_kind": "file",
        }],
    }])
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([str(root)]))
    cap = _tool("ingest", {"source_path": {"type": "string", "description": "input csv path"}})

    report = build_requirement_report([_obj()], cap, situation, ctx)
    src = [r for r in report.per_objective[1] if r.schema_path.endswith("source_path")]
    assert src and src[0].value_origin == "content_derived"
    assert any(a.schema_path.endswith("source_path") for a in report.ask_bundle), \
        "content_derived taint ⇒ never silent, regardless of confidence"


# ── AC3: nested / array leaf diffed at depth with the right schema_path ───────
def test_ac3_nested_array_leaf_diffed_at_depth(tmp_path):
    """A nested required leaf (``jobs[].src_path``) is diffed at depth (drives the
    B2 leaf_fn) and yields a Requirement whose schema_path reflects the nesting."""
    schema = {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"src_path": {"type": "string",
                                                "description": "path to a source file"}},
                    "required": ["src_path"],
                },
            },
        },
        "required": ["jobs"],
    }
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("batch_job", schema)

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    nested = [r for r in reqs if "src_path" in r.schema_path]
    assert nested, "a nested/array leaf must be diffed at depth"
    r = nested[0]
    assert "jobs" in r.schema_path and "src_path" in r.schema_path
    assert "[]" in r.schema_path                       # array item marker in the pointer
    assert r.kind == "input"                            # oracle classified it a path leaf


# ── AC4: unknown/net EffectTag ⇒ requires_external_verification=True ──────────
def test_ac4_unknown_effect_tag_stamps_external_verification():
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("mystery", {"x": {"type": "string"}}, effect_tags=["unknown"])

    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is True


def test_ac4_net_mutate_effect_tag_stamps_external_verification():
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("poster", {"x": {"type": "string"}}, effect_tags=["net_mutate"])

    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is True


def test_ac4_local_read_only_does_not_stamp():
    """A purely local-read capability is NOT dangerous-until-proven."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("reader", {"x": {"type": "string"}}, effect_tags=["local_read"])

    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is False


def test_ac4_empty_effect_tags_is_unknown_stamps():
    """An EMPTY effect_tags list is UNKNOWN-until-classified, not 'no effect' ⇒
    dangerous-until-proven."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("blank", {"x": {"type": "string"}}, effect_tags=[])

    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is True


# ── AC7: two accounts for a service ⇒ a decision requirement (IMPL-8) ─────────
def test_ac7_multiple_accounts_for_service_force_a_decision():
    """Two ConnectedService entries for the same service (two acting identities) for
    a leaf that needs an account ⇒ a decision requirement (WHICH identity), never a
    silent pick of one. (v1 R-A9 sets account=None; we assert the multi-candidate →
    decision path by giving the situation two credential-bearing services.)"""
    situation = _situation(
        services=[
            {"name": "github", "auth_kind": "oauth", "has_live_token": True,
             "account": "alice", "origin_class": "operator", "source_kind": "connected_service"},
            {"name": "github", "auth_kind": "oauth", "has_live_token": True,
             "account": "bob", "origin_class": "operator", "source_kind": "connected_service"},
        ],
    )
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("gh_act", {"account": {"type": "string", "description": "github account to act as"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    acct = [r for r in reqs if r.schema_path.endswith("account")]
    assert acct, "the account leaf should produce a Requirement"
    r = acct[0]
    assert r.kind == "decision"
    assert r.state in ("resolvable", "missing")       # not silently bound to one identity


# ── AC: schema default / const / enum[0] → systemu_authored bind ─────────────
def test_schema_default_binds_systemu_authored():
    """A leaf carrying a schema default binds from source #5 with
    value_origin="systemu_authored" (systemu's own catalog)."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("fmt", {"format": {"type": "string", "default": "pdf"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    fmt = [r for r in reqs if r.schema_path.endswith("format")]
    assert fmt
    r = fmt[0]
    assert r.state == "have"
    assert r.source == "schema"
    assert r.value_origin == "systemu_authored"


# ── required-ness derivation for a FLAT Tool.parameters_schema ───────────────
def test_flat_schema_leaf_without_default_is_required():
    """DRIFT: Tool.parameters_schema is a FLAT {param:{...}} map with no required[].
    Required-ness = 'a leaf with no default'. A leaf WITH a default is bindable (not
    a gap); a leaf WITHOUT one and unbindable is a missing requirement."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("mix", {
        "needed": {"type": "string", "description": "no default → required"},
        "optional": {"type": "string", "default": "x"},
    })

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    needed = [r for r in reqs if r.schema_path.endswith("needed")]
    optional = [r for r in reqs if r.schema_path.endswith("optional")]
    assert needed and needed[0].state == "missing"
    # the defaulted one binds (systemu_authored) rather than being a gap
    assert optional and optional[0].state == "have"


# ── defensive: broken schema / missing situation → [], never raises ──────────
def test_defensive_broken_schema_returns_list_no_raise():
    ctx = _FakeCtx(situation=None, granted_roots=None)
    cap = _tool("t", {})                               # empty schema
    assert compute_requirements(_obj(), cap, None, ctx) == []


def test_defensive_none_capability_returns_empty():
    ctx = _FakeCtx(situation=_situation(), granted_roots=_FakeGrantedRoots([]))
    assert compute_requirements(_obj(), None, _situation(), ctx) == []


def test_defensive_malformed_situation_does_not_raise():
    ctx = _FakeCtx(situation="not-a-dict", granted_roots=_FakeGrantedRoots([]))
    cap = _tool("t", {"x": {"type": "string"}})
    # must not raise; returns a best-effort list (the leaf is unbindable → missing)
    reqs = compute_requirements(_obj(), cap, "not-a-dict", ctx)
    assert isinstance(reqs, list)


# ── Finding 1: prefixItems (2020-12 tuple array) diffs, not [] ───────────────
def test_prefixitems_tuple_array_emits_requirements():
    """A required ``prefixItems`` (tuple) array — a real MCP schema shape — must be
    diffed by the binder. Before the rework the mirror ``_walk_bind`` handled neither
    prefixItems nor additionalProperties, so a required prefixItems leaf emitted ZERO
    Requirements (the binder reported 'nothing missing' for a required param). Driving
    the real ``_walk`` (which handles prefixItems) fixes it by construction."""
    schema = {
        "type": "object",
        "properties": {
            "coord": {
                "type": "array",
                "prefixItems": [
                    {"type": "string", "description": "path to a file"},
                    {"type": "number"},
                ],
            },
        },
        "required": ["coord"],
    }
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("tuple_tool", schema)

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    coord = [r for r in reqs if "coord" in r.schema_path]
    assert coord, "a required prefixItems tuple array must emit Requirement(s), not []"
    # the tuple leaves are diffed at depth with an array-item marker
    assert any("[]" in r.schema_path for r in coord)


# ── Finding 2 / #2: whole-objective swallow — a bogus origin never empties the diff ─
def test_whole_objective_swallow_bogus_origin_class_clamped(tmp_path):
    """A situation entry with a NON-canonical ``origin_class`` that matches a leaf must
    NOT raise a Requirement ValidationError that propagates to the outer except and
    returns [] for the ENTIRE objective. The bad value_origin is CLAMPED to
    content_derived (fail-untrusted) and the diff is non-empty & valid."""
    root = tmp_path / "g"
    root.mkdir()
    f = root / "data.csv"
    f.write_bytes(b"x")
    situation = _situation(roots=[{
        "path": str(root), "origin_class": "operator", "salient": [{
            "path": str(f), "name": "data.csv", "ext": ".csv", "size": 1, "mtime": 0.0,
            "origin_class": "weird_bogus",              # NON-canonical taint
            "source_kind": "file",
        }],
    }])
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([str(root)]))
    cap = _tool("ingest", {"source_path": {"type": "string", "description": "input csv path"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    assert isinstance(reqs, list) and reqs, "a bogus origin must not empty the whole objective"
    src = [r for r in reqs if r.schema_path.endswith("source_path")]
    assert src, "the matched leaf must still produce a Requirement"
    r = src[0]
    assert r.value_origin == "content_derived"          # clamped (fail-untrusted)

    report = build_requirement_report([_obj()], cap, situation, ctx)
    assert any(a.schema_path.endswith("source_path") for a in report.ask_bundle), \
        "the clamped content_derived bind must route to the ask_bundle (never silent)"


# ── Finding 2: declared_intents taint travels; a content_derived intent → ask ──
def test_declared_intent_content_derived_binds_into_ask(tmp_path):
    """A ``declared_intents`` entry carrying ``origin_class='content_derived'`` that
    binds a leaf must be forced into the ask_bundle (never silent). IMPL-5: the entry's
    origin_class is copied verbatim onto the Requirement's value_origin."""
    situation = _situation(declared_intents=[{
        "id": "i-1", "kind": "tool", "name": "special_account", "detail": "",
        "status": "declared", "origin_class": "content_derived",
    }])
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("api", {"special_account": {"type": "string", "description": "an account"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    acct = [r for r in reqs if r.schema_path.endswith("special_account")]
    assert acct, "the declared-intent-matched leaf should produce a Requirement"
    r = acct[0]
    assert r.value_origin == "content_derived"          # copied verbatim from the intent
    report = build_requirement_report([_obj()], cap, situation, ctx)
    assert any(a.schema_path.endswith("special_account") for a in report.ask_bundle), \
        "a content_derived declared-intent bind is never silent"


# ── additionalProperties + $ref-cycle parity (inherited from _walk) ──────────
def test_additional_properties_and_ref_cycle_diff_without_crash():
    """A schema using ``additionalProperties`` and a ``$ref`` cycle must diff without
    crashing — the binder now inherits ``_walk``'s handling of both (before the rework
    ``_walk_bind`` mirrored neither additionalProperties)."""
    schema = {
        "type": "object",
        "$defs": {"node": {"$ref": "#/$defs/node"}},
        "properties": {
            "n": {"$ref": "#/$defs/node"},                 # $ref cycle → bottoms out
            # additionalProperties whose value carries a schema default → the synthetic
            # sample_key leaf BINDS (source #5), so it is diffed & reported (proves the
            # binder inherited _walk's additionalProperties handling).
            "bag": {"type": "object",
                    "additionalProperties": {"type": "string", "default": "d"}},
        },
        "required": ["bag"],
    }
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("mapper", schema)

    reqs = compute_requirements(_obj(), cap, situation, ctx)   # must not raise (cycle-safe)
    assert isinstance(reqs, list)
    # additionalProperties' synthetic sample_key leaf is diffed at depth under `bag`
    # (inherited from _walk); the $ref cycle terminated without hanging or crashing.
    bag_reqs = [r for r in reqs if "bag" in r.schema_path]
    assert bag_reqs, "the additionalProperties leaf under `bag` must be diffed"
    assert bag_reqs[0].value_origin == "systemu_authored"    # bound from the schema default


# ── Finding (MEDIUM): a forged file-handle origin_class is CLAMPED (no laundering) ─
def test_forged_filehandle_operator_origin_is_clamped_to_content_derived(tmp_path):
    """A FileHandleLite claiming ``origin_class='operator'`` (a poisoned / rehydrated
    survey handle) must NOT launder an untrusted file value into the trusted axis. A
    file's content is inherently untrusted → the binder CLAMPS the origin to
    content_derived REGARDLESS of the object's claimed origin_class (IMPL-5). Result:
    value_origin='content_derived' ⇒ _needs_ask True ⇒ NOT silent-bound. Before the
    fix the forged 'operator' was copied verbatim → a silent bind of a file value."""
    root = tmp_path / "granted"
    root.mkdir()
    salient_file = root / "report.docx"
    salient_file.write_bytes(b"x")

    situation = _situation(
        roots=[{
            "path": str(root),
            "origin_class": "operator",
            "curated": False,
            "salient": [{
                "path": str(salient_file),
                "name": "report.docx",
                "ext": ".docx",
                "size": 1,
                "mtime": 0.0,
                "origin_class": "operator",     # FORGED — a file can't be operator-trusted
                "source_kind": "file",
            }],
        }],
    )
    ctx = _FakeCtx(situation=situation,
                   granted_roots=_FakeGrantedRoots([str(root)]))
    cap = _tool("open_doc", {"files": {"type": "string", "description": "path to input file"}})

    reqs = compute_requirements(_obj(), cap, situation, ctx)
    files = [r for r in reqs if r.schema_path.endswith("files")]
    assert files, "the required path leaf should produce a Requirement"
    r = files[0]
    assert r.value_origin == "content_derived", \
        "a file handle's forged operator origin must be clamped to content_derived"

    report = build_requirement_report([_obj()], cap, situation, ctx)
    assert any(a.schema_path.endswith("files") for a in report.ask_bundle), \
        "the clamped (content_derived) file bind must route to the ask_bundle (never silent)"


# ── the AC5 operator-profile path is UNTOUCHED by the clamp (still silent) ────
def test_forged_service_origin_class_is_clamped_but_profile_fact_preserved(tmp_path):
    """The clamp applies to a scanned/untrusted inventory SOURCE (a service entry's
    copied origin_class), NOT to a genuinely operator-authored profile fact. A forged
    ConnectedService claiming origin_class='operator' that binds an account leaf must
    NOT bind silently as operator; but the AC5 operator-PROFILE fact still does."""
    # (a) a forged service entry → its origin is clamped (not silent-operator).
    situation = _situation(services=[{
        "name": "acct-svc", "auth_kind": "oauth", "has_live_token": True,
        "account": "forged-identity", "origin_class": "operator",
        "source_kind": "connected_service",
    }])
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("act", {"account": {"type": "string", "description": "acct-svc account to act as"}})
    reqs = compute_requirements(_obj(), cap, situation, ctx)
    acct = [r for r in reqs if r.schema_path.endswith("account")]
    assert acct, "the account leaf should produce a Requirement"
    r = acct[0]
    assert r.value_origin == "content_derived", \
        "a service entry's copied origin_class must clamp to content_derived (no laundering)"
    report = build_requirement_report([_obj()], cap, situation, ctx)
    assert any(a.schema_path.endswith("account") for a in report.ask_bundle), \
        "the clamped service bind must route to the ask_bundle (never silent)"

    # (b) the genuine operator-PROFILE fact (AC5) is UNTOUCHED — still binds silently.
    situation2 = _situation(profile={
        "name": "Op", "location_text": "NYC", "timezone": "UTC",
        "default_output_dir": "/out",
        "user_facts": [{
            "id": "fact_1", "ts": "2020", "fact": "account_id is acct-42",
            "tags": ["account_id"], "source": "operator", "confidence": 1.0,
        }],
    })
    ctx2 = _FakeCtx(situation=situation2, granted_roots=_FakeGrantedRoots([]))
    cap2 = _tool("api_call", {"account_id": {"type": "string", "description": "the account id"}})
    reqs2 = compute_requirements(_obj(), cap2, situation2, ctx2)
    acct2 = [r for r in reqs2 if r.schema_path.endswith("account_id")]
    assert acct2 and acct2[0].value_origin == "operator", \
        "an operator-profile fact must still carry operator (AC5 preserved)"
    report2 = build_requirement_report([_obj()], cap2, situation2, ctx2)
    assert not any(a.schema_path.endswith("account_id") for a in report2.ask_bundle), \
        "the operator-profile bind stays silent (AC5)"


# ── build_requirement_report aggregation + ask_bundle dedupe ─────────────────
def test_build_report_aggregates_per_objective_and_dedupes_ask_bundle():
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("push_repo", {"repo": {"type": "string"}})
    o1, o2 = _obj(1), _obj(2)

    report = build_requirement_report([o1, o2], cap, situation, ctx)
    assert set(report.per_objective.keys()) == {1, 2}
    # both objectives surface the same missing `repo` decision → ask_bundle deduped
    repo_asks = [a for a in report.ask_bundle if a.schema_path.endswith("repo")]
    assert len(repo_asks) == 1, "ask_bundle must dedupe identical requirements"
