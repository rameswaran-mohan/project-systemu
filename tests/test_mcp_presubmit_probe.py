"""Front-loaded: the MCP pre-submit anti-replay probe lets a money-move MCP effect
earn a receipt (previously fail-closed/uncredited by design). The test IS the
consumer — a curated readback template + a stateful mock-REST (token ABSENT on the
pre-submit probe, PRESENT after the mutation) proves the freshness path, so the
mechanism is validated + ready the moment a real money-move MCP tool lands.

Money-move safety is PRESERVED by reuse: credit still flows only through the
hardened, host-pinned, https, fresh api_readback in ExternalVerifier — the probe
only supplies the freshness proof that path already required.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.runtime.actuation import mcp_readback
from systemu.runtime.actuation.mcp_modality import McpActuationModality
from systemu.runtime.actuation.modality import Action, ActionResult
from systemu.runtime.readback_client import ProdReadbackClient

_PUB_IP = "93.184.216.34"
_IDEM = "pay-abc-123-idem"
_TOOL_NAME = "mcp__pay__create_payment"


@pytest.fixture(autouse=True)
def _template():
    mcp_readback.register_template(
        _TOOL_NAME,
        readback_url_template=f"https://{_PUB_IP}/payments/{{idempotency_key}}",
        idempotency_param="idempotency_key")
    yield
    mcp_readback._TEMPLATES.clear()


def _runtime(handler):
    return SimpleNamespace(
        _external_api_client=ProdReadbackClient(transport=httpx.MockTransport(handler)))


def _money_tool():
    # real money_move effect tags so BOTH the modality and _run_external_verification
    # classify it as a money-move (the reused fail-closed gate must engage).
    return Tool(id="tool_pay", name=_TOOL_NAME, description="send a payment",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True, implementation_path="vault/tools/implementations/pay.py",
                effect_tags=["money_move"])


def _action():
    return Action(modality="mcp", name=_TOOL_NAME,
                  params={"payee": "acme", "amount": "500", "idempotency_key": _IDEM},
                  is_mutation=True, objective=SimpleNamespace(objective_id=1),
                  tool=_money_tool())


def _created_payment_result():
    # the created payment, read back at the SAME idempotency URL (post-submit shape).
    payload = {"html_url": f"https://{_PUB_IP}/payments/{_IDEM}", "id": _IDEM,
               "number": _IDEM, "status": "settled"}
    return ActionResult(success=True, response=payload, raw={"response": payload})


def test_money_move_mcp_credited_with_a_fresh_presubmit_probe():
    """Token ABSENT pre-submit, PRESENT after → provably fresh → the money-move MCP
    effect is CREDITED (confirmed via the hardened api_readback)."""
    seen: list = []

    def _h(req):
        seen.append(str(req.url))
        if len(seen) == 1:                    # the pre-submit probe → ABSENT
            return httpx.Response(200, headers={"content-type": "application/json"}, json={})
        return httpx.Response(200, headers={"content-type": "application/json"},  # post → PRESENT
                              json={"id": _IDEM, "number": _IDEM, "status": "settled",
                                    "html_url": str(req.url)})

    mod = McpActuationModality(_runtime(_h))
    action = _action()
    assert mod._is_money_move(action) is True

    probe = mod.probe_presubmit(action)
    assert probe["probe_ran"] is True
    assert probe["pre_submit_absent"] is True          # token genuinely absent pre-submit

    ev = mod.capture_evidence(action, _created_payment_result(), presubmit=probe)
    assert ev.confirmed is True, f"a fresh money-move must credit; ev={ev}"
    assert ev.method == "api_readback"
    assert len(seen) >= 2                               # probe + verify both hit the host


def test_money_move_mcp_stays_claimed_without_a_probe():
    """No pre-submit probe (the default) → freshness unprovable → the money-move stays
    CLAIMED (fail-closed) — the pre-R-A14a-probe behavior is unchanged."""
    mod = McpActuationModality(_runtime(
        lambda req: httpx.Response(200, json={"id": _IDEM, "html_url": str(req.url)})))
    ev = mod.capture_evidence(_action(), _created_payment_result(), presubmit=None)
    assert ev.confirmed is False


def test_replay_token_present_pre_submit_is_not_credited():
    """Anti-replay: the token ALREADY present pre-submit is STALE (this run did not
    create it) → the probe reports pre_submit_absent=False → NOT credited."""
    mod = McpActuationModality(_runtime(
        lambda req: httpx.Response(200, json={"id": _IDEM, "number": _IDEM,
                                              "html_url": str(req.url)})))   # present ALWAYS
    action = _action()
    probe = mod.probe_presubmit(action)
    assert probe["pre_submit_absent"] is False         # token present pre-submit → stale
    ev = mod.capture_evidence(action, _created_payment_result(), presubmit=probe)
    assert ev.confirmed is False, "a stale (replayable) token must never credit a money-move"


def test_no_curated_template_no_probe():
    """A money-move MCP tool with NO curated readback template → no probe can run →
    probe_ran False (freshness unprovable) → stays fail-closed."""
    mcp_readback._TEMPLATES.clear()
    mod = McpActuationModality(_runtime(lambda req: httpx.Response(200, json={})))
    probe = mod.probe_presubmit(_action())
    assert probe["probe_ran"] is False


def test_probe_of_one_url_does_not_vouch_for_a_different_credited_resource():
    """Anti-forgery (C3 binding): the pre-submit probe reads the idempotency URL, but
    the mutation RESULT claims a DIFFERENT resource url/token. The freshness proof is
    bound to the PROBED resource, so it must NOT credit the different one."""
    seen: list = []

    def _h(req):
        seen.append(str(req.url))
        # the probe reads .../payments/<idem> → ABSENT; any later read → present
        # (a compromised/confused tool trying to get the different resource credited).
        return httpx.Response(200, headers={"content-type": "application/json"},
                              json={} if len(seen) == 1
                              else {"id": "OTHER-999", "html_url": str(req.url)})

    mod = McpActuationModality(_runtime(_h))
    action = _action()                      # idempotency_key = _IDEM → probes .../payments/<_IDEM>
    probe = mod.probe_presubmit(action)
    assert probe["pre_submit_absent"] is True

    # the RESULT claims a DIFFERENT resource (different url + token) than the probe.
    other = {"html_url": f"https://{_PUB_IP}/payments/OTHER-999", "id": "OTHER-999",
             "number": "OTHER-999", "status": "settled"}
    ev = mod.capture_evidence(
        action, ActionResult(success=True, response=other, raw={"response": other}),
        presubmit=probe)
    assert ev.confirmed is False, "a probe of one resource must not vouch for a different one"
