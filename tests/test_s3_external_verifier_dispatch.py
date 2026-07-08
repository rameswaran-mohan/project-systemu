"""S3 wave1 Step 2 — ExternalVerifier scaffold + strategy dispatch.

The verifier turns a strategy result into an ExternalEvidence whose ``confirmed``
bit is set ONLY by a DETERMINISTIC equality/predicate match — NEVER from any
model output. Wave-1 scope:

  * dispatch by effect_class / strategy name to _api_readback / _email_confirm /
    _web_assertion / _operator_attest;
  * ``_api_readback`` confirms on a deterministic token-equality match;
  * ``_web_assertion`` is ADVISORY-ONLY — it can NEVER, alone, credit a
    money-move (returns confirmed=False for a money-move even on a positive
    assertion) — it is hard-gated via money_move_net_applies;
  * NO strategy path calls an LLM — every llm_router entry point is monkeypatched
    to RAISE and asserted never hit.
"""
from __future__ import annotations

import pytest

from systemu.core.models import ExternalEvidence
from systemu.runtime.effect_tags import EffectTag
from systemu.runtime.external_verifier import ExternalVerifier


# ── a router tripwire: any LLM call during verification must blow up ──────────

@pytest.fixture(autouse=True)
def _forbid_llm(monkeypatch):
    """Make EVERY llm_router entry point raise. A strategy that (wrongly) reaches
    for a model would crash the test rather than silently pass — the deterministic-
    only contract is asserted by construction."""
    import systemu.core.llm_router as lr

    def _boom(*a, **k):
        raise AssertionError("ExternalVerifier must NEVER call an LLM (deterministic-only)")

    for name in ("llm_call", "async_llm_call_json", "llm_call_json"):
        if hasattr(lr, name):
            monkeypatch.setattr(lr, name, _boom, raising=True)
    return _boom


# ── a simple objective stand-in ──────────────────────────────────────────────

class _Obj:
    def __init__(self, objective_id, text="", params=None, effect_tags=None,
                 requires_external=False):
        self.id = objective_id
        self.objective_id = objective_id
        self.text = text
        self.params = params or {}
        self.effect_tags = effect_tags or set()
        self.requires_external = requires_external


# ── api_readback: deterministic token-equality confirms ──────────────────────

def test_api_readback_confirms_on_token_match():
    v = ExternalVerifier()
    obj = _Obj(1, text="create the record", effect_tags={EffectTag.NET_MUTATE},
               requires_external=True)
    # the deterministic readback echoes back the exact expected token.
    # BLOCKER-2 hardening: the legacy inline path now also enforces token-freshness
    # (a create-once invariant) — a FRESH token (proven absent pre-submit) on a
    # NON-money effect still confirms (benign back-compat preserved).
    ev = v.verify(
        obj,
        effect_class="net_mutate",
        evidence_input={
            "strategy": "api_readback",
            "expected_tokens": ["REC-777"],
            "observed_tokens": ["REC-777", "other-noise"],
            "pre_submit_absent": True,          # freshness proof (create-once)
        },
    )
    assert isinstance(ev, ExternalEvidence)
    assert ev.confirmed is True
    assert ev.method == "api_readback"
    assert ev.objective_id == 1


def test_api_readback_rejects_on_token_mismatch():
    v = ExternalVerifier()
    obj = _Obj(2, effect_tags={EffectTag.NET_MUTATE}, requires_external=True)
    ev = v.verify(
        obj,
        effect_class="net_mutate",
        evidence_input={
            "strategy": "api_readback",
            "expected_tokens": ["REC-777"],
            "observed_tokens": ["something-else"],
        },
    )
    assert ev.confirmed is False
    assert ev.method == "api_readback"


def test_api_readback_missing_tokens_fails_closed():
    v = ExternalVerifier()
    obj = _Obj(3, requires_external=True)
    ev = v.verify(obj, effect_class="net_mutate",
                  evidence_input={"strategy": "api_readback"})
    assert ev.confirmed is False


# ── web_assertion: advisory-only; can NEVER alone confirm a money-move ────────

def test_web_assertion_cannot_confirm_money_move():
    v = ExternalVerifier()
    # an explicit MONEY_MOVE objective with a positive web assertion
    obj = _Obj(4, text="pay invoice 42", params={"amount": 500},
               effect_tags={EffectTag.MONEY_MOVE}, requires_external=True)
    ev = v.verify(
        obj,
        effect_class="money_move",
        evidence_input={
            "strategy": "web_assertion",
            "assertion_passed": True,          # a positive UI/DOM assertion…
            "expected_text": "Payment complete",
            "observed_text": "Payment complete",
        },
    )
    # …must still NOT confirm — web_assertion is advisory-only for a money-move
    assert ev.confirmed is False
    assert ev.method == "web_assertion"


def test_web_assertion_money_move_via_unknown_effect_disjunction():
    """Even when the effect is UNKNOWN, if it's a financial signal the money-move
    net catches it and web_assertion still cannot confirm (BLOCKER-3 path)."""
    v = ExternalVerifier()
    obj = _Obj(5, text="wire $500 to acme", effect_tags={EffectTag.UNKNOWN},
               requires_external=False)
    ev = v.verify(
        obj,
        effect_class="unknown",
        evidence_input={"strategy": "web_assertion", "assertion_passed": True},
    )
    assert ev.confirmed is False
    assert ev.method == "web_assertion"


def test_web_assertion_advisory_confirms_nonmoney_on_deterministic_match():
    """For a NON-money effect, a deterministic text-equality web assertion may
    return confirmed=True — but only on an exact equality predicate, never a model
    judgement."""
    v = ExternalVerifier()
    obj = _Obj(6, text="post the update", effect_tags={EffectTag.NET_MUTATE},
               requires_external=True)
    ev = v.verify(
        obj,
        effect_class="net_mutate",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Posted!",
            "observed_text": "Posted!",
        },
    )
    assert ev.confirmed is True
    assert ev.method == "web_assertion"


def test_web_assertion_nonmoney_rejects_on_text_mismatch():
    v = ExternalVerifier()
    obj = _Obj(7, effect_tags={EffectTag.NET_MUTATE}, requires_external=True)
    ev = v.verify(
        obj,
        effect_class="net_mutate",
        evidence_input={
            "strategy": "web_assertion",
            "expected_text": "Posted!",
            "observed_text": "Error",
        },
    )
    assert ev.confirmed is False


# ── dispatch: unknown/absent strategy fails closed, never raises ─────────────

def test_unknown_strategy_fails_closed():
    v = ExternalVerifier()
    obj = _Obj(8)
    ev = v.verify(obj, effect_class="net_mutate",
                  evidence_input={"strategy": "does_not_exist"})
    assert isinstance(ev, ExternalEvidence)
    assert ev.confirmed is False


def test_missing_evidence_input_fails_closed():
    v = ExternalVerifier()
    obj = _Obj(9)
    ev = v.verify(obj, effect_class="net_mutate", evidence_input=None)
    assert ev.confirmed is False


def test_dispatch_by_effect_class_defaults_web_assertion_for_ui_effect():
    """When no explicit strategy is named, the verifier still dispatches
    deterministically (here: by effect_class) and never raises."""
    v = ExternalVerifier()
    obj = _Obj(10, text="click submit", effect_tags={EffectTag.NET_MUTATE},
               requires_external=True)
    ev = v.verify(obj, effect_class="net_mutate", evidence_input={})
    assert isinstance(ev, ExternalEvidence)
    assert ev.confirmed is False  # empty evidence ⇒ nothing to match ⇒ fail-closed


def test_operator_attest_confirms_only_on_explicit_true_predicate():
    v = ExternalVerifier()
    obj = _Obj(11, effect_tags={EffectTag.NET_MUTATE}, requires_external=True)
    ev_ok = v.verify(obj, effect_class="net_mutate",
                     evidence_input={"strategy": "operator_attest", "attested": True})
    assert ev_ok.confirmed is True
    assert ev_ok.method == "operator_attest"
    ev_no = v.verify(obj, effect_class="net_mutate",
                     evidence_input={"strategy": "operator_attest", "attested": False})
    assert ev_no.confirmed is False


def test_operator_attest_cannot_confirm_money_move():
    """Operator attestation is a human predicate but wave-1 keeps the money-move
    hard gate uniform: a money-move needs the strong readback path, so a bare
    operator attestation does NOT alone confirm a money-move here."""
    v = ExternalVerifier()
    obj = _Obj(12, text="charge the card", params={"amount": 99},
               effect_tags={EffectTag.MONEY_MOVE}, requires_external=True)
    ev = v.verify(obj, effect_class="money_move",
                  evidence_input={"strategy": "operator_attest", "attested": True})
    assert ev.confirmed is False


def test_email_confirm_deterministic_token_match():
    v = ExternalVerifier()
    obj = _Obj(13, effect_tags={EffectTag.SEND_MESSAGE}, requires_external=True)
    ev = v.verify(
        obj,
        effect_class="send_message",
        evidence_input={
            "strategy": "email_confirm",
            "expected_tokens": ["CONF-123"],
            "observed_tokens": ["subject: CONF-123 received"],
        },
    )
    assert ev.confirmed is True
    assert ev.method == "email_confirm"


def test_api_readback_CAN_confirm_money_move_via_strong_readback():
    """The strong deterministic api_readback path is the one strategy allowed to
    confirm a money-move — a create-once token echo IS a real ground-truth match.

    BLOCKER-2 hardening: the LEGACY INLINE variant (bare ``observed_tokens`` with
    NO ``readback_url``) has no host-pin/https/create-once proof, so it can NO
    LONGER confirm a money-move — it is demoted by verify()'s money-move gate. The
    money-move must go through the HARDENED ``readback_url`` path (host-pinned,
    https, fresh) via an injected client."""
    from systemu.runtime.external_verifier import ExternalVerifier as _EV

    class _EchoClient:
        def readback(self, url):
            return {"observed_tokens": ["PAY-CONF-42"]}

    obj = _Obj(14, text="pay invoice 42", params={"amount": 500},
               effect_tags={EffectTag.MONEY_MOVE}, requires_external=True)

    # 1) legacy INLINE path (no readback_url) ⇒ money-move REFUSED (demoted).
    v_inline = ExternalVerifier()
    ev_inline = v_inline.verify(
        obj,
        effect_class="money_move",
        evidence_input={
            "strategy": "api_readback",
            "expected_tokens": ["PAY-CONF-42"],
            "observed_tokens": ["PAY-CONF-42"],
            "pre_submit_absent": True,
        },
    )
    assert ev_inline.confirmed is False, (
        "the legacy inline api_readback path lacks the hardened proof a money-move "
        "requires — it must NOT confirm")

    # 2) the HARDENED readback_url path (host-pin + https + fresh) DOES confirm.
    v_strong = _EV(api_client=_EchoClient())
    ev_strong = v_strong.verify(
        obj,
        effect_class="money_move",
        evidence_input={
            "strategy": "api_readback",
            "expected_tokens": ["PAY-CONF-42"],
            "submit_host": "api.example.com",
            "readback_url": "https://api.example.com/payments/PAY-CONF-42",
            "pre_submit_absent": True,
        },
    )
    assert ev_strong.confirmed is True
    assert ev_strong.method == "api_readback"
