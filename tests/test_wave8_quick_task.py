"""W8.2 — the quick-lane ReAct executor.

The factory pipeline (refine → approve → extract → decide → persona → execute)
costs ~70s of meta-work before the first useful action — wrong default for
one-shot asks. `run_quick_task` is a bounded ReAct loop over the Gate-3-enabled
vault tools, executing through the SAME ToolSandbox path as the runtime
(Wave-6 runner, truth-in-results). All tests run with an injected fake LLM and
a real temp vault — no network, no API key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    for sub in ["tools/implementations", "elder", "notifications"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _add_tool(vault: Vault, name: str, body: str, *, enabled: bool = True,
              status: ToolStatus = ToolStatus.DEPLOYED) -> Tool:
    """Register a module-style tool with a real implementation file."""
    impl = Path(vault.root) / "tools" / "implementations" / f"{name}.py"
    impl.write_text(body, encoding="utf-8")
    tool = Tool(
        id=generate_id("tool"), name=name, description=f"test tool {name}",
        tool_type=ToolType.PYTHON_FUNCTION, status=status, enabled=enabled,
        implementation_path=str(impl),
        forged_by_systemu=True,   # exercises the W6 subprocess runner path
        parameter_names=["x"],
    )
    vault.save_tool(tool)
    return tool


_ECHO_BODY = (
    "TOOL_META = {'name': 'echo_tool'}\n"
    "def run(**kwargs):\n"
    "    return {'success': True, 'data': {'echo': kwargs.get('x')}, 'error': None}\n"
)

_FAIL_BODY = (
    "def run(**kwargs):\n"
    "    return {'success': False, 'data': None, 'error': 'always broken'}\n"
)


def _fake_llm(script):
    """Return an llm_json callable that replays `script` (list of action dicts)
    and records every user payload it was shown."""
    calls = {"payloads": [], "i": 0}

    def llm_json(*, system, user, config=None):
        calls["payloads"].append(user)
        action = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return action

    llm_json.calls = calls
    return llm_json


class TestQuickLoop:
    def test_answer_on_first_turn(self, vault):
        from systemu.pipelines.quick_task import run_quick_task
        llm = _fake_llm([{"action": "ANSWER", "answer_md": "**42**"}])
        res = run_quick_task("what is 6x7", None, vault, llm_json=llm)
        assert res.status == "success"
        assert res.answer_md == "**42**"
        assert res.tool_calls == 0 and res.iterations == 1

    def test_tool_call_really_executes_and_feeds_the_transcript(self, vault):
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "echo_tool", _ECHO_BODY)
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "echo_tool",
             "params": {"x": "spa-data"}, "reasoning": "need the echo"},
            {"action": "ANSWER", "answer_md": "echoed"},
        ])
        res = run_quick_task("echo me", None, vault, llm_json=llm)
        assert res.status == "success" and res.tool_calls == 1
        # The REAL parsed payload (through the W6 subprocess runner) must have
        # reached the second LLM turn — this is the anti-no-op assertion.
        second_payload = llm.calls["payloads"][1]
        assert "spa-data" in second_payload

    def test_disabled_tool_is_invisible_and_uncallable(self, vault):
        """Gate-3: the quick lane can never call a tool the operator hasn't
        enabled — not in the index, and a call to it is an error observation."""
        from systemu.pipelines.quick_task import run_quick_task, _enabled_tool_records
        _add_tool(vault, "echo_tool", _ECHO_BODY, enabled=False)
        assert _enabled_tool_records(vault) == []
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "echo_tool", "params": {"x": "hi"}},
            {"action": "ANSWER", "answer_md": "done"},
        ])
        res = run_quick_task("echo me", None, vault, llm_json=llm)
        assert res.status == "success"
        assert res.tool_calls == 0, "disabled tool must not execute"
        assert "unknown or disabled" in llm.calls["payloads"][1]

    def test_proposed_tool_not_runtime_ready_is_excluded(self, vault):
        from systemu.pipelines.quick_task import _enabled_tool_records
        _add_tool(vault, "echo_tool", _ECHO_BODY, status=ToolStatus.PROPOSED)
        assert _enabled_tool_records(vault) == []

    def test_iteration_cap_fails_honestly(self, vault):
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "echo_tool", _ECHO_BODY)
        llm = _fake_llm([{"action": "TOOL_CALL", "tool": "echo_tool",
                          "params": {"x": "again"}}])
        res = run_quick_task("loop forever", None, vault, llm_json=llm, max_iters=3)
        assert res.status == "failed"
        assert "iteration" in (res.error or "").lower()
        assert res.iterations == 3

    def test_same_tool_failure_streak_ends_the_run(self, vault):
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "broken_tool", _FAIL_BODY)
        llm = _fake_llm([{"action": "TOOL_CALL", "tool": "broken_tool",
                          "params": {"x": "1"}}])
        res = run_quick_task("use the broken tool", None, vault, llm_json=llm)
        assert res.status == "failed"
        assert "broken_tool" in (res.error or "")
        assert res.iterations <= 4   # 3 failures + bail, not 12 silent loops

    def test_ask_user_returns_needs_input(self, vault):
        from systemu.pipelines.quick_task import run_quick_task
        llm = _fake_llm([{"action": "ASK_USER",
                          "question": "Which city are you in?"}])
        res = run_quick_task("find a spa", None, vault, llm_json=llm)
        assert res.status == "needs_input"
        assert res.question == "Which city are you in?"

    def test_malformed_actions_twice_fail_honestly(self, vault):
        from systemu.pipelines.quick_task import run_quick_task
        llm = _fake_llm([{"nonsense": True}, {"also": "nonsense"}])
        res = run_quick_task("hello", None, vault, llm_json=llm)
        assert res.status == "failed"

    _WRITER_BODY = (
        "from pathlib import Path\n"
        "def run(**kwargs):\n"
        "    p = Path(kwargs['file_path'])\n"
        "    p.write_text(kwargs.get('content',''), encoding='utf-8')\n"
        "    return {'success': True, 'error': None}\n"
    )

    def test_artifacts_collected_from_real_write(self, vault, tmp_path):
        from types import SimpleNamespace
        from systemu.pipelines.quick_task import run_quick_task
        out_dir = tmp_path / "deliverables"
        out = out_dir / "result.txt"
        _add_tool(vault, "write_note", self._WRITER_BODY)
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "write_note",
             "params": {"file_path": str(out), "content": "spa list"}},
            {"action": "ANSWER", "answer_md": "saved"},
        ])
        cfg = SimpleNamespace(output_dir=str(out_dir))
        res = run_quick_task("save my note", cfg, vault, llm_json=llm)
        assert res.status == "success"
        assert res.files_produced == [str(out.resolve())]

    def test_out_of_bounds_write_redirects_into_output_dir(self, vault, tmp_path):
        """The sandbox redirects write paths outside output_dir (deliverables
        contract). The quick lane must (a) pre-create the dir so the redirect
        doesn't fail the write, and (b) report the EFFECTIVE artifact path."""
        from types import SimpleNamespace
        from systemu.pipelines.quick_task import run_quick_task
        out_dir = tmp_path / "deliverables"     # does NOT exist yet
        _add_tool(vault, "write_note", self._WRITER_BODY)
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "write_note",
             "params": {"file_path": "C:/definitely/elsewhere/note.txt",
                        "content": "spa list"}},
            {"action": "ANSWER", "answer_md": "saved"},
        ])
        cfg = SimpleNamespace(output_dir=str(out_dir))
        res = run_quick_task("save my note", cfg, vault, llm_json=llm)
        assert res.status == "success"
        expected = (out_dir / "note.txt").resolve()
        assert expected.read_text(encoding="utf-8") == "spa list"
        assert res.files_produced == [str(expected)]


class TestSubmitQuickTask:
    def test_chat_history_contract(self, vault):
        """submit_quick_task writes the same field contract direct_task uses,
        so the Status dropdown / chat history render with zero changes."""
        from systemu.pipelines.quick_task import submit_quick_task
        llm = _fake_llm([{"action": "ANSWER", "answer_md": "## the answer"}])
        res = submit_quick_task("quick q", None, vault, llm_json=llm)
        assert res.status == "success"
        entries = vault.load_chat_history(limit=5)
        assert len(entries) == 1
        e = entries[0]
        assert e["prompt"] == "quick q"
        assert e["status"] == "success"
        assert e["summary"] == "## the answer"
        assert e["lane"] == "quick"
        assert e["files_produced"] == []
