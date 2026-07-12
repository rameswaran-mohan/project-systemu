"""R-UX1 (SPEC §15-UX UX-6) — the /health page render-DATA helper.

Tested at the DATA level (a pure ``health_view() -> dict``), not via a NiceGUI
render: it surfaces the platform profile + provider/keyring/daemon status + the
DEP-10 honesty rows + a status chip.
"""
from __future__ import annotations

from systemu.interface.pages import health


def test_health_view_surfaces_profile_provider_keyring_daemon():
    v = health.health_view(provider_configured=True, provider_reachable=True,
                           keyring_locked=False, daemon_running=True)
    assert v["profile"]["forged_net_jail"] == "absent"
    assert v["provider"]["configured"] is True
    assert "backend" in v["keyring"]
    assert v["daemon"]["running"] is True
    assert v["status_chip"] == "ok"
    assert v["ok"] is True


def test_health_view_surfaces_dep10_honesty_rows():
    v = health.health_view(provider_configured=True, provider_reachable=True,
                           keyring_locked=False, daemon_running=True,
                           platform_str="linux", in_container=True)
    assert v["honesty_rows"] == v["profile"]["host_capabilities"]
    # in a container every host-only capability defers to the host companion
    for r in v["honesty_rows"]:
        assert r["via"] == "host_companion"
        assert "Host Companion" in r["note"]


def test_health_view_danger_chip_on_blocking_problem():
    v = health.health_view(provider_configured=False, keyring_locked=False,
                           daemon_running=True)
    assert v["status_chip"] == "danger"
    assert v["ok"] is False


def test_health_view_warn_chip_on_nonblocking_only():
    # daemon down is a non-blocking warning -> warn chip, still ok
    v = health.health_view(provider_configured=True, provider_reachable=True,
                           keyring_locked=False, daemon_running=False)
    assert v["status_chip"] == "warn"
    assert v["ok"] is True


def test_build_health_page_is_callable():
    # The NiceGUI renderer exists (render tested via the data helper above).
    assert callable(health.build_health_page)
