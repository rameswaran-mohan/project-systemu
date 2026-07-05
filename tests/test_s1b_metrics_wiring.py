"""S1b (Task 7 / PLAN-11) — approval-fatigue counters wired at the decision
post/resolve chokepoints.

Uses a real (filesystem) Vault + real OperatorDecisionQueue/InboxQueue so the
wiring is exercised end-to-end, then reads back MetricsStore.snapshot() from
``<vault.root>/metrics``. Mirrors the vault-construction helper used by
``tests/test_decision_queue_amend.py``.
"""
from __future__ import annotations

from pathlib import Path

from systemu.approval.decision_queue import OperatorDecisionQueue
from systemu.runtime.metrics_store import MetricsStore


def _make_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications",
        "executions", "decisions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in [
        "scrolls", "activities", "shadow_army", "skills", "tools",
        "evolutions", "decisions",
    ]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _snapshot(tmp_path):
    return MetricsStore(Path(tmp_path) / "metrics").snapshot()


# ── 1. post() counts once; the dedup short-circuit does not double-count ────

def test_post_counts_created_once_dedup_does_not(tmp_path):
    vault = _make_vault(tmp_path)
    queue = OperatorDecisionQueue(vault)

    first = queue.post(
        title="Run tool: csv_summariser", body="?",
        options=["Deny", "Approve once", "Always allow"],
        context={"kind": "gate", "gate_type": "tool"},
        dedup_key="tool:sig_abc",
    )
    second = queue.post(
        title="Run tool: csv_summariser", body="?",
        options=["Deny", "Approve once", "Always allow"],
        context={"kind": "gate", "gate_type": "tool"},
        dedup_key="tool:sig_abc",
    )

    assert first == second  # dedup short-circuit returned the existing id
    assert _snapshot(tmp_path)["gate_cards_created"] == 1


# ── 2. resolve() records choice + latency + resolved-count ──────────────────

def test_resolve_records_choice_and_latency(tmp_path):
    vault = _make_vault(tmp_path)
    queue = OperatorDecisionQueue(vault)

    did = queue.post(
        title="Run tool: csv_summariser", body="?",
        options=["Deny", "Approve once", "Always allow"],
        context={"kind": "gate", "gate_type": "tool"},
        dedup_key="tool:sig_def",
    )
    queue.resolve(did, choice="Always allow")

    snap = _snapshot(tmp_path)
    assert snap["always_allow_grants"] == 1
    assert snap["gate_cards_resolved"] == 1
    assert len(snap["resolution_latency_ms"]) == 1


# ── 3. the auto-allow bypass (born-resolved row) is counted ─────────────────

def test_auto_allow_bypass_is_counted(tmp_path):
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.gate_mode import GateMode, GateModePolicy
    from systemu.interface.command.inbox import InboxQueue

    vault = _make_vault(tmp_path)
    inbox = InboxQueue(vault)

    descriptor = GateDescriptor.from_tool(
        tool_name="csv_summariser", sig="sig_xyz", verdict="allow")
    policy = GateModePolicy(mode=GateMode.BYPASS)  # "tool" is not on the floor

    inbox.enqueue(descriptor, gate_type="tool", policy=policy, vault=vault)

    snap = _snapshot(tmp_path)
    assert snap["always_allow_grants"] == 1
    assert snap["gate_cards_resolved"] == 1


# ── 4. structured_question decisions are tracked separately from gates ──────

def test_structured_question_split_from_gate_counters(tmp_path):
    vault = _make_vault(tmp_path)
    queue = OperatorDecisionQueue(vault)

    did = queue.post(
        title="What's the destination city?", body="?",
        options=["__structured_answer__"],
        context={"kind": "structured_question"},
        dedup_key="ask:q1",
    )

    snap = _snapshot(tmp_path)
    assert snap["asks_created"] == 1
    assert snap["gate_cards_created"] == 0

    queue.resolve(did, choice='{"city": "Paris"}')

    snap = _snapshot(tmp_path)
    assert snap["asks_resolved"] == 1
    assert snap["gate_cards_resolved"] == 0
