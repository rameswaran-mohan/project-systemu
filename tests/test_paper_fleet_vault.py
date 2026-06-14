"""Plan 0 Build 3 (Task 3.2) — vault child-execution namespace + namespaced audit.

Two additions:
  * create_child_execution_namespace(parent_id, child_id) -> Path
      returns (and mkdirs) vault.root/execution_<parent>/child_<child>.
  * append_action_audit(entry, namespace_path=None)
      when namespace_path given, the entry lands under
      namespace_path/audit/actions.jsonl; when None, behaviour is
      unchanged (vault/audit/actions.jsonl).
"""
import json

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def tmp_vault(tmp_path):
    return Vault(str(tmp_path))


class TestCreateChildExecutionNamespace:
    def test_returns_expected_path_and_mkdirs(self, tmp_vault):
        ns = tmp_vault.create_child_execution_namespace("exec_abc", "child_001")
        expected = tmp_vault.root / "execution_exec_abc" / "child_child_001"
        assert ns == expected
        assert ns.exists()
        assert ns.is_dir()

    def test_idempotent(self, tmp_vault):
        a = tmp_vault.create_child_execution_namespace("p1", "c1")
        b = tmp_vault.create_child_execution_namespace("p1", "c1")
        assert a == b
        assert a.exists()


class TestAppendActionAuditNamespace:
    _ENTRY = {
        "ts": "2026-06-14T00:00:00+00:00",
        "execution_id": "exec_abc",
        "objective_id": "1",
        "action": "write_csv_file",
        "params": {"path": "/tmp/x.csv"},
        "success": True,
        "error": None,
    }

    def test_without_namespace_unchanged(self, tmp_vault):
        tmp_vault.append_action_audit(dict(self._ENTRY))
        default_path = tmp_vault.root / "audit" / "actions.jsonl"
        assert default_path.exists()
        rows = [
            json.loads(l)
            for l in default_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["execution_id"] == "exec_abc"

    def test_with_namespace_writes_under_child_dir(self, tmp_vault):
        ns = tmp_vault.create_child_execution_namespace("exec_abc", "child_001")
        tmp_vault.append_action_audit(dict(self._ENTRY), namespace_path=ns)

        ns_audit = ns / "audit" / "actions.jsonl"
        assert ns_audit.exists()
        rows = [
            json.loads(l)
            for l in ns_audit.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["action"] == "write_csv_file"

        # Default global log must NOT have received the namespaced entry.
        default_path = tmp_vault.root / "audit" / "actions.jsonl"
        if default_path.exists():
            default_rows = [
                l for l in default_path.read_text(encoding="utf-8").splitlines() if l.strip()
            ]
            assert default_rows == []
