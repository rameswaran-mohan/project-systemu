"""CONC-MAP v1 — writer-ownership assertions (DEC-10 concurrency guardrail).

These tests are the *teeth* of `docs/CONC-MAP.md`: they pin the single-writer /
known-writer-set discipline for the durable stores that have one, so that adding a
concurrent writer to such a store **fails CI** — forcing a conscious CONC-MAP update
and the DEC-10 concurrency review before the new writer ships.

Why this is the R-A12 precondition (SEQ-2 / amended DEC-10): R-A12 adds an
`external_wait_reconciler` — a new background writer on `ExecutionSnapshot`, the
highest-risk store. `test_execution_snapshot_writer_set` will FAIL the moment that
reconciler calls `write_snapshot`, until it is added to the allowlist here (and to
CONC-MAP.md) — which is exactly the review checkpoint the guardrail exists to force.

The scan is deliberately source-level (grep-style), not a runtime probe: the invariant
is "who is *allowed* to write this store", and that lives in the code's call graph.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_SYSTEMU = Path(__file__).resolve().parent.parent / "systemu"


def _relpath(p: Path) -> str:
    return p.relative_to(_SYSTEMU).as_posix()


def _caller_files(call: str, *, scan_root: Path, def_file: str) -> set[str]:
    """Every systemu/ file (relative posix path) that CALLS `call`.

    A "call" is a line containing the substring where it is NOT a `def`/`async def`
    of that name and NOT a comment line. The definition file itself is excluded.
    """
    callers: set[str] = set()
    def_re = re.compile(r"^\s*(async\s+)?def\s+" + re.escape(call.rstrip("(")) + r"\b")
    for py in scan_root.rglob("*.py"):
        rel = _relpath(py)
        if rel == def_file:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if call in line and not def_re.match(line):
                callers.add(rel)
                break
    return callers


# store label -> {call, allowed writer files, def file, optional scan sub-dir, note}
WRITER_OWNERSHIP = {
    "ExecutionSnapshot (data/audit/exec_*/resume_snapshot.json)": {
        "call": "write_snapshot(",
        "allowed": {
            "runtime/shadow_runtime.py",      # the shadow execution loop (per-run thread)
            "runtime/resume_on_decision.py",  # __STUCK_ANSWER__ sticky (parked-run resume)
            "runtime/supervisor.py",          # __HARNESS_GRANT__ sticky (parked-run resume)
            # R-A12a external_wait_reconciler (DEC-10 reviewed): writes pending_waits
            # ONLY on PARKED runs, per-execution_id invariant
            "scheduler/jobs.py",
        },
        "def": "runtime/execution_snapshot.py",
        "note": ("DEC-10 R-A12 GUARD — external_wait_reconciler (scheduler/jobs.py) is "
                 "the reviewed 4th writer: writes pending_waits ONLY on PARKED runs "
                 "(per-execution_id invariant). Any FURTHER writer needs the same review."),
    },
    "OnTheTable (<root>/table/items.json)": {
        "call": "save_items(",
        "allowed": {"runtime/table_reconciler.py"},  # the sole 60s table reconciler
        "def": "runtime/table_store.py",
        "note": "Clean single writer.",
    },
    "R-A13.5 ask corpus (<root>/audit/ask_corpus.jsonl)": {
        "call": "record_ask(",
        "allowed": {"runtime/shadow_runtime.py"},    # the shadow exec loop, at the ask point
        "def": "runtime/replay_metrics.py",
        "note": ("APPEND-only deterministic ask corpus (observability; the §10 avoidable-ask "
                 "signal). Appends are SERIALIZED (process lock + best-effort OS file lock, "
                 "then one os.write to an O_APPEND fd) so concurrent shadow runs interleave "
                 "whole lines AND lose none. Any further writer needs a DEC-10 review."),
    },
    "R-A16 answer-linked ask corpus (<root>/audit/ask_avoidable.jsonl)": {
        "call": "record_ask_avoidable(",
        "allowed": {
            "runtime/elicitation.py",   # the pre-loop B10 rail — Requirement + accept
                                        # envelope in one frame (shadow exec thread)
            "scheduler/jobs.py",        # the harness-grant reconciler — the bundled
                                        # scope card's answer-time join (daemon thread)
        },
        "def": "runtime/replay_metrics.py",
        "note": ("R-A16/G-LEARN §5.9 AskWasAvoidable events. APPEND-only, "
                 "observability-only (never raises, never affects a run). Appends are "
                 "SERIALIZED (process lock + best-effort OS file lock, then one "
                 "os.write to an O_APPEND fd) so the two writer threads interleave "
                 "whole lines AND lose none — a bare buffered append silently dropped "
                 "~4% of rows under load. DELIBERATELY a separate file from "
                 "ask_corpus.jsonl: folding answer-linked rows in would have them "
                 "silently counted by avoidable_ask_report's no-attempt proxy (absent "
                 "attempt fields default to 0), corrupting the shipped DEC-7 metric. "
                 "Records REFS ONLY — credential/secret-mode asks are excluded at both "
                 "requirement_snapshot() and record_ask_avoidable(). Any further "
                 "writer needs a DEC-10 review."),
    },
    "CapabilitySlots index (<root>/capabilities/capability_index.json)": {
        "call": "reconcile_index(",
        "allowed": {"scheduler/daemon.py"},          # the sole 60s capability reconciler job
        "def": "runtime/capability_index.py",
        "note": ("R-CAP1 CAP-0.1: reconcile_index is DERIVE-ONLY (rebuilds the whole file "
                 "from {Tool catalog ∪ mcp enabled_tools}; no RMW). The daemon job is the "
                 "sole writer; read-only callers use find_tools(live=True) which derives in "
                 "memory and never writes. Any further writer needs a DEC-10 review."),
    },
    "Fatigue metrics (<root>/metrics/metrics.json) — resolution side": {
        "call": "record_resolution(",
        "allowed": {"approval/decision_queue.py", "interface/command/inbox.py"},
        "def": "runtime/metrics_store.py",
        "note": "Resolution-side writer set; creation side is incr() on the exec thread.",
    },
    "S4 shadow meter (<root>/metrics/metrics.json — s4_shadow bucket)": {
        "call": "incr_s4_shadow_meter(",
        "allowed": {"runtime/shadow_runtime.py"},  # the record-only meter at the credit seam
        "def": "runtime/metrics_store.py",
        "note": ("R-A13b-1 park-surface meter: the SOLE writer is the shadow exec loop's "
                 "credit-seam meter branch (record-only, same single writer thread as incr()). "
                 "Any further writer needs a DEC-10 review + this allowlist update."),
    },
    "Cost ledger (in-process — systemu.runtime.costing._LEDGER)": {
        "call": "record_usage(",
        "allowed": {"core/llm_router.py"},  # the router's token-capture hook, sole writer
        "def": "runtime/costing.py",
        "note": ("R-P3a cost accumulator: the SOLE writer is the LLM router's "
                 "per-call token-capture hook (_record_usage_safe → record_usage), "
                 "reading the ambient execution_id. In-process ledger (not a durable "
                 "vault store). Any further writer needs a DEC-10 review + this update."),
    },
    "R-P1 resolve audit (<root>/messaging/resolve_audit.jsonl)": {
        "call": "_audit(",
        "allowed": {"messaging/decision_bridge.py"},
        # `_audit` is defined AND called inside decision_bridge.py — the def IS the
        # (only) writer, so don't exclude it; scope to messaging/ since `_audit` is a
        # common private name used by unrelated modules elsewhere.
        "def": "",
        "scan_subdir": "messaging",
        "note": "Single-writer append on the telegram-gateway thread.",
    },
    "World-model facts (<root>/world_model/facts.json)": {
        "call": "put_facts(",
        "allowed": {"runtime/world_model_populator.py"},   # the WRITE-ONLY projector
        "def": "runtime/world_model.py",
        "note": ("R-W1 §5.11.a. The projector is the SOLE writer, called from the shadow "
                 "exec thread's post-survey step. Concurrent RUNS are concurrent writers, "
                 "but the write is idempotent-convergent (ids derive from (kind, value)), "
                 "so a lost update costs a re-confirmation, never a fact. W-A's final "
                 "slice made the store READABLE (situational_inventory composes a view, "
                 "world_tools exposes the query tool) — both are read-only and neither "
                 "may appear here. The R-W4 world gardener is the standing writer DEC-10 "
                 "names explicitly: it needs its own review AND this allowlist update."),
    },
    "World-model negatives (<root>/world_model/negatives.json)": {
        "call": "put_negative(",
        "allowed": {"runtime/world_model_discovery.py"},   # the discovery-miss loop
        "def": "runtime/world_model.py",
        "note": ("R-W1 WM-2 'searched and not found'. Unlocked RMW across concurrent "
                 "runs; a lost update drops a SUPPRESSION, whose cost is a repeated "
                 "search and never a missed one. A writer that could assert absence "
                 "from a NEW source needs a DEC-10 review — NegativeFact refuses "
                 "content_derived at construction, and that must stay the only way in."),
    },
    "World-model survey watermarks (<root>/world_model/surveys.json)": {
        "call": "record_survey(",
        "allowed": {"runtime/world_model_populator.py"},
        "def": "runtime/world_model.py",
        "note": ("R-W1 read-side staleness. Same single writer/call as put_facts. A lost "
                 "watermark makes staleness_of UNDER-report, its documented safe "
                 "direction. A second writer would let one survey claim another's "
                 "coverage, which is the one error that turns a live fact into "
                 "'may be gone' — hence the pin."),
    },
    "U-12-Outbox (<root>/Outbox/<yyyy-mm-dd>-<slug>/)": {
        "call": "write_outbox_for_run(",
        "allowed": {
            "pipelines/direct_task.py",  # workflow-lane terminal
            "pipelines/quick_task.py",   # quick-lane terminal (the DEFAULT lane)
        },
        "def": "runtime/outbox.py",
        "note": ("R-UTL1 U-12. The two LANE TERMINALS are the only writers. Each "
                 "run writes its OWN uniquely-named folder (_unique_dir), so "
                 "concurrent runs never share a path and no lock is needed — that "
                 "invariant is what makes a third caller dangerous: it could drop "
                 "artifacts into a folder another writer is about to seal with "
                 ".done, which is exactly the torn-read `.done` exists to prevent."),
    },
}


@pytest.mark.parametrize("store,spec", WRITER_OWNERSHIP.items(),
                         ids=[k.split(" ")[0] for k in WRITER_OWNERSHIP])
def test_writer_set_matches_conc_map(store, spec):
    scan_root = _SYSTEMU / spec["scan_subdir"] if spec.get("scan_subdir") else _SYSTEMU
    callers = _caller_files(spec["call"], scan_root=scan_root, def_file=spec["def"])
    unexpected = callers - spec["allowed"]
    assert not unexpected, (
        f"\nCONC-MAP writer-ownership VIOLATION for: {store}\n"
        f"  New writer(s) of `{spec['call']}` appeared in: {sorted(unexpected)}\n"
        f"  CONC-MAP declares the writer set as: {sorted(spec['allowed'])}\n"
        f"  {spec['note']}\n"
        f"  -> Adding a concurrent writer to this store requires updating docs/CONC-MAP.md\n"
        f"     AND running the DEC-10 concurrency review (single-writer / serialization).\n"
        f"     If this writer is legitimate and reviewed, add its file to the allowlist here."
    )
    # Also assert we didn't LOSE a declared writer (keeps the map honest as code moves).
    missing = spec["allowed"] - callers
    assert not missing, (
        f"CONC-MAP lists writer(s) {sorted(missing)} for {store} but they no longer call "
        f"`{spec['call']}` — the map is stale; update docs/CONC-MAP.md + the allowlist."
    )


# --- atomic-write invariant: every guarded side-store must write via tmp + os.replace ---
_ATOMIC_WRITE_STORES = {
    "runtime/execution_snapshot.py",  # write_snapshot
    "runtime/table_store.py",         # _write_atomic
    "runtime/command_approvals.py",   # _save
    "runtime/metrics_store.py",       # _write_atomic
    "runtime/dashboard_auth.py",      # LockoutStore._save + _write_secret_file
    "runtime/outbox.py",              # _write_atomic (receipt/.done/FAILED note)
    "runtime/world_model.py",         # _write_atomic (facts/negatives/surveys)
}


@pytest.mark.parametrize("rel", sorted(_ATOMIC_WRITE_STORES))
def test_side_store_writes_are_atomic(rel):
    """Durable side-stores must use tmp-file + os.replace so a crash mid-write can never
    leave a torn file. (os.replace is atomic on the same filesystem on both POSIX+NT.)"""
    text = (_SYSTEMU / rel).read_text(encoding="utf-8", errors="replace")
    assert "os.replace(" in text, (
        f"{rel} is a durable side-store but does not use os.replace() for an atomic "
        f"write — a mid-write crash could leave a torn file. Keep the tmp+replace pattern."
    )
