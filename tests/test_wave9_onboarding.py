"""W9.1 — first-run onboarding (/welcome).

Fresh installs land on an empty dashboard with a warning banner; the
UserProfile model has existed since v0.9.0 but nothing ever collects it —
which is why a run guessed the operator's location by IP. The welcome page
collects the office profile (name, location, timezone, output folder, plus
role/org as user_facts) in one screen, surfaces the model-preset choice
(8.1's deferred discoverability), and the dashboard redirects to it exactly
until the operator finishes or explicitly skips.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(str(tmp_path))


class TestNeedsOnboarding:
    def test_true_on_fresh_vault(self, vault):
        from systemu.interface.pages.welcome import needs_onboarding
        assert needs_onboarding(vault) is True

    def test_false_after_completion(self, vault):
        from systemu.interface.pages.welcome import needs_onboarding, save_onboarding
        save_onboarding(vault, name="Ramesh", location="Chennai, IN",
                        timezone="Asia/Kolkata", output_dir="C:/Users/r/Documents")
        assert needs_onboarding(vault) is False

    def test_false_after_explicit_skip(self, vault):
        from systemu.interface.pages.welcome import needs_onboarding, mark_skipped
        mark_skipped(vault)
        assert needs_onboarding(vault) is False, \
            "'later' must not nag on every page load"

    def test_defensive_on_broken_vault(self):
        from systemu.interface.pages.welcome import needs_onboarding
        # A vault that raises must not break the page shell — no redirect.
        assert needs_onboarding(object()) is False


class TestSaveOnboarding:
    def test_profile_round_trip(self, vault):
        from systemu.interface.pages.welcome import save_onboarding
        profile = save_onboarding(
            vault, name="Ramesh", location="Chennai, IN",
            timezone="Asia/Kolkata", output_dir="C:/Users/r/Documents",
        )
        stored = vault.get_user_profile()
        assert stored is not None
        assert stored.name == "Ramesh"
        assert stored.location_text == "Chennai, IN"
        assert stored.timezone == "Asia/Kolkata"
        assert stored.default_output_dir == "C:/Users/r/Documents"
        assert profile.name == stored.name

    def test_office_context_stored_as_facts(self, vault):
        from systemu.runtime.user_profile import get_facts
        from systemu.interface.pages.welcome import save_onboarding
        save_onboarding(
            vault, name="R", location="Chennai", timezone="Asia/Kolkata",
            output_dir="C:/x", role="Finance analyst", org="Acme Pvt Ltd",
        )
        facts = get_facts(vault, tags=["office_context"])
        texts = " | ".join(f.fact for f in facts)
        assert "Finance analyst" in texts and "Acme Pvt Ltd" in texts
        assert all(f.source == "onboarding" for f in facts)

    def test_empty_office_context_adds_no_facts(self, vault):
        from systemu.runtime.user_profile import get_facts
        from systemu.interface.pages.welcome import save_onboarding
        save_onboarding(vault, name="R", location="X", timezone="UTC",
                        output_dir="C:/x")
        assert get_facts(vault, tags=["office_context"]) == []

    def test_persona_stored_as_tagged_fact(self, vault):
        """Charter v2 req 5: one product, persona-adaptive — the choice is a
        fact that trust posture + starter kits consume later."""
        from systemu.runtime.user_profile import get_facts
        from systemu.interface.pages.welcome import save_onboarding, personas
        assert len(personas()) == 5
        save_onboarding(vault, name="R", location="X", timezone="UTC",
                        output_dir="C:/x", persona="Freelance")
        facts = get_facts(vault, tags=["persona"])
        assert len(facts) == 1 and "Freelance" in facts[0].fact


class TestHelpers:
    def test_detect_timezone_returns_nonempty(self):
        from systemu.interface.pages.welcome import detect_timezone
        tz = detect_timezone()
        assert isinstance(tz, str) and tz

    def test_steps_contract(self):
        from systemu.interface.pages.welcome import onboarding_steps
        steps = onboarding_steps()
        assert len(steps) == 4
        joined = " ".join(steps).lower()
        for needle in ("key", "preset", "profile", "try"):
            assert needle in joined


class TestWiring:
    def test_route_registered_and_home_redirects(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        assert '"/welcome"' in src, "the /welcome route must be registered"
        # W11.4 superseded the home-only needs_onboarding redirect with the
        # every-route onboarding_gate funnel (mandatory setup) — same intent,
        # stronger contract.
        assert "onboarding_gate" in src, \
            "first-run operators must be redirected to /welcome"

    def test_welcome_offers_presets_but_never_collects_the_key(self):
        from systemu.interface.pages import welcome
        src = inspect.getsource(welcome)
        assert "PRESETS" in src, "step 2 surfaces the model presets"
        # Security stance (mirrors Settings): the key is NEVER typed into the
        # UI — status + .env instructions only.
        assert 'type=password' not in src
        assert '_update_env_var("OPENROUTER_API_KEY"' not in src