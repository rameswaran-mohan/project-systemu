"""N1 -- a NUL-device stdin is NOT a terminal, so a headless Windows run gets
exit 2 and not exit 1.

THE DEFECT (observed on a scratch v0.10.28 install, Windows 11)
    ``consent_prompt.stdin_is_a_terminal()`` was ``sys.stdin.isatty()``. On
    Windows the NUL device is a CHARACTER device, and the CRT answers
    ``isatty()`` for a character device with True::

        > python -c "import sys;print(sys.stdin.isatty())" < NUL
        True

    So the canonical headless shape on Windows -- a Service, a Task Scheduler
    action, ``pythonw``, or any wrapper that passes ``stdin=subprocess.DEVNULL``
    -- looked to this module exactly like an operator standing at a console::

        subprocess.run([systemu, "roots", "grant", P], stdin=subprocess.DEVNULL)
        subprocess.run([systemu, "census", "grant", "cloud_sync_roots"], ...)

    Both printed ``[y/N]:`` to nobody, read EOF, and exited **1** -- the code
    this module's own docstring reserves for "a person was asked and said no".
    An empty REGULAR file on stdin gave the correct **2**. The distinction the
    three exit codes exist for -- "nothing here can ask you" (retry with
    ``--yes``) versus "the operator said no" (do NOT retry) -- was therefore
    inverted on precisely the platform and precisely the wiring where headless
    automation lives.

THE PROPERTY
    On Windows, "there is a terminal to ask on" means the stdin handle is a
    CONSOLE, not merely a character device:

        GetFileType(handle) == FILE_TYPE_CHAR   AND   GetConsoleMode(handle)
        succeeds

    NUL passes the first and fails the second; a real console passes both. On
    every other platform ``isatty()`` already means this and is used unchanged.
    The probe fails CLOSED: anything that goes wrong inside it resolves to "not
    a terminal", which costs an operator a ``--yes`` re-run and never takes a
    standing permission nobody granted.

HOW IT IS WITNESSED
    By REAL child processes with real stdin wirings, because the whole defect
    lives in what the OS hands a process that nobody is sitting in front of --
    an in-process fake stdin cannot exhibit it. The positive control spawns the
    same probe on a genuine console handle (``CONIN$``) and is SKIPPED rather
    than faked when no console is attached: a positive control that manufactures
    its own True would witness nothing.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from systemu.interface import consent_prompt


IS_WINDOWS = sys.platform == "win32"

#: The child prints the module file it imported and then the verdict, so a run
#: that silently picked up a DIFFERENT installed ``systemu`` cannot pass.
_PROBE = (
    "import sys\n"
    "from systemu.interface import consent_prompt as cp\n"
    "sys.stdout.write(cp.__file__ + chr(10))\n"
    "sys.stdout.write(str(cp.stdin_is_a_terminal()) + chr(10))\n"
)


def _import_root() -> str:
    """The directory to put on the child's PYTHONPATH so it imports THIS tree.

    Derived from the module this test process actually imported, not from the
    test file's location: the two can differ, and the child must exercise the
    same bytes the parent is asserting about.
    """
    return str(Path(consent_prompt.__file__).resolve().parents[2])


def _child_env() -> dict:
    env = dict(os.environ)
    root = _import_root()
    existing = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    return env


def _run_probe(stdin) -> str:
    """Run the probe in a child with the given stdin wiring; return its verdict."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="backslashreplace",
        env=_child_env(),
        cwd=_import_root(),
        timeout=60,
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    lines = result.stdout.splitlines()
    assert len(lines) == 2, (result.stdout, result.stderr)
    module_file, verdict = lines
    # The child imported the module under test, not some other installed copy.
    assert os.path.normcase(os.path.normpath(module_file)) == os.path.normcase(
        os.path.normpath(str(Path(consent_prompt.__file__).resolve()))
    ), (module_file, consent_prompt.__file__)
    return verdict.strip()


# -- the headless wirings: none of them is a person -------------------------

def test_a_devnull_stdin_is_not_a_terminal():
    """THE repro. On Windows this is the NUL device, and it answered True.

    Every headless wrapper in the wild is shaped exactly like this line.
    """
    assert _run_probe(subprocess.DEVNULL) == "False"


def test_an_empty_regular_file_on_stdin_is_not_a_terminal(tmp_path):
    """The wiring that always answered correctly -- kept as the contrast that
    made the NUL answer visibly inconsistent rather than merely strict."""
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="ascii")
    with open(empty, "rb") as handle:
        assert _run_probe(handle) == "False"


def test_a_pipe_on_stdin_is_not_a_terminal():
    """A byte arriving on a pipe is not a person who read a disclosure."""
    assert _run_probe(subprocess.PIPE) == "False"


# -- the positive control: a REAL console still reads as a terminal ---------

def _console_is_attached():
    """(attached, an open CONIN$ handle or None). Never fakes an attachment."""
    if not IS_WINDOWS:
        return False, None
    try:
        handle = open("CONIN$", "rb")
    except Exception:
        return False, None
    try:
        raw = __import__("msvcrt").get_osfhandle(handle.fileno())
        mode = ctypes.c_uint32(0)
        ok = ctypes.windll.kernel32.GetConsoleMode(
            ctypes.c_void_p(raw), ctypes.byref(mode))
        if int(ok) == 0:
            handle.close()
            return False, None
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        return False, None
    return True, handle


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only console probe")
def test_a_real_console_stdin_is_a_terminal():
    """Without this, "not a terminal" everywhere would pass every test above
    and refuse every operator who is genuinely standing at a prompt.

    Skipped -- never faked -- when this run has no console attached.
    """
    attached, handle = _console_is_attached()
    if not attached:
        pytest.skip("no console attached")
    try:
        assert _run_probe(handle) == "True"
    finally:
        handle.close()


# -- fail closed ------------------------------------------------------------

@pytest.mark.skipif(not IS_WINDOWS, reason="the kernel32 seam is Windows-only")
def test_an_exploding_probe_is_not_a_terminal(monkeypatch):
    """Fail CLOSED. A probe that cannot answer must not answer "yes": refusing
    costs a re-run with --yes, agreeing takes a permission nobody granted."""

    def boom():
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(consent_prompt, "_kernel32", boom)
    assert consent_prompt.stdin_is_a_terminal() is False


def test_the_predicate_always_returns_a_real_bool():
    """DEC-36: callers branch on this in the same frame. A truthy non-bool
    (a ctypes result, a handle) would read as consent-is-possible."""
    value = consent_prompt.stdin_is_a_terminal()
    assert type(value) is bool, type(value)


# -- the consent rule the predicate feeds -----------------------------------

def test_a_headless_ask_returns_the_no_terminal_code_not_the_declined_one():
    """The point of the fix, stated as the operator sees it: exit 2 ("re-run
    with --yes"), not exit 1 ("the operator said no")."""
    code = consent_prompt.ask_for_consent(
        "Grant?", is_a_terminal=lambda: False)
    assert code == consent_prompt.CONSENT_NO_TERMINAL
    assert code == 2
