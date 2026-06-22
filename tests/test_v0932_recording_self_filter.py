"""v0.9.32 Item 2 — recorder self-filter (origin plumbing + layered drop)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest


# ── B.1: origin env is set at the single spawn site (dispatch) ────────────────
class TestDashboardOriginPlumbing:
    def test_dispatch_sets_origin_env_on_streamed_record_job(self, monkeypatch):
        """dispatch(..., stream=True) must export SYSTEMU_DASHBOARD_ORIGIN into
        os.environ before spawning, so JobManager's os.environ.copy() child
        inherits it. We set the port in env; dispatch derives the origin."""
        from systemu.interface.command import dispatch as dispatch_mod

        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        monkeypatch.setenv("SYSTEMU_DASHBOARD_HOST", "127.0.0.1")
        monkeypatch.setenv("SYSTEMU_DASHBOARD_PORT", "8765")

        captured = {}

        class _FakeJob:
            id = "job-xyz"

        class _FakeJM:
            def start_job(self, **kwargs):
                # snapshot the env the child would inherit
                captured["origin"] = os.environ.get("SYSTEMU_DASHBOARD_ORIGIN")
                return _FakeJob()

        monkeypatch.setattr(dispatch_mod, "_job_manager", lambda: _FakeJM())

        res = dispatch_mod.dispatch("record", ["--name", "t"], stream=True,
                                    job_type="capture")
        assert res.stream_ref == "job-xyz"
        assert captured["origin"] == "http://127.0.0.1:8765"
        # and it persists in the parent env for the child's os.environ.copy()
        assert os.environ.get("SYSTEMU_DASHBOARD_ORIGIN") == "http://127.0.0.1:8765"

    def test_dispatch_origin_falls_back_to_localhost_when_host_unset(self, monkeypatch):
        from systemu.interface.command import dispatch as dispatch_mod

        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        monkeypatch.delenv("SYSTEMU_DASHBOARD_HOST", raising=False)
        monkeypatch.setenv("SYSTEMU_DASHBOARD_PORT", "9999")

        captured = {}

        class _FakeJob:
            id = "j"

        class _FakeJM:
            def start_job(self, **kwargs):
                captured["origin"] = os.environ.get("SYSTEMU_DASHBOARD_ORIGIN")
                return _FakeJob()

        monkeypatch.setattr(dispatch_mod, "_job_manager", lambda: _FakeJM())
        dispatch_mod.dispatch("record", [], stream=True, job_type="capture")
        # 0.0.0.0 bind is rewritten to localhost for a usable origin
        assert captured["origin"] == "http://localhost:9999"


# ── B.2: Layer 1 — web-extension origin drop ──────────────────────────────────
class TestLayer1OriginDrop:
    def _make_collector(self):
        from sharing_on.collectors.web_extension import WebExtensionCollector

        col = WebExtensionCollector.__new__(WebExtensionCollector)
        emitted = []
        col.emit = emitted.append  # type: ignore[attr-defined]
        return col, emitted

    def test_dashboard_origin_event_is_dropped(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://127.0.0.1:8765")
        col, emitted = self._make_collector()
        col.handle_extension_event({
            "action": "mouse_click",
            "url": "http://127.0.0.1:8765/",
            "tab_title": "Systemu Dashboard",
            "element_text": "Stop & Analyze",
        })
        assert emitted == []   # default-deny our own UI

    def test_dashboard_origin_match_ignores_path_and_query(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://127.0.0.1:8765")
        col, emitted = self._make_collector()
        col.handle_extension_event({
            "action": "mouse_click",
            "url": "http://127.0.0.1:8765/inbox?x=1",
            "tab_title": "Systemu Dashboard",
        })
        assert emitted == []

    def test_real_task_url_survives(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://127.0.0.1:8765")
        col, emitted = self._make_collector()
        col.handle_extension_event({
            "action": "mouse_click",
            "url": "https://github.com/foo/bar",
            "tab_title": "GitHub",
            "element_text": "New issue",
        })
        assert len(emitted) == 1
        assert emitted[0].data["url"] == "https://github.com/foo/bar"

    def test_no_origin_configured_lets_everything_through(self, monkeypatch):
        # Layers 2+3 cover the extension-absent / origin-unset case.
        monkeypatch.delenv("SYSTEMU_DASHBOARD_ORIGIN", raising=False)
        col, emitted = self._make_collector()
        col.handle_extension_event({
            "action": "mouse_click",
            "url": "http://127.0.0.1:8765/",
            "tab_title": "Systemu Dashboard",
        })
        assert len(emitted) == 1

    def test_localhost_url_matches_127_origin_and_is_dropped(self, monkeypatch):
        """FIX 2A: the dashboard stamps 127.0.0.1 but operators open localhost.
        The loopback aliases must be normalized so Layer 1 still fires."""
        monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://127.0.0.1:8765")
        col, emitted = self._make_collector()
        col.handle_extension_event({
            "action": "mouse_click",
            "url": "http://localhost:8765/inbox",
            "tab_title": "Systemu Dashboard",
            "element_text": "Stop & Analyze",
        })
        assert emitted == []   # localhost == 127.0.0.1 — dropped


# ── B.2b: FIX 2A — _same_origin loopback-alias normalization ──────────────────
class TestSameOriginLoopback:
    def test_localhost_equals_127_loopback(self):
        from sharing_on.collectors.web_extension import _same_origin
        assert _same_origin("http://localhost:8765/", "http://127.0.0.1:8765") is True
        assert _same_origin("http://127.0.0.1:8765/x", "http://localhost:8765") is True
        assert _same_origin("http://[::1]:8765/", "http://localhost:8765") is True
        assert _same_origin("http://0.0.0.0:8765/", "http://127.0.0.1:8765") is True

    def test_loopback_port_must_still_match(self):
        from sharing_on.collectors.web_extension import _same_origin
        # same loopback bucket but DIFFERENT port → not the same origin
        assert _same_origin("http://localhost:9999/", "http://127.0.0.1:8765") is False

    def test_loopback_scheme_must_still_match(self):
        from sharing_on.collectors.web_extension import _same_origin
        assert _same_origin("https://localhost:8765/", "http://127.0.0.1:8765") is False

    def test_genuinely_different_host_not_dropped(self):
        from sharing_on.collectors.web_extension import _same_origin
        assert _same_origin("https://github.com:8765/", "http://127.0.0.1:8765") is False
        # a real remote host that is NOT loopback must compare by hostname
        assert _same_origin("http://example.com:8765/", "http://localhost:8765") is False

    def test_unset_origin_is_noop(self):
        from sharing_on.collectors.web_extension import _same_origin
        assert _same_origin("http://localhost:8765/", "") is False
        assert _same_origin("", "http://127.0.0.1:8765") is False

    def test_two_non_loopback_hosts_compared_normally(self):
        from sharing_on.collectors.web_extension import _same_origin
        assert _same_origin("https://app.example.com/", "https://app.example.com") is True
        assert _same_origin("https://a.example.com/", "https://b.example.com") is False


# ── B.3: Layer 2 — unifier label/title strip ──────────────────────────────────
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory


def _click(ts=None, *, app="Chrome", title="", element_text="", url=""):
    return CaptureEvent(
        category=EventCategory.INTERACTION,
        action=EventAction.MOUSE_CLICK,
        timestamp=ts or datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        application=app,
        window_title=title,
        data={"element_text": element_text, "url": url, "element_tag": "button"},
    )


class TestLayer2LabelStrip:
    def test_stop_and_analyze_text_dropped(self):
        from sharing_on.analyzer.unifier import _filter_self_noise
        evts = [_click(element_text="Stop & Analyze")]
        assert _filter_self_noise(evts) == []

    def test_cancel_and_trash_text_dropped(self):
        from sharing_on.analyzer.unifier import _filter_self_noise
        evts = [_click(element_text="Cancel & Trash")]
        assert _filter_self_noise(evts) == []

    def test_systemu_dashboard_window_title_dropped(self):
        from sharing_on.analyzer.unifier import _filter_self_noise
        evts = [_click(title="Systemu Dashboard", element_text="Inbox")]
        assert _filter_self_noise(evts) == []

    def test_unrelated_interaction_survives(self):
        from sharing_on.analyzer.unifier import _filter_self_noise
        evts = [_click(title="GitHub", element_text="New issue")]
        out = _filter_self_noise(evts)
        assert len(out) == 1
        assert out[0].data["element_text"] == "New issue"


# ── B.4: Layer 3 — trailing stop-click timestamp trim ─────────────────────────
class TestLayer3TrailingTrim:
    def _base(self):
        return datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_click_just_before_stop_is_trimmed(self):
        from sharing_on.analyzer.unifier import unify_events
        t0 = self._base()
        stop_ts = t0 + timedelta(seconds=10)
        # A bare raw-hook click (no text/title) 200ms before stop = the stop click
        raw_stop = _click(ts=stop_ts - timedelta(milliseconds=200),
                          app="", title="", element_text="", url="")
        legit = _click(ts=t0, app="Chrome", title="GitHub", element_text="New issue")
        out = unify_events([legit, raw_stop], stop_ts=stop_ts)
        texts = [e.data.get("element_text") for e in out
                 if e.category == EventCategory.INTERACTION]
        assert "New issue" in texts
        # the trailing near-stop click is gone
        assert raw_stop.event_id not in {e.event_id for e in out}

    def test_legit_click_5s_before_stop_survives(self):
        from sharing_on.analyzer.unifier import unify_events
        t0 = self._base()
        stop_ts = t0 + timedelta(seconds=10)
        legit_last = _click(ts=stop_ts - timedelta(seconds=5),
                            app="Chrome", title="GitHub", element_text="Save")
        out = unify_events([legit_last], stop_ts=stop_ts)
        assert legit_last.event_id in {e.event_id for e in out}

    def test_keystroke_in_window_is_not_trimmed(self):
        """Only MOUSE_CLICK is trimmed in the window — a final keystroke stays."""
        from sharing_on.analyzer.unifier import unify_events
        t0 = self._base()
        stop_ts = t0 + timedelta(seconds=10)
        key = CaptureEvent(
            category=EventCategory.INTERACTION,
            action=EventAction.KEY_PRESS,
            timestamp=stop_ts - timedelta(milliseconds=100),
            application="Editor", window_title="notes.txt",
            data={"element_text": "x"},
        )
        out = unify_events([key], stop_ts=stop_ts)
        assert key.event_id in {e.event_id for e in out}

    def test_no_stop_ts_is_a_noop(self):
        from sharing_on.analyzer.unifier import unify_events
        t0 = self._base()
        c = _click(ts=t0, app="Chrome", title="GitHub", element_text="New issue")
        out = unify_events([c])   # backward-compatible: no stop_ts
        assert len(out) == 1


# ── B.4b: FIX 2B/2C/2D — robust, bare-only trim with widened window ───────────
class TestLayer3TrimHardening:
    def _base(self):
        return datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    # FIX 2B — tz-naive stop_ts (cross-version data) must not crash and must trim.
    def test_tznaive_stop_ts_does_not_crash_and_trims(self):
        from sharing_on.analyzer.unifier import _trim_trailing_stop_clicks
        t0 = self._base()
        stop_ts_naive = datetime(2026, 6, 15, 12, 0, 10)  # tz-naive, == t0+10s UTC
        bare_stop = _click(ts=t0 + timedelta(seconds=10) - timedelta(milliseconds=200),
                           app="", title="", element_text="", url="")
        out = _trim_trailing_stop_clicks([bare_stop], stop_ts_naive)
        assert out == []   # treated as UTC; bare stop click trimmed

    def test_tznaive_stop_ts_keeps_earlier_legit_event(self):
        from sharing_on.analyzer.unifier import _trim_trailing_stop_clicks
        t0 = self._base()
        stop_ts_naive = datetime(2026, 6, 15, 12, 0, 10)
        legit = _click(ts=t0, app="Chrome", title="GitHub", element_text="New issue")
        out = _trim_trailing_stop_clicks([legit], stop_ts_naive)
        assert len(out) == 1

    # FIX 2C — only BARE raw-hook clicks are trimmed in the window.
    def test_bare_click_in_window_is_trimmed(self):
        from sharing_on.analyzer.unifier import _trim_trailing_stop_clicks, STOP_CLICK_TRIM_WINDOW
        stop_ts = self._base() + timedelta(seconds=10)
        bare = _click(ts=stop_ts - timedelta(milliseconds=200),
                      app="", title="", element_text="", url="")
        out = _trim_trailing_stop_clicks([bare], stop_ts)
        assert out == []

    def test_enriched_click_in_window_survives(self):
        """An enriched legit final click (element_text/url) inside the trim
        window must SURVIVE — only the contextless raw-hook stop click goes."""
        from sharing_on.analyzer.unifier import _trim_trailing_stop_clicks
        stop_ts = self._base() + timedelta(seconds=10)
        enriched = _click(ts=stop_ts - timedelta(milliseconds=200),
                          app="Chrome", title="GitHub",
                          element_text="Save", url="https://github.com/x")
        out = _trim_trailing_stop_clicks([enriched], stop_ts)
        assert len(out) == 1
        assert out[0].data["element_text"] == "Save"

    def test_click_with_only_element_name_in_window_survives(self):
        """element_name alone (introspector enrichment) makes a click non-bare."""
        from sharing_on.analyzer.unifier import _trim_trailing_stop_clicks
        stop_ts = self._base() + timedelta(seconds=10)
        ev = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=stop_ts - timedelta(milliseconds=100),
            application="Chrome", window_title="App",
            data={"element_name": "Submit"},
        )
        out = _trim_trailing_stop_clicks([ev], stop_ts)
        assert len(out) == 1

    # FIX 2D — widened window absorbs the end_time poll/IPC lag (~0.5-1s).
    def test_bare_stop_click_1s_after_last_action_is_trimmed(self):
        from sharing_on.analyzer.unifier import unify_events
        t0 = self._base()
        # end_time lags the real click; the bare stop click lands ~1s after the
        # last legit action and still inside the widened window.
        stop_ts = t0 + timedelta(seconds=10)
        bare_stop = _click(ts=t0 + timedelta(seconds=9),  # ~1s before end_time
                           app="", title="", element_text="", url="")
        legit = _click(ts=t0, app="Chrome", title="GitHub", element_text="New issue")
        out = unify_events([legit, bare_stop], stop_ts=stop_ts)
        ids = {e.event_id for e in out}
        assert bare_stop.event_id not in ids
        assert legit.event_id in ids

    def test_legit_enriched_action_well_before_stop_survives(self):
        from sharing_on.analyzer.unifier import unify_events
        t0 = self._base()
        stop_ts = t0 + timedelta(seconds=10)
        legit = _click(ts=t0 + timedelta(seconds=5),  # well outside ~1.5s window
                       app="Chrome", title="GitHub", element_text="Save",
                       url="https://github.com/x")
        out = unify_events([legit], stop_ts=stop_ts)
        assert legit.event_id in {e.event_id for e in out}

    def test_window_widened_to_absorb_poll_lag(self):
        from sharing_on.analyzer.unifier import STOP_CLICK_TRIM_WINDOW
        # Must be wide enough to cover the live-display poll (0.5s) + IPC lag.
        assert STOP_CLICK_TRIM_WINDOW >= timedelta(milliseconds=1500)


def test_stop_ts_holder_removed_from_cli():
    """FIX 2D: the dead SIGINT stop_ts_holder must be gone — the trim is
    anchored to session.end_time, not the captured SIGINT timestamp."""
    import inspect
    from sharing_on import cli
    src = inspect.getsource(cli)
    assert "stop_ts_holder" not in src


# ── B.5: Integration — all 3 leaked representations of the stop click ─────────
class TestStopClickIntegration:
    def test_all_three_representations_produce_zero_stop_steps(self):
        """A synthetic stream containing the raw-hook click, the introspector-
        enriched click, and the web-extension DOM click of the SAME 'Stop &
        Analyze' press → unify_events(..., stop_ts=...) leaves zero stop-click
        interactions. Layers 1/2/3 together, exercised through the public API."""
        from sharing_on.analyzer.unifier import unify_events

        t0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        stop_ts = t0 + timedelta(seconds=30)
        click_ts = stop_ts - timedelta(milliseconds=150)  # the physical stop press

        # Rep #1: raw input-hook click — coords only, no text/title.
        rep1 = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=click_ts, application="", window_title="",
            data={"x": 640, "y": 400},
        )
        # Rep #2: introspector-enriched — browser window title + element name.
        rep2 = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=click_ts + timedelta(milliseconds=20),
            application="Chrome", window_title="Systemu Dashboard",
            data={"element_name": "Stop & Analyze", "element_text": "Stop & Analyze"},
        )
        # Rep #3: web-extension DOM click — element_text="Stop & Analyze".
        rep3 = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=click_ts + timedelta(milliseconds=40),
            application="Chrome/Edge Web Browser", window_title="Systemu Dashboard",
            data={"element_text": "Stop & Analyze", "url": "http://127.0.0.1:8765/"},
        )
        # A genuine earlier task action that MUST survive.
        legit = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=t0, application="Chrome", window_title="GitHub",
            data={"element_text": "New issue", "url": "https://github.com/x"},
        )

        out = unify_events([legit, rep1, rep2, rep3], stop_ts=stop_ts)

        interactions = [e for e in out if e.category == EventCategory.INTERACTION]
        # No surviving interaction references the stop control or its time window.
        for e in interactions:
            assert (e.data.get("element_text") or "") not in {"Stop & Analyze", "Cancel & Trash"}
            assert "Systemu Dashboard" not in (e.window_title or "")
            assert not (stop_ts - timedelta(milliseconds=800) <= e.timestamp <= stop_ts)
        # The legit task action is preserved.
        assert any(e.data.get("element_text") == "New issue" for e in interactions)

    def test_extension_absent_still_zero_stop_steps_via_layers_2_3(self):
        """If the Chrome extension is absent (no rep #3) AND origin env unset,
        Layers 2+3 alone still remove reps #1 and #2."""
        from sharing_on.analyzer.unifier import unify_events
        t0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        stop_ts = t0 + timedelta(seconds=30)
        click_ts = stop_ts - timedelta(milliseconds=150)
        rep1 = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=click_ts, application="", window_title="", data={"x": 1, "y": 2},
        )
        rep2 = CaptureEvent(
            category=EventCategory.INTERACTION, action=EventAction.MOUSE_CLICK,
            timestamp=click_ts + timedelta(milliseconds=20),
            application="Chrome", window_title="Systemu Dashboard",
            data={"element_text": "Stop & Analyze"},
        )
        out = unify_events([rep1, rep2], stop_ts=stop_ts)
        interactions = [e for e in out if e.category == EventCategory.INTERACTION]
        assert interactions == []
