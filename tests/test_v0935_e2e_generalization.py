"""v0.9.35 — end-to-end JOIN test for the generalization toggle.

The per-phase suites already prove each phase in isolation
(tests/test_v0935_p2_analysis_generalization.py — analysis;
tests/runtime/test_param_resolution.py — slot schema + substitution;
tests/test_v0935_param_elicitation.py — the suspend/resume rail).

THIS test proves the phases JOIN across the package seam:

    sharing_on.analyzer.intent_extractor.extract_intent(generalization="broad")
        -> sharing_on.output.markdown._render_intent_block  (### Parameters)
        -> systemu.pipelines.scroll_refiner._build_scroll_parameters
           + _coerce_scroll_generalization  -> a REAL systemu.core.models.Scroll
        -> systemu.runtime.param_resolution.slot_schema_from_parameters
           (every slot required[] + ABSENT + captured value as default)
        -> systemu.runtime.param_resolution.substitute_parameters
           (operator answers slotted into the live scroll context)

and that the STANDARD path is a STRICT NO-OP:

    a standard Scroll (no parameters)
        -> systemu.runtime.shadow_runtime.ShadowRuntime._resolve_scroll_parameters
           returns None  (no INPUT request)
        -> the ExecutionContext the agent sees is byte-identical.

All LLM calls are MOCKED — the intent_extractor's ``OpenAI`` client is patched
exactly like tests/test_v0935_p2_analysis_generalization.py. No live calls.

The chain uses the REAL functions across BOTH packages (sharing_on analyzer +
output, systemu pipelines + runtime) — not reimplementations. The only mock is
the network LLM boundary (extract_intent's OpenAI client) and a refiner stand-in
(_build_scroll_parameters consumes the parameters block the refiner LLM echoes;
we feed it the same captured values the markdown surfaced, so the seam — not a
second mocked LLM — is what is exercised).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sharing_on.analyzer.intent_extractor import extract_intent


# ─────────────────────────────────────────────────────────────────────────────
#  Mocked-LLM intent extraction (verbatim style of the P2 phase suite)
# ─────────────────────────────────────────────────────────────────────────────

def _fake_step(n, label=None, event_summary=None):
    s = MagicMock()
    s.step_number = n
    s.label = label
    s.primary_app = None
    s.event_summary = event_summary or {}
    return s


def _fake_event(application=None):
    ev = MagicMock()
    ev.application = application
    ev.action = None
    ev.file_path = None
    ev.category = None
    ev.url = None
    ev.data = {}
    return ev


def _extract_broad():
    """Run the REAL extract_intent with a MOCKED OpenAI client that returns an
    abstract intent + the two lifted what/where parameters (product, site) with
    their CAPTURED values as defaults."""
    content = json.dumps({
        "intent": "Order a product from an online store",
        "expected_outcome": "the product is in the cart on the store",
        "success_signal": "an order-confirmation page is shown",
        "abstracted_steps": ["Find the product on the store", "Add it to the cart"],
        "confidence": "high",
        "parameters": [
            {"name": "product", "description": "Which product to order",
             "type": "string", "default": "Samsung Galaxy S24",
             "salient_kind": "product", "required": True},
            {"name": "site", "description": "Which store to order from",
             "type": "string", "default": "amazon.com",
             "salient_kind": "site", "required": True},
        ],
    })
    steps = [_fake_step(1, event_summary={"file": 1})]
    events = [_fake_event(application="Chrome")]
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = content
    with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        OpenAI_mock.return_value = client
        result = extract_intent(
            steps=steps, events=events,
            session_name="order-online", platform_info="windows",
            api_key="k", generalization="broad",
        )
    return result


def _extract_standard():
    """Same capture, recorded in STANDARD mode: the mocked LLM may still emit
    parameters, but extract_intent must DROP them (standard == no params)."""
    content = json.dumps({
        "intent": "Order a Samsung Galaxy S24 from amazon.com",
        "expected_outcome": "the product is in the cart",
        "success_signal": "an order-confirmation page is shown",
        "abstracted_steps": ["Add the item to the cart"],
        "confidence": "high",
        # standard must ignore these even if the model leaks them:
        "parameters": [{"name": "product", "default": "Samsung Galaxy S24"}],
    })
    steps = [_fake_step(1, event_summary={"file": 1})]
    events = [_fake_event(application="Chrome")]
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = content
    with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        OpenAI_mock.return_value = client
        result = extract_intent(
            steps=steps, events=events,
            session_name="order-online", platform_info="windows",
            api_key="k", generalization="standard",
        )
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  THE JOIN — broad path spans analysis -> scroll -> execution
# ─────────────────────────────────────────────────────────────────────────────

def test_broad_path_joins_analysis_to_scroll_to_execution():
    # ── Phase 1+2: record-time broad toggle + analysis ──────────────────────
    # The mocked LLM lifts ONLY the two salient what/where specifics; the
    # intent itself is abstract (no product/site baked in).
    extraction = _extract_broad()
    assert extraction.generalization == "broad"
    assert {p["name"] for p in extraction.parameters} == {"product", "site"}
    # captured values survive (redaction-aware passthrough) as the param default
    by_name = {p["name"]: p for p in extraction.parameters}
    assert by_name["product"]["default"] == "Samsung Galaxy S24"
    assert by_name["site"]["default"] == "amazon.com"
    # broad intent is genuinely abstract — the salient specifics are NOT baked in
    assert "Samsung Galaxy S24" not in extraction.intent
    assert "amazon.com" not in extraction.intent

    # ── markdown: ## Intent gets a ### Parameters sub-block (real renderer) ──
    from sharing_on.output.markdown import _render_intent_block
    md = _render_intent_block(extraction)
    assert "## Intent" in md
    assert "### Parameters" in md
    assert "**Generalization:** broad" in md
    assert "`product`" in md and "`site`" in md
    # the captured values are visible as the editable defaults
    assert "Samsung Galaxy S24" in md
    assert "amazon.com" in md

    # ── Phase 2 -> Phase 3 seam: refiner projects onto Scroll.parameters ────
    # _build_scroll_parameters consumes exactly the parameters block the
    # refiner LLM echoes from the markdown above (same captured values).
    from systemu.pipelines.scroll_refiner import (
        _build_scroll_parameters, _coerce_scroll_generalization,
    )
    from systemu.core.models import Scroll, ScrollParameter, ScrollStatus, Objective

    refiner_result = {
        "generalization": extraction.generalization,
        "parameters": extraction.parameters,
    }
    scroll_params = _build_scroll_parameters(refiner_result)
    scroll_generalization = _coerce_scroll_generalization(extraction.generalization)

    # Build a REAL Scroll carrying the projected parameters + generalization.
    scroll = Scroll(
        id="scroll_e2e_broad",
        name="Order a product from an online store",
        source_session_id="sess_e2e",
        raw_instructions_path="",
        narrative_md="",
        intent=extraction.intent,
        expected_outcome=extraction.expected_outcome,
        objectives=[
            Objective(
                id=1,
                goal="Order Samsung Galaxy S24 from amazon.com",
                success_criteria="Samsung Galaxy S24 is in the cart on amazon.com",
                output_type="state_change",
            ),
        ],
        status=ScrollStatus.APPROVED,
        generalization=scroll_generalization,
        parameters=scroll_params,
    )

    # The Scroll really carries broad generalization + two ScrollParameter rows,
    # each required=True with the captured value as the default (pinned KEY
    # CONSTRAINT, surviving the model round-trip).
    assert scroll.generalization == "broad"
    assert len(scroll.parameters) == 2
    assert all(isinstance(p, ScrollParameter) for p in scroll.parameters)
    sp = {p.name: p for p in scroll.parameters}
    assert sp["product"].required is True and sp["site"].required is True
    assert sp["product"].default == "Samsung Galaxy S24"
    assert sp["site"].default == "amazon.com"

    # ── Phase 3: slot schema — required[] + ABSENT + captured default ───────
    from systemu.runtime.param_resolution import (
        slot_schema_from_parameters, substitute_parameters,
    )
    schema = slot_schema_from_parameters(scroll.parameters)
    assert schema["type"] == "object"
    # KEY CONSTRAINT 1 — every slot is in required[] (the gap the operator fills)
    assert set(schema["required"]) == {"product", "site"}
    # KEY CONSTRAINT 2 — each slot carries the captured value as the schema
    # default (so the operator is ASKED with the value pre-filled + editable).
    assert schema["properties"]["product"]["default"] == "Samsung Galaxy S24"
    assert schema["properties"]["site"]["default"] == "amazon.com"
    # KEY CONSTRAINT 3 — the captured value is ABSENT from any *provided* values:
    # the schema declares it only as a default, never as a pre-supplied answer.
    # (A form built from this schema starts with no answers; required[] forces
    #  the ask.) We assert the schema itself carries no "value"/answer key and
    #  that the operator-provided answer dict below is what supplies values.
    for slot in ("product", "site"):
        assert "value" not in schema["properties"][slot]
        assert "const" not in schema["properties"][slot]

    # ── Phase 3 -> Phase 4 seam: operator edits the pre-filled values ───────
    # The operator changes BOTH slots away from the captured defaults. These
    # are the answers the suspend/resume rail (Phase 4) hands to
    # substitute_parameters; we drive substitution directly here to prove the
    # values flow into the live scroll context the agent executes against.
    scroll_json = [o.model_dump(mode="json") for o in scroll.objectives]
    answers = {"product": "Pixel 9", "site": "bestbuy.com"}
    new_json, new_intent, new_constraints, resolved = substitute_parameters(
        scroll.parameters, answers,
        scroll_json=scroll_json,
        intent=scroll.intent,
        constraints=dict(scroll.constraints),
    )
    # The operator's edited values are slotted into the objectives the agent runs.
    assert new_json[0]["goal"] == "Order Pixel 9 from bestbuy.com"
    assert new_json[0]["success_criteria"] == "Pixel 9 is in the cart on bestbuy.com"
    assert resolved == {"product": "Pixel 9", "site": "bestbuy.com"}
    # The captured defaults are GONE from the resolved context (fully substituted).
    assert "Samsung Galaxy S24" not in json.dumps(new_json)
    assert "amazon.com" not in json.dumps(new_json)
    # Inputs are never mutated (substitution returns copies).
    assert scroll_json[0]["goal"] == "Order Samsung Galaxy S24 from amazon.com"


# ─────────────────────────────────────────────────────────────────────────────
#  THE STRICT NO-OP — standard path adds nothing, changes nothing
# ─────────────────────────────────────────────────────────────────────────────

def _bare_runtime():
    """A ShadowRuntime without __init__ side effects. _resolve_scroll_parameters
    reads only scroll.parameters (no vault/network), so the bypass is safe —
    same pattern as tests/test_v0935_param_elicitation.py::_bare_runtime."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.config = None
    rt.vault = None
    return rt


def test_standard_path_is_strict_no_op():
    # ── analysis: standard drops parameters even if the model emits them ────
    extraction = _extract_standard()
    assert extraction.generalization == "standard"
    assert extraction.parameters == []

    # ── markdown: standard renders NO ### Parameters / Generalization line ──
    from sharing_on.output.markdown import _render_intent_block
    md = _render_intent_block(extraction)
    assert "### Parameters" not in md
    assert "**Generalization:**" not in md

    # ── refiner: standard -> no ScrollParameters, generalization collapses ──
    from systemu.pipelines.scroll_refiner import (
        _build_scroll_parameters, _coerce_scroll_generalization,
    )
    assert _build_scroll_parameters(
        {"generalization": "standard", "parameters": extraction.parameters}
    ) == []
    assert _coerce_scroll_generalization("standard") is None

    # Build a REAL standard Scroll (no parameters).
    from systemu.core.models import Scroll, ScrollStatus, Objective
    scroll = Scroll(
        id="scroll_e2e_std",
        name="Order a Samsung Galaxy S24 from amazon.com",
        source_session_id="sess_e2e_std",
        raw_instructions_path="",
        narrative_md="",
        intent=extraction.intent,
        objectives=[
            Objective(id=1, goal="Order the item",
                      success_criteria="item in cart", output_type="state_change"),
        ],
        status=ScrollStatus.APPROVED,
        generalization=None,
        parameters=[],
    )
    assert scroll.generalization is None
    assert scroll.parameters == []

    # ── execution: REAL _resolve_scroll_parameters returns None (no INPUT) ──
    rt = _bare_runtime()
    req = rt._resolve_scroll_parameters(scroll)
    assert req is None, "standard scroll must not raise a parameter INPUT request"

    # ── and the ExecutionContext the agent sees is byte-identical ───────────
    # A standard scroll never enters the substitute_parameters branch; prove the
    # context build for a paramless scroll is unchanged by the toggle by showing
    # the no-params substitution is the IDENTITY (the only thing Phase 4 could
    # do on resume — and there is no resume because _resolve returned None).
    from systemu.runtime.param_resolution import substitute_parameters
    scroll_json = [o.model_dump(mode="json") for o in scroll.objectives]
    before = json.dumps(scroll_json)
    new_json, new_intent, new_constraints, resolved = substitute_parameters(
        scroll.parameters, {},
        scroll_json=scroll_json, intent=scroll.intent,
        constraints=dict(scroll.constraints),
    )
    assert json.dumps(new_json) == before          # objectives unchanged
    assert new_intent == scroll.intent             # intent unchanged
    assert new_constraints == scroll.constraints   # constraints unchanged
    assert resolved == {}                          # nothing resolved


def test_broad_resolve_scroll_parameters_emits_input_request():
    """Companion to the no-op: the SAME runtime method, given a BROAD scroll,
    DOES emit a kind=INPUT param-substitution request whose requested_schema is
    exactly the slot schema (required[] + captured defaults). This pins that the
    broad/standard branch in _resolve_scroll_parameters keys on scroll.parameters
    — i.e. the two paths diverge at the real execution seam, not a test stub."""
    from systemu.core.models import (
        Scroll, ScrollParameter, ScrollStatus, Objective, HarnessKind,
    )
    scroll = Scroll(
        id="scroll_e2e_broad2",
        name="Order a product from an online store",
        source_session_id="sess_e2e2",
        raw_instructions_path="",
        narrative_md="",
        intent="Order a product from an online store",
        objectives=[Objective(id=1, goal="Order the item",
                              success_criteria="item in cart")],
        status=ScrollStatus.APPROVED,
        generalization="broad",
        parameters=[
            ScrollParameter(name="product", description="Which product",
                            type="string", default="Samsung Galaxy S24",
                            salient_kind="product", required=True),
            ScrollParameter(name="site", description="Which store",
                            type="string", default="amazon.com",
                            salient_kind="site", required=True),
        ],
    )
    rt = _bare_runtime()
    req = rt._resolve_scroll_parameters(scroll)
    assert req is not None
    assert getattr(req.kind, "value", req.kind) == HarnessKind.INPUT.value
    assert req.spec.get("param_substitution") is True
    sch = req.spec["requested_schema"]
    assert set(sch["required"]) == {"product", "site"}
    assert sch["properties"]["product"]["default"] == "Samsung Galaxy S24"
    assert sch["properties"]["site"]["default"] == "amazon.com"
    # No pending_tool — answers are substituted into the scroll, not re-dispatched.
    assert "pending_tool" not in req.spec
