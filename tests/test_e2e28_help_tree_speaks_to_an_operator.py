"""N5 + N6 + N8 -- every help surface in the tree speaks to an operator.

WITNESSED DEFECT (v0.10.28, a scratch install, COLUMNS=80)
    A human-style walk of ``--help`` across the whole command tree kept meeting
    this project's own build vocabulary in the text a first-time operator reads:

      * ``systemu --help`` and ``spend-caps --help`` -- a roadmap item id,
      * ``daemon start --help`` -- a decision-ledger id opening the paragraph,
      * ``daemon status --help`` -- the same, one ledger id further on,
      * ``debug --help`` / ``debug avoidable-forge --help`` /
        ``debug resolver-replay --help`` -- roadmap ids, capability ids, a
        section sign and a spec implementation note, plus middle dots,
      * ``doctor --help`` -- two roadmap ids,
      * ``find-tools --help`` -- a roadmap id and a capability id joined by a
        middle dot,
      * ``world --help`` -- a roadmap id and a spec section number,
      * ``user set --help`` -- an entire developer post-mortem ("F3: this used
        to refuse with ... F23 left the old program name in it deliberately"),
      * ``decisions --help`` / ``decisions mode --help`` -- section signs.

    None of these mean anything outside this repository. An operator who
    searches for ``R-P3b`` finds nothing, and the em dashes and middle dots come
    back as replacement characters on a cp437 console -- so the line the
    operator is meant to quote is the one that corrupts.

WHAT THIS FILE PINS
    The DURABLE fence, not the ten witnessed nodes: it walks the entire click
    tree reachable from the installed entry point (``sharing_on.cli:main``),
    invokes ``--help`` on EVERY node, and holds all of them to both rules --
    no internal identifiers, and ASCII only. A new command inherits the fence
    the moment it is registered.

    The identifiers stay wherever the next engineer needs them: code comments
    and non-rendered prose. What may not carry them is text the product PRINTS,
    and a click callback's docstring IS its ``--help``.
"""
from __future__ import annotations

import re

import click
import pytest
from click.testing import CliRunner

from sharing_on import cli as sharing_on_cli


# --------------------------------------------------------------------------- #
# The ruled classes
# --------------------------------------------------------------------------- #

#: Each pattern is matched against the text a node's ``--help`` actually prints.
_INTERNAL_IDENTIFIERS = {
    # The trailing ``[a-z]?`` is load-bearing, not decoration: the witnessed
    # leak in `spend-caps --help` is ``R-P3b``, and a pattern that ends at the
    # digit's word boundary reads straight past it.
    "roadmap item id (R-XX<n>)": re.compile(r"\bR-[A-Z]+\d+(\.\d+)?[a-z]?\b"),
    "decision-ledger id (DEC-<n>)": re.compile(r"\bDEC-\d+"),
    "spec implementation note (IMPL-<n>)": re.compile(r"\bIMPL-\d+"),
    "capability id (CAP-<n>)": re.compile(r"\bCAP-\d+[a-z]?\b"),
    "defect-log entry (F<n>:)": re.compile(r"\bF\d{1,2}:"),
    "spec section sign": re.compile("§"),
    "internal threshold symbol (T_high)": re.compile(r"T_high"),
}


def _offences(text: str) -> list[str]:
    """Every ruled violation in ``text``, each quoted with the line it sits on."""
    out: list[str] = []
    lines = text.splitlines()
    for name, pattern in _INTERNAL_IDENTIFIERS.items():
        for lineno, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if match:
                out.append(
                    f"{name}: {match.group()!r} on line {lineno}: {line.strip()!r}"
                )
    non_ascii = sorted({ch for ch in text if ord(ch) > 127})
    if non_ascii:
        for lineno, line in enumerate(lines, start=1):
            bad = sorted({ch for ch in line if ord(ch) > 127})
            if bad:
                out.append(
                    f"non-ASCII {bad!r} on line {lineno}: "
                    f"{line.strip().encode('ascii', 'backslashreplace').decode()!r}"
                )
    return out


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #

def _root_group() -> click.Group:
    """The click group the installed console script actually runs.

    ``sharing_on.cli:main`` is the ``[project.scripts]`` entry point for BOTH
    installed names; it calls exactly one group, and that group is the root of
    the tree an operator can reach.
    """
    return sharing_on_cli.cli


def _walk(node: click.Command, path: tuple[str, ...]) -> list[tuple[tuple[str, ...], click.Command]]:
    """Every node in the tree, depth-first, each with the words an operator types."""
    found = [(path, node)]
    if isinstance(node, click.Group):
        ctx = click.Context(node, info_name=path[-1] if path else "systemu")
        for name in sorted(node.list_commands(ctx)):
            child = node.get_command(ctx, name)
            if child is None:
                continue
            found.extend(_walk(child, path + (name,)))
    return found


def _every_node() -> list[tuple[tuple[str, ...], click.Command]]:
    return _walk(_root_group(), ("systemu",))


def _help_text(node: click.Command, path: tuple[str, ...]) -> str:
    """``--help`` for one node, rendered at the 80 columns a default console has."""
    runner = CliRunner()
    result = runner.invoke(node, ["--help"], obj={}, env={"COLUMNS": "80"})
    assert result.exit_code == 0, (
        f"`{' '.join(path)} --help` exited {result.exit_code}:\n{result.output}"
    )
    return result.output


def _node_ids() -> list[str]:
    return [" ".join(path) for path, _ in _every_node()]


# --------------------------------------------------------------------------- #
# THE FENCE
# --------------------------------------------------------------------------- #

def test_the_walk_reaches_the_whole_tree():
    """Anti-vacuity: a fence over an empty walk passes forever.

    Names a handful of the witnessed nodes explicitly, so a registration change
    that silently drops a whole group from the tree fails HERE rather than
    quietly narrowing the fence below.
    """
    ids = set(_node_ids())
    assert len(ids) > 40, f"the walk found only {len(ids)} nodes: {sorted(ids)}"
    for expected in (
        "systemu",
        "systemu daemon start",
        "systemu daemon status",
        "systemu spend-caps",
        "systemu debug",
        "systemu doctor",
        "systemu find-tools",
        "systemu world",
        "systemu user set",
        "systemu decisions",
        "systemu start",
    ):
        assert expected in ids, f"{expected!r} is not reachable from the entry point"


@pytest.mark.parametrize(
    "path,node",
    _every_node(),
    ids=_node_ids(),
)
def test_every_help_surface_speaks_to_an_operator(path, node):
    """No internal identifiers and ASCII only, on every node in the tree."""
    text = _help_text(node, path)
    offences = _offences(text)
    assert not offences, (
        "`" + " ".join(path) + " --help` does not speak to an operator:\n  "
        + "\n  ".join(offences)
    )


def test_the_fence_catches_every_witnessed_string():
    """Not vacuous: each class quoted in this module's docstring is rejected."""
    for witnessed in (
        "Ceilings on what a run may spend (R-P3b).",
        "DEC-41: the spawn is a CLAIM, not a witness.",
        "DEC-43: the verdict is the SAME mint.",
        "the R-A13.5 resolver replay",
        "R-CAP1 · CAP-4c admission",
        "the world scan (R-W1 § 5.11)",
        "IMPL-15 covers the rest",
        "F3: this used to refuse with a message",
        "asked only for the T_high confirm",
        "an em dash — like this one",
    ):
        assert _offences(witnessed), f"the fence let {witnessed!r} through"


def test_main_runs_the_group_this_fence_walks():
    """Reachability: the fence is worth nothing if it walks a group the installed
    console script does not run."""
    import inspect

    source = inspect.getsource(sharing_on_cli.main)
    assert "cli(" in source, (
        "sharing_on.cli:main no longer invokes `cli` -- this fence is walking a "
        f"tree the entry point does not run:\n{source}"
    )
