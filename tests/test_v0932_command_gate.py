"""v0.9.32 Item 4 — per-command operator approval gate (FOUNDATIONS D.1–D.3).

Tasks D.1 (CommandApprovalStore), D.2 (GateDescriptor.from_command), and
D.3 (command floor gate type + dispatcher handler). D.4–D.7 append here.
"""
import importlib
from pathlib import Path


# ── D.1: CommandApprovalStore ─────────────────────────────────────────────────

def test_command_signature_normalizes_whitespace_and_includes_cwd():
    from systemu.runtime.command_approvals import command_signature

    a = command_signature("rm   -rf   build", cwd="/proj")
    b = command_signature("rm -rf build", cwd="/proj")
    assert a == b, "whitespace must be collapsed before hashing"

    c = command_signature("rm -rf build", cwd="/other")
    assert a != c, "cwd is part of the signature"

    # sha1 hex digest shape
    assert len(a) == 40 and all(ch in "0123456789abcdef" for ch in a)


def test_approve_persists_and_survives_reload(tmp_path: Path):
    from systemu.runtime.command_approvals import CommandApprovalStore, command_signature

    p = tmp_path / "command_approvals.json"
    store = CommandApprovalStore(p)
    sig = command_signature("rm -rf build", cwd="/proj")

    assert store.is_approved(sig) is False
    assert store.approve(sig, command="rm -rf build", cwd="/proj") is True
    # idempotent
    assert store.approve(sig, command="rm -rf build", cwd="/proj") is False

    # A fresh instance reading the same file sees the approval (out-of-process).
    reloaded = CommandApprovalStore(p)
    assert reloaded.is_approved(sig) is True
    assert p.exists()


def test_unrelated_signature_is_not_approved(tmp_path: Path):
    from systemu.runtime.command_approvals import CommandApprovalStore, command_signature

    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    store.approve(command_signature("rm -rf build", cwd="/proj"),
                  command="rm -rf build", cwd="/proj")
    assert store.is_approved(command_signature("rm -rf other", cwd="/proj")) is False


def test_init_default_store_is_singleton(tmp_path: Path):
    import systemu.runtime.command_approvals as ca
    ca.reset_default_store_for_tests()
    s1 = ca.init_default_store(tmp_path)
    s2 = ca.init_default_store(tmp_path)
    assert s1 is s2
    assert s1.path == tmp_path / "command_approvals.json"


# ── D.2: GateDescriptor.from_command ──────────────────────────────────────────

def test_from_command_descriptor_shape():
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.command_approvals import command_signature

    sig = command_signature("rm -rf build", cwd="/proj")
    g = GateDescriptor.from_command(
        tool_name="run_command", command="rm -rf build", cwd="/proj",
        reason="agent step 3")

    assert g.title == "Run command: run_command"
    assert g.risk == "high"
    assert "rm -rf build" in g.inspect and "/proj" in g.inspect
    assert g.options == ["Deny", "Approve once", "Always allow"]
    assert g.safe_default == "Deny"            # index 0
    assert g.dedup == f"command:{sig}"


# ── D.3: command floor gate type + dispatcher handler module ──────────────────

def test_command_is_a_floor_gate_type_under_bypass():
    from systemu.interface.command.gate_mode import GateMode, GateModePolicy
    policy = GateModePolicy(mode=GateMode.BYPASS)
    # Bypass auto-allows non-floor; the command floor must still ask.
    assert policy.decide(risk="high", gate_type="command") == "ask"
    assert policy.decide(risk="low", gate_type="command") == "ask"


def test_command_handler_registered_in_dispatcher_bootstrap():
    from systemu.approval import decision_dispatcher as dd
    dd._handlers_bootstrapped = False
    dd._ensure_handlers_registered()
    assert "command" in dd._HANDLERS


def test_always_allow_persists_via_handler(tmp_path, monkeypatch):
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.command_approvals import command_signature
    from systemu.pipelines import command_gate_handler as h

    ca.reset_default_store_for_tests()
    store = ca.init_default_store(tmp_path)
    sig = command_signature("rm -rf build", cwd="/proj")

    class _Dec:
        dedup_key = f"command:{sig}"
        context = {"command": "rm -rf build", "cwd": "/proj"}

    h._handle_resolved_command(_Dec(), "Always allow", None, None)
    assert store.is_approved(sig) is True


def test_approve_once_and_deny_do_not_persist(tmp_path):
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.command_approvals import command_signature
    from systemu.pipelines import command_gate_handler as h

    ca.reset_default_store_for_tests()
    store = ca.init_default_store(tmp_path)
    sig = command_signature("rm -rf build", cwd="/proj")

    class _Dec:
        dedup_key = f"command:{sig}"
        context = {"command": "rm -rf build", "cwd": "/proj"}

    h._handle_resolved_command(_Dec(), "Approve once", None, None)
    h._handle_resolved_command(_Dec(), "Deny", None, None)
    assert store.is_approved(sig) is False


# ── D.4: the gate inside ToolSandbox.execute_tool ─────────────────────────────
import asyncio
import pytest


def _sandbox_with_store(tmp_path, vault=None):
    from systemu.runtime.tool_sandbox import ToolSandbox
    from systemu.runtime.command_approvals import CommandApprovalStore
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=vault, command_approvals=store)
    return sb, store


def test_destructive_command_raises_pending_operator_decision(tmp_path, monkeypatch):
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime.command_approvals import command_signature

    # A fake vault whose InboxQueue.enqueue returns a decision id (so the gate
    # can post and raise). We patch InboxQueue at the gate's import site.
    posted = {}

    class _FakeInbox:
        def __init__(self, vault):
            pass
        def enqueue(self, descriptor, *, gate_type, **kw):
            posted["gate_type"] = gate_type
            posted["dedup"] = descriptor.dedup
            return "dec_cmd_1"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())
    impl = "systemu/vault/tools/implementations/run_command.py"

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(impl, {"command": "rm -rf build", "cwd": "/proj"}))

    sig = command_signature("rm -rf build", cwd="/proj")
    assert ei.value.dedup_key == f"command:{sig}"
    assert posted["gate_type"] == "command"
    assert posted["dedup"] == f"command:{sig}"


def test_readonly_command_does_not_gate(tmp_path, monkeypatch):
    # A read-only `dir` must never post a gate; we assert enqueue is NOT called.
    called = {"enqueue": False}

    class _FakeInbox:
        def __init__(self, vault):
            pass
        def enqueue(self, *a, **k):
            called["enqueue"] = True
            return "dec_x"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())
    impl = "systemu/vault/tools/implementations/run_command.py"
    # No registry attached + impl exists → subprocess path runs the read-only
    # command; the point is only that NO gate was posted.
    asyncio.run(sb.execute_tool(impl, {"command": "dir"}))
    assert called["enqueue"] is False


def test_approved_signature_skips_the_gate(tmp_path, monkeypatch):
    from systemu.runtime.command_approvals import command_signature

    called = {"enqueue": False}

    class _FakeInbox:
        def __init__(self, vault):
            pass
        def enqueue(self, *a, **k):
            called["enqueue"] = True
            return "dec_x"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, store = _sandbox_with_store(tmp_path, vault=object())
    store.approve(command_signature("rm -rf build", cwd="/proj"),
                  command="rm -rf build", cwd="/proj")
    impl = "systemu/vault/tools/implementations/run_command.py"
    # Approved → runs (subprocess path), no gate posted.
    asyncio.run(sb.execute_tool(impl, {"command": "rm -rf build", "cwd": "/proj"}))
    assert called["enqueue"] is False


# ── D.5: shadow lane routes destructive shell calls to the sandbox gate ───────
def test_shadow_legacy_autodeny_skips_shell_tools():
    """The legacy headless auto-deny must NOT fire for shell tools — those are
    gated at the sandbox (D.4). Non-shell destructive tools still use it."""
    from systemu.runtime.shadow_runtime import _legacy_autodeny_applies

    # Shell tools are handled by the sandbox command gate → legacy block skipped.
    assert _legacy_autodeny_applies("run_command") is False
    assert _legacy_autodeny_applies("run_cli_command") is False
    # A non-shell destructive tool still uses the legacy confirm/auto-deny path.
    assert _legacy_autodeny_applies("delete_file") is True


# ── D.6: chat (quick) lane block-and-ask ──────────────────────────────────────
def test_quick_lane_blocks_and_denies_on_operator_deny(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.pipelines import quick_task as qt

    # Sandbox stub: first call raises PendingOperatorDecision (the gate).
    calls = {"n": 0}

    class _Sandbox:
        def execute_tool(self, *a, **k):
            calls["n"] += 1
            raise PendingOperatorDecision(
                decision_id="dec_1", dedup_key="command:abc",
                options=["Deny", "Approve once", "Always allow"])

    sb = _Sandbox()
    sb._vault = object()  # _execute_tool reads sandbox._vault for the queue

    # Operator chose Deny.
    monkeypatch.setattr(qt, "_poll_command_choice",
                        lambda vault, dedup_key, timeout=None: "Deny")

    tool = SimpleNamespace(implementation_path="x.py", dependencies=[],
                           tool_type="system")
    result = qt._execute_tool(sb, tool, {"command": "rm -rf build"})
    assert getattr(result, "success", True) is False
    assert "deny" in str(getattr(result, "error", "")).lower() \
        or "denied" in str(getattr(result, "error", "")).lower()


def test_quick_lane_timeout_is_fail_closed(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.pipelines import quick_task as qt

    class _Sandbox:
        def execute_tool(self, *a, **k):
            raise PendingOperatorDecision(
                decision_id="dec_1", dedup_key="command:abc",
                options=["Deny", "Approve once", "Always allow"])

    sb = _Sandbox()
    sb._vault = object()
    # Timeout → poll returns None → treated as Deny (fail-closed).
    monkeypatch.setattr(qt, "_poll_command_choice",
                        lambda vault, dedup_key, timeout=None: None)

    tool = SimpleNamespace(implementation_path="x.py", dependencies=[],
                           tool_type="system")
    result = qt._execute_tool(sb, tool, {"command": "rm -rf build"})
    assert getattr(result, "success", True) is False


def test_quick_lane_approve_once_runs_then_does_not_persist(tmp_path, monkeypatch):
    """Approve once → re-attempt runs via the one-shot bypass token; no persist."""
    from types import SimpleNamespace
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.pipelines import quick_task as qt

    calls = {"n": 0, "resolved": []}

    class _Sandbox:
        async def execute_tool(self, *a, **k):
            calls["n"] += 1
            calls["resolved"].append(k.get("_command_gate_resolved"))
            # First call gates; second (with the bypass token) runs.
            if k.get("_command_gate_resolved") == "command:abc":
                return SimpleNamespace(success=True, error=None,
                                       parsed={"success": True})
            raise PendingOperatorDecision(
                decision_id="dec_1", dedup_key="command:abc",
                options=["Deny", "Approve once", "Always allow"])

    sb = _Sandbox()
    sb._vault = object()
    monkeypatch.setattr(qt, "_poll_command_choice",
                        lambda vault, dedup_key, timeout=None: "Approve once")

    tool = SimpleNamespace(implementation_path="x.py", dependencies=[],
                           tool_type="system")
    result = qt._execute_tool(sb, tool, {"command": "rm -rf build"})
    assert getattr(result, "success", False) is True
    assert calls["n"] == 2
    # The re-attempt carried the one-shot bypass token.
    assert "command:abc" in calls["resolved"]


# ── D.7: consolidated gate-behavior tests ─────────────────────────────────────
import json as _json


def test_is_destructive_integration_matrix():
    from systemu.runtime.tool_sandbox import ToolSandbox
    D = ToolSandbox.is_destructive_call
    # gate-required
    assert D("run_command", {"command": "rm -rf /tmp/x"}) is True
    assert D("run_command", {"command": "git push && rm x"}) is True   # metachar
    assert D("run_cli_command", {"command": "drop table users"}) is True
    # no gate (provably read-only)
    assert D("run_command", {"command": "dir"}) is False
    assert D("run_command", {"command": "git status"}) is False
    assert D("run_cli_command", {"command": "ls"}) is False


def test_always_allow_then_second_identical_command_runs_without_gate(tmp_path, monkeypatch):
    """End-to-end D-2: after Always-allow persists the signature, a second
    identical command does NOT post a new gate."""
    import asyncio
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.command_approvals import command_signature
    from systemu.pipelines import command_gate_handler as h

    ca.reset_default_store_for_tests()
    store = ca.init_default_store(tmp_path)

    enqueue_count = {"n": 0}

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, descriptor, *, gate_type, **kw):
            enqueue_count["n"] += 1
            return "dec_1"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    from systemu.runtime.tool_sandbox import ToolSandbox
    from systemu.approval.exceptions import PendingOperatorDecision
    sb = ToolSandbox(str(tmp_path), vault=object(), command_approvals=store)
    impl = "systemu/vault/tools/implementations/run_command.py"
    params = {"command": "rm -rf build", "cwd": "/proj"}

    # 1st call → gated.
    try:
        asyncio.run(sb.execute_tool(impl, params))
    except PendingOperatorDecision:
        pass
    assert enqueue_count["n"] == 1

    # Operator picks "Always allow" → handler persists the signature.
    sig = command_signature("rm -rf build", cwd="/proj")
    class _Dec:
        dedup_key = f"command:{sig}"
        context = {"command": "rm -rf build", "cwd": "/proj"}
    h._handle_resolved_command(_Dec(), "Always allow", None, None)
    assert store.is_approved(sig) is True

    # 2nd identical call → NO new gate (runs via subprocess path).
    asyncio.run(sb.execute_tool(impl, params))
    assert enqueue_count["n"] == 1  # unchanged


def test_floor_under_bypass_still_asks_for_command():
    from systemu.interface.command.gate_mode import GateMode, GateModePolicy
    # Even with no overrides and Bypass, the command floor resolves to "ask".
    assert GateModePolicy(mode=GateMode.BYPASS).decide(
        risk="high", gate_type="command") == "ask"


def test_no_store_is_fail_closed_default_deny(tmp_path, monkeypatch):
    """When the approval store cannot be resolved, the gate still posts +
    raises (never silently runs the destructive command)."""
    import asyncio
    import systemu.runtime.command_approvals as ca
    from systemu.approval.exceptions import PendingOperatorDecision

    # Force init_default_store to fail so the gate has no store to consult.
    monkeypatch.setattr(ca, "init_default_store",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no store")))

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, descriptor, *, gate_type, **kw): return "dec_1"
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    from systemu.runtime.tool_sandbox import ToolSandbox
    sb = ToolSandbox(str(tmp_path), vault=object(), command_approvals=None)
    impl = "systemu/vault/tools/implementations/run_command.py"
    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(impl, {"command": "rm -rf build"}))


# ── REVIEW FIX-1: queued/background shadow lane fail-closed deny ──────────────
def _bare_supervisor():
    """A Supervisor with __init__ bypassed — we exercise _handle_result in
    isolation with the exact attributes it reads stubbed. Never touches a real
    vault/daemon/queue."""
    from systemu.runtime.supervisor import Supervisor
    import threading as _t
    sup = Supervisor.__new__(Supervisor)
    sup.vault = None
    sup._task_queue = None
    sup._dl_lock = _t.Lock()
    sup._dead_letters = []
    return sup


def test_command_gate_blocked_is_terminal_no_retry_no_postmortem(monkeypatch):
    """FIX-1: a queued shadow whose runtime.execute raised
    PendingOperatorDecision becomes status='command_gate_blocked'. _handle_result
    marks the activity terminal, publishes a WARNING, and does NOT retry /
    dead-letter / call _analyze_failure."""
    from systemu.runtime import supervisor as sup_mod

    sup = _bare_supervisor()

    counters = {"retry_submit": 0, "analyze": 0, "dead_letter": 0,
                "mark_failed": [], "published": []}

    # Stub the pieces _handle_result calls so we observe behavior, not effects.
    monkeypatch.setattr(sup, "submit",
                        lambda **kw: counters.__setitem__(
                            "retry_submit", counters["retry_submit"] + 1))
    monkeypatch.setattr(sup, "_analyze_failure",
                        lambda *a, **k: counters.__setitem__(
                            "analyze", counters["analyze"] + 1))
    monkeypatch.setattr(sup, "_aname", lambda aid: aid)
    monkeypatch.setattr(sup, "_publish",
                        lambda msg, level="INFO", context=None, origin=None:
                        counters["published"].append((level, msg)))

    def _fake_mark_failed(vault, activity_id, *, status="failed", summary=""):
        counters["mark_failed"].append((activity_id, status, summary))
        return True
    monkeypatch.setattr(
        "systemu.runtime.activity_completion.mark_activity_failed",
        _fake_mark_failed)

    payload = {"activity_id": "act_1", "shadow_id": "shд_1", "retry_count": 0}
    result = {"status": "command_gate_blocked", "error": "command_gate",
              "final_summary": "Blocked: approval required."}

    sup._handle_result(payload, result)

    # Terminal mark with status='failed' (NOT cancelled/suspended).
    assert counters["mark_failed"] == [("act_1", "failed", "Blocked: approval required.")]
    # No retry storm, no dead-letter append, no LLM post-mortem.
    assert counters["retry_submit"] == 0
    assert counters["analyze"] == 0
    assert sup._dead_letters == []
    # A WARNING-level "needs approval" message was published.
    assert any(level == "WARNING" and "approval" in msg.lower()
               for level, msg in counters["published"])


def test_failure_status_still_retries(monkeypatch):
    """Guard: a genuine transient failure (status='failure') is unaffected by
    FIX-1 — it still schedules a retry (proves the new branch is scoped)."""
    sup = _bare_supervisor()
    counters = {"retry_submit": 0}

    # threading.Timer(...).start() calls self.submit — intercept the Timer so
    # the retry counter increments synchronously without a real timer thread.
    import systemu.runtime.supervisor as sup_mod

    class _FakeTimer:
        def __init__(self, wait, fn, kwargs=None):
            self.fn, self.kwargs = fn, kwargs or {}
        def start(self):
            self.fn(**self.kwargs)

    monkeypatch.setattr(sup_mod.threading, "Timer", _FakeTimer)
    monkeypatch.setattr(sup, "submit",
                        lambda **kw: counters.__setitem__(
                            "retry_submit", counters["retry_submit"] + 1))
    monkeypatch.setattr(sup, "_aname", lambda aid: aid)
    monkeypatch.setattr(sup, "_publish",
                        lambda *a, **k: None)

    payload = {"activity_id": "act_2", "shadow_id": "shд_2", "retry_count": 0}
    result = {"status": "failure", "error": "boom"}
    sup._handle_result(payload, result)
    assert counters["retry_submit"] == 1


# ── REVIEW FIX-2: "Approve once" is single-use ────────────────────────────────
class _MemVault:
    """In-memory vault implementing exactly the decision-queue API surface:
    save_decision / get_decision / load_index('decisions'). Never touches disk."""
    def __init__(self):
        self._decisions = {}

    def save_decision(self, decision):
        self._decisions[decision.id] = decision

    def get_decision(self, did):
        return self._decisions[did]

    def load_index(self, kind):
        if kind != "decisions":
            return []
        out = []
        for d in self._decisions.values():
            out.append({
                "id": d.id,
                "status": d.status,
                "dedup_key": d.dedup_key,
                "created_at": (d.created_at.isoformat()
                               if getattr(d, "created_at", None) else ""),
            })
        return out


def test_consume_resolved_choice_is_single_use(tmp_path):
    """consume_resolved_choice returns the resolved choice ONCE, then expires it
    so get_resolved_choice returns None — a repeat command re-asks."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    vault = _MemVault()
    q = OperatorDecisionQueue(vault)

    dedup = "command:abc123"
    dec_id = q.post(title="Run command", body="rm -rf build",
                    options=["Deny", "Approve once", "Always allow"],
                    dedup_key=dedup)
    q.resolve(dec_id, choice="Approve once")

    # Non-consuming read still sees it.
    assert q.get_resolved_choice(dedup) == "Approve once"

    # Consume once: returns the choice and retires the decision.
    assert q.consume_resolved_choice(dedup) == "Approve once"

    # Now it is gone for both readers — the next identical command re-asks.
    assert q.get_resolved_choice(dedup) is None
    assert q.consume_resolved_choice(dedup) is None


def test_approve_once_re_gates_on_second_identical_command(tmp_path, monkeypatch):
    """End-to-end FIX-2: with ONE 'Approve once' resolution, the FIRST gated
    command runs (bypass consumes the decision); the SECOND identical command
    RE-GATES (raises PendingOperatorDecision again), because the one-shot was
    consumed."""
    import asyncio
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime.command_approvals import command_signature, CommandApprovalStore
    from systemu.runtime.tool_sandbox import ToolSandbox

    vault = _MemVault()
    q = OperatorDecisionQueue(vault)
    sig = command_signature("rm -rf build", cwd="/proj")
    dedup = f"command:{sig}"

    # The gate posts via InboxQueue.enqueue — back it onto the SAME _MemVault so
    # the resolved decision lives where consume_resolved_choice reads it.
    def _enqueue(self, descriptor, *, gate_type, **kw):
        return q.post(title=descriptor.title, body=descriptor.inspect,
                      options=descriptor.options, context=kw.get("context_extras"),
                      dedup_key=descriptor.dedup)
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue.enqueue",
                        _enqueue)

    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=vault, command_approvals=store)

    # First call WITHOUT a bypass token gates and posts the decision.
    with pytest.raises(PendingOperatorDecision):
        sb._maybe_gate_command("run_command",
                               {"command": "rm -rf build", "cwd": "/proj"})
    # Operator resolves "Approve once".
    pending = q.list_pending()
    assert len(pending) == 1
    q.resolve(pending[0].id, choice="Approve once")

    # FIRST honored attempt with the bypass token → does NOT raise (runs once),
    # and CONSUMES the one-shot.
    sb._maybe_gate_command("run_command",
                           {"command": "rm -rf build", "cwd": "/proj"},
                           resolved_dedup=dedup)

    # SECOND identical attempt with the bypass token → the one-shot was
    # consumed, so it RE-GATES (raises again) instead of replaying.
    with pytest.raises(PendingOperatorDecision):
        sb._maybe_gate_command("run_command",
                               {"command": "rm -rf build", "cwd": "/proj"},
                               resolved_dedup=dedup)


def test_always_allow_skips_gate_after_first(tmp_path, monkeypatch):
    """FIX-2 guard: 'Always allow' is UNCHANGED — it persists in the store and a
    second identical command runs without re-gating (no consume)."""
    import asyncio
    from systemu.runtime.command_approvals import command_signature, CommandApprovalStore
    from systemu.runtime.tool_sandbox import ToolSandbox

    enqueue_count = {"n": 0}

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, descriptor, *, gate_type, **kw):
            enqueue_count["n"] += 1
            return "dec_1"
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=object(), command_approvals=store)
    params = {"command": "rm -rf build", "cwd": "/proj"}

    # 1st call gates.
    with pytest.raises(__import__("systemu.approval.exceptions", fromlist=["PendingOperatorDecision"]).PendingOperatorDecision):
        sb._maybe_gate_command("run_command", params)
    assert enqueue_count["n"] == 1

    # Operator picks "Always allow" → store persists the signature.
    store.approve(command_signature("rm -rf build", cwd="/proj"),
                  command="rm -rf build", cwd="/proj")

    # 2nd identical call → NO new gate (is_approved short-circuits before bypass).
    sb._maybe_gate_command("run_command", params)
    assert enqueue_count["n"] == 1  # unchanged


# ── REVIEW FIX-3: command-gate resolution persists ONLY via the dispatcher ────
def test_resolve_gate_does_not_persist_command_always_allow(tmp_path):
    """The rail's resolve path (resolve_gate) must NOT persist an 'Always allow'
    for a command gate — it has no command branch and 'always allow' isn't in
    _APPROVE_LABELS, so it NOOPs. Always-allow persists ONLY via the dispatcher
    (command_gate_handler). This proves a stray rail resolve cannot silently
    permanently-allow a destructive command."""
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.command_approvals import command_signature
    from systemu.interface.command.inbox import resolve_gate
    from systemu.interface.command.result import CommandStatus

    ca.reset_default_store_for_tests()
    store = ca.init_default_store(tmp_path)
    sig = command_signature("rm -rf build", cwd="/proj")

    class _Dec:
        dedup_key = f"command:{sig}"
        choice = "Always allow"
        context = {"kind": "gate", "gate_type": "command",
                   "command": "rm -rf build", "cwd": "/proj"}

    result = resolve_gate(_Dec(), vault=object())
    # NOOP — no executor wired into resolve_gate for command gates.
    assert result.status == CommandStatus.NOOP
    # And crucially: nothing was persisted by the rail path.
    assert store.is_approved(sig) is False

    # The dispatcher path DOES persist (the only sanctioned route).
    from systemu.pipelines import command_gate_handler as h
    h._handle_resolved_command(_Dec(), "Always allow", None, None)
    assert store.is_approved(sig) is True


# ── REVIEW FIX-4: --force substring must not over-match --force-with-lease ─────
def test_force_with_lease_not_flagged_by_substring():
    """`git push --force-with-lease` is a SAFE git flag — it must NOT be flagged
    destructive SOLELY by the params-substring `--force` denylist. (It is still
    NOT read-only, so a shell tool gates it via is_readonly_shell_command — the
    point of this test is the substring denylist no longer over-matches.)"""
    from systemu.runtime.tool_sandbox import _FORCE_FLAG_RE
    import json as _j

    # The token-boundary regex itself: the load-bearing fix.
    assert _FORCE_FLAG_RE.search(_j.dumps(
        {"command": "git push --force-with-lease"}).lower()) is None
    # Standalone --force still matches.
    assert _FORCE_FLAG_RE.search(_j.dumps(
        {"command": "git push --force"}).lower()) is not None
    # --force=true (value form) still matches.
    assert _FORCE_FLAG_RE.search(_j.dumps(
        {"command": "cmd --force=true"}).lower()) is not None


def test_is_destructive_force_boundary_matrix():
    """is_destructive_call: standalone `--force` is destructive; on a NON-shell
    tool, `--force-with-lease` is not flagged by the substring rule. (For a shell
    tool both gate via the command read-only check; we use a non-shell tool here
    to isolate the substring denylist behavior.)"""
    from systemu.runtime.tool_sandbox import ToolSandbox
    D = ToolSandbox.is_destructive_call

    # Non-shell tool: the ONLY thing that can flag it is the params denylist.
    assert D("some_api_tool", {"args": "deploy --force"}) is True
    assert D("some_api_tool", {"args": "git push --force-with-lease"}) is False
    # rm -rf / drop table / delete from unchanged.
    assert D("some_api_tool", {"args": "rm -rf /tmp/x"}) is True
    assert D("some_api_tool", {"q": "DROP TABLE users"}) is True


# ── REVIEW FIX-5: quick lane fail-closed on a NON-Pending gate error ──────────
def test_quick_lane_fail_closed_on_non_pending_exception(tmp_path):
    """A generic exception from the tool dispatch (e.g. _maybe_gate_command's
    inbox enqueue against an unusable vault) must NOT crash run_quick_task — the
    quick lane returns a clean fail-closed denial result instead."""
    from types import SimpleNamespace
    from systemu.pipelines import quick_task as qt

    class _Sandbox:
        async def execute_tool(self, *a, **k):
            raise RuntimeError("vault unusable — enqueue blew up")

    sb = _Sandbox()
    sb._vault = object()

    tool = SimpleNamespace(implementation_path="x.py", dependencies=[],
                           tool_type="system")
    # Must NOT raise — returns a denial.
    result = qt._execute_tool(sb, tool, {"command": "rm -rf build"})
    assert getattr(result, "success", True) is False
    parsed = getattr(result, "parsed", {}) or {}
    assert parsed.get("error_type") == "command_denied"


def test_quick_lane_fail_closed_on_reattempt_exception(tmp_path, monkeypatch):
    """If the post-approval re-attempt itself raises a non-Pending error, the
    lane still fails closed (does not crash)."""
    from types import SimpleNamespace
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.pipelines import quick_task as qt

    calls = {"n": 0}

    class _Sandbox:
        async def execute_tool(self, *a, **k):
            calls["n"] += 1
            if k.get("_command_gate_resolved"):
                raise RuntimeError("re-attempt boom")
            raise PendingOperatorDecision(
                decision_id="dec_1", dedup_key="command:abc",
                options=["Deny", "Approve once", "Always allow"])

    sb = _Sandbox()
    sb._vault = object()
    monkeypatch.setattr(qt, "_poll_command_choice",
                        lambda vault, dedup_key, timeout=None: "Approve once")

    tool = SimpleNamespace(implementation_path="x.py", dependencies=[],
                           tool_type="system")
    result = qt._execute_tool(sb, tool, {"command": "rm -rf build"})
    assert getattr(result, "success", True) is False
    assert (getattr(result, "parsed", {}) or {}).get("error_type") == "command_denied"
    assert calls["n"] == 2  # gated, then re-attempt errored
