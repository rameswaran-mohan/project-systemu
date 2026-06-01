"""v0.8.19 — agentic-UX hardening (param-validation, structured ask-user, live TODO)."""
from pathlib import Path
import pytest


# ── shared fixtures (used across tasks) ─────────────────────────────────────
@pytest.fixture
def tmp_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))

@pytest.fixture
def registry(tmp_vault):
    from systemu.runtime.tool_registry import ToolRegistry
    return ToolRegistry(Path(tmp_vault.root) / "tools" / "implementations", tmp_vault)


class TestValidateParams:
    def _schema(self):
        return {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}}

    def test_empty_schema_no_checks(self):
        from systemu.runtime.param_validation import validate_params
        assert validate_params({}, {"x": 1}) == []

    def test_valid_and_optional_omitted_ok(self):
        from systemu.runtime.param_validation import validate_params
        assert validate_params(self._schema(), {"query": "a", "max_results": 5}) == []
        assert validate_params(self._schema(), {"query": "a"}) == []

    def test_wrong_type_flagged(self):
        from systemu.runtime.param_validation import validate_params
        errs = validate_params(self._schema(), {"query": "a", "max_results": "five"})
        assert len(errs) == 1 and "must be integer" in errs[0]

    def test_required_only_when_explicit(self):
        from systemu.runtime.param_validation import validate_params
        assert validate_params({"x": {"type": "string"}}, {}) == []
        errs = validate_params({"x": {"type": "string", "required": True}}, {})
        assert len(errs) == 1 and "missing required parameter 'x'" in errs[0]

    def test_bool_is_not_integer(self):
        from systemu.runtime.param_validation import validate_params
        errs = validate_params({"n": {"type": "integer"}}, {"n": True})
        assert len(errs) == 1 and "boolean" in errs[0]

    def test_unknown_param_allowed(self):
        from systemu.runtime.param_validation import validate_params
        assert validate_params({"x": {"type": "string"}}, {"x": "a", "extra": 1}) == []


class TestParamGate:
    @pytest.mark.asyncio
    async def test_bad_params_rejected_before_load(self, tmp_vault, registry):
        from systemu.core.models import Tool, ToolType, ToolStatus
        t = Tool(id="tool_pv", name="pv_tool", description="d", tool_type=ToolType.API_CALL,
                 status=ToolStatus.DEPLOYED, enabled=True,
                 parameters_schema={"n": {"type": "integer"}})
        tmp_vault.save_tool(t)
        out = await registry.execute("pv_tool", {"n": "five"})
        assert out["success"] is False and out["error_type"] == "tool_param_invalid"
        assert "must be integer" in out["error"]

    @pytest.mark.asyncio
    async def test_no_schema_tool_unaffected(self, tmp_vault, registry):
        from systemu.core.models import Tool, ToolType, ToolStatus
        t = Tool(id="tool_ns", name="ns_tool", description="d", tool_type=ToolType.PYTHON_FUNCTION,
                 status=ToolStatus.DEPLOYED, enabled=True)
        tmp_vault.save_tool(t)
        out = await registry.execute("ns_tool", {"anything": 1})
        assert out.get("error_type") != "tool_param_invalid"


class TestRequestChoice:
    def _q(self):
        return [{"id": "approach", "prompt": "Which export?", "multi": False,
                 "options": [{"label": "CSV", "desc": "flat"}, {"label": "XLSX", "desc": "formatted"}],
                 "allow_free_text": True}]

    def test_no_queue_returns_none(self, monkeypatch):
        import systemu.interface.notifications as nf
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: None)
        assert nf.request_choice(self._q(), dedup_key="ask:1") is None

    def test_returns_parsed_answer_when_resolved(self, monkeypatch):
        import json, systemu.interface.notifications as nf
        class _Q:
            def get_resolved_choice(self, k): return json.dumps({"approach": "CSV"})
            def post(self, **kw): raise AssertionError("must not post when already resolved")
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: _Q())
        assert nf.request_choice(self._q(), dedup_key="ask:1") == {"approach": "CSV"}

    def test_pending_raised_when_unresolved(self, monkeypatch):
        import systemu.interface.notifications as nf
        from systemu.approval.exceptions import PendingChoiceRequest
        posted = {}
        class _Q:
            def get_resolved_choice(self, k): return None
            def post(self, **kw): posted.update(kw); return "dec_q"
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: _Q())
        with pytest.raises(PendingChoiceRequest) as ei:
            nf.request_choice(self._q(), dedup_key="ask:1")
        assert ei.value.dedup_key == "ask:1"
        assert posted["context"]["kind"] == "structured_question"
        assert posted["options"] and "Other" in posted["options"]


class TestResolveStructured:
    def test_accepts_json_for_structured(self, tmp_vault):
        import json
        from systemu.approval.decision_queue import OperatorDecisionQueue
        q = OperatorDecisionQueue(tmp_vault)
        did = q.post(title="t", body="b", options=["Submit"],
                     context={"kind": "structured_question", "questions": []}, dedup_key="ask:s")
        ans = json.dumps({"approach": "free text answer"})
        dec = q.resolve(did, choice=ans)
        assert dec.status == "resolved" and dec.choice == ans

    def test_plain_decision_still_strict(self, tmp_vault):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        q = OperatorDecisionQueue(tmp_vault)
        did = q.post(title="t", body="b", options=["Yes", "No"], dedup_key="ask:p")
        with pytest.raises(ValueError):
            q.resolve(did, choice="Maybe")


class TestStructuredRender:
    def test_build_structured_answer_serializes(self):
        from systemu.interface.pages.insights import build_structured_answer
        import json
        qs = [{"id": "approach", "options": [{"label": "CSV"}]},
              {"id": "note", "options": [], "allow_free_text": True}]
        out = build_structured_answer(qs, {"approach": "CSV", "note": "hello"})
        assert json.loads(out) == {"approach": "CSV", "note": "hello"}

    def test_view_model_carries_kind(self, tmp_vault):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.interface.pages.insights import _build_pending_decision_view_model
        q = OperatorDecisionQueue(tmp_vault)
        q.post(title="t", body="b", options=["Submit"],
               context={"kind": "structured_question", "questions": [{"id": "a", "options": []}]},
               dedup_key="ask:vm")
        view = _build_pending_decision_view_model(tmp_vault)
        assert isinstance(view, list) and view[0]["context"]["kind"] == "structured_question"


class TestObjectiveItems:
    def test_status_derivation(self):
        from systemu.runtime.shadow_runtime import _objective_items
        from systemu.core.models import Objective
        objs = [Objective(id=1, goal="a", success_criteria="x"),
                Objective(id=2, goal="b", success_criteria="y", depends_on=[1])]
        assert _objective_items(objs, set()) == [
            {"id": 1, "goal": "a", "status": "in_progress"},
            {"id": 2, "goal": "b", "status": "pending"}]
        done1 = _objective_items(objs, {1})
        assert done1[0]["status"] == "done" and done1[1]["status"] == "in_progress"

    def test_event_builder_shape(self):
        from systemu.runtime.shadow_runtime import _objective_state_event
        from systemu.core.models import Objective
        objs = [Objective(id=1, goal="a", success_criteria="x")]
        ev = _objective_state_event(objs, set(), "exec_1", stamp=lambda e: e)
        assert ev["category"] == "objective_state"
        assert ev["context"]["execution_id"] == "exec_1"
        assert ev["context"]["items"][0]["goal"] == "a"


class TestLiveObjectivesPane:
    def test_latest_objective_items_reducer(self):
        from systemu.interface.components.live_objectives_pane import _latest_objective_items
        evs = [
            {"category": "shadow", "message": "noise"},
            {"category": "objective_state", "context": {"items": [{"id": 1, "goal": "a", "status": "in_progress"}]}},
            {"category": "objective_state", "context": {"items": [{"id": 1, "goal": "a", "status": "done"}]}},
        ]
        assert _latest_objective_items(evs) == [{"id": 1, "goal": "a", "status": "done"}]
        assert _latest_objective_items([{"category": "shadow"}]) == []


class TestRefinerClarify:
    def test_no_questions_is_noop(self):
        from systemu.pipelines.scroll_refiner import _apply_clarifications
        r = {"title": "x"}
        out = _apply_clarifications(r, "s1", lambda c: {"_": c}, asker=lambda q, dedup_key: {"a": "b"})
        assert out is r   # no clarifying_questions → unchanged, asker never consulted

    def test_answers_folded_via_recall(self):
        from systemu.pipelines.scroll_refiner import _apply_clarifications
        r = {"clarifying_questions": [{"id": "fmt", "prompt": "Which format?"}]}
        seen = {}
        def _recall(ctx): seen["ctx"] = ctx; return {"title": "refined"}
        out = _apply_clarifications(r, "s1", _recall, asker=lambda q, dedup_key: {"fmt": "CSV"})
        assert out == {"title": "refined"} and "fmt: CSV" in seen["ctx"]

    def test_headless_none_proceeds_unchanged(self):
        from systemu.pipelines.scroll_refiner import _apply_clarifications
        r = {"clarifying_questions": [{"id": "fmt", "prompt": "?"}]}
        out = _apply_clarifications(r, "s1", lambda c: {"_": c}, asker=lambda q, dedup_key: None)
        assert out is r   # no queue → proceed with original draft (no stall)

    def test_pending_propagates(self):
        from systemu.pipelines.scroll_refiner import _apply_clarifications
        from systemu.approval.exceptions import PendingChoiceRequest
        def _asker(q, dedup_key):
            raise PendingChoiceRequest(decision_id="d", dedup_key=dedup_key, options=["x"])
        with pytest.raises(PendingChoiceRequest):
            _apply_clarifications({"clarifying_questions": [{"id": "a"}]}, "s1", lambda c: {}, asker=_asker)
