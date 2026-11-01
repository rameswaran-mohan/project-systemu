"""No durable writer persists a raw tool-call parameter or prompt value (DEC-31).

Each fenced writer is driven through its real call path and the bytes it left on
disk are read back:

  * the quick lane's event log (``notifications.log_event`` -> event_log.jsonl),
  * the workflow lane's execute subprocess bridge (``install_bridge_writer``),
    its prompt transcript (``llm_transcript.append_call``) and its dry-run replay
    buffer (``record_successful_params`` -> the tool record), all from ONE real
    ``ShadowRuntime.execute`` run,
  * the resume snapshot (``capture_from_context`` -> ``write_snapshot``), with
    resume shown to still get what it reads.

Fixture rule (DEC-31): every secret is LONGER than every cap on its path, and
the JWTs are built so that the cap-length PREFIX is not recognisable -- a
truncate-then-mask writer leaks that prefix and these tests see it. Each path
gets a JWT-shaped, an ``sk-``-shaped and a shapeless value under a secret-NAMED
key.

DEC-27: a value whose redaction raised is written as ``[unredactable]``, never
raw -- pinned per writer below.

The structural half (``tools/lint_durable_writers.py`` + its committed
allowlist) is pinned at the bottom.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from systemu.runtime.durable_redact import UNREDACTABLE
from tools.redaction_fixtures import (caps_on, secret_longer_than,
                                      truncation_blind_secret)

# ── fixtures sized against the live caps ─────────────────────────────────────

#: quick lane: the run-end message clips the prompt at 80, the summary at 1000,
#: a failed call's error at 80.
_QUICK_CAPS = (80, 1000)
#: workflow lane: transcript entry cap (live), the iteration event's tool result
#: (4000), the history slice's nested-result preview (400) and thought (300).
_WORKFLOW_CAPS = (max(caps_on("llm_transcript").values()), 4000, 400, 300, 80)

_Q_JWT = truncation_blind_secret(*_QUICK_CAPS)
_Q_SK = secret_longer_than(*_QUICK_CAPS, kind="sk")
_W_JWT = truncation_blind_secret(*_WORKFLOW_CAPS)
_W_SK = secret_longer_than(*_WORKFLOW_CAPS, kind="sk")
#: no shape at all: only the secret-NAMED key it sits under can catch it.
_KEYED = "Zz9-" * 120


def _assert_absent(blob: str, secret: str) -> None:
    """Not the secret, and no 40-char window of it either -- a truncated head is
    a leak too."""
    assert secret not in blob
    for i in range(0, max(1, len(secret) - 40), 40):
        assert secret[i:i + 40] not in blob, f"window at {i} leaked: {secret[i:i + 40]!r}"


def _all_absent(blob: str, *secrets: str) -> None:
    for s in secrets:
        _assert_absent(blob, s)


def _jsonl(path: Path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def test_fixtures_defeat_every_cap_on_their_path():
    from tools.redaction_fixtures import _detected
    for jwt, caps in ((_Q_JWT, _QUICK_CAPS), (_W_JWT, _WORKFLOW_CAPS)):
        for cap in caps:
            assert len(jwt) > cap
            assert not _detected(jwt[:cap]), f"a {cap}-char prefix is still caught"
    assert len(_W_SK) > max(_WORKFLOW_CAPS) and len(_Q_SK) > max(_QUICK_CAPS)


# ═════════════════════════════════════════════════════════════════════════════
#  1. the event log -- quick lane, real loop
# ═════════════════════════════════════════════════════════════════════════════

_ECHO = (
    "TOOL_META = {'name': 'echo_tool'}\n"
    "def run(**kwargs):\n"
    "    return {'success': True, 'data': {'echo': kwargs.get('x'),\n"
    "            'note': kwargs.get('note')}, 'error': None}\n"
)
_FAIL = (
    "TOOL_META = {'name': 'fail_tool'}\n"
    "def run(**kwargs):\n"
    "    return {'success': False, 'data': None,\n"
    "            'error': 'rejected credential ' + str(kwargs.get('x'))}\n"
)


def _quick_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ["tools/implementations", "elder", "notifications"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _quick_tool(vault, name, body, store):
    import hashlib
    from systemu.core.models import Tool, ToolStatus, ToolType
    from systemu.core.utils import generate_id
    from systemu.runtime.command_approvals import tool_signature
    from systemu.runtime.effect_tags import classify_source
    impl = Path(vault.root) / "tools" / "implementations" / f"{name}.py"
    impl.write_text(body, encoding="utf-8")
    tags = sorted(t.value for t in classify_source(body))
    tool = Tool(id=generate_id("tool"), name=name, description=f"test {name}",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True, implementation_path=str(impl), forged_by_systemu=True,
                parameter_names=["x", "api_key", "note"], effect_tags=tags)
    vault.save_tool(tool)
    sig = tool_signature(name, hashlib.sha1(impl.read_bytes()).hexdigest(),
                         set(tags), host_class="")
    store.approve(sig, command=name)
    return tool


def test_quick_lane_event_log_holds_no_raw_parameter_prompt_or_result(tmp_path, monkeypatch):
    from systemu.interface import notifications
    from systemu.pipelines.quick_task import run_quick_task
    from systemu.runtime.command_approvals import CommandApprovalStore
    from systemu.runtime.tool_sandbox import ToolSandbox

    vault = _quick_vault(tmp_path)
    log_path = tmp_path / "notifications" / "event_log.jsonl"
    monkeypatch.setattr(notifications, "_event_log_path", log_path)
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    _quick_tool(vault, "echo_tool", _ECHO, store)
    _quick_tool(vault, "fail_tool", _FAIL, store)
    sandbox = ToolSandbox(vault.root, vault=vault, command_approvals=store)

    params = {"x": _Q_JWT, "api_key": _KEYED, "note": f"use {_Q_SK} here"}
    script = [
        {"action": "TOOL_CALL", "tool": "echo_tool", "params": params,
         "reasoning": f"the token is {_Q_JWT}"},
        {"action": "TOOL_CALL", "tool": "fail_tool", "params": {"x": _Q_JWT}},
        # the JWT LEADS the answer, so a summary sliced before the mask would
        # keep its unrecognisable head
        {"action": "ANSWER", "answer_md": f"{_Q_JWT} is the token; key {_Q_SK}"},
    ]
    calls = {"i": 0}

    def llm_json(*, system, user, config=None):
        a = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return a

    res = run_quick_task(f"log in with {_Q_JWT} please", None, vault,
                         llm_json=llm_json, sandbox=sandbox, max_iters=4)
    assert res.tool_calls >= 1

    blob = log_path.read_bytes().decode("utf-8")
    _all_absent(blob, _Q_JWT, _Q_SK, _KEYED)

    # Non-vacuous: the tool-call event IS on disk, its arguments are named.
    rows = _jsonl(log_path)
    with_params = [r for r in rows if (r.get("details") or {}).get("tool_params")]
    assert with_params, "the tool-call event never reached the log"
    tp = with_params[0]["details"]["tool_params"]
    assert set(tp) == {"x", "api_key", "note"}
    assert tp["note"].startswith("use ") and tp["note"].endswith(" here")
    assert any("failed:" in r["message"] for r in rows)        # the error path ran
    assert any((r.get("details") or {}).get("summary") for r in rows)  # run-end ran


def test_event_log_writer_masks_for_every_caller(tmp_path, monkeypatch):
    """Any caller of log_event, not just the quick lane: the writer masks."""
    from systemu.interface import notifications
    log_path = tmp_path / "event_log.jsonl"
    monkeypatch.setattr(notifications, "_event_log_path", log_path)
    notifications.log_event(
        "INFO", "runtime", f"call failed: {_W_JWT}",
        {"execution_id": "exec_ab12cd34", "origin": "manual", "hint": _W_SK},
        details={"tool_params": {"body": _W_JWT, "password": _KEYED},
                 "tool_result": {"echo": _W_SK}, "reasoning": _W_JWT})
    blob = log_path.read_text(encoding="utf-8")
    _all_absent(blob, _W_JWT, _W_SK, _KEYED)
    row = _jsonl(log_path)[0]
    assert row["context"]["execution_id"] == "exec_ab12cd34"   # ids survive
    assert row["origin"] == "manual"
    assert set(row["details"]["tool_params"]) == {"body", "password"}


# ═════════════════════════════════════════════════════════════════════════════
#  2. the workflow lane -- one real ShadowRuntime.execute run
# ═════════════════════════════════════════════════════════════════════════════

def _drive_workflow(tmp_path, monkeypatch, bridge_file: Path):
    """ShadowRuntime.execute with the real loop, the real transcript writer, the
    real bridge writer and the real success path; only the sandbox's execution
    and the LLM are stubbed."""
    from unittest.mock import patch
    from sharing_on.config import Config
    from systemu.core.models import Objective, Tool, ToolStatus, ToolType
    from systemu.interface.event_bridge_writer import install_bridge_writer
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.runtime.tool_sandbox import ToolResult
    import systemu.runtime.shadow_runtime as _sr
    from test_s3_credit_wiring import (_build_entities_objs, _redirect_snapshot_io,
                                       _softpass_outcome)

    monkeypatch.chdir(tmp_path)
    impl = tmp_path / "tools" / "implementations" / "lookup.py"
    impl.parent.mkdir(parents=True, exist_ok=True)
    impl.write_text("def run(x=None, api_key=None, note=None):\n"
                    "    return {'success': True}\n", encoding="utf-8")
    tool = Tool(id="tool_lookup", name="lookup", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True, implementation_path=str(impl),
                parameter_names=["x", "api_key", "note"])
    vault, shadow, activity = _build_entities_objs(
        tmp_path, [Objective(id=1, goal="look it up", success_criteria="found")],
        tool=tool)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    _redirect_snapshot_io(monkeypatch, tmp_path / "snap_data")
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request", lambda *a, **k: "card",
                        raising=False)
    runtime = ShadowRuntime(cfg, vault)

    async def _exec(impl_path, params, **kw):
        return ToolResult(success=True,
                          parsed={"success": True,
                                  "data": {"session": {"jwt": _W_JWT, "pad": "p" * 500},
                                           "raw": f"issued {_W_SK}"}},
                          gate={"gate": "tool", "verdict": "allow",
                                "passed_via": "policy"})
    monkeypatch.setattr(runtime.sandbox, "execute_tool", _exec)
    monkeypatch.setattr(_sr, "process_completion_claim", _softpass_outcome)
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "lookup",
         "parameters": {"x": _W_JWT, "api_key": _KEYED, "note": f"with {_W_SK}"},
         "reasoning": f"I hold {_W_JWT}"},
        # not credited, so the loop asks again -- with the first call in its
        # prompt's history, which is what the transcript must not keep raw
        {"action": "FAIL", "reason": "stop here"},
    ]
    unsubscribe = install_bridge_writer(str(bridge_file))
    try:
        with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
             patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
            asyncio.run(runtime.execute(shadow, activity))
    finally:
        unsubscribe()
    return vault


@pytest.fixture
def workflow_run(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge" / "manual_events.jsonl"
    bridge.parent.mkdir(parents=True)
    vault = _drive_workflow(tmp_path, monkeypatch, bridge)
    return tmp_path, vault, bridge


def test_workflow_transcript_holds_no_raw_parameter_or_result(workflow_run):
    tmp, _vault, _ = workflow_run
    files = list((tmp / "executions").glob("*/llm_transcript.jsonl"))
    assert files, "the run wrote no transcript"
    blob = "".join(f.read_text(encoding="utf-8") for f in files)
    _all_absent(blob, _W_JWT, _W_SK, _KEYED)
    entries = [e for f in files for e in _jsonl(f)]
    # Non-vacuous: both iterations recorded; the decision's arguments are named.
    assert len(entries) >= 2
    decision = json.loads(entries[0]["response"])
    assert set(decision["parameters"]) == {"x", "api_key", "note"}
    assert decision["parameters"]["note"].startswith("with ")
    # the second prompt carried the first call back -- masked, keys intact
    assert '"api_key"' in entries[1]["user"]


def test_workflow_bridge_file_holds_no_raw_parameter_or_result(workflow_run):
    _tmp, _vault, bridge = workflow_run
    blob = bridge.read_text(encoding="utf-8")
    _all_absent(blob, _W_JWT, _W_SK, _KEYED)
    rows = _jsonl(bridge)
    calls = [r for r in rows if (r.get("details") or {}).get("action") == "TOOL_CALL"]
    assert calls, "the per-iteration TOOL_CALL event never reached the bridge"
    det = calls[0]["details"]
    assert set(det["tool_params"]) == {"x", "api_key", "note"}
    assert det["tool_result"]                                  # rendered, masked


def test_workflow_decision_audit_holds_no_raw_reasoning(workflow_run):
    """The per-iteration decision audit keeps the model's reasoning, which quoted
    the token it was about to pass."""
    tmp, _vault, _ = workflow_run
    files = list((tmp / "executions").glob("*/decision_audit.jsonl"))
    assert files, "the run wrote no decision audit"
    blob = "".join(f.read_text(encoding="utf-8") for f in files)
    _all_absent(blob, _W_JWT, _W_SK, _KEYED)
    first = _jsonl(files[0])[0]
    assert first["action"] == "TOOL_CALL" and first["reasoning"].startswith("I hold ")


def test_workflow_replay_buffer_holds_no_raw_parameter(workflow_run):
    tmp, vault, _ = workflow_run
    blob = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in (tmp / "tools").rglob("*.json"))
    _all_absent(blob, _W_JWT, _W_SK, _KEYED)
    tool = vault.get_tool("tool_lookup")
    buf = tool.last_successful_params
    assert buf, "the success path recorded no replay params"
    assert set(buf[-1]) == {"x", "api_key", "note"}
    assert buf[-1]["api_key"] == "<redacted>"                 # key-name pass first
    assert buf[-1]["note"].startswith("with ")                 # value pass kept prose


# ═════════════════════════════════════════════════════════════════════════════
#  3. the resume snapshot -- a log, not custody: values masked, resume intact
# ═════════════════════════════════════════════════════════════════════════════

def _context_with_secrets(error: str):
    from systemu.runtime.context_builder import ExecutionContext
    ctx = ExecutionContext(execution_id="exec_snap", system_prompt="s",
                           scroll_json=[], tool_index=[])
    ctx.add_tool_call({"action": "TOOL_CALL", "tool_name": "lookup",
                       "parameters": {"x": _W_JWT, "api_key": _KEYED,
                                      "note": f"with {_W_SK}"},
                       "completes_objective": 1}, 1)
    ctx.add_observation({"success": False, "error": error,
                         "data": {"session": {"jwt": _W_JWT, "pad": "p" * 500}}}, 1)
    ctx.add_thought(f"the bearer is {_W_JWT}", 1)
    return ctx


def _capture(ctx):
    from systemu.runtime.execution_snapshot import capture_from_context
    return capture_from_context(execution_id="exec_snap", shadow_id="sh", scroll_id="sc",
                                iteration=3, current_action_block=1,
                                completed_objectives=set(), context=ctx)


def test_resume_snapshot_on_disk_holds_no_raw_parameter(tmp_path):
    from systemu.runtime.execution_snapshot import write_snapshot
    path = write_snapshot(_capture(_context_with_secrets(f"denied {_W_SK}")),
                          data_dir=tmp_path)
    blob = Path(path).read_text(encoding="utf-8")
    _all_absent(blob, _W_JWT, _W_SK, _KEYED)
    hist = json.loads(blob)["recent_history_slice"]
    call = [e for e in hist if e["role"] == "tool_call"][0]
    assert call["tool"] == "lookup"
    assert set(call["params"]) == {"x", "api_key", "note"}


def test_resume_reads_back_what_it_needs_from_the_masked_snapshot(tmp_path):
    """The reflection block a resumed run gets from the masked snapshot is the
    one it got from the unmasked snapshot: resume reads the tool name, the
    parameter KEYS and the success flag/error, never a parameter value. (The
    keys come back sorted either way -- ``write_snapshot`` has always written
    with ``sort_keys``.)"""
    from systemu.runtime.context_builder import ExecutionContext
    from systemu.runtime.execution_snapshot import (_snapshot_path, _to_dict,
                                                    apply_to_context, audit_data_root,
                                                    read_snapshot, write_snapshot)
    raw = _capture(_context_with_secrets("plain failure"))
    write_snapshot(raw, data_dir=tmp_path / "masked")
    back = read_snapshot("exec_snap", data_dir=tmp_path / "masked")
    # what the same snapshot read back as before the mask: the raw dict on disk
    unmasked = _snapshot_path(audit_data_root(tmp_path / "raw"), "exec_snap")
    unmasked.parent.mkdir(parents=True)
    unmasked.write_text(json.dumps(_to_dict(raw), indent=2, sort_keys=True),
                        encoding="utf-8")
    before = read_snapshot("exec_snap", data_dir=tmp_path / "raw")

    def _reflection(snap):
        ctx = ExecutionContext(execution_id="exec_r", system_prompt="s",
                               scroll_json=[], tool_index=[])
        apply_to_context(snap, context=ctx)
        return ctx._pending_reflection

    from_raw, from_disk = _reflection(before), _reflection(back)
    assert "tool_call: lookup (params keys: ['api_key', 'note', 'x'])" in from_disk
    calls = lambda b: [ln for ln in b.splitlines() if "tool_call:" in ln or "tool_result:" in ln]
    assert calls(from_disk) == calls(from_raw)


# ═════════════════════════════════════════════════════════════════════════════
#  4. the dry-run replay buffer -- value shapes under neutral keys
# ═════════════════════════════════════════════════════════════════════════════

def test_redact_secrets_scans_values_after_the_key_pass():
    from systemu.pipelines.tool_dry_run import _redact_secrets
    out = _redact_secrets({"token": "short", "body": _W_JWT,
                           "headers": {"x-trace": f"Bearer {_W_SK}"},
                           "author": "Ada", "count": 3})
    blob = json.dumps(out)
    _all_absent(blob, _W_JWT, _W_SK)
    assert out["token"] == "<redacted>"                        # key pass unchanged
    assert out["author"] == "Ada" and out["count"] == 3        # benign kept verbatim


# ═════════════════════════════════════════════════════════════════════════════
#  5. DEC-27 -- a redactor that raises drops the value, never writes it raw
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def broken_redactor(monkeypatch):
    import systemu.runtime.external_verifier as ev

    def _boom(text):
        raise RuntimeError("redactor down")
    monkeypatch.setattr(ev, "_scrub_value_shapes", _boom)


def test_unredactable_event_log_value_is_dropped_and_named(tmp_path, monkeypatch,
                                                          broken_redactor):
    from systemu.interface import notifications
    log_path = tmp_path / "event_log.jsonl"
    monkeypatch.setattr(notifications, "_event_log_path", log_path)
    notifications.log_event("INFO", "runtime", f"x {_W_JWT}", {"origin": "manual"},
                            details={"tool_params": {"body": _W_JWT}})
    blob = log_path.read_text(encoding="utf-8")
    _assert_absent(blob, _W_JWT)
    row = _jsonl(log_path)[0]
    assert row["message"] == UNREDACTABLE
    assert row["details"]["tool_params"] == UNREDACTABLE or \
        row["details"] == UNREDACTABLE or \
        row["details"]["tool_params"]["body"] == UNREDACTABLE


def test_unredactable_transcript_value_is_dropped_and_named(tmp_path, broken_redactor):
    from systemu.runtime.llm_transcript import append_call
    append_call(tmp_path, "exec_u", {"iteration": 1, "user": f"see {_W_JWT}",
                                     "response": json.dumps({"parameters": {"b": _W_JWT}})})
    p = tmp_path / "executions" / "exec_u" / "llm_transcript.jsonl"
    blob = p.read_text(encoding="utf-8")
    _assert_absent(blob, _W_JWT)
    row = _jsonl(p)[0]
    assert row["user"] == UNREDACTABLE and row["response"] == UNREDACTABLE
    assert row["iteration"] == 1


def test_unredactable_snapshot_history_is_dropped_and_named(tmp_path, broken_redactor):
    from systemu.runtime.execution_snapshot import write_snapshot
    path = write_snapshot(_capture(_context_with_secrets("e")), data_dir=tmp_path)
    blob = Path(path).read_text(encoding="utf-8")
    _assert_absent(blob, _W_JWT)
    assert UNREDACTABLE in blob


def test_unredactable_decision_reasoning_is_dropped_and_named(tmp_path, broken_redactor):
    from systemu.runtime import decision_audit as da
    da.append_iteration_decision(tmp_path, "exec_d", da.IterationDecision(
        execution_id="exec_d", iteration=1, action="THINK", reasoning=f"r {_W_JWT}",
        consecutive_thinks=0, loop_guard_active=False, loop_guard_message=None,
        stuck_round_count=0, consec_research_reads=0, consec_tool_failures=0,
        is_request_harness=False))
    rows = da.read_iteration_decisions(tmp_path, "exec_d")
    assert rows and rows[0]["reasoning"] == UNREDACTABLE


def test_unredactable_replay_param_is_dropped_and_named(broken_redactor):
    from systemu.pipelines.tool_dry_run import _redact_secrets
    out = _redact_secrets({"token": "t", "body": _W_JWT})
    assert out == {"token": "<redacted>", "body": UNREDACTABLE}


def test_unredactable_bridge_event_is_dropped_and_named(tmp_path, broken_redactor):
    from systemu.interface.event_bridge_writer import install_bridge_writer
    from systemu.interface.event_bus import EventBus
    bridge = tmp_path / "b.jsonl"
    unsub = install_bridge_writer(str(bridge))
    try:
        EventBus.get().publish({"category": "runtime", "message": f"m {_W_JWT}",
                                "details": {"tool_params": {"x": _W_JWT}}})
    finally:
        unsub()
    blob = bridge.read_text(encoding="utf-8")
    _assert_absent(blob, _W_JWT)
    assert UNREDACTABLE in blob


# ═════════════════════════════════════════════════════════════════════════════
#  6. the shared helper -- DEC-31 order, keys kept
# ═════════════════════════════════════════════════════════════════════════════

def test_mask_text_masks_before_it_caps():
    from systemu.runtime.durable_redact import mask_text
    for cap in _WORKFLOW_CAPS:
        out = mask_text(f"token {_W_JWT} end", max_len=cap)
        assert len(out) <= cap
        _assert_absent(out, _W_JWT)


def test_mask_params_keeps_keys_and_drops_values():
    from systemu.runtime.durable_redact import mask_params
    out = mask_params({"password": _KEYED, "q": f"x {_W_SK}", "n": 2, "nested": [{"a": _W_JWT}]})
    assert set(out) == {"password", "q", "n", "nested"}
    assert out["n"] == 2
    _all_absent(json.dumps(out), _KEYED, _W_SK, _W_JWT)


def test_mask_text_structured_honours_secret_named_keys_inside_json():
    from systemu.runtime.durable_redact import mask_text
    doc = json.dumps({"action": "TOOL_CALL", "parameters": {"password": _KEYED}})
    _assert_absent(mask_text(doc, structured=True), _KEYED)


# ═════════════════════════════════════════════════════════════════════════════
#  7. structural -- a new unmasked writer fails; the allowlist only shrinks
# ═════════════════════════════════════════════════════════════════════════════

def test_no_unmasked_durable_writer_outside_the_allowlist():
    from tools import lint_durable_writers as L
    new, stale = L.drift(L.scan_repo(), L.load_allowlist())
    assert new == [], "\n".join(new)
    assert stale == [], "\n".join(stale)


def test_every_fenced_writer_still_calls_its_mask():
    from tools import lint_durable_writers as L
    assert L.fence_breaks() == []


def test_every_allowlist_entry_carries_a_reason_and_a_kind():
    from tools import lint_durable_writers as L
    kinds = {"masked-upstream", "not-a-param", "operator-record", "residual"}
    for key, e in L.load_allowlist().items():
        assert e.get("kind") in kinds, key
        assert len(e["reason"]) > 40, key


def test_the_lint_flags_a_new_writer_that_skips_the_mask():
    from tools import lint_durable_writers as L
    src = ("import json\n"
           "def save(path, params):\n"
           "    with open(path, 'a') as f:\n"
           "        f.write(json.dumps(params))\n")
    assert [f.qualname for f in L.scan_source(src, "x.py")] == ["save"]
    masked = src.replace("json.dumps(params)", "json.dumps(mask_params(params))")
    assert L.scan_source(masked, "x.py") == []


def test_the_lint_sees_api_sinks_and_closures():
    from tools import lint_durable_writers as L
    src = ("def outer(vault, decision):\n"
           "    def _cb(ev):\n"
           "        vault.append_action_audit({'params': decision['parameters']})\n"
           "    return _cb\n")
    assert [f.qualname for f in L.scan_source(src, "x.py")] == ["outer._cb"]


def test_the_lint_fails_both_ways_against_the_baseline():
    from tools import lint_durable_writers as L
    f = L.Finding("systemu/a.py", "w", 2)
    allow = {("systemu/a.py", "w"): {"sinks": 1}, ("systemu/b.py", "gone"): {"sinks": 1}}
    new, stale = L.drift([f], allow)
    assert len(new) == 1 and "2 write sites" in new[0]          # grew past its baseline
    assert len(stale) == 1 and "gone" in stale[0]               # fixed -> must be deleted


def test_the_lint_fails_loud_on_unparseable_source():
    from tools import lint_durable_writers as L
    with pytest.raises(L.DurableWriterLintError):
        L.scan_source("def broken(:\n", "x.py")
