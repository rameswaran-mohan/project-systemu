"""S3 / R-A7 wave-3b — IMPL-6 the AMBIGUOUS-OUTCOME / DOUBLE-SUBMIT protocol.

The most safety-critical piece of S3. On ANY transport-ambiguous failure of an
EFFECTFUL call (a timeout AFTER send, a connection reset, a 5xx-after-send), the
loop must run a read-back BEFORE any retry decision and branch three ways:

  * confirmed-present  ⇒ credit path, NO re-submit.
  * confirmed-absent   ⇒ a retry is SAFE and permitted.
  * still-indeterminate ⇒ an operator card — never a silent retry, never a
    silent give-up.

CRITICAL correctness: the read-back MUST be keyed to a CLIENT-GENERATED
idempotency key (a UUID written INTO the request — an ``Idempotency-Key`` header
/ tool-param — BEFORE send), NOT a server-assigned token. On the exact lost-
response case this rule handles, a server token was never received, so keying to
it makes "confirmed-absent" undecidable and risks reading a lost SUCCESS as
absent ⇒ a DOUBLE-SUBMITTED money-move. Where the target API offers NO
idempotency primitive to key the read-back deterministically, IMPL-6 falls to
"indeterminate ⇒ operator card" — it must NEVER risk a confirmed-absent false
negative.

Everything here is DETERMINISTIC (key equality) — the autouse ``_forbid_llm``
fixture asserts no llm_router entry point is ever reached. Transports are MOCKED.
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch

from systemu.core.models import ExternalEvidence
from systemu.runtime.effect_tags import EffectTag
from systemu.runtime.external_verifier import (
    ExternalVerifier,
    Impl6Outcome,
    inject_idempotency_key,
    mint_idempotency_key,
)


# ── the deterministic-only tripwire (mirrors the wave-1/2 dispatch tests) ──────

@pytest.fixture(autouse=True)
def _forbid_llm(monkeypatch):
    import systemu.core.llm_router as lr

    def _boom(*a, **k):
        raise AssertionError("IMPL-6 must NEVER call an LLM (deterministic-only)")

    for name in ("llm_call", "async_llm_call_json", "llm_call_json"):
        if hasattr(lr, name):
            monkeypatch.setattr(lr, name, _boom, raising=True)
    return _boom


# ── objective stand-in (matches test_s3_api_readback._Obj) ────────────────────

class _Obj:
    def __init__(self, objective_id, text="", params=None, effect_tags=None,
                 requires_external=True):
        self.id = objective_id
        self.objective_id = objective_id
        self.text = text
        self.params = params or {}
        self.effect_tags = effect_tags or set()
        self.requires_external = requires_external


# ── a MOCK readback transport keyed to the CLIENT idempotency key ─────────────

class _MockIdemReadbackClient:
    """An injected transport with ``readback(url)``. It models a server that
    records the CLIENT idempotency key it processed. Its readback envelope echoes
    the key(s) it has seen under ``processed_idempotency_keys`` — the deterministic
    signal IMPL-6 keys to.

      * ``processed_keys`` present in the envelope ⇒ the effect for THAT client key
        landed (confirmed-present).
      * ``processed_keys`` explicitly EMPTY + ``supports_idempotency=True`` ⇒ the
        server can deterministically say the key was NOT processed (confirmed-
        absent).
      * ``supports_idempotency=False`` (default when the target has no primitive)
        ⇒ the readback cannot decide by client key ⇒ indeterminate.
      * ``raises`` ⇒ a transport exception during the readback itself ⇒
        indeterminate (never confirmed-absent).
    """

    def __init__(self, *, processed_keys=None, supports_idempotency=True,
                 raises=None, envelope=None):
        self._processed = list(processed_keys or [])
        self._supports = supports_idempotency
        self._raises = raises
        self._envelope = envelope
        self.calls = []

    def readback(self, url):
        self.calls.append(url)
        if self._raises is not None:
            raise self._raises
        if self._envelope is not None:
            return self._envelope
        return {
            "url": url,
            "supports_idempotency": self._supports,
            "processed_idempotency_keys": list(self._processed),
        }


def _ambiguous_fail(**overrides):
    """A ToolResult-shaped transport-ambiguous failure descriptor for the
    shadow-runtime detector. timed_out True models a timeout AFTER send."""
    base = {"success": False, "timed_out": True, "error": "read timed out"}
    base.update(overrides)
    return base


# ═════════════════════════════════════════════════════════════════════════════
#  1. timeout-after-send, CONFIRMED-PRESENT ⇒ credit, submit EXACTLY ONCE
# ═════════════════════════════════════════════════════════════════════════════

def test_timeout_after_send_confirmed_present_credits_no_resubmit():
    """An effectful call with an injected CLIENT key times out AFTER send; the
    read-back keyed to the CLIENT key shows the effect landed ⇒ confirmed-present,
    an ExternalEvidence(confirmed=True) carrying the key, and the submit is issued
    EXACTLY ONCE (no second submit)."""
    key = mint_idempotency_key()
    # server RECORDS this client key as processed ⇒ the effect landed.
    client = _MockIdemReadbackClient(processed_keys=[key], supports_idempotency=True)
    v = ExternalVerifier(api_client=client)
    obj = _Obj(1, text="pay invoice 7", params={"amount": 200},
               effect_tags={EffectTag.MONEY_MOVE})

    # a submit spy — IMPL-6 must NOT re-submit under confirmed-present.
    submits = {"n": 0}

    def _do_submit():
        submits["n"] += 1

    # model that the ORIGINAL submit already fired once (before the ambiguous fail)
    _do_submit()
    assert submits["n"] == 1

    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=key,
        readback_url="https://api.example.com/payments?idem=" + key,
        submit_host="api.example.com",
        retry_fn=_do_submit,
    )

    assert outcome.decision == "confirmed_present"
    assert outcome.evidence is not None
    assert outcome.evidence.confirmed is True
    assert outcome.evidence.idempotency_key == key
    assert outcome.allow_retry is False
    assert outcome.operator_card is False
    # the headline invariant: NO second submit.
    assert submits["n"] == 1
    # the read-back was keyed to the CLIENT key (the URL carried it).
    assert client.calls and key in client.calls[0]


# ═════════════════════════════════════════════════════════════════════════════
#  2. CONFIRMED-ABSENT ⇒ retry permitted
# ═════════════════════════════════════════════════════════════════════════════

def test_confirmed_absent_permits_retry():
    """The read-back keyed to the CLIENT key deterministically shows the key was
    NOT processed (server supports idempotency + reports it absent) ⇒ a retry is
    ALLOWED."""
    key = mint_idempotency_key()
    client = _MockIdemReadbackClient(processed_keys=[], supports_idempotency=True)
    v = ExternalVerifier(api_client=client)
    obj = _Obj(2, text="pay invoice 8", params={"amount": 50},
               effect_tags={EffectTag.MONEY_MOVE})

    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=key,
        readback_url="https://api.example.com/payments?idem=" + key,
        submit_host="api.example.com",
    )

    assert outcome.decision == "confirmed_absent"
    assert outcome.allow_retry is True
    assert outcome.operator_card is False
    # NOT confirmed — the effect did not land.
    assert outcome.evidence is None or outcome.evidence.confirmed is False


# ═════════════════════════════════════════════════════════════════════════════
#  3. INDETERMINATE ⇒ operator card, never a silent retry
# ═════════════════════════════════════════════════════════════════════════════

def test_indeterminate_readback_enqueues_operator_card_never_silent_retry():
    """The read-back itself errors (TLS/timeout on the readback) ⇒ it can't decide
    ⇒ operator card + NO retry. The submit spy stays at 1."""
    key = mint_idempotency_key()
    client = _MockIdemReadbackClient(raises=TimeoutError("readback timed out too"))
    v = ExternalVerifier(api_client=client)
    obj = _Obj(3, text="pay invoice 9", params={"amount": 75},
               effect_tags={EffectTag.MONEY_MOVE})

    submits = {"n": 0}

    def _do_submit():
        submits["n"] += 1

    _do_submit()  # original submit fired once
    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=key,
        readback_url="https://api.example.com/payments?idem=" + key,
        submit_host="api.example.com",
        retry_fn=_do_submit,
    )

    assert outcome.decision == "indeterminate"
    assert outcome.operator_card is True
    assert outcome.allow_retry is False
    # never a silent retry.
    assert submits["n"] == 1
    # and NEVER concluded confirmed-absent (which would permit a double-submit).
    assert outcome.decision != "confirmed_absent"


# ═════════════════════════════════════════════════════════════════════════════
#  4. NO idempotency primitive ⇒ operator card, NEVER confirmed-absent
# ═════════════════════════════════════════════════════════════════════════════

def test_no_idempotency_primitive_falls_to_operator_card_never_confirmed_absent():
    """A target with NO idempotency primitive (server can't be keyed
    deterministically) ⇒ IMPL-6 falls to the operator card. It must NOT return
    confirmed-absent (which would permit a double-submit of a possibly-landed
    money-move)."""
    key = mint_idempotency_key()
    # server does NOT support idempotency ⇒ the readback can't decide by client key
    client = _MockIdemReadbackClient(processed_keys=[], supports_idempotency=False)
    v = ExternalVerifier(api_client=client)
    obj = _Obj(4, text="pay invoice 10", params={"amount": 999},
               effect_tags={EffectTag.MONEY_MOVE})

    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=key,
        readback_url="https://api.example.com/payments",  # no idem support
        submit_host="api.example.com",
    )

    assert outcome.decision == "indeterminate"
    assert outcome.operator_card is True
    assert outcome.allow_retry is False
    # the CRITICAL negative: it must NOT read as confirmed-absent.
    assert outcome.decision != "confirmed_absent"


def test_no_idempotency_key_at_all_falls_to_operator_card():
    """Where NO client key could be minted/injected before send (empty key) ⇒
    IMPL-6 cannot key the read-back deterministically ⇒ operator card, never
    confirmed-absent."""
    client = _MockIdemReadbackClient(processed_keys=[], supports_idempotency=True)
    v = ExternalVerifier(api_client=client)
    obj = _Obj(5, text="pay invoice 11", params={"amount": 10},
               effect_tags={EffectTag.MONEY_MOVE})

    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key="",  # none minted
        readback_url="https://api.example.com/payments",
        submit_host="api.example.com",
    )
    assert outcome.decision == "indeterminate"
    assert outcome.operator_card is True
    assert outcome.allow_retry is False
    assert outcome.decision != "confirmed_absent"


# ═════════════════════════════════════════════════════════════════════════════
#  HARDENING 2 — host-pin parity: an EMPTY submit_host must FAIL CLOSED.
#
#  The hardened _api_readback fails closed on an empty submit_host
#  (`if not submit_host or rb_host != submit_host: <reject>`), but _impl6_readback
#  only rejected on a MISMATCH (`if sub_host and rb_host and rb_host != sub_host`)
#  — so an EMPTY submit_host BYPASSED the pin and a would-confirm envelope was read
#  as confirmed_present (an UNPINNED readback confirming a money-move). Make the
#  IMPL-6 pin match _api_readback: an unpinned (empty submit_host) readback is
#  inadmissible ⇒ indeterminate, NEVER confirmed_present.
# ═════════════════════════════════════════════════════════════════════════════

def test_impl6_readback_empty_submit_host_fails_closed():
    """_impl6_readback with a would-CONFIRM envelope (supports_idempotency +
    processed_idempotency_keys containing the key) but an EMPTY submit_host must
    return indeterminate (unpinned readback inadmissible), NOT confirmed_present."""
    key = mint_idempotency_key()
    client = _MockIdemReadbackClient(processed_keys=[key], supports_idempotency=True)
    v = ExternalVerifier(api_client=client)

    # EMPTY submit_host — the readback is UNPINNED.
    decision, _detail = v._impl6_readback(
        idempotency_key=key,
        readback_url="https://api.example.com/pay?idem=" + key,
        submit_host="",
    )
    assert decision == "indeterminate", (
        "an UNPINNED (empty submit_host) idempotency readback must fail closed to "
        f"indeterminate, never confirmed_present; got {decision!r}")
    assert decision != "confirmed_present"

    # None submit_host is likewise unpinned ⇒ indeterminate.
    decision_none, _ = v._impl6_readback(
        idempotency_key=key,
        readback_url="https://api.example.com/pay?idem=" + key,
        submit_host=None,
    )
    assert decision_none == "indeterminate"


def test_impl6_readback_matching_submit_host_still_confirms():
    """No regression: a NON-empty, MATCHING submit_host still confirms_present."""
    key = mint_idempotency_key()
    client = _MockIdemReadbackClient(processed_keys=[key], supports_idempotency=True)
    v = ExternalVerifier(api_client=client)

    decision, _ = v._impl6_readback(
        idempotency_key=key,
        readback_url="https://api.example.com/pay?idem=" + key,
        submit_host="api.example.com",
    )
    assert decision == "confirmed_present"


def test_handle_ambiguous_empty_submit_host_never_credits_money_move():
    """End-to-end: an ambiguous money-move whose readback WOULD confirm but whose
    submit_host is EMPTY must route to the operator card (indeterminate), never
    credit — an unpinned confirmation could be an attacker-chosen host."""
    key = mint_idempotency_key()
    client = _MockIdemReadbackClient(processed_keys=[key], supports_idempotency=True)
    v = ExternalVerifier(api_client=client)
    obj = _Obj(9, text="pay the vendor", params={"amount": 5000},
               effect_tags={EffectTag.MONEY_MOVE})

    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=key,
        readback_url="https://api.example.com/pay?idem=" + key,
        submit_host="",   # UNPINNED
    )
    assert outcome.decision == "indeterminate"
    assert outcome.decision != "confirmed_present"
    assert outcome.operator_card is True
    assert outcome.allow_retry is False
    assert outcome.evidence is None


# ═════════════════════════════════════════════════════════════════════════════
#  5. SERVER-TOKEN KEYING IS THE BUG — a server-token impl FAILS this test
# ═════════════════════════════════════════════════════════════════════════════

def test_server_token_keying_is_undecidable_client_key_decides():
    """The lost-response case: the submit's effect LANDED but the server's
    response (which carried the SERVER token) was NEVER received. A read-back keyed
    to a SERVER token is UNDECIDABLE — the client never learned the token, so it
    cannot form the query ⇒ routed to the operator card (or refused), NEVER
    confirmed-absent. The CLIENT-key read-back decides correctly (confirmed-present
    here, since the effect landed under the client key).

    A (wrong) server-token implementation would either:
      * form the read-back with an EMPTY/unknown server token and read the effect
        as ABSENT (confirmed-absent) ⇒ a double-submit — this assertion FAILS it, or
      * key to the client key correctly ⇒ passes.
    """
    client_key = mint_idempotency_key()
    # The effect LANDED under the CLIENT key. The SERVER token was assigned server-
    # side but never delivered to the client (lost response). The server's readback
    # records ONLY the client key it processed — a server-token query would find
    # nothing (the client can't even name the server token).
    server_side_envelope = {
        "supports_idempotency": True,
        "processed_idempotency_keys": [client_key],   # keyed to the CLIENT key
        # note: NO way for the client to query by a server token it never received.
    }
    client = _MockIdemReadbackClient(envelope=server_side_envelope)
    v = ExternalVerifier(api_client=client)
    obj = _Obj(6, text="wire the funds", params={"amount": 5000},
               effect_tags={EffectTag.MONEY_MOVE})

    # (a) the CLIENT-key read-back decides correctly: confirmed-present.
    outcome_client = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=client_key,
        readback_url="https://api.example.com/wires?idem=" + client_key,
        submit_host="api.example.com",
    )
    assert outcome_client.decision == "confirmed_present", (
        "the CLIENT-key read-back must decide the lost-response case correctly — "
        "the effect landed under the client key")
    assert outcome_client.evidence.confirmed is True
    assert outcome_client.evidence.idempotency_key == client_key

    # (b) a SERVER-token keyed read-back is UNDECIDABLE (the client never received
    # the token, so the key is empty/unknown) ⇒ must NOT be confirmed-absent.
    server_token = ""   # the client never received it — this IS the bug condition
    outcome_server = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=server_token,
        readback_url="https://api.example.com/wires",
        submit_host="api.example.com",
    )
    assert outcome_server.decision != "confirmed_absent", (
        "keying the read-back to a SERVER token the client never received must be "
        "UNDECIDABLE — never confirmed-absent (that would double-submit a wire)")
    assert outcome_server.operator_card is True


# ═════════════════════════════════════════════════════════════════════════════
#  6. money-move DOUBLE-SUBMIT prevention (the headline) — submit spy == 1
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "scenario",
    ["present", "indeterminate", "no_primitive", "readback_error"],
)
def test_money_move_never_double_submits_under_any_branch(scenario):
    """An ambiguous money-move call: under NO branch that is NOT deterministically
    confirmed-absent is a second submit issued, and a lost-SUCCESS is never read as
    absent. Only a deterministic confirmed-absent permits a retry; every other
    branch keeps submit spy at 1."""
    key = mint_idempotency_key()
    obj = _Obj(7, text="pay the vendor", params={"amount": 12000},
               effect_tags={EffectTag.MONEY_MOVE})

    if scenario == "present":
        client = _MockIdemReadbackClient(processed_keys=[key])
    elif scenario == "indeterminate":
        # server supports idempotency but the readback is inconclusive (unknown)
        client = _MockIdemReadbackClient(
            envelope={"supports_idempotency": True})  # no processed_keys field ⇒ unknown
    elif scenario == "no_primitive":
        client = _MockIdemReadbackClient(processed_keys=[], supports_idempotency=False)
    else:  # readback_error
        client = _MockIdemReadbackClient(raises=ConnectionResetError("reset"))

    v = ExternalVerifier(api_client=client)

    submits = {"n": 0}

    def _do_submit():
        submits["n"] += 1

    _do_submit()   # the original (ambiguous) submit fired once
    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=key,
        readback_url="https://api.example.com/pay?idem=" + key,
        submit_host="api.example.com",
        retry_fn=_do_submit,
    )

    # a lost-success is NEVER read as absent → no branch here is confirmed-absent.
    assert outcome.decision != "confirmed_absent", (
        f"scenario={scenario}: a money-move ambiguous outcome must never be read "
        "as confirmed-absent (that risks a double-submit)")
    # under no branch is the submit issued twice.
    assert submits["n"] == 1, (
        f"scenario={scenario}: the money-move submit was issued twice — "
        "DOUBLE-SUBMIT")


def test_money_move_confirmed_absent_is_only_deterministic_permit():
    """The ONLY branch that permits a money-move retry is a deterministic
    confirmed-absent (server supports idempotency AND reports the client key
    unprocessed). Even then the retry is DEFERRED to the caller — IMPL-6 does not
    itself re-submit; it only signals allow_retry."""
    key = mint_idempotency_key()
    client = _MockIdemReadbackClient(processed_keys=[], supports_idempotency=True)
    v = ExternalVerifier(api_client=client)
    obj = _Obj(8, text="pay the vendor", params={"amount": 12000},
               effect_tags={EffectTag.MONEY_MOVE})

    submits = {"n": 0}

    def _do_submit():
        submits["n"] += 1

    _do_submit()
    outcome = v.handle_ambiguous_effect(
        objective=obj, effect_class="money_move", idempotency_key=key,
        readback_url="https://api.example.com/pay?idem=" + key,
        submit_host="api.example.com",
        retry_fn=_do_submit,
    )
    assert outcome.decision == "confirmed_absent"
    assert outcome.allow_retry is True
    # IMPL-6 signals the permit but does NOT itself re-submit.
    assert submits["n"] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  KEY MINT + INJECTION — the client key is written INTO the request before send
# ═════════════════════════════════════════════════════════════════════════════

def test_mint_idempotency_key_is_nonempty_and_unique():
    a = mint_idempotency_key()
    b = mint_idempotency_key()
    assert a and b and a != b
    assert len(a) >= 16


def test_inject_key_into_mcp_headers_spec():
    """The MCP transport target: the key is written into spec['headers']
    ['Idempotency-Key'] BEFORE send."""
    spec = {"transport": "http", "url": "https://api.example.com",
            "headers": {"Authorization": "Bearer x"}}
    key = mint_idempotency_key()
    supported = inject_idempotency_key(spec, key, target="mcp_headers")
    assert supported is True
    assert spec["headers"]["Idempotency-Key"] == key
    # existing headers preserved
    assert spec["headers"]["Authorization"] == "Bearer x"


def test_inject_key_into_mcp_headers_creates_headers_when_absent():
    spec = {"transport": "http", "url": "https://api.example.com"}
    key = mint_idempotency_key()
    supported = inject_idempotency_key(spec, key, target="mcp_headers")
    assert supported is True
    assert spec["headers"]["Idempotency-Key"] == key


def test_inject_key_into_tool_params_when_schema_declares_field():
    """A tool whose schema declares an idempotency field takes the key in params."""
    params = {"amount": 100}
    key = mint_idempotency_key()
    supported = inject_idempotency_key(
        params, key, target="tool_params",
        idempotency_field="idempotency_key")
    assert supported is True
    assert params["idempotency_key"] == key


def test_inject_key_no_target_reports_unsupported():
    """A tool with NO idempotency primitive (no header transport, no declared
    field) reports UNSUPPORTED — IMPL-6 then falls to the operator card, never
    confirmed-absent."""
    params = {"amount": 100}
    key = mint_idempotency_key()
    supported = inject_idempotency_key(
        params, key, target="tool_params", idempotency_field=None)
    assert supported is False
    # the key was NOT written (no field to write it to)
    assert "idempotency_key" not in params


# ═════════════════════════════════════════════════════════════════════════════
#  7. NON-EXTERNAL ambiguous failure ⇒ today's retry behavior UNCHANGED
# ═════════════════════════════════════════════════════════════════════════════

def test_non_external_ambiguous_failure_is_not_intercepted():
    """A non-external / non-money-move ambiguous failure is NOT an IMPL-6 concern —
    the detector reports False and the loop keeps today's retry behavior. IMPL-6
    scope is requires_external_verification effectful calls only."""
    from systemu.runtime.shadow_runtime import _is_ambiguous_effectful_failure

    class _NonExtObj:
        id = 20
        requires_external_verification = False

    # a plain (non-external) objective + a timed-out result ⇒ NOT an IMPL-6 case.
    assert _is_ambiguous_effectful_failure(
        objective=_NonExtObj(), result_dict=_ambiguous_fail()) is False


def test_external_timeout_after_send_is_detected():
    """An EXTERNAL objective + a transport-ambiguous failure (timeout after send)
    IS an IMPL-6 case."""
    from systemu.runtime.shadow_runtime import _is_ambiguous_effectful_failure

    class _ExtObj:
        id = 21
        requires_external_verification = True

    assert _is_ambiguous_effectful_failure(
        objective=_ExtObj(), result_dict=_ambiguous_fail()) is True
    # connection reset after send is also ambiguous
    assert _is_ambiguous_effectful_failure(
        objective=_ExtObj(),
        result_dict={"success": False, "timed_out": False,
                     "error": "connection reset by peer"}) is True
    # a 5xx-after-send is ambiguous
    assert _is_ambiguous_effectful_failure(
        objective=_ExtObj(),
        result_dict={"success": False, "timed_out": False,
                     "error": "502 Bad Gateway"}) is True


def test_external_clean_client_error_is_not_ambiguous():
    """An EXTERNAL objective with a CLEAN, unambiguous failure (a 4xx the server
    rejected BEFORE any effect, or a validation error) is NOT transport-ambiguous —
    the effect provably never landed, so today's retry behavior is safe. IMPL-6
    only intercepts genuinely AMBIGUOUS transport failures."""
    from systemu.runtime.shadow_runtime import _is_ambiguous_effectful_failure

    class _ExtObj:
        id = 22
        requires_external_verification = True

    # a 400 validation error — the request was rejected before any effect.
    assert _is_ambiguous_effectful_failure(
        objective=_ExtObj(),
        result_dict={"success": False, "timed_out": False,
                     "error": "400 Bad Request: invalid amount"}) is False


# ═════════════════════════════════════════════════════════════════════════════
#  Outcome dataclass sanity
# ═════════════════════════════════════════════════════════════════════════════

def test_impl6_outcome_shape():
    o = Impl6Outcome(decision="confirmed_absent", allow_retry=True,
                     operator_card=False, evidence=None, detail="x")
    assert o.decision == "confirmed_absent"
    assert o.allow_retry is True
    assert o.operator_card is False


# ═════════════════════════════════════════════════════════════════════════════
#  MID-RUN LOOP WIRING — the interception fires at the failure branch, before
#  any retry (proves the shadow_runtime wiring is not dead code). Modelled on
#  tests/test_s4_failclosed_primary.py::_drive_live_credit.
# ═════════════════════════════════════════════════════════════════════════════

def _build_entities_objs(tmp_path, objectives):
    from systemu.vault.vault import Vault
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType, Scroll,
    )
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    vault = Vault(str(tmp_path))
    shadow = Shadow(id="shadow_i6", name="I6 Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_i6", name="api_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/api_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_i6", name="I6 Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=objectives)
    vault.save_scroll(scroll)
    activity = Activity(id="act_i6", name="I6 Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_i6"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


def _redirect_snapshot_io(monkeypatch, data_dir):
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))


def _external_obj(**overrides):
    from systemu.core.models import Objective
    base = dict(id=1, goal="POST the payment to the external API",
                success_criteria="payment visible via readback",
                requires_external_verification=True)
    base.update(overrides)
    return Objective(**base)


class _LoopReadbackClient:
    """Injected onto runtime._external_api_client — a readback keyed to the
    processed CLIENT idempotency key."""
    def __init__(self, *, processed_keys=None, supports=True, raises=None):
        self._processed = list(processed_keys or [])
        self._supports = supports
        self._raises = raises
        self.calls = []

    def readback(self, url):
        self.calls.append(url)
        if self._raises is not None:
            raise self._raises
        return {"url": url, "supports_idempotency": self._supports,
                "processed_idempotency_keys": list(self._processed)}


def _drive_ambiguous(tmp_path, monkeypatch, *, external_envelope,
                     readback_client, submit_spy):
    """Drive execute(): the LLM issues one TOOL_CALL claiming the external
    objective; _handle_tool_call returns a FAILED + timed_out ToolResult carrying
    ``external_envelope`` on parsed. Each _handle_tool_call call bumps submit_spy
    (models an effectful submit). Returns (result, context)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from systemu.runtime.tool_sandbox import ToolResult

    objectives = [_external_obj()]
    vault, shadow, activity = _build_entities_objs(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    runtime = ShadowRuntime(cfg, vault)
    runtime._external_api_client = readback_client

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        submit_spy["n"] += 1   # an effectful submit fired
        return ToolResult(success=False, timed_out=True,
                          error="read timed out after send",
                          parsed={"ok": False, "external": dict(external_envelope)})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _resolve_spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        return objs, sj
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _resolve_spy)

    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "submit the payment"},
        {"action": "FAIL", "reason": "terminal — reached only if not parked/credited"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(shadow, activity))
    return result, captured.get("context")


def test_loop_confirmed_present_credits_no_resubmit(tmp_path, monkeypatch):
    """MID-RUN: an ambiguous (timed-out-after-send) effectful failure whose
    read-back CONFIRMS the effect landed ⇒ the objective is credited via the
    persisted ExternalEvidence.confirmed bit, and the effectful tool is submitted
    EXACTLY ONCE (no re-submit on the failure path)."""
    key = mint_idempotency_key()
    submit_spy = {"n": 0}
    client = _LoopReadbackClient(processed_keys=[key], supports=True)
    envelope = {"idempotency_key": key, "submit_host": "api.example.com",
                "readback_url": "https://api.example.com/pay?idem=" + key}
    result, ctx = _drive_ambiguous(
        tmp_path, monkeypatch, external_envelope=envelope,
        readback_client=client, submit_spy=submit_spy)

    # the confirmed bit was persisted → objective 1 credited (run reaches success).
    store = getattr(ctx, "_external_evidence", {}) or {}
    assert store.get("1", {}).get("confirmed") is True, (
        f"IMPL-6 confirmed-present must persist a confirmed bit; store={store}")
    assert store.get("1", {}).get("idempotency_key") == key, (
        "the persisted evidence must carry the CLIENT idempotency key (resume-deterministic)")
    # the confirmed bit credits the objective → the run finalizes success (never
    # reaching the deterministic terminal FAIL).
    assert result.get("status") == "success", (
        f"a confirmed-present ambiguous outcome must credit + finalize success; "
        f"got {result.get('status')}")
    # EXACTLY ONE submit — the failure path did NOT re-submit.
    assert submit_spy["n"] == 1, (
        f"the effectful submit must fire exactly once; fired {submit_spy['n']}x")
    assert client.calls, "the CLIENT-key read-back must have been performed"


def test_loop_indeterminate_parks_with_operator_card_no_resubmit(tmp_path, monkeypatch):
    """MID-RUN: an ambiguous effectful failure whose read-back is INDETERMINATE
    (the target has NO idempotency primitive) ⇒ the run PARKS + an operator card is
    enqueued, and the effectful tool is NOT re-submitted (submit spy stays 1)."""
    key = mint_idempotency_key()
    submit_spy = {"n": 0}
    # server does NOT support idempotency ⇒ indeterminate.
    client = _LoopReadbackClient(processed_keys=[], supports=False)
    envelope = {"idempotency_key": key, "submit_host": "api.example.com",
                "readback_url": "https://api.example.com/pay"}

    enqueued = []
    import systemu.interface.command.inbox as _inbox
    monkeypatch.setattr(_inbox.InboxQueue, "enqueue",
                        lambda self, d, *a, **k: enqueued.append(
                            getattr(d, "title", "card")) or "decision_fake")

    result, ctx = _drive_ambiguous(
        tmp_path, monkeypatch, external_envelope=envelope,
        readback_client=client, submit_spy=submit_spy)

    parked = str(result.get("status", "")).startswith("suspended")
    assert parked or enqueued, (
        "an indeterminate ambiguous outcome must park + surface an operator card; "
        f"status={result.get('status')} cards={enqueued}")
    assert result.get("status") != "success", (
        "an indeterminate ambiguous money-move must NOT finalize success")
    # NO re-submit on the failure path.
    assert submit_spy["n"] == 1, (
        f"the effectful submit must not be re-issued; fired {submit_spy['n']}x")
    # and the confirmed bit was NEVER set.
    store = getattr(ctx, "_external_evidence", {}) or {}
    assert store.get("1", {}).get("confirmed") is not True
