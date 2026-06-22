from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from systemu.interface.command.gate import GateDescriptor


def test_from_scroll_builds_descriptor():
    scroll = SimpleNamespace(id="scr_abc", name="Make a burrito",
                             status=SimpleNamespace(value="pending_approval"))
    g = GateDescriptor.from_scroll(scroll, summary="3 activities, 1 new tool")
    assert g.title == "Approve scroll: Make a burrito"
    assert g.risk == "medium"
    assert g.options == ["Reject", "Approve"]
    assert g.safe_default == "Reject"
    assert g.dedup == "scroll:scr_abc"
    assert "extraction" in g.what_approve_does.lower()
    assert g.inspect == "3 activities, 1 new tool"


def test_scroll_reapproval_enqueues_gate_descriptor():
    from systemu.pipelines import scroll_refiner
    from systemu.interface.command.gate_mode import GateModePolicy
    scroll = type("S", (), {"id": "scr_abc", "name": "Burrito"})()
    fake_inbox = MagicMock()
    with patch.object(scroll_refiner, "InboxQueue", return_value=fake_inbox):
        scroll_refiner._queue_ready_for_reapproval_notification(scroll, MagicMock())
    assert fake_inbox.enqueue.called
    desc = fake_inbox.enqueue.call_args.args[0]
    assert desc.dedup == "scroll:scr_abc"
    kwargs = fake_inbox.enqueue.call_args.kwargs
    assert kwargs["gate_type"] == "scroll"
    # Task 14: the scroll gate is routed through the gate-mode dial — the
    # enqueue carries a GateModePolicy so the dial has teeth on the live path.
    assert isinstance(kwargs["policy"], GateModePolicy)


# ── G1: dep ──────────────────────────────────────────────────────────────────

def test_from_dep_builds_descriptor():
    # Shape mirrors DepApprovalStore.list_pending() entries.
    entry = {
        "package": "python-docx",
        "first_seen_at": "2026-05-13T12:35:01+00:00",
        "first_seen_tool": "create_word_doc",
        "first_seen_tool_id": "tool_6e6e62c0",
        "request_count": 3,
    }
    g = GateDescriptor.from_dep(entry)
    assert g.title == "Install dependency: python-docx"
    assert g.risk == "high"
    assert g.options == ["Dismiss", "Approve & Install"]
    assert g.safe_default == "Dismiss"
    assert g.dedup == "dep:python-docx"
    assert "pip install python-docx" in g.what_approve_does
    # Inspect surfaces the requesting tool + count so the operator has context.
    assert "create_word_doc" in g.inspect
    assert "3" in g.inspect


# ── G2: forge ────────────────────────────────────────────────────────────────

def test_from_forge_builds_descriptor():
    tool = {"id": "tool_abc", "name": "csv_summariser",
            "description": "Summarise a CSV", "status": "proposed"}
    g = GateDescriptor.from_forge(tool)
    assert g.title == "Forge tool: csv_summariser"
    assert g.risk == "high"
    assert g.options == ["Skip", "Forge"]
    assert g.safe_default == "Skip"
    assert g.dedup == "forge:tool_abc"
    assert "enables it" in g.what_approve_does.lower()
    assert "Summarise a CSV" in g.inspect


# ── G3: evolution ────────────────────────────────────────────────────────────

def test_from_evolution_builds_descriptor():
    proposal = SimpleNamespace(
        id="evo_123",
        evolution_type=SimpleNamespace(value="upgrade"),
        target_entity_type="shadow",
        target_entity_ids=["sh_1"],
        description="Teach the chef shadow to plate dishes",
        priority="high",
    )
    g = GateDescriptor.from_evolution(proposal)
    assert g.title.startswith("Evolution")
    assert "upgrade" in g.title.lower()
    assert g.risk == "high"
    assert g.options == ["Dismiss", "Approve & Apply"]
    assert g.safe_default == "Dismiss"
    assert g.dedup == "evolution:evo_123"
    # what_approve_does names the concrete change.
    assert "plate dishes" in g.what_approve_does


def test_from_evolution_priority_maps_to_risk():
    low = SimpleNamespace(
        id="evo_low", evolution_type=SimpleNamespace(value="discover"),
        target_entity_type="skill", target_entity_ids=["sk_1"],
        description="New pattern", priority="low",
    )
    assert GateDescriptor.from_evolution(low).risk == "low"
    medium = SimpleNamespace(
        id="evo_med", evolution_type=SimpleNamespace(value="merge"),
        target_entity_type="tool", target_entity_ids=["t_1", "t_2"],
        description="Merge two tools", priority="medium",
    )
    assert GateDescriptor.from_evolution(medium).risk == "medium"


# ── G4: operator (generic passthrough) ───────────────────────────────────────

def test_from_operator_builds_generic_descriptor():
    g = GateDescriptor.from_operator(
        title="Which city?",
        body="Pick the destination.",
        options=["Paris", "Lyon"],
        dedup="operator:choice:q1",
    )
    assert g.title == "Which city?"
    assert g.options == ["Paris", "Lyon"]
    assert g.safe_default == "Paris"      # defaults to options[0]
    assert g.dedup == "operator:choice:q1"
    assert g.risk == "low"                # default
    assert g.inspect == "Pick the destination."
