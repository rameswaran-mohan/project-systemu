"""What approving a dependency would pull in -- shown BEFORE the approval.

THE GAP THIS CLOSES
    The dependency card said "Install dependency: X" and nothing else. Approving
    it runs `pip install X`, which installs X AND every package X needs that is
    not already present -- a set the operator never saw. This file pins the
    transitive list on the card and in `systemu tools diff --pending`:

      * resolved by `python -m pip install --dry-run --quiet --report <file>
        <spec>` in a subprocess, with a bounded timeout, OFF the caller's thread
        for the card (a worker thread) -- the subprocess seam is faked here, no
        test touches the network;
      * each package named with its version and marked when it is already
        installed;
      * on a pip failure, timeout or malformed report the card says
        `could not resolve the dependency tree (<reason>)` and lists NOTHING --
        a guessed list would be a consent surface lying about what it consents to;
      * cached per spec for the session.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import capability_diff as cd


@pytest.fixture(autouse=True)
def _fresh_cache():
    cd.reset_transitive_cache()
    yield
    cd.reset_transitive_cache()


def _report_runner(install, *, rc=0, stderr="", calls=None, raw=None):
    """A fake pip: writes a canned --report file where pip would, returns rc."""
    def run(argv, timeout):
        if calls is not None:
            calls.append((list(argv), timeout, threading.current_thread()))
        path = Path(argv[argv.index("--report") + 1])
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        elif rc == 0:
            path.write_text(json.dumps({"version": "1", "install": [
                {"metadata": {"name": n, "version": v}, "requested": i == 0}
                for i, (n, v) in enumerate(install)]}), encoding="utf-8")
        return rc, stderr
    return run


def _timeout_runner(argv, timeout):
    raise subprocess.TimeoutExpired(argv, timeout)


INSTALLED = {"typing_extensions": "4.12.2", "lxml": "4.9.0"}


def _resolve(spec="python-docx", runner=None, **kw):
    return cd.resolve_transitive(spec, runner=runner, installed_fn=INSTALLED.get, **kw)


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #

def test_the_exact_pip_command_and_a_bounded_timeout():
    calls = []
    _resolve(runner=_report_runner([("python-docx", "1.1.2")], calls=calls))
    (argv, timeout, _thread), = calls
    assert argv[:3] == [sys.executable, "-m", "pip"]
    assert argv[3:6] == ["install", "--dry-run", "--quiet"]
    assert "--report" in argv and argv[-1] == "python-docx"
    assert timeout == 60


def test_a_canned_report_lists_every_package_with_its_version_and_marks_installed():
    plan = _resolve(runner=_report_runner([("python-docx", "1.1.2"),
                                           ("lxml", "5.3.0"),
                                           ("typing_extensions", "4.12.2")]))
    assert plan.ok is True
    lines = cd.transitive_lines("python-docx", plan)
    text = "\n".join(lines)
    text.encode("ascii")
    assert "3 package(s)" in lines[0]
    assert any("python-docx 1.1.2" in ln and "(new)" in ln for ln in lines)
    assert any("lxml 5.3.0" in ln and "replaces installed 4.9.0" in ln for ln in lines)
    assert any("typing_extensions 4.12.2" in ln and "(already installed)" in ln
               for ln in lines)


@pytest.mark.parametrize("runner,reason", [
    (_timeout_runner, "pip did not finish within 60s"),
    (_report_runner([], rc=1, stderr="ERROR: something else"), "pip exited with code 1"),
    (_report_runner([], rc=1, stderr="ERROR: Could not find a version that satisfies"
                                    " the requirement x\nERROR: No matching "
                                    "distribution found for x"),
     "no matching distribution was found"),
    (_report_runner([], rc=1, stderr="WARNING: Retrying ... NewConnectionError("
                                    "Failed to establish a new connection"),
     "the package index could not be reached"),
    (_report_runner([], raw="{not json"), "pip's report was malformed"),
    (_report_runner([], raw='{"install": "nope"}'), "pip's report was malformed"),
    (_report_runner([], raw='{"install": [{"metadata": {"name": 3}}]}'),
     "pip's report was malformed"),
])
def test_every_failure_says_it_could_not_resolve_and_lists_nothing(runner, reason):
    """NEVER invent a list. Mutation: render a guessed list (e.g. the requested
    spec alone) on failure and this goes red."""
    plan = _resolve(runner=runner)
    assert plan.ok is False
    assert plan.packages == ()
    lines = cd.transitive_lines("python-docx", plan)
    assert lines == [f"{cd.DEP_TREE_HEADER}: could not resolve the dependency tree "
                     f"({reason})"]


def test_an_invalid_spec_never_reaches_pip():
    """A hand-edited pending entry cannot smuggle a pip OPTION in as a spec."""
    calls = []
    plan = _resolve("--index-url=http://example.invalid/simple",
                    runner=_report_runner([("x", "1")], calls=calls))
    assert calls == []
    assert plan.ok is False and "not a valid package spec" in plan.reason


def test_resolution_is_cached_per_spec_for_the_session():
    calls = []
    runner = _report_runner([("python-docx", "1.1.2")], calls=calls)
    first = _resolve(runner=runner)
    second = _resolve(runner=runner)
    assert second is first and len(calls) == 1
    assert cd.cached_transitive("python-docx") is first
    _resolve("requests", runner=runner)
    assert len(calls) == 2


def test_a_failure_is_cached_too():
    calls = []
    runner = _report_runner([], rc=1, calls=calls)
    _resolve(runner=runner)
    _resolve(runner=runner)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# The dependency card
# --------------------------------------------------------------------------- #

_ENTRY = {"package": "python-docx", "first_seen_tool": "create_word_doc",
          "first_seen_tool_id": "tool_6e6e62c0", "request_count": 3}


def test_the_card_shows_the_resolved_tree():
    from systemu.interface.command.gate import GateDescriptor
    plan = _resolve(runner=_report_runner([("python-docx", "1.1.2"),
                                           ("typing_extensions", "4.12.2")]))
    card = GateDescriptor.from_dep(_ENTRY, transitive=plan)
    card.inspect.encode("ascii")
    assert "Requested by create_word_doc x3" in card.inspect
    assert "python-docx 1.1.2" in card.inspect
    assert "(already installed)" in card.inspect


def test_the_card_on_a_failure_shows_the_reason_and_no_list():
    from systemu.interface.command.gate import GateDescriptor
    plan = _resolve(runner=_timeout_runner)
    card = GateDescriptor.from_dep(_ENTRY, transitive=plan)
    assert ("could not resolve the dependency tree (pip did not finish within 60s)"
            in card.inspect)
    assert "package(s)" not in card.inspect


def test_an_unresolved_card_says_so_and_names_the_command_that_resolves_it():
    from systemu.interface.command.gate import GateDescriptor
    card = GateDescriptor.from_dep(_ENTRY)
    assert "not resolved yet" in card.inspect
    assert "systemu tools diff --pending" in card.inspect


def test_the_rendered_card_picks_up_a_resolution_that_landed_later():
    """The card is persisted when it is posted -- usually before pip finishes. The
    Inbox rebuilds it from the stored context, and the rebuild reads the session
    cache, so the resolved tree replaces the placeholder: ONE section, never two."""
    from systemu.interface.command.gate import GateDescriptor
    posted = GateDescriptor.from_dep(_ENTRY)
    ctx = posted.to_decision_context(gate_type="dep")
    _resolve(runner=_report_runner([("python-docx", "1.1.2")]))

    shown = GateDescriptor.from_decision_context(ctx, title=posted.title,
                                                 options=posted.options,
                                                 dedup=posted.dedup)
    assert "python-docx 1.1.2" in shown.inspect
    assert "not resolved yet" not in shown.inspect
    assert shown.inspect.count(cd.DEP_TREE_HEADER) == 1
    assert "Requested by create_word_doc x3" in shown.inspect


def test_a_card_posted_by_a_live_run_resolves_on_a_worker_thread(monkeypatch):
    """Posting the card from a run in a process that serves operator decisions
    starts the resolution on a WORKER thread -- the caller is never blocked on
    pip, and the card it returns says so honestly."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.chat_submission_ctx import set_execution_id

    calls = []
    monkeypatch.setattr(cd, "_pip_runner",
                        _report_runner([("python-docx", "1.1.2")], calls=calls))
    monkeypatch.setattr(cd, "installed_version", INSTALLED.get)
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    token = set_execution_id("exec_capdiff")
    try:
        card = GateDescriptor.from_dep(_ENTRY)
    finally:
        set_execution_id(None, reset_token=token)
    assert cd.wait_for_transitive("python-docx", timeout=10) is True
    (_argv, _timeout, thread), = calls
    assert thread is not threading.main_thread()
    assert "resolving now" in card.inspect
    assert cd.cached_transitive("python-docx").ok is True


def test_no_live_run_means_no_background_pip(monkeypatch):
    """Outside a run (a test, the CLI, a page render) building the card never
    starts pip on its own."""
    from systemu.interface.command.gate import GateDescriptor
    calls = []
    monkeypatch.setattr(cd, "_pip_runner",
                        _report_runner([("python-docx", "1.1.2")], calls=calls))
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    GateDescriptor.from_dep(_ENTRY)
    assert cd.transitive_in_flight("python-docx") is False
    assert calls == []


# --------------------------------------------------------------------------- #
# `systemu tools diff --pending`
# --------------------------------------------------------------------------- #

def _cli(tmp_path, monkeypatch, *args, runner):
    from click.testing import CliRunner

    from sharing_on import cli as sharing_on_cli
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "dep_approvals.json").write_text(json.dumps({
        "version": 1, "approved": {},
        "pending": {"python-docx": {"first_seen_at": "2026-10-01T00:00:00+00:00",
                                    "first_seen_tool": "create_word_doc",
                                    "first_seen_tool_id": "tool_6e6e62c0",
                                    "request_count": 3}}}), encoding="utf-8")
    monkeypatch.setattr(cd, "_pip_runner", runner)
    monkeypatch.setattr(cd, "installed_version", INSTALLED.get)
    vault = SimpleNamespace(root=tmp_path / "vault", load_index=lambda e: [],
                            get_tool=None, get_decision=None)
    return CliRunner().invoke(sharing_on_cli.cli, ["tools", "diff", "--pending", *args],
                              obj={"config": object(), "vault": vault})


def test_cli_pending_lists_what_each_pending_dependency_would_add(tmp_path, monkeypatch):
    res = _cli(tmp_path, monkeypatch, runner=_report_runner(
        [("python-docx", "1.1.2"), ("typing_extensions", "4.12.2")]))
    assert res.exit_code == 0, res.output
    out = res.output
    out.encode("ascii")
    assert "python-docx" in out and "create_word_doc" in out
    assert "python-docx 1.1.2" in out
    assert "typing_extensions 4.12.2" in out and "(already installed)" in out


def test_cli_pending_on_a_pip_failure_names_the_reason(tmp_path, monkeypatch):
    res = _cli(tmp_path, monkeypatch, runner=_timeout_runner)
    assert res.exit_code == 0, res.output
    assert ("could not resolve the dependency tree (pip did not finish within 60s)"
            in res.output)


def test_cli_pending_json(tmp_path, monkeypatch):
    res = _cli(tmp_path, monkeypatch, "--json",
               runner=_report_runner([("python-docx", "1.1.2")]))
    data = json.loads(res.output)
    (p,) = data["pending"]
    assert p["spec"] == "python-docx" and p["ok"] is True
    assert p["packages"][0] == {"name": "python-docx", "version": "1.1.2",
                                "installed": None}


# --------------------------------------------------------------------------- #
# R-UX2: no blocking call became visible in the interface layer
# --------------------------------------------------------------------------- #

def test_the_offload_lint_sees_nothing_new_in_the_files_this_touches():
    from tools.lint_offload import repo_root, scan_repo
    hits = [v for v in scan_repo(repo_root())
            if v.path.replace("\\", "/").endswith(("interface/command/gate.py",
                                                   "interface/cli_commands.py"))]
    assert hits == [], hits
