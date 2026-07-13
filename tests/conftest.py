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
seconds in tests, so an **unresolved** gate fails fast (``None`` ⇒ Deny / decline)
instead of hanging.

This does NOT weaken any gate: the gate still fires, and a test that legitimately
exercises the gate by **pre-resolving** the decision is unaffected — the resolved choice
is returned on the first poll iteration, well within the bound. A test that specifically
needs the multi-second wait can monkeypatch these back.
"""
import pytest

_TEST_POLL_TIMEOUT_S = 2.0


@pytest.fixture(autouse=True)
def _bound_gate_block_polls(monkeypatch):
    import systemu.pipelines.quick_task as qt

    _orig_poll = qt._poll_command_choice
    _orig_ask = qt._ask_operator_inline

    def _fast_poll(vault, dedup_key, timeout=None):
        bound = _TEST_POLL_TIMEOUT_S if timeout is None else min(timeout, _TEST_POLL_TIMEOUT_S)
        return _orig_poll(vault, dedup_key, timeout=bound)

    def _fast_ask(vault, question, *, dedup_key, cancel_event=None, timeout=600.0):
        return _orig_ask(
            vault, question, dedup_key=dedup_key, cancel_event=cancel_event,
            timeout=min(timeout, _TEST_POLL_TIMEOUT_S),
        )

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
#  CRITICAL scoping — do NOT stub for the survey's OWN tests, which exercise the
#  REAL survey: early-return (no patch) when the test node carries
#  ``@pytest.mark.real_survey`` OR its file basename starts with ``test_ra9_``.
#  Those tests hit the real survey_situation directly (not via execute()), so the
#  module-attr patch would not even reach them — but we belt-and-suspenders skip
#  the patch entirely so nothing masks a regression there.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _stub_situation_survey(request, monkeypatch):
    import os

    node_path = str(getattr(request.node, "fspath", "") or "")
    basename = os.path.basename(node_path)
    if request.node.get_closest_marker("real_survey") or basename.startswith("test_ra9_"):
        # The survey's own tests exercise the REAL survey_situation — never stub.
        yield
        return

    import systemu.runtime.situational_inventory as _si

    async def _instant_empty_survey(scroll, *, vault, cache=None):
        # An instant, empty survey identical in SHAPE to a real empty-vault survey:
        # a bare SituationReport() (all slices empty) + an empty stamps dict. Its
        # model_dump() is a truthy, valid dict so context._situation_report and the
        # downstream planner gate behave exactly as with a real empty survey.
        return _si.SituationReport(), {}

    monkeypatch.setattr(_si, "survey_situation", _instant_empty_survey, raising=False)
    yield


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
