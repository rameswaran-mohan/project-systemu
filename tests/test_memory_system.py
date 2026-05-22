import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from systemu.core.models import Shadow, ShadowStatus
from systemu.vault.vault import Vault

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions", "elder"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "elder" / "memory_buffer.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))

@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    return cfg

def test_parse_elder_memory():
    from systemu.elder.memory import parse_elder_memory
    
    md_text = """---
frontmatter: here
---

# Elder Memory

## Workflow Patterns
- Pattern 1
- Pattern 2

## Tool Affinities
- Tool A
"""
    sections = parse_elder_memory(md_text)
    assert len(sections["Workflow Patterns"]) == 2
    assert "Pattern 1" in sections["Workflow Patterns"]
    assert len(sections["Tool Affinities"]) == 1
    assert "Tool A" in sections["Tool Affinities"]
    assert len(sections["User Preferences"]) == 0

def test_build_elder_memory_block():
    from systemu.elder.memory import build_elder_memory_block
    
    md_text = """
## Workflow Patterns
- Does thing A
"""
    block = build_elder_memory_block(md_text)
    assert "## Elder Memory" in block
    assert "**Workflow Patterns**" in block
    assert "- Does thing A" in block

def test_consolidate_shadow_memory(tmp_vault, mock_config):
    shadow = Shadow(id="shadow_1", name="Test Shadow", description="Test", system_prompt="Test", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)

    # Add buffer entries
    shadow_dir = Path(tmp_vault.root) / "shadow_army" / "shadow_shadow_1"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    buffer_file = shadow_dir / "memory_buffer.jsonl"
    buffer_file.write_text('{"observation": "test obs"}\n', encoding="utf-8")

    mock_llm_response = {
        "content": "---\ntest: true\n---\n# Memory\n- test obs consolidated"
    }

    import systemu.pipelines.memory_consolidator
    with patch("systemu.pipelines.memory_consolidator._run_coroutine", return_value=mock_llm_response):
        from systemu.pipelines.memory_consolidator import consolidate_shadow_memory
        result = consolidate_shadow_memory("shadow_1", tmp_vault, mock_config)

        assert result is True
        md_text, buf = tmp_vault.load_shadow_memory("shadow_1")
        assert "test obs consolidated" in md_text
        assert len(buf) == 0

def test_consolidate_global_memory(tmp_vault, mock_config):
    # Add global buffer entries
    buffer_file = Path(tmp_vault.root) / "elder" / "memory_buffer.jsonl"
    buffer_file.write_text('{"observation": "global obs"}\n', encoding="utf-8")

    mock_llm_response = {
        "content": "---\ntest: true\n---\n# Global\n- global obs consolidated"
    }

    import systemu.pipelines.memory_consolidator
    with patch("systemu.pipelines.memory_consolidator._run_coroutine", return_value=mock_llm_response):
        from systemu.pipelines.memory_consolidator import consolidate_global_memory
        result = consolidate_global_memory(tmp_vault, mock_config)

        assert result is True
        md_text = tmp_vault.load_global_memory()
        assert "global obs consolidated" in md_text
        buf = tmp_vault.load_elder_memory_buffer()
        assert len(buf) == 0
