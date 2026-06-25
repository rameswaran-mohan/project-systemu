"""Tests for systemu/interface/harness_review.py — Phase 2.4.

Covers:
  - surface_harness_request: decision posted with correct dedup_key + options
  - load_harness_ledger: reads lines; handles missing/empty file
  - summarize_harness: aggregates counts_by_kind, counts_by_verdict, leases
  - empty/missing ledger → empty summary (no raise)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_request(
    *,
    kind_str: str = "tool",
    request_id: str | None = None,
    rationale: str = "Need a new tool",
    urgency: str = "normal",
    blocking: bool = True,
    spec: dict | None = None,
):
    """Build a minimal HarnessRequest-like object via the real model."""
    from systemu.core.models import HarnessKind, HarnessRequest

    return HarnessRequest(
        request_id=request_id or ("hreq_" + uuid.uuid4().hex[:8]),
        kind=HarnessKind(kind_str),
        spec=spec or {"name": "my_tool"},
        rationale=rationale,
        urgency=urgency,
        blocking=blocking,
    )


def _make_verdict(*, decision_str: str = "escalate", risk_band: str = "high", rationale: str = "risky"):
    """Build a minimal HarnessVerdict-like object via the real model."""
    from systemu.core.models import HarnessDecision, HarnessVerdict, RiskBand

    return HarnessVerdict(
        decision=HarnessDecision(decision_str),
        risk_band=RiskBand(risk_band),
        rationale=rationale,
    )


def _make_vault_mock():
    """Return a MagicMock vault with the minimal decision-queue surface."""
    vault = MagicMock()
    vault.root = None  # will be overridden per test
    # load_index returns [] by default (no pending decisions)
    vault.load_index.return_value = []
    vault.get_decision.side_effect = KeyError("not found")
    return vault


def _ledger_entry(
    *,
    execution_id: str = "exec_001",
    kind: str = "tool",
    decision: str = "grant",
    lease_id: str | None = "lease_abc",
):
    """Build a minimal ledger JSONL dict (mirrors Governor._ledger_entry shape)."""
    return {
        "ts":           "2026-06-08T00:00:00Z",
        "execution_id": execution_id,
        "request": {
            "request_id": "hreq_" + uuid.uuid4().hex[:6],
            "kind":        kind,
            "spec":        {"name": "my_tool"},
            "rationale":   "Need a tool",
            "urgency":     "normal",
            "blocking":    True,
        },
        "verdict": {
            "decision":   decision,
            "risk_band":  "high",
            "rationale":  "risky",
            "lease_id":   lease_id,
        },
        "outcome": {
            "materialised": decision == "grant",
            "lease_id":     lease_id,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
#  surface_harness_request
# ─────────────────────────────────────────────────────────────────────────────

class TestSurfaceHarnessRequest:
    def test_posts_decision_with_correct_dedup_key(self):
        """surface_harness_request posts exactly one decision with the right dedup_key."""
        from systemu.interface.harness_review import surface_harness_request, _HARNESS_OPTIONS

        request = _make_request(request_id="hreq_abc123")
        verdict = _make_verdict()
        vault = _make_vault_mock()
        execution_id = "exec_xyz"

        expected_dedup_key = f"harness:{execution_id}:{request.request_id}"

        captured_calls: list[dict] = []

        def fake_post(*, title, body, options, context, dedup_key=""):
            captured_calls.append({
                "title":     title,
                "body":      body,
                "options":   options,
                "context":   context,
                "dedup_key": dedup_key,
            })
            return "dec_test001"

        with patch(
            "systemu.approval.decision_queue.OperatorDecisionQueue.post",
            side_effect=fake_post,
        ):
            with patch("systemu.interface.harness_review.log_event"):
                decision_id = surface_harness_request(
                    request, verdict, execution_id=execution_id, vault=vault
                )

        assert decision_id == "dec_test001"
        assert len(captured_calls) == 1
        posted = captured_calls[0]
        assert posted["dedup_key"] == expected_dedup_key

    def test_options_are_deny_approve(self):
        """options[0] == 'Deny' (safe default), options are Deny + Approve only.

        "Edit spec" was dropped — the inline "Edit" affordance resolves as
        "Approve" with an amended spec, it is no longer a separate option.
        """
        from systemu.interface.harness_review import surface_harness_request, _HARNESS_OPTIONS

        request = _make_request()
        verdict = _make_verdict()
        vault = _make_vault_mock()

        captured_options: list = []

        def fake_post(*, title, body, options, context, dedup_key=""):
            captured_options.extend(options)
            return "dec_opt_test"

        with patch(
            "systemu.approval.decision_queue.OperatorDecisionQueue.post",
            side_effect=fake_post,
        ):
            with patch("systemu.interface.harness_review.log_event"):
                surface_harness_request(
                    request, verdict, execution_id="exec_1", vault=vault
                )

        assert captured_options[0] == "Deny", "First option must be Deny (safe-by-default)"
        assert "Approve" in captured_options
        assert "Edit spec" not in captured_options

    def test_context_carries_harness_fields(self):
        """Posted context includes execution_id, request_id, harness_kind, etc."""
        from systemu.interface.harness_review import surface_harness_request

        request = _make_request(kind_str="tool", request_id="hreq_ctx01")
        verdict = _make_verdict(decision_str="escalate", risk_band="high")
        vault = _make_vault_mock()

        captured_context: dict = {}

        def fake_post(*, title, body, options, context, dedup_key=""):
            captured_context.update(context)
            return "dec_ctx_test"

        with patch(
            "systemu.approval.decision_queue.OperatorDecisionQueue.post",
            side_effect=fake_post,
        ):
            with patch("systemu.interface.harness_review.log_event"):
                surface_harness_request(
                    request, verdict, execution_id="exec_ctx", vault=vault
                )

        # Re-tagged to a gate (Task 4) so InboxQueue.list_descriptors() surfaces
        # it; the harness-specific fields are preserved alongside the gate marker.
        assert captured_context["kind"] == "gate"
        assert captured_context["gate_type"] == "harness"
        assert captured_context["execution_id"] == "exec_ctx"
        assert captured_context["request_id"] == "hreq_ctx01"
        assert captured_context["harness_kind"] == "tool"
        assert captured_context["risk_band"] == "high"

    def test_dedup_prevents_duplicate_post(self):
        """When a pending decision with the same dedup_key exists, post returns it."""
        from systemu.interface.harness_review import surface_harness_request
        from systemu.approval.decision_queue import OperatorDecision
        from datetime import datetime, timezone

        request = _make_request(request_id="hreq_dup")
        verdict = _make_verdict()
        vault = _make_vault_mock()
        execution_id = "exec_dup"
        expected_dedup_key = f"harness:{execution_id}:{request.request_id}"

        existing = OperatorDecision(
            id="dec_existing",
            title="already posted",
            body="",
            options=["Deny", "Approve", "Edit spec"],
            dedup_key=expected_dedup_key,
            status="pending",
            created_at=datetime.now(tz=timezone.utc),
        )
        # Simulate vault returning existing pending decision.
        # side_effect takes precedence over return_value in MagicMock, so clear
        # the KeyError side_effect that _make_vault_mock set before setting the value.
        vault.load_index.return_value = [existing.to_dict()]
        vault.get_decision.side_effect = None
        vault.get_decision.return_value = existing

        with patch("systemu.interface.harness_review.log_event"):
            decision_id = surface_harness_request(
                request, verdict, execution_id=execution_id, vault=vault
            )

        # Should get the existing id back, not create a new one
        assert decision_id == "dec_existing"

    def test_returns_decision_id_string(self):
        """surface_harness_request always returns a non-empty string."""
        from systemu.interface.harness_review import surface_harness_request

        request = _make_request()
        verdict = _make_verdict()
        vault = _make_vault_mock()

        with patch(
            "systemu.approval.decision_queue.OperatorDecisionQueue.post",
            return_value="dec_ret001",
        ):
            with patch("systemu.interface.harness_review.log_event"):
                result = surface_harness_request(
                    request, verdict, execution_id="exec_r", vault=vault
                )

        assert isinstance(result, str)
        assert result == "dec_ret001"


# ─────────────────────────────────────────────────────────────────────────────
#  load_harness_ledger
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadHarnessLedger:
    def test_returns_empty_for_missing_file(self, tmp_path):
        """Missing ledger file → empty list, no exception."""
        from systemu.interface.harness_review import load_harness_ledger

        vault = MagicMock()
        vault.root = str(tmp_path)

        result = load_harness_ledger("exec_nonexistent", vault)
        assert result == []

    def test_reads_valid_jsonl(self, tmp_path):
        """load_harness_ledger reads all valid JSONL lines."""
        from systemu.interface.harness_review import load_harness_ledger

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        ledger_file = ledger_dir / "exec_001.jsonl"

        entries = [
            _ledger_entry(execution_id="exec_001", kind="tool",  decision="grant"),
            _ledger_entry(execution_id="exec_001", kind="skill", decision="deny"),
            _ledger_entry(execution_id="exec_001", kind="tool",  decision="escalate", lease_id=None),
        ]
        with ledger_file.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        result = load_harness_ledger("exec_001", vault)
        assert len(result) == 3
        assert result[0]["verdict"]["decision"] == "grant"
        assert result[1]["request"]["kind"] == "skill"

    def test_skips_malformed_lines(self, tmp_path):
        """Malformed JSONL lines are skipped; valid lines still returned."""
        from systemu.interface.harness_review import load_harness_ledger

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        ledger_file = ledger_dir / "exec_bad.jsonl"

        good_entry = _ledger_entry(execution_id="exec_bad", kind="tool", decision="grant")
        with ledger_file.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(good_entry) + "\n")
            fh.write("NOT VALID JSON !!!\n")
            fh.write(json.dumps(good_entry) + "\n")

        result = load_harness_ledger("exec_bad", vault)
        assert len(result) == 2  # bad line skipped

    def test_empty_ledger_file(self, tmp_path):
        """Empty ledger file → empty list."""
        from systemu.interface.harness_review import load_harness_ledger

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "exec_empty.jsonl").touch()

        result = load_harness_ledger("exec_empty", vault)
        assert result == []

    def test_vault_none_falls_back_to_default_path(self, tmp_path):
        """When vault is None, load_harness_ledger falls back without raising."""
        from systemu.interface.harness_review import load_harness_ledger

        # The fallback path won't exist in tmp_path, so we just get []
        result = load_harness_ledger("exec_no_vault", None)
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
#  summarize_harness
# ─────────────────────────────────────────────────────────────────────────────

class TestSummarizeHarness:
    def test_empty_summary_for_missing_ledger(self, tmp_path):
        """Missing ledger → empty summary dict with zero counts (no raise)."""
        from systemu.interface.harness_review import summarize_harness

        vault = MagicMock()
        vault.root = str(tmp_path)

        result = summarize_harness("exec_missing", vault)

        assert result["total"] == 0
        assert result["counts_by_kind"] == {}
        assert result["counts_by_verdict"] == {}
        assert result["leases"] == []
        assert result["execution_id"] == "exec_missing"

    def test_aggregates_counts_by_kind(self, tmp_path):
        """counts_by_kind tallies correctly across entries."""
        from systemu.interface.harness_review import summarize_harness

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        ledger_file = ledger_dir / "exec_ck.jsonl"

        entries = [
            _ledger_entry(kind="tool",    decision="grant"),
            _ledger_entry(kind="tool",    decision="deny"),
            _ledger_entry(kind="skill",   decision="escalate", lease_id=None),
            _ledger_entry(kind="compute", decision="grant"),
        ]
        with ledger_file.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        result = summarize_harness("exec_ck", vault)

        assert result["total"] == 4
        assert result["counts_by_kind"]["tool"] == 2
        assert result["counts_by_kind"]["skill"] == 1
        assert result["counts_by_kind"]["compute"] == 1

    def test_aggregates_counts_by_verdict(self, tmp_path):
        """counts_by_verdict tallies correctly across entries."""
        from systemu.interface.harness_review import summarize_harness

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        ledger_file = ledger_dir / "exec_cv.jsonl"

        entries = [
            _ledger_entry(decision="grant"),
            _ledger_entry(decision="grant"),
            _ledger_entry(decision="deny"),
            _ledger_entry(decision="escalate", lease_id=None),
        ]
        with ledger_file.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        result = summarize_harness("exec_cv", vault)

        assert result["counts_by_verdict"]["grant"] == 2
        assert result["counts_by_verdict"]["deny"] == 1
        assert result["counts_by_verdict"]["escalate"] == 1

    def test_collects_lease_ids(self, tmp_path):
        """leases list includes lease_ids from granted/materialised entries."""
        from systemu.interface.harness_review import summarize_harness

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        ledger_file = ledger_dir / "exec_lease.jsonl"

        entries = [
            _ledger_entry(decision="grant", lease_id="lease_aaa"),
            _ledger_entry(decision="grant", lease_id="lease_bbb"),
            _ledger_entry(decision="deny",  lease_id=None),
        ]
        with ledger_file.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        result = summarize_harness("exec_lease", vault)

        assert "lease_aaa" in result["leases"]
        assert "lease_bbb" in result["leases"]
        assert len(result["leases"]) == 2

    def test_no_duplicate_lease_ids(self, tmp_path):
        """Duplicate lease_ids in ledger appear only once in summary."""
        from systemu.interface.harness_review import summarize_harness

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        ledger_file = ledger_dir / "exec_dedup.jsonl"

        entries = [
            _ledger_entry(decision="grant", lease_id="lease_same"),
            _ledger_entry(decision="grant", lease_id="lease_same"),  # duplicate
        ]
        with ledger_file.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        result = summarize_harness("exec_dedup", vault)
        assert result["leases"].count("lease_same") == 1

    def test_empty_ledger_file(self, tmp_path):
        """Empty ledger file → empty summary (no raise)."""
        from systemu.interface.harness_review import summarize_harness

        vault = MagicMock()
        vault.root = str(tmp_path)

        ledger_dir = tmp_path / "harness_ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "exec_empty2.jsonl").touch()

        result = summarize_harness("exec_empty2", vault)
        assert result["total"] == 0
        assert result["counts_by_kind"] == {}
        assert result["counts_by_verdict"] == {}

    def test_vault_none_returns_empty(self):
        """vault=None → empty summary, no raise."""
        from systemu.interface.harness_review import summarize_harness

        result = summarize_harness("exec_novaul", None)
        assert result["total"] == 0
        assert result["execution_id"] == "exec_novaul"
