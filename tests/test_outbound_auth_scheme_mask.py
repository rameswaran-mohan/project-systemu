"""No outbound message carries the credential that follows an auth scheme.

The defect: ``gateway.mask_outbound`` ran its header/kv rule first, and that rule
replaced only the FIRST word after ``Authorization:`` -- the scheme word. So
``Authorization: Bearer <t>`` became ``Authorization: *** <t>``, and the Bearer
rule that ran next no longer saw a scheme in front of ``<t>``. The token left
in clear unless it happened to match a token SHAPE (``sk-``, a JWT, long hex).
The shipped pin used an ``sk-`` token, so the shape rule caught it and hid the
gap.

Fixture rule (DEC-31): every secret here is LONGER than every cap on the path
(82-char token, 104-char Basic value; the capped entry points are called with a
cap SHORTER than the secret), and none of them matches a token shape, so only
the scheme rule can mask them. A leak is detected by any 10-character window of
the secret surviving, not only the whole value, so a partly masked credential
still fails.

Every entry point is driven through its own public surface:

  * ``gateway.mask_outbound`` -- the outbound chokepoint;
  * ``TelegramGateway.push`` -- what actually reaches ``send_message`` (text
    and button label);
  * ``event_pusher.translate_event`` -- the push composer's detail body;
  * ``durable_redact.mask_text`` / ``mask_values`` / ``mask_params`` /
    ``mask_event`` / ``mask_display`` -- the durable-writer mask, which shares
    the primitive;
  * ``outbox.redact`` -- the file-export fence, which also shares it.

Readability is pinned too: on the outbound surface the header name, the scheme
word and the prose on both sides survive, so a masked push still says what
failed.
"""
from __future__ import annotations

import base64
import json

import pytest

from systemu.messaging.gateway import InlineButton, OutboundMessage, mask_outbound

# ── fixtures ────────────────────────────────────────────────────────────────

#: 82 chars, letters + digits + token68 punctuation. Not hex, not ``sk-``, not a
#: JWT, not ``ghp_``/``AKIA``/Slack: no shape rule can see it.
TOKEN = "Zq7aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ5aB6cD7eF8gH9iJ0kL1mN2oP3qR4s-uV5.wX6~yZ7"

#: base64("svc-deploy:<66-char password>") -- 104 chars with ``=`` padding.
BASIC = base64.b64encode(
    ("svc-deploy:" + "Corr3ct-H0rse_Battery.Staple~2026" * 2).encode()).decode()

#: A password for URL userinfo, also longer than any cap and shapeless.
URL_PW = "pW9" + TOKEN

PRE = "Push to the CRM failed:"
POST = "-- rotate it and retry tomorrow."

_WINDOW = 10


def test_fixtures_obey_dec31_and_dodge_every_shape_rule():
    """The fixtures are only meaningful if they are long and shapeless: if a
    shape rule caught them, a broken scheme rule would still pass."""
    from systemu.messaging import gateway as gw
    assert len(TOKEN) > 64 and len(BASIC) > 64 and len(URL_PW) > 64
    assert BASIC.endswith("=")
    for secret in (TOKEN, BASIC, URL_PW):
        for rx in gw._TOKEN_SHAPE_RES:
            assert not rx.search(secret), (rx.pattern, secret)
        assert mask_outbound(secret) == secret, "a bare value must not be masked by shape"


def _leaks(out: str, secret: str) -> list:
    """Every ``_WINDOW``-char slice of ``secret`` that survived into ``out``."""
    return [secret[i:i + _WINDOW] for i in range(len(secret) - _WINDOW + 1)
            if secret[i:i + _WINDOW] in out]


def _assert_no_leak(out: str, secret: str) -> None:
    found = _leaks(out, secret)
    assert not found, f"credential bytes survived the mask: {found[:3]} in {out!r}"


def _msg(form: str) -> str:
    return f"{PRE} {form} {POST}"


# ── the spellings ───────────────────────────────────────────────────────────

#: (id, form, secret, words that must survive on the outbound surface)
HEADER_FORMS = [
    ("authorization-bearer", f"Authorization: Bearer {TOKEN}", TOKEN, ["Authorization", "Bearer"]),
    ("authorization-basic", f"Authorization: Basic {BASIC}", BASIC, ["Authorization", "Basic"]),
    ("authorization-token", f"Authorization: Token {TOKEN}", TOKEN, ["Authorization", "Token"]),
    ("lowercase-header-bearer", f"authorization: bearer {TOKEN}", TOKEN, ["authorization", "bearer"]),
    ("lowercase-header-basic", f"authorization: Basic {BASIC}", BASIC, ["authorization", "Basic"]),
    ("lowercase-header-token", f"authorization: token {TOKEN}", TOKEN, ["authorization", "token"]),
    ("uppercase-header-bearer", f"AUTHORIZATION: BEARER {TOKEN}", TOKEN, ["AUTHORIZATION", "BEARER"]),
    ("proxy-authorization-basic", f"Proxy-Authorization: Basic {BASIC}", BASIC, ["Proxy-Authorization", "Basic"]),
    ("proxy-authorization-bearer", f"proxy-authorization: Bearer {TOKEN}", TOKEN, ["proxy-authorization", "Bearer"]),
    ("no-space-after-colon", f"Authorization:Bearer {TOKEN}", TOKEN, ["Authorization", "Bearer"]),
    ("equals-separator", f"Authorization=Basic {BASIC}", BASIC, ["Authorization", "Basic"]),
    ("scheme-then-newline", f"Authorization: Bearer\n{TOKEN}", TOKEN, ["Authorization", "Bearer"]),
]

BARE_FORMS = [
    ("bare-bearer", f"Bearer {TOKEN}", TOKEN, ["Bearer"]),
    ("bare-bearer-lowercase", f"bearer {TOKEN}", TOKEN, ["bearer"]),
    ("bare-basic", f"Basic {BASIC}", BASIC, ["Basic"]),
    ("bare-token", f"Token {TOKEN}", TOKEN, ["Token"]),
    ("bare-token-lowercase", f"token {TOKEN}", TOKEN, ["token"]),
]

JSON_FORMS = [
    ("json-bearer", '{"Authorization": "Bearer %s", "retry": true}' % TOKEN, TOKEN,
     ["Authorization", "Bearer", '"retry": true}']),
    ("json-compact-basic", '{"authorization":"Basic %s","retry":true}' % BASIC, BASIC,
     ["authorization", "Basic", '"retry":true}']),
    ("json-token", '{"Authorization": "Token %s"}' % TOKEN, TOKEN, ["Authorization", "Token"]),
    ("json-proxy-basic", '"Proxy-Authorization": "Basic %s"' % BASIC, BASIC,
     ["Proxy-Authorization", "Basic"]),
    ("python-repr-bearer", "{'Authorization': 'Bearer %s', 'retry': True}" % TOKEN, TOKEN,
     ["Authorization", "Bearer", "'retry': True}"]),
    ("escaped-json-token", '{\\"Authorization\\": \\"Token %s\\"}' % TOKEN, TOKEN,
     ["Authorization", "Token"]),
]

URL_FORMS = [
    ("url-userinfo", f"https://alice:{URL_PW}@example.com/api/v1", URL_PW,
     ["https://alice:", "@example.com/api/v1"]),
    ("url-userinfo-empty-user", f"redis://:{URL_PW}@cache.internal:6379/0", URL_PW,
     ["redis://:", "@cache.internal:6379/0"]),
    ("url-userinfo-at-in-password", f"postgres://svc:p@ss{URL_PW}@db.internal/prod", URL_PW,
     ["postgres://svc:", "@db.internal/prod"]),
]

ALL_FORMS = HEADER_FORMS + BARE_FORMS + JSON_FORMS + URL_FORMS
_IDS = [f[0] for f in ALL_FORMS]


# ── 1. gateway.mask_outbound: no credential, readable message ──────────────

@pytest.mark.parametrize("fid,form,secret,keep", ALL_FORMS, ids=_IDS)
def test_mask_outbound_masks_the_credential_after_every_auth_spelling(fid, form, secret, keep):
    out = mask_outbound(_msg(form))
    _assert_no_leak(out, secret)
    assert "***" in out


@pytest.mark.parametrize("fid,form,secret,keep", ALL_FORMS, ids=_IDS)
def test_mask_outbound_keeps_the_scheme_and_the_prose_readable(fid, form, secret, keep):
    out = mask_outbound(_msg(form))
    assert out.startswith(PRE), out
    assert out.endswith(POST), out
    for word in keep:
        assert word in out, f"{word!r} was masked away: {out!r}"


@pytest.mark.parametrize("scheme", ["Negotiate", "NTLM", "DPoP", "Bot", "SSWS", "ApiKey", "Api-Key"])
def test_other_header_schemes_keep_the_scheme_and_lose_the_credential(scheme):
    out = mask_outbound(_msg(f"Authorization: {scheme} {TOKEN}"))
    _assert_no_leak(out, TOKEN)
    assert f"Authorization: {scheme} ***" in out, out
    assert out.endswith(POST)


def test_the_original_sk_shaped_pin_is_no_longer_what_hides_the_gap():
    """The shipped pin used ``Bearer sk-...``. Re-run it with the shape rules
    unable to help: the header rule alone has to mask it."""
    out = mask_outbound(f"Deploy done. Authorization: Bearer {TOKEN} and retry")
    assert out == "Deploy done. Authorization: Bearer *** and retry"


# ── negative controls: ordinary prose is left alone ─────────────────────────

@pytest.mark.parametrize("text", [
    "Approve the deploy to staging? It touches 3 files.",
    "Token budget exceeded for this run; 1200 tokens used.",
    "Token refresh scheduled; basic plan renews on Friday.",
    "Mail alice@example.com about the token refresh.",
    "Open https://example.com:8443/status and https://alice@example.com/inbox",
    "Order token 1234567890123456 shipped.",
])
def test_ordinary_prose_is_untouched(text):
    assert mask_outbound(text) == text


# ── 2. the Telegram push: what reaches send_message ─────────────────────────

class _Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, user_id, message):
        self.sent.append((user_id, message))


def _push_through_telegram(text: str, label: str):
    from systemu.messaging.telegram_gateway import TelegramGateway
    gw = TelegramGateway(bot_token="123:test-not-a-real-token", allowlist={"42"})
    gw._app = object()                  # "started": push() refuses before start()
    rec = _Recorder()
    gw._send_to = rec                   # the transport call, captured
    gw.push(OutboundMessage(
        text=text, inline_buttons=[InlineButton(label=label, callback="d|t|a1")]))
    assert len(rec.sent) == 1
    return rec.sent[0][1]


@pytest.mark.parametrize("fid,form,secret,keep", ALL_FORMS, ids=_IDS)
def test_telegram_push_never_sends_the_credential(fid, form, secret, keep):
    sent = _push_through_telegram(_msg(form), form)
    _assert_no_leak(sent.text, secret)
    _assert_no_leak(sent.inline_buttons[0].label, secret)
    assert sent.inline_buttons[0].callback == "d|t|a1"
    for word in keep:
        assert word in sent.text


def test_push_composer_detail_body_never_carries_the_credential(monkeypatch):
    from systemu.messaging.event_pusher import translate_event
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "full")
    body = _msg(f"Authorization: Basic {BASIC} and Proxy-Authorization: Token {TOKEN}")
    ev = {
        "category": "operator_decision_posted",
        "message": "Needs you: Approve tool call?",
        "context": {
            "decision_id": "dec_abc", "title": "Approve tool call?",
            "options": ["Deny", "Approve"], "tag": "k3f7qa",
            "decision_context": {"kind": "gate", "gate_type": "tool",
                                 "resolution_class": "remotely_resolvable",
                                 "body": body},
        },
    }
    msg = translate_event(ev)
    _assert_no_leak(msg.text, BASIC)
    _assert_no_leak(msg.text, TOKEN)
    assert "Authorization: Basic ***" in msg.text
    assert "Proxy-Authorization: Token ***" in msg.text


# ── 3. the durable-writer mask, through its own entry points ────────────────

def _durable_entry_points():
    from systemu.runtime import durable_redact as dr

    def _text(s):
        return dr.mask_text(s)

    def _text_capped(s):
        # DEC-31: the cap is SHORTER than the secret; the mask runs first.
        return dr.mask_text(s, max_len=len(PRE) + 48)

    def _text_structured(s):
        return dr.mask_text(json.dumps({"note": s}), structured=True)

    def _values(s):
        return json.dumps(dr.mask_values({"note": s, "nested": [s]}))

    def _params(s):
        return json.dumps(dr.mask_params({"note": s, "nested": {"body": s}}))

    def _event(s):
        return json.dumps(dr.mask_event({"message": s, "details": {"result": s}}))

    def _display(s):
        return dr.mask_display({"note": s})

    return [("mask_text", _text), ("mask_text-capped", _text_capped),
            ("mask_text-structured", _text_structured), ("mask_values", _values),
            ("mask_params", _params), ("mask_event", _event),
            ("mask_display", _display)]


_DURABLE = _durable_entry_points()


@pytest.mark.parametrize("entry", [e[1] for e in _DURABLE], ids=[e[0] for e in _DURABLE])
@pytest.mark.parametrize("fid,form,secret,keep", ALL_FORMS, ids=_IDS)
def test_durable_writer_mask_never_persists_the_credential(fid, form, secret, keep, entry):
    from systemu.runtime.durable_redact import UNREDACTABLE
    out = entry(_msg(form))
    assert UNREDACTABLE not in out, "the mask must succeed, not drop the value"
    _assert_no_leak(out, secret)


@pytest.mark.parametrize("fid,form,secret,keep", ALL_FORMS, ids=_IDS)
def test_durable_mask_text_keeps_the_surrounding_prose(fid, form, secret, keep):
    from systemu.runtime.durable_redact import mask_text
    out = mask_text(_msg(form))
    assert out.startswith(PRE) and out.endswith(POST), out


# ── 4. the file-export fence shares the primitive too ───────────────────────

@pytest.mark.parametrize("fid,form,secret,keep", ALL_FORMS, ids=_IDS)
def test_outbox_redact_never_exports_the_credential(fid, form, secret, keep):
    from systemu.runtime.outbox import redact
    _assert_no_leak(redact(_msg(form)), secret)
    _assert_no_leak(redact(_msg(form), max_len=40), secret)
