"""D5 (dogfood 0.10.28) - Chat "Run Task" must never silently lose a submission.

Operator witness, twice, on the shipped 0.10.28 wheel: click Run Task ->
renderer freeze -> composer cleared -> no task created, no error shown.

Ruling under test:
  * the composer clears ONLY after the submit is acknowledged (a task/activity
    id returned AND the submission event witnessed);
  * on ANY failure (exception, timeout, no id) the text stays in the composer
    and a toast names the failure: "Could not submit: <reason>. Your text is
    still here.";
  * a submission refused because a task is already running is a failure of this
    kind (D6) - it must not clear the composer either.

Everything here runs against the real handler seam
(``chat_page.handle_chat_submit``) with injected callables: no NiceGUI client,
no vault, no LLM, no daemon.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


# ---------------------------------------------------------------- helpers ----

class _Recorder:
    """Records the ORDER of the handler's side effects.

    Order is the contract: `clear` may only appear after `await_ack`.
    """

    def __init__(self):
        self.calls = []
        self.notices = []

    def notify(self, message, **kwargs):
        self.calls.append("notify")
        self.notices.append((message, kwargs.get("type")))

    def clear(self):
        self.calls.append("clear")

    @property
    def last_message(self):
        return self.notices[-1][0] if self.notices else ""

    @property
    def last_type(self):
        return self.notices[-1][1] if self.notices else ""


def _handle(**kwargs):
    from systemu.interface.pages.chat_page import handle_chat_submit
    kwargs.setdefault("text", "take a screenshot of example.com")
    kwargs.setdefault("entries", [])
    return handle_chat_submit(**kwargs)


def _ok_ack(rec, task_id_seen=None):
    def _await_ack(task_id):
        rec.calls.append("await_ack")
        if task_id_seen is not None:
            task_id_seen.append(task_id)
        return True, ""
    return _await_ack


def _start(rec, task_id="2026-09-07T10:00:00.123456"):
    def _s():
        rec.calls.append("start")
        return task_id
    return _s


# ------------------------------------------------- D5: failures keep text ----

class TestFailurePreservesTheText:
    def test_start_raises_keeps_text_and_names_the_reason(self):
        rec = _Recorder()

        def _boom():
            rec.calls.append("start")
            raise RuntimeError("can't start new thread")

        out = _handle(start=_boom, await_ack=_ok_ack(rec),
                      notify=rec.notify, clear=rec.clear)

        assert out is None
        assert "clear" not in rec.calls, "a failed submit must NOT clear the composer"
        assert rec.last_message.startswith("Could not submit: ")
        assert "can't start new thread" in rec.last_message
        assert "Your text is still here." in rec.last_message
        assert rec.last_type == "negative"

    def test_start_returning_no_id_keeps_text(self):
        rec = _Recorder()

        def _no_id():
            rec.calls.append("start")
            return None

        out = _handle(start=_no_id, await_ack=_ok_ack(rec),
                      notify=rec.notify, clear=rec.clear)

        assert out is None
        assert "clear" not in rec.calls
        assert "await_ack" not in rec.calls, "no id means there is nothing to wait for"
        assert rec.last_message.startswith("Could not submit: ")
        assert "Your text is still here." in rec.last_message

    def test_start_returning_empty_string_keeps_text(self):
        rec = _Recorder()
        out = _handle(start=lambda: "", await_ack=_ok_ack(rec),
                      notify=rec.notify, clear=rec.clear)
        assert out is None
        assert "clear" not in rec.calls

    def test_submission_never_witnessed_keeps_text_and_names_it(self):
        """The witnessed defect: the pipeline died before creating the task."""
        rec = _Recorder()

        def _never(task_id):
            rec.calls.append("await_ack")
            return False, "Scroll refinement failed: provider unreachable"

        out = _handle(start=_start(rec), await_ack=_never,
                      notify=rec.notify, clear=rec.clear)

        assert out is None
        assert "clear" not in rec.calls
        assert "Scroll refinement failed: provider unreachable" in rec.last_message
        assert "Your text is still here." in rec.last_message
        assert rec.last_type == "negative"

    def test_ack_timeout_keeps_text(self):
        rec = _Recorder()

        def _timeout(task_id):
            rec.calls.append("await_ack")
            raise TimeoutError("timed out waiting for the task to be created")

        out = _handle(start=_start(rec), await_ack=_timeout,
                      notify=rec.notify, clear=rec.clear)

        assert out is None
        assert "clear" not in rec.calls
        assert "timed out" in rec.last_message
        assert "Your text is still here." in rec.last_message

    def test_ack_with_no_reason_still_names_a_failure(self):
        rec = _Recorder()
        out = _handle(start=_start(rec),
                      await_ack=lambda tid: (False, ""),
                      notify=rec.notify, clear=rec.clear)
        assert out is None
        assert "clear" not in rec.calls
        assert rec.last_message.startswith("Could not submit: ")
        # never an empty/dangling reason
        assert "Could not submit: . " not in rec.last_message

    def test_a_refused_submission_keeps_the_text_too(self):
        """The ruling names this a D5 failure: refused because something else is
        already running still means the operator keeps what they typed."""
        rec = _Recorder()
        out = _handle(
            entries=[{"ts": "2026-09-07T09:00:00", "status": "running",
                      "prompt": "the earlier task"}],
            start=_start(rec), await_ack=_ok_ack(rec),
            notify=rec.notify, clear=rec.clear)
        assert out is None
        assert rec.calls == ["notify"], (
            "a refusal must not start work and must not clear the composer"
        )
        assert rec.notices, "MUTATION PIN: drop the refusal toast and this goes red"

    def test_empty_text_is_refused_without_starting_anything(self):
        rec = _Recorder()
        out = _handle(text="   ", start=_start(rec), await_ack=_ok_ack(rec),
                      notify=rec.notify, clear=rec.clear)
        assert out is None
        assert "start" not in rec.calls
        assert "clear" not in rec.calls
        assert rec.notices, "an empty submit still gets told why"


# ------------------------------------------- D5: acknowledged submit clears ---

class TestAcknowledgedSubmitClears:
    def test_id_plus_witnessed_ack_clears_and_confirms(self):
        rec = _Recorder()
        out = _handle(start=_start(rec, "ts-1"), await_ack=_ok_ack(rec),
                      notify=rec.notify, clear=rec.clear)

        assert out == "ts-1"
        assert "clear" in rec.calls
        assert rec.last_type == "positive"

    def test_the_ack_receives_the_id_start_returned(self):
        rec = _Recorder()
        seen = []
        _handle(start=_start(rec, "ts-42"), await_ack=_ok_ack(rec, seen),
                notify=rec.notify, clear=rec.clear)
        assert seen == ["ts-42"], "the ack must be waited on for THIS submission"

    def test_clear_happens_only_after_the_ack(self):
        """MUTATION PIN (D5): move `clear()` above `await_ack` and this goes red.

        This is the whole defect in one assertion - 0.10.28 cleared the
        composer at the top of the handler, before anything was known.
        """
        rec = _Recorder()
        _handle(start=_start(rec), await_ack=_ok_ack(rec),
                notify=rec.notify, clear=rec.clear)
        assert rec.calls == ["start", "await_ack", "clear", "notify"]


# ------------------------------------------------- the ack predicate itself ---

class TestSubmissionWitnessed:
    def test_row_with_the_task_id_counts_as_witnessed(self):
        from systemu.interface.pages.chat_page import submission_witnessed
        entries = [{"ts": "a", "status": "success"}, {"ts": "b", "status": "running"}]
        assert submission_witnessed(entries, "b") is True

    def test_absent_row_is_not_witnessed(self):
        from systemu.interface.pages.chat_page import submission_witnessed
        assert submission_witnessed([{"ts": "a"}], "b") is False

    def test_no_entries_is_not_witnessed(self):
        from systemu.interface.pages.chat_page import submission_witnessed
        assert submission_witnessed([], "b") is False
        assert submission_witnessed(None, "b") is False

    def test_empty_task_id_is_never_witnessed(self):
        from systemu.interface.pages.chat_page import submission_witnessed
        assert submission_witnessed([{"ts": ""}], "") is False


# ------------------------------------------------------- reachability pins ---

class TestTheFixIsWired:
    """Not just present - CALLED from the page's real submit handler."""

    def test_on_submit_routes_through_handle_chat_submit(self):
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page.build_chat_page)
        assert "handle_chat_submit(" in src, (
            "handle_chat_submit has no production call site - the D5 fix is inert"
        )

    def test_on_submit_no_longer_clears_the_composer_up_front(self):
        """The 0.10.28 line was `prompt_input.set_value("")` inside _on_submit,
        before the run thread even started.

        The only clear left must live in the `clear=` callable the handler owns,
        so nothing between the top of `_on_submit` and that callable may wipe the
        composer.
        """
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page.build_chat_page)
        body = src[src.index("def _on_submit"):src.index("submit_btn.on_click")]
        prologue = body[:body.index("def _clear(")]
        assert 'prompt_input.set_value("")' not in prologue, (
            "the composer is still cleared eagerly inside _on_submit"
        )
        assert "clear=_clear" in body, (
            "the composer clear is not the handler's to make any more"
        )

    def test_the_failure_message_is_ascii(self):
        from systemu.interface.pages.chat_page import submit_failed_message
        msg = submit_failed_message("provider unreachable")
        msg.encode("ascii")  # raises if a non-ASCII char sneaks in
        assert "Your text is still here." in msg


# ------------------------------------------- R-UX2: the ack wait is offloaded -

class TestTheAckWaitNeverRunsOnTheEventLoop:
    """The D5 acknowledgement WAITS, so R-UX2 (SPEC Part II 15-UX UX-9(a))
    applies: it may not wait on the UI thread.

    ``tools/lint_offload`` accepts the site because it carries an
    ``offload-lint: ok`` marker naming where it runs. That marker is a claim,
    and a claim nobody checks is the escape hatch swallowing the finding it was
    meant to surface - so these pin the two facts it asserts.
    """

    _THREAD_NAME = "chat-submit-gate"

    def _page_src(self):
        from systemu.interface.pages import chat_page
        return inspect.getsource(chat_page.build_chat_page)

    def _submit_thread_name(self):
        """The ``name=`` of the real ``Thread(target=_submit, ...)`` CALL.

        Read from the AST, never from the source text: the marker comment
        itself spells ``name="chat-submit-gate"`` so a substring search over
        ``inspect.getsource`` is satisfied by the very comment it is meant to
        check. A ``ast.Call`` node is something a comment cannot forge.
        """
        tree = ast.parse(textwrap.dedent(self._page_src()))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else "")
            if attr != "Thread":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            target = kw.get("target")
            if not (isinstance(target, ast.Name) and target.id == "_submit"):
                continue
            name = kw.get("name")
            if type(name) is ast.Constant and type(name.value) is str:
                return name.value
            return None          # a Thread, but unnamed
        return ""                # no such Thread at all

    def test_the_submit_chokepoint_really_runs_on_its_own_thread(self):
        """Delete or rename the thread and the marker starts lying."""
        found = self._submit_thread_name()
        assert found != "", (
            "no Thread(target=_submit, ...) call in build_chat_page - the "
            "submit chokepoint no longer runs off the UI thread and the ack "
            "wait would block the event loop"
        )
        assert found == self._THREAD_NAME, (
            f"the submit thread is named {found!r}, but the offload-lint "
            f"marker on the ack poll names {self._THREAD_NAME!r}"
        )

    def test_the_ack_wait_marker_names_that_thread_not_merely_ok(self):
        src = self._page_src()
        marked = [ln for ln in src.splitlines() if "offload-lint: ok" in ln]
        assert marked, "the ack poll carries no offload-lint accounting at all"
        block = "\n".join(
            ln for ln in src.splitlines()
            if "offload-lint: ok" in ln or self._THREAD_NAME in ln
        )
        assert self._THREAD_NAME in block, (
            "a bare '# offload-lint: ok' says nothing - the marker must name "
            "the thread the wait actually runs on"
        )

    def test_the_interface_tree_has_no_unaccounted_blocking_call(self):
        """The gate itself, restated at the point of the change."""
        from tools.lint_offload import scan_repo
        violations = [v for v in scan_repo() if "chat_page.py" in v.path]
        assert violations == [], "\n".join(
            f"{v.path}:{v.line} {v.message}" for v in violations)
