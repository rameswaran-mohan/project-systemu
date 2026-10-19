"""D-7 -- every line the start and refusal paths EMIT is ASCII.

THE WITNESS (0.10.31 human-style e2e)
-------------------------------------
Three surfaces on the path an operator hits first still carried an em dash::

    scheduler/daemon.py   [Daemon] Port %d is already in use by %s <U+2014> refusing to start.
    scheduler/daemon.py   [Daemon] Port %d is already in use <U+2014> another systemu daemon? ...
    interface/cli_commands.py   No LLM provider is usable yet <U+2014> let's set one up ...

DEC-32c. A console that cannot encode a verdict does not print a plainer one:
it raises UnicodeEncodeError where the answer should have been. On cp1252 and
cp437 -- the default code pages an operator's first `systemu daemon start`
meets on Windows -- U+2014 is unencodable, so the traceback lands exactly on
the screen that was supposed to say why nothing started. These three are the
REFUSALS: the two that say the port is taken, and the one that says no model
provider is configured. Losing any of them leaves the operator with a stack
trace and no diagnosis.

THE PROPERTY
    Every string constant handed to a logger, to `print`, to `click.echo` /
    `click.secho` or to `console.print` inside the START and REFUSAL seams is
    ASCII-encodable.

THE SEAMS, by name -- `daemon.start_daemon` (the parent's spawn and its two
guards), `daemon._run_daemon_loop` (the child's own boot, including the W13.7
port refusal) and the `daemon start` CLI callback (the dashboard-extra gate,
the provider gate and the verdict lines).

NOT FENCED: `daemon status`. Its panel bullets are decoration on a bordered
Rich panel, not verdicts, and are ruled to stay as they are; the verdict-
carrying lines it prints go out through the D6/N7/P8 unwrapped writers, which
have their own ASCII fences.

The fence reads the SOURCE, not a rendered screen: an emitted string is
unencodable whether or not this machine's console happens to be UTF-8, and a
test that only looked at captured output would pass on the developer's
terminal and fail on the operator's.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


#: The calls whose string arguments REACH AN OPERATOR -- a log file, a console,
#: a captured stderr. `logger.log`'s level argument is an int, so scanning every
#: argument of these calls costs nothing and misses nothing.
_EMITTERS = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
    "echo", "secho", "print",
})

#: The seams under fence, by the name the failure message must be able to say.
_SEAMS = {
    "daemon.start_daemon": daemon_mod.start_daemon,
    "daemon._run_daemon_loop": daemon_mod._run_daemon_loop,
    "cli.daemon_start": cc.daemon_start.callback,
}


def _emitted_strings(fn):
    """Every string constant this function hands to an emitter, with the
    emitter's own name -- so a finding names the call, not just the text."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name not in _EMITTERS:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    found.append((name, sub.value))
    return found


def _offenders(fn):
    out = []
    for emitter, text in _emitted_strings(fn):
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            out.append("{}(...): {}".format(
                emitter, text.encode("ascii", "backslashreplace").decode("ascii")))
    return out


# --------------------------------------------------------------------------- #
# THE FENCE
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("seam", sorted(_SEAMS))
def test_every_emitted_line_on_the_start_and_refusal_paths_is_ascii(seam):
    offenders = _offenders(_SEAMS[seam])

    assert not offenders, (
        "{} emits {} line(s) a cp1252/cp437 console cannot encode -- on those "
        "consoles the operator gets a UnicodeEncodeError where the diagnosis "
        "should have been (DEC-32c):\n  {}".format(
            seam, len(offenders), "\n  ".join(offenders)))


def test_the_fence_has_something_to_check():
    """A fence over an empty set passes by vacuity. Premise: each seam really
    does emit lines, and the extractor really does find them."""
    for seam, fn in sorted(_SEAMS.items()):
        assert _emitted_strings(fn), (
            "the extractor found no emitted string in {} -- the fence above is "
            "vacuous".format(seam))


# --------------------------------------------------------------------------- #
# the three witnessed lines, by their own words
# --------------------------------------------------------------------------- #

def test_the_port_in_use_refusal_says_it_in_ascii():
    """`daemon start` against a taken port. The operator sees this one before
    they see anything else."""
    lines = [t for _e, t in _emitted_strings(daemon_mod.start_daemon)
             if "already in use by" in t]

    assert lines, "the port-in-use refusal is gone from `start_daemon`"
    for text in lines:
        text.encode("ascii")
        assert "\u2014" not in text, text
        assert "refusing to start" in text, text


def test_the_childs_own_port_refusal_says_it_in_ascii():
    """W13.7's refusal, inside the daemon child: five daemons racing one port
    was a field hazard, and this is the line that names it."""
    lines = [t for _e, t in _emitted_strings(daemon_mod._run_daemon_loop)
             if "already in use" in t]

    assert lines, "the W13.7 port refusal is gone from `_run_daemon_loop`"
    for text in lines:
        text.encode("ascii")
        assert "\u2014" not in text, text


def test_the_no_provider_refusal_says_it_in_ascii():
    """The interactive branch: a first install with no key configured."""
    lines = [t for _e, t in _emitted_strings(cc.daemon_start.callback)
             if "provider" in t.lower()]

    assert lines, "the no-provider branch no longer says anything"
    for text in lines:
        text.encode("ascii")
        assert "\u2014" not in text, text
