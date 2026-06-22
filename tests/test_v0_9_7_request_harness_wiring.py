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
    # an ESCALATE surfaces an operator decision card
    assert "surface_harness_request" in src
    # the materialised grant is applied via the shared helper (extracted so the
    # deferred harness grant-resume replays the same code, byte-identical).
    assert "self._apply_materialised_grant(" in src
    # the apply-helper offers the granted tool back + deploys it inline so it's
    # callable this run
    apply_src = inspect.getsource(ShadowRuntime._apply_materialised_grant)
    assert "harness_granted" in apply_src
    assert "deploy_forged_tool" in apply_src


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


def test_prompt_advertises_mcp_as_requestable_kind():
    """The MCP connect-as-capability backend ships + is gated, but the agent
    only emits a kind it has been told exists. The execute_step kind set must
    name `mcp` so a Shadow can actually propose an MCP-connect recipe (Feature A.1)."""
    p = Path("systemu/prompts/execute_step.md").read_text(encoding="utf-8")
    assert "`mcp`" in p
    # all five pre-existing kinds must remain (no regression in the kind set)
    for kind in ("`tool`", "`skill`", "`access`", "`compute`", "`subagent`"):
        assert kind in p, f"kind {kind} dropped from execute_step.md"


def test_prompt_describes_mcp_connect_spec_shape_and_approval():
    """The agent needs the spec field names to propose a valid connect recipe,
    and must be told a NEW server requires operator approval (Feature A.2)."""
    p = Path("systemu/prompts/execute_step.md").read_text(encoding="utf-8")
    for field in (
        "server_id", "transport", "command", "args",
        "env_keys", "url", "tool_filter",
    ):
        assert field in p, f"MCP spec field {field} not documented in prompt"
    assert "stdio" in p and "http" in p and "sse" in p
    assert "operator approval" in p
    # secrets travel as env-key NAMES, never values
    assert "env_keys" in p and "names" in p.lower()


def test_prompt_documents_compute_extra_iterations():
    """Bug 7 (prompt-doc half, folded into Feature A): the agent must know the
    compute request can carry extra_iterations / extra_think so a well-specified
    request is possible (the code also defaults a non-zero bump when omitted)."""
    p = Path("systemu/prompts/execute_step.md").read_text(encoding="utf-8")
    assert "extra_iterations" in p and "extra_think" in p


def test_prompt_has_honesty_disposition_against_fabrication():
    """v0.9.34.2: the executor prompt must set a no-fabrication disposition — the
    agent uses/acquires real capabilities rather than inventing or approximating
    a result it cannot genuinely produce (e.g. a guessed SHA-256). The disposition
    is deliberately GENERIC (names no task/operation/REQUEST_HARNESS action)."""
    p = Path("systemu/prompts/execute_step.md").read_text(encoding="utf-8")
    assert "## How you work" in p
    assert "careful, honest agent" in p
    assert "not by guessing" in p
    assert "acquiring the rest, is the job" in p


def test_judge_prompt_kind_list_names_mcp():
    """The harness judge may receive an MCP request; its documented kind list
    must name mcp explicitly so the closed kind set stays consistent across every
    prompt that enumerates kinds (Feature A.3)."""
    p = Path("systemu/prompts/harness_judge.md").read_text(encoding="utf-8")
    assert "mcp" in p
    for kind in ("skill", "access", "compute", "subagent"):
        assert kind in p
