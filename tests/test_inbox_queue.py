from unittest.mock import MagicMock, patch

from systemu.interface.command.gate import GateDescriptor


def test_descriptor_round_trips_through_decision_context():
    g = GateDescriptor(
        title="Approve scroll: Make a burrito",
        risk="medium",
        inspect="3 activities, 1 new tool needed.",
        options=["Reject", "Approve"],
        safe_default="Reject",
        what_approve_does="Runs skill/tool extraction and creates the activity.",
        dedup="scroll:scr_abc",
    )
    ctx = g.to_decision_context(gate_type="scroll")
    assert ctx["kind"] == "gate"
    assert ctx["gate_type"] == "scroll"
    assert ctx["risk"] == "medium"
    assert ctx["what_approve_does"].startswith("Runs skill")

    back = GateDescriptor.from_decision_context(ctx, title=g.title, options=g.options, dedup=g.dedup)
    assert back == g


def test_enqueue_posts_descriptor_to_operator_decision_queue():
    g = GateDescriptor.from_scroll(
        type("S", (), {"id": "scr_abc", "name": "Burrito"})(), summary="x")
    fake_queue = MagicMock()
    fake_queue.post.return_value = "dec_111"

    with patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue):
        from systemu.interface.command.inbox import InboxQueue
        inbox = InboxQueue(MagicMock())
        dec_id = inbox.enqueue(g, gate_type="scroll")

    assert dec_id == "dec_111"
    kwargs = fake_queue.post.call_args.kwargs
    assert kwargs["title"] == g.title
    assert kwargs["options"] == ["Reject", "Approve"]
    assert kwargs["dedup_key"] == "scroll:scr_abc"
    assert kwargs["context"]["kind"] == "gate"
    assert kwargs["context"]["gate_type"] == "scroll"
    assert kwargs["context"]["what_approve_does"]


# ── Task 12: enqueue enforces the gate-mode dial (Bypass auto-grants) ────────

def test_bypass_auto_grants_non_floor_gate_without_posting():
    """Under Bypass, a non-floor gate auto-grants: resolve_gate runs the action
    directly and the decision is NEVER posted to the operator queue."""
    from systemu.interface.command.gate_mode import GateMode, GateModePolicy
    from systemu.interface.command.result import CommandResult, CommandStatus

    g = GateDescriptor.from_forge(
        {"id": "tool_abc", "name": "csv_summariser", "description": "x"})
    fake_queue = MagicMock()
    policy = GateModePolicy(mode=GateMode.BYPASS)
    fake_vault = MagicMock()

    with patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue), \
         patch("systemu.interface.command.inbox.resolve_gate",
               return_value=CommandResult(status=CommandStatus.OK, summary="ran")) as rg:
        from systemu.interface.command.inbox import InboxQueue
        inbox = InboxQueue(fake_vault)
        inbox.enqueue(g, gate_type="forge", policy=policy)

    fake_queue.post.assert_not_called()
    rg.assert_called_once()
    # The synthetic decision carries the gate context, dedup, and the approve
    # choice (options[-1]) so resolve_gate executes the authorized action.
    synthetic = rg.call_args.args[0]
    assert synthetic.context["gate_type"] == "forge"
    assert synthetic.dedup_key == "forge:tool_abc"
    assert synthetic.choice == g.options[-1]   # "Forge"
    assert rg.call_args.kwargs["vault"] is fake_vault


def test_bypass_floor_gate_still_posts():
    """Under Bypass, a FLOOR gate (dep) still posts to the operator queue (D5):
    auto-grant must never apply to the floor."""
    from systemu.interface.command.gate_mode import GateMode, GateModePolicy

    g = GateDescriptor.from_dep({
        "package": "python-docx", "first_seen_tool": "create_word_doc",
        "first_seen_tool_id": "tool_x", "request_count": 1})
    fake_queue = MagicMock()
    fake_queue.post.return_value = "dec_dep"
    policy = GateModePolicy(mode=GateMode.BYPASS)

    with patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue), \
         patch("systemu.interface.command.inbox.resolve_gate") as rg:
        from systemu.interface.command.inbox import InboxQueue
        inbox = InboxQueue(MagicMock())
        dec_id = inbox.enqueue(g, gate_type="dep", policy=policy)

    fake_queue.post.assert_called_once()
    rg.assert_not_called()
    assert dec_id == "dec_dep"


def test_deny_records_resolved_audit_row_without_executing():
    """A 'deny' verdict records an auditable resolved-denied row (post then
    resolve with the safe_default) and never executes the action."""
    from systemu.interface.command.gate_mode import GateMode, GateModePolicy

    g = GateDescriptor.from_forge(
        {"id": "tool_abc", "name": "csv_summariser", "description": "x"})
    fake_queue = MagicMock()
    fake_queue.post.return_value = "dec_denied"
    # Per-type override forces deny even under Bypass.
    policy = GateModePolicy(mode=GateMode.BYPASS, overrides={"forge": "deny"})

    with patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue), \
         patch("systemu.interface.command.inbox.resolve_gate") as rg:
        from systemu.interface.command.inbox import InboxQueue
        inbox = InboxQueue(MagicMock())
        dec_id = inbox.enqueue(g, gate_type="forge", policy=policy)

    # Posted for audit, then resolved with the safe-default (deny) choice.
    fake_queue.post.assert_called_once()
    fake_queue.resolve.assert_called_once()
    assert fake_queue.resolve.call_args.kwargs["choice"] == g.safe_default  # "Skip"
    # The action's executor (resolve_gate) is NEVER invoked for a denied gate.
    rg.assert_not_called()
    assert dec_id == "dec_denied"


def test_enqueue_without_policy_behaves_as_today_post_always():
    """policy=None preserves today's post-always behaviour exactly."""
    g = GateDescriptor.from_scroll(
        type("S", (), {"id": "scr_abc", "name": "Burrito"})(), summary="x")
    fake_queue = MagicMock()
    fake_queue.post.return_value = "dec_111"
    with patch("systemu.approval.decision_queue.OperatorDecisionQueue",
               return_value=fake_queue), \
         patch("systemu.interface.command.inbox.resolve_gate") as rg:
        from systemu.interface.command.inbox import InboxQueue
        inbox = InboxQueue(MagicMock())
        dec_id = inbox.enqueue(g, gate_type="scroll")
    fake_queue.post.assert_called_once()
    rg.assert_not_called()
    assert dec_id == "dec_111"


def test_resolve_gate_approve_executes_scroll_extraction():
    from systemu.interface.command.inbox import resolve_gate
    fake_vault = MagicMock()
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "scroll"}
    decision.dedup_key = "scroll:scr_abc"
    decision.choice = "Approve"

    with patch("systemu.pipelines.scroll_refiner.approve_pending_scroll") as ap:
        result = resolve_gate(decision, vault=fake_vault)

    ap.assert_called_once_with("scr_abc", fake_vault)
    assert result.status.value in ("ok", "queued")


def test_resolve_gate_reject_does_not_execute():
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "scroll"}
    decision.dedup_key = "scroll:scr_abc"
    decision.choice = "Reject"
    with patch("systemu.pipelines.scroll_refiner.approve_pending_scroll") as ap:
        resolve_gate(decision, vault=MagicMock())
    ap.assert_not_called()


# ── G1: dep resolve branch ───────────────────────────────────────────────────

def test_resolve_gate_approve_dep_calls_approve_and_install():
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "dep",
                        "inspect": "Requested by create_word_doc (tool_6e6e62c0) x3"}
    decision.dedup_key = "dep:python-docx"
    decision.choice = "Approve & Install"
    with patch("systemu.runtime.dep_approvals.approve_and_install") as ai:
        result = resolve_gate(decision, vault=MagicMock())
    ai.assert_called_once()
    kwargs = ai.call_args.kwargs
    assert kwargs["package"] == "python-docx"
    assert "tool_id" in kwargs
    assert result.status.value in ("ok", "queued")


def test_resolve_gate_dismiss_dep_does_not_install():
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "dep"}
    decision.dedup_key = "dep:python-docx"
    decision.choice = "Dismiss"
    with patch("systemu.runtime.dep_approvals.approve_and_install") as ai:
        resolve_gate(decision, vault=MagicMock())
    ai.assert_not_called()


# ── G2: forge resolve branch ─────────────────────────────────────────────────

def test_resolve_gate_forge_calls_forge_tool_from_spec():
    from systemu.interface.command.inbox import resolve_gate
    fake_vault = MagicMock()
    fake_tool = MagicMock()
    fake_tool.model_dump_json.return_value = '{"name": "csv_summariser"}'
    fake_vault.get_tool.return_value = fake_tool
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "forge"}
    decision.dedup_key = "forge:tool_abc"
    decision.choice = "Forge"
    with patch("systemu.pipelines.tool_forge.forge_tool_from_spec") as ft, \
         patch("sharing_on.config.Config.from_env", return_value=MagicMock()):
        result = resolve_gate(decision, vault=fake_vault)
    ft.assert_called_once()
    assert ft.call_args.args[0] == "tool_abc"
    # Two-stage labelled in the summary — never silent.
    assert "code generation" in result.summary.lower() or "queued" in result.summary.lower()


def test_resolve_gate_skip_forge_does_not_execute():
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "forge"}
    decision.dedup_key = "forge:tool_abc"
    decision.choice = "Skip"
    with patch("systemu.pipelines.tool_forge.forge_tool_from_spec") as ft:
        resolve_gate(decision, vault=MagicMock())
    ft.assert_not_called()


# ── G3: evolution resolve branch ─────────────────────────────────────────────

def test_resolve_gate_approve_evolution_calls_apply():
    from systemu.interface.command.inbox import resolve_gate
    fake_vault = MagicMock()
    # The implemented apply path is UPGRADE on a shadow; set up that record so
    # the unimplemented-type guard lets it through to apply_evolution.
    evo = MagicMock()
    evo.evolution_type.value = "upgrade"
    evo.target_entity_type = "shadow"
    fake_vault.get_evolution.return_value = evo
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "evolution"}
    decision.dedup_key = "evolution:evo_123"
    decision.choice = "Approve & Apply"
    with patch("systemu.pipelines.evolution_engine.apply_evolution",
               return_value=True) as ae, \
         patch("sharing_on.config.Config.from_env", return_value=MagicMock()):
        result = resolve_gate(decision, vault=fake_vault)
    ae.assert_called_once()
    assert ae.call_args.args[0] == "evo_123"
    assert result.status.value == "ok"


def test_resolve_gate_evolution_unimplemented_type_surfaces_error():
    """The silent-no-op-for-non-UPGRADE bug: an unimplemented evolution type
    must surface ERROR with a clear summary, never a silent NOOP/OK."""
    from systemu.interface.command.inbox import resolve_gate
    fake_vault = MagicMock()
    # The evolution record's type is one apply_evolution doesn't implement.
    evo = MagicMock()
    evo.evolution_type = MagicMock()
    evo.evolution_type.value = "merge"
    evo.target_entity_type = "tool"
    fake_vault.get_evolution.return_value = evo
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "evolution"}
    decision.dedup_key = "evolution:evo_merge"
    decision.choice = "Approve & Apply"
    with patch("sharing_on.config.Config.from_env", return_value=MagicMock()):
        result = resolve_gate(decision, vault=fake_vault)
    assert result.status.value == "error"
    assert "merge" in result.summary.lower()
    assert "not yet implemented" in result.summary.lower()


# ── G4: operator resolve branch (render-only, no re-execution) ───────────────

def test_resolve_gate_operator_returns_ok_without_executor():
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "operator"}
    decision.dedup_key = "operator:choice:q1"
    decision.choice = "Paris"   # any choice unblocks the waiting run
    result = resolve_gate(decision, vault=MagicMock())
    assert result.status.value == "ok"


# ── G5: recovery resolve branch ──────────────────────────────────────────────

def test_resolve_gate_approve_recovery_calls_doctor_apply():
    from systemu.interface.command.inbox import resolve_gate
    from systemu.interface.command.result import CommandResult, CommandStatus
    fake_vault = MagicMock()
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "recovery",
                        "inspect": "Missing package: python-docx",
                        "what_approve_does": "pip install python-docx"}
    decision.dedup_key = "recovery:tool:tool_abc:DEP_PENDING"
    decision.choice = "Approve & Apply"
    with patch("systemu.interface.command.verbs.doctor_apply",
               return_value=CommandResult(status=CommandStatus.OK, summary="Applied 1")) as da:
        result = resolve_gate(decision, vault=fake_vault)
    da.assert_called_once()
    actions = da.call_args.args[0]
    assert len(actions) == 1
    assert actions[0].scope_kind == "tool"
    assert actions[0].scope_id == "tool_abc"
    assert actions[0].kind == "DEP_PENDING"
    assert result.status.value in ("ok", "noop")


def test_resolve_gate_harness_approve_surfaces_unwired_state_not_silent():
    """The real harness grant executor (Supervisor.resume_after_grant) needs a
    live daemon Supervisor + resume coords absent from resolve_gate's signature.
    Approving a harness gate must SURFACE that (QUEUED), never silently NOOP/OK."""
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "harness",
                        "execution_id": "exec_1", "request_id": "req_1"}
    decision.dedup_key = "harness:exec_1:req_1"
    decision.choice = "Approve"
    result = resolve_gate(decision, vault=MagicMock())
    assert result.status.value == "queued"
    assert result.summary  # non-empty, explains the deferral


def test_resolve_gate_dismiss_recovery_does_not_apply():
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = {"kind": "gate", "gate_type": "recovery"}
    decision.dedup_key = "recovery:tool:tool_abc:DEP_PENDING"
    decision.choice = "Dismiss"
    with patch("systemu.interface.command.verbs.doctor_apply") as da:
        resolve_gate(decision, vault=MagicMock())
    da.assert_not_called()


# ── Batch-1 cleanup: dep tool_id round-trips through the decision context ─────

def test_dep_gate_round_trips_tool_id_into_resolve_branch():
    """A dep gate built from a DepApprovalStore entry must carry the real
    requesting tool_id through to_decision_context → from_decision_context so the
    resolve branch's approve_and_install(tool_id=...) targets the real tool
    (previously dropped → always tool_id="")."""
    entry = {
        "package": "python-docx",
        "first_seen_tool": "create_word_doc",
        "first_seen_tool_id": "tool_6e6e62c0",
        "request_count": 3,
    }
    g = GateDescriptor.from_dep(entry)
    ctx = g.to_decision_context(gate_type="dep")
    # The tool_id is serialized (non-empty) into the stored context.
    assert ctx.get("tool_id") == "tool_6e6e62c0"

    # And the resolve branch passes that real tool_id to approve_and_install.
    from systemu.interface.command.inbox import resolve_gate
    decision = MagicMock()
    decision.context = ctx
    decision.dedup_key = g.dedup            # "dep:python-docx"
    decision.choice = "Approve & Install"
    with patch("systemu.runtime.dep_approvals.approve_and_install") as ai:
        resolve_gate(decision, vault=MagicMock())
    ai.assert_called_once()
    kwargs = ai.call_args.kwargs
    assert kwargs["tool_id"] == "tool_6e6e62c0"
    assert kwargs["package"] == "python-docx"
