"""W12-B5 — responsiveness + first-run polish (audit findings F1–F4).

F1 (the big one): EVERY page build took 1.2–2.0 s server-side. Profiling
showed the health banner's `_count_systemu_daemons` runs a FULL psutil
process scan (~1 s on Windows) on every render. The count changes rarely —
a TTL cache makes the banner effectively free without losing the warning.

F2: the sidebar footer hardcoded "localhost:8765" via an env var that
run_dashboard never stamped — wrong on any custom port.
F3: detect_timezone returned Windows names ("India Standard Time"); IANA
("Asia/Kolkata") via tzlocal (ships with apscheduler).
F4: the welcome preset select rendered as a tiny unlabeled box.
"""
from __future__ import annotations

import inspect


class TestDaemonScanCache:
    def setup_method(self):
        from systemu.interface.components import health_banner as hb
        hb._daemon_probe_cache["ts"] = -1e9
        hb._daemon_probe_cache["count"] = 0

    def test_scan_runs_once_within_ttl(self, monkeypatch):
        from systemu.interface.components import health_banner as hb
        calls = []
        monkeypatch.setattr(hb, "_scan_daemon_count",
                            lambda: calls.append(1) or 3)
        assert hb._count_systemu_daemons(_now=0.0) == 3
        assert hb._count_systemu_daemons(_now=5.0) == 3
        assert hb._count_systemu_daemons(_now=19.0) == 3
        assert len(calls) == 1, "page builds must not re-scan within the TTL"

    def test_scan_refreshes_after_ttl(self, monkeypatch):
        from systemu.interface.components import health_banner as hb
        values = iter([5, 1])
        monkeypatch.setattr(hb, "_scan_daemon_count", lambda: next(values))
        assert hb._count_systemu_daemons(_now=0.0) == 5
        assert hb._count_systemu_daemons(_now=25.0) == 1

    def test_existing_patch_contract_still_works(self, monkeypatch):
        """test_health_banner.py patches _count_systemu_daemons directly —
        the cached wrapper must keep that seam."""
        from systemu.interface.components import health_banner as hb
        monkeypatch.setattr(hb, "_count_systemu_daemons", lambda: 2)
        state = hb.build_health_state()
        assert any("2 systemu daemon" in i.message for i in state.issues)


class TestFooterPort:
    def test_run_dashboard_stamps_the_real_port(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard.run_dashboard)
        assert 'environ["SYSTEMU_DASHBOARD_PORT"]' in src, \
            "the sidebar footer reads this env — custom ports showed :8765"


class TestTimezoneIana:
    def test_detect_timezone_prefers_iana(self):
        import pytest
        pytest.importorskip("tzlocal")
        from systemu.interface.pages.welcome import detect_timezone
        tz = detect_timezone()
        assert "/" in tz, f"expected IANA name (got {tz!r})"

    def test_never_raises_without_tzlocal(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _no_tzlocal(name, *a, **k):
            if name == "tzlocal":
                raise ImportError("simulated")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_tzlocal)
        from systemu.interface.pages.welcome import detect_timezone
        assert isinstance(detect_timezone(), str) and detect_timezone()


class TestPresetSelectVisible:
    def test_preset_select_is_full_width_labeled(self):
        from systemu.interface.pages import welcome
        src = inspect.getsource(welcome.build_welcome_page)
        sel_line = next(l for l in src.splitlines() if "preset_select" in l)
        assert "s-input-full" in sel_line and 'label="Preset"' in sel_line
