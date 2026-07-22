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
        froze too.

        The marker payload is `<version>+<derivation generation>`, not the bare
        version: a fix to the DERIVATION RULES has to re-derive on vaults already
        sitting on the installed version, and the live-tryout rule means those
        vaults may never see a bump. The version is still IN the payload — that is
        what this asserts — so a release bump re-derives exactly as before.
        """
        vault_dir = self._make_vault(tmp_path)
        marker = vault_dir / vm._EFFECT_TAGS_SEED_FILENAME
        marker.write_text("0.9.59", encoding="utf-8")

        out = vm.backfill_effect_tags(vault_dir)

        assert out.get("fast_path") is False, out
        assert out["stamped"] > 0, out
        written = marker.read_text(encoding="utf-8").strip()
        assert written == vm._effect_tags_marker_value(systemu.__version__)
        assert systemu.__version__ in written, (
            "the marker stopped carrying the version — a release bump would no "
            "longer re-derive")
        assert written != systemu.__version__, (
            "the marker is the bare version again, so a derivation-rules fix can "
            "never reach a vault already on this version")
        # Converged ⇒ the next boot is a no-op.
        assert vm.backfill_effect_tags(vault_dir).get("fast_path") is True


class TestSeedUpdateKeepsTheOperatorsDisable:
    """`tools/index.json` — MERGED, not backed up, and the merge is one field.

    Measured on a real vault before writing any of this (see
    `test_dropped_header_fields_self_heal_on_next_boot` and
    `test_the_index_is_already_rewritten_earlier_in_the_same_run`):

      * A live vault's index headers carry FIVE fields the packaged seed index
        does not — `dry_run_status`, `version`, `parameters_schema_summary`,
        `return_schema_summary`, `implementation_path` — because they are
        written by `vault._tool_header`, not shipped in the seed file. `run`'s
        update branch replaces the whole entry with `pkg_entry`, so all five go.
      * Every one of those five is DERIVED from the tool body and is re-derived
        by `jobs._backfill_tool_headers_v061` on the next boot. They self-heal.
      * `enabled` does NOT. It is Gate 3's input
        (`tool_registry.execute` → `if not tool.enabled: raise
        ToolNotEnabledError`), the migration copies the package body over the
        vault's, and every packaged seed ships `enabled: true` — so an upgrade
        silently re-arms a tool the operator switched OFF in the dashboard.

    Hence a merge rather than a copy-aside. See
    `test_the_index_is_already_rewritten_earlier_in_the_same_run` for why a
    `_preserve_before_overwrite`-style whole-file backup is the wrong shape here.
    """

    _make_vault = staticmethod(TestMigratorActuallyDelivers._make_vault)

    @staticmethod
    def _booted(vault_dir: Path):
        """A vault as it exists AFTER a real boot: headers written by
        `vault._tool_header` (15 fields), not the 10-field packaged seed index.

        The concrete production type — a test that hand-writes a header proves
        nothing about the fields the real writer emits.
        """
        from systemu.vault.vault import Vault
        v = Vault(vault_dir)
        for hdr in list(v.load_index("tools")):
            v.save_tool(v.get_tool(hdr["id"]))
        return v

    @staticmethod
    def _stale(vault_dir: Path) -> None:
        (vault_dir / vm._SEED_VERSION_FILENAME).write_text("0.9.59", encoding="utf-8")

    @staticmethod
    def _bump_impl(vault_dir: Path, name: str) -> None:
        """Make the package's implementation win the update branch — the ordinary
        case where upstream changed a seed, not only the operator-edited one."""
        impl = vault_dir / "tools" / "implementations" / f"{name}.py"
        impl.write_text(impl.read_text(encoding="utf-8") + "\n# older release\n",
                        encoding="utf-8")

    def test_operator_disable_survives_a_seed_update(self, tmp_path):
        """THE PIN. Reproduced on a real vault before the fix: `enabled` went
        False → True and the tool became callable again."""
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        name = v.load_index("tools")[0]["name"]

        tool = v.find_tool_by_name(name)
        tool.enabled = False
        v.save_tool(tool)
        assert v.find_tool_by_name(name).enabled is False

        self._bump_impl(vault_dir, name)
        self._stale(vault_dir)

        summary = vm.run(vault_dir)
        assert summary["updated"] >= 1, summary

        from systemu.vault.vault import Vault
        after = Vault(vault_dir)
        # Gate 3's OWN input expression, not a proxy for it.
        assert after.find_tool_by_name(name).enabled is False, (
            "the migration re-armed a tool the operator switched off")
        hdr = {e["name"]: e for e in after.load_index("tools")}[name]
        assert hdr["enabled"] is False, (
            "index header disagrees with the body — the merge must cover both")

    def test_the_seed_update_still_lands_while_the_disable_is_kept(self, tmp_path):
        """The merge must not turn into a skip: package-authoritative fields
        still win, only `enabled` is carried across."""
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        name = v.load_index("tools")[0]["name"]
        tool = v.find_tool_by_name(name)
        tool.enabled = False
        tool.description = "operator's stale description"
        v.save_tool(tool)

        impl = vault_dir / "tools" / "implementations" / f"{name}.py"
        pkg_impl_bytes = (vm._package_vault_root() / "tools" / "implementations"
                          / f"{name}.py").read_bytes()
        impl.write_text("# an older release's body\n", encoding="utf-8")
        self._stale(vault_dir)

        vm.run(vault_dir)

        pkg_entry = {e["name"]: e for e in json.loads(
            (vm._package_vault_root() / "tools" / "index.json").read_text(
                encoding="utf-8"))}[name]
        hdr = {e["name"]: e for e in json.loads(
            (vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))}[name]
        assert impl.read_bytes() == pkg_impl_bytes, "the seed update must still land"
        assert hdr["description"] == pkg_entry["description"], (
            "description is package-authoritative and must still be replaced")
        assert hdr["enabled"] is False

    def test_nothing_the_operator_left_enabled_is_switched_off(self, tmp_path):
        """No over-firing. A vault nobody disabled anything in must come out with
        every seed still enabled and `kept_disabled` at zero — otherwise the
        count means nothing and an upgrade would break working tools."""
        vault_dir = self._make_vault(tmp_path)
        self._booted(vault_dir)
        idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        for entry in idx[:3]:
            self._bump_impl(vault_dir, entry["name"])
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["updated"] >= 3, summary
        assert summary["kept_disabled"] == 0, summary
        after = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
        assert all(e["enabled"] is True for e in after), (
            [e["name"] for e in after if e["enabled"] is not True])

    def test_a_brand_new_seed_arrives_enabled(self, tmp_path):
        """The other over-fire, and the one that would hurt everybody.

        A seed the vault has never held has NO body at either id — the probe's
        last source is a file that does not exist. If "no record" fell to the
        restrictive side, every new tool in every release would install switched
        OFF and the operator would have to hunt for it. Absent is not a disable;
        only an existing record can express intent.

        Added because the mutation flipping `_says_off`'s missing-file branch to
        `True` SURVIVED the first cut: every fixture had all its bodies present,
        so the branch never ran.
        """
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        entry = v.load_index("tools")[0]
        name, tid = entry["name"], entry["id"]

        # A seed shipped after this vault's last migration: nothing of it here.
        idx_path = vault_dir / "tools" / "index.json"
        idx = [e for e in json.loads(idx_path.read_text(encoding="utf-8"))
               if e["id"] != tid]
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        (vault_dir / "tools" / f"tool_{tid}.json").unlink()
        (vault_dir / "tools" / "implementations" / f"{name}.py").unlink()
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["added"] >= 1, summary
        assert summary["kept_disabled"] == 0, summary
        from systemu.vault.vault import Vault
        assert Vault(vault_dir).find_tool_by_name(name).enabled is True, (
            "a brand-new seed installed switched OFF")
        hdr = {e["name"]: e for e in json.loads(
            idx_path.read_text(encoding="utf-8"))}[name]
        assert hdr["enabled"] is True

    def test_absent_enabled_key_is_not_read_as_a_disable(self, tmp_path):
        """`enabled` ABSENT is no evidence of operator intent.

        `Vault._backfill_tool_index_enabled` writes `False` when it cannot find a
        body, so reading "absent or falsey" as a disable would latch tools off on
        vaults predating the field. Only an explicit `False` counts.
        """
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        name = v.load_index("tools")[0]["name"]

        idx_path = vault_dir / "tools" / "index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        entry = next(e for e in idx if e["name"] == name)
        entry.pop("enabled", None)
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        body_path = vault_dir / "tools" / f"tool_{entry['id']}.json"
        body = json.loads(body_path.read_text(encoding="utf-8"))
        body.pop("enabled", None)
        body_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

        self._bump_impl(vault_dir, name)
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["kept_disabled"] == 0, summary
        hdr = {e["name"]: e for e in json.loads(
            idx_path.read_text(encoding="utf-8"))}[name]
        assert hdr["enabled"] is True

    def test_kept_disabled_reaches_the_caller(self, tmp_path):
        """A count that only reaches a log line is a count nobody reads — the
        same rule `preserved`/`preserved_dir` follow."""
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        name = v.load_index("tools")[0]["name"]
        tool = v.find_tool_by_name(name)
        tool.enabled = False
        v.save_tool(tool)
        self._bump_impl(vault_dir, name)
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["kept_disabled"] == 1, summary
        assert name in summary["kept_disabled_names"], summary

    def test_an_unreadable_body_is_treated_as_disabled(self, tmp_path):
        """Mutation S12's shape, on this path.

        `_diverges` once returned False from its `except`, so a file that EXISTS
        but cannot be read counted as "no divergence" and was clobbered. The same
        trap here reads an unreadable body as "not disabled" and re-arms the
        tool. Missing evidence must fall to the RESTRICTIVE side: a tool wrongly
        left off is one dashboard click away and Gate 3 says so by name; a tool
        wrongly switched on is a silent control revocation.
        """
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        entry = v.load_index("tools")[0]
        name, tid = entry["name"], entry["id"]

        (vault_dir / "tools" / f"tool_{tid}.json").write_text(
            "{ this is not json", encoding="utf-8")
        self._bump_impl(vault_dir, name)
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["kept_disabled"] == 1, summary
        hdr = {e["name"]: e for e in json.loads(
            (vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))}[name]
        assert hdr["enabled"] is False, (
            "an unreadable body must not read as 'operator left this on'")

    def test_add_branch_keeps_an_orphaned_bodys_disable(self, tmp_path):
        """The ADD branch clobbers `tool_<id>.json` too. A body left behind when
        its index entry was lost is re-indexed by this run — and re-indexing it
        as enabled is the same silent re-arm."""
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        entry = v.load_index("tools")[0]
        name, tid = entry["name"], entry["id"]

        tool = v.find_tool_by_name(name)
        tool.enabled = False
        v.save_tool(tool)

        idx_path = vault_dir / "tools" / "index.json"
        idx = [e for e in json.loads(idx_path.read_text(encoding="utf-8"))
               if e["id"] != tid]
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["added"] >= 1, summary
        assert summary["kept_disabled"] == 1, summary
        from systemu.vault.vault import Vault
        assert Vault(vault_dir).find_tool_by_name(name).enabled is False
        # BOTH records. Asserting only the body let a mutation that reverted the
        # ADD branch's index merge to a plain `append(pkg_entry)` survive: the
        # header then advertised the tool as enabled while the body refused it,
        # so the Tools page showed a toggle that did not match the gate.
        hdr = {e["name"]: e for e in json.loads(
            idx_path.read_text(encoding="utf-8"))}[name]
        assert hdr["enabled"] is False, "ADD branch wrote a header that lies about the gate"

    def test_disable_is_found_when_the_vault_holds_the_seed_under_its_own_id(self, tmp_path):
        """Identity here is by NAME, so the ids need not match.

        `normalize_seed_forged_flags` already documents this: a vault can hold a
        seed under its OWN id, in which case its body is `tool_<vault id>.json`
        while the file the update branch overwrites is `tool_<package id>.json`.
        A probe that reads only the destination path finds no record and re-arms
        the tool. Added because the mutation that passed `None` for the vault
        entry SURVIVED the first cut of these tests — every fixture there had the
        two ids equal, so the two sources were indistinguishable.

        The header carries no `enabled` here on purpose, so the only thing that
        can answer is the vault entry's OWN body.
        """
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        entry = v.load_index("tools")[0]
        name, pkg_tid = entry["name"], entry["id"]
        vault_tid = f"{pkg_tid}_local"

        tool = v.find_tool_by_name(name)
        tool.enabled = False
        v.save_tool(tool)

        # re-home the body under the vault's own id, and strip the header's copy
        body_path = vault_dir / "tools" / f"tool_{pkg_tid}.json"
        body = json.loads(body_path.read_text(encoding="utf-8"))
        body["id"] = vault_tid
        (vault_dir / "tools" / f"tool_{vault_tid}.json").write_text(
            json.dumps(body, indent=2) + "\n", encoding="utf-8")
        body_path.unlink()
        idx_path = vault_dir / "tools" / "index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        for e in idx:
            if e["id"] == pkg_tid:
                e["id"] = vault_tid
                e.pop("enabled", None)
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        assert not body_path.exists(), "fixture unreal — the ids still collide"

        self._bump_impl(vault_dir, name)
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["updated"] >= 1, summary
        assert summary["kept_disabled"] == 1, summary
        from systemu.vault.vault import Vault
        assert Vault(vault_dir).find_tool_by_name(name).enabled is False

    def test_a_header_only_disable_is_honoured(self, tmp_path):
        """Either record is enough.

        `Vault._backfill_tool_index_enabled` writes `enabled` into the INDEX from
        whatever it can find, so the header can carry a disable the body does not.
        Reading only the body would drop it.
        """
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        entry = v.load_index("tools")[0]
        name, tid = entry["name"], entry["id"]

        idx_path = vault_dir / "tools" / "index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        next(e for e in idx if e["id"] == tid)["enabled"] = False
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        body_path = vault_dir / "tools" / f"tool_{tid}.json"
        body = json.loads(body_path.read_text(encoding="utf-8"))
        assert body["enabled"] is True, "fixture unreal — the body already agrees"

        self._bump_impl(vault_dir, name)
        self._stale(vault_dir)

        summary = vm.run(vault_dir)

        assert summary["kept_disabled"] == 1, summary
        from systemu.vault.vault import Vault
        assert Vault(vault_dir).find_tool_by_name(name).enabled is False

    def test_the_merge_does_not_mutate_the_package_catalog(self, tmp_path):
        """`_entry_keeping_disable` must COPY.

        Dropping the copy currently changes nothing observable — `run` re-parses
        the package index from disk each call, computes `pkg_names` before the
        loop, and never reads `pkg_idx` again afterwards — so the mutation that
        aliased it SURVIVED, and this pin exists to stop that staying true by
        accident. Writing a vault's state into the authoritative package catalog
        is the kind of aliasing that only bites once something starts caching it.
        """
        pkg_entry = {"id": "tool_x", "name": "x", "enabled": True}
        merged = vm._entry_keeping_disable(pkg_entry, True)
        assert merged["enabled"] is False
        assert pkg_entry["enabled"] is True, (
            "the package catalog entry was mutated in place")
        assert merged is not pkg_entry

    def test_the_index_is_already_rewritten_earlier_in_the_same_run(self, tmp_path):
        """WHY `tools/index.json` GETS NO `_preserve_before_overwrite` BACKUP.

        Measured, not assumed. `normalize_seed_forged_flags` runs ABOVE the
        write-back inside the same `run()` and rewrites `tools/index.json` itself
        whenever it clears a mis-flag — and the POC finding is that real vaults
        mis-flag every seed tool, so that is the field state, not an edge case.
        A copy-aside taken at the write-back would therefore capture a file the
        migrator had already rewritten on this same boot, and present a
        machine-edited intermediate as "your previous contents" — the exact trap
        that got body-JSON preservation removed one commit ago.

        Two further reasons the shape is wrong, both checked here:
          * there is no divergence predicate. A vault index legitimately differs
            from the package's on every real vault (user-forged tools are only
            in the vault's), so a `_diverges`-gated backup would fire on 100% of
            migrations and `preserved` would stop meaning "you had local edits".
          * nothing in the index is unique to it — every field `_tool_header`
            emits is derived from the tool body.

        If this test ever fails, an earlier index writer was removed and
        whole-file preservation becomes worth re-costing.
        """
        vault_dir = self._make_vault(tmp_path)
        self._booted(vault_dir)
        idx_path = vault_dir / "tools" / "index.json"

        # The POC field state: every seed mis-flagged as LLM-forged.
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        for e in idx:
            e["forged_by_systemu"] = True
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        for e in idx:
            bp = vault_dir / "tools" / f"tool_{e['id']}.json"
            body = json.loads(bp.read_text(encoding="utf-8"))
            body["forged_by_systemu"] = True
            bp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        self._stale(vault_dir)

        # Record the atomic writes `run` performs BEFORE its own write-back;
        # the real function still does the work.
        seen: list = []
        real = vm._write_text_atomic

        def recording(path, text):
            seen.append(Path(path))
            return real(path, text)

        import unittest.mock as _mock
        with _mock.patch.object(vm, "_write_text_atomic", recording):
            summary = vm.run(vault_dir)

        assert summary["forged_normalized"] > 0, summary
        assert any(p == idx_path for p in seen), (
            "no earlier writer touched tools/index.json — the measurement that "
            "rules out whole-file preservation no longer holds; re-cost it")

    def test_dropped_header_fields_self_heal_on_next_boot(self, tmp_path):
        """The other five dropped fields need no carrying — they are derived.

        This is the evidence for merging ONE field instead of all of them.
        Carrying `parameters_schema_summary` / `return_schema_summary` forward
        would be actively wrong (they would describe the OLD schema while the
        body is the package's new one), and `implementation_path` is
        vault-declared — re-stamping it from the old entry would carry a
        redirected path across a migration that just replaced `{name}.py`.
        """
        vault_dir = self._make_vault(tmp_path)
        v = self._booted(vault_dir)
        name = v.load_index("tools")[0]["name"]
        derived = ("dry_run_status", "version", "parameters_schema_summary",
                   "return_schema_summary", "implementation_path")
        hdr = {e["name"]: e for e in v.load_index("tools")}[name]
        assert all(k in hdr for k in derived), sorted(hdr)

        self._bump_impl(vault_dir, name)
        self._stale(vault_dir)
        vm.run(vault_dir)

        from systemu.vault.vault import Vault
        from systemu.scheduler.jobs import _backfill_tool_headers_v061
        v2 = Vault(vault_dir)
        hdr = {e["name"]: e for e in v2.load_index("tools")}[name]
        assert not any(k in hdr for k in derived), (
            "fixture unreal — the migration no longer drops these")

        _backfill_tool_headers_v061(v2)

        hdr = {e["name"]: e for e in Vault(vault_dir).load_index("tools")}[name]
        assert all(k in hdr for k in derived), (
            f"a dropped header field did NOT self-heal: {sorted(hdr)}")
