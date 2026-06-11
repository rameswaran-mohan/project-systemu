"""Wave 1.3 — ToolSandbox must anchor vault_root at construction.

Root cause: ``self.vault_root = Path(vault_root)`` kept relative paths
relative (default ``Path(".")``), so implementation-path resolution
(``vault_root.parent / implementation_path``) floated with the process CWD —
a tool forged from the project root broke when the daemon/worker later ran
with a different working directory.
"""
import os
from pathlib import Path

from systemu.runtime.tool_sandbox import ToolSandbox


class TestVaultRootAnchoring:
    def test_relative_vault_root_is_resolved_at_construction(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sandbox = ToolSandbox("vault")          # relative, like config default
        assert sandbox.vault_root.is_absolute()
        assert sandbox.vault_root == (tmp_path / "vault").resolve()

    def test_default_dot_vault_root_is_resolved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sandbox = ToolSandbox()                 # vault_root=None → "." fallback
        assert sandbox.vault_root.is_absolute()

    def test_resolution_survives_cwd_change(self, tmp_path, monkeypatch):
        # The actual failure mode: construct in the project root, execute with
        # a different CWD. The anchored root must keep pointing at the
        # original implementation file.
        project = tmp_path / "project"
        impl_dir = project / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True)
        impl_file = impl_dir / "mytool.py"
        impl_file.write_text("print('{}')", encoding="utf-8")

        monkeypatch.chdir(project)
        sandbox = ToolSandbox("vault")
        stored_rel = "vault/tools/implementations/mytool.py"   # as the forge stores it

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        resolved = (sandbox.vault_root.parent / stored_rel).resolve()
        assert resolved == impl_file.resolve()
        assert resolved.exists()

    def test_absolute_vault_root_unchanged(self, tmp_path):
        sandbox = ToolSandbox(tmp_path / "vault")
        assert sandbox.vault_root == (tmp_path / "vault").resolve()
