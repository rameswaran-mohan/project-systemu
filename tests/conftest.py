"""Global pytest fixtures.

Added for **S1b (the live action gate)**. Once the per-tool gate (``_maybe_gate_tool``)
and the MCP first-use gate (``_gate_mcp_call``) are wired into the live path, running
an effectful / UNKNOWN-tagged tool through the **quick lane** posts a
``PendingOperatorDecision`` and then **block-polls** for the operator's choice:

* ``pipelines.quick_task._poll_command_choice`` — waits up to **300 s**
* ``pipelines.quick_task._ask_operator_inline`` — waits up to **600 s**

In a test where no operator ever resolves the card, that stalls the whole suite for
minutes *per test* (this is exactly the ``_poll_command_choice`` block-poll the roadmap
flags for R-UX2). The autouse fixture below bounds those two block-polls to a couple of
seconds in tests, so an **unresolved** gate fails fast instead of hanging.

DEC-44 HONESTY FIX - the clamp used to hand the caller the timeout's ``None``, which
the product reads as **Deny / decline**. That silently manufactured a *product verdict*
(deny-on-timeout) inside every test that simply forgot to resolve its gate, and no test
asserted it. The bound stays (an unbounded suite hangs), but the clamped wrapper now
**raises AssertionError** instead of returning ``None``: a gate the test never resolved
is a broken test, not a Deny.

Escape hatches, both explicit and in the test's own source:

* pre-resolve the decision (``OperatorDecisionQueue.resolve``) - the resolved choice is
  returned on the first poll iteration, well within the bound. Use
  ``resolve_gate_as_denied`` below to assert a *chosen* Deny (never a defaulted one).
* ``@pytest.mark.slow_gate_polls`` - opt out of the clamp entirely and get the REAL
  block-poll with its real timeout semantics (for tests whose SUBJECT is the timeout
  path itself). Such a test must pass its own short ``timeout=``; nothing bounds it.

This does NOT weaken any gate: the gate still fires either way.
"""
import pytest

_TEST_POLL_TIMEOUT_S = 2.0


def resolve_gate_as_denied(vault, dedup_key: str, *, title: str = "gate"):
    """Post + resolve a command gate as an EXPLICIT operator Deny (DEC-44).

    A test that wants to assert the deny path must *choose* Deny, not inherit it
    from an unresolved poll timing out. Returns the decision id.
    """
    from systemu.approval.decision_queue import OperatorDecisionQueue

    q = OperatorDecisionQueue(vault)
    dec_id = q.post(
        title=title, body="Resolved as Deny by the test.",
        options=["Deny", "Approve once", "Always allow"],
        dedup_key=dedup_key,
    )
    q.resolve(dec_id, choice="Deny")
    return dec_id


def _gate_was_answered(vault, dedup_key) -> bool:
    """True when the operator actually RESOLVED this decision.

    Distinguishes ``_ask_operator_inline``'s several ``None`` returns: a resolved
    empty/whitespace answer is a genuine *decline* (leave it alone), while an
    unresolved wait that ran out is the dishonest case DEC-44 targets. A timed-out
    ask is EXPIRED (``expire_by_dedup_key``), never resolved, so this stays False.
    Unreadable queue -> False, i.e. fail LOUD rather than restore the silent decline.
    """
    try:
        from systemu.approval.decision_queue import OperatorDecisionQueue
        return OperatorDecisionQueue(vault).get_resolved_choice(dedup_key) is not None
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _bound_gate_block_polls(request, monkeypatch):
    import systemu.pipelines.quick_task as qt

    if request.node.get_closest_marker("slow_gate_polls"):
        # DEC-44: this test's SUBJECT is the real block-poll timeout. No clamp,
        # no raise-on-None -- it gets the untouched product functions.
        yield
        return

    _orig_poll = qt._poll_command_choice
    _orig_ask = qt._ask_operator_inline

    def _fast_poll(vault, dedup_key, timeout=None):
        bound = _TEST_POLL_TIMEOUT_S if timeout is None else min(timeout, _TEST_POLL_TIMEOUT_S)
        choice = _orig_poll(vault, dedup_key, timeout=bound)
        if choice is None:
            raise AssertionError(
                f"gate {dedup_key!r} unresolved after clamped {bound}s - resolve or "
                "extend it in the test (conftest.resolve_gate_as_denied for an "
                "explicit Deny, or @pytest.mark.slow_gate_polls for the real timeout)"
            )
        return choice

    def _fast_ask(vault, question, *, dedup_key, cancel_event=None, timeout=600.0):
        bound = min(timeout, _TEST_POLL_TIMEOUT_S)
        answer = _orig_ask(
            vault, question, dedup_key=dedup_key, cancel_event=cancel_event,
            timeout=bound,
        )
        if answer is None:
            cancelled = cancel_event is not None and cancel_event.is_set()
            if not cancelled and not _gate_was_answered(vault, dedup_key):
                raise AssertionError(
                    f"gate {dedup_key!r} unresolved after clamped {bound}s - resolve "
                    "or extend it in the test (resolve the decision, or "
                    "@pytest.mark.slow_gate_polls for the real timeout)"
                )
        return answer

    monkeypatch.setattr(qt, "_poll_command_choice", _fast_poll)
    monkeypatch.setattr(qt, "_ask_operator_inline", _fast_ask)
    yield


# ─────────────────────────────────────────────────────────────────────────────
#  Fixture A — router fast-fail safety net.
#
#  ANY unmocked LLM call in a test (e.g. a bare Config() with empty provider keys
#  whose caller degrades on failure, or a test that passes a dummy key) otherwise
#  hits the router's real network ladder: _API_TIMEOUT_SECONDS (120s) ×
#  (_NETWORK_MAX_RETRIES + 1) with _NETWORK_BACKOFF_S back-off ([5, 15]) ≈ 380s
#  PER unmocked call. That is exactly the ~380s end-of-run stall the episodic
#  capture caused before its key-guard fix (episodic_memory._has_llm_provider).
#
#  This autouse fixture bounds that ladder to ~2s so an unmocked call fails FAST.
#  It weakens NO assertion: every in-tree caller of llm_call_json degrades on a
#  failed/timed-out call (planner → static tree, episodic → None, verifiers →
#  soft-pass/None). A test that legitimately needs the real timeout can
#  monkeypatch these three names back.
#
#  DEC-44 HYGIENE CARVE-OUT - this one STAYS autouse, deliberately. DEC-44 is
#  about autouse fixtures that replace PRODUCT BEHAVIOUR with a double, so a
#  green test proves the double rather than the product. This fixture replaces
#  no behaviour and no code path: it sets three timing/retry SCALARS. Every
#  outcome it can produce (success, failure, timeout) is one the unclamped
#  router also produces -- it only changes how long the suite waits to get
#  there. Nothing here can turn a red test green.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _fast_fail_llm_router(monkeypatch):
    import systemu.core.llm_router as _lr

    monkeypatch.setattr(_lr, "_API_TIMEOUT_SECONDS", 2.0, raising=False)
    monkeypatch.setattr(_lr, "_NETWORK_MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(_lr, "_NETWORK_BACKOFF_S", [], raising=False)
    yield


# ─────────────────────────────────────────────────────────────────────────────
#  Fixture B — situational-inventory survey stub (R-A9, execute()'s pre-planner
#  survey at shadow_runtime.py:~4649).
#
#  execute() runs ``survey_situation(...)`` under a 20s asyncio.wait_for. In a
#  hermetic test with a fresh empty vault the survey is cheap, but it still spins
#  the dedicated survey ThreadPoolExecutor and walks the (empty) stores every
#  execute()-driving test — pure per-test cost that buys nothing for tests that
#  don't assert on the survey. Replace it with an async no-op that returns an
#  INSTANT empty ``(SituationReport(), {})``.
#
#  execute() re-imports the symbol locally
#  (``from systemu.runtime.situational_inventory import survey_situation``) right
#  before the call, so patching the MODULE attribute is what takes effect.
#
#  The empty ``SituationReport()`` .model_dump() is a full, non-empty dict (all
#  slice keys present, empty) — byte-compatible with a real empty-vault survey —
#  so ``context._situation_report`` stays truthy/valid and the downstream R-A10
#  planner gate (which only checks truthiness) is unaffected.
#
#  DEC-44 - THIS IS NOW OPT-IN. It used to be autouse with two escapes (the
#  ``real_survey`` marker and a ``test_ra9_`` FILENAME prefix), which meant the
#  default for the whole suite was: execute()'s pre-planner survey does not run.
#  Any behaviour that depends on a real survey -- and any regression in it --
#  was invisible to ~every execute()-driving test, and no reader of those tests
#  could tell. The real survey is the DEFAULT again. A test that genuinely wants
#  the double (unit isolation, a hot loop where the survey is pure cost) asks
#  for it by name and says why:
#
#      @pytest.mark.stub_survey  # <one-line justification>
#
#  The ``real_survey`` marker and the ``test_ra9_`` filename escape are GONE:
#  real is no longer the exception, so there is nothing to opt out of. A
#  filename prefix was never a sound scoping rule anyway -- it silently
#  un-stubbed any new file that happened to be named that way, and silently
#  stubbed any survey test that was not.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def _stub_situation_survey(monkeypatch):
    import systemu.runtime.situational_inventory as _si

    async def _instant_empty_survey(scroll, *, vault, cache=None):
        # An instant, empty survey identical in SHAPE to a real empty-vault survey:
        # a bare SituationReport() (all slices empty) + an empty stamps dict. Its
        # model_dump() is a truthy, valid dict so context._situation_report and the
        # downstream planner gate behave exactly as with a real empty survey.
        return _si.SituationReport(), {}

    monkeypatch.setattr(_si, "survey_situation", _instant_empty_survey, raising=False)
    yield


def _wire_stub_survey_marker(item):
    """Make ``@pytest.mark.stub_survey`` pull in the (non-autouse) stub fixture.

    The fixture itself carries no ``autouse``: a test that does not ask for the
    double gets the real ``survey_situation``, full stop (DEC-44). This is the
    single place the marker turns into a fixture request.
    """
    if item.get_closest_marker("stub_survey") is None:
        return
    names = getattr(item, "fixturenames", None)
    if names is None or "_stub_situation_survey" in names:
        return
    names.append("_stub_situation_survey")


# ─────────────────────────────────────────────────────────────────────────────
# GATE-TIER (DEC-14) — auto-tag SOURCE-SENSITIVE tests so there is an EDIT-SAFE
# subset. Many tests assert on a function/class body read via ``inspect.getsource``
# (~90 files). Those compare against a source SNAPSHOT: if a subagent edits the
# file under test WHILE the suite runs, getsource returns the new text and the
# assertion fails spuriously — the long-standing "never run the full gate while
# subagents edit the same file" constraint. Rather than rewrite ~90 files, we
# auto-tag them ``source_sensitive`` by module content, so:
#     pytest -m "not source_sensitive"
# is a fast, EDIT-SAFE gate you CAN run concurrently with source edits.
# ─────────────────────────────────────────────────────────────────────────────
import functools as _functools


def module_text_is_source_sensitive(text: str) -> bool:
    """True if a test module reads source via ``inspect.getsource`` — pure +
    trivially testable (the detection contract GATE-TIER's auto-tagger relies on)."""
    return "getsource(" in (text or "")


@_functools.lru_cache(maxsize=None)
def _path_is_source_sensitive(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return module_text_is_source_sensitive(fh.read())
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``source_sensitive`` marker to every test whose module reads
    source via getsource — no manual per-file marking. Defensive: a detection
    hiccup on one item never breaks collection."""
    for item in items:
        try:
            path = str(getattr(item, "path", None) or getattr(item, "fspath", "") or "")
            if path and _path_is_source_sensitive(path):
                item.add_marker(pytest.mark.source_sensitive)
        except Exception:
            continue

    # DEC-44 survey opt-in. NOT wrapped in try/except: a failure to wire is a
    # conftest bug and must be loud. (Its only failure direction is safe anyway
    # -- an unwired test gets the REAL survey.)
    for item in items:
        _wire_stub_survey_marker(item)
