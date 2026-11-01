"""P26 -- launch_application / close_application are gated PER RESOLVED TARGET.

The defect: both tools spawn a process their parameters choose, but only the
per-tool gate governed them, and its signature ignores parameters -- ONE standing
approval of ``launch_application`` covered EVERY program the model later chose
(one of ``close_application`` covered stopping any process). Alias selection and
the friendly-name lookup happened INSIDE the body, after every gate, and the card
showed the raw parameters cut to 8 keys, so a 9th key reconciled onto ``path``
ran but was never shown.

Ruling L1, pinned here against the REAL sandbox, the REAL registry and the
SHIPPED tool bodies (``subprocess.Popen`` / ``subprocess.run`` / ``os.kill`` /
``os.startfile`` are recorders -- nothing is ever really launched or killed):

  * the target is resolved BEFORE the gate by the one resolver the body then
    consumes (``systemu.runtime.app_targets``); the gate signs and shows it;
  * two different targets need two approvals; a standing approval (minted by the
    real recorder from the real card) for A runs A and never B;
  * the card names the resolved target and EVERY parameter that reaches the tool
    (a 9th key reconciled onto ``path`` is listed), redacted then capped;
  * an unresolvable target is REFUSED -- never carded, never signed -- and
    nothing spawns;
  * a target resolving to a systemu operator command is refused by the fence;
  * the parameter-free approval such a tool held before is NOT honoured: the
    first call of each target asks once, and the log and card say so;
  * close_application had the same hole and gets the same rule; a process ID is
    never covered by a standing approval (PIDs are reused).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.approval.exceptions import PendingOperatorDecision

REPO = Path(__file__).resolve().parent.parent
VAULT = REPO / "systemu" / "vault"
IMPLS = VAULT / "tools" / "implementations"

UNDETERMINABLE = "target_undeterminable"


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────

class _Vault:
    """The registry's vault: every tool enabled and dry-run-passed."""

    def find_tool_by_name(self, name):
        return SimpleNamespace(
            name=name, id="tool_" + name, version=1, enabled=True,
            parameters_schema={}, dry_run_status="passed",
            requires_credentials=[], credential_refusals=[], dependencies=[],
            timeout_seconds=None)


def _tool(name):
    """The shipped tool as the runtime threads it (``effect_tags`` as backfilled)."""
    return SimpleNamespace(name=name, effect_tags=["shell_exec"],
                           forged_by_systemu=False, id="tool_" + name, version=1,
                           implementation_path=str(IMPLS / (name + ".py")))


@pytest.fixture
def g(tmp_path, monkeypatch):
    """The real gate. Witnesses: ``spawned`` (every Popen/startfile), ``ran``
    (every subprocess.run argv), ``killed`` (every os.kill), ``posted`` (every
    card, with its context extras), ``events`` (operator notifications)."""
    import systemu.runtime.command_approvals as ca

    monkeypatch.chdir(tmp_path)
    spawned, ran, killed = [], [], []

    class _Proc:
        pid = 4321

    def _popen(args, *a, **k):
        spawned.append(args)
        return _Proc()

    def _run(args, *a, **k):
        ran.append(list(args) if not isinstance(args, str) else args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(os, "startfile", lambda p, *a, **k: spawned.append(("startfile", p)),
                        raising=False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    posted = []

    class _Inbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, policy=None, context_extras=None,
                    **kw):
            posted.append(SimpleNamespace(d=descriptor, gate_type=gate_type,
                                          extras=dict(context_extras or {})))
            return "dec_p26"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _Inbox)
    events = []
    import systemu.interface.notifications as notifications
    monkeypatch.setattr(notifications, "log_event",
                        lambda *a, **k: events.append((a, k)))

    # The process-wide store, so the REAL recorder (resume_on_decision) and the
    # gate read and write the same file.
    ca.reset_default_store_for_tests()
    store = ca.init_default_store(Path("data"))

    def call(name, params, *, tool="shipped", force_subprocess=False):
        from systemu.runtime.tool_registry import ToolRegistry
        from systemu.runtime.tool_sandbox import ToolSandbox
        reg = ToolRegistry(IMPLS, _Vault())
        sb = ToolSandbox(vault_root=VAULT, vault=object(), command_approvals=store,
                         registry=reg)
        t = _tool(name) if tool == "shipped" else tool
        return asyncio.run(sb.execute_tool(str(IMPLS / (name + ".py")), params,
                                           tool=t, force_subprocess=force_subprocess))

    def card(name, params, **kw):
        """Call, expect a card, return it."""
        n = len(posted)
        with pytest.raises(PendingOperatorDecision) as ei:
            call(name, params, **kw)
        assert len(posted) == n + 1
        posted[-1].raised = ei.value
        return posted[-1]

    def always_allow(c):
        """The operator clicks "Always allow" on card ``c`` -- the real recorder."""
        from systemu.runtime.resume_on_decision import _record_gate_approval
        _record_gate_approval(dict(c.extras), is_tool_gate=True, choice="always allow")

    def approve_once(c):
        from systemu.runtime.resume_on_decision import _record_gate_approval
        _record_gate_approval(dict(c.extras), is_tool_gate=True, choice="approve once")

    yield SimpleNamespace(store=store, spawned=spawned, ran=ran, killed=killed,
                          posted=posted, events=events, call=call, card=card,
                          always_allow=always_allow, approve_once=approve_once,
                          tmp=tmp_path)
    ca.reset_default_store_for_tests()


def _launched(g):
    """What the launch body handed to Popen / startfile."""
    return [s[1] if isinstance(s, tuple) else s for s in g.spawned]


def _assert_refused(g, result, *, error_type=UNDETERMINABLE):
    assert result.success is False, result
    assert (result.parsed or {}).get("error_type") == error_type, result.parsed
    assert "Nothing was run" in (result.error or ""), result.error
    assert result.error.isascii()
    assert g.spawned == [] and g.ran == [] and g.killed == [], (g.spawned, g.ran, g.killed)
    assert g.posted == [], "a refused call was offered as an approval card"


def _legacy_signature(name):
    """The PARAMETER-FREE signature the per-tool gate keyed this tool on before
    P26 -- computed the way the gate computed it then."""
    from systemu.runtime.command_approvals import (signature_host_class,
                                                   tool_host_class, tool_signature)
    from systemu.runtime.tool_sandbox import tool_body_hash
    t = _tool(name)
    body_hash, _missing = tool_body_hash(t, VAULT)
    return tool_signature(name, body_hash, set(t.effect_tags),
                          host_class=signature_host_class(tool_host_class(t)))


# ─────────────────────────────────────────────────────────────────────────────
# 1. One approval covers ONE application
# ─────────────────────────────────────────────────────────────────────────────

def test_two_different_targets_need_two_approvals(g):
    a = g.card("launch_application", {"path": "notepad.exe"})
    g.always_allow(a)
    res = g.call("launch_application", {"path": "notepad.exe"})
    assert res.success, res.error
    assert res.gate["passed_via"] == "standing_approval"
    assert _launched(g) == ["notepad.exe"]

    b = g.card("launch_application", {"path": "calc.exe"})
    assert b.d.dedup != a.d.dedup
    assert b.extras["tool_signature"] != a.extras["tool_signature"]
    assert _launched(g) == ["notepad.exe"], "the second application ran on the first's approval"
    g.always_allow(b)
    assert g.call("launch_application", {"path": "calc.exe"}).success
    assert _launched(g) == ["notepad.exe", "calc.exe"]


def test_a_standing_approval_for_A_does_not_run_B(g):
    a = g.card("launch_application", {"application_name": "notepad"})
    g.always_allow(a)
    g.posted.clear()
    with pytest.raises(PendingOperatorDecision):
        g.call("launch_application", {"application_name": "calculator"})
    assert g.spawned == []
    assert g.posted[-1].d.inspect.splitlines()[0].startswith("launches: calc.exe")


def test_an_approve_once_bridge_for_A_runs_A_once_and_never_B(g):
    a = g.card("launch_application", {"path": "notepad.exe"})
    g.approve_once(a)
    g.card("launch_application", {"path": "calc.exe"})       # B: still asks
    assert g.spawned == []
    res = g.call("launch_application", {"path": "notepad.exe"})
    assert res.success and res.gate["passed_via"] == "resume_bridge"
    g.card("launch_application", {"path": "notepad.exe"})    # single use
    assert _launched(g) == ["notepad.exe"]


def test_two_spellings_of_one_application_are_one_target(g):
    """The approval binds the RESOLVED target, not the words the model used."""
    a = g.card("launch_application", {"application_name": "Notepad "})
    g.always_allow(a)
    for params in ({"path": "notepad"}, {"name": "NOTEPAD"}, {"path": "notepad.exe"},
                   {"path": "notepad.exe", "application_name": "notepad"}):
        res = g.call("launch_application", params)
        assert res.success and res.gate["passed_via"] == "standing_approval", params
    assert _launched(g) == ["notepad.exe"] * 4


def test_the_signature_binds_the_target_and_reproduces_from_the_card(g):
    from systemu.runtime.command_approvals import parts_reproduce_signature
    c = g.card("launch_application", {"path": "notepad.exe"})
    parts = c.extras["tool_parts"]
    assert json.loads(parts["target"]) == ["launch_application", "launch", "command",
                                           "notepad.exe", False]
    assert parts_reproduce_signature(c.extras["tool_signature"], parts)
    assert c.d.dedup == "tool:" + c.extras["tool_signature"]
    assert c.extras["tool_signature"] != _legacy_signature("launch_application")


# ─────────────────────────────────────────────────────────────────────────────
# 2. The card shows the resolved target and every parameter that reaches the tool
# ─────────────────────────────────────────────────────────────────────────────

def _args_line(card):
    lines = [ln for ln in card.d.inspect.splitlines() if ln.startswith("args: ")]
    assert len(lines) == 1, card.d.inspect
    return lines[0]


def test_the_card_lists_a_9th_reconciled_key(g):
    """Eight junk keys and a 9th, ``target``, that the registry reconciles onto
    ``path``: the card leads with what will launch and lists ``path`` itself."""
    target = "notepad.exe C:/p26-target.txt"
    params = {"k{}".format(i): "x" for i in range(8)}
    params["target"] = target
    c = g.card("launch_application", params)
    assert c.d.inspect.splitlines()[0] == "launches: " + target
    assert "path={!r}".format(target) in _args_line(c)
    assert target in str(c.raised)
    assert g.spawned == []


def test_the_card_lists_every_key_the_tool_receives(g):
    """The 9th key is one the tool takes by its own name (no remap needed)."""
    params = {"k{}".format(i): "x" for i in range(8)}
    params["application_name"] = "paint"
    c = g.card("launch_application", params)
    assert "application_name='paint'" in _args_line(c)
    assert c.d.inspect.splitlines()[0] == ("launches: mspaint.exe (the name paint "
                                           "resolves to it)")


def test_long_values_are_redacted_before_they_are_capped(g):
    """DEC-31: the secret is LONGER than every cap on this card path (the 300-char
    parameter cap; the target line's cap is above the longest accepted target)."""
    secret = "sk-" + "Ab1Cd2Ef3Gh4" * 40
    assert len(secret) > 300
    c = g.card("launch_application", {"path": "app.exe " + secret})
    shown = c.d.inspect + c.d.what_approve_does + str(c.raised)
    assert secret[:24] not in shown
    assert "redacted" in _args_line(c)
    assert c.d.inspect.splitlines()[0] == "launches: [redacted - looked like a credential]"


def test_the_launch_path_is_not_redirected_into_the_output_dir(g):
    """The output-path normalizer rewrites a ``path`` whose parent directory is
    missing into the output dir. For a launch that would make the target depend on
    the filesystem, not the call, and open an output-dir file instead."""
    target = "notepad.exe C:/p26-no-such-dir/notes.txt"
    c = g.card("launch_application", {"path": target})
    assert c.d.inspect.splitlines()[0] == "launches: " + target
    assert json.loads(c.extras["tool_parts"]["target"])[3] == target


def test_the_card_says_approval_now_covers_one_application(g):
    c = g.card("launch_application", {"path": "notepad.exe"})
    assert "approval now covers one application" in c.d.inspect.lower()
    assert "remembers only that one" in c.d.what_approve_does
    assert c.d.options == ["Deny", "Approve once", "Always allow"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. An unresolvable target is refused: never carded, never signed, nothing spawns
# ─────────────────────────────────────────────────────────────────────────────

_UNRESOLVABLE_LAUNCH = [
    pytest.param({}, id="no_target"),
    pytest.param({"app": "notepad"}, id="unknown_alias_dropped_by_registry"),
    pytest.param({"path": "   "}, id="blank"),
    pytest.param({"path": ["notepad.exe"]}, id="not_a_string"),
    pytest.param({"path": "note\x00pad.exe"}, id="nul"),
    pytest.param({"path": "notepad.exe\u202e"}, id="bidi_override"),
    pytest.param({"path": "notepad.exe\x1b[2K"}, id="control_char"),
    pytest.param({"path": "notepad.exe & calc.exe"}, id="chained"),
    pytest.param({"path": "notepad.exe > out.txt"}, id="redirected"),
    pytest.param({"path": "%COMSPEC% /c calc"}, id="expanded"),
    pytest.param({"path": "$(calc)"}, id="substituted"),
    pytest.param({"path": '"C:/Program Files/x.exe'}, id="unbalanced_quotes"),
    pytest.param({"path": "notepad", "name": "calculator"}, id="ambiguous"),
    pytest.param({"path": "a" * 1100}, id="too_long"),
]


@pytest.mark.parametrize("params", _UNRESOLVABLE_LAUNCH)
def test_an_unresolvable_launch_target_is_refused(g, params):
    g.store.approve(_legacy_signature("launch_application"))   # even with a legacy allow
    res = g.call("launch_application", params)
    _assert_refused(g, res)
    assert "could not determine which application" in res.error


def test_an_unknown_parameter_on_the_unreconciled_path_is_refused(g):
    """The subprocess runner calls ``run(**params)`` as given: a target under a
    parameter the tool does not take never reaches it, so there is no target."""
    res = g.call("launch_application", {"target": "notepad.exe"}, force_subprocess=True)
    _assert_refused(g, res)
    assert "'target'" in res.error


def test_the_refusal_is_told_to_the_operator(g):
    res = g.call("launch_application", {"path": "notepad.exe & calc.exe"})
    _assert_refused(g, res)
    assert any("could not determine which application" in str(a[2])
               for a, _k in g.events)


_UNRESOLVABLE_CLOSE = [
    pytest.param({}, id="no_target"),
    pytest.param({"pid": "1234"}, id="pid_string"),
    pytest.param({"pid": 0}, id="pid_zero"),
    pytest.param({"pid": -1}, id="pid_negative_every_process"),
    pytest.param({"pid": True}, id="pid_bool"),
    pytest.param({"pid": 1234, "name": "notepad"}, id="pid_and_name"),
    pytest.param({"name": "notepad", "force": "false"}, id="force_not_bool"),
    pytest.param({"application_name": "word", "name": "excel"}, id="ambiguous"),
    pytest.param({"name": "note\x00pad"}, id="nul"),
]


@pytest.mark.parametrize("params", _UNRESOLVABLE_CLOSE)
def test_an_unresolvable_close_target_is_refused(g, params):
    g.store.approve(_legacy_signature("close_application"))
    res = g.call("close_application", params)
    _assert_refused(g, res)


# ─────────────────────────────────────────────────────────────────────────────
# 4. The operator-command fence sees the RESOLVED target
# ─────────────────────────────────────────────────────────────────────────────

def test_a_target_resolving_to_a_systemu_operator_command_is_refused(g, monkeypatch):
    """A friendly name whose RESOLUTION is a systemu operator command: the raw
    strings never name systemu, so only the fence on the resolved target sees it."""
    from systemu.runtime import app_targets
    from systemu.runtime.operator_cli_fence import REFUSAL_LEAD
    monkeypatch.setitem(app_targets.LAUNCH_APP_MAP, "consent helper",
                        "systemu census grant cloud_sync_roots --yes")
    g.store.approve(_legacy_signature("launch_application"))
    res = g.call("launch_application", {"application_name": "consent helper"})
    assert res.success is False
    assert (res.parsed or {}).get("error_type") == "operator_command_refused"
    assert REFUSAL_LEAD in res.error
    assert g.spawned == [] and g.posted == []


def test_the_installed_systemu_launcher_with_an_operator_verb_is_refused(g):
    from systemu.runtime.operator_cli_fence import _console_script_files
    scripts = [p for p in _console_script_files() if p.stem.lower() == "systemu"]
    if not scripts:
        pytest.skip("no systemu console script is installed in this environment")
    res = g.call("launch_application",
                 {"path": '"{}" census grant cloud_sync_roots --yes'.format(scripts[0])})
    assert (res.parsed or {}).get("error_type") == "operator_command_refused"
    assert g.spawned == [] and g.posted == []


def test_the_bare_systemu_launcher_is_carded_never_run_unapproved(g):
    """``systemu`` with no verb is the help page, which the live command registry
    classifies agent-safe, so the fence lets it through -- to the per-target gate."""
    c = g.card("launch_application", {"path": "systemu"})
    assert c.d.inspect.splitlines()[0] == "launches: systemu"
    assert g.spawned == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. The parameter-free approval is no longer honoured
# ─────────────────────────────────────────────────────────────────────────────

def test_a_legacy_parameter_free_approval_does_not_cover_a_target(g, caplog):
    g.store.approve(_legacy_signature("launch_application"))
    with caplog.at_level(logging.WARNING, logger="systemu.runtime.tool_sandbox"):
        c = g.card("launch_application", {"path": "notepad.exe"})
    assert g.spawned == []
    assert "approval now covers one application" in c.d.inspect
    assert "An earlier 'Always allow' of launch_application" in c.d.inspect
    warned = [r.getMessage() for r in caplog.records
              if "approval now covers one application" in r.getMessage()]
    assert warned and "notepad.exe" in warned[0]
    # ...and the first approval of THIS target is what lets it run.
    g.always_allow(c)
    assert g.call("launch_application", {"path": "notepad.exe"}).success
    g.card("launch_application", {"path": "calc.exe"})
    assert _launched(g) == ["notepad.exe"]


def test_the_legacy_signature_is_the_one_the_old_gate_minted(g):
    """The planted legacy approval is the REAL pre-P26 key: the same gate, scoring
    a tool that is not target-gated, still keys on exactly this recipe."""
    from systemu.runtime.command_approvals import signature_from_parts
    c = g.card("launch_application", {"path": "notepad.exe"})
    parts = dict(c.extras["tool_parts"])
    parts.pop("target")
    assert signature_from_parts(parts) == _legacy_signature("launch_application")


# ─────────────────────────────────────────────────────────────────────────────
# 6. close_application: the same rule
# ─────────────────────────────────────────────────────────────────────────────

def test_close_two_applications_need_two_approvals(g):
    a = g.card("close_application", {"application_name": "notepad"})
    g.always_allow(a)
    res = g.call("close_application", {"application_name": "notepad"})
    assert res.success, res.error
    expect = (["taskkill", "/IM", "notepad.exe"] if sys.platform == "win32"
              else ["pkill", "-f", "notepad.exe"])
    assert g.ran == [expect]
    g.card("close_application", {"application_name": "excel"})
    assert g.ran == [expect]
    assert g.killed == []


def test_close_force_is_part_of_the_target(g):
    a = g.card("close_application", {"name": "notepad"})
    g.always_allow(a)
    g.card("close_application", {"name": "notepad", "force": True})
    assert g.ran == []


def test_a_process_id_is_never_remembered(g):
    c = g.card("close_application", {"pid": 4242})
    assert "Always allow" not in c.d.options
    assert "never remembered" in c.d.what_approve_does
    assert c.d.inspect.splitlines()[0] == "terminates process ID 4242"
    # Even a standing approval written for exactly this target is not honoured.
    g.store.approve(c.extras["tool_signature"])
    g.card("close_application", {"pid": 4242})
    assert g.killed == []
    # Approve once runs it, once.
    g.approve_once(g.posted[-1])
    assert g.call("close_application", {"pid": 4242}).success
    assert [pid for pid, _sig in g.killed] == [4242]


def test_a_legacy_close_approval_is_not_honoured(g):
    g.store.approve(_legacy_signature("close_application"))
    c = g.card("close_application", {"application_name": "notepad"})
    assert "approval now covers one application" in c.d.inspect
    assert g.ran == []


# ─────────────────────────────────────────────────────────────────────────────
# 7. One mint: the body acts on exactly the target the gate signed
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("params,expected", [
    pytest.param({"application_name": " Notepad "}, "notepad.exe", id="mapped"),
    pytest.param({"name": "C:/Program Files (x86)/App/app.exe"},
                 "C:/Program Files (x86)/App/app.exe", id="path_with_spaces"),
    pytest.param({"executable_name": "firefox"}, "firefox.exe", id="executable_name"),
])
def test_what_runs_is_what_was_signed(g, params, expected):
    c = g.card("launch_application", params)
    signed = json.loads(c.extras["tool_parts"]["target"])[3]
    assert signed == expected
    g.always_allow(c)
    assert g.call("launch_application", params).success
    assert _launched(g) == [signed]


def test_the_body_keeps_no_map_of_its_own(g, monkeypatch):
    """Change the ONE map: the card and the spawn both follow it."""
    from systemu.runtime import app_targets
    monkeypatch.setitem(app_targets.LAUNCH_APP_MAP, "notepad", "write.exe")
    c = g.card("launch_application", {"application_name": "notepad"})
    assert c.d.inspect.splitlines()[0].startswith("launches: write.exe")
    g.always_allow(c)
    assert g.call("launch_application", {"application_name": "notepad"}).success
    assert _launched(g) == ["write.exe"]


def test_the_body_refuses_what_the_gate_refuses():
    """Called directly (no gate at all), the shipped body launches nothing the
    resolver refuses -- it has no second way to pick a target."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_p26_launch",
                                                  IMPLS / "launch_application.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.run(path="notepad.exe & calc.exe")
    assert out["success"] is False and "Nothing was launched" in out["error"]
    assert not hasattr(mod, "_APP_MAP")


# ─────────────────────────────────────────────────────────────────────────────
# 8. No path around the per-target gate
# ─────────────────────────────────────────────────────────────────────────────

def test_a_call_without_a_tool_object_is_still_gated_per_target(g):
    c = g.card("launch_application", {"path": "notepad.exe"}, tool=None)
    assert c.d.inspect.splitlines()[0] == "launches: notepad.exe"
    assert g.spawned == []


def test_a_code_registered_handler_under_the_name_is_not_dispatched(g):
    from systemu.runtime.tool_registry_v2 import registry
    from systemu.runtime.tool_sandbox import ToolSandbox
    got = []
    assert registry.get("launch_application") is None
    registry.register(name="launch_application", toolset="test", schema={},
                      handler=lambda **kw: got.append(kw) or {"success": True})
    try:
        sb = ToolSandbox(vault_root=VAULT, vault=object(), command_approvals=g.store)
        out = asyncio.run(sb.execute("launch_application", {"path": "calc.exe"}))
    finally:
        registry.unregister("launch_application")
    assert got == []
    assert out["success"] is False
    assert g.spawned == []


def test_the_card_preview_drops_no_key():
    """The card helper's own contract, independent of how many keys the shipped
    tools take today (a sealed launch/close call carries at most five): every
    key is listed, every value is redacted before it is capped."""
    from systemu.runtime.tool_sandbox import _card_args_preview
    params = {"k{:02d}".format(i): "v{}".format(i) for i in range(12)}
    params["k12"] = "x" * 1000
    shown = _card_args_preview(params)
    assert list(shown) == list(params)
    assert shown["k11"] == "v11"
    assert len(shown["k12"]) <= 300
