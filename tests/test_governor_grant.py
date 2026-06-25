import systemu.runtime.harness_arbiter as _ha
from systemu.runtime.governor import Governor, _amend_meta
from systemu.core.models import HarnessRequest, HarnessVerdict, HarnessKind, \
    HarnessDecision, RiskBand


def test_ledger_entry_carries_amend_meta():
    req = HarnessRequest(kind=HarnessKind.ACCESS, spec={"access_type": "read"})
    vd = HarnessVerdict(request_id=req.request_id, decision=HarnessDecision.GRANT,
                        risk_band=RiskBand.MEDIUM)
    meta = {"operator_amended": True, "fresh_risk_band": "medium"}
    entry = Governor._ledger_entry(req, vd, {"materialised": True},
                                   "exec_x", amend_meta=meta)
    assert entry["amend"] == meta


def test_amend_meta_builds_record_on_amend():
    prior = HarnessRequest(kind=HarnessKind.ACCESS, spec={"access_type": "read"})
    req = HarnessRequest(request_id=prior.request_id, kind=HarnessKind.ACCESS,
                         spec={"access_type": "write"})
    vd = HarnessVerdict(request_id=req.request_id, decision=HarnessDecision.GRANT,
                        risk_band=RiskBand.HIGH)
    meta = _amend_meta(prior, req, vd, band_escalation_confirmed=True)
    assert meta["operator_amended"] is True
    assert meta["original_spec"] == {"access_type": "read"}
    assert meta["amended_spec"] == {"access_type": "write"}
    assert meta["fresh_risk_band"] == "high"
    assert meta["band_escalation_confirmed"] is True


def test_amend_meta_none_when_not_amended():
    req = HarnessRequest(kind=HarnessKind.TOOL, spec={"name": "t"})
    vd = HarnessVerdict(request_id=req.request_id, decision=HarnessDecision.GRANT,
                        risk_band=RiskBand.HIGH)
    assert _amend_meta(None, req, vd, band_escalation_confirmed=False) is None


def _fake_arbitrate(verdict_by_spec):
    """Return an arbitrate() stub keyed by a spec marker so prior vs edited can
    differ. verdict_by_spec maps spec.get('m') -> (decision, band)."""
    def _f(request, policy, context=None):
        decision, band = verdict_by_spec[request.spec.get("m")]
        return {"verdict": HarnessVerdict(request_id=request.request_id,
                                          decision=decision, risk_band=band),
                "risk_band": band, "needs_llm_judgment": False}
    return _f


def _patch_materialise(monkeypatch, calls):
    def _m(self, request, verdict, *, vault, config, execution_id, amend_meta=None):
        calls.append({"verdict": verdict, "amend_meta": amend_meta})
        return {"materialised": True, "lease_id": "lease_1", "tool": "t"}
    monkeypatch.setattr(Governor, "materialise", _m)


def test_grant_forces_grant_on_escalate(monkeypatch):
    # Unedited TOOL forge: arbitrate → ESCALATE; grant must STILL materialise.
    monkeypatch.setattr(_ha, "arbitrate",
                        _fake_arbitrate({"x": (HarnessDecision.ESCALATE, RiskBand.HIGH)}))
    calls = []
    _patch_materialise(monkeypatch, calls)
    req = HarnessRequest(kind=HarnessKind.TOOL, spec={"m": "x", "name": "t"})
    g = Governor(None).grant(req, context={}, vault=object(), config=None,
                             execution_id="e1")
    assert g["materialised"] is True
    assert calls[0]["verdict"].decision == HarnessDecision.GRANT
    assert len(calls) == 1                       # exactly one materialise


def test_grant_blocks_on_hard_deny(monkeypatch):
    monkeypatch.setattr(_ha, "arbitrate",
                        _fake_arbitrate({"x": (HarnessDecision.DENY, RiskBand.HIGH)}))
    calls = []
    _patch_materialise(monkeypatch, calls)
    req = HarnessRequest(kind=HarnessKind.MCP, spec={"m": "x"})
    g = Governor(None).grant(req, context={}, vault=object(), config=None,
                             execution_id="e1")
    assert g["denied"] is True
    assert calls == []                           # never materialised


def test_grant_blocks_unconfirmed_band_increase(monkeypatch):
    monkeypatch.setattr(_ha, "arbitrate", _fake_arbitrate({
        "lo": (HarnessDecision.ESCALATE, RiskBand.MEDIUM),
        "hi": (HarnessDecision.ESCALATE, RiskBand.HIGH),
    }))
    calls = []
    _patch_materialise(monkeypatch, calls)
    prior = HarnessRequest(kind=HarnessKind.ACCESS, spec={"m": "lo"})
    req = HarnessRequest(request_id=prior.request_id, kind=HarnessKind.ACCESS,
                         spec={"m": "hi"})
    g = Governor(None).grant(req, context={}, vault=object(), config=None,
                             execution_id="e1", prior_request=prior,
                             band_escalation_confirmed=False)
    assert g["denied"] is True
    assert "risk" in g["reason"]
    assert calls == []


def test_grant_allows_confirmed_band_increase(monkeypatch):
    monkeypatch.setattr(_ha, "arbitrate", _fake_arbitrate({
        "lo": (HarnessDecision.ESCALATE, RiskBand.MEDIUM),
        "hi": (HarnessDecision.ESCALATE, RiskBand.HIGH),
    }))
    calls = []
    _patch_materialise(monkeypatch, calls)
    prior = HarnessRequest(kind=HarnessKind.ACCESS, spec={"m": "lo"})
    req = HarnessRequest(request_id=prior.request_id, kind=HarnessKind.ACCESS,
                         spec={"m": "hi"})
    g = Governor(None).grant(req, context={}, vault=object(), config=None,
                             execution_id="e1", prior_request=prior,
                             band_escalation_confirmed=True)
    assert g["materialised"] is True
    assert calls[0]["amend_meta"]["operator_amended"] is True
    assert calls[0]["amend_meta"]["band_escalation_confirmed"] is True
