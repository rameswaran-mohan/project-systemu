"""W5.1 — the "needs you" accounting includes ALL pending operator decisions.

Reproduces the field bug: a run parked on a stuck-loop ask posts a
``structured_question`` decision, but the header badge, right-rail Needs-you
section, and /inbox Triage all filtered ``context.kind == "gate"`` — so the
whole shell said "nothing needs you" while two runs sat parked. These tests
pin the complete accounting (gates + asks) and the inline-answer wiring.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.vault.vault import Vault
from systemu.approval.decision_queue import OperatorDecisionQueue


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _post_ask(queue, *, dedup="stuck:scroll_x:obj_1:r1") -> str:
    """Seed a stuck-loop structured question (the exact field shape)."""
    return queue.post(
        title="Stuck on Objective 1: 'Search for parking options'",
        body="Answer to continue.",
        options=["Provide hint", "Accept partial", "Cancel run", "Other"],
        context={"kind": "structured_question",
                 "questions": [{"id": "action", "prompt": "Stuck…", "multi": False,
                                "options": [{"label": "Provide hint"}],
                                "allow_free_text": True}],
                 "execution_id": "exec_1", "activity_id": "act_1",
                 "scroll_id": "scroll_x", "shadow_id": "shadow_1"},
        dedup_key=dedup,
    )


def _post_gate(queue) -> str:
    return queue.post(
        title="Enable tool 'write_csv_file'?",
        body="Gate 3 enable.",
        options=["Skip", "Enable & run"],
        context={"kind": "gate", "gate_type": "tools_blocked", "risk": "low"},
        dedup_key="tools_blocked:act_9",
    )


class TestPendingAskRows:
    def test_ask_included_gate_excluded(self, vault):
        from systemu.interface.components.attention import pending_ask_rows
        q = OperatorDecisionQueue(vault)
        _post_ask(q)
        _post_gate(q)
        rows = pending_ask_rows(vault)
        assert len(rows) == 1
        assert rows[0]["kind"] == "structured_question"
        assert "Stuck on Objective 1" in rows[0]["title"]
        assert rows[0]["options"] == ["Provide hint", "Accept partial",
                                      "Cancel run", "Other"]
        # The full decision dict is attached for render_decision_card.
        assert rows[0]["decision"]["id"] == rows[0]["id"]
        assert rows[0]["decision"]["context"]["kind"] == "structured_question"

    def test_resolved_ask_drops_out(self, vault):
        from systemu.interface.components.attention import pending_ask_rows
        q = OperatorDecisionQueue(vault)
        did = _post_ask(q)
        q.resolve(did, choice='{"action": "Accept partial"}')
        assert pending_ask_rows(vault) == []

    def test_defensive_on_broken_vault(self):
        from systemu.interface.components.attention import pending_ask_rows
        assert pending_ask_rows(object()) == []


class TestNeedsYouTotal:
    def test_counts_gates_plus_asks(self, vault):
        from systemu.interface.components.attention import needs_you_total
        q = OperatorDecisionQueue(vault)
        assert needs_you_total(vault) == 0
        _post_ask(q)
        assert needs_you_total(vault) == 1
        _post_gate(q)
        assert needs_you_total(vault) == 2

    def test_header_badge_model_sees_asks(self, vault):
        """THE bug: badge said count=0/hidden with a parked stuck run."""
        from systemu.interface.dashboard import needs_you_badge_model
        q = OperatorDecisionQueue(vault)
        _post_ask(q)
        m = needs_you_badge_model(vault)
        assert m["count"] == 1
        assert m["visible"] is True
        assert m["target"] == "/inbox"


class TestSurfacesWired:
    def test_rail_renders_ask_rows(self):
        from systemu.interface.components import inbox_rail
        src = inspect.getsource(inbox_rail.build_inbox_rail_section)
        assert "pending_ask_rows" in src, \
            "the right-rail Needs-you section must list non-gate asks"
        assert "open_answer_dialog" in src, \
            "ask rows must be answerable inline from the rail"

    def test_inbox_triage_renders_ask_cards(self):
        from systemu.interface.pages import inbox_page
        src = inspect.getsource(inbox_page)
        assert "pending_ask_rows" in src, \
            "/inbox Triage must render non-gate pending decisions"
        assert "render_decision_card" in src, \
            "asks must use the proven render_decision_card answer path"

    def test_history_includes_resolved_asks(self, vault):
        """The decisions INDEX is slim (no context) — history must hydrate via
        get_decision, and must keep resolved asks alongside resolved gates."""
        from systemu.interface.pages.inbox_page import _resolved_inbox_rows
        q = OperatorDecisionQueue(vault)
        did_ask = _post_ask(q)
        did_gate = _post_gate(q)
        q.resolve(did_ask, choice='{"action": "Cancel run"}')
        q.resolve(did_gate, choice="Enable & run")
        rows = vault.load_index("decisions")
        kept = _resolved_inbox_rows(rows, vault.get_decision)
        kinds = {(r.get("context") or {}).get("kind") for r in kept}
        assert kinds == {"structured_question", "gate"}
        # Hydration also recovers the operator's choice for the history card.
        choices = {r.get("choice") for r in kept}
        assert "Enable & run" in choices


class TestAnswerDialogContract:
    def test_question_status_token_tints_warn(self):
        # The ask pill must read as needs-attention (warn), not muted.
        from systemu.interface.design.tokens import TOKENS
        assert TOKENS["status"].get("question") == "warn"
