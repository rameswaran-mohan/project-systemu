"""W3.3 — a silent non-file backend downgrade is surfaced loudly.

AppState.create falls back to the file vault when a requested backend
(sqlite/postgres/parallel) can't init. That used to be a log line only — a
prod box configured for postgres that couldn't reach its DB would quietly run
on a local file vault, splitting data with no visible signal. Now
_degraded_fallback stamps a marker and the health banner raises a danger issue.
"""
from types import SimpleNamespace

from systemu.interface.components.health_banner import build_health_state


class TestHealthBannerSurfacesDegradation:
    def test_degraded_marker_raises_danger_issue(self, monkeypatch):
        import systemu.interface.dashboard_state as ds
        fake = SimpleNamespace(storage_degraded={
            "requested": "postgres", "actual": "file", "reason": "connection refused"})
        monkeypatch.setattr(ds.AppState, "get", classmethod(lambda cls: fake))

        state = build_health_state(vault_dir=None)
        deg = [i for i in state.issues if "DEGRADED" in i.message]
        assert len(deg) == 1
        assert deg[0].severity == "danger"
        assert "postgres" in deg[0].message and "connection refused" in deg[0].message
        assert state.worst_severity == "danger"

    def test_no_marker_no_issue(self, monkeypatch):
        import systemu.interface.dashboard_state as ds
        fake = SimpleNamespace(storage_degraded=None)
        monkeypatch.setattr(ds.AppState, "get", classmethod(lambda cls: fake))
        state = build_health_state(vault_dir=None)
        assert not any("DEGRADED" in i.message for i in state.issues)

    def test_appstate_unavailable_is_safe(self, monkeypatch):
        import systemu.interface.dashboard_state as ds

        def _boom(cls):
            raise RuntimeError("AppState not initialised")
        monkeypatch.setattr(ds.AppState, "get", classmethod(_boom))
        # Must not raise even when AppState.get() blows up.
        state = build_health_state(vault_dir=None)
        assert not any("DEGRADED" in i.message for i in state.issues)


class TestDegradedFallbackStampsMarker:
    def test_fallback_stamps_marker_without_booting_a_backend(self, monkeypatch):
        import systemu.interface.dashboard_state as ds
        captured = {}
        sentinel = SimpleNamespace(storage_degraded=None)

        def _fake_file_backend(cls, config):
            captured["called"] = True
            return sentinel
        monkeypatch.setattr(ds.AppState, "_create_file_backend",
                            classmethod(_fake_file_backend))

        out = ds.AppState._degraded_fallback(
            config=object(), requested="postgres", reason=RuntimeError("boom"))
        assert out is sentinel
        assert captured.get("called") is True
        assert out.storage_degraded == {
            "requested": "postgres", "actual": "file", "reason": "boom"}


class TestFallbackSitesWired:
    def test_create_uses_degraded_fallback_not_silent(self):
        import inspect
        import systemu.interface.dashboard_state as ds
        src = inspect.getsource(ds.AppState)
        # Every non-file backend path routes failures through _degraded_fallback
        # (each call passes requested=); no bare silent fallback remains.
        assert src.count("_degraded_fallback(") >= 5   # 4 call sites + 1 def
        assert src.count("requested=") >= 4            # sqlite, postgres x2, parallel
