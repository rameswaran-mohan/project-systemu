"""Route forge gate (Task 3): an auto-proposed tool is surfaced as exactly ONE
forge gate in the unified Decisions Inbox, and the routing seam does NOT ALSO
drive the SAME tool through the legacy ``notify_user(dedup_key="tool_forge:<id>")``
+ ``decision_dispatcher`` path — so a routed proposed tool forges exactly ONCE
on Approve (no double-forge).

Single-owner contract proved here:
  * The activity-extractor proposed-tool seam (``_queue_forge_notifications``)
    enqueues a ``forge:<id>`` gate; ``resolve_gate``'s forge branch is the
    executor.
  * That seam never posts a ``tool_forge:<id>`` operator decision, so the
    competing ``_handle_resolved_forge_tool`` dispatcher never fires for a
    routed tool.
"""
from unittest.mock import MagicMock, patch


# ── 1. Descriptor pin (sanity) ────────────────────────────────────────────────

def test_from_forge_dedup_pins_tool_id():
    """from_forge must accept a Tool-like object (not only a dict) and produce
    the forge:<id> dedup the resolve branch + InboxQueue rely on."""
    from systemu.interface.command.gate import GateDescriptor

    tool = type("T", (), {"id": "tool_y", "name": "Y", "description": "does y"})()
    g = GateDescriptor.from_forge(tool)
    assert g.dedup == "forge:tool_y"
    assert g.options == ["Skip", "Forge"]
    assert g.safe_default == "Skip"
    assert g.risk == "high"


def test_from_forge_still_accepts_dict():
    """Back-compat: the existing dict shape (Pending Tools card) keeps working."""
    from systemu.interface.command.gate import GateDescriptor

    g = GateDescriptor.from_forge(
        {"id": "tool_abc", "name": "csv_summariser",
         "description": "Summarise a CSV", "status": "proposed"})
    assert g.dedup == "forge:tool_abc"
    assert g.title == "Forge tool: csv_summariser"


# ── 2. The double-exec guard (the acceptance bar) ─────────────────────────────

def _make_tool(tool_id="tool_y", name="Y", description="does y"):
    """A minimal Tool-like stub the proposed-tool seam can render + dump."""
    return type("T", (), {
        "id": tool_id,
        "name": name,
        "description": description,
        "parameters_schema": {},
        "dependencies": [],
        "tool_type": "python_function",
        "model_dump": lambda self, *a, **k: {
            "id": tool_id, "name": name, "description": description},
    })()


def _make_vault_with_tool(tool):
    vault = MagicMock()
    vault.get_tool.return_value = tool
    return vault


def test_proposed_seam_enqueues_one_forge_gate(monkeypatch):
    """When the activity-extractor surfaces a PROPOSED tool, it enqueues exactly
    ONE forge GateDescriptor (gate_type='forge', dedup forge:<id>)."""
    import systemu.pipelines.activity_extractor as ax

    calls = []

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, **kw):
            calls.append((descriptor, gate_type))

    monkeypatch.setattr(ax, "InboxQueue", _FakeInbox, raising=False)

    tool = _make_tool()
    vault = _make_vault_with_tool(tool)
    activity = type("A", (), {"id": "act_1"})()
    scroll = type("S", (), {"id": "scroll_1", "name": "s"})()

    ax._queue_forge_notifications(["tool_y"], activity, scroll, vault)

    forge_gates = [(d, g) for d, g in calls if g == "forge"]
    assert len(forge_gates) == 1, f"expected exactly one forge gate, got {calls}"
    assert forge_gates[0][0].dedup == "forge:tool_y"


def test_routed_tool_does_not_also_post_tool_forge_decision(monkeypatch):
    """THE GUARD: routing a proposed tool as a forge: gate must NOT also leave a
    ``tool_forge:<id>`` operator decision that the dispatcher
    (_handle_resolved_forge_tool) would execute — that would double-forge.

    We patch the real notify_user the legacy ``forge_tool`` path uses and assert
    it is NEVER called with dedup_key="tool_forge:tool_y" while routing the
    proposed-tool seam. Exactly one executor (the forge: gate) owns the tool."""
    import systemu.pipelines.activity_extractor as ax
    import systemu.interface.notifications as N

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, **kw):
            pass

    monkeypatch.setattr(ax, "InboxQueue", _FakeInbox, raising=False)

    notify_calls = []

    def _spy_notify_user(*args, **kwargs):
        notify_calls.append(kwargs.get("dedup_key"))
        return "Skip"

    monkeypatch.setattr(N, "notify_user", _spy_notify_user, raising=False)

    tool = _make_tool()
    vault = _make_vault_with_tool(tool)
    activity = type("A", (), {"id": "act_1"})()
    scroll = type("S", (), {"id": "scroll_1", "name": "s"})()

    ax._queue_forge_notifications(["tool_y"], activity, scroll, vault)

    # No tool_forge:* decision was posted for the routed tool → the dispatcher
    # has nothing to re-run → forge happens exactly once (via the gate).
    assert "tool_forge:tool_y" not in notify_calls, (
        "routing the proposed tool ALSO posted a tool_forge: decision — "
        "the dispatcher would double-forge")


# ── 3. End-to-end: a routed tool forges EXACTLY ONCE on Approve ───────────────

def _tmp_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def test_routed_tool_forges_exactly_once_on_approve(tmp_path, monkeypatch):
    """The acceptance bar: a real proposed tool, routed as a forge: gate and
    Approved via the Inbox (resolve_gate), runs forge_tool_from_spec EXACTLY
    once. A subsequent dialog-cleanup resolve must NOT trigger a second forge."""
    from systemu.core.models import Tool, ToolType, ToolStatus
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.inbox import InboxQueue, resolve_gate
    from systemu.interface.command.gate import GateDescriptor

    vault = _tmp_vault(tmp_path)
    tool = Tool(
        id="tool_once", name="forge_once", description="x",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.PROPOSED,
        forged_by_systemu=True,
    )
    vault.save_tool(tool)

    # Route it (the seam's enqueue, exercised directly via the public adapter).
    InboxQueue(vault).enqueue(GateDescriptor.from_forge(tool), gate_type="forge")

    queue = OperatorDecisionQueue(vault)
    pending = [d for d in queue.list_pending() if d.dedup_key == "forge:tool_once"]
    assert len(pending) == 1, "expected exactly one forge gate row"

    # Count real forge executions by spying on forge_tool_from_spec.
    forge_calls = []
    import systemu.interface.command.inbox as inbox_mod

    def _spy_forge(tool_id, spec_json, config, vault):
        forge_calls.append(tool_id)
        return None

    monkeypatch.setattr(
        "systemu.pipelines.tool_forge.forge_tool_from_spec", _spy_forge)
    # resolve_gate imports Config.from_env(); stub it so no env is needed.
    monkeypatch.setattr("sharing_on.config.Config.from_env",
                        staticmethod(lambda: MagicMock()))

    # Approve via the Inbox → the single executor runs once.
    resolved = queue.resolve(pending[0].id, choice="Forge")
    resolve_gate(resolved, vault=vault)
    assert forge_calls == ["tool_once"], "Approve must forge exactly once"

    # The gate row is now resolved (not pending) — a dialog-cleanup or a second
    # Inbox Approve has nothing to re-resolve, so no second forge can happen.
    still_pending = [d for d in queue.list_pending()
                     if d.dedup_key == "forge:tool_once"]
    assert still_pending == [], "resolved gate must not remain pending"
    assert forge_calls == ["tool_once"], "no double-forge"
