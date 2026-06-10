"""Phase 5 Slice 2b — ``ensure_scroll_gate``: enqueue-on-demand, never auto-exec.

The scroll gate row does NOT reliably exist: the only producer
(``scroll_refiner._queue_ready_for_reapproval_notification``) runs on
RE-approvals after a tool unblock, so first-time PENDING_APPROVAL scrolls
have no row.  The Work/Scrolls "Review & Approve" flow therefore creates the
gate on demand — idempotently, and WITHOUT consulting the gate-mode dial
(``policy=None``): a policy'd enqueue under Bypass would auto-execute via
``_synthetic_approved`` (inbox.py) and skip the inspection the operator just
asked for.

Uses a real FileVault(Vault(tmp)) end-to-end (no queue mocks), mirroring
tests/test_harness_surface_coords.py.
"""
from __future__ import annotations

from systemu.core.models import Scroll, ScrollStatus


def _file_vault(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    return FileVault(Vault(tmp_path / "vault"))


def _scroll(**kw) -> Scroll:
    base = dict(
        id="scroll_2b01",
        name="Burrito order",
        source_session_id="sess_1",
        raw_instructions_path="captures/sess_1/instructions.md",
        narrative_md="Open the shop page, add a burrito, check out.",
        intent="Order the usual burrito for Friday lunch.",
        status=ScrollStatus.PENDING_APPROVAL,
    )
    base.update(kw)
    return Scroll(**base)


def _scroll_descriptors(vault, scroll_id):
    """All PENDING gate descriptors for this scroll (dedup scroll:<id>)."""
    from systemu.interface.command.inbox import InboxQueue
    return [(dec_id, d) for dec_id, d in InboxQueue(vault).list_descriptors()
            if d.dedup == f"scroll:{scroll_id}"]


# ── (a) no existing gate → enqueues exactly ONE ──────────────────────────────

def test_no_existing_gate_enqueues_exactly_one(tmp_path):
    from systemu.interface.scroll_gate import ensure_scroll_gate
    vault = _file_vault(tmp_path)
    scroll = _scroll()
    vault.save_scroll(scroll)

    dec_id = ensure_scroll_gate(vault, scroll)

    rows = _scroll_descriptors(vault, scroll.id)
    assert len(rows) == 1
    assert rows[0][0] == dec_id
    desc = rows[0][1]
    # The from_scroll contract: Reject-first safe default, scroll-scoped dedup.
    assert desc.options == ["Reject", "Approve"]
    assert desc.safe_default == "Reject"
    assert desc.dedup == f"scroll:{scroll.id}"


# ── (b) second call → NO duplicate (same dec_id) ─────────────────────────────

def test_second_call_returns_same_id_no_duplicate(tmp_path):
    from systemu.interface.scroll_gate import ensure_scroll_gate
    vault = _file_vault(tmp_path)
    scroll = _scroll()
    vault.save_scroll(scroll)

    first = ensure_scroll_gate(vault, scroll)
    second = ensure_scroll_gate(vault, scroll)

    assert first == second
    assert len(_scroll_descriptors(vault, scroll.id)) == 1


# ── (c) Bypass-mode guard: posts pending, NEVER executes ─────────────────────

def test_bypass_mode_still_posts_pending_and_never_executes(tmp_path, monkeypatch):
    """With the dial at Bypass (SYSTEMU_GATE_MODE=bypass), a policy'd enqueue
    would auto-grant the scroll gate (non-floor) and run the approve executor
    without ever posting.  ensure_scroll_gate must do the OPPOSITE: post a
    pending row for inspection and never call approve_pending_scroll."""
    monkeypatch.setenv("SYSTEMU_GATE_MODE", "bypass")
    calls = []
    monkeypatch.setattr(
        "systemu.pipelines.scroll_refiner.approve_pending_scroll",
        lambda *a, **k: calls.append((a, k)))

    from systemu.interface.scroll_gate import ensure_scroll_gate
    vault = _file_vault(tmp_path)
    scroll = _scroll()
    vault.save_scroll(scroll)

    dec_id = ensure_scroll_gate(vault, scroll)

    assert calls == []                                   # executor NEVER ran
    rows = _scroll_descriptors(vault, scroll.id)
    assert len(rows) == 1 and rows[0][0] == dec_id       # pending row posted
    decision = vault.get_decision(dec_id)
    assert decision.status == "pending"                  # not auto-resolved


# ── (d) non-empty inspect for first-time gates ───────────────────────────────

def test_inspect_is_scroll_intent_when_present(tmp_path):
    from systemu.interface.scroll_gate import ensure_scroll_gate
    vault = _file_vault(tmp_path)
    scroll = _scroll()
    vault.save_scroll(scroll)

    ensure_scroll_gate(vault, scroll)

    (_, desc), = _scroll_descriptors(vault, scroll.id)
    assert desc.inspect == "Order the usual burrito for Friday lunch."


def test_inspect_falls_back_to_narrative_when_no_intent(tmp_path):
    from systemu.interface.scroll_gate import ensure_scroll_gate
    vault = _file_vault(tmp_path)
    scroll = _scroll(intent="", narrative_md="X" * 500)
    vault.save_scroll(scroll)

    ensure_scroll_gate(vault, scroll)

    (_, desc), = _scroll_descriptors(vault, scroll.id)
    assert desc.inspect == "X" * 200                     # capped at 200 chars


# ── Slice 2b wiring: both pages open the dialog; the blind approve is gone ──

class TestWiring:
    """Source pins (precedent: test_v0_8_12_names.TestRemainingPages) — the
    Work row's needs_approval affordance and the Scrolls page both route
    through open_scroll_review_dialog; the blind dispatch path is retired."""

    def test_work_row_wires_review_dialog(self):
        import inspect
        from systemu.interface.pages import work
        src = inspect.getsource(work)
        assert "open_scroll_review_dialog" in src
        assert "Review & Approve" in src
        # The Slice 2a placeholder "Review → /inbox" link is retired.
        assert '"/inbox"' not in src

    def test_scrolls_page_blind_approve_retired(self):
        import inspect
        from systemu.interface.pages import scrolls
        src = inspect.getsource(scrolls)
        assert "open_scroll_review_dialog" in src
        assert "Review & Approve" in src
        assert "scrolls approve" not in src    # the blind dispatch is gone
        assert "_approve_scroll" not in src    # dead helper removed
        assert "resolve_name" in src           # v0_8_12 names pin stays true
