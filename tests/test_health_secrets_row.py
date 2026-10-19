"""S5 / R-A3 -- /health's capability table shows the secrets-at-rest row.

The platform profile has carried a truthful ``secrets_at_rest`` row (derived from
a WORKING envelope round trip) since 2d, but /health's "Platform capability
profile" table listed only ``keyring_backend`` -- the row that over-claimed.

THE PROPERTY
    The table renders ``profile["secrets_at_rest"]["label"]`` from the view the
    page was built from (the render snapshot ``health_view()`` returns). The
    render itself runs NO probe: every seam that could compute the row is armed
    to raise and count while the real ``build_health_page`` executes.

HERMETIC: the real renderer runs against a recording ``nicegui.ui`` stub; the
view is built once up front with the envelope and keyring seams faked.
"""
from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

from systemu.interface.pages import health
from systemu.runtime import platform_profile as pp
from systemu.runtime.credentials import at_rest


class _Node:
    def __init__(self, rec, kind):
        self._rec, self._kind = rec, kind

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def classes(self, *a, **k):
        return self

    def style(self, *a, **k):
        return self

    def props(self, *a, **k):
        return self

    def tooltip(self, *a, **k):
        return self

    def clear(self):
        return self


class _RecordingUI:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def texts(self, fn=None):
        return [str(a[0]) for n, a, _k in self.calls
                if a and (fn is None or n == fn)]


@pytest.fixture
def no_vault(monkeypatch):
    """No open vault: the Needs-you and Approvals sections stay out of the way."""
    from systemu.interface.dashboard_state import AppState
    monkeypatch.setattr(AppState, "_instance",
                        types.SimpleNamespace(vault=None), raising=False)


def _view(monkeypatch, *, working: bool, has_keyring: bool):
    """The render snapshot, built BEFORE the render with the seams faked."""
    monkeypatch.setattr(pp, "_envelope_status",
                        lambda: (True, "") if working else (False, "envelope off (test)"))
    monkeypatch.setattr(pp, "_usable_keyring",
                        (lambda: object()) if has_keyring else (lambda: None))
    return health.health_view(provider_configured=True, provider_reachable=True,
                              keyring_locked=False, daemon_running=True,
                              platform_str="win32", in_container=False,
                              load={"load_state": "unknown"})


def _render(view, monkeypatch):
    """Execute the REAL build_health_page; every secrets seam is armed."""
    spent = []

    def _arm(name):
        def _probe(*a, **k):
            spent.append(name)
            raise AssertionError(f"the /health render ran {name}")
        return _probe

    for name in ("_envelope_status", "_usable_keyring", "secrets_at_rest",
                 "keyring_backend", "platform_profile", "build_doctor_report",
                 "_probe_keyring_locked", "_probe_daemon_state"):
        monkeypatch.setattr(pp, name, _arm(f"platform_profile.{name}"))
    monkeypatch.setattr(at_rest, "envelope_status", _arm("at_rest.envelope_status"))
    monkeypatch.setattr(at_rest, "_win32crypt", _arm("at_rest._win32crypt"))

    ui = _RecordingUI()
    fake = types.ModuleType("nicegui")
    fake.ui = ui
    with mock.patch.dict(sys.modules, {"nicegui": fake}), \
            mock.patch.object(health, "health_view", lambda **kw: view):
        health.build_health_page()
    return ui, spent


def _profile_rows(ui):
    """(label, value) pairs of the 'Platform capability profile' table."""
    labels = ui.texts("label")
    start = labels.index("Platform capability profile")
    end = labels.index("Optional capability groups")
    body = labels[start + 1:end]
    return dict(zip(body[0::2], body[1::2]))


@pytest.mark.parametrize("working, has_keyring", [
    (True, True), (True, False), (False, True), (False, False)])
def test_the_capability_table_shows_the_secrets_row_from_the_snapshot(
        monkeypatch, no_vault, working, has_keyring):
    view = _view(monkeypatch, working=working, has_keyring=has_keyring)
    row = view["profile"]["secrets_at_rest"]
    ui, spent = _render(view, monkeypatch)

    assert spent == [], f"the render probed: {spent}"
    rows = _profile_rows(ui)
    assert rows.get("Secrets at rest") == row["label"], rows
    assert rows["Secrets at rest"].isascii()
    if not working:
        assert "dpapi" not in rows["Secrets at rest"]


def test_the_table_shows_the_row_even_when_the_keyring_row_disagrees(monkeypatch, no_vault):
    """Non-vacuity: a snapshot whose keyring row over-claims must still show the
    secrets row's own words, so the value cannot be derived from the keyring."""
    view = _view(monkeypatch, working=False, has_keyring=False)
    prof = dict(view["profile"], keyring_backend="dpapi")
    ui, spent = _render(dict(view, profile=prof), monkeypatch)
    assert spent == []
    shown = _profile_rows(ui)["Secrets at rest"]
    assert shown.startswith("plaintext (") and "envelope off (test)" in shown


@pytest.mark.parametrize("prof", [
    {},                                                       # no row at all
    {"secrets_at_rest": "dpapi"},                             # not a row
    {"secrets_at_rest": {"backend": "dpapi", "label": 7, "reason": ""}},
])
def test_an_unreadable_row_reads_unknown_never_a_claim(prof):
    text = health.secrets_at_rest_text(prof)
    assert text.startswith("unknown") and "dpapi" not in text and text.isascii()
