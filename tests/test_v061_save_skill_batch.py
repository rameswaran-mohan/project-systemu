"""— ``save_skill`` resolves ``required_tool_names`` in a single
batch lookup, not N separate ``find_tool_by_name`` calls.

Closes review issue #5.  The previous implementation opened one SQLAlchemy
session per tool name; for a 6-tool skill that meant 6 round trips per save.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def json_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder", "notifications"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    (tmp_path / "global_memory.jsonl").write_text("")
    (tmp_path / "chat_history.jsonl").write_text("")
    return Vault(str(tmp_path))


@pytest.fixture
def sqlite_vault(tmp_path):
    from systemu.storage.sqlite.vault import SqliteVault
    from sqlalchemy import create_engine
    from systemu.storage.sqlite.models import Base
    url = f"sqlite:///{tmp_path / 'systemu.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    engine.dispose()
    return SqliteVault(url, memory_dir=tmp_path / "memory")


def _seed_tools(vault, names):
    from systemu.core.models import Tool, ToolType
    for n in names:
        vault.save_tool(Tool(
            id=f"t_{n}", name=n, description="d",
            tool_type=ToolType.PYTHON_FUNCTION,
        ))


def _make_skill():
    from systemu.core.models import Skill
    return Skill(
        id="s1", name="x", description="d",
        target_outcomes=["a"], produces=["data"],
        required_tool_names=["tool_a", "tool_b", "tool_c",
                             "tool_d", "tool_e", "tool_f"],
    )


class TestJsonVaultBatch:
    def test_does_not_call_find_tool_by_name_per_tool(self, json_vault):
        _seed_tools(json_vault, ["tool_a", "tool_b", "tool_c",
                                 "tool_d", "tool_e", "tool_f"])
        skill = _make_skill()

        with patch.object(json_vault, "find_tool_by_name",
                          side_effect=AssertionError("must not be called")):
            json_vault.save_skill(skill)

        reloaded = json_vault.get_skill("s1")
        assert sorted(reloaded.required_tool_ids) == sorted([
            f"t_{n}" for n in ["tool_a", "tool_b", "tool_c",
                               "tool_d", "tool_e", "tool_f"]
        ])

    def test_handles_unknown_tool_names_gracefully(self, json_vault):
        _seed_tools(json_vault, ["tool_a", "tool_b"])
        from systemu.core.models import Skill
        skill = Skill(
            id="s2", name="x", description="d",
            target_outcomes=["a"], produces=["data"],
            required_tool_names=["tool_a", "missing", "tool_b"],
        )

        with patch.object(json_vault, "find_tool_by_name",
                          side_effect=AssertionError("must not be called")):
            json_vault.save_skill(skill)

        reloaded = json_vault.get_skill("s2")
        # Unknown name is silently dropped (no exception raised)
        assert sorted(reloaded.required_tool_ids) == ["t_tool_a", "t_tool_b"]


class TestSqliteVaultBatch:
    def test_does_not_call_find_tool_by_name_per_tool(self, sqlite_vault):
        _seed_tools(sqlite_vault, ["tool_a", "tool_b", "tool_c",
                                   "tool_d", "tool_e", "tool_f"])
        skill = _make_skill()

        with patch.object(sqlite_vault, "find_tool_by_name",
                          side_effect=AssertionError("must not be called")):
            sqlite_vault.save_skill(skill)

        reloaded = sqlite_vault.get_skill("s1")
        assert sorted(reloaded.required_tool_ids) == sorted([
            f"t_{n}" for n in ["tool_a", "tool_b", "tool_c",
                               "tool_d", "tool_e", "tool_f"]
        ])
