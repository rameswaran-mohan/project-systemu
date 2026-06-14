"""Plan 0 Build 2 (Tasks 2.2/2.3/2.4) — pull-decision provenance on the
Governor: decided_by="llm" plumbing + Tool.forged_by_execution_id.

Asserts:
  * Task 2.3 — every HarnessVerdict the LLM judge resolves (GRANT / DENY /
    ESCALATE-with-appended-rationale) carries decided_by="llm";
  * Task 2.3 — _ledger_entry surfaces verdict.decided_by into the verdict
    sub-dict (an "llm"-decided GRANT's ledger row reads decided_by="llm");
  * Task 2.4 — _provision_tool stamps forged_by_execution_id=execution_id on
    the Tool it constructs and saves to the vault.

No network: the LLM judge symbol and the tool_forge spine are monkeypatched.
Fixtures mirror tests/test_v0_9_7_harness_judge.py and test_v0_9_7_governor.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from systemu.core.models import (
    HarnessDecision,
    HarnessKind,
    HarnessRequest,
    HarnessVerdict,
    RiskBand,
    Tool,
    ToolStatus,
    ToolType,
)
from systemu.runtime import harness_judge
from systemu.runtime.governor import Governor


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _req(kind: HarnessKind, spec: dict | None = None, **kwargs) -> HarnessRequest:
    return HarnessRequest(kind=kind, spec=spec or {}, **kwargs)


def _fake_tool(name: str = "test_tool") -> Tool:
    return Tool(
        id="tool_abc123",
        name=name,
        description="A fake tool for tests",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.FORGED,
    )


def _mock_vault(tmp_path: Path):
    vault = MagicMock()
    vault.root = str(tmp_path)
    vault.save_tool.return_value = None
    vault.get_tool.return_value = _fake_tool()
    return vault


def _mock_config():
    cfg = MagicMock()
    cfg.vault_dir = "data/systemu/vault"
    return cfg


def _grant_verdict(lease_id: str | None = None) -> HarnessVerdict:
    return HarnessVerdict(
        request_id="hreq_test",
        decision=HarnessDecision.GRANT,
        risk_band=RiskBand.LOW,
        rationale="auto-grant for tests",
        lease_id=lease_id,
    )


class _Cfg:
    verifier_tier = 1


def _judge_governor() -> Governor:
    """Governor wired so a new-skill request needs LLM judgment."""
    gov = Governor(config={
        "auto_grant_skill": False,     # new-skill request → needs_llm_judgment
        "llm_judge_enabled": True,
        "allowed_resources": {"vault/tools", "vault/skills"},
    })
    gov.config = _Cfg()
    return gov


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2.3 — decided_by="llm" on every judge-resolved verdict
# ─────────────────────────────────────────────────────────────────────────────

class TestDecidedByLlm:
    def test_judge_grant_sets_decided_by_llm(self, monkeypatch):
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "GRANT", "confidence": 0.9, "rationale": "clearly safe"},
        )
        gov = _judge_governor()
        req = _req(HarnessKind.SKILL, spec={"name": "new_skill_text"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.GRANT
        assert verdict.decided_by == "llm"

    def test_judge_deny_sets_decided_by_llm(self, monkeypatch):
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "DENY", "confidence": 0.85, "rationale": "outside policy"},
        )
        gov = _judge_governor()
        req = _req(HarnessKind.SKILL, spec={"name": "deny_me"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.DENY
        assert verdict.decided_by == "llm"

    def test_judge_escalate_sets_decided_by_llm(self, monkeypatch):
        """The judge touched this verdict (appended its rationale) → decided_by=llm,
        even though the decision stays ESCALATE."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "ESCALATE", "confidence": 0.5, "rationale": "unsure"},
        )
        gov = _judge_governor()
        req = _req(HarnessKind.SKILL, spec={"name": "unsure_skill"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.ESCALATE
        assert verdict.decided_by == "llm"

    def test_deterministic_path_stays_deterministic(self):
        """A verdict the arbiter resolves on its own keeps decided_by=deterministic."""
        gov = Governor(config={
            "auto_grant_access": False,
            "allowed_resources": {"vault/tools"},
        })
        req = _req(HarnessKind.ACCESS, spec={"resource": "vault/tools", "access_type": "read"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.GRANT
        assert verdict.decided_by == "deterministic"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2.3 — _ledger_entry surfaces decided_by
# ─────────────────────────────────────────────────────────────────────────────

class TestLedgerEntryDecidedBy:
    def test_llm_verdict_decided_by_in_ledger_entry(self):
        """_ledger_entry copies verdict.decided_by into the verdict sub-dict."""
        gov = Governor()
        req = _req(HarnessKind.SKILL, spec={"name": "x"})
        verdict = HarnessVerdict(
            request_id=req.request_id,
            decision=HarnessDecision.GRANT,
            risk_band=RiskBand.MEDIUM,
            rationale="[judged_by=llm] safe",
            lease_id="lease_xyz",
            decided_by="llm",
        )
        entry = gov._ledger_entry(req, verdict, {"materialised": True}, "exec_le")
        assert entry["verdict"]["decided_by"] == "llm"

    def test_default_verdict_decided_by_in_ledger_entry(self):
        gov = Governor()
        req = _req(HarnessKind.SKILL, spec={"name": "x"})
        verdict = HarnessVerdict(
            request_id=req.request_id,
            decision=HarnessDecision.DENY,
            risk_band=RiskBand.HIGH,
            rationale="denied",
        )
        entry = gov._ledger_entry(req, verdict, {"materialised": False}, "exec_le2")
        assert entry["verdict"]["decided_by"] == "deterministic"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2.4 — _provision_tool stamps forged_by_execution_id
# ─────────────────────────────────────────────────────────────────────────────

class TestForgedByExecutionId:
    def test_provision_tool_stamps_execution_id(self, tmp_path):
        """The Tool constructed + saved by _provision_tool carries
        forged_by_execution_id == execution_id."""
        gov = Governor(config={"allowed_resources": {"vault/tools"}})
        vault = _mock_vault(tmp_path)
        exec_id = "exec_provenance_tool"
        req = _req(
            HarnessKind.TOOL,
            spec={"name": "csv_reader", "description": "Read CSV files"},
            rationale="Need CSV parsing",
        )
        verdict = _grant_verdict(lease_id="lease_provenanc1")

        with patch("systemu.runtime.governor.forge_proposed_tools") as mock_forge:
            mock_forge.return_value = [_fake_tool("csv_reader")]
            result = gov._provision_tool(
                req, verdict, vault=vault, config=_mock_config(), execution_id=exec_id,
            )

        assert result["materialised"] is True
        # The Tool record built before forge is the one passed to vault.save_tool.
        vault.save_tool.assert_called_once()
        saved_tool = vault.save_tool.call_args.args[0]
        assert isinstance(saved_tool, Tool)
        assert saved_tool.forged_by_execution_id == exec_id
