"""v0.6.1-a — Tool.name validator rejects unsafe values before any disk write.

Closes review issue #1.  The LLM-supplied tool name was previously written
to disk via ``impl_dir / f"{tool.name}.py"`` BEFORE any validation, so a
prompt-injected `"../etc/passwd"` could escape the implementations directory.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from systemu.core.models import Tool, ToolType


def _make(name: str) -> Tool:
    return Tool(id="t_x", name=name, description="d",
                tool_type=ToolType.PYTHON_FUNCTION)


class TestToolNameValidator:
    @pytest.mark.parametrize("bad", [
        "../etc/passwd",
        "..\\windows\\foo",
        "/abs/path",
        "foo/bar",
        "foo\\bar",
        "Foo",
        "FOO",
        "foo bar",
        "1starts_with_digit",
        "",
        " ",
        "x" * 65,
        "name.with.dot",
        "name-with-dash",
        "..",
        ".",
    ])
    def test_rejects_unsafe(self, bad):
        with pytest.raises(ValidationError):
            _make(bad)

    @pytest.mark.parametrize("good", [
        "fetch_json",
        "create_word_doc",
        "web_screenshot",
        "a",
        "a_b_c",
        "x9",
        "snake_case_64chars_" + "x" * 45,    # exactly 64 chars
    ])
    def test_accepts_safe(self, good):
        t = _make(good)
        assert t.name == good


class TestToolForgeBackstop:
    """Defense-in-depth: even if a non-Pydantic path supplies a tool, the
    backstop in tool_forge rejects unsafe names before any disk write."""

    def test_save_approved_code_rejects_unsafe_name(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from systemu.pipelines import tool_forge

        # Construct a Tool but mutate name AFTER construction to bypass Pydantic.
        # This simulates any future code path that constructs Tool without re-
        # validating after mutation.
        tool = Tool(id="t_x", name="fetch_json", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        object.__setattr__(tool, "name", "../etc/evil")

        cfg = MagicMock()
        cfg.vault_dir = str(tmp_path)
        vault = MagicMock()

        with pytest.raises(ValueError, match="unsafe"):
            tool_forge.save_approved_code(tool, "print('hi')\n", cfg, vault)

        # Confirm no file was written to the vault tools dir
        impl_dir = tmp_path / "tools" / "implementations"
        assert not impl_dir.exists() or not any(impl_dir.iterdir())
