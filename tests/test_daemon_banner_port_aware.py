"""The multi-daemon banner claimed a PORT RACE from a machine-wide COUNT.

Observed live (v0.10.25): the operator's daemon served the dashboard on 8765
while an isolated test daemon ran on 8901 out of its own vault root. The banner
fired DANGER -- "Whichever wins the port race will serve this dashboard, and
recordings or decisions may land in the wrong vault" -- and prescribed
``systemu daemon stop --all``, which would have stopped the healthy daemon too.

The COUNT was true. The CLAIM and the REMEDY were not. Two daemons bound to
DIFFERENT ports are not racing: each serves its own port out of its own vault,
and the dashboard you are looking at is served by the one whose port is in the
address bar. That is a WARNING ("more daemons are running than you meant to"),
not a DANGER ("your recordings may land in the wrong vault").

FAIL-CLOSED (DEC-27 / DEC-32: completeness is WITNESSED, never inferred).
The softer WARNING is reachable ONLY when every sibling's port is KNOWN and all
of them are DISTINCT. A shared port, an unknowable port, or a count that the
per-process records do not fully account for all keep the DANGER wording and the
``stop --all`` remedy VERBATIM. The banner never talks itself down from a fact
it could not establish.

The model is a PURE function -- process records in, banner issue out -- so every
shape below is exercised without a single daemon running.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from systemu.interface.components import health_banner as hb

# The DANGER copy is load-bearing: the same-port case must keep it VERBATIM.
DANGER_MESSAGE_TAIL = ("Whichever wins the port race will serve this dashboard, "
                       "and recordings or decisions may land in the wrong vault.")
DANGER_CTA = "systemu daemon stop --all"


def _d(pid=None, port=None, vault=None, cwd=None, is_self=False):
    return hb.DaemonProcess(pid=pid, port=port, vault=vault, cwd=cwd,
                            is_self=is_self)


# -- the quiet case ----------------------------------------------------------

def test_a_single_daemon_raises_no_issue():
    assert hb.daemon_conflict_issue(1, (_d(pid=1, port=8765, vault="/v"),)) is None
    assert hb.daemon_conflict_issue(0, ()) is None


# -- shape 1: SAME PORT -> the DANGER wording, unchanged ---------------------

def test_same_port_keeps_the_danger_wording_and_remedy_verbatim():
    """Two daemons told to serve ONE port genuinely race for it."""
    issue = hb.daemon_conflict_issue(2, (
        _d(pid=11, port=8765, vault=r"C:\a\systemu\vault", cwd=r"C:\a"),
        _d(pid=22, port=8765, vault=r"C:\b\systemu\vault", cwd=r"C:\b"),
    ))
    assert issue is not None
    assert issue.severity == "danger"
    assert issue.message.startswith("2 systemu daemon processes are running.")
    assert DANGER_MESSAGE_TAIL in issue.message
    assert issue.cta == DANGER_CTA


def test_three_daemons_where_only_two_collide_is_still_danger():
    issue = hb.daemon_conflict_issue(3, (
        _d(pid=11, port=8765, vault="/a/systemu/vault"),
        _d(pid=22, port=8901, vault="/b/systemu/vault"),
        _d(pid=33, port=8765, vault="/c/systemu/vault"),
    ))
    assert issue.severity == "danger"
    assert DANGER_MESSAGE_TAIL in issue.message
    assert issue.cta == DANGER_CTA


# -- shape 2: DISTINCT PORTS -> an honest WARNING ----------------------------

def _distinct_issue():
    return hb.daemon_conflict_issue(2, (
        _d(pid=11, port=8765, vault=r"C:\work\systemu\vault", cwd=r"C:\work",
           is_self=True),
        _d(pid=22, port=8901, vault=r"C:\iso\systemu\vault", cwd=r"C:\iso"),
    ))


def test_distinct_ports_downgrade_to_warning():
    issue = _distinct_issue()
    assert issue is not None
    assert issue.severity == "warning", issue.message


def test_distinct_ports_drop_the_false_port_race_claim():
    """The whole defect: a true count asserting a race that is not happening."""
    issue = _distinct_issue()
    assert "port race" not in issue.message.lower()
    assert "wrong vault" not in issue.message.lower()


def test_distinct_ports_list_each_daemon_as_port_then_vault():
    issue = _distinct_issue()
    assert "port 8765 - vault C:\\work\\systemu\\vault" in issue.message
    assert "port 8901 - vault C:\\iso\\systemu\\vault" in issue.message


def test_distinct_ports_say_plainly_which_daemon_serves_this_dashboard():
    issue = _distinct_issue()
    low = issue.message.lower()
    assert "this dashboard is served by the daemon on" in low
    assert "port" in low


def test_the_count_survives_the_downgrade():
    """Machine-wide detection is KEPT -- only the diagnosis changed."""
    issue = _distinct_issue()
    assert issue.message.startswith("2 systemu daemon processes are running")


def test_distinct_ports_remedy_is_the_specific_stop_not_stop_all():
    """The over-broad advice is the second half of the defect: `stop --all`
    would have stopped the daemon serving this very page."""
    issue = _distinct_issue()
    cta = issue.cta or ""
    assert "systemu daemon stop" in cta
    # the invocable command form must NOT be the prescription any more
    assert "systemu daemon stop --all" not in cta, cta


def test_distinct_ports_remedy_names_the_other_daemons_working_folder():
    """`systemu daemon stop` resolves the vault from the CWD it runs in, so the
    remedy is worthless unless it names the folder to run it from."""
    issue = _distinct_issue()
    cta = issue.cta or ""
    assert r"C:\iso" in cta, cta
    # ...and must NOT aim the operator at the daemon serving this very page.
    assert r"C:\work" not in cta, cta


def test_distinct_ports_warn_against_stop_all():
    issue = _distinct_issue()
    assert "stop --all" in (issue.cta or ""), "the over-broad remedy must be named"
    assert "not" in (issue.cta or "").lower()


def test_warning_copy_is_ascii_only():
    """DEC-32c: verdict-carrying operator copy is ASCII-only."""
    issue = _distinct_issue()
    (issue.message + (issue.cta or "")).encode("ascii")


def test_without_a_self_record_every_daemon_is_offered_as_a_stop_target():
    """The dashboard may be served by something we could not match to a record;
    the remedy then names them all rather than guessing."""
    issue = hb.daemon_conflict_issue(2, (
        _d(pid=11, port=8765, vault="/a/systemu/vault", cwd="/a"),
        _d(pid=22, port=8901, vault="/b/systemu/vault", cwd="/b"),
    ))
    assert issue.severity == "warning"
    assert "/a" in (issue.cta or "") and "/b" in (issue.cta or "")


# -- shape 3: UNKNOWABLE PORT -> fail toward the stronger warning ------------

def test_one_unknown_port_falls_back_to_danger():
    issue = hb.daemon_conflict_issue(2, (
        _d(pid=11, port=8765, vault="/a/systemu/vault"),
        _d(pid=22, port=None, vault="/b/systemu/vault"),
    ))
    assert issue.severity == "danger"
    assert DANGER_MESSAGE_TAIL in issue.message
    assert issue.cta == DANGER_CTA


def test_no_records_at_all_falls_back_to_danger():
    """psutil denied us cmdlines: a count with no ports is exactly the old case."""
    issue = hb.daemon_conflict_issue(2, ())
    assert issue.severity == "danger"
    assert DANGER_MESSAGE_TAIL in issue.message


def test_records_that_do_not_account_for_the_count_fall_back_to_danger():
    """2 records for 3 processes means one sibling's port is unknown by
    omission -- indistinguishable from a racer, so it is treated as one."""
    issue = hb.daemon_conflict_issue(3, (
        _d(pid=11, port=8765, vault="/a/systemu/vault"),
        _d(pid=22, port=8901, vault="/b/systemu/vault"),
    ))
    assert issue.severity == "danger"
    assert issue.message.startswith("3 systemu daemon processes are running.")


def test_a_non_integer_port_is_not_a_known_port():
    """A sidecar is a file on disk any process may have left in any shape
    (DEC-36: pin the concrete type in this frame)."""
    for junk in ("8765", None, True, 0):
        issue = hb.daemon_conflict_issue(2, (
            _d(pid=11, port=8765, vault="/a"),
            _d(pid=22, port=junk, vault="/b"),
        ))
        assert issue.severity == "danger", junk


# -- the argv parser (pure: the daemon's own launch line) --------------------

def test_argv_parser_reads_port_and_vault_from_the_spawn_line():
    argv = ["python", "-m", "systemu.scheduler.daemon",
            "--vault-dir", r"C:\work\systemu\vault", "--port", "8901"]
    assert hb.parse_daemon_argv(argv) == (8901, r"C:\work\systemu\vault")


def test_argv_parser_reads_the_equals_spelling():
    argv = ["python", "-m", "systemu.scheduler.daemon",
            "--vault-dir=/v/systemu/vault", "--port=8123"]
    assert hb.parse_daemon_argv(argv) == (8123, "/v/systemu/vault")


def test_argv_parser_reports_unknown_rather_than_guessing_a_default():
    """DEFAULT_DASHBOARD_PORT is 8765; assuming it here would invent the very
    collision this banner is supposed to detect."""
    assert hb.parse_daemon_argv(["python", "-m", "systemu.scheduler.daemon"]) == (
        None, None)
    assert hb.parse_daemon_argv(["python", "-m", "systemu.scheduler.daemon",
                                 "--port", "not-a-port"]) == (None, None)
    assert hb.parse_daemon_argv(None) == (None, None)
    assert hb.parse_daemon_argv("--port 8765") == (None, None)


# -- purity + reachability ---------------------------------------------------

def test_the_model_function_touches_no_process_or_filesystem():
    src = inspect.getsource(hb.daemon_conflict_issue)
    for forbidden in ("psutil", "process_iter", "read_text", "open(", "getpid",
                      "_scan_", "socket"):
        assert forbidden not in src, forbidden


def _calls_in(func_name: str) -> set:
    """Every plain function name called inside `func_name` of health_banner."""
    tree = ast.parse(Path(hb.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        names.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        names.add(f.attr)
            return names
    raise AssertionError(f"{func_name} not found in health_banner")


def test_the_state_builder_consumes_the_model_function():
    """Reachability pin. Delete the ``daemon_conflict_issue(...)`` call from
    ``build_health_state`` and this goes red -- a port-aware model nothing
    consumes is the half-built shape this repo keeps shipping."""
    calls = _calls_in("build_health_state")
    assert "daemon_conflict_issue" in calls, sorted(calls)


def test_the_state_builder_feeds_the_model_real_process_records():
    calls = _calls_in("build_health_state")
    assert "_daemon_processes" in calls, sorted(calls)
    assert "_count_systemu_daemons" in calls, sorted(calls)


def test_the_renderer_consumes_the_state_builder():
    assert "build_health_state" in _calls_in("render_health_banner")


# -- end to end through build_health_state -----------------------------------

@pytest.fixture()
def _quiet_providers(monkeypatch):
    from systemu.runtime import provider_status as ps
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def test_build_health_state_downgrades_distinct_ports(monkeypatch, _quiet_providers):
    monkeypatch.setattr(hb, "_count_systemu_daemons", lambda *a, **k: 2)
    monkeypatch.setattr(hb, "_daemon_processes", lambda *a, **k: (
        _d(pid=11, port=8765, vault=r"C:\work\systemu\vault", cwd=r"C:\work",
           is_self=True),
        _d(pid=22, port=8901, vault=r"C:\iso\systemu\vault", cwd=r"C:\iso"),
    ))
    state = hb.build_health_state(vault_dir=None)
    hits = [i for i in state.issues if "systemu daemon processes" in i.message]
    assert len(hits) == 1
    assert hits[0].severity == "warning"
    assert state.worst_severity == "warning"
    assert "port 8901 - vault C:\\iso\\systemu\\vault" in hits[0].message


def test_build_health_state_keeps_danger_on_a_shared_port(monkeypatch,
                                                          _quiet_providers):
    monkeypatch.setattr(hb, "_count_systemu_daemons", lambda *a, **k: 2)
    monkeypatch.setattr(hb, "_daemon_processes", lambda *a, **k: (
        _d(pid=11, port=8765, vault=r"C:\work\systemu\vault"),
        _d(pid=22, port=8765, vault=r"C:\iso\systemu\vault"),
    ))
    state = hb.build_health_state(vault_dir=None)
    hits = [i for i in state.issues if "systemu daemon processes" in i.message]
    assert hits[0].severity == "danger"
    assert hits[0].cta == DANGER_CTA


def test_a_scan_that_raises_leaves_the_danger_case_intact(monkeypatch,
                                                          _quiet_providers):
    """The probe is best-effort; a probe that blew up must not soften anything."""
    def _boom(*a, **k):
        raise RuntimeError("psutil denied")
    monkeypatch.setattr(hb, "_count_systemu_daemons", lambda *a, **k: 2)
    monkeypatch.setattr(hb, "_scan_daemon_processes", _boom)
    hb._daemon_procs_cache["ts"] = -1e9
    state = hb.build_health_state(vault_dir=None)
    hits = [i for i in state.issues if "systemu daemon processes" in i.message]
    assert hits[0].severity == "danger"
    assert hits[0].cta == DANGER_CTA


def test_the_process_scan_returns_records_without_raising():
    """Live smoke: whatever this machine has, the probe answers with a tuple."""
    got = hb._scan_daemon_processes()
    assert type(got) is tuple
    for rec in got:
        assert type(rec) is hb.DaemonProcess
