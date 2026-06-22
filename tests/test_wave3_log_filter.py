"""W3.1 — the benign 'parent slot deleted' timer record is suppressed; others survive."""
import logging

from systemu.interface.log_filters import (
    DropParentSlotDeleted,
    install_nicegui_log_filters,
)


def _record(msg, exc=None):
    return logging.LogRecord(
        "nicegui", logging.ERROR, __file__, 1, msg, None,
        (type(exc), exc, None) if exc else None,
    )


class TestFilter:
    def test_drops_by_message(self):
        assert DropParentSlotDeleted().filter(
            _record("The parent slot of the element has been deleted.")) is False

    def test_drops_via_exc_info(self):
        rec = _record("background task failed",
                      RuntimeError("The parent slot of the element has been deleted."))
        assert DropParentSlotDeleted().filter(rec) is False

    def test_keeps_other_messages(self):
        assert DropParentSlotDeleted().filter(_record("some real dashboard error")) is True

    def test_keeps_other_exceptions(self):
        assert DropParentSlotDeleted().filter(
            _record("boom", ValueError("kaboom"))) is True


class TestInstall:
    def test_idempotent(self):
        lg = logging.getLogger("nicegui")
        lg.filters[:] = [f for f in lg.filters if not isinstance(f, DropParentSlotDeleted)]
        install_nicegui_log_filters()
        install_nicegui_log_filters()
        assert sum(isinstance(f, DropParentSlotDeleted) for f in lg.filters) == 1

    def test_installed_filter_drops_record_end_to_end(self):
        lg = logging.getLogger("nicegui")
        lg.filters[:] = [f for f in lg.filters if not isinstance(f, DropParentSlotDeleted)]
        captured = []
        h = logging.Handler()
        h.emit = lambda rec: captured.append(rec.getMessage())
        lg.addHandler(h)
        try:
            install_nicegui_log_filters()
            lg.error("The parent slot of the element has been deleted.")   # dropped
            lg.error("a genuine error")                                    # kept
        finally:
            lg.removeHandler(h)
            lg.filters[:] = [f for f in lg.filters if not isinstance(f, DropParentSlotDeleted)]
        assert "a genuine error" in captured
        assert all("parent slot" not in m for m in captured)
