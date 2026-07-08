"""R-P1 Task 4 — push rendering: DecisionPush + render_options + MASK + the
event_pusher extension.

Three pieces under test:

  1. ``render_options(decision, *, tag)`` — keyed FIRST on
     ``context["resolution_class"]``; returns ``(surface_hint, options)`` where
     each option is ``(choice_key, label)`` and choice keys are POSITIONAL
     (``a1`` -> options[0] …) so they match the inbound resolver's
     ``a{i+1} -> decision.options[i]`` convention exactly.

  2. ``mask_outbound(text)`` — the outbound MASK chokepoint, called inside
     ``TelegramGateway.push()`` on ``message.text`` (and button labels) so no
     secret ever leaves in a push. AC9.

  3. ``event_pusher.translate_event`` ``operator_decision_posted`` — attaches the
     ``[tag]`` headline + ``inline_buttons`` and honours ``messaging_push_detail``
     / ``messaging_decision_resolution``.
"""
from __future__ import annotations

import pytest

from systemu.messaging import decision_bridge as db
from systemu.messaging.decision_bridge import DecisionPush, render_options, callback_token
from systemu.messaging.gateway import mask_outbound, OutboundMessage
from systemu.messaging.event_pusher import translate_event


# ── test doubles ──────────────────────────────────────────────────────────────

class _Dec:
    """Minimal stand-in for OperatorDecision."""
    def __init__(self, *, id="dec_x", title="Do the thing", body="",
                 options=("Deny", "Approve"), context=None):
        self.id = id
        self.title = title
        self.body = body
        self.options = list(options)
        self.context = dict(context or {})


def _remote_gate(**kw):
    ctx = {"kind": "gate", "gate_type": "tool",
           "resolution_class": "remotely_resolvable"}
    ctx.update(kw.pop("context", {}))
    return _Dec(context=ctx, **kw)


def _remote_enum(values, **kw):
    ctx = {"kind": "structured_question",
           "requested_schema": {"properties": {"choice": {"enum": list(values)}}},
           "resolution_class": "remotely_resolvable"}
    return _Dec(options=list(values), context=ctx, **kw)


def _remote_freetext(**kw):
    ctx = {"kind": "structured_question",
           "requested_schema": {"properties": {"note": {"type": "string"}}},
           "resolution_class": "remotely_resolvable"}
    return _Dec(options=["(free text)"], context=ctx, **kw)


TAG = "k3f7qa"


# ── render_options — the surface-hint + positional-option builder ─────────────

def test_gate_card_renders_positional_buttons():
    dec = _remote_gate(options=["Deny", "Approve"])
    hint, options = render_options(dec, tag=TAG)
    assert hint == "buttons"
    assert options == [("a1", "Deny"), ("a2", "Approve")]
    # AND the callback token for each is d|<tag>|a{i+1}
    assert callback_token(TAG, options[0][0]) == f"d|{TAG}|a1"
    assert callback_token(TAG, options[1][0]) == f"d|{TAG}|a2"


def test_enum_le4_renders_buttons():
    dec = _remote_enum(["low", "medium", "high"])
    hint, options = render_options(dec, tag=TAG)
    assert hint == "buttons"
    assert options == [("a1", "low"), ("a2", "medium"), ("a3", "high")]


def test_freetext_renders_reply_hint():
    dec = _remote_freetext()
    hint, options = render_options(dec, tag=TAG)
    assert hint == "reply"
    assert options == []


def test_multifield_or_5plus_is_dashboard_only():
    # 5+ options on a gate → dashboard_only.
    gate5 = _remote_gate(options=["a", "b", "c", "d", "e"])
    assert render_options(gate5, tag=TAG) == ("dashboard_only", [])
    # multi-field elicitation → dashboard_only.
    multi = _Dec(
        options=["(form)"],
        context={"kind": "structured_question",
                 "requested_schema": {"properties": {"a": {}, "b": {}}},
                 "resolution_class": "remotely_resolvable"},
    )
    assert render_options(multi, tag=TAG) == ("dashboard_only", [])


def test_floor_decision_is_dashboard_only():
    # resolution_class == "floor" → dashboard_only, zero buttons, regardless of
    # an otherwise button-shaped gate.
    dec = _Dec(options=["Deny", "Approve"],
               context={"kind": "gate", "gate_type": "tool",
                        "resolution_class": "floor"})
    assert render_options(dec, tag=TAG) == ("dashboard_only", [])
    # missing resolution_class also → dashboard_only (fail-closed).
    dec2 = _Dec(options=["Deny", "Approve"], context={"kind": "gate"})
    assert render_options(dec2, tag=TAG) == ("dashboard_only", [])


def test_callback_data_le_64_bytes_property():
    # AC10: for a long (disambiguated 8-char) tag and long option labels, every
    # emitted callback token stays within Telegram's 64-byte cap.
    long_tag = "abcdefgh"  # 8-char disambiguated form
    dec = _remote_gate(options=["X" * 40, "Y" * 40, "Z" * 40, "W" * 40])
    _, options = render_options(dec, tag=long_tag)
    for key, _label in options:
        assert len(callback_token(long_tag, key).encode("utf-8")) <= 64


def test_decision_push_is_pydantic_model():
    # DecisionPush is a pydantic BaseModel carrying the rendered push shape.
    push = DecisionPush(tag=TAG, surface_hint="buttons",
                        options=[("a1", "Deny"), ("a2", "Approve")])
    assert push.tag == TAG
    assert push.surface_hint == "buttons"
    assert push.options[0] == ("a1", "Deny")


# ── mask_outbound — the outbound MASK ─────────────────────────────────────────

def test_mask_redacts_secret_in_outbound():
    # AC9: an Authorization header value + a bearer/sk token in outbound text is
    # masked; normal prose is untouched.
    text = ("Deploy done. Authorization: Bearer sk-ABCD1234efgh5678ijkl "
            "and key sk-proj-9zXy8Wv7Uu6Tt5Ss4Rr")
    masked = mask_outbound(text)
    assert "sk-ABCD1234efgh5678ijkl" not in masked
    assert "sk-proj-9zXy8Wv7Uu6Tt5Ss4Rr" not in masked
    assert "***" in masked
    # normal prose survives
    assert "Deploy done." in masked


def test_mask_leaves_normal_prose_untouched():
    text = "Approve the deploy to staging? It touches 3 files."
    assert mask_outbound(text) == text


def test_mask_redacts_jwt_and_aws_key():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456ghi"
    aws = "AKIAIOSFODNN7EXAMPLE"
    masked = mask_outbound(f"token={jwt} awskey={aws}")
    assert jwt not in masked
    assert aws not in masked
    assert "***" in masked


# ── event_pusher.translate_event operator_decision_posted ─────────────────────

def _posted_event(*, context, options, title="Approve tool call?", tag=TAG):
    return {
        "category": "operator_decision_posted",
        "message": f"Needs you: {title}",
        "context": {
            "decision_id": "dec_abc",
            "title": title,
            "options": list(options),
            "tag": tag,
            "decision_context": dict(context),
        },
    }


def test_translate_event_attaches_tag_and_buttons(monkeypatch):
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    ev = _posted_event(
        context={"kind": "gate", "gate_type": "tool",
                 "resolution_class": "remotely_resolvable"},
        options=["Deny", "Approve"],
    )
    msg = translate_event(ev)
    assert msg is not None
    assert isinstance(msg, OutboundMessage)
    assert msg.category == "approval"
    # headline carries the [tag]
    assert f"[{TAG}]" in msg.text
    # positional inline buttons, in options order
    assert [b.label for b in msg.inline_buttons] == ["Deny", "Approve"]
    assert [b.callback for b in msg.inline_buttons] == [
        f"d|{TAG}|a1", f"d|{TAG}|a2"]


def test_translate_event_floor_has_no_buttons(monkeypatch):
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    ev = _posted_event(
        context={"kind": "gate", "resolution_class": "floor"},
        options=["Deny", "Approve"],
    )
    msg = translate_event(ev)
    assert msg is not None
    assert msg.inline_buttons == []
    # dashboard fallback text
    assert "dashboard" in msg.text.lower()


def test_translate_event_resolution_off_no_buttons(monkeypatch):
    # messaging_decision_resolution off → no buttons, old dashboard text.
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "off")
    ev = _posted_event(
        context={"kind": "gate", "gate_type": "tool",
                 "resolution_class": "remotely_resolvable"},
        options=["Deny", "Approve"],
    )
    msg = translate_event(ev)
    assert msg is not None
    assert msg.inline_buttons == []
    assert "dashboard" in msg.text.lower()


def test_translate_event_freetext_gives_answer_hint(monkeypatch):
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    ev = _posted_event(
        context={"kind": "structured_question",
                 "requested_schema": {"properties": {"note": {"type": "string"}}},
                 "resolution_class": "remotely_resolvable"},
        options=["(free text)"],
    )
    msg = translate_event(ev)
    assert msg is not None
    assert msg.inline_buttons == []
    # tells the operator to /answer <tag> <value>
    assert "/answer" in msg.text
    assert TAG in msg.text


def test_push_detail_summary_vs_full(monkeypatch):
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    body = "This tool will write to disk and hit the network. Long detail here."
    ev = _posted_event(
        context={"kind": "gate", "gate_type": "tool",
                 "resolution_class": "remotely_resolvable",
                 "body": body},
        options=["Deny", "Approve"],
    )
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "summary")
    summary_msg = translate_event(ev)
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "full")
    full_msg = translate_event(ev)
    # full carries strictly more text than summary
    assert len(full_msg.text) > len(summary_msg.text)
    assert body in full_msg.text
    assert body not in summary_msg.text


def test_translate_event_detail_is_mask_redacted(monkeypatch):
    # The push-side detail is MASK-redacted (belt to the gateway's suspenders).
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "full")
    ev = _posted_event(
        context={"kind": "gate", "gate_type": "tool",
                 "resolution_class": "remotely_resolvable",
                 "body": "run with Authorization: Bearer sk-SECRETVALUE123456789"},
        options=["Deny", "Approve"],
    )
    msg = translate_event(ev)
    assert "sk-SECRETVALUE123456789" not in msg.text
    assert "***" in msg.text
