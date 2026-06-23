"""v0.9.39 Bug 15 — reconciliation + request cap are run-tree-scoped.

A logical run spans many executions — a suspend→approve→resume chain plus the
spawned sub-agent children — yet the per-run request cap and the terminal
outcome reconciliation were keyed to a single execution_id. This bundles a
shared ``root_execution_id`` (the run-tree id) into the snapshot + a per-root
governor sidecar so BOTH the cap and reconciliation span the whole tree.
"""
from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

from systemu.runtime.governor import Governor
from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot, write_snapshot, read_snapshot,
)
from systemu.vault.vault import Vault


# ── snapshot carries root_execution_id across suspend→resume ─────────────────

def test_snapshot_round_trips_root_execution_id(tmp_path):
    snap = ExecutionSnapshot(
        execution_id="exec_child", shadow_id="s", scroll_id="sc",
        requests_this_run=3, subagent_depth=1, root_execution_id="exec_root",
    )
    write_snapshot(snap, data_dir=tmp_path)
    got = read_snapshot("exec_child", data_dir=tmp_path)
    assert got is not None
    assert got.root_execution_id == "exec_root"
    assert got.requests_this_run == 3

def test_snapshot_root_defaults_none_for_legacy(tmp_path):
    # a pre-v0.9.39 snapshot file without the field reads back as None (no crash).
    # _snapshot_path() prepends "exec_" to the id, so the dir is exec_<id>.
    p = tmp_path / "audit" / "exec_old" / "resume_snapshot.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"execution_id": "old", "shadow_id": "s",
                             "scroll_id": "sc"}), encoding="utf-8")
    got = read_snapshot("old", data_dir=tmp_path)
    assert got is not None and got.root_execution_id is None


# ── sidecar: one shared request counter + lineage across the tree ────────────

def _vault(tmp_path):
    (tmp_path / "harness_ledger").mkdir(parents=True, exist_ok=True)
    return Vault(str(tmp_path))

def test_runtree_counter_accumulates_across_executions(tmp_path):
    g = Governor()
    v = _vault(tmp_path)
    root = "exec_root"
    # 8 requests spread across the parent + two children all sharing one root.
    seq = []
    for eid in ["exec_root", "exec_root", "exec_child0", "exec_child1",
                "exec_child1", "exec_root", "exec_resume", "exec_resume"]:
        seq.append(g.next_runtree_request(root, eid, v))
    # PRE-increment values must be a strict 0..7 run — the cap operand is tree-wide,
    # NOT reset per execution.
    assert seq == [0, 1, 2, 3, 4, 5, 6, 7]
    # next one would be 8 → an arbiter with max_requests_per_run=8 caps here.
    assert g.next_runtree_request(root, "exec_root", v) == 8

def test_runtree_lineage_indexes_every_requesting_execution(tmp_path):
    g = Governor()
    v = _vault(tmp_path)
    root = "R"
    for eid in ["R", "R", "c0", "c1", "c0"]:
        g.next_runtree_request(root, eid, v)
    assert set(g.runtree_execution_ids(root, v)) == {"R", "c0", "c1"}

def test_runtree_no_vault_falls_back_to_none(tmp_path):
    g = Governor()
    # empty root → None (caller uses the per-exec count); never raises.
    assert g.next_runtree_request("", "e", _vault(tmp_path)) is None
    assert g.runtree_execution_ids("", _vault(tmp_path)) == []


# ── reconciliation sweeps EVERY ledger in the run-tree at the single terminal ─

def test_terminal_reconciles_all_tree_ledgers(tmp_path):
    g = Governor()
    v = _vault(tmp_path)
    root = "exec_root"
    # Three executions in one tree, each with a SUBAGENT grant in its own ledger
    # (kind=subagent → outcome 'granted', usage N/A). Before the fix only the
    # root's single ledger reconciled (≈1/N); now all N reconcile.
    rows = {
        "exec_root":   {"request_id": "rs_root",   "kind": "subagent", "attempts_before": 2},
        "exec_child0": {"request_id": "rs_child0", "kind": "subagent", "attempts_before": 2},
        "exec_resume": {"request_id": "rs_resume", "kind": "subagent", "attempts_before": 2},
    }
    for eid, req in rows.items():
        g.next_runtree_request(root, eid, v)   # register in lineage
        led = g.ledger_path(eid, v)
        led.parent.mkdir(parents=True, exist_ok=True)
        with led.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"request": req, "verdict": {"decision": "grant"},
                                 "outcome": {"materialised": True},
                                 "execution_id": eid}) + "\n")
    # the single top-level terminal reconciles the WHOLE tree
    also = [e for e in g.runtree_execution_ids(root, v) if e != root]
    n = g.write_outcome_reconciliation(root, set(), run_success=True, vault=v, also_ids=also)
    assert n == 3, f"expected all 3 tree grants reconciled, got {n}"
    led = g.ledger_path(root, v)
    outs = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("event_type") == "request-outcome"]
    assert {o["request_id"]: o["outcome"] for o in outs} == {
        "rs_root": "granted", "rs_child0": "granted", "rs_resume": "granted"}

def test_non_root_terminal_reconciles_whole_tree(tmp_path):
    # Mirrors the v0.9.39 smoke: a suspend→resume chain where the ROOT exec
    # suspends (never reconciles) and a NON-root resume is the genuine terminal.
    # That terminal must still reconcile every grant in the tree — including the
    # root's own grant — via the run-tree lineage (the gate-on-root bug emitted 1
    # event for 8 distinct grants).
    g = Governor()
    v = _vault(tmp_path)
    root = "exec_root"
    chain = ["exec_root", "exec_r1", "exec_r2", "exec_term"]  # exec_term terminates
    for i, eid in enumerate(chain):
        g.next_runtree_request(root, eid, v)
        led = g.ledger_path(eid, v)
        led.parent.mkdir(parents=True, exist_ok=True)
        with led.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "request": {"request_id": f"hreq_{i}", "kind": "subagent",
                            "attempts_before": 2},
                "verdict": {"decision": "grant"},
                "outcome": {"materialised": True}, "execution_id": eid}) + "\n")
    # the genuine terminal is exec_term (NOT the root) — it sweeps the whole tree
    also = [e for e in g.runtree_execution_ids(root, v) if e != "exec_term"]
    n = g.write_outcome_reconciliation("exec_term", set(), run_success=True,
                                       vault=v, also_ids=also)
    assert n == 4, f"a non-root terminal must reconcile all 4 tree grants, got {n}"
    # every grant — including the ROOT's — now has an outcome (written to exec_term)
    led = g.ledger_path("exec_term", v)
    outs = {json.loads(l)["request_id"] for l in led.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("event_type") == "request-outcome"}
    assert outs == {"hreq_0", "hreq_1", "hreq_2", "hreq_3"}


def test_tree_reconciliation_is_idempotent(tmp_path):
    g = Governor()
    v = _vault(tmp_path)
    root = "exec_root"
    g.next_runtree_request(root, root, v)
    led = g.ledger_path(root, v)
    led.parent.mkdir(parents=True, exist_ok=True)
    with led.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"request": {"request_id": "r1", "kind": "subagent"},
                             "verdict": {"decision": "grant"},
                             "outcome": {"materialised": True},
                             "execution_id": root}) + "\n")
    assert g.write_outcome_reconciliation(root, set(), run_success=True, vault=v) == 1
    # a second terminal pass must NOT double-count (Bug 11 per-request_id dedup)
    assert g.write_outcome_reconciliation(root, set(), run_success=True, vault=v) == 0


# ── SubagentFleet threads the run-tree root into its children ────────────────

def test_fleet_root_defaults_to_parent_or_takes_explicit():
    from systemu.runtime.subagent_fleet import SubagentFleet
    cfg = MagicMock(); cfg.delegate_max_concurrent_children = 2
    f = SubagentFleet(parent_execution_id="p1", config=cfg, vault=MagicMock())
    assert f.root_execution_id == "p1"                       # parent IS the root
    f2 = SubagentFleet(parent_execution_id="p1", config=cfg, vault=MagicMock(),
                       root_execution_id="ROOT")
    assert f2.root_execution_id == "ROOT"                    # inherited tree root wins

@pytest.mark.asyncio
async def test_fleet_passes_root_to_child_execute():
    from systemu.runtime.subagent_fleet import SubagentFleet
    cfg = MagicMock(); cfg.delegate_max_concurrent_children = 2
    vault = MagicMock(); vault.create_child_execution_namespace.return_value = None
    seen = {}

    class FakeRuntime:
        def __init__(self, config, vault, audit_namespace=None):
            pass

        async def execute(self, shadow, activity, origin=None, root_execution_id=None):
            seen["root"] = root_execution_id
            return {"status": "success", "summary": "ok"}

    with patch("systemu.runtime.shadow_runtime.ShadowRuntime", FakeRuntime), \
         patch("systemu.runtime.subagent_fleet.build_child_shadow",
               lambda parent, cid: MagicMock()), \
         patch("systemu.runtime.subagent_fleet.build_child_activity",
               lambda pa, task, cid, v: MagicMock()):
        fleet = SubagentFleet(parent_execution_id="p1", config=cfg, vault=vault,
                              root_execution_id="ROOT")
        await fleet.spawn_children(MagicMock(), MagicMock(), ["t1"])
    assert seen.get("root") == "ROOT"   # child adopts the tree root, not its own id


# ── execute() seam: cap + reconciliation wired to the run-tree id ────────────

def test_execute_wires_runtree_cap_and_reconciliation():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert "root_eid = root_execution_id or execution_id" in src      # identity set
    assert "next_runtree_request(" in src                             # cap is tree-wide
    assert "runtree_execution_ids(" in src                            # finalize sweeps tree
    # the root id flows into the fleet spawn + all 4 suspend/park snapshots
    assert src.count("root_execution_id=root_eid") >= 5
    # REGRESSION GUARD (Bug 15 fix): the tree sweep must NOT be gated on the
    # terminal being the root. In a suspend→resume chain the root suspends and
    # never terminates — the genuine terminal is a non-root resume — so gating
    # on root reconciled 8 distinct grants to 1 event. Keep the sweep ungated.
    assert "if execution_id == root_eid:" not in src
