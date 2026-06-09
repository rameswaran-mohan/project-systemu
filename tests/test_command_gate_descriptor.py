from systemu.interface.command.gate import GateDescriptor


def test_descriptor_has_seven_canonical_fields():
    g = GateDescriptor(
        title="Approve scroll", risk="medium", inspect="diff here",
        options=["Deny", "Approve"], safe_default="Deny",
        what_approve_does="Marks the scroll APPROVED and runs it.",
        dedup="scroll:scr_1",
    )
    assert g.title and g.risk == "medium"
    assert g.safe_default in g.options
    assert g.dedup == "scroll:scr_1"


def test_from_harness_subsumes_surface_harness_context():
    class _Req:
        request_id = "hreq_1"
        rationale = "need network egress"
        urgency = "high"
        blocking = True
        class kind: value = "access"
    class _Verdict:
        class decision: value = "escalate"
        class risk_band: value = "high"
        rationale = "irreversible"

    g = GateDescriptor.from_harness(_Req(), _Verdict(), execution_id="exec_9")
    assert g.risk == "high"
    assert g.dedup == "harness:exec_9:hreq_1"
    assert "access" in g.title
    assert g.safe_default == "Deny"
    assert g.options[0] == "Deny"


def test_from_recovery_action_subsumes_recovery_action():
    class _Action:
        scope_kind = "tool"
        scope_id = "tool_a"
        kind = "GATE_3_DISABLED"
        reason = "tool not enabled"
        fix_url = "/recover/tool/tool_a"
        fix_command = "sharing_on tools enable tool_a"
        severity = "blocker"

    g = GateDescriptor.from_recovery_action(_Action())
    assert g.risk == "high"
    assert g.dedup == "recovery:tool:tool_a:GATE_3_DISABLED"
    assert "tools enable tool_a" in g.what_approve_does
    assert g.inspect == "tool not enabled"
