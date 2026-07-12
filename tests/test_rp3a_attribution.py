"""R-P3a — BOTH lanes attribute their LLM cost; no orphan (Phase A steps 3-4).

* Quick lane (AC1 "both lanes"): run_quick_task now stamps the ambient
  execution_id so its calls attribute to it (they orphaned before — the lane
  never set the carrier).
* Subagent/verifier (AC6): a call dispatched into a fresh worker thread inherits
  the parent run's execution_id via copy_context()+ctx.run — the pattern the
  shadow loop's run_in_executor LLM site now uses — so its cost rolls up to the
  parent instead of orphaning.
* The ExecutionSnapshot cost field round-trips + is populated from the ledger.
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import systemu.core.llm_router as router
from systemu.core.llm_router import llm_call_json
from systemu.runtime import costing
from systemu.runtime.chat_submission_ctx import current_execution_id, set_execution_id
from sharing_on.config import Config
from systemu.vault.vault import Vault


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    router._client = None
    costing.reset_ledger()
    monkeypatch.delenv(costing.PRICE_OVERRIDE_ENV, raising=False)
    yield
    router._client = None
    costing.reset_ledger()
    set_execution_id(None)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(root=str(tmp_path / "vault"))


@pytest.fixture
def dummy_config():
    cfg = Config.from_env()
    cfg.openrouter_api_key = "dummy_key"
    cfg.tier1_model = "test/tier1"
    cfg.tier2_model = "test/tier2"
    cfg.tier3_model = "test/tier3"
    return cfg


def _mock_openai(mock_async_openai, *, content, prompt_tokens=10, completion_tokens=5):
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_choice = AsyncMock()
    mock_choice.message.content = content
    mock_choice.message.reasoning_details = []
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    mock_client.chat.completions.create.return_value = mock_response
    ctx = AsyncMock()
    ctx.__aenter__.return_value = mock_client
    ctx.__aexit__.return_value = False
    mock_async_openai.return_value = ctx
    return mock_client


# ─────────────────────────────────────────────────────────────────────────────
#  Quick lane (AC1)


def test_quick_lane_sets_the_ambient_execution_id(vault):
    """During a quick run the ambient execution_id is the run's own quick_ id —
    so a router call inside the loop would attribute to it (not orphan)."""
    from systemu.pipelines.quick_task import run_quick_task

    seen = {}

    def llm(*, system, user, config=None):
        seen["eid"] = current_execution_id()
        return {"action": "ANSWER", "answer_md": "42", "completed": True}

    res = run_quick_task("what is 6x7", None, vault, llm_json=llm)
    assert res.status == "success"
    assert seen["eid"] is not None
    assert seen["eid"].startswith("quick_")


def test_quick_lane_resets_the_carrier_after_the_run(vault):
    """No leak: after the run the ambient execution_id is back to None so a later
    same-thread call can't be misattributed to the finished quick run."""
    from systemu.pipelines.quick_task import run_quick_task

    def llm(*, system, user, config=None):
        return {"action": "ANSWER", "answer_md": "ok", "completed": True}

    assert current_execution_id() is None
    run_quick_task("x", None, vault, llm_json=llm)
    assert current_execution_id() is None


@patch("systemu.core.llm_router.AsyncOpenAI")
def test_quick_lane_call_records_cost_end_to_end_no_orphan(mock_openai, vault, dummy_config):
    """The real no-orphan proof: an injected llm that drives the REAL router
    (mocked network only) records usage under the quick run's execution_id, and
    the result carries that cost."""
    _mock_openai(mock_openai,
                 content='{"action": "ANSWER", "answer_md": "done", "completed": true}',
                 prompt_tokens=12, completion_tokens=9)
    from systemu.pipelines.quick_task import run_quick_task

    captured = {}

    def real_llm(*, system, user, config=None):
        captured["eid"] = current_execution_id()
        return llm_call_json(tier=2, system=system, user=user, config=dummy_config)

    res = run_quick_task("do a thing", dummy_config, vault, llm_json=real_llm)
    assert res.status == "success"
    # The run's cost rows are attached to the result (AC1: quick lane shows cost).
    assert len(res.cost) == 1
    assert res.cost[0]["tokens_in"] == 12 and res.cost[0]["tokens_out"] == 9
    # And they are attributed to the quick run — not orphaned.
    summary = costing.cost_of(captured["eid"])
    assert summary.tokens_in == 12 and summary.tokens_out == 9


# ─────────────────────────────────────────────────────────────────────────────
#  Subagent / verifier attribution (AC6)


@patch("systemu.core.llm_router.AsyncOpenAI")
def test_subagent_worker_thread_attributes_to_parent_via_context_copy(mock_openai, dummy_config):
    """A subagent/verifier dispatched into a fresh worker thread that copies the
    parent context (the fixed run_in_executor pattern) attributes to the PARENT
    run — no orphan cost."""
    _mock_openai(mock_openai, content='{"ok": true}', prompt_tokens=40, completion_tokens=11)
    token = set_execution_id("parent-run")
    try:
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ctx.run, llm_call_json,
                        tier=2, system="s", user="u", config=dummy_config).result()
    finally:
        set_execution_id(None, reset_token=token)

    rows = costing.usage_rows("parent-run")
    assert len(rows) == 1
    assert rows[0]["tokens_in"] == 40 and rows[0]["tokens_out"] == 11


@patch("systemu.core.llm_router.AsyncOpenAI")
def test_naive_worker_thread_without_context_copy_orphans(mock_openai, dummy_config):
    """Documents the hazard the fix addresses: concurrent.futures does NOT copy
    contextvars, so a naive worker-thread dispatch orphans (this is why the
    shadow loop's run_in_executor LLM site must copy_context())."""
    _mock_openai(mock_openai, content='{"ok": true}')
    token = set_execution_id("parent-run")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(llm_call_json,
                        tier=2, system="s", user="u", config=dummy_config).result()
    finally:
        set_execution_id(None, reset_token=token)
    # Orphaned — nothing attributed to the parent.
    assert costing.usage_rows("parent-run") == []


# ─────────────────────────────────────────────────────────────────────────────
#  Snapshot cost field durability


def test_execution_snapshot_cost_field_round_trips(tmp_path):
    from systemu.runtime import execution_snapshot as es
    snap = es.ExecutionSnapshot(
        execution_id="exec-1", shadow_id="s", scroll_id="sc",
        cost=[{"model": "m/x", "tokens_in": 5, "tokens_out": 3, "at": "2026-07-12T00:00:00+00:00"}],
    )
    es.write_snapshot(snap, data_dir=tmp_path)
    loaded = es.read_snapshot("exec-1", data_dir=tmp_path)
    assert loaded is not None
    assert loaded.cost == snap.cost
    # And it prices off the record directly.
    assert costing.cost_of(loaded).tokens_in == 5


def test_capture_from_context_drains_the_ledger():
    """capture_from_context copies the run's ledger rows onto the snapshot so the
    cost survives a suspend→resume."""
    from systemu.runtime import execution_snapshot as es

    costing.record_usage("exec-9", "m/x", 100, 50)

    class _Ctx:
        def get_sticky_notes(self):
            return []

    snap = es.capture_from_context(
        execution_id="exec-9", shadow_id="s", scroll_id="sc",
        iteration=1, current_action_block=1, completed_objectives=set(), context=_Ctx(),
    )
    assert len(snap.cost) == 1
    assert snap.cost[0]["tokens_in"] == 100


def test_open_world_planner_call_attributes_to_the_run_not_orphan(monkeypatch):
    """HIGH (adversarial): the open-world planner's LLM call runs via
    run_in_executor — WITHOUT copy_context it orphaned its full cost (the exact
    hazard fixed at the decision loop but originally missed here). Prove the
    ambient execution_id propagates across the hop into the planner's LLM call."""
    import asyncio
    from systemu.runtime import open_world_planner as owp
    seen = {}

    def _capture(**kw):
        seen["eid"] = current_execution_id()
        raise ValueError("stop after capturing the eid")   # → planner fail-safe

    monkeypatch.setattr(owp, "_has_llm_provider", lambda cfg: True)
    monkeypatch.setattr(owp, "load_prompt", lambda *a, **k: "sys")
    monkeypatch.setattr(owp, "_build_planner_prompt", lambda **k: "prompt")
    monkeypatch.setattr(owp, "_resolve_planner_tier", lambda cfg: 1)
    monkeypatch.setattr(owp, "llm_call_json", _capture)

    set_execution_id("exec_planner_attr")
    try:
        asyncio.run(owp.run_open_world_planner(
            objectives=[object()], scroll_intent="x",
            situation_report=object(), config=Config(), next_id=1))
    finally:
        set_execution_id(None)

    assert seen.get("eid") == "exec_planner_attr", (
        "the planner LLM call must attribute to its run via copy_context across the "
        f"run_in_executor hop — not orphan; got {seen.get('eid')!r}")


def test_subagent_snapshot_cost_carries_only_its_own_rows():
    """LOW (adversarial): _usage_rows_for must NOT union the root run's rows into a
    sub-agent's durable cost field — that double-counts the root at query time
    (the root's rows already live in the root's own record)."""
    from systemu.runtime.execution_snapshot import _usage_rows_for
    costing.reset_ledger()
    costing.record_usage("root_eid", "m", 100, 50)
    costing.record_usage("child_eid", "m", 10, 5)
    child_rows = _usage_rows_for("child_eid", "root_eid")
    assert len(child_rows) == 1, "the child carries ONLY its own row, not the root's"
    assert child_rows[0]["tokens_in"] == 10 and child_rows[0]["tokens_out"] == 5


def test_resume_reseeds_ledger_and_migrates_stale_eid_no_loss_no_double(monkeypatch):
    """HIGH (adversarial): on resume execute() mints a FRESH execution_id, so the
    pre-suspend cost would be lost (the next capture overwrites snapshot.cost with
    post-resume-only rows) and the daily-total ledger-scan would double-count a
    stale eid. apply_to_context must re-seed the fresh eid from snapshot.cost AND
    drop the stale original eid → no loss, no double-count, full accumulation."""
    from systemu.runtime import execution_snapshot as es
    costing.reset_ledger()

    # pre-suspend: the run (eid A) recorded some cost, captured into snapshot.cost.
    costing.record_usage("exec_A", "deepseek/deepseek-v4-flash", 1000, 500)
    pre = costing.usage_rows("exec_A")
    snap = es.ExecutionSnapshot(execution_id="exec_A", shadow_id="s", scroll_id="sc",
                                cost=list(pre))

    # resume: execute() minted a fresh eid B and set it ambient.
    set_execution_id("exec_B")

    class _Ctx:
        def add_sticky_note(self, *a, **k):
            pass

    try:
        es.apply_to_context(snap, context=_Ctx())

        # the fresh eid carries the pre-suspend rows; the stale eid is gone.
        assert costing.usage_rows("exec_B") == pre, "fresh eid re-seeded from durable cost"
        assert costing.usage_rows("exec_A") == [], "stale eid dropped (no double-count)"

        # a post-resume call accumulates ON TOP (no overwrite / loss).
        costing.record_usage("exec_B", "deepseek/deepseek-v4-flash", 200, 100)
        full = costing.cost_of("exec_B")
        assert full.tokens_in == 1200 and full.tokens_out == 600, "pre + post, nothing lost"

        # daily-total ledger-scan == the run's own cost (no stale-eid double-count).
        daily = costing.daily_total()
        assert daily.tokens_in == 1200 and daily.tokens_out == 600
    finally:
        set_execution_id(None)
        costing.reset_ledger()
