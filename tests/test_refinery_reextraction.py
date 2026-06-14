"""Fix 4 — a STRUCTURALLY-failed refined scroll must re-map its tools/skills.

Root cause Y: the refinery appended a feedback hint but the activity kept its
frozen (broken) tool mapping because the extractor's idempotency guard returns
the existing activity whenever scroll.activity_id is set. Fix: on a structural
failure the refinery clears activity_id + sets status=APPROVED, so the next
extraction (recovery-sweep Pass 1 / re-approval) recomputes the mapping. A
TRANSIENT failure leaves the activity intact (the supervisor retry path).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _objective(oid=1):
    return SimpleNamespace(id=oid, goal="g", hints={},
                           model_dump=lambda mode=None: {"id": oid})


def _scroll():
    return SimpleNamespace(id="sc1", name="s", activity_id="act_old", status="linked",
                           action_blocks=[], objectives=[_objective(1)], updated_at=None)


def _run(monkeypatch, exec_result, exec_id):
    from systemu.pipelines import refinery
    appraisal = {"outcome": "scroll_refinement", "failed_action_block_index": 1,
                 "feedback": "web_act could not perceive the page"}
    calls = {"n": 0}

    def fake_llm(**kw):
        calls["n"] += 1
        return appraisal if calls["n"] == 1 else {"lessons": []}

    monkeypatch.setattr(refinery, "llm_call_json", fake_llm)
    vault = MagicMock()
    vault.load_index.return_value = []
    vault.load_shadow_memory.return_value = ("", [])
    scroll = _scroll()
    shadow = SimpleNamespace(id="sh1", name="x", description="")
    ctx = SimpleNamespace(execution_id=exec_id, get_full_history=lambda: [])
    refinery.process_execution_result(shadow, scroll, exec_result, ctx, MagicMock(), vault)
    return scroll


def test_structural_refinement_clears_activity_for_reextraction(monkeypatch):
    from systemu.core.models import ScrollStatus
    scroll = _run(monkeypatch, {"status": "partial", "structural_failure": True}, "ex_struct")
    assert scroll.activity_id is None                 # idempotency guard will now yield
    assert scroll.status == ScrollStatus.APPROVED     # recovery-sweep Pass 1 / re-approval picks it up


def test_transient_refinement_keeps_activity_for_retry(monkeypatch):
    scroll = _run(monkeypatch, {"status": "partial"}, "ex_transient")   # no structural_failure
    assert scroll.activity_id == "act_old"            # supervisor retry path unbroken
