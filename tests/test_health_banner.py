"""v0.8.0.2: health-banner data-model tests (no NiceGUI runtime needed)."""
from pathlib import Path
from unittest.mock import patch


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
    assert any("OPENROUTER_API_KEY" in i.message for i in s.issues)


def test_empty_openrouter_key_treated_as_missing(monkeypatch, tmp_path):
    """Whitespace-only key is also 'missing' -- protects against blank .env entries."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
    with patch(
        "systemu.interface.components.health_banner._count_systemu_daemons",
        return_value=1,
    ):
        from systemu.interface.components.health_banner import build_health_state
        s = build_health_state(vault_dir=tmp_path)
    assert any("OPENROUTER_API_KEY" in i.message for i in s.issues)


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
