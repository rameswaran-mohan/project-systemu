"""v0.9.7 Phase 1.3 — Governor: arbitrate + materialise + ledger + leases.

No network.  tool_forge spine is monkeypatched throughout.

Coverage:
  - arbitrate(TOOL new-code)          → ESCALATE (delegates to arbiter correctly)
  - arbitrate(ACCESS read whitelisted)→ GRANT
  - materialise(GRANT, TOOL)          → calls forge spine; materialised + lease_id; ledger written
  - materialise(non-GRANT)            → no-op {"materialised": False}
  - materialise(GRANT, SKILL)         → Phase 1 stub "not implemented"
  - forge raises                      → materialise returns materialised=False (never raises)
  - revoke_leases(execution_id)       → marks leases revoked
  - ledger file is written + reloadable as JSONL
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

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
from systemu.runtime.governor import Governor


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _req(kind: HarnessKind, spec: dict | None = None, **kwargs) -> HarnessRequest:
    return HarnessRequest(kind=kind, spec=spec or {}, **kwargs)


def _grant_verdict(lease_id: str | None = None) -> HarnessVerdict:
    return HarnessVerdict(
        request_id="hreq_test",
        decision=HarnessDecision.GRANT,
        risk_band=RiskBand.LOW,
        rationale="auto-grant for tests",
        lease_id=lease_id,
    )


def _escalate_verdict() -> HarnessVerdict:
    return HarnessVerdict(
        request_id="hreq_test",
        decision=HarnessDecision.ESCALATE,
        risk_band=RiskBand.HIGH,
        rationale="HIGH risk — escalate",
    )


def _deny_verdict() -> HarnessVerdict:
    return HarnessVerdict(
        request_id="hreq_test",
        decision=HarnessDecision.DENY,
        risk_band=RiskBand.HIGH,
        rationale="denied",
    )


def _fake_tool(name: str = "test_tool") -> Tool:
    return Tool(
        id="tool_abc123",
        name=name,
        description="A fake tool for tests",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.FORGED,
    )


def _mock_vault(tmp_path: Path):
    """A minimal mock Vault with a real root directory."""
    vault = MagicMock()
    vault.root = str(tmp_path)
    vault.save_tool.return_value = None
    vault.get_tool.return_value = _fake_tool()
    return vault


def _mock_config():
    cfg = MagicMock()
    cfg.vault_dir = "data/systemu/vault"
    return cfg


def _default_governor(config_dict: dict | None = None) -> Governor:
    """Governor with permissive policy for most test scenarios."""
    cfg = config_dict or {
        "auto_grant_tool": False,
        "auto_grant_skill": True,
        "auto_grant_access": False,
        "auto_grant_compute": True,
        "auto_grant_subagent": False,
        "allowed_resources": {"vault/tools", "vault/skills"},
        "max_requests_per_run": 8,
    }
    return Governor(config=cfg)


# ─────────────────────────────────────────────────────────────────────────────
#  1. arbitrate — delegation to harness_arbiter
# ─────────────────────────────────────────────────────────────────────────────

class TestArbitrate:
    def test_tool_new_code_escalates(self):
        """arbitrate(TOOL new-code) → ESCALATE (delegates to arbiter)."""
        gov = _default_governor()
        req = _req(HarnessKind.TOOL, spec={"name": "brand_new_tool"}, rationale="need geo")
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.ESCALATE
        assert verdict.risk_band == RiskBand.HIGH

    def test_access_read_whitelisted_grants(self):
        """arbitrate(ACCESS read whitelisted) → GRANT."""
        gov = _default_governor()
        req = _req(
            HarnessKind.ACCESS,
            spec={"resource": "vault/tools", "access_type": "read"},
        )
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.GRANT
        assert verdict.risk_band == RiskBand.LOW

    def test_arbitrate_returns_harness_verdict(self):
        """arbitrate() must always return a HarnessVerdict instance."""
        gov = _default_governor()
        req = _req(HarnessKind.INPUT, spec={"question": "What?"})
        verdict = gov.arbitrate(req)
        assert isinstance(verdict, HarnessVerdict)

    def test_request_id_propagated(self):
        """The verdict's request_id must match the request's request_id."""
        gov = _default_governor()
        req = _req(HarnessKind.COMPUTE, spec={"budget_fraction": 0.5})
        verdict = gov.arbitrate(req)
        assert verdict.request_id == req.request_id

    def test_needs_llm_judgment_kept_as_escalate(self, monkeypatch):
        """When arbiter flags needs_llm_judgment, Governor keeps ESCALATE (no LLM in Phase 1)."""
        gov = _default_governor()
        # Force skill with auto_grant=False → needs_llm_judgment=True → ESCALATE
        gov.policy.auto_grant_skill = False
        req = _req(HarnessKind.SKILL, spec={"name": "some_new_skill"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.ESCALATE


# ─────────────────────────────────────────────────────────────────────────────
#  2. materialise — non-GRANT is always a no-op
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseNonGrant:
    def test_escalate_verdict_is_noop(self, tmp_path):
        gov = _default_governor()
        req = _req(HarnessKind.TOOL, spec={"name": "x"})
        verdict = _escalate_verdict()
        result = gov.materialise(req, verdict, vault=_mock_vault(tmp_path),
                                 config=_mock_config(), execution_id="exec_001")
        assert result["materialised"] is False

    def test_deny_verdict_is_noop(self, tmp_path):
        gov = _default_governor()
        req = _req(HarnessKind.TOOL, spec={"name": "x"})
        verdict = _deny_verdict()
        result = gov.materialise(req, verdict, vault=_mock_vault(tmp_path),
                                 config=_mock_config(), execution_id="exec_001")
        assert result["materialised"] is False

    def test_non_grant_still_writes_ledger(self, tmp_path):
        """Even on non-GRANT, ledger must be written."""
        gov = _default_governor()
        req = _req(HarnessKind.TOOL, spec={"name": "x"})
        verdict = _deny_verdict()
        exec_id = "exec_ledger_test"
        gov.materialise(req, verdict, vault=_mock_vault(tmp_path),
                        config=_mock_config(), execution_id=exec_id)
        ledger = gov.ledger_path(exec_id, _mock_vault(tmp_path))
        assert ledger.exists(), "Ledger must be created even on non-GRANT"
        lines = ledger.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1


# ─────────────────────────────────────────────────────────────────────────────
#  3. materialise — GRANT + TOOL → forge spine called
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseTool:
    def _run_with_mock_forge(self, tmp_path: Path, forge_return=None, side_effect=None):
        """Helper: patch forge_proposed_tools and call materialise."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        config = _mock_config()
        exec_id = "exec_tool_test"
        req = _req(
            HarnessKind.TOOL,
            spec={
                "name": "csv_reader",
                "description": "Read CSV files",
                "tool_type": "python_function",
                "parameters_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "return_schema": {"type": "array"},
                "implementation_notes": "Use pandas",
            },
            rationale="Need CSV parsing",
        )
        verdict = _grant_verdict(lease_id="lease_abc1234567")

        mock_forged = forge_return if forge_return is not None else [_fake_tool("csv_reader")]

        with patch("systemu.runtime.governor.forge_proposed_tools") as mock_forge:
            if side_effect is not None:
                mock_forge.side_effect = side_effect
            else:
                mock_forge.return_value = mock_forged
            result = gov.materialise(req, verdict, vault=vault, config=config, execution_id=exec_id)

        return gov, result, exec_id, vault, mock_forge

    def test_grant_tool_calls_forge_spine(self, tmp_path):
        """materialise(GRANT, TOOL) → forge spine invoked exactly once."""
        _, result, _, _, mock_forge = self._run_with_mock_forge(tmp_path)
        mock_forge.assert_called_once()

    def test_grant_tool_returns_materialised_true(self, tmp_path):
        """materialise(GRANT, TOOL) → {"materialised": True}."""
        _, result, _, _, _ = self._run_with_mock_forge(tmp_path)
        assert result["materialised"] is True

    def test_grant_tool_returns_lease_id(self, tmp_path):
        """materialise(GRANT, TOOL) → result contains lease_id."""
        _, result, _, _, _ = self._run_with_mock_forge(tmp_path)
        assert "lease_id" in result
        assert result["lease_id"]  # non-empty

    def test_grant_tool_returns_tool_name(self, tmp_path):
        """materialise(GRANT, TOOL) → result contains the tool name."""
        _, result, _, _, _ = self._run_with_mock_forge(tmp_path)
        assert result.get("tool") == "csv_reader"

    def test_grant_tool_registers_lease_in_process(self, tmp_path):
        """materialise(GRANT, TOOL) → lease is registered in governor._leases."""
        gov, result, exec_id, _, _ = self._run_with_mock_forge(tmp_path)
        lease_id = result["lease_id"]
        assert gov.get_lease(lease_id) is not None
        lease = gov.get_lease(lease_id)
        assert lease["execution_id"] == exec_id
        assert lease["revoked"] is False

    def test_grant_tool_writes_ledger(self, tmp_path):
        """materialise(GRANT, TOOL) → JSONL ledger entry written."""
        gov, result, exec_id, vault, _ = self._run_with_mock_forge(tmp_path)
        ledger = gov.ledger_path(exec_id, vault)
        assert ledger.exists()
        entries = [json.loads(line) for line in ledger.read_text(encoding="utf-8").strip().splitlines()]
        assert len(entries) >= 1
        entry = entries[0]
        assert entry["execution_id"] == exec_id
        assert entry["verdict"]["decision"] == "grant"
        assert entry["outcome"]["materialised"] is True

    def test_forge_raises_returns_materialised_false(self, tmp_path):
        """When forge raises, materialise returns materialised=False — never raises."""
        _, result, _, _, _ = self._run_with_mock_forge(
            tmp_path,
            side_effect=RuntimeError("LLM unavailable"),
        )
        assert result["materialised"] is False
        assert "forge failed" in result.get("reason", "").lower()

    def test_forge_returns_empty_list_returns_materialised_false(self, tmp_path):
        """When forge returns [], materialise returns materialised=False."""
        _, result, _, _, _ = self._run_with_mock_forge(tmp_path, forge_return=[])
        assert result["materialised"] is False


# ─────────────────────────────────────────────────────────────────────────────
#  4. materialise — GRANT + SKILL / ACCESS / COMPUTE / SUBAGENT
#     Phase 1 stubs replaced by Phase 3.1 implementations.
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseSkillStub:
    def test_grant_skill_now_materialises(self, tmp_path):
        """GRANT on SKILL is now implemented (Phase 3.1) — materialised=True or False
        depending on the candidate validity, but never a Phase-1 'not implemented' stub."""
        from unittest.mock import patch
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec={
            "name": "pdf-extract",
            "description": "Extract text from PDFs",
            "procedure": ["open pdf", "read pages"],
            "pitfalls": [],
            "confidence": 0.9,
        })
        verdict = _grant_verdict()
        with patch("systemu.runtime.governor.persist_skill_candidate",
                   return_value=str(tmp_path / "skills" / "pdf-extract" / "SKILL.md")):
            result = gov.materialise(req, verdict, vault=vault,
                                     config=_mock_config(), execution_id="exec_skill")
        # Phase 3.1 returns True; Phase 1 stub returned False with "not implemented"
        assert result["materialised"] is True
        assert "skill" in result

    def test_grant_access_now_materialises(self, tmp_path):
        """GRANT on ACCESS is now implemented (Phase 3.1)."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.ACCESS, spec={"resource": "vault/tools", "access_type": "read"})
        verdict = _grant_verdict()
        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_access")
        assert result["materialised"] is True
        assert "lease_id" in result
        assert "apply" in result

    def test_grant_compute_now_materialises(self, tmp_path):
        """GRANT on COMPUTE is now implemented (Phase 3.1)."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 5})
        verdict = _grant_verdict()
        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute")
        assert result["materialised"] is True
        assert "compute_grant" in result

    def test_grant_subagent_now_materialises(self, tmp_path):
        """GRANT on SUBAGENT is now implemented (Phase 3.1)."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec={"task": "summarise logs", "depth": 1, "budget_fraction": 0.3})
        verdict = _grant_verdict()
        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub")
        assert result["materialised"] is True
        assert "subagent" in result


# ─────────────────────────────────────────────────────────────────────────────
#  5. revoke_leases
# ─────────────────────────────────────────────────────────────────────────────

class TestRevokeLeases:
    def _setup_leases(self, tmp_path: Path) -> tuple[Governor, str, list[str]]:
        """Create a governor with two leases for exec_A and one for exec_B."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)

        exec_a = "exec_revoke_a"
        exec_b = "exec_revoke_b"

        # Manually register leases (bypass materialise for speed)
        lease_a1 = "lease_a1aaaaaaaaa"
        lease_a2 = "lease_a2aaaaaaaaa"
        lease_b1 = "lease_b1bbbbbbbbb"

        req_a = _req(HarnessKind.TOOL, spec={"name": "t1"})
        req_b = _req(HarnessKind.TOOL, spec={"name": "t2"})
        req_a2 = _req(HarnessKind.TOOL, spec={"name": "t3"})

        gov._register_lease(lease_a1, req_a, exec_a)
        gov._register_lease(lease_a2, req_a2, exec_a)
        gov._register_lease(lease_b1, req_b, exec_b)

        return gov, exec_a, [lease_a1, lease_a2, lease_b1]

    def test_revoke_marks_leases_revoked(self, tmp_path):
        gov, exec_a, (lease_a1, lease_a2, _) = self._setup_leases(tmp_path)
        count = gov.revoke_leases(exec_a)
        assert count == 2
        assert gov.get_lease(lease_a1)["revoked"] is True
        assert gov.get_lease(lease_a2)["revoked"] is True

    def test_revoke_only_affects_target_execution(self, tmp_path):
        gov, exec_a, (_, _, lease_b1) = self._setup_leases(tmp_path)
        gov.revoke_leases(exec_a)
        # exec_b lease must remain intact
        assert gov.get_lease(lease_b1)["revoked"] is False

    def test_revoke_idempotent(self, tmp_path):
        gov, exec_a, _ = self._setup_leases(tmp_path)
        count1 = gov.revoke_leases(exec_a)
        count2 = gov.revoke_leases(exec_a)
        assert count1 == 2
        assert count2 == 0  # Already revoked — nothing new to mark

    def test_revoke_unknown_execution_returns_zero(self, tmp_path):
        gov = _default_governor()
        count = gov.revoke_leases("exec_nonexistent")
        assert count == 0


# ─────────────────────────────────────────────────────────────────────────────
#  6. Ledger — written + reloadable
# ─────────────────────────────────────────────────────────────────────────────

class TestLedger:
    def test_ledger_written_on_materialise(self, tmp_path):
        """Each materialise() call writes at least one JSONL line."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_ledger_001"
        req = _req(HarnessKind.TOOL, spec={"name": "x"})
        verdict = _deny_verdict()
        gov.materialise(req, verdict, vault=vault, config=_mock_config(), execution_id=exec_id)
        path = gov.ledger_path(exec_id, vault)
        assert path.exists()

    def test_ledger_is_valid_jsonl(self, tmp_path):
        """Every line in the ledger must parse as valid JSON."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_ledger_002"

        # Multiple materialise calls → multiple entries
        for i in range(3):
            req = _req(HarnessKind.ACCESS, spec={"resource": "r", "access_type": "read"})
            verdict = _deny_verdict()
            gov.materialise(req, verdict, vault=vault, config=_mock_config(), execution_id=exec_id)

        path = gov.ledger_path(exec_id, vault)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        for line in lines:
            parsed = json.loads(line)
            assert "ts" in parsed
            assert "execution_id" in parsed
            assert "request" in parsed
            assert "verdict" in parsed
            assert "outcome" in parsed

    def test_ledger_entry_contains_request_kind(self, tmp_path):
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_ledger_003"
        req = _req(HarnessKind.SKILL, spec={"name": "pdf_parse"})
        verdict = _deny_verdict()
        gov.materialise(req, verdict, vault=vault, config=_mock_config(), execution_id=exec_id)
        path = gov.ledger_path(exec_id, vault)
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        assert entry["request"]["kind"] == "skill"

    def test_ledger_scoped_per_execution_id(self, tmp_path):
        """Different execution_ids produce separate ledger files."""
        gov = _default_governor()

        for exec_id in ("exec_aaa", "exec_bbb"):
            vault = _mock_vault(tmp_path)
            req = _req(HarnessKind.ACCESS, spec={"resource": "r", "access_type": "read"})
            gov.materialise(req, _deny_verdict(), vault=vault,
                            config=_mock_config(), execution_id=exec_id)

        path_a = gov.ledger_path("exec_aaa", _mock_vault(tmp_path))
        path_b = gov.ledger_path("exec_bbb", _mock_vault(tmp_path))
        assert path_a != path_b
        assert path_a.exists()
        assert path_b.exists()

    def test_ledger_path_helper_returns_correct_dir(self, tmp_path):
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        p = gov.ledger_path("exec_xyz", vault)
        assert "harness_ledger" in str(p)
        assert p.name == "exec_xyz.jsonl"

    def test_ledger_tool_grant_records_materialised_true(self, tmp_path):
        """A successful TOOL grant records materialised=True in the ledger."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_tool_ledger"
        req = _req(HarnessKind.TOOL, spec={"name": "csv_writer", "description": "Write CSV"})
        verdict = _grant_verdict(lease_id="lease_writecsvtest")

        with patch("systemu.runtime.governor.forge_proposed_tools") as mock_forge:
            mock_forge.return_value = [_fake_tool("csv_writer")]
            gov.materialise(req, verdict, vault=vault, config=_mock_config(), execution_id=exec_id)

        path = gov.ledger_path(exec_id, vault)
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        assert entry["outcome"]["materialised"] is True
        assert entry["outcome"]["lease_id"] == "lease_writecsvtest"


# ─────────────────────────────────────────────────────────────────────────────
#  7. Governor constructor / policy wiring
# ─────────────────────────────────────────────────────────────────────────────

class TestGovernorInit:
    def test_default_init_builds_policy(self):
        gov = Governor()
        assert gov.policy is not None
        # Default policy: auto_grant_tool=False
        assert gov.policy.auto_grant_tool is False

    def test_config_dict_forwarded_to_policy(self):
        gov = Governor(config={"auto_grant_tool": True, "max_requests_per_run": 3})
        assert gov.policy.auto_grant_tool is True
        assert gov.policy.max_requests_per_run == 3

    def test_config_none_uses_defaults(self):
        gov = Governor(config=None)
        assert gov.policy.max_requests_per_run == 8  # compiled-in default

    def test_leases_start_empty(self):
        gov = Governor()
        assert gov.list_leases() == []
