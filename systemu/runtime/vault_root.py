"""vault_root -- THE ONE MINT for "which directory is the operating vault".

WHY THIS EXISTS
    Every process in the system used to answer this question for itself, from
    the same relative string (``systemu/vault``) resolved against whatever cwd
    that process happened to have.  The CLI stood in the operator's directory;
    the daemon child it spawned stood in ``Path(systemu.__file__).parent.parent``
    whenever the operator's directory did not happen to contain a ``.env`` or a
    ``.systemu_mode`` marker.  One string, two processes, two different vaults.

    Observed live (v0.10.23+): `systemu daemon start` from an EMPTY working
    directory wrote its runtime state into the CHECKOUT -- daemon sidecar, first
    gate review marker, package manifest baseline, dashboard session secret,
    capabilities/metrics/table stores, tool bodies -- and ran the effect-tags
    migration IN PLACE over the packaged seed tools (57 tracked files modified).
    On a pip install the same shape mutates site-packages, which the next
    upgrade then destroys.  The daemon's build record landed in one vault while
    the CLI read the other, which is why the same boot also reported an
    "UNVERIFIED build".

THE RULE (one sentence)
    The operating vault root is derived from the OPERATING HOME -- the process's
    own current working directory -- and never from where the package happens to
    be installed.

    Ordered inputs, all resolved to ONE absolute path:
      1. an ``explicit`` value, when a caller already holds a chosen root (this
         is how the parent CLI hands its answer to the spawned child);
      2. ``SYSTEMU_VAULT_DIR``, used verbatim when absolute (Docker mounts) and
         resolved against the operating home when relative;
      3. otherwise ``<home>/systemu/vault``.

THE FENCE (DEC-32: a fence is a VALUE, not a raise)
    :func:`resolve_vault_root` returns a frozen :class:`VaultRootVerdict` whose
    ``refused`` bit crosses the boundary with the path itself.  Callers read the
    bit in the same frame in which they decide to boot; there is no exception
    for an intermediate frame to swallow, and the refusal path (a printed
    message + a nonzero exit / a not-ready verdict) is not reachable from the
    success path.

    The fence refuses a root that lands inside ``Path(systemu.__file__).parent``
    -- UNLESS the package directory is itself inside the operating home.  That
    carve-out is the source checkout: standing in the tree that CONTAINS the
    package makes ``./systemu/vault`` the working vault by construction, and has
    since v0.7.4.  The defect being fenced is falling INTO a package tree the
    operator is not standing in, which is a different fact and is what the
    ``refused`` bit reports.

TWO HOMES, TWO QUESTIONS (N9)
    ``home`` is the directory the root was RESOLVED AGAINST -- this process's
    cwd.  It answers "where is the operator standing", and the carve-out above
    is the only thing that may consult it: a root pointed at
    ``<site-packages>/systemu/vault`` must not be able to carve ITSELF out of
    its own refusal, which is exactly what happens if the carve-out is moved to
    a root-derived directory.

    ``operating_home`` is where FILES GO -- see :func:`operating_home_for`.  It
    is a pure function of the resolved root, so every process holding the same
    root names the same home whatever cwd it was launched from.  It is a DERIVED
    member rather than a stored one, so not even a hand-built verdict can carry
    a home that contradicts its root.  The daemon child's cwd and
    ``execution_snapshot.audit_data_root()`` both consume this one member;
    nothing downstream re-derives either from a cwd.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

#: The operating vault, relative to the operating home, when nothing else says.
DEFAULT_RELATIVE_VAULT = "systemu/vault"

#: The one env var that names the operating vault.
VAULT_DIR_ENV = "SYSTEMU_VAULT_DIR"


def package_dir() -> Path:
    """The directory of the ``systemu`` package THIS process imported."""
    import systemu

    return Path(systemu.__file__).resolve().parent


def _norm(path) -> str:
    """One absolute, comparison-ready spelling of a path.

    Deliberately string-level: ``os.path`` comparisons neither dispatch on the
    operand's type nor follow symlinks, so the containment answer below is the
    same one the filesystem will give the writer.
    """
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def _within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lies underneath it."""
    c = os.path.normcase(_norm(child))
    p = os.path.normcase(_norm(parent))
    return c == p or c.startswith(p + os.sep)


def operating_home_for(root: str) -> str:
    """THE operating home of a vault root -- a pure function of the root alone.

    N9. This is the ONE definition, for EVERY layout: the vault directory's
    parent, unless the root is the default ``<home>/systemu/vault`` layout, in
    which case ``<home>``.  Nothing here reads a cwd, an env var or the clock,
    so two processes that resolved the same root name the same home however they
    were launched -- which is the whole property the audit tree needs and the
    one a cwd could never provide.

    The defect this replaces: the audit-root resolver inverted the default
    layout (correct, and cwd-free) and then fell back to the PROCESS CWD for
    every other layout, so a mounted vault -- exactly the shape the mint honours
    verbatim -- split the writer's tree from the reader's again.  The daemon
    derived its child's cwd from the same cwd-shaped field, so the two answers
    were both accidents of whoever asked first.
    """
    norm = _norm(root)
    parts = Path(norm).parts
    suffix = Path(DEFAULT_RELATIVE_VAULT).parts
    if len(parts) > len(suffix):
        tail = tuple(os.path.normcase(p) for p in parts[-len(suffix):])
        if tail == tuple(os.path.normcase(p) for p in suffix):
            return _norm(Path(*parts[:-len(suffix)]))
    return _norm(Path(norm).parent)


@dataclass(frozen=True)
class VaultRootVerdict:
    """The minted answer.  ``refused`` is the fence bit; it travels WITH the
    path so no caller can act on the path without having been handed the
    verdict about it."""

    root: str            # absolute, normalised
    # The directory the root was RESOLVED AGAINST -- this process's cwd. It
    # answers "where is the operator standing", which is what the package-tree
    # carve-out below needs and NOT what a consumer wants when it needs a home
    # to put files under. Read `operating_home` for that; see N9 in
    # `operating_home_for`.
    home: str
    source: str          # "explicit" | "env" | "default"
    package_dir: str     # the package tree this process imported
    inside_package: bool
    refused: bool
    reason: str          # "" unless refused

    @property
    def operating_home(self) -> str:
        """THE operating home (N9) -- where FILES GO for this verdict's layout.

        A DERIVED member, deliberately not a stored one. A stored field could be
        handed a value that contradicts ``root``, and "two answers, one of them
        wrong" is the entire defect class this closes; as a property it is
        recomputed from ``root`` by :func:`operating_home_for` every time, so no
        constructor -- test double included -- can mint a verdict whose home and
        root disagree.

        Identical in every process that resolved the same root, whatever cwd
        each was launched from. ``audit_data_root()`` and the daemon child's cwd
        both consume THIS, never ``home``.
        """
        return operating_home_for(self.root)


def resolve_vault_root(
    *,
    explicit: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> VaultRootVerdict:
    """Mint the operating vault root.  Pure: no directory is created or read."""
    environ = os.environ if env is None else env
    home = _norm(cwd if cwd is not None else os.getcwd())

    raw = "" if explicit is None else str(explicit).strip()
    if raw:
        source = "explicit"
    else:
        raw = (environ.get(VAULT_DIR_ENV) or "").strip()
        source = "env" if raw else "default"
        if not raw:
            raw = DEFAULT_RELATIVE_VAULT

    candidate = os.path.expanduser(raw)
    if not os.path.isabs(candidate):
        candidate = os.path.join(home, candidate)
    root = _norm(candidate)

    pkg = str(package_dir())
    inside_package = _within(root, pkg)
    # The source-checkout carve-out -- see the module docstring.
    home_contains_package = _within(pkg, home)
    refused = inside_package and not home_contains_package

    reason = ""
    if refused:
        reason = (
            "REFUSED: the operating vault root {root} is inside the installed "
            "systemu package {pkg}. Booting here writes runtime state into the "
            "package and migrates the packaged seed catalog in place. The "
            "operating home is {home} (source: {source})."
        ).format(root=root, pkg=pkg, home=home, source=source)

    return VaultRootVerdict(
        root=root,
        home=home,
        source=source,
        package_dir=pkg,
        inside_package=inside_package,
        refused=refused,
        reason=reason,
    )


def refusal_message(verdict: VaultRootVerdict) -> str:
    """The operator-facing text for a refused verdict. ASCII-only (DEC-32c)."""
    return "\n".join([
        "REFUSED: the operating vault would live inside the systemu package.",
        "",
        "  resolved vault root : {}".format(verdict.root),
        "  systemu package dir : {}".format(verdict.package_dir),
        "  resolved against    : {}".format(verdict.home),
        "  operating home      : {}".format(verdict.operating_home),
        "  root chosen from    : {}".format(verdict.source),
        "",
        "Starting here would write daemon state, secrets and capability stores",
        "into the package tree and migrate the packaged seed catalog in place.",
        "On a pip install that mutates site-packages and the next upgrade",
        "destroys it.",
        "",
        "Fix one of:",
        "  cd <a working directory of your own> && systemu init",
        "  {}=<an absolute path outside the package> systemu daemon start".format(
            VAULT_DIR_ENV),
    ])
