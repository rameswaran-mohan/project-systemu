import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from systemu.core.models import Activity, ActivityStatus
from systemu.vault.vault import Vault

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))

@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    return cfg

def test_hourly_shadow_sweep(tmp_vault, mock_config):
    from systemu.scheduler.jobs import init_jobs, hourly_shadow_sweep
    
    init_jobs(mock_config, tmp_vault)

    activity = Activity(
        id="act_1", name="Test Act", scroll_id="s1",
        status=ActivityStatus.UNASSIGNED
    )
    tmp_vault.save_activity(activity)

    with patch("systemu.pipelines.shadow_decision.decide_shadow") as mock_decide:
        hourly_shadow_sweep()
        mock_decide.assert_called_once()
        args, kwargs = mock_decide.call_args
        assert args[0].id == "act_1"

def test_daily_evolution_check(tmp_vault, mock_config):
    from systemu.scheduler.jobs import init_jobs, daily_evolution_check
    
    init_jobs(mock_config, tmp_vault)

    with patch("systemu.pipelines.evolution_engine.run_evolution_check") as mock_evo:
        mock_evo.return_value = ["evo_1"]
        daily_evolution_check()
        mock_evo.assert_called_once()

def test_parse_last_consolidated():
    from systemu.scheduler.jobs import _parse_last_consolidated
    
    md_text = """---
last_consolidated: 2026-05-04T12:00:00Z
---
"""
    dt = _parse_last_consolidated(md_text)
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 4
    
    md_text_bad = "no frontmatter"
    dt2 = _parse_last_consolidated(md_text_bad)
    from systemu.core.utils import utcnow
    assert dt2 < utcnow()
