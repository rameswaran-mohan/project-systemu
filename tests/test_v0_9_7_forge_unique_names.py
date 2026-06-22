"""v0.9.7 Phase 0b Task B3 — name-collision guard for tool forge / upsert.

Three scenarios tested:
  1. Proposing a tool whose name matches a v2-registered tool is rejected in
     both the activity_extractor._upsert_tool path AND the
     tool_forge.propose_tools_from_specs path — no new vault artefact with a
     fresh id is created, and a warning is logged.
  2. Proposing a tool whose name matches an existing deployed vault tool reuses
     that tool's id (no duplicate vault record).
  3. A genuinely novel name still creates a new record (no false positives).
"""
from __future__ import annotations

import logging
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault


# ── Fixture helpers ────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_vault(tmp_path: Path) -> Vault:
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications", "executions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _make_spec(name: str, *, is_new: bool = True, existing_id: str = "") -> dict:
    """Build a minimal tool spec dict as the LLM / extractor would emit it."""
    return {
        "name": name,
        "description": f"A proposed tool named {name}",
        "tool_type": "python_function",
        "parameters_schema": {},
        "return_schema": {},
        "implementation_notes": "",
        "dependencies": [],
        "is_new": is_new,
        "existing_id": existing_id,
    }


def _make_proposed_tool_spec(name: str):
    """Build a ProposedToolSpec-like object (as emitted by the scroll validator)."""
    spec = MagicMock()
    spec.name = name
    spec.description = f"Validator-proposed tool: {name}"
    spec.tool_type = "python_function"
    spec.parameter_hints = []
    spec.output_hint = ""
    spec.rationale = ""
    return spec


# ── Tests: activity_extractor._upsert_tool ────────────────────────────────────

class TestUpsertToolV2CollisionGuard:
    """Tests for the v2-registry name-collision guard in _upsert_tool."""

    def test_v2_name_returns_empty_sentinel_and_no_vault_record(
        self, tmp_vault: Vault, caplog
    ):
        """A spec whose name matches a v2-registered tool must not create a
        new vault artefact — _upsert_tool should return ("", False)."""
        from systemu.pipelines.activity_extractor import _upsert_tool

        # Patch _is_v2_registered_name to say "write_file" is v2-registered.
        with patch(
            "systemu.pipelines.activity_extractor._is_v2_registered_name",
            return_value=True,
        ):
            with caplog.at_level(logging.WARNING, logger="systemu.pipelines.activity_extractor"):
                tid, is_new = _upsert_tool(_make_spec("write_file"), tmp_vault)

        assert tid == "", "Should return empty sentinel for v2-collision"
        assert is_new is False, "Collision should not be treated as a new tool"

        # Confirm no vault record was created with this name.
        assert tmp_vault.find_tool_by_name("write_file") is None, (
            "No vault artefact should be created for a v2-registered name"
        )

        # A warning must be logged.
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("write_file" in m for m in warning_msgs), (
            f"Expected warning mentioning 'write_file'; got: {warning_msgs}"
        )

    def test_v2_name_is_checked_via_is_v2_registered_name(
        self, tmp_vault: Vault
    ):
        """_upsert_tool must call _is_v2_registered_name (not bypass it) when
        no vault name-match exists."""
        from systemu.pipelines import activity_extractor

        call_log: list[str] = []

        def fake_check(name: str) -> bool:
            call_log.append(name)
            return False  # Not v2-registered → should proceed to create vault tool.

        with patch.object(activity_extractor, "_is_v2_registered_name", fake_check):
            tid, is_new = activity_extractor._upsert_tool(
                _make_spec("completely_novel_tool_xyz"), tmp_vault
            )

        assert "completely_novel_tool_xyz" in call_log, (
            "_is_v2_registered_name must be called for unknown names"
        )
        # A novel name should create a new vault record.
        assert tid != ""
        assert is_new is True

    def test_novel_name_creates_vault_record(self, tmp_vault: Vault):
        """A genuinely novel name (not in vault, not v2-registered) must create
        a new PROPOSED tool record — no false positives."""
        from systemu.pipelines.activity_extractor import _upsert_tool

        with patch(
            "systemu.pipelines.activity_extractor._is_v2_registered_name",
            return_value=False,
        ):
            tid, is_new = _upsert_tool(_make_spec("brand_new_tool_abc"), tmp_vault)

        assert tid != ""
        assert is_new is True
        created = tmp_vault.find_tool_by_name("brand_new_tool_abc")
        assert created is not None, "A new vault tool must be created for a novel name"
        assert created.status == ToolStatus.PROPOSED


class TestUpsertToolVaultDedup:
    """Existing vault-dedup behaviour must be preserved (no regression)."""

    def test_existing_vault_tool_reuses_id(self, tmp_vault: Vault):
        """A spec whose name matches an existing vault tool reuses that tool's
        id — no duplicate record is created."""
        from systemu.pipelines.activity_extractor import _upsert_tool

        existing = Tool(
            id=generate_id("tool"),
            name="my_existing_tool",
            description="Already in vault",
            tool_type=ToolType.PYTHON_FUNCTION,
            status=ToolStatus.FORGED,
            enabled=True,
        )
        tmp_vault.save_tool(existing)

        with patch(
            "systemu.pipelines.activity_extractor._is_v2_registered_name",
            return_value=False,
        ):
            tid, is_new = _upsert_tool(_make_spec("my_existing_tool"), tmp_vault)

        assert tid == existing.id, "Must reuse the existing vault tool's id"
        assert is_new is False

        # Ensure only one tool with this name exists.
        all_tools = tmp_vault.load_index("tools")
        matching = [t for t in all_tools if t.get("name") == "my_existing_tool"]
        assert len(matching) == 1, f"Expected exactly 1 tool, got {len(matching)}"


# ── Tests: tool_forge.propose_tools_from_specs ────────────────────────────────

class TestProposeToolsFromSpecsV2Guard:
    """V2-collision guard in propose_tools_from_specs (tool_forge.py)."""

    @pytest.fixture
    def mock_config(self, tmp_path: Path):
        cfg = MagicMock()
        cfg.vault_dir = str(tmp_path)
        cfg.tier2_model = "test-model"
        return cfg

    @pytest.fixture
    def stub_scroll(self):
        scroll = MagicMock()
        scroll.narrative_md = "Test scroll narrative"
        scroll.name = "test_scroll"
        return scroll

    def test_v2_name_is_skipped_no_vault_record(
        self, tmp_vault: Vault, mock_config, stub_scroll, caplog
    ):
        """propose_tools_from_specs must not create a vault record when the
        proposed name matches a v2-registered tool."""
        from systemu.pipelines import tool_forge

        # Patch the registry so 'write_file' appears v2-registered.
        mock_entry = MagicMock()
        mock_entry.name = "write_file"
        mock_registry = MagicMock()
        mock_registry.list.return_value = [mock_entry]
        mock_registry.discover_modules.return_value = []

        with patch.dict("sys.modules", {"systemu.runtime.tool_registry_v2": MagicMock(
            registry=mock_registry,
        )}):
            # Re-import to pick up the patched module within the function.
            import importlib
            importlib.reload(tool_forge)
            from systemu.pipelines.tool_forge import propose_tools_from_specs

            spec = _make_proposed_tool_spec("write_file")

            with caplog.at_level(logging.WARNING, logger="systemu.pipelines.tool_forge"):
                result = propose_tools_from_specs(
                    [spec], stub_scroll, mock_config, tmp_vault
                )

        assert result == [], "No tool should be proposed when name collides with v2"
        assert tmp_vault.find_tool_by_name("write_file") is None

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("write_file" in m for m in warning_msgs), (
            f"Expected collision warning; got: {warning_msgs}"
        )

    def test_novel_name_proceeds_through_llm(
        self, tmp_vault: Vault, mock_config, stub_scroll
    ):
        """A genuinely novel name (not in vault, not v2) calls the LLM spec
        step and saves the proposed tool."""
        from systemu.pipelines import tool_forge

        mock_entry = MagicMock()
        mock_entry.name = "some_other_v2_tool"
        mock_registry = MagicMock()
        mock_registry.list.return_value = [mock_entry]
        mock_registry.discover_modules.return_value = []

        fake_spec_result = {
            "name": "truly_novel_tool_xyz",
            "description": "A novel tool",
            "tool_type": "python_function",
            "parameters_schema": {},
            "return_schema": {},
            "implementation_notes": "",
            "dependencies": [],
            "requires_credentials": [],
        }

        with patch.dict("sys.modules", {"systemu.runtime.tool_registry_v2": MagicMock(
            registry=mock_registry,
        )}):
            import importlib
            importlib.reload(tool_forge)
            from systemu.pipelines.tool_forge import propose_tools_from_specs

            spec = _make_proposed_tool_spec("truly_novel_tool_xyz")

            with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=fake_spec_result):
                result = propose_tools_from_specs(
                    [spec], stub_scroll, mock_config, tmp_vault
                )

        assert len(result) == 1, "Novel name should produce one proposed tool"
        assert result[0].name == "truly_novel_tool_xyz"
        assert result[0].status == ToolStatus.PROPOSED

    def test_existing_vault_name_is_still_skipped(
        self, tmp_vault: Vault, mock_config, stub_scroll, caplog
    ):
        """Existing vault dedup (pre-v0.9.7 behaviour) must still work — a
        vault-resident name is skipped before even reaching the v2 check."""
        existing = Tool(
            id=generate_id("tool"),
            name="vault_resident_tool",
            description="Pre-existing",
            tool_type=ToolType.PYTHON_FUNCTION,
            status=ToolStatus.FORGED,
            enabled=True,
        )
        tmp_vault.save_tool(existing)

        mock_registry = MagicMock()
        mock_registry.list.return_value = []
        mock_registry.discover_modules.return_value = []

        with patch.dict("sys.modules", {"systemu.runtime.tool_registry_v2": MagicMock(
            registry=mock_registry,
        )}):
            import importlib
            from systemu.pipelines import tool_forge as _tf
            importlib.reload(_tf)
            from systemu.pipelines.tool_forge import propose_tools_from_specs

            spec = _make_proposed_tool_spec("vault_resident_tool")
            with caplog.at_level(logging.INFO, logger="systemu.pipelines.tool_forge"):
                result = propose_tools_from_specs(
                    [spec], stub_scroll, mock_config, tmp_vault
                )

        assert result == [], "Vault-resident name must be skipped"
        # Exactly one tool with that name must exist (no duplicate).
        all_tools = tmp_vault.load_index("tools")
        matching = [t for t in all_tools if t.get("name") == "vault_resident_tool"]
        assert len(matching) == 1


# ── Integration-style test for _is_v2_registered_name helper ─────────────────

class TestIsV2RegisteredName:
    """Unit tests for the _is_v2_registered_name helper itself."""

    def test_returns_false_when_registry_is_empty(self):
        from systemu.pipelines import activity_extractor

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        mock_registry.discover_modules.return_value = []

        with patch.dict("sys.modules", {"systemu.runtime.tool_registry_v2": MagicMock(
            registry=mock_registry,
        )}):
            import importlib
            importlib.reload(activity_extractor)
            result = activity_extractor._is_v2_registered_name("some_tool")

        assert result is False

    def test_returns_true_when_name_is_registered(self):
        from systemu.pipelines import activity_extractor

        mock_entry = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry  # Found!
        mock_registry.discover_modules.return_value = []

        with patch.dict("sys.modules", {"systemu.runtime.tool_registry_v2": MagicMock(
            registry=mock_registry,
        )}):
            import importlib
            importlib.reload(activity_extractor)
            result = activity_extractor._is_v2_registered_name("write_file")

        assert result is True

    def test_fail_quiet_on_exception(self):
        """An exception inside the v2 registry check must not propagate —
        _is_v2_registered_name should return False gracefully.

        We test this by temporarily replacing the registry singleton on the
        real module with one whose discover_modules() raises, then calling
        _is_v2_registered_name directly.
        """
        import sys

        # Ensure the real module is loaded before we touch it.
        import systemu.runtime.tool_registry_v2 as v2_mod
        from systemu.pipelines import activity_extractor

        mock_registry = MagicMock()
        mock_registry.discover_modules.side_effect = RuntimeError("injected error")

        orig_registry = v2_mod.registry
        v2_mod.registry = mock_registry
        try:
            result = activity_extractor._is_v2_registered_name("write_file")
        finally:
            v2_mod.registry = orig_registry

        assert result is False, (
            "_is_v2_registered_name must return False on exception, not raise"
        )
