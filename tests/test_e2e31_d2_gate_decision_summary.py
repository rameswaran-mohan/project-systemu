"""D-2 (human-style e2e, v0.10.31) -- the decision summary says what the decision DID.

WITNESSED
    A tool-run gate was resolved with **Deny**. The activity went to FAILED and
    the resume rail wrote the correct event::

        Denied: Run tool: pdf_encrypt - the task was stopped and will not run.

    The operator's toast, however, was GREEN and said::

        Operator decision recorded (Deny); run unblocked.

    ``inbox.resolve_gate`` returned that one sentence for EVERY resolution of a
    render-only gate (operator / command / tool), and ``inbox_page`` showed
    whatever came back with ``type="positive"``. So the two surfaces the same
    operator reads disagreed about the same event, and the louder, greener one
    was the false one: nothing was unblocked, the run was stopped.

THE RULING PINNED HERE
    The summary names BOTH the RECORDED CHOICE and the OUTCOME, in one shape --
    ``Recorded (<Choice>): <title> - <outcome>.`` -- and the toast type matches
    it:

      * Deny          -> "Recorded (Deny): <title> - the task was stopped and will not run."  (negative)
      * Approve once  -> "Recorded (Approve once): <title> - resuming now."                   (positive)
      * Always allow  -> "Recorded (Always allow): <title> - resuming now[ and remembering
                          <what this gate type actually remembers>]."                         (positive)
      * anything else -> "Recorded (<choice>): <title>."                                      (info)

    THE PARENTHESISED ECHO IS THE ONE SHAPE BOTH SHIPPED FENCES ACCEPT.
    ``test_f34_gate_resolution_message_is_true::
    test_the_choice_is_echoed_so_the_operator_can_see_what_was_recorded``
    requires ``choice.lower() in summary.lower()`` -- an operator who has just
    clicked one of three buttons must be able to read back WHICH ONE was
    recorded, and a paraphrase of a click is indistinguishable from a paraphrase
    of a different click. ``test_v0932_command_gate::
    test_resolve_gate_does_not_persist_command_always_allow`` requires
    ``"always allow" not in summary.lower().replace("(always allow)", "")`` --
    the rail must not sound like it applied a standing allow, and the ONLY echo
    it tolerates is the parenthesised one. Both files' predicates are restated
    below so the three cannot drift apart silently.

    THE REMEMBERING CLAUSE IS GATE-TYPE-AWARE, AND IT IS WITNESSED.
    The tail used to read "remembering this exact tool body and effect set" for
    EVERY render-only gate. That is the TOOL card's copy
    (``gate.py`` ~342); the COMMAND card says "'Always allow' remembers this
    exact command", and an operator card has neither semantics. Promising a
    command gate that the system just memorised a tool body and effect set is a
    false assertion at the operator boundary (DEC-34) -- so the clause is now
    minted per gate type, and each variant is pinned by a test that WITNESSES
    the store: it drives the REAL recorder and reads the persistence back.
    A clause without a witnessed persistence is a defect, so the default is
    silence: no clause unless a standing approval is provably written.

    WHAT ACTUALLY PERSISTS (traced, then witnessed below):

      * ``tool``     -- ``resume_on_decision._record_gate_approval`` calls
                        ``CommandApprovalStore.approve(ctx["tool_signature"])``,
                        a STANDING allow-list entry keyed on
                        ``tool_signature(name, body_hash, effect_tags,
                        host_class)`` -- literally "this exact tool body and
                        effect set". It does so ONLY when the card carries a
                        stamped ``verdict`` that is not ``deny`` and is not a
                        ``reclassified`` follow-up; otherwise it falls back to a
                        SINGLE-USE resume bridge, or records nothing at all.
      * ``command``  -- the same recorder's else-branch ALWAYS mints only a
                        single-use ``mark_resume_approved`` bridge, consumed
                        once by ``tool_sandbox``. The standing command allow is
                        minted exclusively by ``command_gate_handler``, reached
                        only through ``decision_dispatcher.dispatch`` -- which
                        exactly one surface calls (``pages/insights.py``), and
                        that surface never calls ``resolve_gate``. So NO caller
                        of this sentence has persisted a standing command
                        approval by the time the operator reads it. That is also
                        the security property ``test_v0932_command_gate`` pins.
      * ``operator`` -- ``_dispatch_resume`` reaches the recorder only for
                        ``gate_type`` in {command, tool}, and no dispatcher
                        handler is registered for the ``operator`` namespace.
                        Nothing persists, so nothing is promised.

    The summary is derived from the SAME decision object the resume rail reads
    -- ``decision.choice``, normalized once, exactly the way
    ``resume_on_decision._dispatch_resume`` normalizes it -- so the toast cannot
    say one thing while the rail does another. The card TITLE comes from the
    rail's own ``_gate_card_label``, for the same reason.

    The toast type is carried BY the result (``data["notice_kind"]``) rather than
    re-derived at the page, so a page cannot re-colour a verdict it did not mint.

NOTHING IS EXECUTED HERE. A render-only gate resolution runs no action, so
``resolve_gate`` is called directly with ``vault=None``; no LLM, no daemon, no
writes into the worktree vault. The witness tests DO write -- to a
``tmp_path`` the recorder's own relative ``Path("data")`` is chdir'd onto.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.approval.decision_queue import OperatorDecision
from systemu.interface.command.inbox import resolve_gate
from systemu.interface.command.result import CommandStatus

TITLE = "Run tool: pdf_encrypt"
TOOL_SIG = "a" * 40
TOOL_SCOPE = "this exact tool body and effect set"

# A tool card as ``tool_sandbox`` actually stamps it: the params-independent
# signature plus the verdict it was scored under. This is the ONLY shape for
# which the recorder writes a standing allow.
STANDING_TOOL_CTX = {"tool_signature": TOOL_SIG, "verdict": "require_approval"}


def _gate_decision(choice, *, gate_type="tool", title=TITLE, context=None):
    ctx = {"kind": "gate", "gate_type": gate_type}
    ctx.update(context or {})
    return OperatorDecision(
        id="dec_e2e31",
        title=title,
        body="",
        options=["Deny", "Approve once", "Always allow"],
        context=ctx,
        dedup_key="tool:sigX",
        status="resolved",
        choice=choice,
    )


def _resolve(choice, **kw):
    return resolve_gate(_gate_decision(choice, **kw), vault=None)


def _notice(result):
    from systemu.interface.pages.inbox_page import gate_outcome_notice
    return gate_outcome_notice(result)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The REAL approval store the REAL recorder writes to.

    ``_record_gate_approval`` resolves its store at ``Path("data")`` -- a
    RELATIVE path -- so the witness tests chdir onto a tmp dir rather than
    letting the recorder write ``data/command_approvals.json`` into the
    worktree. The singleton is reset on both sides so no other test inherits a
    store pointed at a deleted directory.
    """
    import systemu.runtime.command_approvals as ca
    monkeypatch.chdir(tmp_path)
    ca.reset_default_store_for_tests()
    yield ca.init_default_store(Path("data"))
    ca.reset_default_store_for_tests()


def _record(ctx, *, is_tool_gate, choice="always allow"):
    """Drive the production recorder -- not a re-implementation of it."""
    from systemu.runtime.resume_on_decision import _record_gate_approval
    _record_gate_approval(ctx, is_tool_gate=is_tool_gate, choice=choice)


# ── the witnessed defect ─────────────────────────────────────────────────────

def test_deny_never_reports_the_run_as_unblocked():
    res = _resolve("Deny")
    assert "unblock" not in res.summary.lower(), (
        "a denial still claims the run was unblocked -- the run was STOPPED"
    )
    assert res.summary == \
        f"Recorded (Deny): {TITLE} - the task was stopped and will not run."


def test_deny_is_not_shown_in_a_green_toast():
    msg, kind = _notice(_resolve("Deny"))
    assert kind == "negative", f"Deny rendered as {kind!r}"
    assert msg == f"Recorded (Deny): {TITLE} - the task was stopped and will not run."


def test_the_deny_summary_matches_what_the_resume_rail_does():
    """The rail finalizes the activity FAILED and emits the same sentence. The
    toast is the operator's copy of that fact, not a second opinion."""
    res = _resolve("Deny")
    assert "stopped and will not run" in res.summary
    assert "resuming" not in res.summary.lower()


# ── the affirmative choices ──────────────────────────────────────────────────

def test_approve_once_says_it_is_resuming():
    res = _resolve("Approve once")
    assert res.summary == f"Recorded (Approve once): {TITLE} - resuming now."
    assert _notice(res)[1] == "positive"


def test_always_allow_on_a_standing_tool_card_says_it_was_remembered():
    res = _resolve("Always allow", context=STANDING_TOOL_CTX)
    assert res.summary == (
        f"Recorded (Always allow): {TITLE} - resuming now and remembering "
        f"{TOOL_SCOPE}.")
    assert "remember" in res.summary.lower()
    assert _notice(res)[1] == "positive"


def test_approve_once_is_not_described_as_remembered():
    """The two affirmative choices differ in exactly the thing the operator
    cares about -- whether the answer carries forward. One sentence for both
    would restore half of the defect."""
    assert "remember" not in _resolve(
        "Approve once", context=STANDING_TOOL_CTX).summary.lower()


# ── every other render-only choice ───────────────────────────────────────────

def test_an_operator_card_choice_is_recorded_not_approved():
    res = _resolve("Later", gate_type="operator", title="Pick a folder")
    assert res.summary == "Recorded (Later): Pick a folder."
    assert _notice(res)[1] == "info"


def test_a_free_choice_is_never_called_an_approval():
    res = _resolve("Option B", gate_type="operator", title="Pick a folder")
    assert "approve" not in res.summary.lower()
    assert "denied" not in res.summary.lower()


# ── the recorded LABEL, echoed the one way BOTH fences accept ────────────────

@pytest.mark.parametrize("choice", ["Deny", "Approve once", "Always allow", "Later"])
def test_the_recorded_choice_label_appears_verbatim(choice):
    """The shipped fence ``test_f34_gate_resolution_message_is_true::
    test_the_choice_is_echoed_so_the_operator_can_see_what_was_recorded`` asserts
    ``choice.lower() in summary.lower()``: the operator must be able to read back
    WHICH BUTTON was recorded, not a paraphrase of it.

    That fence lives in another file, and the first version of this one broke it
    -- "Denied:" for a button labelled "Deny", "Approved and remembered:" for
    one labelled "Always allow". Restating it here makes the two files agree BY
    CONSTRUCTION: any future rewording that drops the label reddens this file
    too, in the same run, instead of surfacing as a mystery failure in a file
    the author never opened."""
    summary = _resolve(choice).summary
    assert summary.startswith(f"Recorded ({choice}):"), (
        f"the recorded label {choice!r} must lead the sentence, parenthesised; "
        f"got {summary!r}"
    )
    assert choice.lower() in summary.lower()  # F34's own predicate, verbatim


@pytest.mark.parametrize("gate_type", ["operator", "command", "tool"])
def test_the_only_always_allow_echo_is_the_parenthesised_one(gate_type):
    """``test_v0932_command_gate::test_resolve_gate_does_not_persist_command_
    always_allow``'s own predicate, verbatim and widened to every render-only
    gate type. Strip the parenthesised echo and the phrase must be GONE: the
    rail persists no standing command approval, so any prose repetition of the
    button's name reads as a claim that it applied one."""
    summary = (_resolve("Always allow", gate_type=gate_type).summary or "").lower()
    assert "always allow" not in summary.replace("(always allow)", ""), (
        f"the rail implied it applied a standing allow: {summary!r}"
    )


# ── ONE derivation of the choice, from the object the rail reads ─────────────

def test_the_summary_follows_the_recorded_choice_not_a_context_copy():
    """A stale ``choice`` in the context must not be able to re-colour the
    verdict: the rail reads ``decision.choice`` and so does this."""
    res = _resolve("Deny", context={"choice": "Approve once"})
    assert res.summary.startswith("Recorded (Deny):")
    assert _notice(res)[1] == "negative"


def test_choice_normalization_matches_the_resume_rail():
    """``_dispatch_resume`` normalizes with ``(choice or "").strip().lower()``
    and treats "" as a denial. Both facts are pinned here, because a summary
    that disagreed with either would be describing a different decision.

    The label the sentence echoes is the canonical one for the branch, not a
    raw echo of the stored string: a padded "  DENY  " and an absent choice are
    the same recorded decision, and neither may be spelled back at the operator
    as itself."""
    assert _resolve("  DENY  ").summary.startswith("Recorded (Deny):")
    assert _resolve("").summary.startswith("Recorded (Deny):")
    assert _resolve(None).summary.startswith("Recorded (Deny):")
    assert "None" not in _resolve(None).summary


def test_the_title_is_the_one_the_resume_rail_names():
    """A decision with no persisted title falls back to the rail's own rebuilt
    label -- not to a blank, and not to a second invented format."""
    from systemu.runtime.resume_on_decision import _gate_card_label
    dec = _gate_decision("Deny", title="", context={"tool_name": "pdf_encrypt"})
    label = _gate_card_label(dec, dec.context, "tool")
    assert label == "Run tool: pdf_encrypt"
    assert resolve_gate(dec, vault=None).summary == \
        f"Recorded (Deny): {label} - the task was stopped and will not run."


# ── the other render-only gate types read identically ────────────────────────

@pytest.mark.parametrize("gate_type", ["operator", "command", "tool"])
def test_every_render_only_gate_type_names_its_outcome(gate_type):
    res = _resolve("Deny", gate_type=gate_type)
    assert res.summary.startswith("Recorded (Deny):")
    assert res.status == CommandStatus.OK, (
        "recording the decision still SUCCEEDED -- the outcome rides the "
        "notice kind, not an error status"
    )


# ── what "Always allow" REMEMBERS, per gate type, witnessed at the store ─────
#
# Each test drives the PRODUCTION recorder and reads the persistence back, then
# asserts the sentence matches what it just witnessed. The mapping in
# ``inbox.standing_approval_scope`` is therefore bound to observed behaviour,
# not to a second reading of the rail's source.

def test_a_tool_gate_really_persists_a_standing_allow_over_body_and_effects(store):
    """WITNESS for the tool clause. ``_record_gate_approval`` writes a STANDING
    allow-list entry keyed on the card's ``tool_signature`` -- which
    ``command_approvals.tool_signature`` builds from (name, body_hash,
    effect_tags, host_class). "This exact tool body and effect set" is that key
    said in English, and it is the phrase the tool CARD itself uses."""
    assert store.is_approved(TOOL_SIG) is False
    _record(dict(STANDING_TOOL_CTX), is_tool_gate=True)
    assert store.is_approved(TOOL_SIG) is True, (
        "the recorder no longer writes a standing tool allow -- the sentence "
        "promising one is now false"
    )
    res = _resolve("Always allow", context=STANDING_TOOL_CTX)
    assert res.summary.endswith(f"resuming now and remembering {TOOL_SCOPE}.")


def test_a_command_gate_persists_no_standing_allow_so_promises_none(store):
    """WITNESS for the command clause: there ISN'T one.

    The recorder's command branch mints a SINGLE-USE resume bridge and nothing
    else -- pinned in both directions below. The standing command allow exists
    (``command_gate_handler``) but is reachable only via
    ``decision_dispatcher.dispatch``, which no caller of ``resolve_gate``
    invokes. So by the time the operator reads this sentence, nothing has been
    remembered, and the sentence says nothing about remembering."""
    from systemu.runtime.command_approvals import command_signature
    sig = command_signature("rm -rf build", cwd="/proj")
    _record({"command": "rm -rf build", "cwd": "/proj"}, is_tool_gate=False)
    assert store.is_approved(sig) is False, (
        "the rail minted a STANDING command allow -- only the dispatcher may"
    )
    assert store.consume_resume_approved(sig) is True, "a one-shot bridge, once"
    assert store.consume_resume_approved(sig) is False, "and only once"

    res = _resolve("Always allow", gate_type="command", title="Run command")
    assert res.summary == "Recorded (Always allow): Run command - resuming now."
    assert "remember" not in res.summary.lower()
    assert TOOL_SCOPE not in res.summary, (
        "the TOOL card's copy is being read back on a COMMAND gate"
    )


def test_a_deny_band_tool_gate_persists_nothing_so_promises_nothing(store):
    """WITNESS: on the DENY band the recorder writes NOTHING -- not a standing
    allow and not even a bridge (IMPL-2). A remembering clause there would be
    the exact over-claim this file exists to stop, on the most dangerous band."""
    _record({"tool_signature": TOOL_SIG, "verdict": "deny"}, is_tool_gate=True)
    assert store.is_approved(TOOL_SIG) is False
    assert store.consume_resume_approved(TOOL_SIG) is False

    res = _resolve("Always allow",
                   context={"tool_signature": TOOL_SIG, "verdict": "deny"})
    assert res.summary == f"Recorded (Always allow): {TITLE} - resuming now."


def test_a_tool_card_with_no_stamped_verdict_promises_nothing(store):
    """WITNESS: an absent verdict FAILS CLOSED at the recorder -- a single-use
    bridge, never a standing entry. The sentence fails closed with it."""
    _record({"tool_signature": TOOL_SIG}, is_tool_gate=True)
    assert store.is_approved(TOOL_SIG) is False
    assert store.consume_resume_approved(TOOL_SIG) is True

    res = _resolve("Always allow", context={"tool_signature": TOOL_SIG})
    assert res.summary == f"Recorded (Always allow): {TITLE} - resuming now."


def test_a_reclassified_tool_card_promises_nothing(store):
    """WITNESS: a follow-up card posted under an operator reclassification may
    never mint a standing allow (IMPL-2) -- only a scoped one-shot."""
    ctx = {"tool_signature": TOOL_SIG, "verdict": "require_approval",
           "reclassified": True, "assigned_class": "local_write"}
    _record(dict(ctx), is_tool_gate=True)
    assert store.is_approved(TOOL_SIG) is False
    assert store.consume_resume_approved(
        TOOL_SIG, for_reclassification="local_write") is True

    res = _resolve("Always allow", context=ctx)
    assert res.summary == f"Recorded (Always allow): {TITLE} - resuming now."


def test_a_tool_card_with_no_signature_promises_nothing(store):
    """WITNESS: with no ``tool_signature`` the recorder has no key and returns
    without writing. There is no entry to point at, so there is no claim."""
    _record({"verdict": "require_approval"}, is_tool_gate=True)
    assert list(store.list_approved()) == []

    res = _resolve("Always allow", context={"verdict": "require_approval"})
    assert res.summary == f"Recorded (Always allow): {TITLE} - resuming now."


def test_an_operator_gate_never_reaches_the_recorder_so_promises_nothing(store):
    """WITNESS by unreachability: ``_dispatch_resume`` returns before any
    recorder for an operator card (``is_gate`` covers only command/tool, and
    the ``operator`` dedup namespace has no dispatcher handler). Nothing can
    persist, so nothing is promised."""
    from systemu.runtime.resume_on_decision import _dispatch_resume
    dec = _gate_decision("Always allow", gate_type="operator",
                         title="Pick a folder",
                         context={"execution_id": "exec_1",
                                  "activity_id": "act_1", "shadow_id": "sh_1"})
    assert _dispatch_resume(dec, vault=None, supervisor=None) is False
    assert list(store.list_approved()) == []

    res = _resolve("Always allow", gate_type="operator", title="Pick a folder")
    assert res.summary == "Recorded (Always allow): Pick a folder - resuming now."


def test_the_clause_is_minted_from_one_scope_helper():
    """The phrase has ONE source. ``gate_decision_notice`` asks
    ``standing_approval_scope`` what this card's Always-allow persists and says
    only that; a branch that spelled its own tail would be a second derivation
    of the persistence fact, which is how the tool copy reached command cards."""
    from systemu.interface.command.inbox import (gate_decision_notice,
                                                 standing_approval_scope)
    assert standing_approval_scope("tool", STANDING_TOOL_CTX) == TOOL_SCOPE
    assert standing_approval_scope("command", {"verdict": "require_approval"}) == ""
    assert standing_approval_scope("operator", STANDING_TOOL_CTX) == ""
    assert "standing_approval_scope(" in inspect.getsource(gate_decision_notice)


# ── contract details ─────────────────────────────────────────────────────────

def test_the_notice_kind_rides_the_result():
    for choice, kind in (("Deny", "negative"), ("Approve once", "positive"),
                         ("Always allow", "positive"), ("Later", "info")):
        assert _resolve(choice).data.get("notice_kind") == kind, choice


def test_every_summary_is_ascii():
    for choice in ("Deny", "Approve once", "Always allow", "Later"):
        _resolve(choice).summary.encode("ascii")
        _resolve(choice, context=STANDING_TOOL_CTX).summary.encode("ascii")
    # A non-ASCII title cannot smuggle non-ASCII into a verdict line either.
    _resolve("Deny", title="Run tool: café").summary.encode("ascii")


def test_a_page_cannot_recolour_a_verdict_it_did_not_mint():
    """An unknown/absent notice kind falls back to the result STATUS, never to a
    blanket green -- a blanket green is the defect."""
    from systemu.interface.pages.inbox_page import gate_outcome_notice
    from systemu.interface.command.result import CommandResult

    ok = CommandResult(status=CommandStatus.OK, summary="did a thing")
    assert gate_outcome_notice(ok) == ("did a thing", "positive")
    noop = CommandResult(status=CommandStatus.NOOP, summary="did nothing")
    assert gate_outcome_notice(noop)[1] == "warning"
    err = CommandResult(status=CommandStatus.ERROR, summary="blew up")
    assert gate_outcome_notice(err)[1] == "negative"
    junk = CommandResult(status=CommandStatus.OK, summary="x",
                         data={"notice_kind": ["positive"]})
    assert gate_outcome_notice(junk)[1] == "positive"  # not a crash, not a claim


# ── reachability ─────────────────────────────────────────────────────────────

def test_the_card_shows_the_minted_notice_kind():
    from systemu.interface.pages import inbox_page
    src = inspect.getsource(inbox_page._render_unified_card)
    assert "gate_outcome_notice(" in src, (
        "the gate card no longer asks for the minted toast type -- it is back "
        "to colouring every resolution green"
    )
    assert 'typ = "positive"' not in src, (
        "the blanket positive toast is back in the resolve handler"
    )
