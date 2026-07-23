"""R-UX2 / SPEC Part II §15-UX UX-9(a) — the offload lint.

UX-9(a) names two mechanisms: the loop-lag watchdog (measures) and an **offload
lint** ("blocking calls in ``interface/`` handlers forbidden — the style-lint
pattern"). This is the lint.

It is deliberately AST-based rather than regex-based, because the regex form has
a false-positive class that is present in this very tree: ``event_bus.py`` calls
``self._approval_requests.get(request_id)``, whose text contains ``requests.get``.
A grep-shaped lint flags that line forever and gets switched off; an AST-shaped
one resolves the receiver and does not.

The largest block below is the FAIL-OPEN suite. Three separate paths used to
return the *clean* value for input that had never been examined, and a lint
whose "all clear" is indistinguishable from "I looked at nothing" is worse than
no lint at all — it is a green light nobody earned.
"""
from __future__ import annotations

import textwrap

import pytest

from tools.lint_offload import (OffloadLintError, find_violations,
                                iter_scanned_files, main, repo_root, scan_base,
                                scan_repo)


def _msgs(src: str):
    return [v.message for v in find_violations(textwrap.dedent(src), "f.py")]


# ── what it must catch ───────────────────────────────────────────────────────

def test_flags_a_bare_time_sleep():
    assert any("time.sleep" in m for m in _msgs("""
        import time
        def handler():
            time.sleep(2)
    """))


def test_resolves_import_aliases():
    """``import time as _time`` is how the one real violation in this tree was
    spelled (console.py) — an unresolved matcher misses it entirely."""
    assert any("time.sleep" in m for m in _msgs("""
        import time as _time
        def handler():
            _time.sleep(0.5)
    """))


def test_resolves_from_imports():
    assert any("time.sleep" in m for m in _msgs("""
        from time import sleep
        def handler():
            sleep(0.5)
    """))


def test_flags_blocking_subprocess_forms():
    for form in ("run", "call", "check_call", "check_output"):
        assert any("subprocess." in m for m in _msgs(f"""
            import subprocess
            def handler():
                subprocess.{form}(["x"])
        """)), form


def test_flags_sync_http_and_asyncio_run():
    assert any("requests.get" in m for m in _msgs("""
        import requests
        def handler():
            requests.get("https://x")
    """))
    assert any("asyncio.run" in m for m in _msgs("""
        import asyncio
        def handler():
            asyncio.run(x())
    """))


# ── the receiver the qualified matcher cannot name ───────────────────────────

def test_flags_a_blocking_wait_on_a_SUBSCRIPT_rooted_receiver():
    """The coverage overstatement this lint shipped with.

    ``interface/event_bus.py`` block-polls on ``gate["event"].wait(timeout=...)``.
    ``_dotted_name`` returns ``None`` for a subscript-rooted receiver, and
    ``threading.Event.wait`` was never in ``_BLOCKING`` either — so a real,
    unbounded wait was doubly invisible while the lint reported the tree clean.
    """
    assert any(".wait" in m for m in _msgs("""
        def resolve(gate, timeout_s):
            return gate["event"].wait(timeout=timeout_s)
    """))


def test_flags_a_blocking_wait_on_a_self_attribute_receiver():
    assert any(".wait" in m for m in _msgs("""
        class Bridge:
            def tail(self):
                if self._stop_event.wait(timeout=1.0):
                    return
    """))


def test_flags_a_wait_on_the_result_of_a_call():
    """Receiver rooted in a Call — also unnameable by the qualified pass."""
    assert any(".wait" in m for m in _msgs("""
        def go(factory):
            factory().wait(timeout=1.0)
    """))


def test_the_unqualified_method_set_is_narrow_enough_to_stay_green():
    """``.join()`` and ``.get()`` are deliberately NOT in the set.

    Measured under ``systemu/interface/``: ``.wait()`` 4 sites (all genuine
    blocking waits), ``.acquire()`` 0, ``.join()`` 82 (essentially all
    ``str.join``), ``.get()`` 893 (essentially all ``dict.get``). A lint that
    fires 975 times on day one is a lint somebody deletes.
    """
    assert _msgs("""
        def render(items, mapping):
            return ", ".join(items) + str(mapping.get("k"))
    """) == []


# ── what it must NOT catch ───────────────────────────────────────────────────

def test_does_not_flag_an_attribute_named_like_a_module():
    """THE false-positive class this tree actually contains (event_bus.py).

    ``self._approval_requests.get(...)`` textually contains ``requests.get``.
    """
    assert _msgs("""
        class Bus:
            def resolve(self, request_id):
                return self._approval_requests.get(request_id)
    """) == []


def test_does_not_flag_awaited_asyncio_sleep():
    """The cooperative form is the FIX, not the defect."""
    assert _msgs("""
        import asyncio
        async def handler():
            await asyncio.sleep(0.5)
    """) == []


def test_does_not_flag_subprocess_popen():
    """Popen does not wait — it is how you AVOID blocking."""
    assert _msgs("""
        import subprocess
        def spawn():
            return subprocess.Popen(["x"])
    """) == []


# ── the escape hatch ─────────────────────────────────────────────────────────

def test_marker_on_the_call_line_suppresses():
    assert _msgs("""
        import time
        def worker():
            time.sleep(2)  # offload-lint: ok — runs on a daemon thread
    """) == []


def test_marker_on_the_line_above_suppresses():
    assert _msgs("""
        import time
        def worker():
            # offload-lint: ok — runs on a daemon thread
            time.sleep(2)
    """) == []


def test_an_unrelated_comment_does_not_suppress():
    """Otherwise the lint passes on the strength of any nearby comment."""
    assert _msgs("""
        import time
        def worker():
            # this is fine, honest
            time.sleep(2)
    """) != []


def test_marker_anywhere_in_the_contiguous_comment_block_suppresses():
    """A justification naming WHERE a call runs rarely fits on one line."""
    assert _msgs("""
        import time
        def worker():
            # offload-lint: ok — runs on the "refine-launcher" daemon thread
            # started below, never on the event loop.
            time.sleep(2)
    """) == []


def test_a_marker_cannot_leak_down_past_intervening_code():
    """The scan stops at the first non-comment line, so a marker attached to an
    EARLIER statement cannot silently excuse a later, unrelated blocking call."""
    assert _msgs("""
        import time
        def worker():
            # offload-lint: ok — this excuses the next line only
            time.sleep(1)
            time.sleep(2)
    """) != []


# ── FAIL LOUD, never fail open ───────────────────────────────────────────────

def test_unparseable_input_raises_instead_of_returning_the_clean_value():
    """REGRESSION — ``except SyntaxError: return []``.

    ``[]`` is precisely the value that means "this file has no blocking calls".
    Handing it back for a file that was never parsed makes a broken tree
    indistinguishable from a clean one.
    """
    with pytest.raises(OffloadLintError) as exc:
        find_violations("def f(:\n    time.sleep(9)\n", "broken.py")
    assert "broken.py" in str(exc.value)
    assert "parse" in str(exc.value).lower()


def test_a_scan_base_that_does_not_exist_raises(tmp_path):
    """REGRESSION — ``scan_repo(Path('C:/')) == []``.

    ``rglob`` on a directory with no ``systemu/interface`` yields nothing, and
    "nothing found" was reported as "nothing wrong".
    """
    with pytest.raises(OffloadLintError):
        scan_repo(tmp_path)                     # exists, but is not a checkout
    with pytest.raises(OffloadLintError):
        scan_repo(tmp_path / "definitely-not-here")


def test_a_missing_base_says_WHY_not_merely_that_something_was_empty(tmp_path):
    """Two guards can both stop the fail-open, and here they do — with the
    ``base.is_dir()`` check removed, the "no Python files" guard still raises
    for every case above (verified by mutation). That makes the message the
    thing worth pinning: "not a systemu checkout" tells the operator they are in
    the wrong directory, where "nothing was scanned" leaves them guessing.
    """
    with pytest.raises(OffloadLintError, match="not a systemu checkout"):
        scan_repo(tmp_path)
    (tmp_path / "systemu" / "interface").mkdir(parents=True)
    with pytest.raises(OffloadLintError, match="nothing was scanned"):
        scan_repo(tmp_path)


def test_an_empty_interface_tree_raises_rather_than_reporting_clean(tmp_path):
    """The 'lint pointed at an empty dir' mutant: a base that exists but holds
    no Python files has told us nothing, and must not read as a pass."""
    (tmp_path / "systemu" / "interface").mkdir(parents=True)
    with pytest.raises(OffloadLintError):
        scan_repo(tmp_path)


def test_an_unparseable_file_inside_a_real_tree_is_reported_not_swallowed(tmp_path):
    """It must not abort the whole scan either — the other findings are still
    worth having — but it must appear, with its path, and force a non-zero exit.
    """
    iface = tmp_path / "systemu" / "interface"
    iface.mkdir(parents=True)
    (iface / "broken.py").write_text("def f(:\n", encoding="utf-8")
    (iface / "alsobad.py").write_text(
        "import time\ndef h():\n    time.sleep(3)\n", encoding="utf-8")

    found = scan_repo(tmp_path)
    joined = " ".join(v.message for v in found)
    assert "UNPARSEABLE" in joined, joined
    assert "broken.py" in " ".join(v.path for v in found)
    # the second file was still scanned
    assert any("time.sleep" in v.message for v in found), joined


def test_main_does_not_report_clean_from_a_foreign_working_directory(monkeypatch,
                                                                    tmp_path,
                                                                    capsys):
    """REGRESSION — ``main()`` exited 0 with "offload lint: clean" from ANY cwd
    that was not the repo root, because the base was ``Path.cwd()/...``.

    The base is now derived from ``__file__``, so the cwd is irrelevant: running
    from elsewhere scans the real tree rather than an imaginary empty one.
    """
    monkeypatch.chdir(tmp_path)
    rc = main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out
    # ...and it says how much it actually looked at, so "clean" is falsifiable
    assert "file(s) scanned" in out
    n = int(out.split("clean")[1].split("file(s)")[0].strip().split()[-1])
    assert n > 20, out


def test_main_returns_a_DISTINCT_code_when_it_could_not_run(monkeypatch, capsys):
    """Exit 1 means "violations found"; exit 2 means "the lint did not run".
    Collapsing the two would let a broken checkout read as a failing gate, or
    worse, a passing one."""
    import tools.lint_offload as mod

    def _boom(*a, **k):
        raise OffloadLintError("no checkout here")

    monkeypatch.setattr(mod, "scan_repo", _boom)
    assert mod.main() == 2
    assert "CANNOT RUN" in capsys.readouterr().err


def test_the_scope_limitation_is_printed_with_the_RESULT_not_only_in_the_source(
        monkeypatch, tmp_path, capsys):
    """The matcher still cannot see ``q.get()``/``conn.execute()``/C-extension
    blocking. That limit belongs where the "clean" is read, not in a docstring
    the reader of a green CI line never opens."""
    monkeypatch.chdir(tmp_path)
    main()
    out = capsys.readouterr().out
    assert "CANNOT see" in out
    assert "NOT 'the event loop is never blocked'" in out


def test_scan_base_rejects_a_falsy_root_rather_than_defaulting(tmp_path):
    """``root or repo_root()`` would silently turn an explicit-but-empty root
    into the default. ``Path('')`` normalises to the CWD, which is exactly the
    surprise this lint already shipped once."""
    with pytest.raises(OffloadLintError):
        scan_base(tmp_path / "nope")


# ── the actual gate ──────────────────────────────────────────────────────────

def test_the_real_interface_tree_is_clean():
    """The gate itself: every blocking call under ``systemu/interface/`` is
    either removed or explicitly accounted for with a reason."""
    violations = scan_repo()
    assert violations == [], "\n".join(
        f"{v.path}:{v.line} {v.message}" for v in violations)


def test_the_scan_actually_reaches_the_interface_tree():
    """Guards the vacuous-pass shape: a scan that silently walked an empty or
    wrong directory would make the gate above pass unconditionally."""
    files = list(iter_scanned_files())
    assert len(files) > 20, len(files)
    names = {f.name for f in files}
    assert "console.py" in names
    assert "dashboard.py" in names
    assert scan_base().is_dir()
    assert (repo_root() / "systemu" / "interface").is_dir()


def test_the_event_bus_wait_carries_a_real_justification_not_a_bare_marker():
    """The four ``.wait()`` sites the extended matcher exposed are annotated,
    and the annotation has to SAY something — the marker exists to make someone
    write down where the call runs, so a bare "# offload-lint: ok" would be the
    escape hatch swallowing the finding it was meant to surface."""
    import inspect

    from systemu.interface import event_bus
    src = inspect.getsource(event_bus.EventBus.request_approval)
    marker_lines = [ln for ln in src.splitlines() if "offload-lint: ok" in ln]
    assert marker_lines, src[-2000:]
    # the justification block names the thread/caller, not just "ok"
    assert "worker" in src.lower()
