"""scan_all() aggregates per-scope diagnoses across the vault, deduped."""
from unittest.mock import MagicMock


def test_scan_all_aggregates_and_dedupes():
    from systemu.recovery.engine import RecoveryEngine, RecoveryAction
    vault = MagicMock()
    vault.load_index.return_value = [{"id": "tool_x"}]   # tools index
    vault.list_shadows.return_value = []
    vault.list_activities.return_value = []
    vault.list_scrolls.return_value = []
    eng = RecoveryEngine(vault)
    a = RecoveryAction(scope_kind="tool", scope_id="tool_x", kind="DEP_PENDING",
                       reason="missing package: pillow", fix_url="", fix_command="",
                       severity="blocker")
    eng.diagnose_tool = MagicMock(return_value=[a])
    eng.diagnose_shadow = MagicMock(return_value=[])
    eng.diagnose_activity = MagicMock(return_value=[])
    eng.diagnose_scroll = MagicMock(return_value=[])
    out = eng.scan_all()
    assert a in out
    assert len(out) == len({(x.scope_kind, x.scope_id, x.kind) for x in out})  # deduped
