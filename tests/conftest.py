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
