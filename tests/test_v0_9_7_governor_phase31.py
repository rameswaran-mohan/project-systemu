"""v0.9.7 Phase 3.1 — Governor: materialise SKILL / ACCESS / COMPUTE / SUBAGENT.

No network.  persist_skill_candidate is monkeypatched where needed.

Coverage:
  - SKILL grant   → SKILL.md persisted (monkeypatched); lease minted; ledger written
  - SKILL bad name → materialised=False, no raise
  - ACCESS grant  → advisory lease returned (no apply patch — D.2); ledger written
  - COMPUTE grant → compute_grant within ceiling; over-spec clamped to ceiling
  - COMPUTE zero  → zero values clamped sensibly
  - SUBAGENT grant → spawn directive with depth/budget capped; no actual spawn
  - SUBAGENT beyond-depth → depth_cap clamped to policy max
  - SUBAGENT no-task → materialised=False
  - each: ledger records outcome
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import (
    HarnessDecision,
    HarnessKind,
    HarnessRequest,
    HarnessVerdict,
    RiskBand,
)
from systemu.runtime.governor import Governor


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers (mirrors test_v0_9_7_governor.py helpers)
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


def _mock_vault(tmp_path: Path):
    vault = MagicMock()
    vault.root = str(tmp_path)
    return vault


def _mock_config(**overrides):
    cfg = MagicMock()
    cfg.vault_dir = "data/systemu/vault"
    cfg.skills_user_dir = None
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _governor(extra_policy: dict | None = None) -> Governor:
    cfg = {
        "auto_grant_tool": False,
        "auto_grant_skill": True,
        "auto_grant_access": True,
        "auto_grant_compute": True,
        "auto_grant_subagent": True,
        "allowed_resources": {"vault/tools", "vault/skills"},
        "max_requests_per_run": 8,
        "max_compute_ceiling": 1.0,
        "max_subagent_depth": 2,
        "max_subagent_budget_fraction": 0.5,
    }
    if extra_policy:
        cfg.update(extra_policy)
    return Governor(config=cfg)


def _read_ledger(gov: Governor, exec_id: str, vault) -> list[dict]:
    path = gov.ledger_path(exec_id, vault)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]


# ─────────────────────────────────────────────────────────────────────────────
#  SKILL
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseSkill:
    _SKILL_SPEC = {
        "name": "pdf-extract",
        "description": "Extract text from PDFs",
        "procedure": ["Open PDF", "Read pages", "Return text"],
        "pitfalls": ["Password-protected PDFs will fail"],
        "confidence": 0.9,
    }

    def test_skill_grant_materialised_true(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        # Let persist_skill_candidate write a real SKILL.md
        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_skill_01")

        assert result["materialised"] is True

    def test_skill_grant_returns_skill_path(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_skill_02")

        assert "skill" in result
        assert result["skill"].endswith("SKILL.md")

    def test_skill_grant_skill_md_on_disk(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_skill_03")

        assert Path(result["skill"]).exists()

    def test_skill_grant_mints_lease(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_skill_04")

        assert "lease_id" in result
        assert result["lease_id"]
        assert gov.get_lease(result["lease_id"]) is not None

    def test_skill_grant_writes_ledger(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_skill_ledger"
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        gov.materialise(req, verdict, vault=vault,
                        config=_mock_config(), execution_id=exec_id)

        entries = _read_ledger(gov, exec_id, vault)
        assert len(entries) >= 1
        entry = entries[0]
        assert entry["request"]["kind"] == "skill"
        assert entry["outcome"]["materialised"] is True

    def test_skill_grant_uses_skills_user_dir(self, tmp_path):
        """When config.skills_user_dir is set, persist_skill_candidate is called with it."""
        gov = _governor()
        vault = _mock_vault(tmp_path)
        skills_dir = str(tmp_path / "my_skills")
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        with patch(
            "systemu.runtime.governor.persist_skill_candidate",
            return_value=str(tmp_path / "my_skills" / "pdf-extract" / "SKILL.md"),
        ) as mock_persist:
            result = gov.materialise(req, verdict, vault=vault,
                                     config=_mock_config(skills_user_dir=skills_dir),
                                     execution_id="exec_skill_dir")

        mock_persist.assert_called_once()
        call_kwargs = mock_persist.call_args
        assert call_kwargs.kwargs["skills_dir"] == skills_dir

    def test_skill_grant_monkeypatch_persist_called(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        fake_path = str(tmp_path / "skills" / "pdf-extract" / "SKILL.md")
        with patch(
            "systemu.runtime.governor.persist_skill_candidate",
            return_value=fake_path,
        ) as mock_persist:
            result = gov.materialise(req, verdict, vault=vault,
                                     config=_mock_config(), execution_id="exec_skill_mp")

        mock_persist.assert_called_once()
        assert result["materialised"] is True
        assert result["skill"] == fake_path

    def test_skill_invalid_name_returns_materialised_false(self, tmp_path):
        """persist_skill_candidate returns None for invalid name → materialised=False."""
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec={"name": "INVALID NAME!", "description": "x"})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_skill_bad")

        assert result["materialised"] is False
        assert "reason" in result

    def test_skill_persist_raises_returns_false_no_raise(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SKILL, spec=self._SKILL_SPEC)
        verdict = _grant_verdict()

        with patch(
            "systemu.runtime.governor.persist_skill_candidate",
            side_effect=OSError("disk full"),
        ):
            result = gov.materialise(req, verdict, vault=vault,
                                     config=_mock_config(), execution_id="exec_skill_err")

        assert result["materialised"] is False
        assert "skill persist failed" in result.get("reason", "")


# ─────────────────────────────────────────────────────────────────────────────
#  ACCESS
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseAccess:
    def test_access_grant_materialised_true(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.ACCESS, spec={"resource": "vault/tools", "access_type": "read"})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_access_01")

        assert result["materialised"] is True

    def test_access_grant_returns_lease_id(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.ACCESS, spec={"resource": "vault/skills", "access_type": "read"})
        verdict = _grant_verdict(lease_id="lease_access123")

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_access_02")

        assert result["lease_id"] == "lease_access123"
        assert gov.get_lease("lease_access123") is not None

    def test_access_grant_returns_access_spec(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        spec = {"resource": "vault/tools", "access_type": "read"}
        req = _req(HarnessKind.ACCESS, spec=spec)
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_access_03")

        assert result["access"]["resource"] == "vault/tools"
        assert result["access"]["access_type"] == "read"

    def test_access_grant_records_lease_no_apply_patch(self, tmp_path):
        """Bug 5 / D.2: ACCESS materialises an advisory lease + the access spec
        only — the old sandbox-policy ``apply`` patch is gone (nothing consumed
        it; single-owner backend by design). Covers the network_host + fs_read
        specs the deleted apply-patch tests used to pin."""
        gov = _governor()
        vault = _mock_vault(tmp_path)
        for spec, eid in [
            ({"network_host": "api.example.com"}, "exec_access_04"),
            ({"fs_read": "/tmp/data"}, "exec_access_05"),
            ({"resource": "vault/secrets", "access_type": "read"}, "exec_access_06"),
        ]:
            req = _req(HarnessKind.ACCESS, spec=spec)
            verdict = _grant_verdict()
            result = gov.materialise(req, verdict, vault=vault,
                                     config=_mock_config(), execution_id=eid)
            assert result["materialised"] is True
            assert "lease_id" in result
            # The access spec is preserved verbatim …
            for k, v in spec.items():
                assert result["access"].get(k) == v
            # … but the dead apply patch is no longer emitted.
            assert "apply" not in result

    def test_access_grant_writes_ledger(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_access_ledger"
        req = _req(HarnessKind.ACCESS, spec={"resource": "vault/tools", "access_type": "read"})
        verdict = _grant_verdict()

        gov.materialise(req, verdict, vault=vault,
                        config=_mock_config(), execution_id=exec_id)

        entries = _read_ledger(gov, exec_id, vault)
        assert len(entries) >= 1
        assert entries[0]["request"]["kind"] == "access"
        assert entries[0]["outcome"]["materialised"] is True


# ─────────────────────────────────────────────────────────────────────────────
#  COMPUTE
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseCompute:
    def test_compute_grant_materialised_true(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 5, "extra_think": 2000})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute_01")

        assert result["materialised"] is True

    def test_compute_grant_returns_compute_grant(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 5, "extra_think": 2000})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute_02")

        assert "compute_grant" in result
        grant = result["compute_grant"]
        assert grant["extra_iterations"] == 5
        assert grant["extra_think"] == 2000

    def test_compute_grant_within_ceiling(self, tmp_path):
        """Values within ceiling are not clamped."""
        gov = _governor({"max_compute_ceiling": 1.0})
        vault = _mock_vault(tmp_path)
        # ceiling 1.0 → max_iterations=100, max_think=32000
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 50, "extra_think": 10000})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute_03")

        grant = result["compute_grant"]
        assert grant["extra_iterations"] == 50
        assert grant["extra_think"] == 10000

    def test_compute_over_spec_clamped(self, tmp_path):
        """Values beyond ceiling are clamped to ceiling caps."""
        gov = _governor({"max_compute_ceiling": 0.1})
        vault = _mock_vault(tmp_path)
        # ceiling 0.1 → max_iterations=10, max_think=3200
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 9999, "extra_think": 99999})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute_04")

        grant = result["compute_grant"]
        assert grant["extra_iterations"] <= 10
        assert grant["extra_think"] <= 3200

    def test_compute_zero_values_allowed(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 0, "extra_think": 0})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute_05")

        assert result["materialised"] is True
        grant = result["compute_grant"]
        assert grant["extra_iterations"] == 0
        assert grant["extra_think"] == 0

    def test_compute_empty_spec_defaults_to_zero(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.COMPUTE, spec={})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute_06")

        assert result["materialised"] is True
        assert result["compute_grant"]["extra_iterations"] == 0
        assert result["compute_grant"]["extra_think"] == 0

    def test_compute_mints_lease(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 3})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_compute_07")

        assert "lease_id" in result
        assert gov.get_lease(result["lease_id"]) is not None

    def test_compute_writes_ledger(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_compute_ledger"
        req = _req(HarnessKind.COMPUTE, spec={"extra_iterations": 2})
        verdict = _grant_verdict()

        gov.materialise(req, verdict, vault=vault,
                        config=_mock_config(), execution_id=exec_id)

        entries = _read_ledger(gov, exec_id, vault)
        assert len(entries) >= 1
        assert entries[0]["request"]["kind"] == "compute"
        assert entries[0]["outcome"]["materialised"] is True


# ─────────────────────────────────────────────────────────────────────────────
#  SUBAGENT
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseSubagent:
    _SUBAGENT_SPEC = {
        "task": "Summarise the top 5 news headlines",
        "depth": 1,
        "budget_fraction": 0.3,
    }

    def test_subagent_grant_materialised_true(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec=self._SUBAGENT_SPEC)
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_01")

        assert result["materialised"] is True

    def test_subagent_grant_returns_spawn_directive(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec=self._SUBAGENT_SPEC)
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_02")

        assert "subagent" in result
        directive = result["subagent"]
        assert directive["task"] == self._SUBAGENT_SPEC["task"]
        assert "depth_cap" in directive
        assert "budget_fraction" in directive

    def test_subagent_depth_capped_at_policy_max(self, tmp_path):
        """Requested depth beyond max_subagent_depth is clamped."""
        gov = _governor({"max_subagent_depth": 2})
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec={
            "task": "do something",
            "depth": 99,
            "budget_fraction": 0.1,
        })
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_03")

        assert result["materialised"] is True
        assert result["subagent"]["depth_cap"] <= 2

    def test_subagent_budget_capped_at_policy_max(self, tmp_path):
        """Requested budget_fraction beyond policy max is clamped."""
        gov = _governor({"max_subagent_budget_fraction": 0.5})
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec={
            "task": "do something",
            "depth": 1,
            "budget_fraction": 0.99,
        })
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_04")

        assert result["materialised"] is True
        assert result["subagent"]["budget_fraction"] <= 0.5

    def test_subagent_no_spawn_inside_materialise(self, tmp_path):
        """spawn_subagent must NOT be called inside materialise."""
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec=self._SUBAGENT_SPEC)
        verdict = _grant_verdict()

        with patch("systemu.runtime.tools.delegate.spawn_subagent") as mock_spawn:
            gov.materialise(req, verdict, vault=vault,
                            config=_mock_config(), execution_id="exec_sub_05")
            mock_spawn.assert_not_called()

    def test_subagent_mints_lease(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec=self._SUBAGENT_SPEC)
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_06")

        assert "lease_id" in result
        assert gov.get_lease(result["lease_id"]) is not None

    def test_subagent_no_task_returns_false(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec={"depth": 1, "budget_fraction": 0.3})
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_07")

        assert result["materialised"] is False
        assert "task" in result.get("reason", "").lower()

    def test_subagent_writes_ledger(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        exec_id = "exec_sub_ledger"
        req = _req(HarnessKind.SUBAGENT, spec=self._SUBAGENT_SPEC)
        verdict = _grant_verdict()

        gov.materialise(req, verdict, vault=vault,
                        config=_mock_config(), execution_id=exec_id)

        entries = _read_ledger(gov, exec_id, vault)
        assert len(entries) >= 1
        assert entries[0]["request"]["kind"] == "subagent"
        assert entries[0]["outcome"]["materialised"] is True

    def test_subagent_directive_task_matches_spec(self, tmp_path):
        gov = _governor()
        vault = _mock_vault(tmp_path)
        task = "Write a haiku about systemu"
        req = _req(HarnessKind.SUBAGENT, spec={
            "task": task, "depth": 1, "budget_fraction": 0.25,
        })
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_08")

        assert result["subagent"]["task"] == task

    def test_subagent_depth_1_within_depth_2_policy(self, tmp_path):
        gov = _governor({"max_subagent_depth": 2})
        vault = _mock_vault(tmp_path)
        req = _req(HarnessKind.SUBAGENT, spec={
            "task": "small subtask", "depth": 1, "budget_fraction": 0.2,
        })
        verdict = _grant_verdict()

        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_sub_09")

        assert result["materialised"] is True
        assert result["subagent"]["depth_cap"] == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Cross-kind: materialise never raises
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialiseNeverRaises:
    @pytest.mark.parametrize("kind,spec", [
        (HarnessKind.SKILL,    {"name": "INVALID NAME!", "description": "x"}),
        (HarnessKind.ACCESS,   {}),
        (HarnessKind.COMPUTE,  {"extra_iterations": "not-a-number"}),
        (HarnessKind.SUBAGENT, {}),  # no task
    ])
    def test_materialise_never_raises(self, tmp_path, kind, spec):
        """materialise() must return a dict and never propagate exceptions."""
        gov = _governor()
        vault = _mock_vault(tmp_path)
        req = _req(kind, spec=spec)
        verdict = _grant_verdict()

        # Must not raise
        result = gov.materialise(req, verdict, vault=vault,
                                 config=_mock_config(), execution_id="exec_noreraise")

        assert isinstance(result, dict)
        assert "materialised" in result
