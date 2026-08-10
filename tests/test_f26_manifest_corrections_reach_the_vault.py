"""F26 — a corrected SHIPPED-TOOL MANIFEST must reach an EXISTING vault, and every
migration counter must report the number of records it actually changed.

THE DEFECT, reproduced on a cold install. A vault seeded by an older wheel carried
``tool_tool_web_read.json`` with ``dependencies: ["playwright"]``. That declaration was
wrong and was corrected in the shipped catalog (F25). With the fixed wheel installed a
FRESH vault seeded correctly, but the EXISTING vault kept the stale list for ever:
``systemu tools list`` reported 3 UNAVAILABLE and refused a tool that works.

Neither repair path reached it. ``systemu init`` skips every entry that already exists.
``vault_migrator.run``'s seed loop keys its whole update decision on the IMPLEMENTATION
file's bytes — a manifest-only correction leaves ``web_read.py`` byte-identical, so the
tool lands in ``skipped_identical`` and its record is never opened. And on this project's
live-tryout rule (fixes folded into the CURRENT version, no bump) ``installed ==
vault_seed`` takes the fast path and the loop does not run at all.

So the shipped starter catalog was effectively IMMUTABLE after first seed. That is a
CLASS of silent staleness — a dependency list, a description, a parameter schema — not
one stale field, and both arms are pinned below.

TWO ARMS, DELIBERATELY SEPARATE FIXTURES:
  * ``stale_vault``        — ``.seed_version == installed``. The FAST PATH: the seed loop
    does not run, so the new convergence pass is the ONLY writer and every counter pin
    below measures it and nothing else. This is also the arm that describes every real
    deployed vault under the no-bump release rule.
  * ``stale_vault_bumped`` — an older ``.seed_version``, so the pre-existing seed loop
    runs too. Used for the arm-crossing convergence pin and for ``impl_replaced``.

BOTH HALVES ARE REQUIRED. The second half is what stops the fix from becoming a
data-loss bug: a vault is USER DATA, and an operator-forged tool, an operator-DISABLED
tool and an operator-EDITED seed must all come through a convergence run untouched.

The counters are pinned against WHAT ACTUALLY CHANGED ON DISK, field by field. This
project has now shipped the same lying-counter shape twice (``updated=41`` here,
``stamped=41 unclassified_bodies=0`` in F14), so a number is only allowed into the
operator-facing line if a test measures it against the filesystem.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

#  The exact set the package OWNS. Hardcoded here rather than imported, so an
#  accidental widening — someone adding `enabled` or `dry_run_status` to the refresh
#  set — fails in this file instead of in a user's vault.
PACKAGE_OWNED = ("description", "tool_type", "parameters_schema",
                 "return_schema", "implementation_notes", "dependencies")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _body(vault: Path, tid: str) -> dict:
    return json.loads((vault / "tools" / f"tool_{tid}.json").read_text(encoding="utf-8"))


def _write_body(vault: Path, tid: str, body: dict) -> None:
    (vault / "tools" / f"tool_{tid}.json").write_text(
        json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _index(vault: Path) -> list:
    return json.loads((vault / "tools" / "index.json").read_text(encoding="utf-8"))


def _write_index(vault: Path, idx: list) -> None:
    (vault / "tools" / "index.json").write_text(
        json.dumps(idx, indent=2) + "\n", encoding="utf-8")


def _header(vault: Path, name: str) -> dict:
    return next(e for e in _index(vault) if e.get("name") == name)


def _snapshot_bodies(vault: Path) -> dict:
    """{body filename: {package-owned field: canonical json}} — nothing else."""
    out = {}
    for p in (vault / "tools").glob("tool_*.json"):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[p.name] = {f: json.dumps(b.get(f), sort_keys=True) for f in PACKAGE_OWNED}
    return out


def _snapshot_impls(vault: Path) -> dict:
    return {p.name: _sha(p) for p in (vault / "tools" / "implementations").glob("*.py")}


def _measured_delta(before: dict, after: dict):
    """(fields changed, tools changed) between two `_snapshot_bodies` results."""
    fields = tools = 0
    for fname, vals in after.items():
        hits = sum(1 for f, v in vals.items() if before.get(fname, {}).get(f) != v)
        fields += hits
        tools += 1 if hits else 0
    return fields, tools


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures: a REAL packaged catalog copied into tmp, then deliberately made STALE
# ─────────────────────────────────────────────────────────────────────────────

#  Roles assigned to real shipped tools, so the fixture breaks loudly if one is ever
#  dropped from the catalog rather than silently testing nothing.
STALE_DEPS_TOOL = "web_read"        # the F26 witness: dependencies corrected in-package
STALE_SPEC_TOOL = "web_search"      # description + schema drift, same class
DISABLED_TOOL = "fetch_json"        # operator switched it OFF
EDITED_TOOL = "download_file"       # operator EDITED the implementation


@pytest.fixture
def pkg_vault():
    from systemu.runtime.vault_migrator import _package_vault_root
    root = _package_vault_root()
    assert (root / "tools" / "index.json").is_file(), root
    return root


def _build_stale_vault(tmp_path: Path, pkg_vault: Path, seed_version: str) -> Path:
    """An EXISTING vault: the shipped catalog as an older wheel left it, plus real
    operator-owned state that a convergence run must not disturb."""
    vault = tmp_path / "vault"
    vault.mkdir()
    shutil.copytree(pkg_vault / "tools", vault / "tools")

    idx = _index(vault)
    names = {e.get("name") for e in idx}
    for n in (STALE_DEPS_TOOL, STALE_SPEC_TOOL, DISABLED_TOOL, EDITED_TOOL):
        assert n in names, f"shipped catalog no longer ships {n!r} — retarget this fixture"
    by_name = {e["name"]: e for e in idx}

    # 1. THE F26 WITNESS — a dependency the package has since corrected.
    tid = by_name[STALE_DEPS_TOOL]["id"]
    b = _body(vault, tid)
    b["dependencies"] = ["playwright"]
    _write_body(vault, tid, b)
    by_name[STALE_DEPS_TOOL]["dependencies"] = ["playwright"]

    # 2. SAME CLASS, DIFFERENT FIELDS — description + parameter schema drift.
    tid = by_name[STALE_SPEC_TOOL]["id"]
    b = _body(vault, tid)
    b["description"] = "STALE DESCRIPTION FROM AN OLDER WHEEL"
    b["parameters_schema"] = {"query": {"type": "string"}}   # lost `max_results`
    _write_body(vault, tid, b)
    by_name[STALE_SPEC_TOOL]["description"] = "STALE DESCRIPTION FROM AN OLDER WHEEL"

    # 3. OPERATOR-DISABLED shipped tool. Re-arming it would be a security regression.
    tid = by_name[DISABLED_TOOL]["id"]
    b = _body(vault, tid)
    b["enabled"] = False
    b["dependencies"] = ["playwright"]      # ALSO stale — a disable must not block repair
    _write_body(vault, tid, b)
    by_name[DISABLED_TOOL]["enabled"] = False

    # 4. OPERATOR-EDITED seed: the implementation is no longer the package's, so the
    #    record describes THEIR code and the package has no standing to rewrite it.
    impl = vault / "tools" / "implementations" / f"{EDITED_TOOL}.py"
    impl.write_text(impl.read_text(encoding="utf-8")
                    + "\n# OPERATOR EDIT — do not clobber\n", encoding="utf-8")
    tid = by_name[EDITED_TOOL]["id"]
    b = _body(vault, tid)
    b["description"] = "MY OWN DOWNLOADER"
    b["dependencies"] = ["playwright"]
    _write_body(vault, tid, b)
    by_name[EDITED_TOOL]["description"] = "MY OWN DOWNLOADER"

    # 5. OPERATOR-FORGED tool the package never shipped.
    (vault / "tools" / "implementations" / "my_custom.py").write_text(
        "def run(**kw):\n    return {'success': True, 'mine': True}\n", encoding="utf-8")
    _write_body(vault, "tool_mine", {
        "id": "tool_mine", "name": "my_custom", "description": "hand written",
        "tool_type": "python_function", "parameters_schema": {},
        "return_schema": {}, "implementation_notes": "mine", "dependencies": [],
        "implementation_path": "vault/tools/implementations/my_custom.py",
        "status": "deployed", "forged_by_systemu": True, "enabled": True,
        "version": 3, "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    })
    idx.append({"id": "tool_mine", "name": "my_custom", "description": "hand written",
                "tool_type": "python_function", "parameter_names": [],
                "dependencies": [], "status": "deployed", "enabled": True,
                "forged_by_systemu": True, "created_at": "2026-01-01T00:00:00"})

    _write_index(vault, idx)
    (vault / ".seed_version").write_text(seed_version, encoding="utf-8")
    return vault


@pytest.fixture
def stale_vault(tmp_path, pkg_vault):
    """FAST-PATH arm: already on the installed version, so the seed loop never runs
    and the convergence pass is the only writer."""
    from systemu.runtime.vault_migrator import _installed_version
    return _build_stale_vault(tmp_path, pkg_vault, _installed_version())


@pytest.fixture
def stale_vault_bumped(tmp_path, pkg_vault):
    """VERSION-BUMP arm: the pre-existing seed loop runs as well."""
    return _build_stale_vault(tmp_path, pkg_vault, "0.9.40")


def _pkg_body(pkg_vault: Path, name: str) -> dict:
    idx = json.loads((pkg_vault / "tools" / "index.json").read_text(encoding="utf-8"))
    tid = next(e["id"] for e in idx if e.get("name") == name)
    return json.loads(
        (pkg_vault / "tools" / f"tool_{tid}.json").read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
#  HALF ONE — the correction must PROPAGATE
# ─────────────────────────────────────────────────────────────────────────────

class TestManifestCorrectionsReachAnExistingVault:

    def test_stale_dependency_converges_in_body_and_header(self, stale_vault, pkg_vault):
        """The F26 witness. `tools list` reads the INDEX HEADER's `dependencies`
        (cli_commands: `_od.unavailable_reason(t.get("dependencies"))`) while the
        tool record itself is the body — so BOTH have to converge or the operator
        still sees UNAVAILABLE.

        This is ALSO the arm that matters most on this project: the live-tryout rule
        folds fixes into the CURRENT version without a bump, so `installed ==
        vault_seed` on every deployed vault and the seed loop never executes. A
        convergence pass gated behind that fast path has not shipped to anybody."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault, STALE_DEPS_TOOL)["id"]
        assert _body(stale_vault, tid)["dependencies"] == ["playwright"]

        out = run(stale_vault)

        assert out.get("fast_path") is True, out
        want = _pkg_body(pkg_vault, STALE_DEPS_TOOL)["dependencies"]
        assert _body(stale_vault, tid)["dependencies"] == want, (
            "a manifest correction shipped WITHOUT a version bump never reached the vault")
        assert _header(stale_vault, STALE_DEPS_TOOL)["dependencies"] == want

    def test_it_converges_on_the_version_bump_arm_too(self, stale_vault_bumped, pkg_vault):
        """The seed loop keys on the IMPLEMENTATION's bytes. A manifest-only
        correction leaves the .py byte-identical, so the tool lands in
        `skipped_identical` and the loop never opens its record."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault_bumped, STALE_DEPS_TOOL)["id"]

        out = run(stale_vault_bumped)

        assert out.get("skipped_identical", 0) > 0, out
        want = _pkg_body(pkg_vault, STALE_DEPS_TOOL)["dependencies"]
        assert _body(stale_vault_bumped, tid)["dependencies"] == want
        assert _header(stale_vault_bumped, STALE_DEPS_TOOL)["dependencies"] == want

    def test_it_is_a_class_not_one_field(self, stale_vault, pkg_vault):
        """`dependencies` was the symptom. A description, a parameter schema, a
        return schema and the implementation notes all come from the same manifest
        and go stale the same way."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault, STALE_SPEC_TOOL)["id"]

        run(stale_vault)

        pkg = _pkg_body(pkg_vault, STALE_SPEC_TOOL)
        got = _body(stale_vault, tid)
        assert got["description"] == pkg["description"]
        assert got["parameters_schema"] == pkg["parameters_schema"]
        assert got["return_schema"] == pkg["return_schema"]
        assert got["implementation_notes"] == pkg["implementation_notes"]
        hdr = _header(stale_vault, STALE_SPEC_TOOL)
        assert hdr["description"] == pkg["description"]
        # The header's `parameter_names` is derived from the body's schema; a header
        # that keeps the old name list is the same staleness one level up.
        assert "max_results" in (hdr.get("parameter_names") or [])

    def test_a_corrected_schema_invalidates_the_stale_header_summary(
            self, stale_vault):
        """A refreshed `parameters_schema` leaves the header's
        `parameters_schema_summary` describing the OLD schema.
        `jobs._backfill_tool_headers_v061` cannot see that — its guard is
        `if all("parameters_schema_summary" in t ...): return`, so it only ever
        repairs an ABSENT summary.

        The pass must therefore DELETE the stale one, NOT rewrite it. Rewriting
        makes that guard true and disarms the sweep, which is also the only repair
        for `dry_run_status` / `version` / `implementation_path` after a seed
        update. Deleting keeps `vault._tool_header` the single writer of every
        derived header field AND leaves the sweep armed."""
        from systemu.runtime.vault_migrator import run
        from systemu.vault.vault import _summarise_schema

        # A stale summary matching the stale schema the fixture installed.
        idx = _index(stale_vault)
        for e in idx:
            if e.get("name") == STALE_SPEC_TOOL:
                e["parameters_schema_summary"] = _summarise_schema(
                    {"query": {"type": "string"}})
        _write_index(stale_vault, idx)

        run(stale_vault)

        hdr = _header(stale_vault, STALE_SPEC_TOOL)
        assert "parameters_schema_summary" not in hdr, (
            "the stale summary survived, or was rewritten in place — rewriting "
            "disarms the v0.6.1-d sweep that repairs the other derived fields")

    def test_a_disabled_tool_still_gets_its_manifest_repaired(self, stale_vault, pkg_vault):
        """A disable is a statement about RUNNING the tool, not about its
        description. Refusing to repair a disabled tool's manifest would leave a
        permanent stale island the operator could never clear."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault, DISABLED_TOOL)["id"]
        run(stale_vault)
        assert _body(stale_vault, tid)["dependencies"] == \
            _pkg_body(pkg_vault, DISABLED_TOOL)["dependencies"]


# ─────────────────────────────────────────────────────────────────────────────
#  HALF TWO — the fix must not become a data-loss bug
# ─────────────────────────────────────────────────────────────────────────────

class TestOperatorOwnedStateSurvives:

    def test_operator_forged_tool_is_untouched(self, stale_vault):
        """Every field EXCEPT `effect_tags`, which `backfill_effect_tags` derives
        from the tool's own source on every boot and has always owned. Comparing
        whole-file bytes would pin that unrelated pass instead of this one."""
        from systemu.runtime.vault_migrator import run
        body_p = stale_vault / "tools" / "tool_tool_mine.json"
        impl_p = stale_vault / "tools" / "implementations" / "my_custom.py"
        before_body = {k: v for k, v in
                       json.loads(body_p.read_text(encoding="utf-8")).items()
                       if k != "effect_tags"}
        before_impl = _sha(impl_p)

        run(stale_vault)

        after_body = {k: v for k, v in
                      json.loads(body_p.read_text(encoding="utf-8")).items()
                      if k != "effect_tags"}
        assert after_body == before_body, "the migrator rewrote an operator-forged tool"
        assert _sha(impl_p) == before_impl
        assert _header(stale_vault, "my_custom")["description"] == "hand written"

    def test_operator_forged_tool_survives_the_bump_arm_too(self, stale_vault_bumped):
        from systemu.runtime.vault_migrator import run
        body_p = stale_vault_bumped / "tools" / "tool_tool_mine.json"
        impl_p = stale_vault_bumped / "tools" / "implementations" / "my_custom.py"
        before_body = {k: v for k, v in
                       json.loads(body_p.read_text(encoding="utf-8")).items()
                       if k != "effect_tags"}
        before_impl = _sha(impl_p)

        run(stale_vault_bumped)

        after_body = {k: v for k, v in
                      json.loads(body_p.read_text(encoding="utf-8")).items()
                      if k != "effect_tags"}
        assert after_body == before_body
        assert _sha(impl_p) == before_impl
        assert _header(stale_vault_bumped, "my_custom")["description"] == "hand written"

    def test_operator_disabled_tool_stays_disabled(self, stale_vault):
        """`kept_disabled` exists because re-enabling something the operator switched
        off is a revoked control, not a migration."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault, DISABLED_TOOL)["id"]
        run(stale_vault)
        assert _body(stale_vault, tid)["enabled"] is False
        assert _header(stale_vault, DISABLED_TOOL)["enabled"] is False

    def test_operator_disabled_tool_stays_disabled_on_the_bump_arm(self, stale_vault_bumped):
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault_bumped, DISABLED_TOOL)["id"]
        run(stale_vault_bumped)
        assert _body(stale_vault_bumped, tid)["enabled"] is False
        assert _header(stale_vault_bumped, DISABLED_TOOL)["enabled"] is False

    def test_operator_edited_seed_keeps_its_manifest(self, stale_vault):
        """The implementation is no longer the package's bytes, so the record
        describes the OPERATOR's code. Refreshing its description or its schema from
        the package would describe code that is not there — and would silently
        re-point an approval, which binds to `(name, impl bytes, effect tags)`."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault, EDITED_TOOL)["id"]
        impl_p = stale_vault / "tools" / "implementations" / f"{EDITED_TOOL}.py"

        out = run(stale_vault)

        assert _body(stale_vault, tid)["description"] == "MY OWN DOWNLOADER"
        assert _body(stale_vault, tid)["dependencies"] == ["playwright"]
        assert "OPERATOR EDIT" in impl_p.read_text(encoding="utf-8")
        assert out.get("manifest_skipped_modified", 0) >= 1, out

    def test_an_operator_edit_made_AFTER_a_convergence_run_is_not_clobbered(
            self, stale_vault):
        """The durable half. Once the pass has recorded what the PACKAGE wrote, a
        later divergence is by definition the operator's and stays theirs across
        every future manifest correction.

        Without a recorded baseline the pass can only ever guess: the workshop edit
        path (`entity_edit.apply_tool_edit`) writes description / implementation_notes
        / dependencies and does NOT touch the .py, so the implementation sha guard is
        structurally blind to it."""
        from systemu.runtime.vault_migrator import run
        run(stale_vault)                                  # baseline now recorded

        tid = _header(stale_vault, STALE_DEPS_TOOL)["id"]
        b = _body(stale_vault, tid)
        b["description"] = "I RENAMED THIS MYSELF"
        b["dependencies"] = ["my_private_wheel"]
        _write_body(stale_vault, tid, b)

        out = run(stale_vault)

        got = _body(stale_vault, tid)
        assert got["description"] == "I RENAMED THIS MYSELF", \
            "a convergence run flattened an operator edit it had the evidence to spot"
        assert got["dependencies"] == ["my_private_wheel"]
        assert out.get("manifest_skipped_operator_edited", 0) >= 2, out

    def test_the_first_refresh_is_recoverable(self, stale_vault):
        """The one boot where the pass genuinely CANNOT tell a stale shipped value
        from an operator edit is the first one, before any baseline exists. It is
        allowed to converge there — that is the whole point of F26 — but the
        pre-refresh body is copied aside first, so the ambiguity is recoverable
        rather than silent."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault, STALE_DEPS_TOOL)["id"]

        out = run(stale_vault)

        assert out.get("manifest_preserved", 0) >= 1, out
        saved = list((stale_vault / ".pre-migration").rglob(f"tool_{tid}.json"))
        assert saved, "no pre-refresh copy of the body was kept"
        assert json.loads(saved[0].read_text(encoding="utf-8"))["dependencies"] \
            == ["playwright"]

    def test_the_refresh_set_never_reaches_operator_or_runtime_state(self):
        """A guard on the FIELD SET itself. `enabled` (Gate 3), `dry_run_status`
        (Gate 3.5), `version`, `effect_tags` (owned by the backfill),
        `implementation_path` (re-points what executes) and `status` are not the
        package's to write here."""
        from systemu.runtime import vault_migrator as vm
        forbidden = {"enabled", "dry_run_status", "dry_run_evidence", "version",
                     "effect_tags", "implementation_path", "tool_md_path", "status",
                     "forged_by_systemu", "id", "name", "created_at", "updated_at",
                     "trusted_inprocess", "evolution_history"}
        assert set(vm._PACKAGE_MANIFEST_FIELDS) == set(PACKAGE_OWNED), \
            vm._PACKAGE_MANIFEST_FIELDS
        assert not (set(vm._PACKAGE_MANIFEST_FIELDS) & forbidden)

    def test_runtime_state_on_a_refreshed_tool_survives(self, stale_vault, pkg_vault):
        """Same tool, both halves at once: the manifest converges WHILE the dry-run
        verdict and version bound to it stay put."""
        from systemu.runtime.vault_migrator import run
        tid = _header(stale_vault, STALE_DEPS_TOOL)["id"]
        b = _body(stale_vault, tid)
        b["dry_run_status"] = "passed"
        b["version"] = 7
        _write_body(stale_vault, tid, b)

        run(stale_vault)

        got = _body(stale_vault, tid)
        assert got["dependencies"] == _pkg_body(pkg_vault, STALE_DEPS_TOOL)["dependencies"]
        assert got["dry_run_status"] == "passed"
        assert got["version"] == 7


# ─────────────────────────────────────────────────────────────────────────────
#  THE COUNTERS — pinned against what actually changed on disk
# ─────────────────────────────────────────────────────────────────────────────

class TestCountersReportWhatActuallyChanged:

    def test_manifest_counters_equal_the_measured_disk_delta(self, stale_vault):
        """F14 and F26 are the same shape: a counter reporting a number nobody can
        check. Here the number IS checked — field by field, against the files. Run on
        the fast-path arm so the convergence pass is the only writer of these
        fields."""
        from systemu.runtime.vault_migrator import run
        before = _snapshot_bodies(stale_vault)

        out = run(stale_vault)

        fields, tools = _measured_delta(before, _snapshot_bodies(stale_vault))
        assert out["manifest_fields_refreshed"] == fields, (
            f"counter says {out['manifest_fields_refreshed']} package-owned fields "
            f"changed; the filesystem says {fields}")
        assert out["manifest_tools_refreshed"] == tools, (
            f"counter says {out['manifest_tools_refreshed']} tools; disk says {tools}")
        assert fields > 0, "fixture is stale — nothing to converge"

    def test_header_counter_equals_the_measured_index_delta(self, stale_vault):
        """The header counter gets the same treatment as the body one. Compared
        without `effect_tags`, which `converge_index_effect_tags` owns and writes
        into the same file on the same boot."""
        from systemu.runtime.vault_migrator import run

        def snap():
            return {e.get("id"): {k: json.dumps(v, sort_keys=True)
                                  for k, v in e.items() if k != "effect_tags"}
                    for e in _index(stale_vault) if isinstance(e, dict)}

        before = snap()

        out = run(stale_vault)

        after = snap()
        changed = sum(1 for tid, e in after.items() if before.get(tid) != e)
        assert out["manifest_headers_refreshed"] == changed, (
            f"counter says {out['manifest_headers_refreshed']} headers; the "
            f"filesystem says {changed}")
        assert changed > 0, "fixture is stale — no header moved"

    def test_impl_counter_is_named_for_what_it_counts(self, stale_vault_bumped):
        """`updated` counted IMPLEMENTATION FILES OVERWRITTEN and was read as 'the
        catalog was refreshed' — the operator saw `updated=41` on a run that moved no
        manifest field at all. The misreadable name must not survive (the daemon logs
        this dict verbatim), and the honest one must equal the measured delta."""
        from systemu.runtime.vault_migrator import run
        before = _snapshot_impls(stale_vault_bumped)

        out = run(stale_vault_bumped)

        after = _snapshot_impls(stale_vault_bumped)
        really = sum(1 for n, s in after.items() if before.get(n) != s)
        assert "updated" not in out, (
            "`updated` is back in the operator-facing summary; the daemon logs this "
            "dict verbatim, so the misreadable name IS the lie")
        assert out["impl_replaced"] == really, (
            f"impl_replaced={out['impl_replaced']} but {really} implementation "
            f"file(s) actually changed")
        assert really >= 1, "fixture is stale — no implementation diverged"

    def test_a_converged_vault_reports_zero_and_writes_nothing(self, stale_vault):
        """Idempotence, and the other half of an honest counter: a run that changes
        nothing must SAY it changed nothing."""
        from systemu.runtime.vault_migrator import run
        run(stale_vault)
        before = _snapshot_bodies(stale_vault)
        before_impls = _snapshot_impls(stale_vault)

        out = run(stale_vault)

        assert _snapshot_bodies(stale_vault) == before
        assert _snapshot_impls(stale_vault) == before_impls
        assert out["manifest_fields_refreshed"] == 0, out
        assert out["manifest_tools_refreshed"] == 0, out

    def test_the_bump_arm_reports_the_manifest_counters_too(self, stale_vault_bumped):
        """A migration that also runs the seed loop must still carry the manifest
        numbers — otherwise the operator's only signal on that boot is `impl_replaced`
        again, which is exactly the number that misled them."""
        from systemu.runtime.vault_migrator import run
        out = run(stale_vault_bumped)
        for k in ("manifest_tools_refreshed", "manifest_fields_refreshed",
                  "manifest_skipped_operator_edited", "manifest_skipped_modified"):
            assert k in out, (k, out)
