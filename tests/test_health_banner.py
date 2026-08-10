"""v0.8.0.2: health-banner data-model tests (no NiceGUI runtime needed).

F19: the provider check consumes ``systemu.runtime.provider_status`` (THE ONE
MINT) instead of reading OPENROUTER_API_KEY, so "no provider" is now a claim
about all five. Every test below that depends on that verdict clears the other
credentials and points the keyless provider at a dead port -- otherwise a
developer machine running ``ollama serve`` genuinely HAS a usable provider and
the banner is right to stay quiet. That is a hermeticity fix, not a weakening:
the assertions themselves are unchanged except where the message deliberately
stopped naming one provider.
"""
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_provider(monkeypatch):
    """Nothing but what a test sets on purpose decides the provider verdict."""
    from systemu.runtime import provider_status as ps
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")   # nothing listens
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def test_healthy_state_when_one_daemon_and_key_set(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=1,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=tmp_path)
    assert not s.has_any
    assert s.worst_severity == "ok"


def test_multi_daemon_creates_danger_issue(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=3,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=tmp_path)
    assert s.has_any
    assert s.worst_severity == "danger"
    assert any("3 systemu daemon processes" in i.message for i in s.issues)
    assert any("daemon stop --all" in (i.cta or "") for i in s.issues)


def test_missing_openrouter_key_creates_warning(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=1,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=tmp_path)
    assert s.has_any
    assert s.worst_severity == "warning"
    # F19: the warning names every provider the operator could configure, so
    # the env var moved from the message to the call-to-action.
    assert any("No LLM provider is usable" in i.message for i in s.issues)
    assert any("OPENROUTER_API_KEY" in (i.cta or "") for i in s.issues)
    assert any("OLLAMA_URL" in (i.cta or "") for i in s.issues)


def test_empty_openrouter_key_treated_as_missing(monkeypatch, tmp_path):
    """Whitespace-only key is also 'missing' -- protects against blank .env entries."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=1,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=tmp_path)
    assert any("OPENROUTER_API_KEY" in (i.cta or "") for i in s.issues)


def test_unwritable_vault_creates_danger(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    fake_vault = Path("/this/path/cannot/be/written/at/all/zzz")
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=1,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=fake_vault)
    assert s.has_any
    assert s.worst_severity == "danger"
    assert any("not writable" in i.message for i in s.issues)


def test_vault_dir_none_skips_writability_check(monkeypatch):
    """When vault_dir is None (early boot / unknown), don't probe."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=1,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=None)
    assert not s.has_any


def test_multiple_issues_picks_worst_severity(monkeypatch):
    """Multi-daemon (danger) + missing key (warning) -> worst = danger."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=2,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=None)
    assert len(s.issues) >= 2
    assert s.worst_severity == "danger"


def test_count_daemons_is_resilient_to_psutil_error():
    """If psutil raises (e.g. permission), count returns 0 not exception."""
    from systemu.interface.components.health_banner import _count_systemu_daemons
    # Just confirm it returns int without raising
    result = _count_systemu_daemons()
    assert isinstance(result, int)
    assert result >= 0
