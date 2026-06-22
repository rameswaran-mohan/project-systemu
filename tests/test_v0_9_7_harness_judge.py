"""v0.9.7 Phase 4.1 — LLM judge for ambiguous MEDIUM-risk harness requests.

The judge (``harness_judge.judge_harness_request``) and its wiring into
``Governor.arbitrate`` are conservative-by-construction: any error, malformed
output, or low confidence resolves to ESCALATE / no-lease. These tests stub the
LLM client entirely — NO network calls are made.

Coverage:
  - judge → ESCALATE when the LLM client raises.
  - judge → ESCALATE when confidence < 0.6 even if decision == "GRANT".
  - judge → GRANT (lease=True) when decision == "GRANT" and confidence >= 0.6.
  - Governor.arbitrate invokes the judge for a needs_llm_judgment case when
    policy.llm_judge_enabled → verdict is GRANT with a lease_id.
  - Governor.arbitrate does NOT call the judge when llm_judge_enabled=False
    (verdict stays ESCALATE) — asserted via inspect.getsource.
"""
from __future__ import annotations

import inspect

import pytest

from systemu.core.models import (
    HarnessDecision,
    HarnessKind,
    HarnessRequest,
    HarnessVerdict,
    RiskBand,
)
from systemu.runtime import harness_judge
from systemu.runtime.harness_judge import judge_harness_request
from systemu.runtime.harness_policy import HarnessPolicy
from systemu.runtime.governor import Governor


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _req(kind: HarnessKind, spec: dict | None = None, **kwargs) -> HarnessRequest:
    return HarnessRequest(kind=kind, spec=spec or {}, **kwargs)


def _arb_result(request: HarnessRequest) -> dict:
    """A minimal arbiter result dict flagged needs_llm_judgment (ambiguous MEDIUM)."""
    verdict = HarnessVerdict(
        request_id=request.request_id,
        decision=HarnessDecision.ESCALATE,
        risk_band=RiskBand.MEDIUM,
        rationale="New skill text requires review (auto-grant disabled).",
    )
    return {
        "verdict": verdict,
        "risk_band": RiskBand.MEDIUM,
        "needs_llm_judgment": True,
    }


class _Cfg:
    """A bare config stub — the judge reads only verifier_tier off it; the LLM
    call itself is always monkeypatched so the rest is irrelevant."""
    verifier_tier = 1


def _policy(**overrides) -> HarnessPolicy:
    base = dict(
        auto_grant_skill=False,   # makes a new-skill request ambiguous → needs_llm_judgment
        llm_judge_enabled=True,
        allowed_resources={"vault/tools", "vault/skills"},
    )
    base.update(overrides)
    return HarnessPolicy.from_config(base)


# ─────────────────────────────────────────────────────────────────────────────
#  1. judge_harness_request — conservative fallback on LLM error
# ─────────────────────────────────────────────────────────────────────────────

class TestJudgeFallback:
    def test_llm_raises_returns_escalate(self, monkeypatch):
        """When the LLM client raises, the judge escalates and never raises."""
        def _boom(*args, **kwargs):
            raise RuntimeError("LLM unavailable")

        monkeypatch.setattr(harness_judge, "llm_call_json", _boom)

        req = _req(HarnessKind.SKILL, spec={"name": "some_new_skill"})
        result = judge_harness_request(
            request=req,
            arb_result=_arb_result(req),
            policy=_policy(),
            context={},
            config=_Cfg(),
        )
        assert result["decision"] == HarnessDecision.ESCALATE
        assert result["lease"] is False

    def test_malformed_output_returns_escalate(self, monkeypatch):
        """A response without 'decision' → ESCALATE."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"garbage": "no decision key"},
        )
        req = _req(HarnessKind.SKILL, spec={"name": "x"})
        result = judge_harness_request(
            request=req, arb_result=_arb_result(req),
            policy=_policy(), context={}, config=_Cfg(),
        )
        assert result["decision"] == HarnessDecision.ESCALATE
        assert result["lease"] is False


# ─────────────────────────────────────────────────────────────────────────────
#  2. judge_harness_request — low-confidence GRANT downgraded to ESCALATE
# ─────────────────────────────────────────────────────────────────────────────

class TestJudgeLowConfidence:
    def test_grant_below_threshold_escalates(self, monkeypatch):
        """decision==GRANT but confidence < 0.6 → ESCALATE, no lease."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "GRANT", "confidence": 0.4, "rationale": "looks okay-ish"},
        )
        req = _req(HarnessKind.SKILL, spec={"name": "x"})
        result = judge_harness_request(
            request=req, arb_result=_arb_result(req),
            policy=_policy(), context={}, config=_Cfg(),
        )
        assert result["decision"] == HarnessDecision.ESCALATE
        assert result["lease"] is False

    def test_grant_exactly_at_threshold_grants(self, monkeypatch):
        """confidence == 0.6 is the inclusive floor → GRANT."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "GRANT", "confidence": 0.6, "rationale": "safe"},
        )
        req = _req(HarnessKind.SKILL, spec={"name": "x"})
        result = judge_harness_request(
            request=req, arb_result=_arb_result(req),
            policy=_policy(), context={}, config=_Cfg(),
        )
        assert result["decision"] == HarnessDecision.GRANT
        assert result["lease"] is True


# ─────────────────────────────────────────────────────────────────────────────
#  3. judge_harness_request — confident GRANT
# ─────────────────────────────────────────────────────────────────────────────

class TestJudgeGrant:
    def test_confident_grant_returns_lease_true(self, monkeypatch):
        """decision==GRANT and confidence>=0.6 → GRANT with lease=True."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "GRANT", "confidence": 0.92,
                             "rationale": "procedural text only, within policy"},
        )
        req = _req(HarnessKind.SKILL, spec={"name": "extract_invoice"})
        result = judge_harness_request(
            request=req, arb_result=_arb_result(req),
            policy=_policy(), context={}, config=_Cfg(),
        )
        assert result["decision"] == HarnessDecision.GRANT
        assert result["lease"] is True
        assert result["confidence"] == 0.92
        assert "procedural" in result["rationale"]

    def test_deny_returns_no_lease(self, monkeypatch):
        """decision==DENY → DENY, lease=False."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "DENY", "confidence": 0.8, "rationale": "outside policy"},
        )
        req = _req(HarnessKind.SKILL, spec={"name": "x"})
        result = judge_harness_request(
            request=req, arb_result=_arb_result(req),
            policy=_policy(), context={}, config=_Cfg(),
        )
        assert result["decision"] == HarnessDecision.DENY
        assert result["lease"] is False


# ─────────────────────────────────────────────────────────────────────────────
#  4. Governor.arbitrate — invokes the judge when llm_judge_enabled
# ─────────────────────────────────────────────────────────────────────────────

class TestGovernorJudgeWiring:
    def _governor(self, **policy_overrides) -> Governor:
        cfg = {
            "auto_grant_skill": False,     # new-skill request → needs_llm_judgment
            "llm_judge_enabled": True,
            "allowed_resources": {"vault/tools", "vault/skills"},
        }
        cfg.update(policy_overrides)
        gov = Governor(config=cfg)
        # give the judge a config object carrying verifier_tier
        gov.config = _Cfg()
        return gov

    def test_arbitrate_invokes_judge_grant(self, monkeypatch):
        """A needs_llm_judgment request + judge GRANT → verdict GRANT with lease_id."""
        # Force the LLM to a confident GRANT.
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "GRANT", "confidence": 0.9, "rationale": "clearly safe"},
        )
        gov = self._governor()
        # A real ambiguous request: new skill, auto_grant_skill=False.
        req = _req(HarnessKind.SKILL, spec={"name": "new_skill_text"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.GRANT
        assert verdict.lease_id is not None
        assert verdict.lease_id.startswith("lease_")
        assert "judged_by=llm" in verdict.rationale

    def test_arbitrate_invokes_judge_via_monkeypatched_judge(self, monkeypatch):
        """Same path, forcing GRANT by stubbing judge_harness_request itself
        (proves arbitrate calls the symbol it imported)."""
        def _fake_judge(**kwargs):
            return {
                "decision": HarnessDecision.GRANT,
                "rationale": "stubbed grant",
                "confidence": 0.95,
                "lease": True,
            }
        monkeypatch.setattr("systemu.runtime.governor.judge_harness_request", _fake_judge)
        gov = self._governor()
        req = _req(HarnessKind.SKILL, spec={"name": "another_skill"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.GRANT
        assert verdict.lease_id is not None

    def test_arbitrate_judge_deny(self, monkeypatch):
        """judge DENY → verdict DENY (no lease)."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "DENY", "confidence": 0.85, "rationale": "no"},
        )
        gov = self._governor()
        req = _req(HarnessKind.SKILL, spec={"name": "deny_me"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.DENY
        assert verdict.lease_id is None

    def test_arbitrate_judge_escalate_kept(self, monkeypatch):
        """judge ESCALATE → arbiter's ESCALATE verdict kept."""
        monkeypatch.setattr(
            harness_judge, "llm_call_json",
            lambda *a, **k: {"decision": "ESCALATE", "confidence": 0.5, "rationale": "unsure"},
        )
        gov = self._governor()
        req = _req(HarnessKind.SKILL, spec={"name": "unsure_skill"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.ESCALATE


# ─────────────────────────────────────────────────────────────────────────────
#  5. Governor.arbitrate — does NOT call the judge when disabled
# ─────────────────────────────────────────────────────────────────────────────

class TestGovernorJudgeDisabled:
    def test_disabled_keeps_escalate_no_judge_call(self, monkeypatch):
        """llm_judge_enabled=False → judge never called; verdict stays ESCALATE."""
        called = {"hit": False}

        def _should_not_run(*a, **k):
            called["hit"] = True
            raise AssertionError("judge must not be called when llm_judge_enabled=False")

        monkeypatch.setattr("systemu.runtime.governor.judge_harness_request", _should_not_run)

        gov = Governor(config={
            "auto_grant_skill": False,
            "llm_judge_enabled": False,
            "allowed_resources": {"vault/tools"},
        })
        assert gov.policy.llm_judge_enabled is False
        req = _req(HarnessKind.SKILL, spec={"name": "skill_when_disabled"})
        verdict = gov.arbitrate(req)
        assert verdict.decision == HarnessDecision.ESCALATE
        assert called["hit"] is False

    def test_arbitrate_source_references_judge_and_flag(self):
        """Source-level guard: arbitrate references both judge_harness_request
        (via its dispatch) and the llm_judge_enabled flag."""
        src = inspect.getsource(Governor.arbitrate)
        assert "llm_judge_enabled" in src
        # arbitrate delegates the judge call to _apply_llm_judgment; assert the
        # judge symbol is reachable from the arbitrate path (either directly or
        # via the helper it invokes).
        combined = src + inspect.getsource(Governor._apply_llm_judgment)
        assert "judge_harness_request" in combined
        assert "_apply_llm_judgment" in src
