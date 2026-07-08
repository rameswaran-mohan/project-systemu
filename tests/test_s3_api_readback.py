"""S3 / R-A7 wave2 Step 3 — the HARDENED ``_api_readback`` strategy.

``_api_readback`` is the strongest verifier strategy: it RE-READS the effect from
the SAME authenticated host over https (via an INJECTED transport client) and
matches a submission-unique token deterministically. Four hardening axes, each
tested here:

  1. host-pin — the readback host MUST equal the submit host (SSRF/DNS mirror of
     the MCP connect gate: manager.connect_and_discover host-pin CONCEPT). A
     readback from a DIFFERENT host CANNOT confirm.
  2. https-only — a non-https (http) readback CANNOT confirm.
  3. token-freshness — the matched token MUST be ABSENT from the pre-submit
     snapshot (``pre_submit_absent`` / not in ``presubmit_tokens``). A token that
     was ALREADY present pre-submit is STALE — it cannot prove THIS run produced
     the effect (create-once proof).
  4. fail-closed-on-exception — ANY transport error (TLS / timeout / connection)
     in the readback ⇒ ``confirmed=False``, NEVER raises.

Everything is deterministic (token equality) — the autouse ``_forbid_llm`` fixture
asserts no llm_router entry point is ever reached. Transports are MOCKED (an
injected client with a ``.readback(...)`` method); no real socket is opened.
"""
from __future__ import annotations

import pytest

from systemu.core.models import ExternalEvidence
from systemu.runtime.effect_tags import EffectTag
from systemu.runtime.external_verifier import ExternalVerifier


# ── the deterministic-only tripwire (mirrors the wave-1 dispatch test) ────────

@pytest.fixture(autouse=True)
def _forbid_llm(monkeypatch):
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
                 requires_external=True):
        self.id = objective_id
        self.objective_id = objective_id
        self.text = text
        self.params = params or {}
        self.effect_tags = effect_tags or set()
        self.requires_external = requires_external


# ── a MOCK readback transport client ─────────────────────────────────────────

class _MockReadbackClient:
    """An injected transport: ``readback(url)`` returns a mock envelope carrying
    the observed token(s). It NEVER opens a real socket. A callable can be passed
    to simulate a TLS/timeout/connection exception (raise-on-call)."""

    def __init__(self, *, observed_tokens=None, raises=None):
        self._observed = list(observed_tokens or [])
        self._raises = raises
        self.calls = []

    def readback(self, url):
        self.calls.append(url)
        if self._raises is not None:
            raise self._raises
        return {"url": url, "observed_tokens": list(self._observed)}


def _ev_in(**overrides):
    """A hardened api_readback evidence_input: submit_host + submission-unique
    token + a readback URL the injected client will fetch."""
    base = {
        "strategy": "api_readback",
        "submit_host": "api.example.com",
        "readback_url": "https://api.example.com/orders/REC-777",
        "expected_tokens": ["REC-777"],
        # token-freshness proof: REC-777 was ABSENT pre-submit
        "pre_submit_absent": True,
        "presubmit_tokens": ["OLD-1", "OLD-2"],
    }
    base.update(overrides)
    return base


# ── 1. token-echo match on the pinned host over https ⇒ confirmed ────────────

def test_api_readback_confirms_token_echo_on_pinned_https_host():
    client = _MockReadbackClient(observed_tokens=["REC-777", "noise"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(1, text="create the record", effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(obj, effect_class="net_mutate", evidence_input=_ev_in())
    assert isinstance(ev, ExternalEvidence)
    assert ev.confirmed is True
    assert ev.method == "api_readback"
    assert client.calls == ["https://api.example.com/orders/REC-777"]


# ── 2. readback from a DIFFERENT host than submit ⇒ host-pin refuses ─────────

def test_api_readback_refuses_host_mismatch():
    client = _MockReadbackClient(observed_tokens=["REC-777"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(2, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input=_ev_in(
            submit_host="api.example.com",
            # readback points at a DIFFERENT host — host-pin must refuse
            readback_url="https://evil.attacker.com/orders/REC-777",
        ),
    )
    assert ev.confirmed is False
    assert ev.method == "api_readback"


# ── 3. an http (non-https) readback ⇒ https-required refuses ─────────────────

def test_api_readback_refuses_non_https():
    client = _MockReadbackClient(observed_tokens=["REC-777"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(3, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input=_ev_in(
            readback_url="http://api.example.com/orders/REC-777",  # plain http
        ),
    )
    assert ev.confirmed is False
    assert ev.method == "api_readback"


# ── 4. a TLS/timeout/connection exception ⇒ fail-closed, never raises ────────

def test_api_readback_fail_closed_on_transport_exception():
    client = _MockReadbackClient(raises=TimeoutError("readback timed out"))
    v = ExternalVerifier(api_client=client)
    obj = _Obj(4, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(obj, effect_class="net_mutate", evidence_input=_ev_in())
    assert ev.confirmed is False          # fail-closed
    assert ev.method == "api_readback"    # method still recorded


def test_api_readback_fail_closed_on_connection_error():
    client = _MockReadbackClient(raises=ConnectionError("TLS handshake failed"))
    v = ExternalVerifier(api_client=client)
    obj = _Obj(5, effect_tags={EffectTag.NET_MUTATE})
    # verify() never raises even if the transport blows up
    ev = v.verify(obj, effect_class="net_mutate", evidence_input=_ev_in())
    assert ev.confirmed is False


# ── 5. token-freshness (the critical one) ────────────────────────────────────

def test_api_readback_fresh_token_absent_presubmit_confirms():
    """A token ABSENT from the pre-submit snapshot and present in the readback ⇒
    confirmed (this run provably produced it)."""
    client = _MockReadbackClient(observed_tokens=["REC-FRESH"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(6, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input=_ev_in(
            readback_url="https://api.example.com/orders/REC-FRESH",
            expected_tokens=["REC-FRESH"],
            pre_submit_absent=True,
            presubmit_tokens=["OLD-1"],          # REC-FRESH not here ⇒ fresh
        ),
    )
    assert ev.confirmed is True


def test_api_readback_stale_token_present_presubmit_refuses():
    """A token that was ALREADY present pre-submit is STALE — even echoed on the
    pinned https host it CANNOT confirm (can't prove THIS run produced it)."""
    client = _MockReadbackClient(observed_tokens=["REC-STALE"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(7, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input=_ev_in(
            readback_url="https://api.example.com/orders/REC-STALE",
            expected_tokens=["REC-STALE"],
            pre_submit_absent=False,
            presubmit_tokens=["REC-STALE"],       # ALREADY present pre-submit ⇒ stale
        ),
    )
    assert ev.confirmed is False
    assert ev.method == "api_readback"


def test_api_readback_freshness_missing_snapshot_refuses():
    """No pre-submit proof at all (pre_submit_absent False AND empty presubmit
    snapshot) ⇒ cannot establish freshness ⇒ fail-closed for the hardened path."""
    client = _MockReadbackClient(observed_tokens=["REC-777"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(8, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(
        obj, effect_class="net_mutate",
        evidence_input=_ev_in(pre_submit_absent=False, presubmit_tokens=[]),
    )
    assert ev.confirmed is False


# ── 6. a partial / mismatched token ⇒ NOT confirmed ──────────────────────────

def test_api_readback_partial_token_refuses():
    """The readback echoes only a PREFIX of the expected token — not an equality
    match ⇒ NOT confirmed."""
    client = _MockReadbackClient(observed_tokens=["REC-7"])   # partial of REC-777
    v = ExternalVerifier(api_client=client)
    obj = _Obj(9, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(obj, effect_class="net_mutate", evidence_input=_ev_in())
    assert ev.confirmed is False


def test_api_readback_mismatched_token_refuses():
    client = _MockReadbackClient(observed_tokens=["TOTALLY-DIFFERENT"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(10, effect_tags={EffectTag.NET_MUTATE})
    ev = v.verify(obj, effect_class="net_mutate", evidence_input=_ev_in())
    assert ev.confirmed is False


# ── the hardened api_readback IS the strong path — it may confirm a money-move ─

def test_api_readback_hardened_confirms_money_move():
    client = _MockReadbackClient(observed_tokens=["PAY-CONF-42"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(11, text="pay invoice 42", params={"amount": 500},
               effect_tags={EffectTag.MONEY_MOVE})
    ev = v.verify(
        obj, effect_class="money_move",
        evidence_input=_ev_in(
            readback_url="https://api.example.com/payments/PAY-CONF-42",
            expected_tokens=["PAY-CONF-42"],
            pre_submit_absent=True,
            presubmit_tokens=[],
        ),
    )
    assert ev.confirmed is True
    assert ev.method == "api_readback"


def test_api_readback_money_move_stale_token_refused():
    """Even on the strong path, a STALE token cannot confirm a money-move (the
    double-submit hazard the freshness rule exists to close)."""
    client = _MockReadbackClient(observed_tokens=["PAY-OLD"])
    v = ExternalVerifier(api_client=client)
    obj = _Obj(12, text="pay invoice", params={"amount": 500},
               effect_tags={EffectTag.MONEY_MOVE})
    ev = v.verify(
        obj, effect_class="money_move",
        evidence_input=_ev_in(
            readback_url="https://api.example.com/payments/PAY-OLD",
            expected_tokens=["PAY-OLD"],
            pre_submit_absent=False,
            presubmit_tokens=["PAY-OLD"],
        ),
    )
    assert ev.confirmed is False
