"""1e -- `systemu debug resolver-replay` must work after `pip install`.

THE DEFECT
    The replay corpus was resolved REPO-RELATIVE
    (``Path(resolver_replay.__file__).parents[2] / "fixtures" / "field"``) and
    ``fixtures/field`` was not package data. On an installed wheel that path is
    ``<site-packages>/fixtures/field``, which does not exist, so the command an
    operator can type found nothing at all.

THE RULING
    The corpus ships INSIDE the package (``systemu/fixtures/field``, package
    data) and is resolved with ``importlib.resources``. The source checkout keeps
    its own ``fixtures/field`` (the existing suite reads it there, and
    ``test_default_corpus_dir_points_at_the_repo_corpus`` pins that a checkout
    replays it), so the packaged copy is a MIRROR -- and a mirror is only honest
    while it is byte-identical, which the first test below pins.

    When no corpus can be found the command says so in plain words, names what to
    do, and exits non-zero. It never renders a rate, and never a silent zero.

WITNESS ON A REAL BUILD
    ``test_an_installed_wheel_replays_its_shipped_corpus_from_an_unrelated_cwd``
    builds a wheel from this tree, installs it into a throwaway venv and runs the
    installed ``systemu debug resolver-replay`` from a directory that is not the
    repo. Revert the resolver to the repo-relative path and that test goes red.

NOTHING HERE WRITES INTO THE REPO OR THE OPERATOR'S HOME: every build, venv,
cwd, HOME and TEMP lives under ``tmp_path``.
"""
from __future__ import annotations

import fnmatch
import importlib.resources
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from systemu.runtime import resolver_replay as rr

REPO = Path(__file__).resolve().parents[1]
SOURCE_CORPUS = REPO / "fixtures" / "field"
PACKAGED_CORPUS = REPO / "systemu" / "fixtures" / "field"


def _pyproject() -> dict:
    try:
        import tomllib
    except ImportError:  # Python 3.10: the [dev] extra ships tomli
        import tomli as tomllib
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _corpus_json(d: Path) -> list:
    return sorted(p.name for p in d.glob("*.json"))


# --------------------------------------------------------------------------- #
# 1. the package carries the corpus, byte for byte
# --------------------------------------------------------------------------- #

def test_the_package_carries_a_byte_identical_copy_of_the_corpus():
    src = _corpus_json(SOURCE_CORPUS)
    # Positive control: an empty listing would make every comparison vacuous.
    assert rr.ROSTER_FILENAME in src and len(src) > 1, src
    assert PACKAGED_CORPUS.is_dir(), (
        f"no packaged corpus at {PACKAGED_CORPUS}: an installed wheel has nothing "
        "to replay")
    # Exactly the corpus data -- no README, no stray dropping rides along.
    assert sorted(p.name for p in PACKAGED_CORPUS.iterdir()) == src, (
        "the packaged corpus is not the same file set as fixtures/field")
    drifted = [n for n in src
               if (PACKAGED_CORPUS / n).read_bytes() != (SOURCE_CORPUS / n).read_bytes()]
    assert not drifted, (
        f"packaged corpus drifted from fixtures/field: {drifted}. The two must be "
        "byte-identical; copy fixtures/field/*.json over systemu/fixtures/field/.")


def test_every_packaged_corpus_file_is_declared_package_data():
    cfg = _pyproject()["tool"]["setuptools"]
    include = cfg["package-data"]["systemu"]
    exclude = cfg.get("exclude-package-data", {}).get("systemu", [])
    names = _corpus_json(PACKAGED_CORPUS)
    assert names, "positive control: the packaged corpus is empty"
    for name in names:
        rel = f"fixtures/field/{name}"
        assert any(fnmatch.fnmatch(rel, g) for g in include), (
            f"{rel} matches no [tool.setuptools.package-data] glob for systemu, "
            "so a wheel would not carry it")
        assert not any(fnmatch.fnmatch(rel, g) for g in exclude), (
            f"{rel} is stripped by [tool.setuptools.exclude-package-data]")


# --------------------------------------------------------------------------- #
# 2. resolution: an install with no source checkout reads the PACKAGED corpus
# --------------------------------------------------------------------------- #

def test_without_a_source_checkout_the_default_corpus_is_the_packaged_one(monkeypatch):
    monkeypatch.delenv("SYSTEMU_FIELD_CORPUS", raising=False)
    monkeypatch.setattr(rr, "_source_checkout_corpus", lambda: None)

    expected = Path(str(importlib.resources.files("systemu") / "fixtures" / "field"))
    assert rr.default_corpus_dir() == expected

    report = rr.resolver_replay_report()
    assert report.definitive, rr.format_resolver_replay(report)
    assert report.declared == len(rr.load_roster(SOURCE_CORPUS))


def test_a_source_checkout_still_replays_its_own_corpus(monkeypatch):
    monkeypatch.delenv("SYSTEMU_FIELD_CORPUS", raising=False)
    assert rr._source_checkout_corpus() == SOURCE_CORPUS
    assert rr.default_corpus_dir() == SOURCE_CORPUS


# --------------------------------------------------------------------------- #
# 3. genuinely absent -> said plainly, with what to do, never a rate
# --------------------------------------------------------------------------- #

def _assert_no_rate_and_ascii(lines):
    text = "\n".join(lines)
    assert "NOT 0%" in text, text
    assert not re.search(r"=\s*\d+%", text), text
    bad = [ln for ln in lines if not ln.isascii()]
    assert not bad, f"verdict lines must be ASCII-only: {bad!r}"


def test_an_explicit_corpus_that_does_not_exist_says_so_and_names_the_remedy(tmp_path):
    gone = tmp_path / "no-such-corpus"
    report = rr.resolver_replay_report(gone)
    assert report.rate is None and report.definitive is False
    assert report.corpus_absent == str(gone)

    lines = rr.format_resolver_replay(report)
    assert lines[0].startswith("Avoidable-ask rate (resolver replay): NO CORPUS"), lines
    text = "\n".join(lines)
    assert str(gone) in text
    assert "--corpus" in text and "SYSTEMU_FIELD_CORPUS" in text
    assert "roster.json" in text
    _assert_no_rate_and_ascii(lines)


def test_an_install_with_no_corpus_anywhere_says_it_did_not_ship(monkeypatch, tmp_path):
    monkeypatch.delenv("SYSTEMU_FIELD_CORPUS", raising=False)
    monkeypatch.setattr(rr, "_source_checkout_corpus", lambda: None)
    monkeypatch.setattr(rr, "packaged_corpus_dir", lambda: tmp_path / "not-shipped")

    report = rr.resolver_replay_report()
    assert report.corpus_absent == str(tmp_path / "not-shipped")
    lines = rr.format_resolver_replay(report)
    text = "\n".join(lines)
    assert lines[0].startswith("Avoidable-ask rate (resolver replay): NO CORPUS"), lines
    assert "did not ship" in text
    assert "pip install --force-reinstall systemu" in text
    _assert_no_rate_and_ascii(lines)

    from click.testing import CliRunner
    from systemu.interface import cli_commands
    res = CliRunner().invoke(cli_commands.debug_group, ["resolver-replay"])
    assert res.exit_code == 1, res.output
    assert "NO CORPUS" in res.output and "did not ship" in res.output


def test_a_found_corpus_carries_no_absence_marker():
    report = rr.resolver_replay_report(SOURCE_CORPUS)
    assert report.corpus_absent == ""
    assert "NO CORPUS" not in "\n".join(rr.format_resolver_replay(report))


# --------------------------------------------------------------------------- #
# 4. WITNESS: a real wheel, a real venv, an unrelated cwd
# --------------------------------------------------------------------------- #

_SOURCE_ROOTS = ("pyproject.toml", "README.md", "sharing_on", "systemu")


def _copy_buildable_tree(dst: Path) -> None:
    """Tracked files plus untracked-but-not-ignored ones under the build roots.

    ``--exclude-standard`` keeps every ignored runtime dropping on the
    developer's disk out of the copy, so a dirty ``systemu/vault`` cannot change
    what this test builds.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard", "--", *_SOURCE_ROOTS],
        cwd=str(REPO), capture_output=True, check=True,
    ).stdout.decode("utf-8", errors="backslashreplace")
    names = [n for n in out.split("\0") if n]
    assert any(n.startswith("systemu/") for n in names), "git listed no sources"
    for rel in names:
        origin = REPO / rel
        if not origin.is_file():
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="backslashreplace", **kw)


def _clean_env(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SYSTEMU_") and k not in ("PYTHONPATH", "PYTHONHOME")}
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir(exist_ok=True)
    temp.mkdir(exist_ok=True)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp),
        "SYSTEMU_VAULT_DIR": str(tmp_path / "vault"),
        "PYTHONIOENCODING": "utf-8",
    })
    return env


@pytest.mark.slow
def test_an_installed_wheel_replays_its_shipped_corpus_from_an_unrelated_cwd(tmp_path):
    missing = [m for m in ("build", "setuptools", "wheel")
               if importlib.util.find_spec(m) is None]
    if missing:
        pytest.xfail(
            f"cannot witness the installed wheel: {missing} not importable in "
            f"{sys.executable} (install the [dev] extra). This is NOT a pass.")

    src = tmp_path / "src"
    _copy_buildable_tree(src)
    dist = tmp_path / "dist"
    built = _run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
                  "--outdir", str(dist)], cwd=str(src))
    wheels = sorted(dist.glob("*.whl"))
    assert built.returncode == 0 and len(wheels) == 1, (
        built.stdout[-3000:] + built.stderr[-3000:])

    venv = tmp_path / "venv"
    made = _run([sys.executable, "-m", "venv", "--without-pip",
                 "--system-site-packages", str(venv)])
    assert made.returncode == 0, made.stderr
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    vpy = bindir / ("python.exe" if os.name == "nt" else "python")
    env = _clean_env(tmp_path)

    inst = _run([str(vpy), "-m", "pip", "install", "--no-deps", "--no-index",
                 "--ignore-installed", "--no-cache-dir", str(wheels[0])], env=env)
    assert inst.returncode == 0, inst.stdout[-3000:] + inst.stderr[-3000:]

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    # Positive control: the interpreter imports the INSTALLED systemu, not this
    # checkout (and not any other install on the system path).
    where = _run([str(vpy), "-c", "import systemu; print(systemu.__file__)"],
                 cwd=str(elsewhere), env=env)
    assert where.returncode == 0, where.stderr
    imported = Path(where.stdout.strip()).resolve()
    assert venv.resolve() in imported.parents, (
        f"the venv imported systemu from {imported}, not from the installed wheel")
    assert not (imported.parents[1] / "fixtures" / "field").exists(), (
        "the install has a repo-relative fixtures/field next to it; the witness "
        "would not prove the packaged corpus is what was read")

    exe = bindir / ("systemu.exe" if os.name == "nt" else "systemu")
    assert exe.is_file(), f"the wheel installed no `systemu` console script in {bindir}"
    res = _run([str(exe), "debug", "resolver-replay"], cwd=str(elsewhere), env=env)
    out = res.stdout + res.stderr
    print(out)
    assert res.returncode == 0, out

    expected = rr.format_resolver_replay(rr.resolver_replay_report(SOURCE_CORPUS))
    assert res.stdout.splitlines()[0] == expected[0], out
    declared = len(rr.load_roster(SOURCE_CORPUS))
    assert f"declared={declared} scenarios={declared}" in res.stdout, out
    assert "DEFINITIVE" in res.stdout and "NO CORPUS" not in res.stdout, out
