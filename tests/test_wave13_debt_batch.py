"""W13.7 — debt batch: port fail-fast + alembic stamp fallback."""
from __future__ import annotations

import inspect
import socket
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TestPortFailFast:
    def test_free_port_probes_true(self):
        from systemu.scheduler.daemon import _dashboard_port_free
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()
        assert _dashboard_port_free(free_port, host="127.0.0.1") is True

    def test_taken_port_probes_false(self):
        from systemu.scheduler.daemon import _dashboard_port_free
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        try:
            taken = s.getsockname()[1]
            assert _dashboard_port_free(taken, host="127.0.0.1") is False
        finally:
            s.close()

    def test_daemon_refuses_to_race_the_port(self):
        from systemu.scheduler import daemon
        src = inspect.getsource(daemon)
        assert "_dashboard_port_free(port)" in src
        assert "Refusing to start" in src, \
            "the race loser must NOT keep running headless (wrong-vault hazard)"


class TestAlembicStampFallback:
    def test_entrypoint_stamps_head_on_upgrade_failure(self):
        text = (REPO / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        assert "alembic stamp head" in text, \
            "without the stamp, future migrations fail forever after a fallback"
