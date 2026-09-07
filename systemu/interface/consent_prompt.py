"""THE ONE consent prompt for every command that CREATES a standing permission.

WHY THIS MODULE EXISTS (D12)
----------------------------
Two shipped commands ask an operator to agree to a standing permission:
``systemu roots grant <path>`` (a folder Systemu may survey and read from) and
``systemu census grant <category>`` (a category of this machine Systemu may
enumerate, repeatedly). Both print a disclosure and then ask. Piped the same
way, they answered differently::

    $ echo y | systemu roots grant ~/Documents
    Granted: /home/me/Documents                        # exit 0

    $ echo y | systemu census grant cloud_sync_roots
    Refusing to record consent: there is no terminal to ask on ...
      Re-run with --yes ...                            # exit 2

One of them took a byte off a pipe as informed consent; the other refused. A
wrapper written against either was wrong about the other, and "did my automation
just grant that?" had two answers depending on WHICH permission was involved.

THE RULE, in one place
----------------------
    no terminal                          -> refuse, exit 2 (name ``--yes``)
    terminal + y / yes                   -> proceed, exit 0
    terminal + Enter / n / anything else -> refused, exit 1
    ``--yes``                            -> proceed (disclosure still printed)

The stricter of the two rules is the right one: a ``y`` arriving on a pipe is
not a person who read a disclosure, and DEFAULTING to yes in a headless context
would take a standing permission nobody granted. ``--yes`` remains the
deliberate script path -- it means "I have read this and I agree", never "do not
tell me", so callers print their disclosure BEFORE asking, on every path.

The three outcomes are three EXIT CODES rather than one non-zero, because a
wrapper that retries on "nothing here can ask you" must not retry on "the
operator said no".

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not print. Each command's disclosure and refusal copy is its own -- the
census names the category and what a scan transmits, ``roots`` names the folder
and what a file NAME discloses -- and a generic sentence in place of either
would be a worse disclosure than both. What is shared is the DECISION: whether
there is a human to ask, what counts as a yes, and which code says so.
"""
from __future__ import annotations

import sys

import click

#: Win32 ``GetFileType`` -- a character device. A console is one of these, and
#: so is NUL, which is the whole reason the second probe below exists.
_FILE_TYPE_CHAR = 0x0002

_IS_WINDOWS = sys.platform == "win32"

#: The three outcomes. A caller returns or exits with these directly; the
#: census surface's ``CENSUS_EXIT_*`` names alias them so the two cannot drift.
CONSENT_GRANTED = 0
CONSENT_DECLINED = 1        # a person was asked and said no (or nothing)
CONSENT_NO_TERMINAL = 2     # there was nobody to ask, and --yes was not passed


def _kernel32():
    """The Win32 entry point the console probe goes through.

    Its own function so a test can make the probe EXPLODE without reaching into
    ``ctypes`` process-wide, and so the fail-closed path below is reachable.
    """
    import ctypes

    return ctypes.windll.kernel32


def _windows_stdin_is_a_console() -> bool:
    """Is this process's stdin an actual CONSOLE, on Windows?

    ``isatty()`` is not that question here. The CRT answers it for any
    CHARACTER device, and NUL is a character device -- so ``< NUL``, which is
    what a Service, a Task Scheduler action, ``pythonw`` and every
    ``stdin=subprocess.DEVNULL`` wrapper hand a process, reported True. The
    handle-level answer distinguishes them: a console and NUL are both
    ``FILE_TYPE_CHAR``, and only a console has a console MODE.

    Raises rather than swallowing; the single caller decides what a failure
    means (it means "not a terminal").
    """
    import ctypes
    import msvcrt

    handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdin.fileno()))
    k32 = _kernel32()
    # The handle is passed as an explicit c_void_p rather than a bare int: on
    # 64-bit Windows ctypes would otherwise marshal it as a C `int` and
    # truncate a high handle, and a truncated handle answers about nothing.
    if int(k32.GetFileType(handle)) != _FILE_TYPE_CHAR:
        return False        # a pipe or a regular file -- nobody is there
    mode = ctypes.c_uint32(0)
    # NUL is FILE_TYPE_CHAR and fails HERE. That single call is the fix.
    return int(k32.GetConsoleMode(handle, ctypes.byref(mode))) != 0


def stdin_is_a_terminal() -> bool:
    """Is there a human to ask? THE definition, for every consent surface.

    Its own function so the answer can be injected in tests without replacing
    ``sys.stdin``, and so both commands read the same one -- a second copy of
    this predicate is exactly how the two surfaces came to disagree.

    On Windows it asks the HANDLE, not the CRT, because ``sys.stdin.isatty()``
    is True for the NUL device (see :func:`_windows_stdin_is_a_console`) -- the
    canonical headless wiring on that platform therefore read as an operator
    standing at a prompt, and a headless run got exit 1 ("the operator said
    no") where it should get exit 2 ("there is nobody to ask; pass --yes").
    Everywhere else ``isatty()`` already means "a terminal" and is unchanged.

    Never raises, and fails CLOSED: a stdin that cannot answer -- no
    ``fileno()``, a kernel32 that will not load, a handle the OS refuses -- is
    not a terminal, which sends the caller down the refuse-and-name-the-flag
    path. Refusing costs the operator a re-run; agreeing by accident costs them
    a standing permission nobody granted.
    """
    try:
        if _IS_WINDOWS:
            return bool(_windows_stdin_is_a_console())
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def ask_for_consent(question: str, *, assume_yes: bool = False,
                    is_a_terminal=None) -> int:
    """Ask for consent under the rule above. Returns one of the three codes.

    ``assume_yes`` is the command's ``--yes`` flag and short-circuits BEFORE the
    terminal is consulted: the flag exists for the box that has no terminal, so
    consulting one first would make it useless exactly where it is needed.

    ``is_a_terminal`` overrides the predicate for one call. The census surface
    passes its own long-standing named wrapper (which delegates here) so that
    the tests injecting there keep working; everything else leaves it None.

    Never raises. Every failure -- a predicate that explodes, a prompt that
    aborts on EOF, an unparseable answer -- resolves to a REFUSAL, because
    refusing costs the operator a re-run and agreeing by accident costs them a
    permission they never granted.
    """
    if assume_yes:
        return CONSENT_GRANTED

    probe = is_a_terminal if callable(is_a_terminal) else stdin_is_a_terminal
    try:
        at_a_terminal = bool(probe())
    except Exception:
        at_a_terminal = False
    if not at_a_terminal:
        return CONSENT_NO_TERMINAL

    try:
        agreed = click.confirm(question, default=False)
    except click.Abort:
        # The terminal went away mid-question (a closed pipe, a ^C at the
        # prompt). Nothing was agreed to.
        return CONSENT_DECLINED
    except Exception:
        return CONSENT_DECLINED
    # DEC-36: pin the concrete type in the same frame rather than trusting the
    # truthiness of whatever came back. A prompt helper that returned a
    # non-empty string would otherwise read as consent.
    return CONSENT_GRANTED if agreed is True else CONSENT_DECLINED
