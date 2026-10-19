"""e2e31 D-9: the .env the settings writer edits is the operating home's.

``settings._update_env_var`` -- the ONE writer behind the wizard's preset, the
tier/provider inputs, the stuck-guard, the price overrides, the adherence and
evolution settings -- opened ``Path(".env")``. That is a cwd-relative path, and
the vault-root doctrine (N9) exists precisely because a cwd is an accident of
who launched the process: ``operating_home`` is a pure function of the resolved
root, so every process holding the same root names the same directory whatever
directory it was started in.

It has worked so far only because the daemon spawns its child WITH
``cwd=operating_home``. Latent, not harmless: any caller that reaches this
writer from another cwd -- a CLI subcommand, a test, a future in-process
surface, an operator running the dashboard by hand -- silently writes a second
``.env`` that the next boot will not read, and the setting appears not to have
been saved.

THE PROPERTY pinned here:

    A setting the wizard writes lands in ``<operating_home>/.env``, derived
    from the mint, whatever directory the process happens to be standing in.

Same consumption as ``execution_snapshot.audit_data_root()``: read the ONE
minted member, never re-derive a home from a cwd. No daemon is started; the
vault, the operating home and the cwd are three distinct directories under
``tmp_path`` so a cwd-relative write cannot accidentally land in the right one.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.interface.pages import settings as settings_page
from systemu.interface.pages import welcome
from systemu.vault.vault import Vault

_TIER_ENV = ("SYSTEMU_TIER1_MODEL", "SYSTEMU_TIER2_MODEL", "SYSTEMU_TIER3_MODEL")


@pytest.fixture
def homes(tmp_path: Path, monkeypatch):
    """Three distinct directories: the vault, its operating home, and the cwd."""
    operating_home = tmp_path / "operating-home"
    vault_root = operating_home / "systemu" / "vault"
    elsewhere = tmp_path / "elsewhere"
    vault_root.mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault_root))
    monkeypatch.chdir(elsewhere)
    # monkeypatch owns these so the writer's live-environment patch is undone.
    for name in _TIER_ENV:
        monkeypatch.setenv(name, "")
    return SimpleNamespace(home=operating_home, vault=vault_root,
                           elsewhere=elsewhere)


def _cfg():
    """A machine the provider gate accepts, with nothing else set."""
    from systemu.runtime import provider_status as ps
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_model": "" for i in (1, 2, 3)})
    base.update(output_dir="", openrouter_api_key="sk-or-test")
    return SimpleNamespace(**base)


def test_the_operating_home_is_where_the_mint_says(homes):
    """The premise, stated so a change to the mint cannot silently pass this
    file by asserting about a directory that is no longer the home."""
    from systemu.runtime.vault_root import resolve_vault_root
    assert Path(resolve_vault_root().operating_home) == homes.home
    assert Path.cwd() == homes.elsewhere


def test_the_wizards_preset_write_lands_in_the_operating_home(homes):
    from sharing_on.model_presets import PRESETS

    vault = Vault(str(homes.vault))
    ok, msg = welcome.finalize_onboarding(
        vault, _cfg(), name="Ramesh", preset="quality",
        refresh_key_fn=lambda _cfg: False)
    assert ok, msg

    written = (homes.home / ".env").read_text(encoding="utf-8")
    assert f'SYSTEMU_TIER1_MODEL={PRESETS["quality"]["tier1"]}' in written
    assert not (homes.elsewhere / ".env").exists(), \
        "the preset was written relative to the process cwd"


def test_the_writer_itself_never_consults_the_cwd(homes):
    settings_page._update_env_var("SYSTEMU_TIER3_MODEL", "some/model")
    assert (homes.home / ".env").read_text(encoding="utf-8").strip() == \
        "SYSTEMU_TIER3_MODEL=some/model"
    assert not (homes.elsewhere / ".env").exists()


def test_an_existing_env_is_updated_in_place_not_replaced(homes):
    (homes.home / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-keepme\nSYSTEMU_TIER1_MODEL=old/model\n",
        encoding="utf-8")
    settings_page._update_env_var("SYSTEMU_TIER1_MODEL", "new/model")
    text = (homes.home / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-keepme" in text, \
        "moving the path must not turn an update into a truncating write"
    assert "SYSTEMU_TIER1_MODEL=new/model" in text
    assert "old/model" not in text


def test_the_env_path_is_the_minted_member_not_a_re_derived_one(homes):
    """N9: the home is read off the verdict, never recomputed from a cwd."""
    from systemu.runtime.vault_root import resolve_vault_root
    assert settings_page._env_file_path() == \
        Path(resolve_vault_root().operating_home) / ".env"
