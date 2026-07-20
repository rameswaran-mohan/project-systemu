"""The package version has ONE source, and it is load-bearing.

`systemu/__init__.py:__version__` and `pyproject.toml:[project].version` were two
independent literals.  They drifted by 22 releases — the dunder sat at "0.9.59"
while pyproject shipped "0.10.21" — and that dunder is not cosmetic.  Four boot
paths gate on it via a per-version marker file:

  * `vault_migrator.run`                          → `.seed_version`
  * `vault_migrator.backfill_effect_tags`         → `.effect_tags_seed`
  * `first_gate_review.maybe_post_first_gate_review`
  * `tool_reconciler.recover_stale_dry_run_failures`

Each compares the marker to `systemu.__version__` and returns early on equality.
A version that never CHANGES is therefore a version gate that never OPENS: a
vault stamped "0.9.59" took the fast path on every subsequent release and never
received a seed-tool add or update again.  That was reproduced against a real
vault directory, and `TestMigratorActuallyDelivers` below is that reproduction
kept as a permanent pin.

The fix removes the second literal rather than syncing it: pyproject declares
`dynamic = ["version"]` and reads the attr, so build metadata is DERIVED and
cannot diverge.  `TestSingleSource` pins the wiring; if someone re-adds a
literal `version =` to `[project]`, it fails.
"""
from pathlib import Path
import json
import shutil

import pytest

import systemu
import sharing_on
from systemu.runtime import vault_migrator as vm

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    tomllib = pytest.importorskip("tomli")

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


class TestSingleSource:
    """pyproject must DERIVE the version, not restate it."""

    def test_pyproject_declares_no_literal_version(self):
        project = _pyproject()["project"]
        assert "version" not in project, (
            "pyproject re-declared a literal version. That is the second source "
            "of truth that drifted 22 releases from systemu.__version__ and "
            "froze the vault migrator's .seed_version gate. Bump "
            "systemu/__init__.py instead."
        )

    def test_pyproject_marks_version_dynamic(self):
        assert "version" in (_pyproject()["project"].get("dynamic") or [])

    def test_dynamic_version_points_at_the_dunder(self):
        dyn = _pyproject()["tool"]["setuptools"]["dynamic"]
        assert dyn["version"] == {"attr": "systemu.__version__"}, (
            "the dynamic version must resolve from systemu.__version__ — that "
            "is what makes the build metadata and the runtime gate the same value"
        )

    def test_init_version_is_a_plain_literal(self):
        """setuptools resolves `attr:` by static AST read ONLY while the value is
        a literal.  A computed version (e.g. an importlib.metadata lookup) forces
        setuptools to IMPORT systemu at build time, which drags the whole runtime
        dependency set into the build env.  Measured in this repo,
        `importlib.metadata.version("systemu")` also answers wrongly and
        cwd-dependently (PackageNotFoundError from a worktree; a stale 0.8.4 from
        a leftover egg-info), so it must not become the source."""
        src = (REPO / "systemu" / "__init__.py").read_text(encoding="utf-8")
        code = [ln for ln in src.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        assigns = [ln for ln in code if ln.startswith("__version__")]
        assert len(assigns) == 1, f"expected exactly one assignment, got {assigns}"
        assert assigns[0].strip() == f'__version__ = "{systemu.__version__}"', (
            "__version__ must stay a bare string literal for the static AST read"
        )
        # Comment-stripped: the file's own prose EXPLAINS why importlib.metadata
        # was rejected, and that explanation must not trip the pin.
        assert not any("importlib" in ln for ln in code), (
            "installed-dist metadata is a build artifact and goes stale on the "
            "next unreleased edit — it cannot be the source of truth here"
        )

    def test_sharing_on_reexports_rather_than_redeclares(self):
        """The sibling top-level package of the same distribution. It carried its
        own drifting literal, which made `sharing_on --version` misreport."""
        assert sharing_on.__version__ == systemu.__version__
        src = (REPO / "sharing_on" / "__init__.py").read_text(encoding="utf-8")
        code = [ln for ln in src.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        assert not any(ln.startswith("__version__ =") and '"' in ln for ln in code), (
            "sharing_on must import the version, not restate it as a literal"
        )

    def test_version_is_not_the_frozen_value(self):
        """A direct pin on the specific regression: 0.9.59 is the string that was
        stuck across releases 0.9.60 … 0.10.21."""
        assert systemu.__version__ != "0.9.59"


class TestMigratorActuallyDelivers:
    """Drive the REAL migrator over a REAL vault tree — no fixture doubles.

    The defect was invisible to any test that stamped the marker with
    `systemu.__version__` itself, because such a test asserts the fast path
    rather than the delivery.  These build a vault that is genuinely BEHIND the
    package and assert the seed changes ARRIVE.
    """

    @staticmethod
    def _make_vault(tmp_path: Path) -> Path:
        vault_dir = tmp_path / "vault"
        shutil.copytree(vm._package_vault_root(), vault_dir)
        return vault_dir

    def test_stale_marker_delivers_added_and_updated_seeds(self, tmp_path):
        vault_dir = self._make_vault(tmp_path)
        idx_path = vault_dir / "tools" / "index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        assert len(idx) > 2, "seed pack unexpectedly empty — fixture is unreal"

        # A seed tool this vault never received (shipped after its last migration).
        gone = idx[0]
        gone_name, gone_id = gone["name"], gone["id"]
        idx = [e for e in idx if e.get("id") != gone_id]
        (vault_dir / "tools" / f"tool_{gone_id}.json").unlink()
        (vault_dir / "tools" / "implementations" / f"{gone_name}.py").unlink()

        # A seed tool whose shipped implementation CHANGED since that migration.
        stale = idx[0]
        stale_impl = (vault_dir / "tools" / "implementations" / f"{stale['name']}.py")
        STALE_BODY = "# body from an older release\n"
        stale_impl.write_text(STALE_BODY, encoding="utf-8")
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")

        # Marker genuinely behind the installed version.
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")

        summary = vm.run(vault_dir)

        assert summary.get("fast_path") is False, summary
        assert summary["seed_version_to"] == systemu.__version__
        assert summary["added"] == 1, summary
        assert summary["updated"] == 1, summary
        assert summary["errors"] == [], summary

        back = json.loads(idx_path.read_text(encoding="utf-8"))
        assert any(e.get("id") == gone_id for e in back), "missing seed not re-added"
        assert (vault_dir / "tools" / "implementations" / f"{gone_name}.py").exists()
        assert stale_impl.read_text(encoding="utf-8") != STALE_BODY, "stale impl kept"

        # And the run must STAMP, so the next boot is a cheap no-op.
        assert vm._read_seed_version(vault_dir) == systemu.__version__

    def test_matching_marker_takes_the_fast_path(self, tmp_path):
        """The other half — the gate must still CLOSE once converged, or every
        boot would rewrite all 41 seeds."""
        vault_dir = self._make_vault(tmp_path)
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text(
            systemu.__version__, encoding="utf-8")
        assert vm.run(vault_dir).get("fast_path") is True

    def test_operator_edited_seed_is_replaced_but_recoverable(self, tmp_path):
        """THE DATA-LOSS PIN.

        `run`'s update branch keys identity on NAME, so a seed tool the operator
        EDITED has the seed's name and different bytes — it does not look forged,
        it is not `skipped_identical`, and it falls straight through to `copy2`.
        Before the safeguard it was overwritten with nothing retained. Unfreezing
        the version gate is what makes this fire on a real vault at next boot, so
        the two ship together.

        Asserts BOTH halves: the package version wins in the live tree (the seed
        update must still happen), AND the operator's bytes come back
        byte-identically from the backup.
        """
        vault_dir = self._make_vault(tmp_path)
        idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        victim = idx[0]
        name = victim["name"]
        impl = vault_dir / "tools" / "implementations" / f"{name}.py"

        EDIT = ("# operator's local edit — hand-tuned, exists nowhere else\n"
                "MY_LOCAL_CONSTANT = 42\n")
        pkg_bytes = (vm._package_vault_root() / "tools" / "implementations"
                     / f"{name}.py").read_bytes()
        impl.write_text(EDIT, encoding="utf-8")
        assert impl.read_bytes() != pkg_bytes
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")

        summary = vm.run(vault_dir)

        # half 1 — the seed update still lands
        assert summary["updated"] >= 1, summary
        assert impl.read_bytes() == pkg_bytes, "package version must win in the live tree"

        # half 2 — and the operator's work is recoverable, byte for byte
        assert summary["preserved"] >= 1, summary
        backup = (vault_dir / vm._PRE_MIGRATION_DIRNAME / "0.9.59"
                  / "implementations" / f"{name}.py")
        assert backup.is_file(), (
            f"no preserved copy at {backup} — the operator's edit was destroyed")
        assert backup.read_text(encoding="utf-8") == EDIT
        assert summary["preserved_dir"], "the backup location must reach the caller"

    def test_preserved_counts_only_files_that_actually_diverged(self, tmp_path):
        """`preserved` must mean "you had local edits", not "files this run
        replaced".

        Reaching the update branch only proves the IMPLEMENTATION diverged. If the
        body JSON were copied aside unconditionally, an operator who edited
        nothing would still get a non-zero count plus a warning naming their
        vault — across 41 seeds, a false alarm big enough to train people to
        ignore the real one.
        """
        vault_dir = self._make_vault(tmp_path)
        idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        entry = idx[0]
        name, tid = entry["name"], entry["id"]
        # Edit ONLY the implementation; leave the body byte-identical to the package.
        (vault_dir / "tools" / "implementations" / f"{name}.py").write_text(
            "# only the impl was edited\n", encoding="utf-8")
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")

        summary = vm.run(vault_dir)

        assert summary["preserved"] == 1, (
            f"expected only the edited impl to count, got {summary['preserved']}")
        root = vault_dir / vm._PRE_MIGRATION_DIRNAME / "0.9.59"
        assert (root / "implementations" / f"{name}.py").is_file()
        assert not (root / "tools" / f"tool_{tid}.json").exists(), (
            "an untouched body must not be counted as a local edit")

    def test_unreadable_file_counts_as_diverging(self, tmp_path, monkeypatch):
        """`_diverges` must fail SAFE, and that branch has to be pinned.

        Added because mutation S12 SURVIVED: flipping the exception handler from
        `return True` to `return False` broke nothing. That flip means a file
        which EXISTS but cannot be hashed — a locked handle on Windows, a
        permission error, a bad sector — is reported as "no divergence" and gets
        overwritten without a backup. The files we cannot inspect are exactly the
        ones where guessing is most expensive, so missing evidence must read as
        divergence, never as safety.
        """
        a, b = tmp_path / "a.py", tmp_path / "b.py"
        a.write_text("same\n", encoding="utf-8")
        b.write_text("same\n", encoding="utf-8")
        assert vm._diverges(a, b) is False        # identical while readable

        def boom(_p):
            raise OSError("file is locked by another process")

        monkeypatch.setattr(vm, "_file_sha256", boom)
        assert vm._diverges(a, b) is True, (
            "an unhashable existing file must be treated as diverging so it is "
            "preserved (or the overwrite is skipped), never silently clobbered")

    def test_preserved_copy_cannot_be_executed_as_an_implementation(self, tmp_path):
        """The backup holds operator code, so it must live where the runtime's
        implementation resolver REFUSES it. If backups were written inside
        `tools/implementations/`, a tool body could name one as its
        `implementation_path` and it would pass containment and run."""
        vault_dir = self._make_vault(tmp_path)
        idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        name = idx[0]["name"]
        impl = vault_dir / "tools" / "implementations" / f"{name}.py"
        impl.write_text("# edited\n", encoding="utf-8")
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")
        vm.run(vault_dir)

        backup = (vault_dir / vm._PRE_MIGRATION_DIRNAME / "0.9.59"
                  / "implementations" / f"{name}.py")
        assert backup.is_file()
        declared = str(backup.relative_to(vault_dir.parent)).replace("\\", "/")
        assert vm._resolve_vault_impl(
            vault_dir, vault_dir / "tools" / "implementations", name, declared) is None, (
            "a tool body must NOT be able to resolve a preserved backup as its "
            "implementation")

    def test_untouched_seeds_are_not_backed_up(self, tmp_path):
        """The safeguard must be driven by ACTUAL divergence, not run blindly —
        otherwise every upgrade copies all 41 seeds aside for nothing and the
        `preserved` count stops meaning 'you had local edits'."""
        vault_dir = self._make_vault(tmp_path)
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")

        summary = vm.run(vault_dir)

        assert summary["skipped_identical"] > 0, summary
        assert summary["preserved"] == 0, summary
        assert not (vault_dir / vm._PRE_MIGRATION_DIRNAME).exists()

    def test_add_branch_preserves_an_orphaned_implementation(self, tmp_path):
        """The ADD branch clobbers too. A vault can hold an implementation FILE
        with no index entry backing it (a hand-dropped tool, or an index that lost
        the entry), and `vault_entry is None` routes straight to `copy2`."""
        vault_dir = self._make_vault(tmp_path)
        idx_path = vault_dir / "tools" / "index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        victim = idx[0]
        name = victim["name"]

        # Drop the INDEX entry but leave the file on disk, holding local content.
        idx = [e for e in idx if e.get("id") != victim["id"]]
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        impl = vault_dir / "tools" / "implementations" / f"{name}.py"
        ORPHAN = "# orphaned local implementation\n"
        impl.write_text(ORPHAN, encoding="utf-8")
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")

        summary = vm.run(vault_dir)

        assert summary["added"] >= 1, summary
        assert summary["preserved"] >= 1, summary
        backup = (vault_dir / vm._PRE_MIGRATION_DIRNAME / "0.9.59"
                  / "implementations" / f"{name}.py")
        assert backup.is_file() and backup.read_text(encoding="utf-8") == ORPHAN

    def test_rerun_keeps_the_original_backup(self, tmp_path):
        """Idempotence in the direction that matters. A second migration at the
        same from-version must NOT re-copy the (now migrated) file over the
        backup — that would silently replace the operator's preserved bytes with
        the package's and leave a backup that protects nothing."""
        vault_dir = self._make_vault(tmp_path)
        idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        name = idx[0]["name"]
        impl = vault_dir / "tools" / "implementations" / f"{name}.py"
        EDIT = "# operator edit\n"
        impl.write_text(EDIT, encoding="utf-8")
        seed = vault_dir / vm._SEED_VERSION_FILENAME
        seed.write_text("0.9.59", encoding="utf-8")

        vm.run(vault_dir)
        backup = (vault_dir / vm._PRE_MIGRATION_DIRNAME / "0.9.59"
                  / "implementations" / f"{name}.py")
        assert backup.read_text(encoding="utf-8") == EDIT

        # Force a second non-fast-path run at the same from-version.
        seed.write_text("0.9.59", encoding="utf-8")
        impl.write_text("# changed again\n", encoding="utf-8")
        vm.run(vault_dir)

        assert backup.read_text(encoding="utf-8") == EDIT, (
            "the re-run replaced the preserved original")

    def test_unpreservable_edit_is_not_overwritten(self, tmp_path):
        """FAIL CLOSED. If the edit cannot be copied aside, the overwrite must not
        happen — an unpreservable file is precisely the one whose loss would be
        unrecoverable. A stale seed tool is the status quo of the last 22
        releases; destroying operator work is not.

        The failure is induced structurally (a FILE where the backup DIRECTORY
        has to go) rather than by patching the helper, so the real `mkdir`/`copy2`
        path is what fails.
        """
        vault_dir = self._make_vault(tmp_path)
        idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        name = idx[0]["name"]
        impl = vault_dir / "tools" / "implementations" / f"{name}.py"
        EDIT = "# irreplaceable operator edit\n"
        impl.write_text(EDIT, encoding="utf-8")
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")

        # A regular file where `.pre-migration/` must be created.
        (vault_dir / vm._PRE_MIGRATION_DIRNAME).write_text("not a dir", encoding="utf-8")

        summary = vm.run(vault_dir)

        assert impl.read_text(encoding="utf-8") == EDIT, (
            "the edit was destroyed even though it could not be preserved")
        assert summary["preserved"] == 0, summary
        assert any(name in e for e in summary["errors"]), (
            f"the refusal must be REPORTED, not silent: {summary['errors']}")

    def test_version_label_cannot_escape_the_backup_root(self):
        """`.seed_version` is file CONTENT and becomes a PATH SEGMENT here."""
        for hostile in ("..", "../..", "a/../../b", "..\\..\\x", "C:\\evil", "", "."):
            label = vm._safe_version_label(hostile)
            assert "/" not in label and "\\" not in label, (hostile, label)
            assert label not in ("", ".", ".."), (hostile, label)
            assert (Path("/root") / label).resolve().is_relative_to(
                Path("/root").resolve()), (hostile, label)

    def test_hostile_marker_cannot_write_outside_the_vault(self, tmp_path):
        """The sanitiser must be WIRED UP, not merely present.

        Added because mutation S5 SURVIVED: swapping `_safe_version_label(...)`
        for `str(...)` at the call site in `run` broke nothing, since the only
        coverage was a unit test of the helper in isolation. A correct function
        that nothing calls is the same as no function. This drives the REAL
        migrator with a traversal payload in `.seed_version` and asserts nothing
        lands outside the vault directory.
        """
        vault_dir = self._make_vault(tmp_path)
        outside = tmp_path / "OUTSIDE"
        outside.mkdir()
        before = {p for p in tmp_path.rglob("*") if not p.is_relative_to(vault_dir)}

        idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        name = idx[0]["name"]
        # Diverging bytes so the preservation path actually runs.
        (vault_dir / "tools" / "implementations" / f"{name}.py").write_text(
            "# edited\n", encoding="utf-8")
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text(
            "../../OUTSIDE/escaped", encoding="utf-8")

        summary = vm.run(vault_dir)
        assert summary["preserved"] >= 1, summary

        after = {p for p in tmp_path.rglob("*") if not p.is_relative_to(vault_dir)}
        assert after == before, f"migrator wrote outside the vault: {sorted(after - before)}"
        assert not any(outside.iterdir()), "traversal payload escaped into OUTSIDE/"
        assert Path(summary["preserved_dir"]).is_relative_to(vault_dir)

    def test_effect_tag_backfill_reruns_on_a_stale_marker(self, tmp_path):
        """`.effect_tags_seed` is an INDEPENDENT marker on the same dunder, so it
        froze too."""
        vault_dir = self._make_vault(tmp_path)
        marker = vault_dir / vm._EFFECT_TAGS_SEED_FILENAME
        marker.write_text("0.9.59", encoding="utf-8")

        out = vm.backfill_effect_tags(vault_dir)

        assert out.get("fast_path") is False, out
        assert out["stamped"] > 0, out
        assert marker.read_text(encoding="utf-8").strip() == systemu.__version__
        # Converged ⇒ the next boot is a no-op.
        assert vm.backfill_effect_tags(vault_dir).get("fast_path") is True
