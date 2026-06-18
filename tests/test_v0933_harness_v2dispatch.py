"""v0.9.33 Section A — v2 built-in tools must dispatch through the v2 dispatcher
in the ShadowRuntime agentic loop (advertised by _build_llm_tool_catalog but,
pre-fix, undispatchable because _handle_tool_call only resolved v1 vault tools).

Test-helper notes (per REVIEW-CORRECTIONS §Section A — verified against source):
  * The real ``ExecutionContext`` ctor is
    ``ExecutionContext(execution_id, system_prompt, scroll_json, tool_index, ...)``
    — there is NO intent/objectives/plan kwarg.
  * ``ctx.observations`` does NOT exist. Observations are ``ExecutionEvent``s in
    ``ctx._history`` (event_type == "observation"); read their ``.content``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from systemu.runtime.shadow_runtime import ShadowRuntime
from systemu.runtime.tool_sandbox import ToolSandbox, ToolResult
from systemu.runtime.context_builder import ExecutionContext
# Force-load the v2 toolsets so registry.get(...) resolves these built-ins.
import systemu.runtime.tools.file_tools  # noqa: F401
import systemu.runtime.tools.delegate  # noqa: F401


class _Cfg:
    """Minimal config stand-in: only the attrs the v2 path reads."""
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.check_fn_cache_ttl_seconds = 30
        self.capability_track_outcomes = False  # skip ledger writes (no vault)


def _make_runtime(tmp_path: Path) -> ShadowRuntime:
    rt = ShadowRuntime.__new__(ShadowRuntime)
    out = tmp_path / "output"
    out.mkdir(parents=True, exist_ok=True)
    cfg = _Cfg(str(out))
    rt.config = cfg
    rt.vault = None
    rt.sandbox = ToolSandbox(vault_root=tmp_path, backend="local",
                             vault=None, config=cfg)
    # per-run bookkeeping _handle_tool_call mutates
    rt._dep_failed_tools = {}
    rt._consec_tool_fails = {}
    rt._fresh_work_since_last_verifier_call = False
    rt._execution_mind = None
    return rt


def _ctx() -> ExecutionContext:
    return ExecutionContext(execution_id="t", system_prompt="",
                            scroll_json=[], tool_index=[])


def _observations(ctx: ExecutionContext):
    """REVIEW-CORRECTIONS §A: observations live in ctx._history, not ctx.observations."""
    return [e.content for e in ctx._history if e.event_type == "observation"]


@pytest.mark.asyncio
async def test_v2_write_file_dispatches_and_writes(tmp_path):
    rt = _make_runtime(tmp_path)
    ctx = _ctx()
    decision = {
        "decision": "TOOL_CALL",
        "tool_name": "write_file",
        "parameters": {"path": "report.txt", "content": "hello-v2"},
        "reasoning": "write the deliverable",
    }
    result = await rt._handle_tool_call(decision, tools=[], context=ctx,
                                        current_ab=0, dry_run=False)

    # It must NOT be the "not found" None return.
    assert result is not None, "v2 tool returned None (treated as not-found)"
    assert isinstance(result, ToolResult)
    assert result.success is True, result.parsed
    # The handler actually ran and wrote into the run workspace.
    written = tmp_path / "output" / "report.txt"
    assert written.exists(), f"file not written; observed parsed={result.parsed}"
    assert written.read_text(encoding="utf-8") == "hello-v2"
    # The call + observation were recorded like the v1 path.
    assert _observations(ctx), "no observation recorded for v2 call"
    # v0.9.33 A1: the written deliverable must register in files_produced
    # CWD-INDEPENDENTLY. The param path is relative ("report.txt"); only works
    # if the handler echoes the resolved ABSOLUTE path (collect_artifact_paths
    # resolves relative params against the process CWD, not output_dir, so a
    # relative-only param would be lost whenever CWD != output_dir — the normal
    # daemon/local-backend case). This mirrors how v1 file_write echoes `path`.
    assert ctx.files_produced, "v2 write not recorded in files_produced (A1)"
    assert any(Path(p).resolve() == written.resolve() for p in ctx.files_produced), \
        f"written file absent from files_produced: {ctx.files_produced}"


class _FakeToolType:
    value = "python"


class _V1Tool:
    """Minimal v1 Tool stand-in for the no-regression check."""
    def __init__(self):
        self.id = "tool_v1"
        self.name = "legacy_v1_tool"
        self.description = "a v1 vault tool"
        self.implementation_path = "tools/implementations/legacy_v1_tool.py"
        self.dependencies = []
        self.tool_type = _FakeToolType()
        self.status = "DEPLOYED"
        self.max_result_size_chars = None


@pytest.mark.asyncio
async def test_unknown_tool_still_not_found(tmp_path):
    rt = _make_runtime(tmp_path)
    ctx = _ctx()
    decision = {"decision": "TOOL_CALL", "tool_name": "no_such_tool_xyz",
                "parameters": {}, "reasoning": "r"}
    result = await rt._handle_tool_call(decision, tools=[], context=ctx,
                                        current_ab=0, dry_run=False)
    assert result is None, "unknown tool must hit the not-found None path"
    assert any("not found" in str(o).lower() for o in _observations(ctx))


@pytest.mark.asyncio
async def test_v1_vault_tool_still_dispatches_via_execute_tool(tmp_path, monkeypatch):
    rt = _make_runtime(tmp_path)
    ctx = _ctx()
    tool = _V1Tool()

    calls = {}

    async def _fake_execute_tool(impl_path, params, **kw):
        calls["impl_path"] = impl_path
        calls["params"] = params
        return ToolResult(success=True, parsed={"success": True, "ok": 1})

    async def _fail_v2_execute(*a, **k):  # the v2 path must NOT be taken
        raise AssertionError("v1 tool wrongly routed through v2 execute()")

    monkeypatch.setattr(rt.sandbox, "execute_tool", _fake_execute_tool)
    monkeypatch.setattr(rt.sandbox, "execute", _fail_v2_execute)
    # _after_successful_call / capability ledger are best-effort; stub the
    # success-side hooks that touch a vault we don't have.
    monkeypatch.setattr(rt.sandbox, "_after_successful_call", lambda **kw: None)
    monkeypatch.setattr(rt.sandbox, "_record_capability_outcome", lambda **kw: None)

    decision = {"decision": "TOOL_CALL", "tool_name": "legacy_v1_tool",
                "parameters": {"x": 1}, "reasoning": "r"}
    result = await rt._handle_tool_call(decision, tools=[tool], context=ctx,
                                        current_ab=0, dry_run=False)
    assert result is not None and result.success is True
    assert calls["impl_path"] == tool.implementation_path
    assert calls["params"] == {"x": 1}


@pytest.mark.asyncio
async def test_v2_dry_run_does_not_execute_handler(tmp_path):
    rt = _make_runtime(tmp_path)
    ctx = _ctx()
    decision = {"decision": "TOOL_CALL", "tool_name": "write_file",
                "parameters": {"path": "should_not_exist.txt", "content": "x"},
                "reasoning": "r"}
    result = await rt._handle_tool_call(decision, tools=[], context=ctx,
                                        current_ab=0, dry_run=True)
    assert result is not None and result.success is True
    assert result.parsed.get("dry_run") is True
    # The handler must NOT have run.
    assert not (tmp_path / "output" / "should_not_exist.txt").exists()


def test_shell_tools_are_not_v2_registered():
    """The v0.9.32 command gate owns shell tools via execute_tool. They must
    NOT be in the v2 registry, or the new short-circuit would bypass the gate."""
    from systemu.runtime.tool_registry_v2 import registry as _v2
    from systemu.runtime.tool_sandbox import _SHELL_TOOL_NAMES
    for name in _SHELL_TOOL_NAMES:
        assert _v2.get(name) is None, (
            f"{name!r} is v2-registered — the v2 short-circuit would bypass "
            f"the v0.9.32 _maybe_gate_command shell gate")


@pytest.mark.asyncio
async def test_v2_spawn_subagent_dispatches_via_short_circuit(tmp_path, monkeypatch):
    """REVIEW-CORRECTIONS §A: a heavier v2 tool (spawn_subagent) dispatches via
    the new short-circuit with llm_call_json monkeypatched (returns a ToolResult,
    not None). These v2 tools become live post-fix — intended, and orthogonal to
    Section B's fleet path."""
    # The registered handler (_delegate_handler) builds Config.from_env() and
    # calls llm_call_json; stub the LLM so no network/key is needed.
    import systemu.runtime.tools.delegate as _delegate
    monkeypatch.setattr(
        _delegate, "llm_call_json",
        lambda **kw: {"summary": "did the thing", "key_findings": ["a", "b"]},
    )
    rt = _make_runtime(tmp_path)
    ctx = _ctx()
    decision = {"decision": "TOOL_CALL", "tool_name": "spawn_subagent",
                "parameters": {"task": "summarize X"}, "reasoning": "delegate"}
    result = await rt._handle_tool_call(decision, tools=[], context=ctx,
                                        current_ab=0, dry_run=False)
    assert result is not None, "spawn_subagent returned None (treated as not-found)"
    assert isinstance(result, ToolResult)
    assert result.success is True, result.parsed
    assert result.parsed.get("summary") == "did the thing"
    assert _observations(ctx), "no observation recorded for the v2 spawn_subagent call"
