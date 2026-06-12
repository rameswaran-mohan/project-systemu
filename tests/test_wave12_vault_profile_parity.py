"""W12 — user-profile methods must exist on EVERY vault backend.

Ship-readiness audit catch (live, 2026-06-12): the welcome wizard's Finish
died with "'FileVault' object has no attribute 'save_user_profile'" — the
v0.9.0 user-profile quartet was added to the SQL Vault but never forwarded
through the FileVault/ParallelVault wrappers (the SAME wrapper-drift class
as the v0.8.0.1 decisions incident and the W1 episodic incident).

Combined with the W11.4 mandatory gate this HARD-LOCKED file-backend
installs at /welcome: the gate requires a profile that could never save.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from systemu.vault.vault import Vault

PROFILE_METHODS = ("get_user_profile", "save_user_profile",
                   "load_user_facts", "append_user_fact")


def _profile():
    from systemu.core.models import UserProfile
    return UserProfile(name="R", location_text="Chennai, IN",
                       timezone="UTC", default_output_dir="C:/x")


@pytest.fixture
def inner(tmp_path: Path) -> Vault:
    return Vault(str(tmp_path / "vault"))


class TestFileVaultParity:
    def test_methods_exist(self, inner):
        from systemu.storage.file_vault import FileVault
        fv = FileVault(inner)
        missing = [m for m in PROFILE_METHODS if not hasattr(fv, m)]
        assert missing == [], f"FileVault drift: {missing}"

    def test_profile_roundtrip(self, inner):
        from systemu.storage.file_vault import FileVault
        fv = FileVault(inner)
        assert fv.get_user_profile() is None
        fv.save_user_profile(_profile())
        assert fv.get_user_profile().name == "R"

    def test_facts_roundtrip(self, inner):
        from systemu.storage.file_vault import FileVault
        fv = FileVault(inner)
        fv.append_user_fact(fact="Role: analyst", source="test",
                            tags=["office_context"])
        facts = fv.load_user_facts(tags=["office_context"])
        assert len(facts) == 1 and "analyst" in facts[0].fact

    def test_wizard_save_path_works_on_filevault(self, inner):
        """The EXACT field repro: save_onboarding against a FileVault."""
        from systemu.interface.pages.welcome import save_onboarding
        from systemu.storage.file_vault import FileVault
        fv = FileVault(inner)
        save_onboarding(fv, name="R", location="X", timezone="UTC",
                        output_dir="C:/x", persona="Freelance")
        assert fv.get_user_profile() is not None
        assert fv.load_user_facts(tags=["persona"])


class TestSqliteVaultParity:
    """W12 docker A6 audit: SqliteVault had neither `.root` nor the quartet —
    the vault migrator crashed (caught) and the profile layer silently broke
    on sqlite/postgres backends; with the W11.4 gate that would lock
    docker-mode installs at /welcome. Fourth wrapper-drift occurrence."""

    def _sv(self, tmp_path):
        from systemu.storage.sqlite.vault import SqliteVault
        db = tmp_path / "v.db"
        return SqliteVault(f"sqlite:///{db}", memory_dir=tmp_path / "memory")

    def test_exposes_root(self, tmp_path):
        sv = self._sv(tmp_path)
        assert Path(sv.root).is_dir()

    def test_methods_exist(self, tmp_path):
        sv = self._sv(tmp_path)
        missing = [m for m in PROFILE_METHODS if not hasattr(sv, m)]
        assert missing == [], f"SqliteVault drift: {missing}"

    def test_profile_roundtrip(self, tmp_path):
        sv = self._sv(tmp_path)
        assert sv.get_user_profile() is None
        sv.save_user_profile(_profile())
        assert sv.get_user_profile().name == "R"

    def test_facts_roundtrip(self, tmp_path):
        sv = self._sv(tmp_path)
        sv.append_user_fact(fact="Role: analyst", source="test",
                            tags=["office_context"])
        assert len(sv.load_user_facts(tags=["office_context"])) == 1


class TestParallelVaultParity:
    def _pv(self, tmp_path):
        from systemu.storage.parallel_vault import ParallelVault
        p = Vault(str(tmp_path / "p"))
        s = Vault(str(tmp_path / "s"))
        return ParallelVault(p, s), p

    def test_methods_exist(self, tmp_path):
        pv, _ = self._pv(tmp_path)
        missing = [m for m in PROFILE_METHODS if not hasattr(pv, m)]
        assert missing == [], f"ParallelVault drift: {missing}"

    def test_profile_roundtrip_hits_primary(self, tmp_path):
        pv, primary = self._pv(tmp_path)
        pv.save_user_profile(_profile())
        assert pv.get_user_profile().name == "R"
        assert primary.get_user_profile() is not None, \
            "the primary store must be authoritative"
