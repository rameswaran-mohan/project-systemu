from systemu.interface.event_bus import EventBus


def test_streamed_dispatch_event_carries_stream_ref(monkeypatch):
    from systemu.interface.command import dispatch as dmod

    published = []
    bus = EventBus.get()
    bus.subscribe(lambda e: published.append(e), replay=False)

    class _FakeJob: id = "job_rail_1"
    class _FakeJM:
        def start_job(self, name, job_type, cmd, cwd, **kw):
            bus.publish({"category": "command", "message": f"dispatched {name}",
                         "stream_ref": "job_rail_1"})
            return _FakeJob()
    monkeypatch.setattr(dmod, "_job_manager", lambda: _FakeJM())

    result = dmod.dispatch("tools dry-run", ["tool_a"], cwd="/p", stream=True)
    assert result.stream_ref == "job_rail_1"
    assert any(e.get("stream_ref") == "job_rail_1" for e in published)


def test_rail_filters_events_by_stream_ref():
    from systemu.interface.components.right_rail import events_for_stream
    events = [{"stream_ref": "job_a", "message": "1"},
              {"stream_ref": "job_b", "message": "2"},
              {"message": "no-ref"}]
    got = events_for_stream(events, "job_a")
    assert [e["message"] for e in got] == ["1"]


# ── Step 1: jobs.py child-env carries the bridge file + stream ref for ALL jobs ──

def test_child_env_includes_bridge_and_stream_ref():
    """Every spawned job (not just execute) gets the bridge file + its job id
    as the stream ref, so its events flow back to the dashboard EventBus."""
    from systemu.interface.jobs import _child_env

    env = _child_env({"PRE_EXISTING": "kept"}, "/tmp/vault", "job_xyz")
    assert env["SYSTEMU_STREAM_REF"] == "job_xyz"
    assert env["SYSTEMU_EVENT_BRIDGE_FILE"].replace("\\", "/").endswith(
        "/tmp/vault/manual_events.jsonl"
    )
    # Base env is preserved (helper merges, does not clobber).
    assert env["PRE_EXISTING"] == "kept"


# ── Step 2: event_bridge_writer stamps stream_ref onto every mirrored event ──

def test_bridge_writer_stamps_stream_ref(tmp_path, monkeypatch):
    import json
    from systemu.interface.event_bridge_writer import install_bridge_writer

    monkeypatch.setenv("SYSTEMU_STREAM_REF", "job_ref_42")
    bridge_file = tmp_path / "manual_events.jsonl"

    unsubscribe = install_bridge_writer(str(bridge_file))
    try:
        EventBus.get().publish({"category": "command", "message": "hi"})
    finally:
        if callable(unsubscribe):
            unsubscribe()

    lines = [l for l in bridge_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines, "bridge writer wrote nothing"
    written = [json.loads(l) for l in lines]
    assert any(e.get("stream_ref") == "job_ref_42" for e in written)


def test_bridge_writer_preserves_existing_stream_ref(tmp_path, monkeypatch):
    import json
    from systemu.interface.event_bridge_writer import install_bridge_writer

    monkeypatch.setenv("SYSTEMU_STREAM_REF", "env_ref")
    bridge_file = tmp_path / "manual_events.jsonl"

    unsubscribe = install_bridge_writer(str(bridge_file))
    try:
        EventBus.get().publish(
            {"category": "command", "message": "tagged", "stream_ref": "own_ref"}
        )
    finally:
        if callable(unsubscribe):
            unsubscribe()

    written = [
        json.loads(l)
        for l in bridge_file.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    # An event that already carried a stream_ref keeps its own (not clobbered).
    assert any(e.get("stream_ref") == "own_ref" for e in written)
    assert not any(e.get("stream_ref") == "env_ref" for e in written)


# ── Step 3: minimal Live pane — pure parts + component guard ──

def test_live_runs_pane_pure_filter_and_format():
    from systemu.interface.components.right_rail import (
        events_for_stream,
        format_live_run_line,
    )

    events = [
        {"stream_ref": "run_1", "level": "INFO", "message": "started"},
        {"stream_ref": "run_2", "level": "ERROR", "message": "boom"},
        {"stream_ref": "run_1", "level": "SUCCESS", "message": "done"},
    ]
    only = events_for_stream(events, "run_1")
    assert [e["message"] for e in only] == ["started", "done"]

    line = format_live_run_line(only[0])
    assert "started" in line
    assert "INFO" in line


def test_live_runs_pane_exists_and_uses_safe_timer():
    import inspect
    from systemu.interface.components import right_rail

    assert hasattr(right_rail, "live_runs_pane")
    src = inspect.getsource(right_rail.live_runs_pane)
    # Liveness contract: schedule refresh via safe_timer, never a raw ui.timer.
    assert "safe_timer" in src
    assert "ui.timer(" not in src
    # Subscribes to the in-process EventBus.
    assert "EventBus" in src
    assert "subscribe" in src
