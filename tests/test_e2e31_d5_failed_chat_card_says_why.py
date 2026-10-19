"""D-5 (human-style e2e, v0.10.31) -- a FAILED chat task says why, and what next.

WITNESSED
    A chat task failed. Its card showed the prompt, the timestamp, a red FAILED
    badge and a restart icon -- and nothing else. The actual reason (the model
    tiers pointed at providers with no key) existed, but only inside the
    COLLAPSED Live-events pane, which the operator had to know to open and then
    scroll. The card offered a restart button and no way to know that pressing
    it would fail again for the same reason.

THE RULING PINNED HERE
    The card for a FAILED chat task renders, as ASCII lines on the card itself:

      * the failure REASON, taken from the task's terminal record (the row's own
        ``error``, which every failing lane writes) or, failing that, the last
        ERROR event that names this task -- read through the runtime module that
        owns chat-task state, not re-derived in the page;
      * a NEXT STEP whenever one is known: a provider/tier reason -> set a
        provider key in Settings; a tool reason -> enable the tool in the Tools
        Registry; anything else -> the pointer to Live events.

    "Anything else" is a real branch, not a fallback that fires on nothing: an
    unrecognized reason must produce a pointer, never a blank card and never a
    guessed remedy. A card whose status is not a failure is untouched.

NO LLM, NO DAEMON. The card builder is a pure function exercised with a FAKE
registry (the reason source is the seam); the registry's own reason lookup is
exercised against plain dicts and a list of event dicts.
"""
from __future__ import annotations

import inspect

import pytest

PROVIDER_REASON = ("LLM call failed (tier=1, model=x/y): the configured tiers "
                   "point at providers with no key")
TOOL_REASON = "Tool 'pdf_encrypt' is not enabled and the task required it"


class _FakeRegistry:
    """Stands in for ``chat_task_registry`` as the REASON SOURCE only."""

    def __init__(self, reason):
        self._reason = reason
        self.asked = []

    def failure_reason(self, entry, **kw):
        self.asked.append(entry)
        return self._reason


def _entry(status="failed", **kw):
    row = {"ts": "2026-09-08T10:00:00", "prompt": "encrypt the quarterly pdf",
           "status": status, "activity_id": "act_1", "execution_id": "exec_1"}
    row.update(kw)
    return row


def _lines(entry, reason):
    from systemu.interface.pages.chat_page import failed_task_lines
    return failed_task_lines(entry, registry=_FakeRegistry(reason))


def _texts(entry, reason):
    return [text for text, _cls in _lines(entry, reason)]


# ── the witnessed defect: the card said nothing ──────────────────────────────

def test_a_failed_card_is_never_silent_about_a_recorded_reason():
    texts = _texts(_entry(), PROVIDER_REASON)
    assert texts, "a FAILED card still renders only the badge"
    assert any("providers with no key" in t for t in texts), (
        "the reason is still only in the collapsed Live-events pane"
    )


def test_a_provider_reason_points_at_settings():
    texts = _texts(_entry(), PROVIDER_REASON)
    assert texts[-1] == "Next step: Set a provider key in Settings."


def test_a_tool_reason_points_at_the_tools_registry():
    texts = _texts(_entry(), TOOL_REASON)
    assert any("pdf_encrypt" in t for t in texts)
    assert texts[-1] == "Next step: Enable the tool in the Tools Registry."


def test_an_unrecorded_reason_still_points_somewhere():
    """The one outcome that must NOT be a blank card."""
    for reason in ("", None, "   "):
        texts = _texts(_entry(), reason)
        assert texts == ["Next step: See Live events for details."], reason


def test_an_unrecognized_reason_never_guesses_a_remedy():
    texts = _texts(_entry(), "the shadow army disbanded")
    assert any("shadow army disbanded" in t for t in texts)
    assert texts[-1] == "Next step: See Live events for details."
    assert "Settings" not in " ".join(texts)


# ── the control: nothing else changes ────────────────────────────────────────

@pytest.mark.parametrize("status", ["success", "partial", "running", "queued",
                                    "cancelled", "waiting_on_tools",
                                    "pending_decision", "spend_cap_reached"])
def test_a_card_that_did_not_fail_is_untouched(status):
    assert _lines(_entry(status=status), PROVIDER_REASON) == []


def test_the_reason_source_is_not_even_consulted_for_a_completed_card():
    from systemu.interface.pages.chat_page import failed_task_lines
    reg = _FakeRegistry(PROVIDER_REASON)
    assert failed_task_lines(_entry(status="success"), registry=reg) == []
    assert reg.asked == [], "a COMPLETED card asked for a failure reason"


def test_both_spellings_of_failure_are_covered():
    """direct_task writes "failed"; a ShadowRuntime result status is "failure"."""
    for status in ("failed", "failure"):
        assert _texts(_entry(status=status), PROVIDER_REASON), status


def test_a_non_dict_row_is_not_a_failed_card():
    from systemu.interface.pages.chat_page import failed_task_lines
    for junk in (None, "failed", ["failed"], 7):
        assert failed_task_lines(junk, registry=_FakeRegistry(PROVIDER_REASON)) == []


# ── how the lines render ─────────────────────────────────────────────────────

def test_every_line_is_ascii_and_carries_a_design_token():
    for text, cls in _lines(_entry(), "reason with a café and an em—dash"):
        text.encode("ascii")
        assert cls.startswith("s-"), f"{cls!r} is not a design-token class"


def test_a_runaway_reason_cannot_take_over_the_card():
    long_reason = "x" * 4000
    texts = _texts(_entry(), long_reason)
    assert len(texts[0]) < 400, "an unbounded traceback is pasted onto the card"
    assert texts[0].endswith("...")
    assert texts[-1] == "Next step: See Live events for details."


def test_the_step_is_read_off_the_WHOLE_reason_not_the_clipped_one():
    """Truncation is a display concern. Classifying the clipped copy would
    silently downgrade a known remedy to the pointer whenever the marker sat
    past the cap -- the DEC-31 ordering (decide first, slice second)."""
    buried = "x" * 3000 + " OPENROUTER_API_KEY is not set"
    texts = _texts(_entry(), buried)
    assert texts[-1] == "Next step: Set a provider key in Settings."
    assert texts[0].endswith("...")


# ── the registry: where the reason comes from ────────────────────────────────

def test_the_terminal_record_is_the_first_source():
    from systemu.runtime.chat_task_registry import failure_reason
    assert failure_reason(_entry(error=PROVIDER_REASON)) == PROVIDER_REASON


def test_the_last_error_event_for_THIS_task_is_the_second_source():
    from systemu.runtime.chat_task_registry import failure_reason
    events = [
        {"level": "ERROR", "message": "someone else's failure",
         "context": {"activity_id": "act_OTHER"}},
        {"level": "INFO", "message": "started", "context": {"activity_id": "act_1"}},
        {"level": "ERROR", "message": "first error", "context": {"activity_id": "act_1"}},
        {"level": "ERROR", "message": PROVIDER_REASON,
         "context": {"execution_id": "exec_1"}},
    ]
    assert failure_reason(_entry(), events=events) == PROVIDER_REASON


def test_an_event_that_does_not_name_this_task_is_never_borrowed():
    from systemu.runtime.chat_task_registry import failure_reason
    events = [{"level": "ERROR", "message": "someone else's failure",
               "context": {"activity_id": "act_OTHER", "execution_id": "exec_OTHER"}}]
    assert failure_reason(_entry(), events=events) == ""


def test_a_non_error_event_is_not_a_failure_reason():
    from systemu.runtime.chat_task_registry import failure_reason
    events = [{"level": "WARNING", "message": "slow",
               "context": {"activity_id": "act_1"}}]
    assert failure_reason(_entry(), events=events) == ""


def test_unreadable_input_reports_no_reason_rather_than_raising():
    from systemu.runtime.chat_task_registry import failure_reason
    assert failure_reason(None) == ""
    assert failure_reason(_entry(error=17)) == ""
    assert failure_reason(_entry(), events=["not an event", None, 3]) == ""


def test_the_next_step_mapping_is_the_ruled_one():
    from systemu.runtime.chat_task_registry import failure_next_step
    assert failure_next_step(PROVIDER_REASON) == "Set a provider key in Settings."
    assert failure_next_step("OPENROUTER_API_KEY is not set") == \
        "Set a provider key in Settings."
    assert failure_next_step(TOOL_REASON) == "Enable the tool in the Tools Registry."
    assert failure_next_step("disk full") == "See Live events for details."
    assert failure_next_step("") == "See Live events for details."
    assert failure_next_step(None) == "See Live events for details."


def test_a_provider_reason_that_also_says_tool_is_still_a_provider_reason():
    from systemu.runtime.chat_task_registry import failure_next_step
    assert failure_next_step(
        "LLM call failed (tier=2): provider rejected the tool schema"
    ) == "Set a provider key in Settings."


# ── reachability ─────────────────────────────────────────────────────────────

def test_the_chat_card_actually_renders_the_lines():
    from systemu.interface.pages import chat_page
    src = inspect.getsource(chat_page.build_chat_page)
    assert "failed_task_lines(" in src, (
        "the failure lines have no production call site - a FAILED card is "
        "back to a badge and a restart button"
    )


def test_the_reason_lookup_lives_in_the_module_that_owns_chat_task_state():
    from systemu.runtime import chat_task_registry as reg
    assert callable(getattr(reg, "failure_reason", None))
    assert callable(getattr(reg, "failure_next_step", None))
