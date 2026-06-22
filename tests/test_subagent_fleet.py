"""Plan 0 Build 3 (Task 3.4) — SubagentFleet: concurrent child fan-out + collation.

The fleet spawns one child ShadowRuntime per sub-task (bounded by
``config.delegate_max_concurrent_children``), runs them with
``asyncio.gather(..., return_exceptions=True)`` so a single child timeout /
exception never aborts its siblings, and then COLLATES the outcomes.

The key deliverable under test is :meth:`SubagentFleet.collate` — the operator
decision that **partial failure is not total failure**: the synthesis must
honestly state what each successful child produced AND explicitly name what
failed and what is therefore missing.

Every ``ShadowRuntime.execute`` is mocked (``unittest.mock``) so NO real LLM
call happens — children resolve to canned result dicts, raise, or hang
(time out) entirely in-process.
"""
import asyncio

import pytest
from unittest.mock import patch

from sharing_on.config import Config
from systemu.core.models import (
    Activity,
    ActivityStatus,
    Shadow,
    ShadowStatus,
)
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault
from systemu.runtime.subagent_fleet import SubagentFleet


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_vault(tmp_path):
    return Vault(str(tmp_path))


@pytest.fixture
def config(tmp_path):
    """Real Config dataclass (MagicMock configs break getattr defaults)."""
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    return cfg


@pytest.fixture
def parent_shadow():
    return Shadow(
        id=generate_id("shadow"),
        name="Parent Shadow",
        description="A parent orchestrator with delegate access.",
        identity_block="You are a careful parent orchestrator agent.",
        available_tool_ids=["tool_read", "delegate", "tool_write"],
        skill_ids=["skill_research"],
        status=ShadowStatus.ACTIVE,
    )


@pytest.fixture
def parent_activity():
    return Activity(
        id=generate_id("activity"),
        name="Parent Activity",
        scroll_id=generate_id("scroll"),
        required_tool_ids=["tool_read"],
        required_skill_ids=["skill_research"],
        status=ActivityStatus.ASSIGNED,
    )


def _success_result(execution_id, summary, *, tool_calls=1):
    """A canonical ShadowRuntime.execute success result dict (build_result shape)."""
    return {
        "execution_id": execution_id,
        "status": "success",
        "summary": summary,
        "error": None,
        "tool_call_count": tool_calls,   # spec cost signal
        "tool_calls": tool_calls,        # real build_result key (alias)
        "rounds": 1,
    }


# ── Test 1: all children succeed ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_children_succeed(
    config, tmp_vault, parent_shadow, parent_activity
):
    tasks = [
        "Research the revenue figures",
        "Summarise the customer feedback",
        "Draft the executive summary",
    ]

    async def fake_execute(self, shadow, activity, *, origin=None, **kwargs):
        # Distinct summary per child so the synthesis can name each contribution.
        return _success_result(
            getattr(self, "_fleet_eid", "exec_x"),
            f"Produced output for: {activity.name}",
            tool_calls=2,
        )

    fleet = SubagentFleet(
        parent_execution_id="exec_parent",
        config=config,
        vault=tmp_vault,
    )

    with patch(
        "systemu.runtime.shadow_runtime.ShadowRuntime.execute",
        new=fake_execute,
    ):
        out = await fleet.spawn_children(parent_shadow, parent_activity, tasks)

    assert len(out["succeeded"]) == 3
    assert len(out["failed"]) == 0
    assert out["any_succeeded"] is True
    assert out["all_succeeded"] is True
    # Synthesis names all three contributions (by their task text).
    for task in tasks:
        assert task in out["synthesis"], f"synthesis missing task {task!r}"


# ── Test 2: partial failure (1 raises + 1 times out + 1 succeeds) ────────────


@pytest.mark.asyncio
async def test_partial_failure_is_not_total_failure(
    config, tmp_vault, parent_shadow, parent_activity
):
    tasks = [
        "TASK_OK fetch the numbers",       # succeeds
        "TASK_BOOM compute the model",     # raises
        "TASK_SLOW render the chart",      # times out
    ]

    async def fake_execute(self, shadow, activity, *, origin=None, **kwargs):
        # The original task text lives on the child scroll's objective goal, not
        # the activity.name (which the harness sets to "Subtask activity · …").
        task_text = tmp_vault.get_scroll(activity.scroll_id).intent
        if "BOOM" in task_text:
            raise RuntimeError("child blew up: model unavailable")
        if "SLOW" in task_text:
            await asyncio.sleep(10)  # exceeds per_child_timeout → wait_for cancels
            return _success_result("exec_slow", "never reached")
        return _success_result(
            "exec_ok", f"Produced output for: {task_text}", tool_calls=3
        )

    fleet = SubagentFleet(
        parent_execution_id="exec_parent",
        config=config,
        vault=tmp_vault,
    )

    with patch(
        "systemu.runtime.shadow_runtime.ShadowRuntime.execute",
        new=fake_execute,
    ):
        out = await fleet.spawn_children(
            parent_shadow, parent_activity, tasks, per_child_timeout=0.05
        )

    assert len(out["succeeded"]) == 1
    assert len(out["failed"]) == 2
    assert out["all_succeeded"] is False
    assert out["any_succeeded"] is True

    synth = out["synthesis"]
    # Honest: names the one success...
    assert "TASK_OK fetch the numbers" in synth
    # ...AND explicitly names BOTH failures / what is missing.
    assert "TASK_BOOM compute the model" in synth
    assert "TASK_SLOW render the chart" in synth
    # The synthesis must NOT pretend everything failed.
    lowered = synth.lower()
    assert "all" not in lowered or "missing" in lowered  # no blanket "all failed"
    # Missing list captures the two failed tasks.
    assert len(out["missing"]) == 2


# ── Test 3: concurrency is bounded by delegate_max_concurrent_children ────────


@pytest.mark.asyncio
async def test_concurrency_bounded_by_semaphore(
    config, tmp_vault, parent_shadow, parent_activity
):
    config.delegate_max_concurrent_children = 2
    tasks = [f"task-{i}" for i in range(4)]

    state = {"current": 0, "peak": 0}
    lock = asyncio.Lock()

    async def fake_execute(self, shadow, activity, *, origin=None, **kwargs):
        async with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        try:
            # Hold the slot long enough that, if concurrency were unbounded,
            # all 4 would overlap and peak would hit 4.
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                state["current"] -= 1
        return _success_result("exec_c", f"done {activity.name}")

    fleet = SubagentFleet(
        parent_execution_id="exec_parent",
        config=config,
        vault=tmp_vault,
    )

    with patch(
        "systemu.runtime.shadow_runtime.ShadowRuntime.execute",
        new=fake_execute,
    ):
        out = await fleet.spawn_children(parent_shadow, parent_activity, tasks)

    assert out["all_succeeded"] is True
    assert len(out["succeeded"]) == 4
    assert state["peak"] <= 2, f"peak concurrency {state['peak']} exceeded cap 2"


# ── Test 4: empty tasks → graceful empty synthesis ───────────────────────────


@pytest.mark.asyncio
async def test_empty_tasks_graceful(
    config, tmp_vault, parent_shadow, parent_activity
):
    fleet = SubagentFleet(
        parent_execution_id="exec_parent",
        config=config,
        vault=tmp_vault,
    )

    # No patching needed — must short-circuit before any ShadowRuntime is built.
    out = await fleet.spawn_children(parent_shadow, parent_activity, [])

    assert out["succeeded"] == []
    assert out["failed"] == []
    assert out["children"] == []
    assert out["synthesis"] == "No sub-tasks requested."
    assert out["budget"]["tool_call_count"] == 0
    assert out["any_succeeded"] is False
    assert out["all_succeeded"] is False


# ── Test 5: budget sums child tool_call_count ────────────────────────────────


@pytest.mark.asyncio
async def test_budget_sums_tool_call_count(
    config, tmp_vault, parent_shadow, parent_activity
):
    tasks = ["alpha", "beta", "gamma"]
    counts = iter([2, 5, 4])  # total = 11

    async def fake_execute(self, shadow, activity, *, origin=None, **kwargs):
        return _success_result("exec_b", f"done {activity.name}", tool_calls=next(counts))

    fleet = SubagentFleet(
        parent_execution_id="exec_parent",
        config=config,
        vault=tmp_vault,
    )

    with patch(
        "systemu.runtime.shadow_runtime.ShadowRuntime.execute",
        new=fake_execute,
    ):
        out = await fleet.spawn_children(parent_shadow, parent_activity, tasks)

    assert out["budget"]["tool_call_count"] == 11


# ── Test 6: over-cap returns a failure dict naming the cap ────────────────────


@pytest.mark.asyncio
async def test_over_cap_rejected(
    config, tmp_vault, parent_shadow, parent_activity
):
    tasks = [f"task-{i}" for i in range(20)]  # > sane max (8)

    fleet = SubagentFleet(
        parent_execution_id="exec_parent",
        config=config,
        vault=tmp_vault,
    )

    # Must reject BEFORE building any ShadowRuntime (no patch needed).
    out = await fleet.spawn_children(parent_shadow, parent_activity, tasks)

    assert out["all_succeeded"] is False
    assert out["any_succeeded"] is False
    assert out["succeeded"] == []
    # Cap value (8) named in the synthesis/failure text.
    assert "8" in out["synthesis"]


# ── collate() unit test (direct, no spawn) ───────────────────────────────────


def test_collate_direct_partial(config, tmp_vault):
    """collate() handles a hand-built mix of result dicts and exceptions."""
    fleet = SubagentFleet(
        parent_execution_id="exec_parent",
        config=config,
        vault=tmp_vault,
    )
    child_outcomes = [
        {"task": "do A", "result": _success_result("e1", "Did A successfully", tool_calls=3)},
        {"task": "do B", "result": TimeoutError("child timed out")},
        {"task": "do C", "result": {"status": "failure", "summary": "C failed", "error": "boom"}},
    ]
    out = fleet.collate(child_outcomes)
    assert len(out["succeeded"]) == 1
    assert len(out["failed"]) == 2
    assert out["any_succeeded"] is True
    assert out["all_succeeded"] is False
    assert out["budget"]["tool_call_count"] == 3
    assert "do A" in out["synthesis"]
    assert "do B" in out["synthesis"]
    assert "do C" in out["synthesis"]
    assert "do B" in out["missing"] or any("do B" in m for m in out["missing"])
