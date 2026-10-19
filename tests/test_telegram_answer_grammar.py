"""``/answer <tag> <value>`` - the push and the handler speak ONE grammar.

Witnessed defect: the needs-you push told the operator "Reply with /answer
<tag> <value>", but the handler accepted only option indexes a1..a4 and the
resolver mapped every index onto ``decision.options``:

  * a free-text question could not be answered at all (any real value got the
    usage text), and the only thing the handler did accept - ``a1`` - recorded
    the placeholder option "Submit", which the elicitation rail reads as a
    DECLINE;
  * an enum question's buttons were labelled with the enum values but resolved
    to ``decision.options`` (tap the first label -> "Submit" -> decline);
  * a one-field question raised as a harness INPUT gate was pushed as the gate's
    Deny/Approve buttons, so a tap recorded a denial or an empty answer.

Ruling pinned here: a question resolves only through the question branch with a
STRUCTURED value (the same ``{field: value}`` JSON the dashboard form records);
an option card keeps a1..a4 (plus its number or label); every floor stays -
the persisted resolution_class, money/irreversible never remote, allowlisted
chat only, the audit row. Real queue on a tmp FileVault; no network, no daemon.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from systemu.messaging import decision_bridge as db
from systemu.messaging.event_pusher import translate_event
from systemu.messaging.gateway import dispatch, parse_command
from systemu.messaging.handlers import default_handlers, handle_answer
from systemu.messaging.telegram_gateway import route_callback

SENDER = "111"


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)

    def subscribe(self, cb, replay=False):
        return lambda: None


@pytest.fixture
def env(tmp_path, monkeypatch):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface import notifications
    from systemu.interface.event_bus import EventBus
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault

    vault = FileVault(Vault(str(tmp_path / "v")))
    queue = OperatorDecisionQueue(vault)
    bus = _Bus()
    monkeypatch.setattr(EventBus, "get", classmethod(lambda cls: bus))
    monkeypatch.setattr(db, "_default_queue", lambda: queue)
    monkeypatch.setattr(notifications, "_decision_queue_instance", queue)
    monkeypatch.setattr(notifications, "_event_log_path", None)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", SENDER)
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "summary")
    monkeypatch.setattr(db, "_rate_hits", {})
    return SimpleNamespace(vault=vault, queue=queue, bus=bus, root=tmp_path / "v")


def _send(text, sender=SENDER):
    """What the gateway does with an inbound message: parse, then dispatch."""
    cmd = parse_command(text, user_id=sender, platform="telegram")
    return dispatch(cmd, default_handlers())


def _posted(env, decision_id):
    ev = [e for e in env.bus.events
          if e.get("category") == "operator_decision_posted"
          and (e.get("context") or {}).get("decision_id") == decision_id]
    assert len(ev) == 1
    return ev[0]


def _push(env, decision_id):
    return translate_event(_posted(env, decision_id))


def _tag(env, decision_id):
    return _posted(env, decision_id)["context"]["tag"]


def _typed_from_push(msg, value):
    """Take the example the PUSH printed and fill in a real value - the
    operator's literal next step. Nothing here re-derives the grammar."""
    line = next(l for l in msg.text.splitlines() if "/" + db.ANSWER_COMMAND in l)
    start = line.index("/" + db.ANSWER_COMMAND)
    end = line.index(db.ANSWER_VALUE_PLACEHOLDER) + len(db.ANSWER_VALUE_PLACEHOLDER)
    return line[start:end].replace(db.ANSWER_VALUE_PLACEHOLDER, value)


def _elicit(env, schema, message):
    """Drive the REAL MCP-elicitation rail; returns (decision_id, rerun)."""
    from systemu.approval.exceptions import PendingChoiceRequest
    from systemu.runtime.elicitation import resolve_structured_input

    with pytest.raises(PendingChoiceRequest) as exc:
        resolve_structured_input(message=message, requested_schema=schema)

    def rerun():
        return resolve_structured_input(message=message, requested_schema=schema)
    return exc.value.decision_id, rerun


def _free_text_schema():
    from systemu.runtime.elicitation import elicitation_schema_from_fields
    return elicitation_schema_from_fields([{"name": "answer", "type": "string"}])


def _enum_schema():
    from systemu.runtime.elicitation import elicitation_schema_from_fields
    return elicitation_schema_from_fields(
        [{"name": "level", "type": "string", "enum": ["Low", "Medium", "High"]}])


def _audit_rows(env):
    p = env.root / "messaging" / "resolve_audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]


# -- one grammar: the push example IS what the parser accepts -----------------

def test_push_example_round_trips_through_the_parser(env):
    did, _ = _elicit(env, _free_text_schema(), "How many copies should be printed?")
    msg = _push(env, did)
    tag = _tag(env, did)
    assert msg.inline_buttons == []
    example = _typed_from_push(msg, db.ANSWER_VALUE_PLACEHOLDER)
    assert example == db.answer_example(tag)
    cmd = parse_command(example, user_id=SENDER, platform="telegram")
    assert default_handlers()[cmd.command] is handle_answer
    assert db.parse_answer_args(cmd.args) == (tag, db.ANSWER_VALUE_PLACEHOLDER)


# -- free text ----------------------------------------------------------------

def test_free_text_question_answered_from_chat_records_the_value(env):
    did, rerun = _elicit(env, _free_text_schema(), "How many copies should be printed?")
    reply = _send(_typed_from_push(_push(env, did), "9 copies, stapled"))

    assert reply == "Recorded: 9 copies, stapled for How many copies should be printed."
    dec = env.vault.get_decision(did)
    assert dec.status == "resolved"
    assert json.loads(dec.choice) == {"answer": "9 copies, stapled"}
    assert dec.choice != "Submit"
    # The rail that asked gets an ACCEPT with the value - never a decline.
    assert rerun() == {"action": "accept", "content": {"answer": "9 copies, stapled"}}
    rows = _audit_rows(env)
    assert [r["outcome"] for r in rows] == ["OK"]
    assert rows[0]["decision_id"] == did


def test_free_text_question_records_a_bare_number(env):
    did, rerun = _elicit(env, _free_text_schema(), "How many copies should be printed?")
    _send(f"/answer {_tag(env, did)} 9")
    assert rerun() == {"action": "accept", "content": {"answer": "9"}}


def test_free_text_question_tap_without_a_value_records_nothing(env):
    did, _ = _elicit(env, _free_text_schema(), "How many copies should be printed?")
    tag = _tag(env, did)
    outcome, reply = route_callback(db.callback_token(tag, "a1"), SENDER)
    assert outcome == "BAD_CHOICE"
    assert db.answer_example(tag) in reply
    assert "Nothing was recorded" in reply
    assert env.vault.get_decision(did).status == "pending"


def test_closing_line_masks_before_it_truncates(env):
    # DEC-31: the secret is LONGER than the echo cap, so a truncate-then-mask
    # order would leak its head.
    secret = "sk-" + "Q" * 400
    did, _ = _elicit(env, _free_text_schema(), "Paste the run note")
    reply = _send(f"/answer {_tag(env, did)} {secret}")
    assert reply.startswith("Recorded: ")
    assert "QQQQQQQQ" not in reply
    assert "***" in reply


# -- enum ---------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["Medium", "medium", "MEDIUM", "2", "a2"])
def test_enum_question_by_label_or_index_records_the_enum_value(env, typed):
    did, rerun = _elicit(env, _enum_schema(), "Which priority level?")
    reply = _send(f"/answer {_tag(env, did)} {typed}")
    assert reply == "Recorded: Medium for Which priority level."
    assert json.loads(env.vault.get_decision(did).choice) == {"level": "Medium"}
    assert rerun() == {"action": "accept", "content": {"level": "Medium"}}


def test_enum_buttons_carry_the_enum_value(env):
    did, rerun = _elicit(env, _enum_schema(), "Which priority level?")
    msg = _push(env, did)
    assert [b.label for b in msg.inline_buttons] == ["Low", "Medium", "High"]
    outcome, reply = route_callback(msg.inline_buttons[0].callback, SENDER)
    assert outcome == "OK"
    assert reply == "Recorded: Low for Which priority level."
    assert rerun() == {"action": "accept", "content": {"level": "Low"}}


@pytest.mark.parametrize("typed", ["purple", "4", "0", "a4"])
def test_invalid_enum_value_lists_options_and_records_nothing(env, typed):
    did, _ = _elicit(env, _enum_schema(), "Which priority level?")
    reply = _send(f"/answer {_tag(env, did)} {typed}")
    assert "Nothing was recorded" in reply
    assert "1 = Low, 2 = Medium, 3 = High" in reply
    assert env.vault.get_decision(did).status == "pending"
    assert _audit_rows(env) == []


def test_unknown_tag_records_nothing(env):
    did, _ = _elicit(env, _free_text_schema(), "How many copies should be printed?")
    reply = _send("/answer zzzzzz 9")
    assert "couldn't find that decision" in reply
    assert env.vault.get_decision(did).status == "pending"


# -- option cards keep a1..a4 (and their number or label) ---------------------

def _post_tool_gate(env, *, effect_tags, title="Sync the calendar"):
    return env.queue.post(
        title=title, body="", options=["Deny", "Approve"],
        context={"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
                 "effect_tags": list(effect_tags), "destructive": False},
        dedup_key=f"t:{title}")


@pytest.mark.parametrize("typed,expected", [("a2", "Approve"), ("2", "Approve"),
                                            ("approve", "Approve"), ("a1", "Deny")])
def test_option_card_by_key_number_or_label(env, typed, expected):
    did = _post_tool_gate(env, effect_tags=["net_mutate"])
    assert env.vault.get_decision(did).context["resolution_class"] == db.RESOLUTION_REMOTE
    reply = _send(f"/answer {_tag(env, did)} {typed}")
    assert reply == f"Recorded: {expected} for Sync the calendar."
    assert env.vault.get_decision(did).choice == expected


def test_option_card_with_a_free_value_lists_options_records_nothing(env):
    did = _post_tool_gate(env, effect_tags=["net_mutate"])
    reply = _send(f"/answer {_tag(env, did)} 9")
    assert "Nothing was recorded" in reply
    assert "1 = Deny, 2 = Approve" in reply
    assert env.vault.get_decision(did).status == "pending"


# -- floors -------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["a2", "approve", "2"])
def test_money_card_is_still_refused_remotely(env, typed):
    did = _post_tool_gate(env, effect_tags=["money_move"], title="Pay the invoice")
    assert env.vault.get_decision(did).context["resolution_class"] == db.RESOLUTION_FLOOR
    reply = _send(f"/answer {_tag(env, did)} {typed}")
    assert "dashboard" in reply.lower()
    assert env.vault.get_decision(did).status == "pending"
    assert [r["outcome"] for r in _audit_rows(env)] == ["REFUSED_TYPED_CONFIRM"]


def test_non_allowlisted_chat_is_refused(env):
    did, _ = _elicit(env, _free_text_schema(), "How many copies should be printed?")
    reply = _send(f"/answer {_tag(env, did)} 9", sender="999")
    assert reply == "I don't have anything for you to resolve."
    assert env.vault.get_decision(did).status == "pending"


# -- a one-field question riding a harness INPUT gate is a QUESTION -----------

def _surface_harness_input(env, spec):
    from systemu.interface.harness_review import surface_harness_request
    req = SimpleNamespace(request_id="req_1", kind=SimpleNamespace(value="input"),
                          spec=spec, rationale="needs an operator value",
                          urgency="normal", blocking=True)
    verdict = SimpleNamespace(decision=SimpleNamespace(value="escalate"),
                              risk_band=SimpleNamespace(value="low"),
                              rationale="operator input required")
    return surface_harness_request(req, verdict, execution_id="exec_1",
                                   activity_id="act_1", shadow_id="sh_1",
                                   vault=env.vault)


def test_harness_input_gate_is_pushed_and_resolved_as_a_question(env):
    from systemu.runtime.elicitation import param_answers_from_choice

    did = _surface_harness_input(env, {"question": "How many copies?"})
    dec = env.vault.get_decision(did)
    assert dec.options == ["Deny", "Approve"]          # the gate's own options
    msg = _push(env, did)
    assert [b.label for b in msg.inline_buttons] == [], \
        "a question must never be pushed as the gate's Deny/Approve buttons"
    reply = _send(_typed_from_push(msg, "42"))
    assert reply.startswith("Recorded: 42 for ")
    dec = env.vault.get_decision(did)
    assert dec.choice not in ("Deny", "Approve")
    schema = dec.context["requested_schema"]
    # What the harness-grant reconciler extracts from the recorded choice.
    assert param_answers_from_choice(schema, json.loads(dec.choice)) == {"answer": "42"}


def test_harness_input_gate_cannot_be_answered_with_gate_keys(env):
    did = _surface_harness_input(env, {"question": "How many copies?"})
    outcome, reply = route_callback(db.callback_token(_tag(env, did), "a2"), SENDER)
    assert outcome == "BAD_CHOICE"
    assert env.vault.get_decision(did).status == "pending"


def test_harness_input_enum_gate_pushes_enum_buttons(env):
    from systemu.runtime.elicitation import elicitation_schema_from_fields
    schema = elicitation_schema_from_fields(
        [{"name": "pick", "type": "string", "enum": ["first", "second"]}])
    did = _surface_harness_input(env, {"question": "Which draft?",
                                       "requested_schema": schema})
    msg = _push(env, did)
    assert [b.label for b in msg.inline_buttons] == ["first", "second"]
    outcome, _ = route_callback(msg.inline_buttons[0].callback, SENDER)
    assert outcome == "OK"
    assert json.loads(env.vault.get_decision(did).choice) == {"pick": "first"}


# -- ASCII verdict strings ----------------------------------------------------

def test_new_reply_templates_are_ascii(env):
    did, _ = _elicit(env, _enum_schema(), "Which priority level?")
    bad = _send(f"/answer {_tag(env, did)} purple")
    ok = _send(f"/answer {_tag(env, did)} low")
    for text in (bad, ok, handle_answer(SimpleNamespace(args="", user_id=SENDER))):
        assert all(ord(c) < 128 for c in text), text
    assert re.search(r"Usage: /answer <tag> <value>", handle_answer(
        SimpleNamespace(args="onlytag", user_id=SENDER)))
