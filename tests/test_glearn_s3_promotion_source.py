"""G-LEARN slice 3 (§5.9) — SOURCE-level pins for the promotion slice.

Isolated in its own module ON PURPOSE (same reason as
``tests/test_ra16_join_placement.py``): it reads source via ``inspect.getsource``, and
``tests/conftest.py`` auto-tags a WHOLE MODULE ``source_sensitive`` on that substring.
Folded into ``test_glearn_s3_promotion.py`` it would have dropped every behavioural and
anti-laundering pin there out of the edit-safe tier (``pytest -m "not source_sensitive"``,
GATE-TIER / DEC-14).

Each pin here protects a property that has NO behavioural expression — "the mistake is
not currently made anywhere in the file" — which is exactly the shape of the defect this
slice is most likely to ship.
"""
from __future__ import annotations

import inspect
import re


def test_the_promoter_never_calls_add_fact_without_an_explicit_origin():
    """THE PIN THIS SLICE EXISTS FOR.

    ``user_profile.add_fact``'s ``origin_class`` defaults to ABSENT, and absent
    grandfathers to ``operator`` in ``requirement_binder._fact_origin``. So a single
    forgotten kwarg silently converts a page-derived value into a trusted, silently-
    bound one. There is no behavioural test for "a SECOND, future call site also passes
    it", so the property is pinned structurally: this module may reach ``add_fact``
    through exactly ONE chokepoint, and that chokepoint passes the origin explicitly."""
    from systemu.runtime import ask_promotion as ap

    src = inspect.getsource(ap)
    calls = [m.start() for m in re.finditer(r"add_fact\(", src)]
    assert len(calls) == 1, (
        f"{len(calls)} add_fact call sites in ask_promotion — the promoter must reach "
        f"the profile through ONE chokepoint (_promote_fact), so the origin stamp "
        f"cannot be forgotten at a second, newer site")
    depth, i = 0, calls[0] + len("add_fact")
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    args = src[calls[0]:i]
    assert "origin_class=" in args, (
        f"the add_fact chokepoint does not pass origin_class:\n{args}\n"
        f"Absent ⇒ grandfathered to `operator` ⇒ the laundering bug.")

    # the chokepoint itself must not carry a default for the stamp
    sig = inspect.signature(ap._promote_fact)
    p = sig.parameters["origin_class"]
    assert p.default is inspect.Parameter.empty, (
        "_promote_fact.origin_class has a default — a caller that forgets it would "
        "silently get the default instead of a TypeError")


def test_the_promoter_never_writes_the_profile_spine():
    """``UserProfile`` has ``extra="forbid"`` and no ``origin_class`` field, so the
    spine structurally cannot carry taint, and ``_bind_profile`` hard-codes ``operator``
    for every spine hit. A promoter targeting ``default_output_dir`` is a natural thing
    for the next engineer to write and would launder 100% of the time."""
    from systemu.runtime import ask_promotion as ap

    src = inspect.getsource(ap)
    assert "save_profile" not in src, (
        "ask_promotion must never write the UserProfile spine — it cannot carry "
        "origin_class, so every spine promotion launders")


def test_the_ask_promotion_source_string_is_carved_out_of_the_grandfather():
    """``_fact_origin`` grandfathers an ABSENT stamp to ``operator``. The promoter's
    source string must be OUTSIDE that grandfather (⇒ ``content_derived``), so a
    dropped stamp over-asks (safe) instead of laundering (unsafe). A NEW source string
    is required — reusing ``auto_extract`` would misattribute promotions in the audit
    trail and couple two unrelated writers."""
    from systemu.runtime import ask_promotion as ap
    from systemu.runtime import requirement_binder as rb

    assert ap.PROMOTION_SOURCE == "ask_promotion"
    assert ap.PROMOTION_SOURCE in rb._UNTRUSTED_ABSENT_SOURCES, (
        "requirement_binder does not carve ask_promotion out of the absent⇒operator "
        "grandfather; a dropped origin stamp would launder silently")
    assert "auto_extract" in rb._UNTRUSTED_ABSENT_SOURCES, (
        "the pre-existing auto_extract carve-out was lost")
    # the carve-out must stay NARROW: widening it to a real operator surface would
    # re-taint legacy facts and break the slice-1 compatibility claim
    assert rb._UNTRUSTED_ABSENT_SOURCES == frozenset({"auto_extract", "ask_promotion"})
    # ...and the reader must actually consult it
    assert "_UNTRUSTED_ABSENT_SOURCES" in inspect.getsource(rb._fact_origin), (
        "_fact_origin no longer consults the carve-out set")


def test_the_learned_loader_preserves_origin_while_forcing_provenance():
    """The mirror-image of ``load_operator_items``. That loader force-stamps BOTH
    ``provenance`` and ``origin_class`` — correct for an operator declaration, fatal
    here: forcing the origin would destroy the very taint this slice carries. Pinned
    structurally because "the assignment is absent" has no behavioural expression
    beyond the one canonical-value case the sibling module already covers."""
    from systemu.runtime import table_store as ts

    src = inspect.getsource(ts.load_learned_items)
    assert re.search(r'\.provenance\s*=\s*"learned"', src), (
        "load_learned_items must force provenance='learned' (a hand-edited sidecar "
        "must not be able to claim operator_added and get an operator badge)")
    assert not re.search(r'\.origin_class\s*=\s*"(operator|systemu_authored)"', src), (
        "load_learned_items must PRESERVE origin_class (clamping unknown values to "
        "content_derived is fine) — force-stamping a trusted origin here launders "
        "every learned item on read")


def test_the_projector_merges_learned_after_the_operator_loop():
    """Merge ORDER is load-bearing and has a thin behavioural surface (only the
    same-ref_key collision case shows it). ``project()`` has no registry or plugin
    point — it reads exactly three sidecars by name — so the learned merge is a direct
    edit that a future refactor could reorder or drop."""
    from systemu.runtime import table_reconciler as tr

    src = inspect.getsource(tr.project)
    assert "load_learned_items" in src, (
        "project() does not merge the learned sidecar — an item written anywhere else "
        "is GONE after one reconcile_once tick (the reconciler is items.json's sole "
        "writer and re-projects from scratch)")
    # Compare the LOOP HEADERS, not the first mention of each loader. The learned block
    # itself calls `load_operator_items` (to build `operator_keys`), so an index() on
    # the bare loader names is satisfied by that call even when the whole block has
    # been hoisted above the operator loop — a mutation that reordered the blocks
    # survived this pin in exactly that way until it was tightened.
    op_loop = src.index("for item in ts.load_operator_items(vault):")
    learned_loop = src.index("for item in ts.load_learned_items(vault):")
    assert op_loop < learned_loop, (
        "the learned merge must run AFTER the operator loop so an operator "
        "declaration always wins a ref_key collision")
    # ...and after the live-store loop, which is what populates `live_keys`
    assert src.index("live_keys.add(key)") < learned_loop, (
        "the learned merge must run after the live-store projection, or `live_keys` "
        "is not yet populated and a learned card can shadow a live object")
    learned = src[src.index("load_learned_items"):]
    assert "tombstones" in learned and "live_keys" in learned, (
        "the learned merge must skip tombstoned refs (no re-add flapping) and refs "
        "already claimed by a live store object")
    assert "setdefault" in learned, (
        "the learned merge must use setdefault so a live/operator item always wins")


def test_the_promotion_call_site_sits_after_the_idempotency_stamp():
    """Same reasoning as the sibling avoidable-ask join: this reconciler RETRIES any
    row whose dispatch raised, so promoting at the answer-coercion point would
    re-promote on every retry and would promote an answer that was never applied to
    the run. It must sit after the ``harness_grant_dispatched`` stamp, exactly once."""
    from systemu.scheduler import jobs

    src = inspect.getsource(jobs.reconcile_resolved_harness_grants)
    assert src.count("promote_answered_asks(") == 1, (
        "exactly one promotion call site — a second one restores the double-promote")
    stamp = src.index('decision.context["harness_grant_dispatched"] = True')
    assert src.index("promote_answered_asks(") > stamp, (
        "promote_answered_asks must be called AFTER the harness_grant_dispatched "
        "stamp, otherwise a retried dispatch re-promotes the same answer")


def test_the_tick_promotion_budget_is_built_OUTSIDE_the_decision_loop():
    """The whole value of the per-tick budget is its PLACEMENT, and placement has no
    behavioural expression from inside ``promote_answered_asks`` — a budget constructed
    per iteration type-checks, passes every behavioural pin, and restores the exact
    defect it fixes (N answered cards ⇒ N × the cap). So it is pinned structurally."""
    from systemu.scheduler import jobs

    src = inspect.getsource(jobs.reconcile_resolved_harness_grants)
    built = src.index("PromotionBudget()")
    loop = src.index("for did in candidate_ids:")
    assert built < loop, (
        "the PromotionBudget is constructed INSIDE the decision loop — each card would "
        "get a fresh allowance, which is exactly the per-call bound it replaces")
    assert src.count("PromotionBudget()") == 1, (
        "more than one budget per tick — the bound is only real if every card in the "
        "tick draws from the SAME allowance")
    call = src.index("promote_answered_asks(")
    args = src[call:src.index(")", src.index("_ask_answers", call))]
    assert "budget=" in args, (
        "the promotion call site does not pass the tick budget:\n%s" % args)


def test_the_promoter_is_wired_only_at_the_mid_loop_chokepoint():
    """``elicitation.surface_ask_bundle_requirement`` is unreachable in production (its
    only call site passes ``capability=None`` ⇒ an empty ask_bundle) and carries NO
    idempotency stamp, so two resumes there produce two promotions. The mid-loop
    reconciler is the ONLY protected chokepoint."""
    from systemu.runtime import elicitation

    src = inspect.getsource(elicitation)
    assert "promote_answered_asks" not in src, (
        "the promoter was wired into the elicitation rail, which has no idempotency "
        "stamp — two resumes would promote twice")
