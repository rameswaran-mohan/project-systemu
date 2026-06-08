"""v0.9.7 Phase 1.5 — REQUEST_HARNESS / ASK_OPERATOR must be wired into the loop
(behind SYSTEMU_INTENT_ENGINE) and routed through the Governor. getsource guards
(the Governor itself is behaviourally tested in test_v0_9_7_governor.py)."""
import inspect
from pathlib import Path


def test_execute_handles_request_harness_via_governor():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert 'action in ("REQUEST_HARNESS", "ASK_OPERATOR")' in src
    assert "from systemu.runtime.governor import Governor" in src
    assert ".arbitrate(" in src and ".materialise(" in src
    # flag-off path must short-circuit gracefully (no fall-through)
    assert "harness_disabled" in src
    # granted tool is offered back to the executor
    assert "harness_granted" in src
    # a freshly-forged grant is deployed inline so it's callable this run
    assert "deploy_forged_tool" in src
    # an ESCALATE surfaces an operator decision card
    assert "surface_harness_request" in src


def test_request_harness_branch_is_flag_gated():
    """When the intent engine is off, the branch must not invoke the Governor."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    # the disabled-guard must appear BEFORE the branch actually uses the Governor
    # (a per-execution Governor is hoisted earlier in execute(); assert on the
    # branch's arbitrate usage, not the import).
    i_disabled = src.find("harness_disabled")
    i_gov_use = src.find("_gov = governor or Governor")
    assert 0 < i_disabled < i_gov_use


def test_prompt_advertises_request_harness_and_ask_operator():
    p = Path("systemu/prompts/execute_step.md").read_text(encoding="utf-8")
    assert "REQUEST_HARNESS" in p and "ASK_OPERATOR" in p
    assert "only when capability provisioning is enabled" in p
