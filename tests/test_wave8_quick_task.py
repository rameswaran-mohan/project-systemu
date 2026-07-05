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
    """Register a module-style tool with a real implementation file.

    S1b: the live per-tool action gate (``ToolSandbox._maybe_gate_tool``)
    treats an UNTAGGED tool as UNKNOWN effect ⇒ REQUIRE_APPROVAL — exactly
    what the forge pipeline avoids by backfilling ``effect_tags`` from
    ``classify_source`` at forge time. Mirror that here so a test tool is
    classified the SAME way a real forged tool would be, instead of being
    unrealistically untagged.
    """
    from systemu.runtime.effect_tags import classify_source

    impl = Path(vault.root) / "tools" / "implementations" / f"{name}.py"
    impl.write_text(body, encoding="utf-8")
    effect_tags = sorted(t.value for t in classify_source(body))
    tool = Tool(
        id=generate_id("tool"), name=name, description=f"test tool {name}",
        tool_type=ToolType.PYTHON_FUNCTION, status=status, enabled=enabled,
        implementation_path=str(impl),
        forged_by_systemu=True,   # exercises the W6 subprocess runner path
        parameter_names=["x"],
        effect_tags=effect_tags,
    )
    vault.save_tool(tool)
    return tool


def _pre_approve_tool(tool: Tool, tmp_path: Path):
    """Bless a tool signature the way an operator's "Always allow" would,
    for a test tool that ``classify_source`` cannot resolve (empty body /
    no recognizable I/O sink ⇒ UNKNOWN ⇒ REQUIRE_APPROVAL — a legitimate
    gate, not a test-tagging gap). Computes the signature EXACTLY the way
    ``ToolSandbox._maybe_gate_tool`` does (name + body sha1 + sorted
    effect_tags + host_class=""), then returns a ``ToolSandbox`` wired to
    the pre-populated store so ``run_quick_task(..., sandbox=...)`` picks
    it up instead of the default ``data/`` store."""
    import hashlib

    from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature
    from systemu.runtime.tool_sandbox import ToolSandbox

    body_hash = hashlib.sha1(Path(tool.implementation_path).read_bytes()).hexdigest()
    sig = tool_signature(tool.name, body_hash, set(tool.effect_tags or []),
                         host_class="")
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    store.approve(sig, command=tool.name)
    return store


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

    def test_iteration_cap_salvages_partial_from_gathered_data(self, vault, tmp_path):
        """RCA 2026-06-13: the loop gathered real data (web_read → a restaurant)
        but never emitted ANSWER, then DROPPED it on budget exhaustion. It must
        now synthesize an honest PARTIAL from the gathered observation — the
        answer was sitting in history — not return a bare 'failed'."""
        from systemu.pipelines.quick_task import run_quick_task
        from systemu.runtime.tool_sandbox import ToolSandbox
        tool = _add_tool(vault, "echo_tool", _ECHO_BODY)
        # echo_tool's body has no recognizable I/O sink (it just returns a
        # literal dict) -> classify_source legitimately yields {} -> UNKNOWN
        # -> REQUIRE_APPROVAL. That's a correctly-gated tool, not a tagging
        # gap, so bless it the way an operator's "Always allow" would.
        store = _pre_approve_tool(tool, tmp_path)
        sandbox = ToolSandbox(vault.root, vault=vault, command_approvals=store)
        llm = _fake_llm([{"action": "TOOL_CALL", "tool": "echo_tool",
                          "params": {"x": "Lassiwala Dhaba 4.0 stars"}}])
        synth_calls = {"n": 0}

        def fake_synth(prompt, history, config):
            synth_calls["n"] += 1
            assert any("Lassiwala Dhaba" in str(h) for h in history)  # grounded
            return "Best match: **Lassiwala Dhaba** (4.0/5)."

        res = run_quick_task("best punjabi restaurant near me", None, vault,
                             llm_json=llm, max_iters=3, synthesize=fake_synth,
                             sandbox=sandbox)
        assert res.status == "partial"
        assert "Lassiwala Dhaba" in res.answer_md
        assert synth_calls["n"] == 1
        assert res.iterations == 3
        assert "iteration" in (res.error or "").lower()   # reason retained

    def test_no_salvage_when_nothing_usable_gathered(self, vault):
        """The anti-hallucination floor: all-failed history has nothing usable,
        so synthesis must NOT fire and the run stays an honest 'failed'."""
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "broken_tool", _FAIL_BODY)
        called = {"n": 0}

        def fake_synth(prompt, history, config):
            called["n"] += 1
            return "should never be called"

        llm = _fake_llm([{"action": "TOOL_CALL", "tool": "broken_tool",
                          "params": {"x": "1"}}])
        res = run_quick_task("use broken", None, vault, llm_json=llm,
                             synthesize=fake_synth)
        assert res.status == "failed"
        assert called["n"] == 0

    def test_same_tool_failure_streak_ends_the_run(self, vault):
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "broken_tool", _FAIL_BODY)
        llm = _fake_llm([{"action": "TOOL_CALL", "tool": "broken_tool",
                          "params": {"x": "1"}}])
        res = run_quick_task("use the broken tool", None, vault, llm_json=llm)
        assert res.status == "failed"
        assert "broken_tool" in (res.error or "")
        assert res.iterations <= 4   # 3 failures + bail, not 12 silent loops

    def test_repeated_failing_call_is_blocked_but_new_params_allowed(self, vault):
        """Step 1 == step 12 in the live RCA: a (tool, params) that already
        failed is refused with an actionable nudge and NOT re-executed; a
        DIFFERENT call to the same tool is still allowed."""
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "broken_tool", _FAIL_BODY)
        _add_tool(vault, "echo_tool", _ECHO_BODY)
        bad = {"action": "TOOL_CALL", "tool": "broken_tool", "params": {"x": "1"}}
        llm = _fake_llm([
            bad,                                                       # executes, fails
            bad,                                                       # BLOCKED (same sig)
            {"action": "TOOL_CALL", "tool": "echo_tool", "params": {"x": "ok"}},  # allowed
            {"action": "ANSWER", "answer_md": "done", "completed": True},
        ])
        res = run_quick_task("repeat then pivot", None, vault, llm_json=llm)
        assert res.status == "success"
        assert res.tool_calls == 2          # broken once + echo once; the repeat was blocked
        assert any("already failed" in p for p in llm.calls["payloads"])

    def test_plan_is_recorded_and_echoed_to_later_turns(self, vault):
        """Plan-first: a non-trivial task emits a PLAN; the plan must then ride
        in the payload of subsequent turns so execution follows it."""
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "echo_tool", _ECHO_BODY)
        llm = _fake_llm([
            {"action": "PLAN",
             "steps": ["find restaurants near santhoshpuram", "rank by rating", "answer"],
             "reasoning": "decompose first"},
            {"action": "TOOL_CALL", "tool": "echo_tool", "params": {"x": "go"}},
            {"action": "ANSWER", "answer_md": "done", "completed": True},
        ])
        res = run_quick_task("best restaurant near me", None, vault, llm_json=llm)
        assert res.status == "success"
        assert res.tool_calls == 1
        # the plan reaches the turns AFTER planning
        assert any("rank by rating" in p for p in llm.calls["payloads"][1:])

    def test_last_action_turn_payload_nudges_answer(self, vault):
        """Point 2: the final action turn must carry an explicit answer-now
        signal (the harvest backstop still catches it if the model ignores it)."""
        from systemu.pipelines.quick_task import run_quick_task
        _add_tool(vault, "echo_tool", _ECHO_BODY)
        llm = _fake_llm([{"action": "TOOL_CALL", "tool": "echo_tool", "params": {"x": "1"}}])
        run_quick_task("x", None, vault, llm_json=llm, max_iters=2,
                       synthesize=lambda *a, **k: "partial")
        assert any('"final_turn": true' in p for p in llm.calls["payloads"])

    def test_ask_user_returns_needs_input(self, vault):
        # No chat surface (default) -> historical needs_input terminal preserved.
        from systemu.pipelines.quick_task import run_quick_task
        llm = _fake_llm([{"action": "ASK_USER",
                          "question": "Which city are you in?"}])
        res = run_quick_task("find a spa", None, vault, llm_json=llm)
        assert res.status == "needs_input"
        assert res.question == "Which city are you in?"

    def test_ask_user_resumes_with_operator_answer(self, vault, monkeypatch):
        """v0.9.43: with a chat surface, ASK_USER PARKS, the operator's answer is
        injected as a tool_result, and the SAME loop continues to a real answer."""
        import systemu.pipelines.quick_task as qt
        monkeypatch.setattr(qt, "_ask_operator_inline", lambda *a, **k: "blue")
        llm = _fake_llm([
            {"action": "ASK_USER", "question": "What color?"},
            {"action": "ANSWER", "answer_md": "The color is blue.",
             "completed": True},
        ])
        res = qt.run_quick_task("name the color", None, vault, llm_json=llm,
                                chat_surface=True)
        assert res.status == "success"
        assert "blue" in res.answer_md
        # the operator's answer reached the model as an ask_user tool_result
        assert "ask_user" in llm.calls["payloads"][1]
        assert "blue" in llm.calls["payloads"][1]

    def test_ask_user_decline_falls_back_to_needs_input(self, vault, monkeypatch):
        """v0.9.43: decline / timeout / cancel -> honest needs_input terminal."""
        import systemu.pipelines.quick_task as qt
        monkeypatch.setattr(qt, "_ask_operator_inline", lambda *a, **k: None)
        llm = _fake_llm([{"action": "ASK_USER", "question": "Which file?"}])
        res = qt.run_quick_task("summarize the file", None, vault, llm_json=llm,
                                chat_surface=True)
        assert res.status == "needs_input"

    def test_ask_user_cap_stops_reask_loop(self, vault, monkeypatch):
        """v0.9.43: a model that keeps re-asking is capped (no budget burn)."""
        import systemu.pipelines.quick_task as qt
        monkeypatch.setattr(qt, "_ask_operator_inline", lambda *a, **k: "again")
        llm = _fake_llm([{"action": "ASK_USER", "question": "Huh?"}])  # always asks
        res = qt.run_quick_task("do it", None, vault, llm_json=llm,
                                chat_surface=True, max_iters=12,
                                synthesize=lambda *a, **k: "summary")
        assert res.status in ("failed", "partial")
        assert res.iterations <= qt._ASK_USER_CAP + 1

    def test_ask_operator_inline_posts_and_parses_answer(self, vault):
        """v0.9.43: the helper posts a free-text structured_question and reads
        back the operator's typed answer (parsed from the structured JSON)."""
        import json as _json
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.pipelines.quick_task import _ask_operator_inline
        q = OperatorDecisionQueue(vault)
        dk = "quick_ask:test:1"
        dec_id = q.post(
            title="What word?", body="Answer to continue.", options=["Submit"],
            context={"kind": "structured_question",
                     "questions": [{"id": "answer", "prompt": "What word?",
                                    "options": [], "allow_free_text": True}]},
            dedup_key=dk)
        q.resolve(dec_id, choice=_json.dumps({"answer": "Paris"}))
        assert _ask_operator_inline(vault, "What word?", dedup_key=dk) == "Paris"

    def test_ask_operator_inline_empty_answer_is_decline(self, vault):
        """An empty typed answer reads as a decline (None)."""
        import json as _json
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.pipelines.quick_task import _ask_operator_inline
        q = OperatorDecisionQueue(vault)
        dk = "quick_ask:test:2"
        dec_id = q.post(
            title="q", body="b", options=["Submit"],
            context={"kind": "structured_question",
                     "questions": [{"id": "answer", "prompt": "q",
                                    "options": [], "allow_free_text": True}]},
            dedup_key=dk)
        q.resolve(dec_id, choice=_json.dumps({"answer": ""}))
        assert _ask_operator_inline(vault, "q", dedup_key=dk) is None

    def test_ask_operator_inline_cancel_expires_pending(self, vault):
        """v0.9.43: an abandoned ask (cancel / timeout) is EXPIRED — it must NOT
        linger pending in the 'Needs you' rail/badge."""
        import threading
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.pipelines.quick_task import _ask_operator_inline
        q = OperatorDecisionQueue(vault)
        dk = "quick_ask:test:cancel"
        ev = threading.Event()
        ev.set()   # pre-cancelled -> the helper posts then immediately abandons
        assert _ask_operator_inline(vault, "What word?", dedup_key=dk,
                                    cancel_event=ev) is None
        # the posted decision was expired, so it is no longer pending
        assert all(d.dedup_key != dk for d in q.list_pending())

    def test_ask_operator_inline_whitespace_is_decline(self, vault):
        """v0.9.43: a whitespace-only typed answer reads as a decline (None)."""
        import json as _json
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.pipelines.quick_task import _ask_operator_inline
        q = OperatorDecisionQueue(vault)
        dk = "quick_ask:test:ws"
        dec_id = q.post(
            title="q", body="b", options=["Submit"],
            context={"kind": "structured_question",
                     "questions": [{"id": "answer", "prompt": "q",
                                    "options": [], "allow_free_text": True}]},
            dedup_key=dk)
        q.resolve(dec_id, choice=_json.dumps({"answer": "   "}))
        assert _ask_operator_inline(vault, "q", dedup_key=dk) is None

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


def test_has_usable_observation_gate():
    """The salvage gate: only a successful result with real content counts.
    A success with an empty payload (web_search 200 / 0 hits) does NOT."""
    from systemu.pipelines.quick_task import _has_usable_observation
    assert _has_usable_observation(
        [{"role": "tool_result", "success": True,
          "parsed": '{"results": [{"title": "x"}]}'}]) is True
    assert _has_usable_observation(
        [{"role": "tool_result", "success": True, "parsed": '{"results": []}'}]) is False
    assert _has_usable_observation(
        [{"role": "tool_result", "success": False, "error": "boom"}]) is False
    assert _has_usable_observation([]) is False
    # a non-dict text payload with real characters counts
    assert _has_usable_observation(
        [{"role": "tool_result", "success": True, "parsed": "Lassiwala Dhaba 4.0"}]) is True
