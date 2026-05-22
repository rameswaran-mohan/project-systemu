"""Tests for scroll pipeline — scroll_refiner and activity_extractor with mocked LLM."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from systemu.core.models import (
    Objective, Scroll, ScrollStatus,
    Activity, ActivityStatus,
    Tool, ToolStatus, ToolType,
    Skill,
)
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "notifications" / "pending.json").write_text("[]", encoding="utf-8")
    (tmp_path / "notifications" / "event_log.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    cfg.non_interactive = False
    cfg.auto_forge_tools = False
    cfg.tier1_model = "test-model"
    cfg.tier2_model = "test-model"
    cfg.tier3_model = "test-model"
    cfg.openrouter_api_key = "test-key"
    return cfg


@pytest.fixture
def capture_session_dir(tmp_path):
    session_dir = tmp_path / "captures" / "test_session"
    session_dir.mkdir(parents=True)
    (session_dir / "instructions.md").write_text(
        "The user opened Chrome, went to finance.yahoo.com, looked at NYSE index, "
        "then opened Word and saved a file called 05042026 NYSE.docx",
        encoding="utf-8",
    )
    (session_dir / "session.json").write_text(
        json.dumps({"session_id": "sess_test_001", "name": "NYSE Capture Test"}),
        encoding="utf-8",
    )
    return session_dir


# ─── LLM response mocks ───────────────────────────────────────────────────────

MOCK_REFINE_RESPONSE = {
    "title": "Capture NYSE Index to Dated Word Document",
    "intent": "Capture current NYSE index data and save it to a dated Word document.",
    "narrative_md": "The user wants to record the NYSE index for today's date in a Word document.",
    "objectives": [
        {
            "id": 1,
            "goal": "Obtain a visual capture of the current NYSE index",
            "success_criteria": "Have an image showing today's NYSE index",
            "output_type": "data",
            "hints": {"source_url": "https://finance.yahoo.com/quote/%5ENYA"},
            "depends_on": [],
        },
        {
            "id": 2,
            "goal": "Create a Word document containing the NYSE index capture",
            "success_criteria": "~/Documents/05042026 NYSE.docx exists with NYSE image",
            "output_type": "file",
            "hints": {"output_path": "~/Documents/", "naming_pattern": "MMDDYYYY NYSE.docx"},
            "depends_on": [1],
        },
    ],
    "constraints": {"output_location": "~/Documents/", "naming_convention": "MMDDYYYY NYSE.docx"},
    "observed_preferences": {"date_format": "MMDDYYYY", "tools_used": ["Chrome", "Word"]},
    "tags": ["finance", "reporting", "word-doc"],
}

MOCK_EXTRACT_RESPONSE = {
    "skills": [
        {
            "name": "web_data_capture",
            "description": "Capture web data programmatically",
            "category": "browser",
            "proficiency_level": "intermediate",
            "required_tools": ["web_screenshot"],
            "instructions_md": "Use web_screenshot to render the target page.",
            "is_new": True,
            "existing_id": None,
        }
    ],
    "tools": [
        {
            "name": "web_screenshot",
            "description": "Screenshot a rendered URL",
            "tool_type": "browser_action",
            "parameters_schema": {"url": {"type": "string"}},
            "return_schema": {"success": {"type": "boolean"}, "image_path": {"type": "string"}, "error": {"type": "string"}},
            "implementation_notes": "Use playwright sync API.",
            "dependencies": ["playwright"],
            "is_new": True,
            "existing_id": None,
        },
        {
            "name": "create_word_doc",
            "description": "Create a Word document",
            "tool_type": "file_operation",
            "parameters_schema": {"output_path": {"type": "string"}},
            "return_schema": {"success": {"type": "boolean"}, "output_path": {"type": "string"}, "error": {"type": "string"}},
            "implementation_notes": "Use python-docx.",
            "dependencies": ["python-docx"],
            "is_new": True,
            "existing_id": None,
        },
    ],
}


# ─── scroll_refiner tests ─────────────────────────────────────────────────────

class TestScrollRefiner:
    def test_refine_produces_objectives(self, capture_session_dir, mock_config, tmp_vault):
        with patch("systemu.pipelines.scroll_refiner.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.scroll_refiner.notify_user") as mock_notify:
            mock_llm.return_value = MOCK_REFINE_RESPONSE
            mock_notify.return_value = "Reject"  # Prevent Stage 3 cascade

            from systemu.pipelines.scroll_refiner import refine_scroll
            scroll = refine_scroll(capture_session_dir, mock_config, tmp_vault)

        assert isinstance(scroll, Scroll)
        assert len(scroll.objectives) == 2
        assert scroll.intent == MOCK_REFINE_RESPONSE["intent"]
        assert scroll.objectives[0].goal == "Obtain a visual capture of the current NYSE index"
        assert scroll.objectives[1].depends_on == [1]
        assert scroll.action_blocks == []  # intent-driven: no GUI steps

    def test_refine_sets_pending_approval_status(self, capture_session_dir, mock_config, tmp_vault):
        with patch("systemu.pipelines.scroll_refiner.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.scroll_refiner.notify_user") as mock_notify:
            mock_llm.return_value = MOCK_REFINE_RESPONSE
            mock_notify.return_value = "Reject"

            from systemu.pipelines.scroll_refiner import refine_scroll
            scroll = refine_scroll(capture_session_dir, mock_config, tmp_vault)

        assert scroll.status == ScrollStatus.PENDING_APPROVAL

    def test_refine_persists_to_vault(self, capture_session_dir, mock_config, tmp_vault):
        with patch("systemu.pipelines.scroll_refiner.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.scroll_refiner.notify_user") as mock_notify:
            mock_llm.return_value = MOCK_REFINE_RESPONSE
            mock_notify.return_value = "Reject"

            from systemu.pipelines.scroll_refiner import refine_scroll
            scroll = refine_scroll(capture_session_dir, mock_config, tmp_vault)

        loaded = tmp_vault.get_scroll(scroll.id)
        assert loaded.id == scroll.id
        assert len(loaded.objectives) == 2

    def test_refine_constraints_preserved(self, capture_session_dir, mock_config, tmp_vault):
        with patch("systemu.pipelines.scroll_refiner.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.scroll_refiner.notify_user") as mock_notify:
            mock_llm.return_value = MOCK_REFINE_RESPONSE
            mock_notify.return_value = "Reject"

            from systemu.pipelines.scroll_refiner import refine_scroll
            scroll = refine_scroll(capture_session_dir, mock_config, tmp_vault)

        assert scroll.constraints["output_location"] == "~/Documents/"
        assert scroll.observed_preferences["date_format"] == "MMDDYYYY"

    def test_refine_missing_instructions_raises(self, tmp_path, mock_config, tmp_vault):
        bad_dir = tmp_path / "bad_session"
        bad_dir.mkdir()
        from systemu.pipelines.scroll_refiner import refine_scroll
        with pytest.raises(FileNotFoundError):
            refine_scroll(bad_dir, mock_config, tmp_vault)

    # ── Regression: session-id dedup must not crash on index dicts ──────
    #
    # Repeatedly bitten by the same bug class — `vault.list_*()` returns
    # index DICTS, not Pydantic instances.  Code that walks the result and
    # treats entries as Pydantic (`s.source_session_id`) raises
    # AttributeError.  v0.2.2 fixed the migration tool; v0.3.1 fixed the
    # SqliteVault seed-on-empty path; v0.3.2 (these tests) fix the
    # scroll_refiner dedup path AND add coverage so instance #4 can't ship.

    def test_dedup_no_match_on_fresh_session_does_not_raise(
        self, capture_session_dir, mock_config, tmp_vault,
    ):
        """Cold path — no existing scroll for this session.  Must walk the
        dict-based index without crashing on AttributeError."""
        # Seed the vault with an UNRELATED scroll to ensure the dedup loop
        # iterates at least one entry (the original bug only surfaced when
        # the for-loop actually ran a body, not when the list was empty).
        from systemu.core.models import Scroll, Objective
        unrelated = Scroll(
            id=generate_id("scroll"),
            name="Unrelated existing scroll",
            source_session_id="sess_OTHER_unrelated",
            raw_instructions_path="",
            narrative_md="",
            intent="Pre-existing",
            objectives=[Objective(id=1, goal="anything", success_criteria="ok")],
            action_blocks=[],
            constraints={},
            observed_preferences={},
            status=ScrollStatus.ACTIVE,
        )
        tmp_vault.save_scroll(unrelated)

        with patch("systemu.pipelines.scroll_refiner.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.scroll_refiner.notify_user") as mock_notify:
            mock_llm.return_value = MOCK_REFINE_RESPONSE
            mock_notify.return_value = "Reject"

            from systemu.pipelines.scroll_refiner import refine_scroll
            # Should NOT raise AttributeError — the dedup loop walks the
            # unrelated index dict, finds no match, falls through to the
            # normal refine path.
            new_scroll = refine_scroll(capture_session_dir, mock_config, tmp_vault)

        assert new_scroll.id != unrelated.id
        assert new_scroll.source_session_id == "sess_test_001"

    def test_dedup_returns_existing_when_session_already_refined(
        self, capture_session_dir, mock_config, tmp_vault,
    ):
        """Hot path — a scroll for this session already exists.  Dedup must
        find the match (via dict access) and return the hydrated Scroll
        without re-calling the LLM."""
        from systemu.core.models import Scroll, Objective
        # Seed the vault with an existing scroll for the SAME session_id
        # as capture_session_dir uses ("sess_test_001").
        existing = Scroll(
            id=generate_id("scroll"),
            name="Already-refined NYSE Capture",
            source_session_id="sess_test_001",
            raw_instructions_path="",
            narrative_md="",
            intent="Already refined earlier",
            objectives=[Objective(id=1, goal="x", success_criteria="ok")],
            action_blocks=[],
            constraints={},
            observed_preferences={},
            status=ScrollStatus.ACTIVE,
        )
        tmp_vault.save_scroll(existing)

        with patch("systemu.pipelines.scroll_refiner.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.scroll_refiner.notify_user") as mock_notify:
            mock_llm.return_value = MOCK_REFINE_RESPONSE  # should not be called
            mock_notify.return_value = "Reject"

            from systemu.pipelines.scroll_refiner import refine_scroll
            returned = refine_scroll(capture_session_dir, mock_config, tmp_vault)

        # Same scroll returned — LLM was not invoked.
        assert returned.id == existing.id
        assert returned.name == "Already-refined NYSE Capture"
        mock_llm.assert_not_called()

    def test_dedup_ignores_draft_status(
        self, capture_session_dir, mock_config, tmp_vault,
    ):
        """Edge case — a DRAFT scroll for this session means the prior
        refine was incomplete.  Dedup should ignore it and produce a fresh
        Scroll, not return the half-done draft."""
        from systemu.core.models import Scroll, Objective
        draft = Scroll(
            id=generate_id("scroll"),
            name="Half-finished draft",
            source_session_id="sess_test_001",
            raw_instructions_path="",
            narrative_md="",
            intent="incomplete",
            objectives=[Objective(id=1, goal="x", success_criteria="ok")],
            action_blocks=[],
            constraints={},
            observed_preferences={},
            status=ScrollStatus.DRAFT,
        )
        tmp_vault.save_scroll(draft)

        with patch("systemu.pipelines.scroll_refiner.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.scroll_refiner.notify_user") as mock_notify:
            mock_llm.return_value = MOCK_REFINE_RESPONSE
            mock_notify.return_value = "Reject"

            from systemu.pipelines.scroll_refiner import refine_scroll
            returned = refine_scroll(capture_session_dir, mock_config, tmp_vault)

        # New scroll produced; the draft was ignored.
        assert returned.id != draft.id
        mock_llm.assert_called_once()


# ─── activity_extractor tests ─────────────────────────────────────────────────

class TestActivityExtractor:
    def _make_approved_scroll(self, tmp_vault):
        """Create an objectives-based scroll already approved."""
        scroll = Scroll(
            id=generate_id("scroll"),
            name="NYSE Report",
            source_session_id="sess_001",
            raw_instructions_path="/tmp/x.md",
            narrative_md="Capture NYSE and save to Word.",
            intent="Capture NYSE index and create dated Word document.",
            objectives=[
                Objective(id=1, goal="Capture NYSE", success_criteria="Image captured"),
                Objective(id=2, goal="Create Word doc", success_criteria="Doc saved", depends_on=[1]),
            ],
            status=ScrollStatus.APPROVED,
        )
        tmp_vault.save_scroll(scroll)
        return scroll

    def test_extract_creates_activity(self, mock_config, tmp_vault):
        scroll = self._make_approved_scroll(tmp_vault)
        with patch("systemu.pipelines.activity_extractor.llm_call_json") as mock_llm, \
             patch("systemu.pipelines.activity_extractor._resolve_deps") as mock_deps, \
             patch("systemu.pipelines.shadow_decision.decide_shadow"):
            mock_deps.return_value = (mock_config, tmp_vault)
            mock_llm.return_value = MOCK_EXTRACT_RESPONSE

            from systemu.pipelines.activity_extractor import extract_and_process
            activity = extract_and_process(scroll, config=mock_config, vault=tmp_vault)

        assert activity is not None
        assert isinstance(activity, Activity)
        assert len(activity.required_tool_ids) == 2
        assert len(activity.required_skill_ids) == 1

    def test_extract_feeds_objectives_to_llm(self, mock_config, tmp_vault):
        scroll = self._make_approved_scroll(tmp_vault)
        captured_payload = {}

        def capture_call(**kwargs):
            captured_payload.update(json.loads(kwargs.get("user", "{}")))
            return MOCK_EXTRACT_RESPONSE

        with patch("systemu.pipelines.activity_extractor.llm_call_json", side_effect=capture_call), \
             patch("systemu.pipelines.activity_extractor._resolve_deps") as mock_deps, \
             patch("systemu.pipelines.shadow_decision.decide_shadow"):
            mock_deps.return_value = (mock_config, tmp_vault)
            from systemu.pipelines.activity_extractor import extract_and_process
            extract_and_process(scroll, config=mock_config, vault=tmp_vault)

        # Should use objectives, not action_blocks
        assert "objectives" in captured_payload
        assert "action_blocks" not in captured_payload
        assert "intent" in captured_payload
        assert len(captured_payload["objectives"]) == 2

    def test_extract_legacy_scroll_uses_action_blocks(self, mock_config, tmp_vault):
        # Legacy scroll: has action_blocks, no objectives
        scroll = Scroll(
            id=generate_id("scroll"),
            name="Legacy",
            source_session_id="sess_002",
            raw_instructions_path="/tmp/x.md",
            narrative_md="Old style scroll.",
            action_blocks=[
                {"step_number": 1, "action": "navigate", "target": "https://example.com",
                 "parameters": {}, "expected_outcome": "loaded", "application": "Chrome"}
            ],
            status=ScrollStatus.APPROVED,
        )
        tmp_vault.save_scroll(scroll)
        captured_payload = {}

        def capture_call(**kwargs):
            captured_payload.update(json.loads(kwargs.get("user", "{}")))
            return MOCK_EXTRACT_RESPONSE

        with patch("systemu.pipelines.activity_extractor.llm_call_json", side_effect=capture_call), \
             patch("systemu.pipelines.activity_extractor._resolve_deps") as mock_deps, \
             patch("systemu.pipelines.shadow_decision.decide_shadow"):
            mock_deps.return_value = (mock_config, tmp_vault)
            from systemu.pipelines.activity_extractor import extract_and_process
            extract_and_process(scroll, config=mock_config, vault=tmp_vault)

        assert "action_blocks" in captured_payload
        assert "objectives" not in captured_payload


# ─── context_builder tests ────────────────────────────────────────────────────

class TestContextBuilder:
    def _make_context(self, use_objectives=True):
        from systemu.runtime.context_builder import ExecutionContext
        if use_objectives:
            scroll_json = [
                {"id": 1, "goal": "Step one", "success_criteria": "done"},
                {"id": 2, "goal": "Step two", "success_criteria": "also done", "depends_on": [1]},
            ]
        else:
            scroll_json = [
                {"step_number": 1, "action": "navigate", "target": "https://example.com"},
            ]
        return ExecutionContext(
            execution_id="exec_test",
            system_prompt="You are a test agent.",
            scroll_json=scroll_json,
            tool_index=[{"id": "tool_001", "name": "web_screenshot", "description": "screenshot"}],
            skill_index=[],
            use_objectives=use_objectives,
            scroll_intent="Test the intent system." if use_objectives else "",
        )

    def test_objectives_mode_messages(self):
        ctx = self._make_context(use_objectives=True)
        messages = ctx.build_messages(current_action_block=0, completed_objectives=set())
        # Should have system + task (objectives) + decision
        assert any("Pending Objectives" in m["content"] for m in messages)
        assert any("Intent" in m["content"] for m in messages)

    def test_legacy_mode_messages(self):
        ctx = self._make_context(use_objectives=False)
        messages = ctx.build_messages(current_action_block=1)
        assert any("ActionBlocks" in m["content"] or "Remaining" in m["content"] for m in messages)

    def test_completed_objectives_excluded(self):
        ctx = self._make_context(use_objectives=True)
        messages = ctx.build_messages(current_action_block=0, completed_objectives={1})
        task_msg = next(m for m in messages if "Pending Objectives" in m.get("content", ""))
        # Objective 1 should be gone from pending
        assert '"id": 1' not in task_msg["content"]
        assert '"id": 2' in task_msg["content"]

    def test_build_result(self):
        ctx = self._make_context()
        result = ctx.build_result(status="success", final_summary="All done.")
        assert result["status"] == "success"
        assert result["summary"] == "All done."
        assert "execution_id" in result
        assert "timestamp" in result

    def test_add_and_retrieve_tool_call(self):
        ctx = self._make_context()
        ctx.add_tool_call({"action": "TOOL_CALL", "tool_name": "web_screenshot"}, 0)
        history = ctx.get_full_history()
        assert len(history) == 1
        assert history[0]["event_type"] == "tool_call"

    def test_add_thought(self):
        ctx = self._make_context()
        ctx.add_thought("I need to think about this.", 0)
        history = ctx.get_full_history()
        assert history[0]["event_type"] == "thought"
        assert "think" in history[0]["content"]["thought"].lower()
