import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from systemu.core.models import Tool, ToolStatus, ToolType, Scroll
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault


@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    cfg.tier2_model = "test-model"
    return cfg


@pytest.fixture
def sample_scroll():
    return Scroll(id=generate_id("scroll"), name="s", source_session_id="t",
                  raw_instructions_path="", narrative_md="ctx")


def _tool():
    return Tool(
        id=generate_id("tool"),
        name="encrypt_docx",
        description="d",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.PROPOSED,
        parameters_schema={"source_path": {"type": "string"}, "password": {"type": "string"}},
    )


def test_nonconforming_tool_not_saved(tmp_vault, mock_config, sample_scroll):
    tool = _tool()
    # run() requires `secret_salt`, which is NOT a declared param and there is no **kwargs.
    bad_impl = {"implementation": "def run(source_path, password, secret_salt):\n    return {'success': True}\n"}

    # _generate_and_save_code does a LOCAL `from systemu.interface.notifications
    # import notify_user`, so patch it at the source module.
    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=bad_impl):
        with patch("systemu.interface.notifications.notify_user") as mock_notify:
            from systemu.pipelines.tool_forge import _generate_and_save_code
            result = _generate_and_save_code(tool, sample_scroll, mock_config, tmp_vault)

    assert result is None
    impl_path = Path(mock_config.vault_dir) / "tools" / "implementations" / "encrypt_docx.py"
    assert not impl_path.exists(), "non-conforming tool must NOT be written to disk"
    # operator gets a forge_retry notification
    assert mock_notify.called
    ctx = mock_notify.call_args.kwargs.get("context", {})
    assert ctx.get("notification_type") == "forge_retry"


def test_conforming_kwargs_tool_is_saved(tmp_vault, mock_config, sample_scroll):
    tool = _tool()
    good_impl = {"implementation": "def run(**params):\n    return {'success': True}\n"}

    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=good_impl):
        from systemu.pipelines.tool_forge import _generate_and_save_code
        result = _generate_and_save_code(tool, sample_scroll, mock_config, tmp_vault)

    assert result is not None
    assert result.status == ToolStatus.FORGED
    assert result.enabled is False
    impl_path = Path(mock_config.vault_dir) / "tools" / "implementations" / "encrypt_docx.py"
    assert impl_path.exists()
