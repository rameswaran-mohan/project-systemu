"""THE WHEEL SHIPS THE SEED, NOT WHATEVER A RUN LEFT ON THE BUILDER'S DISK.

THE LIVE DEFECT
    `pyproject.toml` sets `include-package-data = true` and gives the `systemu`
    / `systemu.vault` packages recursive data globs (`vault/**/*.json`,
    `vault/**/*.md`, ...). Those globs read the FILESYSTEM, not the index, so
    they sweep in anything sitting under `systemu/vault/` at build time --
    including the runtime state a local run or a test drops there. `.gitignore`
    does not help: an ignored file is still a file on disk, and setuptools has
    never heard of git.

    PyPI 0.10.25 shipped five such files. That is not cosmetic:
      * a wheel that carries someone else's activities / evolutions /
        notifications / elder state / scrolls hands every installer a vault
        that is already "used" -- the seed migrator's version fast path, the
        scroll index and the notification queue all start from a stranger's
        state instead of empty;
      * the contents are whatever the builder's machine happened to be doing,
        which is an information-disclosure surface with no review step.

    This is the SAME CLASS as the incident
    `tests/test_packaged_vault_carries_no_runtime_state.py` fences, reached by
    a different road. THAT test fences the COMMIT (`git ls-files` names no
    runtime state). Nothing fenced the BUILD -- an UNTRACKED dropping never
    appears in `git ls-files` and shipped anyway. Both roads are now pinned,
    separately.

PROPERTY
    Build a wheel from a tree that HAS a dropping at each ignored runtime path
    and the wheel contains none of them, while still containing every
    git-tracked file under `systemu/vault/`. Both halves matter: an exclusion
    broad enough to drop the droppings and the seed with them would leave a
    bare `pip install systemu` with no tool pack, which is the failure v0.8.10
    added the globs to fix.

WITNESS / REACHABILITY PIN
    Delete `[tool.setuptools.exclude-package-data]` from `pyproject.toml` and
    `test_no_planted_runtime_dropping_is_a_wheel_member` goes red, naming the
    members that came back.

COST
    One wheel build, from a tmp_path copy of the git-TRACKED sources only, so
    the developer's own dirty `systemu/vault/` cannot make this test pass or
    fail by accident. Marked `slow` (a marker this repo already declares).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import zipfile

import pytest

pytestmark = pytest.mark.slow

REPO = pathlib.Path(__file__).resolve().parents[1]

# What the build actually needs. `pyproject.toml` names `readme = "README.md"`
# and resolves the version by STATIC AST read of `systemu/__init__.py`, so both
# packages plus those two files are the whole input.
_SOURCE_ROOTS = ("pyproject.toml", "README.md", "sharing_on", "systemu")

# One dropping per ignored runtime path named in `.gitignore`, plus the
# `systemu/data/` side-store. These are the shapes a real run leaves behind.
_PLANTED = (
    "systemu/vault/activities/act_20260101_000000_planted.json",
    "systemu/vault/evolutions/evo_planted.json",
    "systemu/vault/notifications/notif_planted.json",
    "systemu/vault/elder/elder_planted.json",
    "systemu/vault/scrolls/scroll_planted.json",
    "systemu/data/x.json",
)


def _git(*args):
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="backslashreplace", check=True,
    ).stdout


def _tracked(*paths):
    out = _git("ls-files", "--", *paths)
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def tracked_vault_files():
    """Every path git tracks under `systemu/vault/`, minus the `.gitkeep`s.

    `git ls-files` is the oracle for the same reason the sibling fence uses it:
    the question is "what is supposed to ship", which is the index, not the
    builder's dirty worktree.
    """
    paths = [p for p in _tracked("systemu/vault") if not p.endswith(".gitkeep")]
    # Positive control: an empty listing (wrong cwd, no git, detached index)
    # would make the "seed survives" assertion pass vacuously.
    assert any(p.startswith("systemu/vault/tools/") for p in paths), (
        "git ls-files returned no seed tools under systemu/vault/ -- the "
        f"oracle is broken, so nothing below could have failed. got {len(paths)}"
    )
    return paths


def _build_wheel(src: pathlib.Path) -> pathlib.Path:
    """Build one wheel in *src*. Prefers `build`; falls back to `pip wheel`."""
    attempts = []

    have_build = subprocess.run(
        [sys.executable, "-c", "import build"],
        capture_output=True, text=True, errors="backslashreplace",
    ).returncode == 0

    if have_build:
        cmds = [[sys.executable, "-m", "build", "--wheel", "--no-isolation",
                 "--outdir", str(src / "dist")]]
    else:
        cmds = [[sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "--no-build-isolation", "--wheel-dir", str(src / "dist"), "."]]

    for cmd in cmds:
        r = subprocess.run(
            cmd, cwd=str(src), capture_output=True, text=True,
            encoding="utf-8", errors="backslashreplace",
        )
        attempts.append(f"$ {' '.join(cmd)}\nrc={r.returncode}\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}")
        if r.returncode == 0:
            break

    wheels = sorted((src / "dist").glob("*.whl"))
    assert wheels, "no wheel was produced:\n\n" + "\n\n".join(attempts)
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def wheel_members(tmp_path_factory):
    """Namelist of ONE wheel built from tracked sources + planted droppings."""
    src = tmp_path_factory.mktemp("wheelsrc")

    for rel in _SOURCE_ROOTS:
        origin = REPO / rel
        if origin.is_dir():
            for tracked in _tracked(rel):
                dst = src / tracked
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / tracked, dst)
        else:
            shutil.copy2(origin, src / rel)

    for rel in _PLANTED:
        dst = src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            json.dumps({"planted_by": "test_wheel_excludes_runtime_state"}),
            encoding="utf-8",
        )

    wheel = _build_wheel(src)
    with zipfile.ZipFile(wheel) as zf:
        return [n.replace("\\", "/") for n in zf.namelist()]


def test_the_planted_droppings_really_existed(wheel_members):
    """Positive control for the fixture itself.

    If the copy or the plant silently no-op'd, the exclusion assertion below
    would pass on an empty tree and witness nothing. The tracked-seed assertion
    proves the sources were copied; this one is its partner.
    """
    assert wheel_members, "the built wheel has no members at all"
    assert any(n.startswith("systemu/") for n in wheel_members)


def test_no_planted_runtime_dropping_is_a_wheel_member(wheel_members):
    """THE FENCE. Remove [tool.setuptools.exclude-package-data] -> red."""
    members = set(wheel_members)
    leaked = [p for p in _PLANTED if p in members]
    assert not leaked, (
        "runtime state left on the builder's disk was swept into the wheel: "
        + ", ".join(leaked)
        + ". These paths are in .gitignore, but setuptools reads the "
        "filesystem, not the index -- they need an exclude-package-data entry."
    )


def test_no_member_lives_under_any_runtime_state_directory(wheel_members):
    """Broader than the planted names: the whole directory must be absent.

    A fence written against six exact filenames would go green on a seventh
    dropping with a different name in the same directory.
    """
    runtime_dirs = (
        "systemu/vault/activities/",
        "systemu/vault/evolutions/",
        "systemu/vault/notifications/",
        "systemu/vault/elder/",
        "systemu/data/",
    )
    leaked = sorted(
        n for n in wheel_members if any(n.startswith(d) for d in runtime_dirs)
    )
    assert not leaked, "runtime-state directories reached the wheel: " + ", ".join(leaked)


def test_no_scroll_json_is_a_wheel_member(wheel_members):
    """`scrolls/` itself SHIPS (it carries a tracked .gitkeep); its *.json do not."""
    leaked = sorted(
        n for n in wheel_members
        if n.startswith("systemu/vault/scrolls/") and n.endswith(".json")
    )
    assert not leaked, "scroll runtime state reached the wheel: " + ", ".join(leaked)


def test_every_tracked_vault_file_still_ships(wheel_members, tracked_vault_files):
    """The other half of the fence.

    An exclusion broad enough to drop the droppings AND the seed would leave a
    bare `pip install systemu` with no tool pack, no skills and no Wild Card
    shadow -- the exact failure v0.8.10 added the data globs to fix. Every
    tracked vault file must still be a wheel member.
    """
    members = set(wheel_members)
    missing = sorted(p for p in tracked_vault_files if p not in members)
    assert not missing, (
        f"{len(missing)} tracked seed file(s) stopped shipping -- the exclusion "
        "is too broad: " + ", ".join(missing[:20])
    )
