"""R-A10 step B12 (RISK-3): merge a param-substitution grant ONTO a persisted
objective graph, instead of dropping it.

At G1 the persisted-graph branch of ``_resolve_objectives_for_run`` unconditionally
WON over the param-substitution seam-fix: if a resume carried BOTH a non-empty
persisted ``objective_graph`` AND a param-sub grant (``context.scroll_json`` was
replaced by ``substitute_parameters``), branch 1 rebuilt from the graph and RETURNED
— silently discarding the operator's freshly-substituted values.

This was UNREACHABLE at G1 (nothing wrote a non-empty graph). It is now LIVE: B5/B7/B9
persist non-empty graphs (planner/backchain inserts). So a run that inserts a precede
persists a graph, and a resume carrying a param-sub then hits branch 1 and loses the
substituted values.

B12 adds a 4th case at the TOP of the precedence: when BOTH a persisted graph AND a
param-sub are present, MERGE by re-applying the operator's ACTUAL string-substitution
to the graph nodes —
  * the graph is authoritative for STRUCTURE (which objectives + which ``requirements``
    exist, ``depends_on`` incl. inserted precede ids, ``origin``,
    ``requires_external_verification``);
  * EVERY string leaf of each graph node is re-substituted with the operator's
    ``(old → new)`` pairs (``goal``, ``success_criteria``, ``output_type``, ``verifier``,
    ``hints`` values, AND each ``requirement``'s ``schema_path`` / ``rationale`` /
    ``bound_value_ref``) — so a value substituted inside ``requirements`` is NO LONGER
    silently dropped;
  * graph nodes with NO original counterpart (inserted precedes) are substituted too —
    harmless, their strings simply don't contain the substituted token.

The substitution pairs are threaded from the grant-apply site as ``context._paramsub_pairs``
(a list of ``(old_s, new_s)`` tuples — exactly what ``substitute_parameters`` computed and
applied). When the pairs are UNAVAILABLE the merge falls back to a leaf-diff between the
static scroll (pre-sub) and ``ctx_scroll_json`` (post-sub) for matched ids.

Precedence is now: merge(graph, param-sub) > graph-only > param-sub-only > static identity.

These tests call the PURE ``_resolve_objectives_for_run`` DIRECTLY (no execute() loop)
so they stay fast.
"""


def _sj(objs):
    return [o.model_dump(mode="json") for o in objs]


# ─────────────────────────────────────────────────────────────────────────────
# 1. RISK-3 repro — a persisted 2-node graph (original id=1 depends_on=[2] +
#    inserted precede id=2) + a param-sub that rewrote id=1's substituted value.
#    The merged result must carry ALL of: (a) the inserted precede id=2,
#    (b) id=1's SUBSTITUTED value (from the param-sub), (c) id=1's depends_on=[2]
#    (from the graph). Before B12 branch 1 wins → id=1 keeps its STALE graph value.
# ─────────────────────────────────────────────────────────────────────────────

def test_risk3_merge_keeps_precede_and_substituted_value_and_depends_on():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    # The STATIC scroll tree — a single original objective id=1 with the CAPTURED
    # (pre-substitution) value in its goal.
    scroll_objs = [Objective(id=1, goal="Ship to Berlin", success_criteria="s")]
    sj = _sj(scroll_objs)

    # The PERSISTED graph: id=1 (STALE goal, gained depends_on=[2] from the insert)
    # + an inserted precede id=2 (origin backchain).
    graph = [
        Objective(id=1, goal="Ship to Berlin", success_criteria="s",
                  depends_on=[2], origin="planner"),
        Objective(id=2, goal="Obtain export licence", success_criteria="s2",
                  depends_on=[], origin="backchain"),
    ]

    # The PARAM-SUB: the operator substituted "Berlin" → "Munich". substitute_parameters
    # rewrote id=1's goal (a literal string replace). NO inserts here — the param-subbed
    # scroll_json is the ORIGINAL scroll objectives (ids 1..N, no precedes),
    # so id=1 here has NO depends_on.
    subbed = [Objective(id=1, goal="Ship to Munich", success_criteria="s")]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        _paramsub_pairs = [("Berlin", "Munich")]

    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )

    by_id = {o.id: o for o in out_objs}
    # (a) the inserted precede survived the merge
    assert set(by_id) == {1, 2}, [o.id for o in out_objs]
    assert by_id[2].goal == "Obtain export licence"
    assert by_id[2].origin == "backchain"
    # (b) id=1 carries the SUBSTITUTED value from the param-sub, NOT the stale graph goal
    assert by_id[1].goal == "Ship to Munich", by_id[1].goal
    # (c) id=1's depends_on=[2] is PRESERVED from the graph (structural)
    assert by_id[1].depends_on == [2], by_id[1].depends_on

    # The returned scroll_json dump matches the merged objectives.
    assert [o["id"] for o in out_sj] == [1, 2]
    dump_by_id = {o["id"]: o for o in out_sj}
    assert dump_by_id[1]["goal"] == "Ship to Munich"
    assert dump_by_id[1]["depends_on"] == [2]


# ─────────────────────────────────────────────────────────────────────────────
# 1b. FINDING 1 repro — a substituted value INSIDE a graph objective's
#     ``requirements`` list. Before B12's rewrite the 5-field overlay left the
#     graph's requirements UNTOUCHED → schema_path/rationale/bound_value_ref kept
#     the STALE "Berlin" while goal said "Munich" (internally incoherent). Now
#     every string leaf — requirements included — is re-substituted.
# ─────────────────────────────────────────────────────────────────────────────

def test_requirements_leaves_are_substituted_not_dropped():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective, Requirement

    scroll_objs = [Objective(id=1, goal="Ship to Berlin", success_criteria="s")]
    sj = _sj(scroll_objs)

    # The graph carries a backchain-added requirement whose string leaves DO contain
    # the substituted token ("Berlin") + id=1 gained depends_on=[2] + an inserted
    # precede id=2.
    req = Requirement(
        kind="input",
        schema_path="/dest/Berlin",
        state="missing",
        source="schema",
        rationale="destination is Berlin",
        bound_value_ref="input:dest=Berlin",
    )
    graph = [
        Objective(id=1, goal="Ship to Berlin", success_criteria="s",
                  depends_on=[2], origin="planner", requirements=[req]),
        Objective(id=2, goal="Obtain export licence", success_criteria="s2",
                  depends_on=[], origin="backchain"),
    ]

    # The operator substituted Berlin → Munich. substitute_parameters rewrote EVERY
    # string leaf of the original scroll objective (goal here); the graph node is what
    # must be re-substituted by the merge.
    subbed = [Objective(id=1, goal="Ship to Munich", success_criteria="s")]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        _paramsub_pairs = [("Berlin", "Munich")]

    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )
    by_id = {o.id: o for o in out_objs}

    # goal substituted
    assert by_id[1].goal == "Ship to Munich", by_id[1].goal
    # depends_on preserved (structural)
    assert by_id[1].depends_on == [2], by_id[1].depends_on
    # the inserted precede survived
    assert 2 in by_id and by_id[2].goal == "Obtain export licence"
    # THE FIX: the requirement's string leaves are SUBSTITUTED, not stale-Berlin.
    assert len(by_id[1].requirements) == 1
    r = by_id[1].requirements[0]
    assert r.schema_path == "/dest/Munich", r.schema_path
    assert r.rationale == "destination is Munich", r.rationale
    assert r.bound_value_ref == "input:dest=Munich", r.bound_value_ref

    # And the returned scroll_json dump is coherent too (no stale Berlin anywhere).
    import json as _json
    blob = _json.dumps(out_sj)
    assert "Berlin" not in blob, blob


# ─────────────────────────────────────────────────────────────────────────────
# 1c. graph-only backchain requirement whose strings DON'T contain the substituted
#     value → it survives the merge UNCHANGED. The graph is authoritative for WHICH
#     requirements exist; substitution only rewrites matching strings.
# ─────────────────────────────────────────────────────────────────────────────

def test_graph_only_requirement_without_token_survives_unchanged():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective, Requirement

    scroll_objs = [Objective(id=1, goal="Ship to Berlin", success_criteria="s")]
    sj = _sj(scroll_objs)

    # A backchain-added requirement about a credential — its strings don't mention Berlin.
    req = Requirement(kind="credential", schema_path="/token", state="missing",
                      source="schema", rationale="an auth token is required",
                      bound_value_ref="cred:api_token")
    graph = [
        Objective(id=1, goal="Ship to Berlin", success_criteria="s",
                  depends_on=[], origin="planner", requirements=[req]),
    ]
    subbed = [Objective(id=1, goal="Ship to Munich", success_criteria="s")]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        _paramsub_pairs = [("Berlin", "Munich")]

    out_objs, _ = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )
    o = out_objs[0]
    assert o.goal == "Ship to Munich"
    # The requirement EXISTS (graph authoritative) and is UNCHANGED (no token to sub).
    assert len(o.requirements) == 1
    r = o.requirements[0]
    assert r.schema_path == "/token"
    assert r.rationale == "an auth token is required"
    assert r.bound_value_ref == "cred:api_token"


# ─────────────────────────────────────────────────────────────────────────────
# 2. graph-only (no param-sub) — ctx_scroll_json is scroll_json (no substitution)
#    + a graph → branch 1 UNCHANGED (rebuild from graph, NO merge). Byte-identical
#    to today's persisted-graph behavior.
# ─────────────────────────────────────────────────────────────────────────────

def test_graph_only_no_paramsub_rebuilds_from_graph_unchanged():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    scroll_objs = [Objective(id=1, goal="g", success_criteria="s")]
    sj = _sj(scroll_objs)
    graph = [
        Objective(id=1, goal="g", success_criteria="s"),
        Objective(id=2, goal="inserted", success_criteria="s2",
                  depends_on=[1], origin="backchain"),
    ]

    # No param-sub: context.scroll_json IS the same object as scroll_json (identity).
    class _Ctx:
        scroll_json = sj

    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )
    assert [o.id for o in out_objs] == [1, 2]
    assert out_objs[1].origin == "backchain"
    assert [o["id"] for o in out_sj] == [1, 2]
    # Graph objective id=1 kept its graph goal (no merge fired — no param-sub).
    assert out_objs[0].goal == "g"


# ─────────────────────────────────────────────────────────────────────────────
# 3. param-sub-only (no graph) — empty persisted graph + a param-sub → branch 2
#    UNCHANGED (rebuild from ctx_scroll_json; return it by identity).
# ─────────────────────────────────────────────────────────────────────────────

def test_paramsub_only_no_graph_unchanged():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    scroll_objs = [Objective(id=1, goal="g", success_criteria="s")]
    sj = _sj(scroll_objs)
    subbed = [Objective(id=1, goal="g", success_criteria="s", hints={"x": "y"})]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj

    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=None,
    )
    assert out_objs[0].hints == {"x": "y"}   # rebuilt from ctx_scroll_json
    assert out_sj is sub_sj                    # returned BY IDENTITY (branch 2)

    # An EMPTY (falsy) graph is also branch 2, not the merge.
    out_objs2, out_sj2 = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=[],
    )
    assert out_objs2[0].hints == {"x": "y"}
    assert out_sj2 is sub_sj


# ─────────────────────────────────────────────────────────────────────────────
# 4. identity (AC6) — neither a graph nor a param-sub → branch 3 returns
#    ``objectives`` / ``scroll_json`` BY IDENTITY (the byte-identical floor).
#    B12 must NOT perturb this.
# ─────────────────────────────────────────────────────────────────────────────

def test_identity_ac6_unchanged():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    objs = [Objective(id=1, goal="g", success_criteria="s")]
    sj = _sj(objs)

    class _Ctx:
        scroll_json = None    # no param-sub

    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=None,
    )
    assert out_objs is objs    # SAME list object — no rebuild
    assert out_sj is sj

    # context.scroll_json IS scroll_json (identity — not a substitution) + no graph
    # is ALSO the identity branch.
    class _CtxSame:
        scroll_json = sj

    out_objs2, out_sj2 = _resolve_objectives_for_run(
        use_objectives=True, objectives=objs, scroll_json=sj,
        context=_CtxSame(), resume_objective_graph=None,
    )
    assert out_objs2 is objs
    assert out_sj2 is sj


# ─────────────────────────────────────────────────────────────────────────────
# 5. structural precedence — a param-subbed obj whose ``depends_on`` in
#    ctx_scroll_json is the ORIGINAL (no precede) must be OVERRIDDEN by the graph's
#    depends_on (WITH the precede). The graph wins for structure so the precede gate
#    is never lost; substitution only rewrites string VALUES (goal), never depends_on.
# ─────────────────────────────────────────────────────────────────────────────

def test_graph_wins_for_structure_over_paramsub_depends_on():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective, Requirement

    scroll_objs = [Objective(id=1, goal="Do X for Berlin", success_criteria="s")]
    sj = _sj(scroll_objs)

    req = Requirement(kind="credential", schema_path="/token", state="missing",
                      source="schema")
    graph = [
        Objective(id=1, goal="Do X for Berlin", success_criteria="s",
                  depends_on=[2], origin="planner", requirements=[req],
                  requires_external_verification=True),
        Objective(id=2, goal="precede", success_criteria="s2",
                  depends_on=[], origin="backchain"),
    ]

    # The param-sub REBUILT id=1 from the original scroll (no precede) — so its
    # depends_on is [] (the ORIGINAL wiring) and it has NO requirements. It ALSO
    # substituted the goal "Berlin" → "Paris".
    subbed = [Objective(id=1, goal="Do X for Paris", success_criteria="s",
                        depends_on=[], requirements=[])]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        _paramsub_pairs = [("Berlin", "Paris")]

    out_objs, _ = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )
    by_id = {o.id: o for o in out_objs}
    # STRUCTURE from the graph (wins): depends_on with the precede, requirements,
    # requires_external_verification, origin.
    assert by_id[1].depends_on == [2], by_id[1].depends_on
    assert len(by_id[1].requirements) == 1
    assert by_id[1].requirements[0].schema_path == "/token"
    assert by_id[1].requires_external_verification is True
    assert by_id[1].origin == "planner"
    # VALUE from the param-sub (wins): the substituted goal.
    assert by_id[1].goal == "Do X for Paris", by_id[1].goal
    # And the precede survived.
    assert by_id[2].goal == "precede"


# ─────────────────────────────────────────────────────────────────────────────
# 6. hints in-place — a graph node with a backchain-added hint key + a param-sub
#    touching ANOTHER hint value → BOTH hint keys survive, the substituted value
#    is updated in place (the old wholesale-overwrite would have replaced the whole
#    hints dict, dropping the graph-only key).
# ─────────────────────────────────────────────────────────────────────────────

def test_hints_substituted_in_place_graph_only_key_preserved():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    scroll_objs = [Objective(id=1, goal="g", success_criteria="s",
                             hints={"url": "http://x/Berlin"})]
    sj = _sj(scroll_objs)

    # The graph node gained a backchain hint key ("precede_note") AND still carries
    # the original "url" hint (with the pre-sub Berlin token).
    graph = [
        Objective(id=1, goal="g", success_criteria="s",
                  hints={"url": "http://x/Berlin", "precede_note": "from backchain"}),
    ]
    # The param-sub rewrote the "url" hint value Berlin → Munich in the original scroll.
    subbed = [Objective(id=1, goal="g", success_criteria="s",
                        hints={"url": "http://x/Munich"})]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        _paramsub_pairs = [("Berlin", "Munich")]

    out_objs, _ = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )
    h = out_objs[0].hints
    # graph-only key preserved
    assert h.get("precede_note") == "from backchain", h
    # substituted value updated in place
    assert h.get("url") == "http://x/Munich", h


# ─────────────────────────────────────────────────────────────────────────────
# 7. fallback (no threaded pairs) — the merge still substitutes by DIFFING the
#    static scroll (pre-sub) against ctx_scroll_json (post-sub) for matched ids.
#    This exercises the robustness path when the grant-apply site didn't stash pairs.
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_leaf_diff_when_pairs_absent():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    scroll_objs = [Objective(id=1, goal="Ship to Berlin", success_criteria="s")]
    sj = _sj(scroll_objs)
    graph = [
        Objective(id=1, goal="Ship to Berlin", success_criteria="s",
                  depends_on=[2], origin="planner"),
        Objective(id=2, goal="precede", success_criteria="s2",
                  depends_on=[], origin="backchain"),
    ]
    subbed = [Objective(id=1, goal="Ship to Munich", success_criteria="s")]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        # NO _paramsub_pairs attribute — force the diff fallback.

    out_objs, _ = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )
    by_id = {o.id: o for o in out_objs}
    assert by_id[1].goal == "Ship to Munich", by_id[1].goal
    assert by_id[1].depends_on == [2]
    assert by_id[2].goal == "precede"


# ─────────────────────────────────────────────────────────────────────────────
# 8. HIGH — a param-sub whose OLD value IS (or contains) a Literal/enum token must
#    NOT crash the merge. The graph is authoritative for STRUCTURE (the Literal
#    fields: Objective.origin + each Requirement's state/kind/source/value_origin).
#    _replace_in_obj rewrites EVERY string leaf including those enum fields; before
#    the fix _Objective.model_validate raised a ValidationError → the resume crashed.
#    After the fix the structural Literal fields are RESTORED from the original graph
#    node before validate, so the enum stays valid while the free-text VALUE leaves
#    are still substituted.
# ─────────────────────────────────────────────────────────────────────────────

import pytest


# Each case pins the FIXTURE's Literal fields to genuinely COLLIDE with old_token, so
# _replace_in_obj really corrupts an enum → model_validate would raise pre-fix.
@pytest.mark.parametrize("old_token,new_token,obj_origin,req_state,req_kind,req_vo", [
    # collides with Requirement.value_origin ("operator")
    ("operator", "the operator", "backchain", "missing", "credential", "operator"),
    # collides with Requirement.state ("missing")
    ("missing", "gone missing", "backchain", "missing", "credential", "operator"),
    # collides with Requirement.kind ("input")
    ("input", "the input file", "backchain", "missing", "input", "operator"),
    # collides with Objective.origin ("planner")
    ("planner", "the planner", "planner", "missing", "credential", "operator"),
])
def test_literal_corrupting_paramsub_does_not_crash_and_restores_enum(
        old_token, new_token, obj_origin, req_state, req_kind, req_vo):
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective, Requirement

    # A graph objective carrying a requirement whose Literal fields spell out the very
    # token the operator's substitution rewrites. Its VALUE leaves (rationale,
    # schema_path, bound_value_ref, goal) ALSO contain the token so we can prove the
    # substitution STILL fires on the value axis.
    req = Requirement(
        kind=req_kind,
        schema_path=f"/creds/{old_token}",
        state=req_state,
        source="runtime_error",
        value_origin=req_vo,
        rationale=f"the {old_token} must supply this",
        bound_value_ref=f"ref:{old_token}",
    )
    graph = [
        Objective(id=1, goal=f"handle the {old_token} case", success_criteria="s",
                  depends_on=[], origin=obj_origin, requirements=[req]),
    ]
    scroll_objs = [Objective(id=1, goal=f"handle the {old_token} case",
                             success_criteria="s")]
    sj = _sj(scroll_objs)
    subbed = [Objective(id=1, goal=f"handle the {new_token} case",
                        success_criteria="s")]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        _paramsub_pairs = [(old_token, new_token)]

    # BEFORE the fix this raised pydantic.ValidationError (enum corrupted). It must
    # not crash — the resume can NEVER crash on a param-sub.
    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )

    o = out_objs[0]
    # STRUCTURAL Literal fields RESTORED from the graph (still valid enums).
    assert o.origin == obj_origin, o.origin
    assert len(o.requirements) == 1
    r = o.requirements[0]
    assert r.state == req_state, r.state
    assert r.kind == req_kind, r.kind
    assert r.source == "runtime_error", r.source
    assert r.value_origin == req_vo, r.value_origin
    # VALUE leaves ARE substituted (the fix substitutes values, restores only structure).
    assert o.goal == f"handle the {new_token} case", o.goal
    assert r.rationale == f"the {new_token} must supply this", r.rationale
    assert r.schema_path == f"/creds/{new_token}", r.schema_path
    assert r.bound_value_ref == f"ref:{new_token}", r.bound_value_ref
    # id preserved throughout.
    assert o.id == 1


def test_literal_corrupting_paramsub_belt_and_suspenders_falls_back_to_original():
    """BELT-AND-SUSPENDERS: even if a substitution corrupts a node in a way the
    structural-restore can't cover (a VALUE leaf becomes something model_validate
    rejects), the per-node guard falls back to the ORIGINAL graph node rather than
    crashing. We force this by monkeypatching _replace_in_obj to corrupt a NON-restored,
    typed field (requires_external_verification → a non-bool/non-coercible string) so
    validate raises; the merge must degrade to the original node, not propagate."""
    from systemu.runtime import shadow_runtime as _sr
    from systemu.core.models import Objective

    graph = [
        Objective(id=1, goal="keep me", success_criteria="s",
                  depends_on=[], origin="planner"),
    ]
    scroll_objs = [Objective(id=1, goal="keep me", success_criteria="s")]
    sj = _sj(scroll_objs)
    subbed = [Objective(id=1, goal="keep me too", success_criteria="s")]
    sub_sj = _sj(subbed)

    class _Ctx:
        scroll_json = sub_sj
        _paramsub_pairs = [("keep me", "keep me too")]

    # Corrupt requires_external_verification (a bool field NOT restored by the
    # structural-restore step) to a non-coercible string so _Objective.model_validate
    # raises — the per-node guard must catch it and fall back to the original node.
    import systemu.runtime.param_resolution as _pr
    orig_replace = _pr._replace_in_obj

    def _corrupting_replace(obj, old, new):
        out = orig_replace(obj, old, new)
        if isinstance(out, dict) and "requires_external_verification" in out:
            out = dict(out)
            out["requires_external_verification"] = "definitely-not-a-bool"
        return out

    import pytest as _pt
    monkey = _pt.MonkeyPatch()
    monkey.setattr(_pr, "_replace_in_obj", _corrupting_replace)
    try:
        out_objs, _out_sj = _sr._resolve_objectives_for_run(
            use_objectives=True, objectives=scroll_objs, scroll_json=sj,
            context=_Ctx(), resume_objective_graph=graph,
        )
    finally:
        monkey.undo()

    # It did NOT crash; the guard fell back to the ORIGINAL graph node (un-substituted).
    assert len(out_objs) == 1
    o = out_objs[0]
    assert o.id == 1
    assert o.goal == "keep me"           # original, since the substituted node was rejected
