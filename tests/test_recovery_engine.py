from systemu.recovery.engine import RecoveryEngine, RecoveryAction


class FakeTool:
    def __init__(self, id, name, status="approved", enabled=True,
                 dry_run_status="passed", dry_run_evidence=None):
        self.id = id
        self.name = name
        self.status = status
        self.enabled = enabled
        self.dry_run_status = dry_run_status
        self.dry_run_evidence = dry_run_evidence


class FakeVault:
    def __init__(self, tools=None):
        self._tools = {t.id: t for t in (tools or [])}
    def find_tool(self, tool_id):
        return self._tools.get(tool_id)


def test_diagnose_tool_clean_returns_no_actions():
    tool = FakeTool("t1", "fetch_json")
    eng = RecoveryEngine(vault=FakeVault([tool]))
    assert eng.diagnose_tool("t1") == []


def test_diagnose_tool_dep_pending_returns_blocker():
    tool = FakeTool(
        "t1", "fetch_json",
        dry_run_status="failed",
        dry_run_evidence={"error": "ImportError: No module named 'requests'"},
    )
    eng = RecoveryEngine(vault=FakeVault([tool]))
    actions = eng.diagnose_tool("t1")
    assert len(actions) == 1
    a = actions[0]
    assert a.kind == "DEP_PENDING"
    assert a.severity == "blocker"
    assert "requests" in a.reason
    assert a.fix_url.endswith("/recover/tool/t1")


def test_diagnose_tool_disabled_returns_blocker():
    tool = FakeTool("t1", "fetch_json", enabled=False)
    eng = RecoveryEngine(vault=FakeVault([tool]))
    actions = eng.diagnose_tool("t1")
    assert any(a.kind == "GATE_3_DISABLED" for a in actions)


def test_diagnose_tool_proposed_returns_gate_1_pending():
    tool = FakeTool("t1", "fetch_json", status="proposed")
    eng = RecoveryEngine(vault=FakeVault([tool]))
    actions = eng.diagnose_tool("t1")
    assert any(a.kind == "GATE_1_PENDING" for a in actions)


def test_diagnose_tool_forged_returns_gate_2_pending():
    tool = FakeTool("t1", "fetch_json", status="forged")
    eng = RecoveryEngine(vault=FakeVault([tool]))
    actions = eng.diagnose_tool("t1")
    assert any(a.kind == "GATE_2_PENDING" for a in actions)


def test_diagnose_tool_unknown_id_returns_empty():
    eng = RecoveryEngine(vault=FakeVault([]))
    assert eng.diagnose_tool("missing_id") == []


def test_diagnose_tool_fs_permission():
    tool = FakeTool(
        "t1", "saver", dry_run_status="failed",
        dry_run_evidence={"error": "PermissionError: [Errno 13] Permission denied: '/x'"},
    )
    eng = RecoveryEngine(vault=FakeVault([tool]))
    actions = eng.diagnose_tool("t1")
    assert any(a.kind == "FS_PERMISSION" for a in actions)


class FakeShadow:
    def __init__(self, id, name="shadow", execution_log=None, available_tool_ids=None,
                 skill_ids=None):
        self.id = id
        self.name = name
        self.execution_log = execution_log or []
        self.available_tool_ids = available_tool_ids or []
        self.skill_ids = skill_ids or []


class FakeActivity:
    def __init__(self, id, scroll_id, assigned_shadow_id=None,
                 required_tool_ids=None, required_skill_ids=None):
        self.id = id
        self.scroll_id = scroll_id
        self.assigned_shadow_id = assigned_shadow_id
        self.required_tool_ids = required_tool_ids or []
        self.required_skill_ids = required_skill_ids or []


class FakeScroll:
    def __init__(self, id, status="approved"):
        self.id = id
        self.status = status


class FakeVault2(FakeVault):
    def __init__(self, tools=None, shadows=None, activities=None, scrolls=None,
                 skills=None):
        super().__init__(tools)
        self._shadows = {s.id: s for s in (shadows or [])}
        self._activities_by_scroll = {a.scroll_id: a for a in (activities or [])}
        self._activities = {a.id: a for a in (activities or [])}
        self._scrolls = {s.id: s for s in (scrolls or [])}
        self._skill_ids = set(skills or [])

    def find_shadow(self, sid): return self._shadows.get(sid)
    def find_activity(self, aid): return self._activities.get(aid)
    def find_activity_for_scroll(self, sid): return self._activities_by_scroll.get(sid)
    def find_scroll(self, sid): return self._scrolls.get(sid)
    def skill_exists(self, sid): return sid in self._skill_ids


def test_diagnose_shadow_memory_poisoning():
    log = [{"status": "failed", "tool": "fetch_json", "reason": "not enabled"}] * 5
    shadow = FakeShadow("sh1", execution_log=log)
    eng = RecoveryEngine(vault=FakeVault2(shadows=[shadow]))
    actions = eng.diagnose_shadow("sh1")
    assert any(a.kind == "MEMORY_POISONED" and a.severity == "warning" for a in actions)


def test_diagnose_shadow_propagates_tool_actions():
    tool = FakeTool("t1", "fetch_json", enabled=False)
    shadow = FakeShadow("sh1", available_tool_ids=["t1"])
    eng = RecoveryEngine(vault=FakeVault2(tools=[tool], shadows=[shadow]))
    actions = eng.diagnose_shadow("sh1")
    assert any(a.kind == "GATE_3_DISABLED" and a.scope_id == "t1" for a in actions)


def test_diagnose_shadow_missing_skill():
    shadow = FakeShadow("sh1", skill_ids=["skill_x"])
    eng = RecoveryEngine(vault=FakeVault2(shadows=[shadow], skills=set()))
    actions = eng.diagnose_shadow("sh1")
    assert any(a.kind == "SKILL_MISSING" for a in actions)


def test_diagnose_shadow_unknown_id_returns_empty():
    eng = RecoveryEngine(vault=FakeVault2(shadows=[]))
    assert eng.diagnose_shadow("missing") == []


def test_diagnose_activity_propagates_required_tools_and_shadow():
    tool = FakeTool("t1", "fetch_json", enabled=False)
    shadow = FakeShadow("sh1", available_tool_ids=["t1"])
    activity = FakeActivity("a1", "scr1", assigned_shadow_id="sh1",
                            required_tool_ids=["t1"])
    eng = RecoveryEngine(vault=FakeVault2(tools=[tool], shadows=[shadow], activities=[activity]))
    actions = eng.diagnose_activity("a1")
    kinds = {a.kind for a in actions}
    assert "GATE_3_DISABLED" in kinds


def test_diagnose_activity_missing_skill():
    activity = FakeActivity("a1", "scr1", required_skill_ids=["skill_x"])
    eng = RecoveryEngine(vault=FakeVault2(activities=[activity], skills=set()))
    actions = eng.diagnose_activity("a1")
    assert any(a.kind == "SKILL_MISSING" for a in actions)


def test_diagnose_activity_unknown_id_returns_empty():
    eng = RecoveryEngine(vault=FakeVault2(activities=[]))
    assert eng.diagnose_activity("missing") == []


def test_diagnose_scroll_walks_activity_chain():
    tool = FakeTool("t1", "fetch_json", enabled=False)
    shadow = FakeShadow("sh1", available_tool_ids=["t1"])
    activity = FakeActivity("a1", "scr1", assigned_shadow_id="sh1",
                            required_tool_ids=["t1"])
    scroll = FakeScroll("scr1")
    eng = RecoveryEngine(vault=FakeVault2(tools=[tool], shadows=[shadow],
                                          activities=[activity], scrolls=[scroll]))
    actions = eng.diagnose_scroll("scr1")
    assert any(a.kind == "GATE_3_DISABLED" for a in actions)


def test_diagnose_scroll_no_activity_returns_empty():
    scroll = FakeScroll("scr1")
    eng = RecoveryEngine(vault=FakeVault2(scrolls=[scroll]))
    assert eng.diagnose_scroll("scr1") == []


def test_diagnose_scroll_unknown_id_returns_empty():
    eng = RecoveryEngine(vault=FakeVault2(scrolls=[]))
    assert eng.diagnose_scroll("missing") == []


def test_dedupe_collapses_duplicate_actions_for_same_scope_and_kind():
    """If an activity requires a tool AND the assigned shadow also lists it,
    we should only see one GATE_3_DISABLED action for that tool."""
    tool = FakeTool("t1", "fetch_json", enabled=False)
    shadow = FakeShadow("sh1", available_tool_ids=["t1"])
    activity = FakeActivity("a1", "scr1", assigned_shadow_id="sh1",
                            required_tool_ids=["t1"])
    eng = RecoveryEngine(vault=FakeVault2(tools=[tool], shadows=[shadow], activities=[activity]))
    actions = eng.diagnose_activity("a1")
    g3 = [a for a in actions if a.kind == "GATE_3_DISABLED" and a.scope_id == "t1"]
    assert len(g3) == 1
