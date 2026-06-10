"""Phase 5 Slice 3e — ``ensure_forge_gate``: enqueue-on-demand, never auto-exec,
and the Inbox forge card routes to the RICH human-code-review dialog (NOT the
degraded ``resolve_gate`` one-shot).

The forge gate row does NOT reliably exist for a tool reached via the registry
"Review & Forge" button: only the activity-extractor proposed-tool seam
(``_queue_forge_notifications``) enqueues a ``forge:<id>`` gate.  The registry
review path therefore creates the gate on demand — idempotently, and WITHOUT
consulting the gate-mode dial (``policy=None``): a policy'd enqueue under Bypass
would AUTO-EXECUTE the degraded one-shot via ``_synthetic_approved`` (inbox.py),
re-running ``forge_tool_from_spec`` over the UNEDITED spec and skipping the
human code review the operator just asked for.

Mirrors tests/test_ensure_scroll_gate.py: real FileVault(Vault(tmp)) end-to-end,
no queue mocks.

THE ACCEPTANCE BAR (Slice 3e): a routed forge approval must forge EXACTLY once,
via the rich review path — the Inbox forge card must NEVER trigger
``resolve_gate``'s ``forge_tool_from_spec`` one-shot.  We pin that the card
ROUTES to the rich dialog (``/tools?forge=<id>`` deep-link) instead of running
the generic Approve→resolve_gate chain the other gate kinds use.
"""
from __future__ import annotations

import inspect

from systemu.core.models import Tool, ToolType, ToolStatus


def _file_vault(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    return FileVault(Vault(tmp_path / "vault"))


def _tool(**kw) -> Tool:
    base = dict(
        id="tool_3e01",
        name="csv_summariser",
        description="Summarise a CSV file into key stats.",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.PROPOSED,
        forged_by_systemu=True,
    )
    base.update(kw)
    return Tool(**base)


def _forge_descriptors(vault, tool_id):
    """All PENDING gate descriptors for this tool (dedup forge:<id>)."""
    from systemu.interface.command.inbox import InboxQueue
    return [(dec_id, d) for dec_id, d in InboxQueue(vault).list_descriptors()
            if d.dedup == f"forge:{tool_id}"]


# ── (a) no existing gate → enqueues exactly ONE ──────────────────────────────

def test_no_existing_gate_enqueues_exactly_one(tmp_path):
    from systemu.interface.forge_gate import ensure_forge_gate
    vault = _file_vault(tmp_path)
    tool = _tool()
    vault.save_tool(tool)

    dec_id = ensure_forge_gate(vault, tool)

    rows = _forge_descriptors(vault, tool.id)
    assert len(rows) == 1
    assert rows[0][0] == dec_id
    desc = rows[0][1]
    # The from_forge contract: Skip-first safe default, forge-scoped dedup.
    assert desc.options == ["Skip", "Forge"]
    assert desc.safe_default == "Skip"
    assert desc.dedup == f"forge:{tool.id}"
    assert desc.risk == "high"


# ── (b) second call → NO duplicate (same dec_id) ─────────────────────────────

def test_second_call_returns_same_id_no_duplicate(tmp_path):
    from systemu.interface.forge_gate import ensure_forge_gate
    vault = _file_vault(tmp_path)
    tool = _tool()
    vault.save_tool(tool)

    first = ensure_forge_gate(vault, tool)
    second = ensure_forge_gate(vault, tool)

    assert first == second
    assert len(_forge_descriptors(vault, tool.id)) == 1


# ── (c) Bypass-mode guard: posts pending, NEVER executes the one-shot ─────────

def test_bypass_mode_still_posts_pending_and_never_forges(tmp_path, monkeypatch):
    """With the dial at Bypass (SYSTEMU_GATE_MODE=bypass), a policy'd enqueue
    would auto-grant the forge gate and run the degraded ``forge_tool_from_spec``
    one-shot without ever posting.  ensure_forge_gate must do the OPPOSITE: post
    a pending row for inspection and NEVER call forge_tool_from_spec (the human
    code review has not happened yet)."""
    monkeypatch.setenv("SYSTEMU_GATE_MODE", "bypass")
    calls = []
    monkeypatch.setattr(
        "systemu.pipelines.tool_forge.forge_tool_from_spec",
        lambda *a, **k: calls.append((a, k)))

    from systemu.interface.forge_gate import ensure_forge_gate
    vault = _file_vault(tmp_path)
    tool = _tool()
    vault.save_tool(tool)

    dec_id = ensure_forge_gate(vault, tool)

    assert calls == []                                   # one-shot NEVER ran
    rows = _forge_descriptors(vault, tool.id)
    assert len(rows) == 1 and rows[0][0] == dec_id       # pending row posted
    decision = vault.get_decision(dec_id)
    assert decision.status == "pending"                  # not auto-resolved


# ── (d) THE ACCEPTANCE BAR — the Inbox forge card routes to the rich dialog ───

def test_inbox_forge_card_routes_to_rich_dialog_not_one_shot(monkeypatch):
    """The Inbox forge card must NOT run ``resolve_gate``'s forge one-shot.

    We render the unified card for a forge gate with NiceGUI stubbed out, capture
    the buttons it builds, and assert: (1) the forge card builds a single
    "Review & Forge" button that navigates to the ``/tools?forge=<id>`` deep-link
    (the canonical rich-review path), and (2) clicking it NEVER calls
    ``resolve_gate`` (the degraded one-shot owns AUTO-PROPOSED tools only).
    """
    import systemu.interface.pages.inbox_page as inbox_page
    import systemu.interface.command.inbox as inbox_mod

    # Spy on resolve_gate at its SOURCE module: _render_unified_card imports it
    # lazily via ``from systemu.interface.command.inbox import resolve_gate``,
    # so a real call from the forge path would resolve to (and hit) this spy.
    resolve_calls = []
    monkeypatch.setattr(inbox_mod, "resolve_gate",
                        lambda *a, **k: resolve_calls.append((a, k)))

    # Capture navigation targets.
    nav_targets = []

    # Minimal NiceGUI stub: record buttons + navigation, swallow layout calls.
    buttons = []

    class _Stub:
        def __init__(self, *a, **k):
            pass

        def classes(self, *a, **k):
            return self

        def style(self, *a, **k):
            return self

        def props(self, *a, **k):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _UI:
        def element(self, *a, **k):
            return _Stub()

        def row(self, *a, **k):
            return _Stub()

        def column(self, *a, **k):
            return _Stub()

        def label(self, *a, **k):
            return _Stub()

        def html(self, *a, **k):
            return _Stub()

    class _Nav:
        def to(self, target):
            nav_targets.append(target)

    fake_ui = _UI()
    fake_ui.navigate = _Nav()
    monkeypatch.setattr(inbox_page, "ui", fake_ui)

    # Stub the design primitives the card imports lazily.
    import systemu.interface.design.primitives as prim

    def _fake_button(label, *, variant=None, on_click=None):
        buttons.append({"label": label, "variant": variant, "on_click": on_click})
        return _Stub()

    monkeypatch.setattr(prim, "button", _fake_button)
    monkeypatch.setattr(prim, "status_pill", lambda *a, **k: _Stub())

    from systemu.interface.command.gate import GateDescriptor
    descriptor = GateDescriptor.from_forge(
        {"id": "tool_route", "name": "router", "description": "routes"})

    inbox_page._render_unified_card(
        "dec_route", descriptor, vault=object(), on_resolved=lambda: None)

    # (1) Exactly one button — "Review & Forge" — and it does NOT offer the
    #     generic Skip/Forge resolve options.
    labels = [b["label"] for b in buttons]
    assert labels == ["Review & Forge"], labels

    # (2) Clicking it navigates to the canonical deep-link; resolve_gate is
    #     NEVER touched (no degraded one-shot).
    buttons[0]["on_click"]()
    assert nav_targets == ["/tools?forge=tool_route"], nav_targets
    assert resolve_calls == [], "forge card must NOT call resolve_gate"


def test_inbox_page_routes_forge_via_deep_link_source_pin():
    """Source pin: the Inbox unified card special-cases forge gates and routes
    via the /tools?forge= deep-link rather than the resolve_gate one-shot."""
    from systemu.interface.pages import inbox_page
    src = inspect.getsource(inbox_page)
    assert "/tools?forge=" in src
    assert "Review & Forge" in src
