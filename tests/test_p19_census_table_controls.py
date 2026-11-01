"""P19 -- the /table census panel's controls: Withdraw / Pause / Resume, and NO grant.

Ruled: GRANT STAYS CLI-ONLY. A grant is a standing permission to read the operator's
machine whose findings go to the model provider, so it is taken where the roots grant is
taken -- a terminal, the full card, a typed y/N. A button on a dashboard page is reachable
by anything that can drive the page. Withdrawing is the safe direction, so it is one click.

Pinned here against the REAL consent store and the REAL fact store, with only nicegui
recorded and ``run.io_bound`` run inline:

  * a not-granted row shows the exact ``systemu census grant <category>`` command as
    copyable text, and no row anywhere has a Grant button;
  * Withdraw (one click) stops future scans and PURGES what the category found (WM-7);
  * Pause keeps the facts; Resume scans again;
  * Resume is NOT a grant: after a withdrawal it records nothing;
  * "copied" is said only when the browser confirms the clipboard write.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from systemu.interface.pages import table_census as tcp
from systemu.runtime import ambient_census as ac
from systemu.runtime import census_consent as cc
from systemu.runtime.world_model import FactStore


class _Node:
    def __getattr__(self, name):
        return lambda *a, **k: self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingUI:
    def __init__(self, browser_accepts=True):
        self.calls = []
        self.notes = []
        self.scripts = []
        self._accepts = browser_accepts

    def notify(self, msg, **k):
        self.notes.append((msg, k.get("type")))

    def run_javascript(self, code, timeout=1.0):
        self.scripts.append(code)

        async def _answer():
            return self._accepts
        return _answer()

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node()
        return _call

    def labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "label" and a]

    def button_texts(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "button" and a]

    def handler(self, *, text=None, icon=None):
        hits = [k.get("on_click") for n, a, k in self.calls if n == "button"
                and ((text is not None and a and a[0] == text)
                     or (icon is not None and k.get("icon") == icon))]
        assert len(hits) == 1, (text, icon, self.button_texts())
        return hits[0]


def _run(result):
    if inspect.isawaitable(result):
        asyncio.run(result)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """The panel's live render + its real controls, against a real vault."""
    rec = _RecordingUI()
    monkeypatch.setattr(tcp, "ui", rec)

    async def _inline(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(tcp, "run", SimpleNamespace(io_bound=_inline))
    reloads = []

    async def _reload():
        reloads.append(1)

    vault = SimpleNamespace(root=tmp_path)
    controls = tcp._census_panel_controls(vault, _reload)

    def render():
        rec.calls.clear()
        tcp._render_overview(ac.census_overview(vault), controls)
        return rec

    return SimpleNamespace(rec=rec, vault=vault, root=tmp_path, controls=controls,
                           reloads=reloads, render=render)


# ── no grant on the panel ─────────────────────────────────────────────────────

def test_the_controls_have_no_grant_verb(live):
    assert set(live.controls) == {"withdraw", "pause", "resume"}


def test_a_not_granted_row_shows_the_cli_grant_command_and_no_grant_button(live):
    rec = live.render()
    labels = rec.labels()
    for row in ac.census_overview(live.vault)["rows"]:
        if row["state"] == ac.STATE_NOT_GRANTED:
            assert f"systemu census grant {row['category']}" in labels, row["category"]
    assert tcp.CENSUS_GRANT_LEAD in labels
    assert not any("grant" in t.lower() for t in rec.button_texts()), rec.button_texts()
    assert not (live.root / "census_consent.json").exists(), "rendering recorded consent"


def test_the_grant_command_is_offered_only_where_the_cli_grants(monkeypatch, tmp_path):
    """A category this machine cannot scan is never offered a command the CLI refuses."""
    monkeypatch.setattr(ac, "_platform_family", lambda: "macos")
    rows = {r["category"]: r for r in
            ac.census_overview(SimpleNamespace(root=tmp_path))["rows"]}
    assert tcp.grant_command(rows["automation_apps"]) == ""
    assert tcp.grant_command(rows["cloud_sync_roots"]) == \
        "systemu census grant cloud_sync_roots"
    assert tcp.grant_command({"state": ac.STATE_NOT_GRANTED, "category": "x"}) == ""


def test_copying_the_grant_command_is_witnessed(live):
    rec = live.render()
    copies = [k["on_click"] for n, _a, k in rec.calls
              if n == "button" and k.get("icon") == "content_copy"]
    assert copies, "no copy button for the grant command"
    first = next(tcp.grant_command(r) for r in ac.census_overview(live.vault)["rows"]
                 if tcp.grant_command(r))
    _run(copies[0]())
    assert json.dumps(first) in rec.scripts[0], rec.scripts
    assert rec.notes[-1] == (tcp.CENSUS_COMMAND_COPIED, "positive")


def test_a_refused_copy_never_says_copied(tmp_path, monkeypatch):
    rec = _RecordingUI(browser_accepts=False)
    monkeypatch.setattr(tcp, "ui", rec)
    _run(tcp._copy_command("systemu census grant path_clis"))
    assert [m for m, _t in rec.notes] == [tcp.CENSUS_COMMAND_NOT_COPIED]


# ── withdraw / pause / resume ────────────────────────────────────────────────

def test_withdraw_is_one_click_and_purges_what_it_found(live, monkeypatch):
    monkeypatch.setitem(ac.PROBES, "cloud_sync_roots",
                        (lambda limit, budget: ["D:/sync/a"], "cloud_sync_root"))
    ac.grant_category(live.vault, "cloud_sync_roots")
    ac.run_census(live.vault)
    assert FactStore(live.vault).all_facts()

    rec = live.render()
    _run(rec.handler(text=tcp._ACTION_LABELS["withdraw"])())
    assert cc.CensusConsentStore(live.root).is_granted("cloud_sync_roots") is False
    assert FactStore(live.vault).all_facts() == []
    assert any("Facts deleted: 1" in m for m, _t in rec.notes)
    assert live.reloads == [1]
    assert ac.run_census(live.vault, min_interval_seconds=0)["skipped"][
        "cloud_sync_roots"] == "not_consented"


def test_pause_keeps_the_facts_and_resume_scans_again(live, monkeypatch):
    monkeypatch.setitem(ac.PROBES, "cloud_sync_roots",
                        (lambda limit, budget: ["D:/sync/a"], "cloud_sync_root"))
    ac.grant_category(live.vault, "cloud_sync_roots")
    ac.run_census(live.vault)

    _run(live.render().handler(text="Pause")())
    assert cc.CensusConsentStore(live.root).is_active("cloud_sync_roots") is False
    assert len(FactStore(live.vault).all_facts()) == 1

    _run(live.render().handler(text="Resume")())
    assert ac.run_census(live.vault, min_interval_seconds=0)["scanned"] == \
        ["cloud_sync_roots"]


def test_resume_cannot_bring_back_a_withdrawn_category(live):
    """Resume is NOT a grant. After a withdrawal there is no row to un-pause: the store
    records nothing, the category stays off, and the operator is told so."""
    ac.grant_category(live.vault, "cloud_sync_roots")
    ac.revoke_category(live.vault, "cloud_sync_roots")
    before = (live.root / "census_consent.json").read_bytes()
    _run(live.controls["resume"]({"category": "cloud_sync_roots"}))
    assert (live.root / "census_consent.json").read_bytes() == before
    assert cc.CensusConsentStore(live.root).is_granted("cloud_sync_roots") is False
    assert any("not granted" in m for m, _t in live.rec.notes)


# ── which buttons a row gets ─────────────────────────────────────────────────

def test_row_actions_never_include_grant():
    ok = {"controls_shipped": True}
    assert tcp.row_actions({**ok, "state": ac.STATE_NOT_GRANTED}) == []
    assert tcp.row_actions({**ok, "state": ac.STATE_GRANTED}) == ["pause", "withdraw"]
    assert tcp.row_actions({**ok, "state": ac.STATE_PAUSED}) == ["resume", "withdraw"]
    assert tcp.row_actions({**ok, "state": ac.STATE_UNAVAILABLE}) == []
    assert tcp.row_actions({**ok, "state": ac.STATE_UNAVAILABLE,
                            "granted_at": "2026-10-19T00:00:00+00:00"}) == ["withdraw"]
    assert tcp.row_actions({"state": ac.STATE_GRANTED}) == []


# ── reachability ─────────────────────────────────────────────────────────────

def test_the_live_panel_builds_its_controls():
    """Delete the controls wiring in ``build_census_panel`` and the panel is display-only
    again; this goes red."""
    src = inspect.getsource(tcp.build_census_panel)
    assert "_census_panel_controls(" in src
    assert 'view["actions"]' in src


def test_every_change_runs_off_the_ui_thread():
    src = inspect.getsource(tcp._census_panel_controls)
    for verb in ("revoke_category", "pause_category"):
        assert f"run.io_bound({verb}" in src, verb


def test_the_new_copy_is_ascii():
    for name in ("CENSUS_GRANT_LEAD", "CENSUS_COMMAND_COPIED", "CENSUS_COMMAND_NOT_COPIED"):
        assert getattr(tcp, name).isascii(), name
    for label in tcp._ACTION_LABELS.values():
        assert label.isascii()
