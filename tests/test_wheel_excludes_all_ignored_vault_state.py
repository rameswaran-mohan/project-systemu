"""EVERY IGNORED PATH UNDER systemu/ IS FENCED OUT OF THE WHEEL, NOT SIX OF THEM.

THE LIVE DEFECT
    `tests/test_wheel_excludes_runtime_state.py` proved the class: the
    `include-package-data` globs in `pyproject.toml` read the FILESYSTEM, not
    the git index, so any runtime state sitting under `systemu/vault/` at build
    time is swept into the wheel. It fenced the five paths PyPI 0.10.25 was
    caught shipping, plus `systemu/data/`.

    `.gitignore` names about forty such paths. Six were fenced. Before this
    test, a build from a tree carrying a dropping at every ignored path swept
    41 of them into the wheel -- `secrets/`, `audit/`, `world_model/`,
    `capabilities/`, `table/`, `metrics/`, `messaging/`, `executions/`, the
    runtime `shadow_shadow_*/` army, and the loose side-stores
    `dashboard_auth.json`, `granted_roots.json`, `command_approvals.json`,
    `census_consent.json`, `user_profile.json` and the rest. Two of those are
    credential-class: `secrets/` is the per-vault secret store and
    `dashboard_auth.json` holds the dashboard credential. A wheel that carries
    them discloses the builder's machine to every installer, with no review
    step.

    Be exact about what did NOT leak, because it is the reason (e) exists.
    `.credentials.json`, `.systemu_daemon.json`, `.effect_tags_seed` and
    `.first_gate_review` stayed out of that wheel, and NOT because anything
    fenced them: `glob` skips names beginning with a dot, and no include glob
    matches an extensionless file or a `*.lock`. That is an accident of the
    include side, one widened glob away from reversing.

THE ORACLE IS `.gitignore` ITSELF
    The planted set is DERIVED from `.gitignore` at test time, never typed out
    here. A hand-typed list is the defect this test exists to close -- it was
    exactly a hand-typed six that left the other thirty-odd open. Add an entry
    to `.gitignore` and it becomes a planted case on the next run, so the fence
    cannot silently fall behind the ignore file again.

PROPERTIES
    (a) NO planted path is a wheel member.
    (b) NO wheel member under `systemu/vault/`, `systemu/data/` or
        `systemu/captures/` is absent from `git ls-files`. This is the
        invariant that catches the classes nobody enumerated: it does not care
        whether a leaking file was ever named in `.gitignore`.

        NOTE the deliberate strictness: (b) treats "untracked" as "must not
        ship", so a brand-new, perfectly legitimate seed file that has been
        written to disk but not yet `git add`ed will red this test. That is the
        intended reading. A release is built from a clean checkout, where
        untracked and runtime-dropping are the same thing; a new seed file that
        is meant to ship is a file that is meant to be committed.
    (c) Every tracked `systemu/vault` file except the `.gitkeep` IS a member.
        An exclusion broad enough to drop the droppings and the seed with them
        would leave a bare `pip install systemu` with no tool pack. If a
        tracked file disappears, NARROW the pattern -- do not relax this.
    (d) Positive controls: the planting really happened on disk before the
        build, and the tracked-seed oracle is non-empty.
    (e) A pyproject self-consistency pin: for every ignore pattern under
        `systemu/vault/`, some `[tool.setuptools.exclude-package-data]` entry
        under BOTH package keys fnmatch-covers the representative path. This is
        NOT a cheaper restatement of the wheel assertions. It covers the
        entries the wheel CANNOT witness: a `*.lock`, a `.effect_tags_seed` or
        a `.first_gate_review` matches none of the include globs today, so it
        never ships and (a) stays green whether or not it is fenced. Widen one
        include glob to `vault/**/*` and those become live leaks with no
        warning. (e) is what holds them.

WHY BOTH PACKAGE KEYS
    The same file is reachable through two packages -- `systemu` (glob
    `vault/**/*.json`) and `systemu.vault` (glob `**/*.json`) -- and setuptools
    applies `exclude_package_data` PER PACKAGE, after the includes. A file
    ships if EITHER package includes it and does not exclude it, so an entry
    present under only one key fences nothing.

WITNESS / REACHABILITY PIN
    Delete one directory entry (say `vault/secrets/*`) from either key in
    `[tool.setuptools.exclude-package-data]` and
    `test_no_ignored_path_is_a_wheel_member` goes red naming the leaked path.

COST
    One wheel build, from a tmp_path copy of the git-TRACKED sources only plus
    the planted droppings, so the developer's own dirty `systemu/vault/` cannot
    make this test pass or fail by accident. Nothing is ever written into the
    checkout's own `systemu/vault/`. Marked `slow`.
"""
from __future__ import annotations

import fnmatch
import json
import pathlib
import shutil
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
import zipfile

import pytest

# Reuse the sibling fence's build helper rather than forking it: one wheel
# build mechanism, so a change to how the wheel is produced cannot make the two
# fences disagree about what "the wheel" means.
_TESTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_wheel_excludes_runtime_state import (  # noqa: E402
    REPO,
    _SOURCE_ROOTS,
    _build_wheel,
    _tracked,
)

pytestmark = pytest.mark.slow

# The prefix that makes a `.gitignore` entry this test's business.
_IGNORE_PREFIX = "systemu/"

# Wheel-member prefixes subject to the "must be tracked" invariant (b).
_TRACKED_ONLY_PREFIXES = ("systemu/vault/", "systemu/data/", "systemu/captures/")

# Filler used to turn a glob pattern into a concrete planted path. Chosen so it
# cannot collide with the ONE un-ignored runtime-shaped directory,
# `shadow_army/shadow_shadow_wildcard/` -- test_no_plant_lands_on_an_unignored_path
# proves that rather than trusting it.
_STAR_FILL = "planted"
_DOUBLESTAR_FILL = "planted_dir/nested"

_PLANT_TAG = "test_wheel_excludes_all_ignored_vault_state"


def _norm(p: str) -> str:
    return p.replace("\\", "/")


# ---------------------------------------------------------------------------
# The oracle: .gitignore
# ---------------------------------------------------------------------------
def _read_gitignore_patterns() -> tuple[list[str], list[str]]:
    """(ignored, un-ignored) patterns under `systemu/`, verbatim from the file.

    Trailing `/` is preserved -- it is what distinguishes a directory pattern
    from a file pattern, and the planting depends on that distinction.
    """
    text = (REPO / ".gitignore").read_text(encoding="utf-8")
    ignored: list[str] = []
    unignored: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            body = line[1:]
            if body.startswith(_IGNORE_PREFIX):
                unignored.append(body)
            continue
        if line.startswith(_IGNORE_PREFIX):
            ignored.append(line)
    return ignored, unignored


def _concrete_stem(pattern: str) -> str:
    """One `.gitignore` pattern -> one concrete repo-relative path stem.

    Deterministic and mechanical: `**` becomes a two-deep directory, a single
    `*` becomes one literal name component. No per-pattern special cases, so a
    future ignore entry needs no code change here.
    """
    stem = pattern.rstrip("/")
    stem = stem.replace("**", _DOUBLESTAR_FILL)
    return stem.replace("*", _STAR_FILL)


def _plants_for(pattern: str) -> list[str]:
    """Representative files to plant for one ignore pattern.

    A directory pattern gets four plants -- a shipping extension, a second
    shipping extension, a NESTED file (a fence written as `<dir>/x` and not
    `<dir>/*` passes the flat case and leaks the nested one) and a
    non-shipping extension.
    """
    stem = _concrete_stem(pattern)
    if pattern.endswith("/"):
        return [
            stem + "/planted.json",
            stem + "/planted.md",
            stem + "/nested/planted.json",
            stem + "/planted.txt",
        ]
    return [stem]


@pytest.fixture(scope="module")
def ignore_patterns() -> list[str]:
    ignored, _ = _read_gitignore_patterns()
    # Positive control on the oracle itself: an empty or mis-parsed .gitignore
    # would plant nothing and every assertion below would pass vacuously.
    assert len(ignored) >= 30, (
        "the .gitignore oracle returned only "
        + str(len(ignored))
        + " pattern(s) under 'systemu/' -- it is supposed to name about forty "
        "runtime paths. Parsing is broken, so nothing below could have failed."
    )
    return ignored


@pytest.fixture(scope="module")
def planted_paths(ignore_patterns) -> list[str]:
    seen: list[str] = []
    for pattern in ignore_patterns:
        for rel in _plants_for(pattern):
            if rel not in seen:
                seen.append(rel)
    return seen


# ---------------------------------------------------------------------------
# The wheel
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def built(tmp_path_factory, planted_paths):
    """Build ONE wheel from tracked sources + a dropping at every ignored path.

    Returns (wheel_members, src_root). Planting happens ONLY in the tmp copy;
    the checkout's own `systemu/vault/` is never written to.
    """
    src = tmp_path_factory.mktemp("allignored")

    for rel in _SOURCE_ROOTS:
        origin = REPO / rel
        if origin.is_dir():
            for tracked in _tracked(rel):
                dst = src / tracked
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / tracked, dst)
        else:
            shutil.copy2(origin, src / rel)

    body = json.dumps({"planted_by": _PLANT_TAG})
    for rel in planted_paths:
        dst = src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # A .jsonl store is one JSON object per line; everything else -- plain
        # .json, .md, .txt, .lock and the extensionless dotfiles -- takes the
        # same object as its whole content.
        dst.write_text(
            body + "\n" if rel.endswith(".jsonl") else body, encoding="utf-8"
        )

    wheel = _build_wheel(src)
    with zipfile.ZipFile(wheel) as zf:
        members = [_norm(n) for n in zf.namelist() if not n.endswith("/")]
    return members, src


@pytest.fixture(scope="module")
def wheel_members(built) -> list[str]:
    return built[0]


@pytest.fixture(scope="module")
def tracked_vault_files() -> list[str]:
    paths = [p for p in _tracked("systemu/vault") if not p.endswith(".gitkeep")]
    assert any(p.startswith("systemu/vault/tools/") for p in paths), (
        "git ls-files returned no seed tools under systemu/vault/ -- the "
        "oracle is broken, so nothing below could have failed. got "
        + str(len(paths))
    )
    return paths


# ---------------------------------------------------------------------------
# (d) positive controls
# ---------------------------------------------------------------------------
def test_the_planting_really_happened(built, planted_paths):
    """Without this, (a) would be a green assertion over an empty set."""
    _, src = built
    missing = sorted(rel for rel in planted_paths if not (src / rel).is_file())
    assert not missing, (
        str(len(missing))
        + " planted dropping(s) were never written to disk, so the exclusion "
        "assertion witnesses nothing: " + ", ".join(missing[:20])
    )
    # Named anchors: the credential-class paths this slice exists for. If the
    # derivation ever stops producing these, the test has lost its point.
    for anchor in (
        "systemu/vault/secrets/planted.json",
        "systemu/vault/dashboard_auth.json",
        "systemu/vault/.credentials.json",
        "systemu/vault/audit/nested/planted.json",
    ):
        assert anchor in planted_paths, (
            anchor + " is no longer in the planted set derived from .gitignore"
        )


def test_no_plant_lands_on_an_unignored_path(planted_paths):
    """`.gitignore` un-ignores the ONE seeded Wild Card shadow.

    If the glob filler ever produced `shadow_shadow_wildcard`, the test would
    be planting on top of tracked seed content and (a) and (c) would contradict
    each other. Prove it does not.
    """
    _, unignored = _read_gitignore_patterns()
    assert unignored, ".gitignore no longer un-ignores anything under systemu/"
    prefixes = tuple(p.rstrip("/*").rstrip("/") + "/" for p in unignored)
    collisions = sorted(p for p in planted_paths if p.startswith(prefixes))
    assert not collisions, (
        "a dropping was planted inside an un-ignored (tracked seed) path: "
        + ", ".join(collisions)
    )


def test_the_wheel_has_members(wheel_members):
    assert wheel_members, "the built wheel has no members at all"
    assert any(n.startswith("systemu/") for n in wheel_members)


# ---------------------------------------------------------------------------
# (a) the fence
# ---------------------------------------------------------------------------
def test_no_ignored_path_is_a_wheel_member(wheel_members, planted_paths):
    """THE FENCE. Every path `.gitignore` names is absent from the wheel."""
    members = set(wheel_members)
    leaked = sorted(p for p in planted_paths if p in members)
    assert not leaked, (
        str(len(leaked))
        + " ignored runtime path(s) were swept into the wheel:\n  "
        + "\n  ".join(leaked)
        + "\nThese are in .gitignore, but setuptools reads the filesystem, not "
        "the index. Each needs an [tool.setuptools.exclude-package-data] entry "
        "under BOTH the 'systemu' and 'systemu.vault' keys."
    )


# ---------------------------------------------------------------------------
# (b) the invariant that does not depend on enumeration
# ---------------------------------------------------------------------------
def test_every_shipped_vault_member_is_git_tracked(wheel_members):
    """MEMBERS SUBSET TRACKED. Catches the classes nobody wrote down.

    See the module docstring: "untracked" is deliberately read as "must not
    ship". A release wheel is built from a clean checkout.
    """
    tracked = set(_tracked("systemu/vault", "systemu/data", "systemu/captures"))
    untracked = sorted(
        n for n in wheel_members
        if n.startswith(_TRACKED_ONLY_PREFIXES) and n not in tracked
    )
    assert not untracked, (
        str(len(untracked))
        + " wheel member(s) under a runtime-state root are not in "
        "`git ls-files` -- the wheel is carrying something that is not seed:"
        "\n  " + "\n  ".join(untracked)
    )


# ---------------------------------------------------------------------------
# (c) the other half: the seed survives
# ---------------------------------------------------------------------------
def test_every_tracked_vault_file_still_ships(wheel_members, tracked_vault_files):
    """If this reds, NARROW the new exclusion -- never drop this assertion."""
    members = set(wheel_members)
    missing = sorted(p for p in tracked_vault_files if p not in members)
    assert not missing, (
        str(len(missing))
        + " tracked seed file(s) stopped shipping -- the exclusion is too "
        "broad:\n  " + "\n  ".join(missing[:20])
    )


# ---------------------------------------------------------------------------
# (e) pyproject self-consistency
# ---------------------------------------------------------------------------
_EXCLUDE_KEYS = {"systemu": "systemu", "systemu.vault": "systemu/vault"}


@pytest.fixture(scope="module")
def exclude_package_data() -> dict:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    spec = data["tool"]["setuptools"]["exclude-package-data"]
    for key in _EXCLUDE_KEYS:
        assert spec.get(key), (
            "[tool.setuptools.exclude-package-data] has no non-empty '"
            + key
            + "' list -- half the fence is missing outright"
        )
    return spec


def test_every_vault_ignore_pattern_is_excluded_under_both_keys(
    exclude_package_data, ignore_patterns, planted_paths
):
    """Pattern-level mirror of `.gitignore`, for both package keys.

    Matches the way setuptools does (`fnmatch` of `<src_dir>/<pattern>` against
    the path, where `*` also matches the separator), so a pattern that looks
    right but cannot match is caught here rather than shipping.
    """
    vault_plants = [p for p in planted_paths if p.startswith("systemu/vault/")]
    assert vault_plants, "no planted path lives under systemu/vault/"

    gaps: list[str] = []
    for key, src_dir in sorted(_EXCLUDE_KEYS.items()):
        patterns = [src_dir + "/" + pat for pat in exclude_package_data[key]]
        for rel in vault_plants:
            if not any(fnmatch.fnmatch(rel, pat) for pat in patterns):
                gaps.append(key + ": " + rel)
    assert not gaps, (
        str(len(gaps))
        + " ignored vault path(s) have no exclude-package-data entry covering "
        "them. A file ships if EITHER package includes it, so an entry under "
        "one key only fences nothing:\n  " + "\n  ".join(gaps)
    )


def test_no_exclude_entry_fences_out_a_tracked_seed_file(
    exclude_package_data, tracked_vault_files
):
    """The exclusion list itself must not name anything the seed needs.

    (c) catches this too, but only for files a build actually emits. This reads
    the patterns directly, so an over-broad entry is named as an over-broad
    ENTRY instead of surfacing as a missing member somewhere downstream.
    """
    offenders: list[str] = []
    for key, src_dir in sorted(_EXCLUDE_KEYS.items()):
        for pat in exclude_package_data[key]:
            full = src_dir + "/" + pat
            hit = [p for p in tracked_vault_files if fnmatch.fnmatch(p, full)]
            if hit:
                offenders.append(
                    key + ": '" + pat + "' excludes " + str(len(hit))
                    + " tracked file(s), e.g. " + hit[0]
                )
    assert not offenders, (
        "an exclude-package-data entry is too broad -- it would strip the "
        "shipped seed:\n  " + "\n  ".join(offenders)
    )
