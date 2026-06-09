import pytest
from systemu.interface.command import dispatch as dmod
from systemu.interface.command.result import CommandResult, CommandStatus


def test_dispatch_in_process_runs_verb(monkeypatch):
    def _fake_verb(args, *, vault):
        return CommandResult(status=CommandStatus.OK, summary=f"ran {args}")
    monkeypatch.setitem(dmod._IN_PROCESS_VERBS, "tools enable", _fake_verb)
    result = dmod.dispatch("tools enable", ["tool_a"], vault=object())
    assert result.status == CommandStatus.OK
    assert "tool_a" in result.summary


def test_dispatch_unknown_verb_is_error():
    result = dmod.dispatch("totally bogus", [], vault=object())
    assert result.status == CommandStatus.ERROR
    assert "unknown" in result.summary.lower()


def test_dispatch_stream_attaches_job_id_as_stream_ref(monkeypatch):
    class _FakeJob:
        id = "job_abcd"
    class _FakeJM:
        def start_job(self, name, job_type, cmd, cwd, **kw):
            assert cmd[1:4] == ["-m", "sharing_on", "tools"]
            assert "enable" in cmd
            return _FakeJob()
    monkeypatch.setattr(dmod, "_job_manager", lambda: _FakeJM())
    result = dmod.dispatch("tools enable", ["tool_a"], cwd="/proj", stream=True)
    assert result.status == CommandStatus.OK
    assert result.stream_ref == "job_abcd"


def test_dispatch_stream_dedup_key_passed_through(monkeypatch):
    seen = {}
    class _FakeJob:
        id = "job_x"
    class _FakeJM:
        def start_job(self, name, job_type, cmd, cwd, **kw):
            seen.update(kw)
            return _FakeJob()
    monkeypatch.setattr(dmod, "_job_manager", lambda: _FakeJM())
    dmod.dispatch("tools enable", ["tool_a"], cwd="/proj", stream=True,
                  dedup_key="enable:tool_a")
    assert seen.get("dedup_key") == "enable:tool_a"


def test_dispatch_passes_job_type(monkeypatch):
    seen = {}
    class _FakeJob: id = "job_jt"
    class _FakeJM:
        def start_job(self, name, job_type, cmd, cwd, **kw):
            seen["job_type"] = job_type
            return _FakeJob()
    monkeypatch.setattr(dmod, "_job_manager", lambda: _FakeJM())
    dmod.dispatch("evolve run", [], cwd="/p", stream=True, job_type="evolve")
    assert seen["job_type"] == "evolve"


def test_tools_enable_is_registered_and_adapts_args(monkeypatch):
    from systemu.interface.command import verbs
    from systemu.interface.command.result import CommandResult, CommandStatus
    seen = {}
    def _fake(tool_id, *, vault):
        seen["tool_id"] = tool_id
        return CommandResult(status=CommandStatus.OK, summary="ok", data={"tool_id": tool_id})
    monkeypatch.setattr(verbs, "tools_enable", _fake)
    assert "tools enable" in dmod._IN_PROCESS_VERBS
    result = dmod.dispatch("tools enable", ["tool_z"], vault=object())
    assert result.status == CommandStatus.OK
    assert seen["tool_id"] == "tool_z"   # list-arg -> scalar adapter works


def test_tools_enable_adapter_requires_tool_id():
    from systemu.interface.command.result import CommandStatus
    result = dmod.dispatch("tools enable", [], vault=object())
    assert result.status == CommandStatus.ERROR
