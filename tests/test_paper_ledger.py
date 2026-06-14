"""Plan 0 Build 2 (Tasks 2.2/2.3) — Governor ledger provenance: lease events
+ decided_by surfaced in the ledger.

These tests exercise the harness-ledger JSONL the Governor writes, asserting:

  * decided_by="deterministic" is surfaced into the verdict sub-dict of a
    deterministic GRANT's ledger entry (Task 2.3: _ledger_entry carries
    verdict.decided_by);
  * after materialise() of a GRANT (TOOL) that mints a lease, a "lease-mint"
    event row appears in the ledger JSONL (Task 2.2);
  * after revoke_leases() with ``_active_ledger_vault`` set, a "lease-revoke"
    event row appears for the revoked lease (Task 2.2);
  * revoke_leases() with ``_active_ledger_vault is None`` is a best-effort
    no-op for the ledger (never raises, never writes a fallback file).

No network. The tool_forge spine is monkeypatched throughout. Fixtures mirror
tests/test_v0_9_7_governor.py.
"""

from __future__ import annotations

import json
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
from systemu.runtime.governor import Governor


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers / fixtures (mirrors tests/test_v0_9_7_governor.py)
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


def _default_governor() -> Governor:
    return Governor(config={
        "auto_grant_tool": False,
        "auto_grant_skill": True,
        "auto_grant_access": False,
        "auto_grant_compute": True,
        "auto_grant_subagent": False,
        "allowed_resources": {"vault/tools", "vault/skills"},
        "max_requests_per_run": 8,
    })


def _read_ledger(gov: Governor, exec_id: str, vault) -> list[dict]:
    path = gov.ledger_path(exec_id, vault)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2.3 — decided_by surfaced into the ledger verdict sub-dict
# ─────────────────────────────────────────────────────────────────────────────

class TestLedgerDecidedBy:
    def test_deterministic_verdict_decided_by_in_ledger(self, tmp_path):
        """A deterministic GRANT's ledger entry carries verdict.decided_by."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_decided_det"
        # ACCESS read whitelisted → deterministic GRANT (decided_by stays default).
        req = _req(HarnessKind.ACCESS, spec={"resource": "vault/tools", "access_type": "read"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.GRANT
        assert verdict.decided_by == "deterministic"

        gov.materialise(req, verdict, vault=vault, config=_mock_config(), execution_id=exec_id)
        entries = _read_ledger(gov, exec_id, vault)
        assert entries, "ledger must contain at least one entry"
        assert entries[0]["verdict"]["decided_by"] == "deterministic"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2.2 — lease-mint event on materialise(GRANT) that mints a lease
# ─────────────────────────────────────────────────────────────────────────────

class TestLeaseMintEvent:
    def _materialise_tool_grant(self, tmp_path, exec_id):
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        req = _req(
            HarnessKind.TOOL,
            spec={"name": "csv_reader", "description": "Read CSV files"},
            rationale="Need CSV parsing",
        )
        verdict = _grant_verdict(lease_id="lease_mintaaaa01")
        with patch("systemu.runtime.governor.forge_proposed_tools") as mock_forge:
            mock_forge.return_value = [_fake_tool("csv_reader")]
            result = gov.materialise(req, verdict, vault=vault,
                                     config=_mock_config(), execution_id=exec_id)
        return gov, vault, result

    def test_materialise_grant_writes_lease_mint_row(self, tmp_path):
        """After materialise of a GRANT (lease minted), a lease-mint row appears."""
        exec_id = "exec_lease_mint"
        gov, vault, result = self._materialise_tool_grant(tmp_path, exec_id)
        assert result["materialised"] is True
        lease_id = result["lease_id"]

        entries = _read_ledger(gov, exec_id, vault)
        mint_rows = [e for e in entries if e.get("event_type") == "lease-mint"]
        assert len(mint_rows) == 1, f"expected exactly one lease-mint row, got {mint_rows}"
        row = mint_rows[0]
        assert row["lease_id"] == lease_id
        assert row["execution_id"] == exec_id
        assert row["kind"] == HarnessKind.TOOL.value
        assert "ts" in row

    def test_non_grant_writes_no_lease_mint_row(self, tmp_path):
        """A non-GRANT (no lease minted) materialise writes no lease-mint event."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_no_mint"
        req = _req(HarnessKind.TOOL, spec={"name": "x"})
        verdict = HarnessVerdict(
            request_id="hreq_test",
            decision=HarnessDecision.DENY,
            risk_band=RiskBand.HIGH,
            rationale="denied",
        )
        gov.materialise(req, verdict, vault=vault, config=_mock_config(), execution_id=exec_id)
        entries = _read_ledger(gov, exec_id, vault)
        assert not [e for e in entries if e.get("event_type") == "lease-mint"]


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2.2 — lease-revoke event on revoke_leases (when _active_ledger_vault set)
# ─────────────────────────────────────────────────────────────────────────────

class TestLeaseRevokeEvent:
    def test_revoke_writes_lease_revoke_row(self, tmp_path):
        """revoke_leases with _active_ledger_vault set writes a lease-revoke row."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_revoke_evt"
        lease_id = "lease_revokeaa01"
        req = _req(HarnessKind.TOOL, spec={"name": "t1"})
        gov._register_lease(lease_id, req, exec_id)

        # The execution loop is expected to publish the active vault before terminal.
        gov._active_ledger_vault = vault
        count = gov.revoke_leases(exec_id)
        assert count == 1

        entries = _read_ledger(gov, exec_id, vault)
        revoke_rows = [e for e in entries if e.get("event_type") == "lease-revoke"]
        assert len(revoke_rows) == 1, f"expected one lease-revoke row, got {revoke_rows}"
        row = revoke_rows[0]
        assert row["lease_id"] == lease_id
        assert row["execution_id"] == exec_id

    def test_revoke_without_active_vault_is_silent_noop(self, tmp_path):
        """revoke_leases with _active_ledger_vault None never raises / writes."""
        gov = _default_governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_revoke_novault"
        req = _req(HarnessKind.TOOL, spec={"name": "t2"})
        gov._register_lease("lease_novaultaa1", req, exec_id)

        assert gov._active_ledger_vault is None  # default after __init__
        count = gov.revoke_leases(exec_id)  # must not raise
        assert count == 1

        # No ledger file should have been created (best-effort skip, no fallback path).
        entries = _read_ledger(gov, exec_id, vault)
        assert not [e for e in entries if e.get("event_type") == "lease-revoke"]

    def test_active_ledger_vault_defaults_none(self):
        """Governor.__init__ initialises _active_ledger_vault to None."""
        gov = Governor()
        assert gov._active_ledger_vault is None
