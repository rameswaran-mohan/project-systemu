"""S3 wave1 Step 5 — the MASK evidence-redaction pass (§5.8 AC).

Before any captured evidence (HTTP headers, storage_state, response bodies) is
stored or logged, secret material must be removed. ``_mask_evidence`` does a
key-targeted redaction (case-insensitive secret keys, recursing into nested
dicts/lists) PLUS a value-level regex backstop for known secret shapes
(sk-…, ghp_…, Bearer …). The §5.8 acceptance criterion: a token echoed in a mock
response is ABSENT from the masked output.
"""
from __future__ import annotations

import json

from systemu.runtime.external_verifier import _mask_evidence


def _flatten_to_str(obj) -> str:
    """Serialise the whole masked structure so we can assert a secret is ABSENT
    anywhere in it (key OR value, nested)."""
    return json.dumps(obj, default=str)


def test_masks_all_secret_material_and_keeps_nonsecret():
    ev = {
        "headers": {
            "Authorization": "Bearer xyz",
            "Cookie": "session=abc",
            "Set-Cookie": "sid=deadbeef; HttpOnly",
            "Content-Type": "application/json",   # non-secret — must survive
        },
        "storage_state": {"token": "sk-secret0123456789ABCDEFGH"},
        "body": "raw sk-ABC123DEF456GHI789JKL0 value",
        "status": 200,                            # non-secret — must survive
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)

    # ── the §5.8 AC: NO secret material survives ──
    assert "Bearer xyz" not in blob
    assert "xyz" not in blob                       # the bare bearer value gone too
    assert "session=abc" not in blob
    assert "deadbeef" not in blob
    assert "sk-secret0123456789ABCDEFGH" not in blob
    assert "sk-ABC123DEF456GHI789JKL0" not in blob

    # ── non-secret fields survive ──
    assert masked["headers"]["Content-Type"] == "application/json"
    assert masked["status"] == 200


def test_echoed_token_absent_from_masked_output():
    """A distinguishing token that appears in a (mock) response body AND as an auth
    secret must not leak through — the §5.8 'echoed token absent' AC."""
    secret = "sk-live-9f8e7d6c5b4a3210ZYXW"
    ev = {
        "request_headers": {"authorization": f"Bearer {secret}"},
        "response_body": f"...created with key {secret}...",
        "nested": [{"api_key": secret}, {"note": "ok"}],
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert secret not in blob


def test_case_insensitive_secret_keys():
    ev = {
        "AUTHORIZATION": "Bearer TOP",
        "Cookie": "x=1",
        "SESSION": "sess-9",
        "Api_Key": "sk-aaaaaaaaaaaaaaaaaaaaaa",
        "Password": "hunter2hunter2",
        "SECRET": "s3cr3tvalue",
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert "Bearer TOP" not in blob
    assert "TOP" not in blob
    assert "sess-9" not in blob
    assert "sk-aaaaaaaaaaaaaaaaaaaaaa" not in blob
    assert "hunter2hunter2" not in blob
    assert "s3cr3tvalue" not in blob


def test_recurses_nested_dicts_and_lists():
    ev = {
        "outer": {
            "inner": {"token": "sk-nested000000000000000000"},
            "list": [{"cookie": "c=deep"}, {"ok": "keepme"}],
        }
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert "sk-nested000000000000000000" not in blob
    assert "c=deep" not in blob
    assert "keepme" in blob            # non-secret survives


def test_value_level_regex_backstop_for_bare_secret_in_neutral_key():
    """Even under a NON-secret key, a value that LOOKS like a known secret shape
    (sk-…, ghp_…, Bearer …) is scrubbed by the value-level backstop."""
    ev = {
        "note": "the key is sk-backstop00000000000000000 embedded here",
        "log": "Authorization was Bearer abc.def.ghi",
        "gh": "token ghp_0123456789012345678901234567890123AB",
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert "sk-backstop00000000000000000" not in blob
    assert "Bearer abc.def.ghi" not in blob
    assert "ghp_0123456789012345678901234567890123AB" not in blob


def test_none_and_scalar_inputs_do_not_raise():
    assert _mask_evidence(None) is not None or _mask_evidence(None) is None  # no raise
    # a scalar / empty passes through without error
    _mask_evidence({})
    _mask_evidence({"a": 1, "b": None, "c": True})


# ── MEDIUM leak: a secret under a NEUTRAL key whose SHAPE wasn't listed ──────

def test_aws_key_under_neutral_key_is_redacted():
    """A secret under a NEUTRAL key (its key name is not a secret hint) whose VALUE
    is an AWS access-key shape (AKIA…) previously leaked. It must be scrubbed by
    the value-level backstop."""
    akia = "AKIAIOSFODNN7EXAMPLE"          # canonical AWS access-key shape
    asia = "ASIA1234567890ABCDEF"          # STS temporary-key shape
    ev = {
        "note": f"the operator used {akia} to authenticate",
        "meta": {"trace": f"session assumed via {asia}"},
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert akia not in blob
    assert asia not in blob


def test_jwt_under_neutral_data_key_is_redacted():
    """A JWT (eyJ…header.payload.sig) under a neutral 'data' key must be scrubbed."""
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
           ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
           ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
    ev = {"data": f"bearer payload {jwt} received", "ok": "status good"}
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert jwt not in blob
    # the JWT header prefix must not leak either
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in blob
    # a benign non-secret note survives
    assert "status good" in blob


def test_raw_session_hex_under_sess_key_is_redacted():
    """A raw high-entropy session id (32+ hex) under a 'sess' key must be redacted —
    both by the broadened key hint AND the high-entropy value backstop."""
    sess_hex = "deadbeefcafebabe0123456789abcdef0011223344556677"  # 48 hex
    ev = {
        "sess": sess_hex,                       # key-hint path
        "log": f"resumed session {sess_hex} ok",  # value-backstop path (neutral key)
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert sess_hex not in blob


def test_broadened_secret_key_hints_redact_values():
    """The broadened key hints (sess/jwt/bearer/auth/sig/credential/key) redact the
    value wholesale regardless of its shape."""
    ev = {
        "jwt": "opaque-value-1",
        "bearer": "opaque-value-2",
        "auth": "opaque-value-3",
        "sig": "opaque-value-4",
        "credential": "opaque-value-5",
        "signing_key": "opaque-value-6",
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    for i in range(1, 7):
        assert f"opaque-value-{i}" not in blob


def test_benign_short_ids_and_prose_survive_no_over_redaction():
    """The high-entropy hex backstop is length-gated (32+) so it must NOT scrub
    benign short ids, order numbers, or ordinary prose."""
    ev = {
        "order_id": "ORD-12345",
        "short_hex": "deadbeef",                  # 8 hex — too short to be a secret
        "uuid_ish": "abc123",
        "status": 200,
        "message": "The record was created successfully at row 42.",
        "commit": "a1b2c3d",                      # 7-char short git sha — survives
    }
    masked = _mask_evidence(ev)
    blob = _flatten_to_str(masked)
    assert "ORD-12345" in blob
    assert "deadbeef" in blob
    assert "abc123" in blob
    assert "The record was created successfully at row 42." in blob
    assert "a1b2c3d" in blob
    assert masked["status"] == 200
