"""/insights renders "Time to first task" from the funnel record.

Reachability, not a source scan: the REAL ``build_insights_page`` runs against a
recording ``ui`` (only the three unrelated tab-panel builders are stubbed), so
removing the line from the journey card - or the card from the page - turns a
NAMED test here red. The computation itself is pinned in
``tests/test_p2d_time_to_first_task.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class _FakeVault:
    def __init__(self, root):
        self.root = root


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return _FakeVault(root)


def _write(vault, **stamps) -> None:
    (Path(vault.root) / "funnel.json").write_text(json.dumps(stamps), encoding="utf-8")


# --- /insights shows it (reachability: the REAL page builder runs) ----------

class _Node:
    def __init__(self, rec, name):
        self._rec, self._name = rec, name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, _name):
        return lambda *a, **k: self


class _RecordingUI:
    def __init__(self):
        self.calls = []
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "label" and a]

    def expansion_captions(self):
        return [k.get("caption") for n, _a, k in self.calls if n == "expansion"]


def _render_insights(vault, monkeypatch) -> _RecordingUI:
    """Run the REAL ``build_insights_page`` against a recording ``ui``; only
    the three unrelated tab-panel builders are stubbed out."""
    from systemu.interface.components import live_events_pane
    from systemu.interface.dashboard_state import AppState
    from systemu.interface.pages import insights

    monkeypatch.setattr(AppState, "_instance", SimpleNamespace(vault=vault),
                        raising=False)
    rec = _RecordingUI()
    monkeypatch.setattr(insights, "ui", rec)
    for name in ("build_memory_consolidation_page", "build_flywheel_page",
                 "build_notifications_page"):
        monkeypatch.setattr(insights, name, lambda *a, **k: None)
    monkeypatch.setattr(live_events_pane, "build_supervisor_events_pane",
                        lambda *a, **k: None)
    fake = ModuleType("nicegui")
    fake.ui = rec
    monkeypatch.setitem(sys.modules, "nicegui", fake)

    insights.build_insights_page()
    return rec


def test_insights_shows_the_measured_time_to_first_task(vault, monkeypatch):
    _write(vault,
           setup_finished="2026-03-04T05:06:07+00:00",
           first_task_succeeded="2026-03-04T05:09:14+00:00")
    rec = _render_insights(vault, monkeypatch)

    assert "Time to first task: 3m 7s" in rec.expansion_captions(), (
        "the journey card no longer carries the time to first task")


def test_insights_shows_not_measured_for_an_old_date_only_record(vault, monkeypatch):
    _write(vault, setup_finished="2026-03-04", first_task_succeeded="2026-03-05")
    rec = _render_insights(vault, monkeypatch)

    assert "Time to first task: NOT MEASURED" in rec.expansion_captions()
    assert not any(c and "m " in c and "NOT" not in c
                   for c in rec.expansion_captions()), "a guessed duration rendered"


def test_insights_says_why_it_is_not_measured(vault, monkeypatch):
    from systemu.runtime import funnel

    _write(vault, setup_finished="2026-03-04T05:06:07+00:00")
    rec = _render_insights(vault, monkeypatch)

    reason = funnel.time_to_first_task(vault).reason
    assert any(reason in label for label in rec.labels()), (
        "NOT MEASURED with no reason on the page is a mystery, not a fact")
