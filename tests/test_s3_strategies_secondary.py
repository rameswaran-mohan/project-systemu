"""S3 / R-A7 wave2 Step 4 — the secondary verifier strategies:

  * ``_email_confirm`` — a DETERMINISTIC predicate over a mock email-MCP result
    (confirmation-number / token equality). Matching email ⇒ confirmed; a
    non-matching email ⇒ NOT confirmed. Deterministic-only (no LLM).

  * ``_web_assertion`` — ADVISORY. NEVER ``confirmed=True`` for a money-move (both
    the explicit MONEY_MOVE tag AND the UNKNOWN∩financial disjunction). For a
    NON-money effect it may confirm on a deterministic text-equality — but it is
    NOT the sole crediting channel for money.

  * ``_operator_attest`` — renders RAW, REDACTED, AGENT-UNINTERPRETED evidence as
    an operator-facing artifact (a GateDescriptor with gate_type="operator"), NOT
    an auto-confirm. The agent's success PROSE must be ABSENT from the payload; a
    token echoed in the raw evidence is REDACTED via ``_mask_evidence``.
"""
from __future__ import annotations

import json

import pytest

from systemu.core.models import ExternalEvidence
from systemu.runtime.effect_tags import EffectTag
from systemu.runtime.external_verifier import ExternalVerifier


# ── deterministic-only tripwire ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _forbid_llm(monkeypatch):
    import systemu.core.llm_router as lr

    def _boom(*a, **k):
        raise AssertionError("ExternalVerifier must NEVER call an LLM (deterministic-only)")

    for name in ("llm_call", "async_llm_call_json", "llm_call_json"):
        if hasattr(lr, name):
            monkeypatch.setattr(lr, name, _boom, raising=True)
    return _boom


class _Obj:
    def __init__(self, objective_id, text="", params=None, effect_tags=None,
                 requires_external=True):
        self.id = objective_id
        self.objective_id = objective_id
        self.text = text
        self.params = params or {}
        self.effect_tags = effect_tags or set()
        self.requires_external = requires_external


# ─────────────────────────────────────────────────────────────────────────────
#  _email_confirm — deterministic predicate over a mock email-MCP result
# ─────────────────────────────────────────────────────────────────────────────

class _MockEmailClient:
    """An injected email-MCP transport: ``fetch()`` returns the latest matching
    email envelope (subject/body). Never opens a socket."""

    def __init__(self, *, envelope=None, raises=None):
        self._envelope = envelope or {}
        self._raises = raises
        self.calls = 0

    def fetch(self, query=None):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return dict(self._envelope)


def test_email_confirm_matches_deterministic_predicate():
    """A confirmation email echoing the exact expected confirmation-number ⇒
    confirmed (deterministic token equality, not an LLM read)."""
    client = _MockEmailClient(envelope={
        "subject": "Your payment CONF-123 is complete",
        "body": "Order CONF-123 confirmed.",
    })
    v = ExternalVerifier(email_client=client)
    obj = _Obj(1, effect_tags={EffectTag.SEND_MESSAGE})
    ev = v.verify(
        obj, effect_class="send_message",
        evidence_input={
            "strategy": "email_confirm",
            "expected_tokens": ["CONF-123"],
            "email_query": "from:receipts@example.com",
        },
    )
    assert ev.confirmed is True
    assert ev.method == "email_confirm"


def test_email_confirm_non_matching_email_refuses():
    client = _MockEmailClient(envelope={
        "subject": "A different, unrelated email",
        "body": "nothing here matches",
    })
    v = ExternalVerifier(email_client=client)
    obj = _Obj(2, effect_tags={EffectTag.SEND_MESSAGE})
    ev = v.verify(
        obj, effect_class="send_message",
        evidence_input={
            "strategy": "email_confirm",
            "expected_tokens": ["CONF-123"],
        },
    )
    assert ev.confirmed is False
    assert ev.method == "email_confirm"


def test_email_confirm_fail_closed_on_transport_exception():
    client = _MockEmailClient(raises=TimeoutError("imap timeout"))
    v = ExternalVerifier(email_client=client)
    obj = _Obj(3, effect_tags={EffectTag.SEND_MESSAGE})
    ev = v.verify(
        obj, effect_class="send_message",
        evidence_input={"strategy": "email_confirm", "expected_tokens": ["CONF-123"]},
    )
    assert ev.confirmed is False


def test_email_confirm_legacy_observed_tokens_still_matches():
    """Back-compat with the wave-1 shape: observed_tokens supplied inline (no
    injected client) still matches deterministically."""
    v = ExternalVerifier()
    obj = _Obj(4, effect_tags={EffectTag.SEND_MESSAGE})
    ev = v.verify(
        obj, effect_class="send_message",
        evidence_input={
            "strategy": "email_confirm",
            "expected_tokens": ["CONF-123"],
            "observed_tokens": ["subject: CONF-123 received"],
        },
    )
    assert ev.confirmed is True


# ─────────────────────────────────────────────────────────────────────────────
#  _web_assertion — advisory; money-move HARD-GATED
# ─────────────────────────────────────────────────────────────────────────────

def test_web_assertion_never_confirms_explicit_money_move():
    v = ExternalVerifier()
    obj = _Obj(5, text="pay invoice 42", params={"amount": 500},
               effect_tags={EffectTag.MONEY_MOVE})
    ev = v.verify(
        obj, effect_class="money_move",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Payment complete",
            "observed_text": "Payment complete",
        },
    )
    assert ev.confirmed is False
    assert ev.method == "web_assertion"


def test_web_assertion_never_confirms_unknown_financial_disjunction():
    """The UNKNOWN∩financial money-move net path: even without the MONEY_MOVE tag,
    a financial signal on an UNKNOWN effect hard-gates web_assertion."""
    v = ExternalVerifier()
    obj = _Obj(6, text="wire $500 to acme", effect_tags={EffectTag.UNKNOWN},
               requires_external=False)
    ev = v.verify(
        obj, effect_class="unknown",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Sent",
            "observed_text": "Sent",
        },
    )
    assert ev.confirmed is False
    assert ev.method == "web_assertion"


def test_web_assertion_never_confirms_settle_balance_unknown_effect():
    """BLOCKER-3 repro: 'settle the outstanding balance' (requires_external, an
    UNKNOWN effect, NO currency/amount) is a money-move once the verb allowlist is
    fixed AND the fail-closed fallback gates the UNKNOWN external effect. A
    web_assertion (fully-automatic advisory) must NOT confirm it — it needs the
    strong hardened channel."""
    v = ExternalVerifier()
    obj = _Obj(20, text="settle the outstanding balance",
               effect_tags={EffectTag.UNKNOWN}, requires_external=True)
    ev = v.verify(
        obj, effect_class="unknown",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Balance settled",
            "observed_text": "Balance settled",
        },
    )
    assert ev.confirmed is False, "a web_assertion must not confirm a settle-the-balance money-move"
    assert ev.method == "web_assertion"


def test_web_assertion_still_confirms_known_nonmoney_net_mutate_not_over_gated():
    """The fail-closed fallback must NOT over-gate a KNOWN non-money NET_MUTATE:
    even with requires_external=True, a plainly-classified net_mutate stays
    advisory-confirmable (the fallback only fires for UNKNOWN/unclassified)."""
    v = ExternalVerifier()
    obj = _Obj(21, text="post the update to the API",
               effect_tags={EffectTag.NET_MUTATE}, requires_external=True)
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Posted!",
            "observed_text": "Posted!",
        },
    )
    assert ev.confirmed is True, "a KNOWN non-money net_mutate must not be over-gated"
    assert ev.method == "web_assertion"


def test_web_assertion_advisory_confirms_nonmoney_net_mutate():
    """For a NON-money net_mutate, a deterministic text-equality assertion MAY
    confirm advisory-only — documenting that web_assertion CAN credit a non-money
    net_mutate (it is not the sole channel only for MONEY moves)."""
    v = ExternalVerifier()
    obj = _Obj(7, text="post the update", effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Posted!",
            "observed_text": "Posted!",
        },
    )
    assert ev.confirmed is True
    assert ev.method == "web_assertion"


def test_web_assertion_nonmoney_rejects_on_mismatch():
    v = ExternalVerifier()
    obj = _Obj(8, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Posted!",
            "observed_text": "Error 500",
        },
    )
    assert ev.confirmed is False


# ─────────────────────────────────────────────────────────────────────────────
#  _api_readback — legacy inline path fail-closed for money-moves + freshness
#  (the stale-token / no-host-pin back-compat hole)
# ─────────────────────────────────────────────────────────────────────────────

def test_legacy_inline_api_readback_refuses_money_move():
    """BLOCKER-2 repro: the legacy inline api_readback path (no readback_url) does
    bare token-equality with NO host-pin/https/freshness proof. Since
    method='api_readback' is in _MONEY_MOVE_STRONG, verify()'s money-move gate does
    NOT demote it — so a money-move would be confirmed by pure inline token
    equality. It MUST NOT confirm a money-move via the legacy path."""
    v = ExternalVerifier()
    obj = _Obj(30, text="pay invoice 42", params={"amount": 500},
               effect_tags={EffectTag.MONEY_MOVE})
    ev = v.verify(
        obj, effect_class="money_move",
        evidence_input={
            "strategy": "api_readback",
            "expected_tokens": ["PAY-42"],
            # legacy inline shape: observed tokens present, but NO readback_url,
            # NO submit_host, NO freshness proof.
            "observed_tokens": ["PAY-42"],
        },
    )
    assert ev.confirmed is False, (
        "the legacy inline api_readback path lacks host-pin/https/freshness — it "
        "must NOT confirm a money-move")


def test_legacy_inline_api_readback_stale_token_refuses_even_nonmoney():
    """Freshness is a create-once invariant regardless of money: a token that was
    ALREADY present pre-submit (in presubmit_tokens) via the legacy inline path is
    STALE and must NOT confirm even for a benign NON-money effect."""
    v = ExternalVerifier()
    obj = _Obj(31, text="create a record", effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input={
            "strategy": "api_readback",
            "expected_tokens": ["REC-STALE"],
            "observed_tokens": ["REC-STALE"],
            # STALE: the token was present pre-submit ⇒ can't prove THIS run made it.
            "presubmit_tokens": ["REC-STALE"],
            "pre_submit_absent": False,
        },
    )
    assert ev.confirmed is False, (
        "a stale token (present pre-submit) must not confirm even a non-money "
        "effect on the legacy inline path")


def test_legacy_inline_api_readback_fresh_nonmoney_still_confirms():
    """Back-compat preserved for BENIGN effects: a FRESH token (absent pre-submit)
    on the legacy inline path for a NON-money effect still confirms."""
    v = ExternalVerifier()
    obj = _Obj(32, text="create a record", effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input={
            "strategy": "api_readback",
            "expected_tokens": ["REC-FRESH"],
            "observed_tokens": ["REC-FRESH"],
            # freshness proof: a pre-submit readback found the effect ABSENT.
            "pre_submit_absent": True,
        },
    )
    assert ev.confirmed is True, (
        "a fresh non-money token on the legacy inline path must still confirm "
        "(benign back-compat preserved)")
    assert ev.method == "api_readback"


# ─────────────────────────────────────────────────────────────────────────────
#  _operator_attest — raw, redacted, agent-uninterpreted artifact (render-only)
# ─────────────────────────────────────────────────────────────────────────────

_AGENT_PROSE = "I successfully completed the payment and everything worked great!"


def test_operator_attest_builds_render_only_artifact_not_autoconfirm():
    """Without an explicit operator attestation, _operator_attest renders an
    operator-facing artifact — it does NOT auto-confirm."""
    v = ExternalVerifier()
    obj = _Obj(9, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input={
            "strategy": "operator_attest",
            "agent_success_prose": _AGENT_PROSE,
            "raw_evidence": {"status": 200, "body": "record created"},
        },
    )
    # rendering an artifact is NOT a confirmation
    assert ev.confirmed is False
    assert ev.method == "operator_attest"


def test_operator_attest_artifact_excludes_agent_prose_and_redacts_token():
    """The operator artifact carries RAW evidence only — the agent's success PROSE
    is ABSENT, and a token/secret echoed in the raw evidence is REDACTED."""
    v = ExternalVerifier()
    obj = _Obj(10, effect_tags={EffectTag.NET_MUTATE})
    descriptor = v.build_operator_attest_artifact(
        obj,
        evidence_input={
            "strategy": "operator_attest",
            "agent_success_prose": _AGENT_PROSE,
            "raw_evidence": {
                "response_headers": {"authorization": "Bearer sk-live-SECRETTOKEN0001"},
                "response_body": "created; key sk-live-SECRETTOKEN0001",
                "status": 201,
            },
        },
    )
    # it is an operator-facing render-only structure typed "operator"
    ctx = descriptor.to_decision_context(gate_type="operator")
    assert ctx["gate_type"] == "operator"

    blob = json.dumps(descriptor.model_dump(), default=str)
    # the agent's interpreted success prose must NOT be in the artifact
    assert "successfully completed" not in blob
    assert _AGENT_PROSE not in blob
    # the secret token echoed in raw evidence is REDACTED (via _mask_evidence)
    assert "sk-live-SECRETTOKEN0001" not in blob
    assert "Bearer sk-live-SECRETTOKEN0001" not in blob
    # …but a non-secret raw fact (the status) survives so the operator can judge
    assert "201" in blob


def test_operator_attest_explicit_attestation_still_advisory_for_money_move():
    """An explicit operator attestation is a deterministic bool predicate but is
    still ADVISORY: it cannot alone confirm a money-move (the money-move gate)."""
    v = ExternalVerifier()
    obj = _Obj(11, text="charge the card", params={"amount": 99},
               effect_tags={EffectTag.MONEY_MOVE})
    ev = v.verify(
        obj, effect_class="money_move",
        evidence_input={"strategy": "operator_attest", "attested": True},
    )
    assert ev.confirmed is False


def test_operator_attest_explicit_attestation_confirms_nonmoney():
    v = ExternalVerifier()
    obj = _Obj(12, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input={"strategy": "operator_attest", "attested": True},
    )
    assert ev.confirmed is True
    assert ev.method == "operator_attest"
