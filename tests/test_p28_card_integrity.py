"""P28 (GAP 1) -- every tool's approval card shows, and its gate scores and signs,
the parameters the tool will actually RECEIVE.

The defect: the launch / close / shell tools were sealed (P23, P26), but every OTHER
tool's card still showed ``_small_args_preview`` of the RAW parameters -- 8 keys,
80 characters, capped BEFORE redaction -- while the registry reconciled them again
at execution. So a 9th key the tool consumed was never shown, a secret was sliced
before the redactor could recognise it (DEC-31), and on the in-process path the
gate's fingerprint, args preview, typed-confirm phrase and destructive check scored
RAW parameters while the registry ran RECONCILED ones.

THE PROPERTY pinned here, against the REAL sandbox and the REAL registry over a
real tool body that records the kwargs it receives:

  * every call is sealed once (``ToolSandbox._seal_call``) and execution consumes
    the sealed value; the per-tool gate scores, signs and shows that value;
  * the card lists EVERY key the tool receives (a 9th key reconciled onto ``url``
    is listed under the name the tool takes), each value redacted BEFORE it is
    capped, by the ONE card-preview function (``_card_args_preview``) -- the MCP
    card included;
  * the in-process gate's fingerprint equals the fingerprint of what the tool
    received, and the card's args line is what the tool received;
  * the fingerprint migration: a one-shot record minted against the parameters as
    the model WROTE them does not carry over -- the call asks once more, and the
    log and the card say so (never a silent widening).

Mutations (each checked; each turns the named tests red):
  * card built from the raw params (``_small_args_preview`` of the as-written call)
      -> test_a_ninth_reconciled_key_is_on_a_generic_tools_card,
         test_the_card_shows_exactly_what_the_tool_then_receives,
         test_a_secret_longer_than_every_cap_is_absent_from_the_tool_card,
         test_a_secret_in_a_parameter_name_is_absent_from_the_card
  * cap before redact (``redact(text[:cap])`` in ``_card_text``)
      -> test_a_secret_longer_than_every_cap_is_absent_from_the_tool_card,
         test_a_secret_in_a_parameter_name_is_absent_from_the_card,
         test_a_secret_longer_than_every_cap_is_absent_from_the_mcp_card
  * the gate scores the raw call while the tool gets the reconciled one
      -> test_a_ninth_reconciled_key_is_on_a_generic_tools_card,
         test_the_card_shows_exactly_what_the_tool_then_receives,
         test_the_in_process_gate_fingerprint_is_what_the_tool_receives,
         test_a_reclassification_recorded_as_written_asks_once_more,
         test_a_typed_approval_stamped_as_written_asks_once_more
"""
from __future__ import annotations

import asyncio
import json
import logging
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.approval.exceptions import PendingOperatorDecision
from systemu.runtime.command_approvals import CommandApprovalStore

TOOL = "p28_notify_peer"

#: Longer than EVERY cap on these card paths (a value: 300; a key: 80; the old
#: preview: 80) -- and a shape the redactor recognises only WHOLE: a JWT sliced
#: before redaction keeps its clear prefix (DEC-31 fixture rule).
SECRET = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ"
          + "c3ViIjoiMTIzNDU2Nzg5MCIs" * 16 + "."
          + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" * 2)

_BODY = '''
import json
from pathlib import Path

_SEEN = Path({seen!r})
_UNSET = object()


def run(url, content, a1=_UNSET, a2=_UNSET, a3=_UNSET, a4=_UNSET, a5=_UNSET,
        a6=_UNSET, a7=_UNSET, amount=_UNSET):
    got = {{k: v for k, v in dict(locals()).items() if v is not _UNSET}}
    with _SEEN.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(got, sort_keys=True) + "\\n")
    return {{"success": True, "sent": True}}
'''


def _nine_keys(**over):
    """Seven keys the tool takes by name, ``content``, and a 9th -- ``link`` -- that
    the registry reconciles onto ``url``. As the model wrote it, the 9th key is the
    one an 8-key preview drops."""
    params = {"a{}".format(i): "v{}".format(i) for i in range(1, 8)}
    params["content"] = "hello"
    params["link"] = "https://example.test/r"
    params.update(over)
    return params


def _shown(card) -> str:
    return "\n".join((card.d.inspect, card.d.what_approve_does, card.d.title,
                      json.dumps(card.extras, sort_keys=True, default=str)))


def _leaks(secret: str, text: str, width: int = 24) -> bool:
    return any(secret[i:i + width] in text
               for i in range(0, len(secret) - width + 1, 8))


def _args_line(card) -> str:
    lines = [ln for ln in card.d.inspect.splitlines() if ln.startswith("args: ")]
    assert len(lines) == 1, card.d.inspect
    return lines[0]


class _Vault:
    """The registry's vault: the tool enabled and dry-run-passed, no schema."""

    def find_tool_by_name(self, name):
        return SimpleNamespace(
            name=name, id="tool_" + name, version=1, enabled=True,
            parameters_schema={}, dry_run_status="passed",
            requires_credentials=[], credential_refusals=[], dependencies=[],
            timeout_seconds=None)


@pytest.fixture
def p(tmp_path, monkeypatch):
    """The real gate over a real tool body. Witnesses: ``seen()`` (the kwargs the
    body received, one dict per run), ``posted`` (every card with its context
    extras), ``events`` (operator notifications)."""
    monkeypatch.chdir(tmp_path)
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir()
    seen_file = tmp_path / "seen.jsonl"
    impl = impl_dir / (TOOL + ".py")
    impl.write_text(textwrap.dedent(_BODY.format(seen=str(seen_file))), encoding="utf-8")

    posted = []

    class _Inbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, policy=None, context_extras=None,
                    **kw):
            posted.append(SimpleNamespace(d=descriptor, gate_type=gate_type,
                                          extras=dict(context_extras or {})))
            return "dec_p28_{}".format(len(posted))

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _Inbox)
    events = []
    import systemu.interface.notifications as notifications
    monkeypatch.setattr(notifications, "log_event",
                        lambda *a, **k: events.append((a, k)))
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    tool = SimpleNamespace(name=TOOL, effect_tags=["send_message"],
                           forged_by_systemu=False, id="tool_" + TOOL, version=1,
                           implementation_path=str(impl))

    def call(params, *, resolved=None, force_subprocess=False):
        from systemu.runtime.tool_registry import ToolRegistry
        from systemu.runtime.tool_sandbox import ToolSandbox
        reg = ToolRegistry(impl_dir, _Vault())
        sb = ToolSandbox(vault_root=tmp_path / "vault", vault=object(),
                         command_approvals=store, registry=reg)
        return asyncio.run(sb.execute_tool(str(impl), params, tool=tool,
                                           _command_gate_resolved=resolved,
                                           force_subprocess=force_subprocess))

    def card(params, **kw):
        before = len(posted)
        with pytest.raises(PendingOperatorDecision) as ei:
            call(params, **kw)
        assert len(posted) == before + 1, "the call did not post exactly one card"
        c = posted[-1]
        c.raised = ei.value
        return c

    def seen():
        if not seen_file.exists():
            return []
        return [json.loads(ln) for ln in seen_file.read_text("utf-8").splitlines() if ln]

    return SimpleNamespace(call=call, card=card, seen=seen, posted=posted,
                           events=events, store=store, tool=tool, tmp=tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
# 1. The card shows every key the tool receives
# ─────────────────────────────────────────────────────────────────────────────

def test_a_ninth_reconciled_key_is_on_a_generic_tools_card(p):
    """Nine keys as written; the 9th (``link``) is reconciled onto ``url``. The
    card lists all nine under the names the tool takes -- ``url`` included -- and
    never the name the tool does not receive."""
    c = p.card(_nine_keys())
    line = _args_line(c)
    assert "url='https://example.test/r'" in line, line
    assert "link" not in line, line
    for i in range(1, 8):
        assert "a{}='v{}'".format(i, i) in line, line
    assert "content='hello'" in line, line
    assert p.seen() == [], "the gated call ran before it was approved"


def test_the_card_shows_exactly_what_the_tool_then_receives(p):
    """The card's args line is rendered from the very kwargs the body later
    receives (after an "Always allow" of that card)."""
    from systemu.interface.command.gate import card_args_line
    from systemu.runtime.tool_sandbox import _card_args_preview
    c = p.card(_nine_keys())
    assert p.store.approve(c.extras["tool_signature"], command=TOOL)
    res = p.call(_nine_keys())
    assert res.success, res.error
    assert res.gate["passed_via"] == "standing_approval"
    (got,) = p.seen()
    assert got["url"] == "https://example.test/r" and "link" not in got
    assert _args_line(c) == card_args_line(_card_args_preview(got))


def test_the_in_process_gate_fingerprint_is_what_the_tool_receives(p):
    """The fingerprint the gate stamps on the card (and binds one-shot records to)
    is computed from the parameters the tool RECEIVES -- not the call as written."""
    from systemu.runtime.tool_sandbox import args_fingerprint
    written = _nine_keys()
    c = p.card(written)
    assert p.store.approve(c.extras["tool_signature"], command=TOOL)
    assert p.call(written).success
    (got,) = p.seen()
    assert c.extras["args_fingerprint"] == args_fingerprint(got)
    assert c.extras["args_fingerprint"] != args_fingerprint(written)


def test_an_approve_once_of_the_card_runs_the_call_it_showed(p):
    """The chat lane's approve-once: the resolved decision carries the card's
    (sealed) fingerprint, so it witnesses the call the tool receives and runs it."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    c = p.card(_nine_keys())
    decision = SimpleNamespace(id="dec_once", choice="Approve once",
                               context=dict(c.extras))

    class _Q:
        def __init__(self, vault):
            pass

        def consume_resolved_decision(self, dedup):
            assert dedup == c.d.dedup
            return decision

    import systemu.approval.decision_queue as dq
    orig = dq.OperatorDecisionQueue
    dq.OperatorDecisionQueue = _Q
    try:
        res = p.call(_nine_keys(), resolved=c.d.dedup)
    finally:
        dq.OperatorDecisionQueue = orig
    assert OperatorDecisionQueue is orig
    assert res.success and res.gate["passed_via"] == "approve_once", res
    assert len(p.seen()) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. DEC-31: redacted BEFORE capped, on every card
# ─────────────────────────────────────────────────────────────────────────────

def test_a_secret_longer_than_every_cap_is_absent_from_the_tool_card(p):
    assert len(SECRET) > 300
    c = p.card(_nine_keys(content="note: " + SECRET))
    assert not _leaks(SECRET, _shown(c)), _args_line(c)
    assert not _leaks(SECRET, str(c.raised))
    assert "redacted" in _args_line(c)
    assert p.seen() == []


def test_a_secret_in_a_parameter_name_is_absent_from_the_card(p):
    """On the subprocess path the call is sealed as given, so a model-chosen key
    reaches the tool -- and the card. The key is redacted before it is capped too;
    two keys that render alike are both listed."""
    params = _nine_keys()
    params[SECRET] = "x"
    params[SECRET + "2"] = "y"
    c = p.card(params, force_subprocess=True)
    line = _args_line(c)
    assert not _leaks(SECRET, _shown(c)), line
    assert line.count("redacted") >= 2, line
    assert "link='https://example.test/r'" in line   # sealed as given: what the runner gets


def test_a_secret_longer_than_every_cap_is_absent_from_the_mcp_card():
    """The MCP card renders the same card-preview function: every argument, each
    redacted before it is capped (it used to slice the JSON to 300 characters)."""
    from systemu.interface.command.gate import GateDescriptor
    params = {"to_account": "acct-9", "memo": "note: " + SECRET, "k" * 3: 1}
    for extra in ({}, {"z{:02d}".format(i): i for i in range(30)}):
        d = GateDescriptor.from_mcp_call(server="srv", tool="send", params={
            **params, **extra}, destructive=False)
        text = d.inspect + d.what_approve_does
        assert not _leaks(SECRET, text), d.inspect
        assert "memo='[redacted" in d.inspect, d.inspect
        for k in extra:          # no argument hides past a slice
            assert "{}={}".format(k, extra[k]) in d.inspect
        assert d.inspect.isascii()


def test_the_card_line_is_ascii_after_redaction():
    """DEC-32c: the args line is ASCII -- escaped after redaction and capping."""
    from systemu.interface.command.gate import card_args_line
    from systemu.runtime.tool_sandbox import _card_args_preview
    line = card_args_line(_card_args_preview({"city": "Zürich", "n": 2}))
    assert line.isascii() and "n=2" in line and "Z\\xfcrich" in line


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fingerprint migration: as-written one-shot records ask once more
# ─────────────────────────────────────────────────────────────────────────────

class _OneDecision:
    """An ``OperatorDecisionQueue`` stand-in that hands the gate one resolved
    decision (the chat lane's re-call path)."""

    def __init__(self, decision):
        self.decision = decision

    def __call__(self, vault):
        return self

    def consume_resolved_decision(self, dedup):
        return self.decision


def test_a_reclassification_recorded_as_written_asks_once_more(p, caplog):
    """IMPL-2: an operator reclassification an older build recorded against the
    fingerprint of the call AS WRITTEN does not apply to the sealed call: the call is
    scored without it (DENY again -- nothing runs), and the log and the fresh card
    say why. Recorded against the SEALED fingerprint, it applies (the contrast)."""
    from systemu.runtime.tool_sandbox import args_fingerprint
    p.tool.effect_tags = []                      # UNKNOWN effect (and a name the
    p.tool.name = "p28_mystery_op"               # verb map cannot classify) ...
    written = _nine_keys(content="DELETE FROM accounts")   # ... + destructive: DENY
    c = p.card(written)
    sig = c.extras["tool_signature"]
    assert c.extras["verdict"] == "deny"
    assert p.store.mark_reclassified(sig, "local_write",
                                     args_fingerprint=args_fingerprint(written))
    with caplog.at_level(logging.WARNING, logger="systemu.runtime.tool_sandbox"):
        stale = p.card(written)
    assert p.seen() == [], "a record minted for the as-written call ran the sealed one"
    assert stale.extras["verdict"] == "deny" and not stale.extras.get("reclassified")
    assert "asks once more" in stale.d.inspect, stale.d.inspect
    assert "as the model wrote them" in stale.d.inspect
    assert any("asks once more" in r.getMessage() for r in caplog.records), caplog.text
    # contrast: recorded against what the tool receives, it applies
    assert p.store.mark_reclassified(sig, "local_write",
                                     args_fingerprint=c.extras["args_fingerprint"])
    follow = p.card(written)
    assert follow.extras.get("reclassified") is True
    assert "asks once more" not in follow.d.inspect


def test_a_typed_approval_stamped_as_written_asks_once_more(p, caplog, monkeypatch):
    """R1 on the chat lane: a typed "Approve once" whose card an older build stamped
    with the AS-WRITTEN fingerprint does not witness the sealed call: nothing runs, a
    fresh card is posted, and the log and that card say why (never widened). The
    same approval stamped with the sealed fingerprint runs it (the contrast)."""
    import systemu.approval.decision_queue as dq
    from systemu.runtime.command_approvals import CONFIRM_TYPED_KEY
    from systemu.runtime.tool_sandbox import args_fingerprint
    p.tool.effect_tags = ["money_move"]
    written = _nine_keys(amount=500)
    c = p.card(written)
    assert c.d.confirm_phrase == "500", c.d.confirm_phrase
    old = dict(c.extras)
    old["args_fingerprint"] = args_fingerprint(written)      # the pre-P28 stamp
    old[CONFIRM_TYPED_KEY] = "500"
    monkeypatch.setattr(dq, "OperatorDecisionQueue", _OneDecision(
        SimpleNamespace(id="dec_old", choice="Approve once", context=old)))
    with caplog.at_level(logging.WARNING, logger="systemu.runtime.tool_sandbox"):
        stale = p.card(written, resolved=c.d.dedup)
    assert p.seen() == []
    assert "asks once more" in stale.d.inspect, stale.d.inspect
    assert any("asks once more" in r.getMessage() for r in caplog.records), caplog.text
    ok = dict(c.extras)
    ok[CONFIRM_TYPED_KEY] = "500"
    monkeypatch.setattr(dq, "OperatorDecisionQueue", _OneDecision(
        SimpleNamespace(id="dec_new", choice="Approve once", context=ok)))
    res = p.call(written, resolved=c.d.dedup)
    assert res.success and res.gate["passed_via"] == "approve_once", res
    assert len(p.seen()) == 1


def test_no_reconciliation_means_no_migration_at_all(p):
    """When the model already used the tool's own names, the sealed call IS the call
    as written: the fingerprint is unchanged from before P28 and no note appears."""
    from systemu.runtime.tool_sandbox import args_fingerprint
    params = {"url": "https://example.test/x", "content": "hi"}
    c = p.card(params)
    assert c.extras["args_fingerprint"] == args_fingerprint(params)
    assert "asks once more" not in c.d.inspect


# ─────────────────────────────────────────────────────────────────────────────
# 4. Sealed once; execution consumes it; validation still reads the call as written
# ─────────────────────────────────────────────────────────────────────────────

def test_every_call_is_sealed_and_the_registry_runs_it_verbatim(p, monkeypatch):
    """The registry receives the sealed value (exact type) -- never re-reconciled."""
    from systemu.runtime.tool_registry import SealedCallKwargs, ToolRegistry
    got = []
    orig = ToolRegistry.execute

    async def _spy(self, name, params, timeout=None):
        got.append((type(params), dict(params)))
        return await orig(self, name, params, timeout=timeout)

    monkeypatch.setattr(ToolRegistry, "execute", _spy)
    c = p.card(_nine_keys())
    p.store.approve(c.extras["tool_signature"], command=TOOL)
    assert p.call(_nine_keys()).success
    assert got and got[0][0] is SealedCallKwargs
    assert got[0][1] == p.seen()[0]


def test_a_schema_required_name_the_model_sent_still_validates(p, monkeypatch):
    """Gate 2.5 reads the sealed value AND the call as written: a forged spec that
    requires ``link`` while its body takes ``url`` still runs (as before P28),
    and a wrong type under either name is still refused."""
    schema = {"link": {"type": "string", "required": True},
              "a1": {"type": "string"}}
    monkeypatch.setattr(_Vault, "find_tool_by_name", lambda self, name: SimpleNamespace(
        name=name, id="tool_" + name, version=1, enabled=True,
        parameters_schema=schema, dry_run_status="passed", requires_credentials=[],
        credential_refusals=[], dependencies=[], timeout_seconds=None))
    c = p.card(_nine_keys())
    p.store.approve(c.extras["tool_signature"], command=TOOL)
    assert p.call(_nine_keys()).success
    bad = p.call(_nine_keys(a1=5))
    assert bad.success is False
    assert (bad.parsed or {}).get("error_type") == "tool_param_invalid"
