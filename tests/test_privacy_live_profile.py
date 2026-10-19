"""S2-PIN - the LIVE platform profile reaches /privacy.

Every test in ``tests/test_privacy.py`` hands ``privacy_report`` a fixture
profile dict, so nothing proved the page renders from the real
``platform_profile()`` of the machine it runs on. These tests build the page
model with NO profile injected - the real ``platform_profile`` runs - and force
only the egress-enforcer seam (``action_governance._egress_enforcer_available``,
the same function the forged-network hard-DENY consults) at its own seam.

Non-vacuity (DEC-32: a pin asserting a value the failure path also produces is
not a pin): forcing the seam AVAILABLE must change what the page says, so the
"unavailable" sentence is a reading of the seam, not a constant.

No network: the provider reachability probe is armed to fail the test if the
render path ever spends it (render-time reads must be cheap file reads only).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from systemu.runtime import action_governance, platform_profile, privacy


@pytest.fixture(autouse=True)
def spent(monkeypatch):
    """Arm every provider probe: the render path may not touch the network.

    A RECORDING arm, not a raising one: ``platform_profile.provider_statuses``
    swallows any exception (fail-closed to "not configured"), so a probe that
    raised would be silently absorbed and the pin would pass on the failure
    path. Every call is recorded, and the probe still refuses to run."""
    from systemu.runtime import provider_status
    calls = []

    def _arm(name):
        def _probe(*a, **k):
            calls.append(name)
            raise RuntimeError("privacy render path spent a provider probe")
        return _probe

    monkeypatch.setattr(provider_status, "probe_ollama", _arm("probe_ollama"))
    monkeypatch.setattr(provider_status, "all_provider_statuses",
                        _arm("all_provider_statuses"))
    for k in ("SHARING_ON_TELEGRAM_BOT_TOKEN", "SYSTEMU_MCP_SERVER_URLS",
              "SYSTEMU_WEB_STACK_V2"):
        monkeypatch.delenv(k, raising=False)
    yield calls


def _model(tmp_path):
    from systemu.interface.pages import privacy as page
    root = tmp_path / "vault"
    root.mkdir(exist_ok=True)
    return page.privacy_page_model(vault=SimpleNamespace(root=str(root)),
                                   config=SimpleNamespace(web_reader_backend="auto"))


def _sandbox_lines(model):
    sec = [s for s in model["sections"] if s["key"] == "sandbox"]
    assert len(sec) == 1
    return " ".join(sec[0]["lines"])


def test_the_live_profile_with_the_enforcer_unavailable_reaches_the_page(tmp_path, monkeypatch, spent):
    monkeypatch.setattr(action_governance, "_egress_enforcer_available", lambda: False)
    model = _model(tmp_path)
    assert spent == []

    # It IS the live profile, not a fixture: the page reports this machine.
    live = platform_profile.platform_profile(provider_configured=False)
    assert model["report"]["os"] == live["os_family"] == platform_profile._os_family(sys.platform)
    assert model["report"]["enforcer"] == "unavailable"
    sec = [s for s in model["report"]["sections"] if s["key"] == "secrets"][0]
    assert live["keyring_backend"] in sec["detail"]

    text = _sandbox_lines(model).lower()
    # States the enforcer is unavailable ...
    assert "egress enforcer is unavailable" in text
    # ... and what that means for the operator.
    assert "no os-level egress jail" in text
    assert "refused outright" in text and "cannot approve" in text
    assert "not a sandbox" in text
    from systemu.interface.pages import privacy as page
    assert page.page_text(model).isascii()


def test_the_page_reads_the_enforcer_seam_rather_than_a_constant(tmp_path, monkeypatch):
    """Non-vacuity. Forced available, the 'unavailable' sentence must go away
    (the profile still reports no jail, so the page must not claim a sandbox
    either)."""
    monkeypatch.setattr(action_governance, "_egress_enforcer_available", lambda: True)
    model = _model(tmp_path)
    text = _sandbox_lines(model).lower()
    assert "egress enforcer is unavailable" not in text
    assert model["report"]["enforcer"] == "available"
    sandbox = [s for s in model["report"]["sections"] if s["key"] == "sandbox"][0]
    assert sandbox["status"] == "unsandboxed"


def test_the_unforced_seam_is_what_ships_today(tmp_path):
    """The seam is not forced here: on this tree S2 has not landed, so the live
    page must say the enforcer is unavailable. When S2 lands this goes red and
    the page wording is revisited on purpose."""
    assert action_governance._egress_enforcer_available() is False
    assert "egress enforcer is unavailable" in _sandbox_lines(_model(tmp_path)).lower()


def test_the_live_render_spends_no_provider_probe(tmp_path, monkeypatch, spent):
    """The autouse fixture arms every probe; building the live page must not
    trip it (``platform_profile()`` with no args would)."""
    monkeypatch.setattr(action_governance, "_egress_enforcer_available", lambda: False)
    _model(tmp_path)
    assert spent == []
    platform_profile.platform_profile()   # proves the arm is live ...
    assert spent == ["all_provider_statuses"]   # ... and recording
