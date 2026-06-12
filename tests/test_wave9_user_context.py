"""W9.2 — one profile-context composer, injected into the quick lane.

The refiner and runtime already consume the profile; the QUICK LANE — the
new default — injects nothing, so the fastest path is also the most
identity-blind one. One pure composer feeds them all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    for sub in ["tools/implementations", "elder"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


class TestProfileContextBlock:
    def test_empty_when_no_profile(self, vault):
        from systemu.runtime.user_context import profile_context_block
        assert profile_context_block(vault) == ""

    def test_block_carries_profile_and_office_facts(self, vault):
        from systemu.interface.pages.welcome import save_onboarding
        from systemu.runtime.user_context import profile_context_block
        save_onboarding(vault, name="Ramesh", location="Chennai, IN",
                        timezone="Asia/Kolkata", output_dir="C:/docs",
                        role="Finance analyst", org="Acme Pvt Ltd")
        block = profile_context_block(vault)
        for needle in ("Ramesh", "Chennai, IN", "Asia/Kolkata", "C:/docs",
                       "Finance analyst", "Acme Pvt Ltd"):
            assert needle in block
        # It must read as instructions, not a JSON dump.
        assert "Operator" in block or "operator" in block

    def test_defensive_on_broken_vault(self):
        from systemu.runtime.user_context import profile_context_block
        assert profile_context_block(object()) == ""


class TestQuickLaneInjection:
    def test_system_prompt_carries_the_block(self, vault):
        """The fastest lane must know who it works for — the spa run guessed
        the operator's location by IP because nothing injected identity."""
        from systemu.interface.pages.welcome import save_onboarding
        from systemu.pipelines.quick_task import run_quick_task
        save_onboarding(vault, name="Ramesh", location="Chennai, IN",
                        timezone="Asia/Kolkata", output_dir="C:/docs")

        seen = {}

        def llm(*, system, user, config=None):
            seen["system"] = system
            return {"action": "ANSWER", "answer_md": "ok"}

        res = run_quick_task("find a spa near me", None, vault, llm_json=llm)
        assert res.status == "success"
        assert "Chennai, IN" in seen["system"], \
            "the quick lane must inject the operator's profile"

    def test_no_profile_means_clean_prompt(self, vault):
        from systemu.pipelines.quick_task import run_quick_task

        seen = {}

        def llm(*, system, user, config=None):
            seen["system"] = system
            return {"action": "ANSWER", "answer_md": "ok"}

        run_quick_task("hello", None, vault, llm_json=llm)
        assert "Operator profile" not in seen["system"]