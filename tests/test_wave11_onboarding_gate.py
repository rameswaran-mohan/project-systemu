"""W11.4 — the mandatory first-run gate.

Operator requirement (2026-06-12): setup must be ENFORCED — a mandatory
walkthrough at the very first start. Fresh installs are funneled to
/welcome from EVERY spine route until the API key exists and the profile
is saved; the wizard then hands off to the tour (W11.5).

Deliberate softness, by design:
  * pre-W11 installs that explicitly skipped stay skipped (no retroactive
    nagging) — the old sentinel is honored forever;
  * SYSTEMU_SKIP_ONBOARDING=1 is the CI/dev/smoke escape hatch;
  * the TOUR never causes redirects (it must navigate spine routes to
    exist) — it auto-starts after the wizard and offers resume until done;
  * any gate error means NO redirect — never brick the dashboard.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(str(tmp_path / "vault"))


def _config(key: str = ""):
    return SimpleNamespace(openrouter_api_key=key, output_dir="")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SYSTEMU_SKIP_ONBOARDING", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


class TestOnboardingGate:
    def test_fresh_install_blocks_on_key_and_profile(self, vault):
        from systemu.interface.pages.welcome import onboarding_gate
        missing = onboarding_gate(vault, _config())
        assert "key_present" in missing and "profile_present" in missing

    def test_clears_when_key_and_profile_exist(self, vault):
        from systemu.interface.pages.welcome import onboarding_gate, save_onboarding
        save_onboarding(vault, name="R", location="X", timezone="UTC",
                        output_dir="C:/x")
        assert onboarding_gate(vault, _config(key="sk-or-x")) == []

    def test_tour_never_causes_redirects(self, vault):
        """The tour navigates spine routes — gating on it would loop."""
        from systemu.interface.pages.welcome import onboarding_gate, save_onboarding
        save_onboarding(vault, name="R", location="X", timezone="UTC",
                        output_dir="C:/x")
        missing = onboarding_gate(vault, _config(key="sk-or-x"))
        assert "tour_completed" not in missing

    def test_env_escape_hatch(self, vault, monkeypatch):
        from systemu.interface.pages.welcome import onboarding_gate
        monkeypatch.setenv("SYSTEMU_SKIP_ONBOARDING", "1")
        assert onboarding_gate(vault, _config()) == []

    def test_pre_w11_skip_sentinel_honored(self, vault):
        """Existing installs that said 'later' are never retro-nagged."""
        from systemu.interface.pages.welcome import mark_skipped, onboarding_gate
        mark_skipped(vault)
        assert onboarding_gate(vault, _config()) == []

    def test_defensive_on_broken_inputs(self):
        from systemu.interface.pages.welcome import onboarding_gate
        assert onboarding_gate(object(), object()) == []


class TestDashboardWiring:
    def test_every_spine_route_is_gated(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        n_routes = src.count("@ui.page(")
        n_guarded = src.count("_redirect_to_welcome_if_needed()")
        # every route except /welcome itself runs the guard
        assert n_guarded >= n_routes - 1, \
            f"only {n_guarded} of {n_routes} routes funnel fresh installs to /welcome"

    def test_welcome_route_is_not_gated_against_itself(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        welcome_fn = src.split('@ui.page("/welcome")')[1].split("@ui.page(")[0]
        assert "_redirect_to_welcome_if_needed" not in welcome_fn


class TestWelcomeEnforcement:
    def test_key_step_offers_recheck_not_text_entry(self):
        from systemu.interface.pages import welcome
        src = inspect.getsource(welcome)
        assert "Re-check" in src, \
            "the operator adds the key to .env, then re-checks — no restart dance"
        assert "type=password" not in src, "keys are NEVER typed in the browser"

    def test_refresh_key_status_reads_env(self, monkeypatch, tmp_path):
        from systemu.interface.pages.welcome import _refresh_key_status
        cfg = _config()
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fresh")
        assert _refresh_key_status(
            cfg, env_file=str(tmp_path / "none.env")) is True
        assert cfg.openrouter_api_key == "sk-or-fresh"

    def test_refresh_key_status_reads_env_file_without_stomping(
            self, monkeypatch, tmp_path):
        """The operator saves .env and clicks Re-check — picked up WITHOUT
        load_dotenv(override=True) clobbering the daemon's environment."""
        from systemu.interface.pages.welcome import _refresh_key_status
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        env = tmp_path / ".env"
        env.write_text("OPENROUTER_API_KEY=sk-or-file\n", encoding="utf-8")
        cfg = _config()
        assert _refresh_key_status(cfg, env_file=str(env)) is True
        assert cfg.openrouter_api_key == "sk-or-file"
        import os
        assert os.environ.get("OPENROUTER_API_KEY") is None, \
            "the process environment must not be mutated"

    def test_refresh_key_status_false_when_absent(self, monkeypatch, tmp_path):
        from systemu.interface.pages.welcome import _refresh_key_status
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert _refresh_key_status(
            _config(), env_file=str(tmp_path / "none.env")) is False

    def test_finish_requires_the_key(self):
        from systemu.interface.pages import welcome
        src = inspect.getsource(welcome.build_welcome_page)
        assert "_refresh_key_status" in src.split("def _finish")[1].split("def _later")[0], \
            "Finish must verify the key exists — setup is enforced, not suggested"

    def test_skip_button_hidden_while_gate_active(self):
        from systemu.interface.pages import welcome
        src = inspect.getsource(welcome.build_welcome_page)
        assert "_gate_active" in src, \
            "'Maybe later' must not be offered to fresh installs (mandatory setup)"

    def test_finish_hands_off_to_the_tour(self):
        from systemu.interface.pages import welcome
        src = inspect.getsource(welcome.build_welcome_page)
        assert "tour=" in src, "the wizard flows directly into the tour (W11.5)"
