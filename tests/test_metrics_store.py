import os, pytest
from systemu.runtime.metrics_store import MetricsStore

def test_atomic_write_survives_crash(tmp_path, monkeypatch):
    s = MetricsStore(tmp_path)
    s.incr("denies")
    orig = os.replace
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        s.incr("denies")
    monkeypatch.setattr(os, "replace", orig)
    assert MetricsStore(tmp_path).snapshot()["denies"] == 1  # unchanged after the failed write

def test_defensive_read_on_corrupt(tmp_path):
    (tmp_path / "metrics.json").write_text("{not json", encoding="utf-8")
    assert MetricsStore(tmp_path).snapshot().get("denies", 0) == 0

def test_bulk_approve_window(tmp_path):
    s = MetricsStore(tmp_path)
    s.record_resolution(latency_ms=10, ts=100.0)
    s.record_resolution(latency_ms=10, ts=101.5)   # <2s -> a bulk event
    s.record_resolution(latency_ms=10, ts=104.6)   # >2s -> not
    assert s.snapshot()["bulk_approve_events"] == 1

def test_always_allow_and_denies(tmp_path):
    s = MetricsStore(tmp_path)
    s.record_resolution(latency_ms=5, ts=1.0, choice="Always allow")
    s.record_resolution(latency_ms=5, ts=2.0, choice="Deny")
    snap = s.snapshot()
    assert snap["always_allow_grants"] == 1 and snap["denies"] == 1
