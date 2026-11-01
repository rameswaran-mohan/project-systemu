"""P23 -- the command gate scores, signs and shows the command that RUNS.

The defect: ``ToolSandbox._maybe_gate_command`` scored and signed only the raw
``command`` / ``cmd`` parameters, while the in-process registry path reconciles
parameter names onto ``run()``'s real signature (``ToolRegistry._reconcile_params``).
``{"shell": X, "timeout_seconds": 5}`` made the gate score and sign an EMPTY
command and post a card reading ``$ `` -- while ``run_cli_command`` ran X. One
"Always allow" of that empty card then covered every remapped command.

THE PROPERTY pinned here: the gate scores, signs and shows exactly the command
string that will execute, after every transformation the execution path applies,
on every path (in-process registry, subprocess, v2 code-registered, forge
dry-run). The witnesses:

  * the exact repro, in-process against the REAL registry and the SHIPPED tool
    body: it is gated on X, and the card, the signature and the decision context
    all carry X;
  * a planted standing approval for "" lets nothing run, on any path -- the gate
    refuses a command it cannot determine (a VALUE, never carded, never signed)
    and says in the log that the empty-command approval is void;
  * per tool x path, an approval planted for exactly X lets exactly X execute
    (recorded at ``subprocess.run`` in-process, by the spawned child's own argv on
    the subprocess and dry-run paths, by the handler on the v2 path);
  * the body changing between the gate and the call cannot make execution run a
    command the gate never scored: execution consumes the sealed value;
  * the residual (``launch_application`` was governed by the per-tool gate, whose
    card could hide the target the body spawns) -- a strict xfail until P26
    gated it per resolved target; it now passes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.approval.exceptions import PendingOperatorDecision
from systemu.runtime.command_approvals import CommandApprovalStore, command_signature

REPO = Path(__file__).resolve().parent.parent
VAULT = REPO / "systemu" / "vault"
IMPLS = VAULT / "tools" / "implementations"

#: Not read-only (``python -c`` is not on the allowlist), harmless if it ran.
CMD = 'python -c "print(23)"'
OTHER = 'python -c "print(42)"'

#: The shipped manifests' parameter schemas (Gate 2.5 validates against them).
SCHEMAS = {
    "run_cli_command": {
        "command": {"type": "string"},
        "timeout_seconds": {"type": "integer", "default": 30},
    },
    "run_command": {
        "command": {"type": "string"},
        "timeout": {"type": "integer", "default": 30},
        "cwd": {"type": "string", "default": ""},
    },
}

UNDETERMINABLE = "command_undeterminable"


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────

class _Vault:
    """The registry's vault: every tool enabled and dry-run-passed. ``hook`` runs
    on every lookup -- ``ToolRegistry.execute``'s first act, i.e. AFTER the gates."""

    def __init__(self, hook=None):
        self.hook = hook

    def find_tool_by_name(self, name):
        if self.hook is not None:
            self.hook(name)
        return SimpleNamespace(
            name=name, id="tool_" + name, version=1, enabled=True,
            parameters_schema=SCHEMAS.get(name, {}), dry_run_status="passed",
            requires_credentials=[], credential_refusals=[], dependencies=[],
            timeout_seconds=None)


def _sandbox(store, registry=None):
    from systemu.runtime.tool_sandbox import ToolSandbox
    return ToolSandbox(vault_root=VAULT, vault=object(), command_approvals=store,
                       registry=registry)


@pytest.fixture
def h(tmp_path, monkeypatch):
    """The real gate with four witnesses: ``ran`` (every ``subprocess.run`` the
    in-process body makes -- recorded, never spawned), ``posted`` (every card),
    ``events`` (every operator notification), and a ``Popen`` that fails loudly."""
    monkeypatch.chdir(tmp_path)
    ran, cwds = [], []

    def _fake_run(args, *a, **kw):
        ran.append(args if isinstance(args, str) else list(args))
        cwds.append(kw.get("cwd"))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    class _NoPopen:
        def __init__(self, args, *a, **k):
            raise AssertionError("sentinel: a process was spawned: {!r}".format(args))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "Popen", _NoPopen)

    posted = []

    class _Inbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, policy=None, context_extras=None,
                    **kw):
            posted.append(SimpleNamespace(d=descriptor, gate_type=gate_type,
                                          extras=dict(context_extras or {})))
            return "dec_p23"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _Inbox)
    events = []
    import systemu.interface.notifications as notifications
    monkeypatch.setattr(notifications, "log_event",
                        lambda *a, **k: events.append((a, k)))

    store = CommandApprovalStore(tmp_path / "command_approvals.json")

    def in_process(name, params, *, impl_dir=IMPLS, vault=None, **kw):
        from systemu.runtime.tool_registry import ToolRegistry
        reg = ToolRegistry(impl_dir, vault or _Vault())
        sb = _sandbox(store, registry=reg)
        return asyncio.run(sb.execute_tool(str(impl_dir / (name + ".py")), params, **kw))

    return SimpleNamespace(store=store, ran=ran, cwds=cwds, posted=posted,
                           events=events, tmp=tmp_path, in_process=in_process)


def _approve(store, command, cwd=""):
    assert store.approve(command_signature(command, cwd=cwd), command=command, cwd=cwd)


def _plant_empty_approvals(store, cwds=("",)):
    """A standing approval for the EMPTY command, as the old gate minted it from a
    ``$ `` card's "Always allow" (``command_gate_handler`` falls back to the dedup
    suffix, which is the empty command's signature)."""
    for cwd in cwds:
        assert store.approve(command_signature("", cwd=cwd), command="", cwd=cwd)


def _assert_refused_undeterminable(h, result):
    assert result.success is False, result
    assert (result.parsed or {}).get("error_type") == UNDETERMINABLE, result.parsed
    assert "Nothing was run" in (result.error or ""), result.error
    assert result.error.isascii()
    assert h.ran == [], "a command ran: {!r}".format(h.ran)
    assert h.posted == [], "an undeterminable command was offered as a card"


# ─────────────────────────────────────────────────────────────────────────────
# 1. The exact repro, in-process against the real registry and shipped body
# ─────────────────────────────────────────────────────────────────────────────

def test_repro_a_remapped_parameter_is_gated_on_the_command_that_runs(h):
    """``{"shell": X, "timeout_seconds": 5}``: the registry maps ``shell`` onto
    ``command``. The card, the signature, the decision context and the raised
    message all name X -- never the empty command."""
    with pytest.raises(PendingOperatorDecision) as ei:
        h.in_process("run_cli_command", {"shell": CMD, "timeout_seconds": 5})
    assert len(h.posted) == 1
    card = h.posted[0]
    sig = command_signature(CMD, cwd="")
    assert card.gate_type == "command"
    assert card.d.inspect.splitlines()[0] == "$ " + CMD
    assert "`" + CMD + "`" in card.d.what_approve_does
    assert card.d.dedup == "command:" + sig
    assert card.extras["command"] == CMD
    assert ei.value.dedup_key == "command:" + sig
    assert CMD in str(ei.value)
    assert h.ran == []


def test_repro_an_approval_of_the_real_command_runs_exactly_it(h):
    _approve(h.store, CMD)
    res = h.in_process("run_cli_command", {"shell": CMD, "timeout_seconds": 5})
    assert res.success, res.error
    assert res.gate["passed_via"] == "standing_approval"
    assert h.ran == [shlex.split(CMD)]
    assert h.posted == []


def test_repro_a_planted_empty_command_approval_does_not_cover_it(h):
    """The original exploit: an "Always allow" on the ``$ `` card. It must not
    let the remapped command run -- the call is gated on X instead."""
    _plant_empty_approvals(h.store)
    with pytest.raises(PendingOperatorDecision):
        h.in_process("run_cli_command", {"shell": CMD, "timeout_seconds": 5})
    assert h.ran == []
    assert [p.d.dedup for p in h.posted] == ["command:" + command_signature(CMD)]


# ─────────────────────────────────────────────────────────────────────────────
# 2. A command the gate cannot determine is REFUSED, and an empty-command
#    approval is void -- on every path
# ─────────────────────────────────────────────────────────────────────────────

_UNDETERMINABLE_IN_PROCESS = [
    pytest.param("run_cli_command", {"command": "", "timeout_seconds": 5}, id="cli_empty"),
    pytest.param("run_cli_command", {"command": "   ", "timeout_seconds": 5}, id="cli_blank"),
    pytest.param("run_cli_command", {"timeout_seconds": 5}, id="cli_absent"),
    pytest.param("run_cli_command", {"shell": CMD}, id="cli_two_unfilled_dropped"),
    pytest.param("run_cli_command", {"command": ["python", "-c", "1"]}, id="cli_list"),
    pytest.param("run_cli_command", {"command": 'python -c "unbalanced'},
                 id="cli_unsplittable"),
    pytest.param("run_cli_command", {"command": "python\x00 -c 1"}, id="cli_nul"),
    pytest.param("run_command", {"cmd": CMD}, id="run_command_cmd_only"),
    pytest.param("run_command", {"shell": CMD}, id="run_command_shell_only"),
    pytest.param("run_command", {"command": ""}, id="run_command_empty"),
    pytest.param("run_command", {"command": CMD, "cwd": ["C:/"]}, id="run_command_cwd_list"),
    pytest.param("run_command", {"command": CMD, "cwd": "C:/x\x00"}, id="run_command_cwd_nul"),
]


@pytest.mark.parametrize("name,params", _UNDETERMINABLE_IN_PROCESS)
def test_an_undeterminable_command_is_refused_in_process(h, name, params):
    _plant_empty_approvals(h.store, cwds=("", "C:/x\x00"))
    result = h.in_process(name, params)
    _assert_refused_undeterminable(h, result)


def test_the_refusal_says_an_empty_command_approval_is_void(h, caplog):
    _plant_empty_approvals(h.store)
    with caplog.at_level(logging.WARNING, logger="systemu.runtime.tool_sandbox"):
        result = h.in_process("run_cli_command", {"command": "", "timeout_seconds": 5})
    _assert_refused_undeterminable(h, result)
    void = [r.getMessage() for r in caplog.records if "VOID" in r.getMessage()]
    assert void, [r.getMessage() for r in caplog.records]
    assert command_signature("", cwd="") in void[0]
    # ...and the operator reads the refusal where they read everything else.
    assert any("could not determine the command" in str(a[2]) for a, _k in h.events)


def test_a_body_reading_another_key_cannot_cash_an_empty_approval(h, tmp_path):
    """Whatever a body does with its other parameters, a call the gate scores as
    the EMPTY command never passes: here the body runs ``shell`` when ``command``
    is empty, so honouring the planted "" approval WOULD run X."""
    impls = tmp_path / "impls"
    impls.mkdir()
    (impls / "run_cli_command.py").write_text(textwrap.dedent('''
        import shlex, subprocess

        def run(command="", shell="", timeout_seconds=30):
            subprocess.run(shlex.split(command or shell), shell=False)
            return {"success": True}
    '''), encoding="utf-8")
    _plant_empty_approvals(h.store)
    result = h.in_process("run_cli_command", {"shell": CMD, "timeout_seconds": 5},
                          impl_dir=impls)
    _assert_refused_undeterminable(h, result)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Execution consumes the value the gate scored -- it never re-derives it
# ─────────────────────────────────────────────────────────────────────────────

def test_a_body_change_between_gate_and_call_cannot_run_an_unscored_command(h, tmp_path):
    """The gate scores the call against ``run(command, ...)``; between the gate
    and the call the body becomes ``run(shell, ...)``. Re-deriving the arguments
    at execution time would now run the ``shell`` value the gate DROPPED and never
    scored. Consuming the sealed value runs only the scored command, or nothing."""
    impls = tmp_path / "impls"
    impls.mkdir()
    body = impls / "run_cli_command.py"
    body.write_text(textwrap.dedent('''
        import shlex, subprocess

        def run(command, timeout_seconds=30):
            subprocess.run(shlex.split(command), shell=False)
            return {"success": True}
    '''), encoding="utf-8")

    def _swap(name):
        body.write_text(textwrap.dedent('''
            import shlex, subprocess

            def run(shell, timeout_seconds=30):
                subprocess.run(shlex.split(shell), shell=False)
                return {"success": True}
        '''), encoding="utf-8")

    _approve(h.store, CMD)
    h.in_process("run_cli_command",
                 {"command": CMD, "shell": OTHER, "timeout_seconds": 5},
                 impl_dir=impls, vault=_Vault(hook=_swap))
    assert shlex.split(OTHER) not in h.ran, "a command the gate never scored ran"
    assert h.ran in ([], [shlex.split(CMD)])


def test_the_registry_passes_a_sealed_call_verbatim():
    """``ToolRegistry.execute`` reconciles a plain dict, but a sealed call is the
    value a gate scored: it is passed to ``run()`` as-is, never re-mapped."""
    from systemu.runtime.tool_registry import SealedCallKwargs, ToolRegistry

    calls = []
    reg = ToolRegistry(IMPLS, _Vault())

    class _Mod:
        @staticmethod
        def run(command, timeout_seconds=30):
            calls.append(command)
            return {"success": True}

    reg._modules["run_cli_command"] = _Mod
    plain = asyncio.run(reg.execute("run_cli_command", {"command": CMD, "bogus": 1}))
    assert plain["success"] and calls == [CMD]          # bogus -> timeout_seconds(pos)
    sealed = asyncio.run(reg.execute("run_cli_command",
                                     SealedCallKwargs({"command": OTHER, "bogus": 1})))
    assert sealed["success"] is False and "Parameter mismatch" in sealed["error"]
    assert calls == [CMD]


def test_sealing_does_not_import_the_tool_module(tmp_path):
    """The gate's view of ``run()`` is read from the SOURCE until the registry has
    loaded the module itself: module-level code never runs before the gates."""
    from systemu.runtime.tool_registry import SealedCallKwargs, ToolRegistry

    impls = tmp_path / "impls"
    impls.mkdir()
    marker = tmp_path / "imported"
    (impls / "run_cli_command.py").write_text(textwrap.dedent('''
        from pathlib import Path
        Path({marker!r}).write_text("x")

        def run(command, timeout_seconds=30):
            return {{"success": True}}
    ''').format(marker=str(marker)), encoding="utf-8")
    reg = ToolRegistry(impls, _Vault())
    sealed = reg.seal_call("run_cli_command", {"shell": CMD, "timeout_seconds": 5})
    assert type(sealed) is SealedCallKwargs
    assert dict(sealed) == {"command": CMD, "timeout_seconds": 5}
    assert not marker.exists()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Scored == executed, per tool and per path
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("params", [
    pytest.param({"command": CMD}, id="canonical"),
    pytest.param({"command": CMD, "timeout_seconds": 5}, id="canonical_timeout"),
    pytest.param({"shell": CMD, "timeout_seconds": 5}, id="positional_remap"),
    pytest.param({"cmd": CMD, "timeout_seconds": 5}, id="cmd_remap"),
    pytest.param({"command": CMD, "timeout": 5}, id="timeout_remapped_not_command"),
    pytest.param({"command": CMD, "shell": OTHER, "timeout_seconds": 5},
                 id="extra_key_dropped"),
])
def test_in_process_run_cli_command_runs_exactly_what_was_approved(h, params):
    """An approval planted for exactly CMD: the call passes via that approval (so
    the gate signed CMD) and ``subprocess.run`` receives exactly CMD's argv."""
    _approve(h.store, CMD)
    res = h.in_process("run_cli_command", params)
    assert h.posted == [], [p.d.inspect for p in h.posted]
    assert res.gate and res.gate["passed_via"] == "standing_approval", res
    assert h.ran == [shlex.split(CMD)]


def test_in_process_parameter_validation_reads_the_sealed_value(h):
    """``{"command": X, "shell": Y}``: the registry maps ``shell`` onto the one
    unfilled slot, ``timeout_seconds``. Gate 2.5 now validates the value that RUNS
    (it used to validate the raw call, and ``subprocess.run`` was then handed a
    string timeout after the process had already been spawned)."""
    _approve(h.store, CMD)
    res = h.in_process("run_cli_command", {"command": CMD, "shell": OTHER})
    assert res.gate and res.gate["passed_via"] == "standing_approval", res
    assert (res.parsed or {}).get("error_type") == "tool_param_invalid", res.parsed
    assert h.ran == []


def test_in_process_run_command_runs_exactly_what_was_approved_where_approved(h, tmp_path):
    cwd = str(tmp_path)
    _approve(h.store, CMD, cwd=cwd)
    res = h.in_process("run_command", {"command": CMD, "cwd": cwd, "cmd": OTHER})
    assert res.gate and res.gate["passed_via"] == "standing_approval", res
    assert h.ran == [CMD]
    assert h.cwds == [cwd]


def test_in_process_cwd_the_tool_ignores_is_not_signed(h):
    """``run_cli_command`` takes no ``cwd``: the registry drops it, so the gate must
    not sign (or show) a working directory the command will not run in."""
    _approve(h.store, CMD, cwd="")
    res = h.in_process("run_cli_command",
                       {"command": CMD, "cwd": "C:/elsewhere", "timeout_seconds": 5})
    assert res.gate and res.gate["passed_via"] == "standing_approval", res
    assert h.ran == [shlex.split(CMD)]


# -- the subprocess path: a REAL child spawns, and records its own argv --------

def _recorder(tmp_path):
    rec = tmp_path / "rec.py"
    out = tmp_path / "argv.json"
    rec.write_text("import json, pathlib, sys\n"
                   "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n",
                   encoding="utf-8")
    command = '"{}" "{}" "{}" alpha "beta gamma"'.format(sys.executable, rec, out)
    return command, out


@pytest.fixture
def real(tmp_path, monkeypatch):
    """No subprocess patching: the tool body really spawns."""
    monkeypatch.chdir(tmp_path)
    posted = []

    class _Inbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, **kw):
            posted.append(descriptor)
            return "dec_p23"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _Inbox)
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    return SimpleNamespace(store=store, posted=posted, tmp=tmp_path)


@pytest.mark.parametrize("name", ["run_cli_command", "run_command"])
def test_subprocess_path_spawns_exactly_what_was_approved(real, name):
    command, out = _recorder(real.tmp)
    _approve(real.store, command)
    sb = _sandbox(real.store)
    res = asyncio.run(sb.execute_tool(str(IMPLS / (name + ".py")), {"command": command},
                                      timeout=120, force_subprocess=True))
    assert res.success, (res.error, res.stdout, res.stderr)
    assert res.gate["passed_via"] == "standing_approval"
    assert real.posted == []
    assert json.loads(out.read_text()) == shlex.split(command)[3:]


@pytest.mark.parametrize("params", [
    pytest.param({"shell": "{c}", "timeout_seconds": 5}, id="shell"),
    pytest.param({"cmd": "{c}", "timeout_seconds": 5}, id="cmd"),
])
def test_subprocess_path_refuses_a_call_whose_command_never_reaches_the_body(real, params):
    """The subprocess runner calls ``run(**params)`` with NO reconciliation, so a
    command under another key never reaches ``command``: there is nothing to score.
    Even an approval for that very string (and one for "") changes nothing."""
    command, out = _recorder(real.tmp)
    params = {k: (v.format(c=command) if isinstance(v, str) else v)
              for k, v in params.items()}
    _approve(real.store, command)
    _plant_empty_approvals(real.store)
    sb = _sandbox(real.store)
    res = asyncio.run(sb.execute_tool(str(IMPLS / "run_cli_command.py"), params,
                                      timeout=120, force_subprocess=True))
    assert (res.parsed or {}).get("error_type") == UNDETERMINABLE, res
    assert not out.exists()
    assert real.posted == []


# -- the forge dry-run path ----------------------------------------------------

def test_dry_run_path_spawns_exactly_what_was_approved(real):
    import systemu.runtime.command_approvals as ca
    from systemu.pipelines import tool_dry_run

    command, out = _recorder(real.tmp)
    ca.reset_default_store_for_tests()
    try:
        ca.init_default_store(Path("data")).approve(command_signature(command),
                                                    command=command)
        tool = SimpleNamespace(implementation_path=str(IMPLS / "run_cli_command.py"),
                               dependencies=[], name="run_cli_command")
        config = SimpleNamespace(vault_dir=str(VAULT), docker_tool_timeout=120)
        res = tool_dry_run._execute(tool, {"command": command}, vault=object(),
                                    config=config)
        assert res["success"], res
        assert json.loads(out.read_text()) == shlex.split(command)[3:]

        out.unlink()
        ca.init_default_store(Path("data")).approve(command_signature(""), command="")
        res = tool_dry_run._execute(tool, {"shell": command, "timeout_seconds": 5},
                                    vault=object(), config=config)
        assert res["success"] is False
        assert (res.get("parsed") or {}).get("error_type") == UNDETERMINABLE, res
        assert not out.exists()
    finally:
        ca.reset_default_store_for_tests()
    assert real.posted == []


# -- the v2 code-registered path -----------------------------------------------

@pytest.fixture
def v2(h):
    from systemu.runtime.tool_registry_v2 import registry
    got = []
    names = ("run_command", "run_cli_command")
    for name in names:
        assert registry.get(name) is None
        registry.register(name=name, toolset="test", schema={},
                          handler=lambda **kw: got.append(dict(kw)) or {"success": True})
    try:
        yield SimpleNamespace(h=h, got=got, sb=_sandbox(h.store))
    finally:
        for name in names:
            registry.unregister(name)


@pytest.mark.parametrize("name", ["run_command", "run_cli_command"])
def test_v2_path_runs_exactly_what_was_approved(v2, name):
    _approve(v2.h.store, CMD)
    out = asyncio.run(v2.sb.execute(name, {"command": CMD}))
    assert out == {"success": True}
    assert [g["command"] for g in v2.got] == [CMD]
    assert v2.h.posted == []


def test_v2_path_gates_on_the_command_the_handler_receives(v2):
    with pytest.raises(PendingOperatorDecision):
        asyncio.run(v2.sb.execute("run_command", {"command": CMD}))
    assert v2.got == []
    assert [p.d.dedup for p in v2.h.posted] == ["command:" + command_signature(CMD)]


@pytest.mark.parametrize("params", [{"cmd": CMD}, {"command": ""}, {"shell": CMD}])
def test_v2_path_refuses_an_undeterminable_command(v2, params):
    _plant_empty_approvals(v2.h.store)
    out = asyncio.run(v2.sb.execute("run_command", params))
    assert out["success"] is False
    assert out["error_type"] == UNDETERMINABLE
    assert v2.got == [] and v2.h.posted == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. (closed by P26) launch_application is gated per resolved target
# ─────────────────────────────────────────────────────────────────────────────

def test_residual_launch_application_card_shows_the_target_it_spawns(h):
    """Was a strict xfail naming the residual: the per-tool card showed the RAW
    parameters (8 keys / 80 chars) with a params-independent standing approval,
    so a 9th key reconciled onto ``path`` ran but was never shown. Closed by P26
    (ruling L1: gated per RESOLVED target); the full witness set is
    ``tests/test_p26_launch_application_per_target.py``."""
    target = "notepad.exe C:/p23-target.txt"
    params = {"k{}".format(i): "x" for i in range(8)}
    params["target"] = target            # synonym group of `path` -> reconciled onto it
    tool = SimpleNamespace(name="launch_application", effect_tags=["shell_exec"],
                           forged_by_systemu=False, id="tool_launch", version=1,
                           implementation_path=str(IMPLS / "launch_application.py"))
    with pytest.raises(PendingOperatorDecision):
        h.in_process("launch_application", params, tool=tool)
    assert target in h.posted[-1].d.inspect
