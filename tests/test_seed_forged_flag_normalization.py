"""Shipped seed tools are REPO CODE, not LLM-forged — and the vault must say so.

``Tool.forged_by_systemu`` means "an LLM authored this body". 39 of the 41
shipped seed JSONs carried ``true`` as a copy-paste artifact of the original
prototype vault export. Two controls key on the flag and both misfired:

  * ``action_governance.forged_network_denied`` HARD-DENIED the seeded network
    tools (``fetch_html`` / ``fetch_json`` / ``api_call_get`` / ``download_file``)
    with ``egress_enforcer_unavailable`` — a live shipped-broken feature;
  * ``tool_sandbox._command_gate_already_scored`` refused the command-gate
    exemption to ``run_command`` / ``run_cli_command``, so they carded twice.

The fix does NOT blanket-flip the flag at runtime. A vault tool is re-labelled
repo-vetted ONLY when its implementation is BYTE-IDENTICAL to the shipped
package implementation (sha match ⇒ vetted). An operator- or forge-modified
body keeps ``forged=true`` and stays denied/gated. The sha guard is the
load-bearing part of this change, because the change RELAXES an existing deny —
``TestShaGuardIsLoadBearing`` and ``TestSecurityPins`` exist to hold that line.
"""
from pathlib import Path
import json

import pytest

from systemu.core.models import Tool
from systemu.runtime import vault_migrator as vm
from systemu.runtime.action_governance import forged_network_denied
from systemu.runtime.tool_sandbox import (
    _command_gate_already_scored,
    requires_subprocess_isolation,
)

# The four seeded network actuators that `forged_network_denied` was refusing.
NET_SEEDS = ["fetch_html", "fetch_json", "api_call_get", "download_file"]
# The two seeded shell tools the command-gate carve-out was refusing to exempt.
SHELL_SEEDS = ["run_command", "run_cli_command"]

TEST_VERSION = "9.9.9-test"


# ── fixtures / helpers ──────────────────────────────────────────────────────
@pytest.fixture
def empty_vault(tmp_path):
    """A bare vault skeleton — no seed tools yet.

    The directory is NAMED ``vault`` on purpose. Every shipped seed body
    declares ``implementation_path`` as ``vault/tools/implementations/<n>.py``
    — a path relative to the vault root's PARENT (``tool_sandbox.execute_tool``
    anchors it there, see ``tool_dry_run._resolve_impl_path``). A bare
    ``tmp_path`` vault would not reproduce that layout, so a guard that resolves
    the declared path the way the runtime does could not be tested honestly.
    """
    vault = tmp_path / "vault"
    (vault / "tools" / "implementations").mkdir(parents=True, exist_ok=True)
    (vault / "shadow_army").mkdir(parents=True, exist_ok=True)
    (vault / "tools" / "index.json").write_text("[]", encoding="utf-8")
    (vault / "shadow_army" / "index.json").write_text("[]", encoding="utf-8")
    return vault


@pytest.fixture
def pinned_version(monkeypatch):
    """Pin the 'installed' version so fast-path vs full-pass is explicit."""
    monkeypatch.setattr(vm, "_installed_version", lambda: TEST_VERSION)
    return TEST_VERSION


def _pkg_root() -> Path:
    return vm._package_vault_root()


def _pkg_entry(name: str) -> dict:
    idx = json.loads((_pkg_root() / "tools" / "index.json").read_text(encoding="utf-8"))
    return next(e for e in idx if e.get("name") == name)


def _seed_vault_tool(vault_dir: Path, name: str, *, forged: bool, modified: bool = False) -> Path:
    """Plant ONE seed tool into a vault: package body (with `forged` forced) plus
    an implementation that is either byte-identical to the package's or modified.

    Copies implementation BYTES — a text round-trip would rewrite newlines on
    Windows and silently break the very sha equality under test.
    """
    pkg = _pkg_root()
    entry = _pkg_entry(name)
    tid = entry["id"]

    body = json.loads((pkg / "tools" / f"tool_{tid}.json").read_text(encoding="utf-8"))
    body["forged_by_systemu"] = forged
    (vault_dir / "tools" / f"tool_{tid}.json").write_text(
        json.dumps(body, indent=2) + "\n", encoding="utf-8")

    raw = (pkg / "tools" / "implementations" / f"{name}.py").read_bytes()
    if modified:
        raw = raw + b"\n# operator edit: exfiltrate()\n"
    impl_path = vault_dir / "tools" / "implementations" / f"{name}.py"
    impl_path.write_bytes(raw)

    idx_path = vault_dir / "tools" / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    idx.append(dict(entry, forged_by_systemu=forged))
    idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
    return impl_path


def _vault_tool(vault_dir: Path, name: str) -> Tool:
    """Load the vault's Tool exactly as the runtime does — index → body."""
    idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
    entry = next(e for e in idx if e.get("name") == name)
    body = json.loads(
        (vault_dir / "tools" / f"tool_{entry['id']}.json").read_text(encoding="utf-8"))
    return Tool.model_validate(body)


def _vault_tool_by_id(vault_dir: Path, tid: str) -> Tool:
    """Load a Tool by ID — `vault._update_index` keys on ``id``, so two index
    entries may legitimately share a NAME and a name lookup cannot address them
    both."""
    body = json.loads(
        (vault_dir / "tools" / f"tool_{tid}.json").read_text(encoding="utf-8"))
    return Tool.model_validate(body)


def _vault_impl(vault_dir: Path, name: str) -> Path:
    return vault_dir / "tools" / "implementations" / f"{name}.py"


def _pkg_impl_bytes(name: str) -> bytes:
    return (_pkg_root() / "tools" / "implementations" / f"{name}.py").read_bytes()


def _plant_impostor(vault_dir: Path, name: str, *, tid: str = "tool_impostor",
                    implementation_path=None) -> str:
    """Append a SECOND index entry carrying an existing ``name`` under a NEW id.

    This is what `governor._materialise_forge` produces: it builds a Tool from
    the LLM-supplied ``spec["name"]`` with a fresh ``generate_id("tool")`` and —
    unlike `activity_extractor` and the tools page — performs NO existing-name
    check. `vault._update_index` upserts on ``id``, so the new entry lands
    ALONGSIDE the seed rather than replacing it. Returns the impostor's id.
    """
    body = json.loads(
        (_pkg_root() / "tools" / f"tool_{_pkg_entry(name)['id']}.json").read_text(
            encoding="utf-8"))
    body["id"] = tid
    body["forged_by_systemu"] = True
    if implementation_path is not None:
        body["implementation_path"] = implementation_path
    (vault_dir / "tools" / f"tool_{tid}.json").write_text(
        json.dumps(body, indent=2) + "\n", encoding="utf-8")

    idx_path = vault_dir / "tools" / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    idx.append(dict(_pkg_entry(name), id=tid, forged_by_systemu=True))
    idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
    return tid


def _raw_body_flag(vault_dir: Path, name: str) -> bool:
    """Read ``forged_by_systemu`` straight off the JSON body.

    Used where the body cannot round-trip through `Tool.model_validate` — a
    non-string ``implementation_path`` fails pydantic validation, which is worth
    knowing (such a body could never load as a Tool) but is not what is under
    test: the migrator reads raw JSON, so the guard still has to refuse it.
    """
    tid = _pkg_entry(name)["id"]
    body = json.loads(
        (vault_dir / "tools" / f"tool_{tid}.json").read_text(encoding="utf-8"))
    return bool(body.get("forged_by_systemu", False))


def _declare_impl_path(vault_dir: Path, name: str, value) -> None:
    """Rewrite a planted seed body's ``implementation_path`` — the field that
    decides which file actually EXECUTES (`tool_sandbox.execute_tool`)."""
    tid = _pkg_entry(name)["id"]
    body_path = vault_dir / "tools" / f"tool_{tid}.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    if value is None:
        body.pop("implementation_path", None)
    else:
        body["implementation_path"] = value
    body_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _fast_path_vault(vault_dir: Path) -> None:
    (vault_dir / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")


# ── Part A: the shipped package data ────────────────────────────────────────
class TestShippedSeedData:
    """The seeds are repo code. Nothing shipped may claim an LLM authored it."""

    def test_no_shipped_seed_body_claims_forged(self):
        offenders = sorted(
            p.name for p in (_pkg_root() / "tools").glob("tool_*.json")
            if json.loads(p.read_text(encoding="utf-8")).get("forged_by_systemu")
        )
        assert offenders == []

    def test_no_shipped_index_entry_claims_forged(self):
        idx = json.loads((_pkg_root() / "tools" / "index.json").read_text(encoding="utf-8"))
        offenders = sorted(e.get("name") for e in idx if e.get("forged_by_systemu"))
        assert offenders == []


# ── (i) fresh vault ─────────────────────────────────────────────────────────
class TestFreshVaultMigration:
    def test_seeds_land_unforged(self, empty_vault, pinned_version):
        out = vm.run(Path(empty_vault))
        assert out["fast_path"] is False
        assert out["added"] > 0

        for name in ["run_command", "fetch_html"]:
            assert _vault_tool(empty_vault, name).forged_by_systemu is False


# ── (ii) existing vault, SAME-VERSION boot (the fast-path case) ─────────────
class TestSameVersionBootNormalizes:
    """No version bump ships with this fix, so a real operator's vault ALWAYS
    takes the fast path. Normalization that ran behind it would never execute."""

    def test_identical_impl_normalized_on_fast_path(self, empty_vault, pinned_version):
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        (empty_vault / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")
        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True

        out = vm.run(Path(empty_vault))

        # The run genuinely short-circuited — this is what proves the
        # normalization is placed BEFORE the fast-path return.
        assert out["fast_path"] is True
        assert out.get("added") is None and out.get("updated") is None
        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is False

    def test_normalization_is_idempotent(self, empty_vault, pinned_version):
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        (empty_vault / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")
        vm.run(Path(empty_vault))
        body_path = empty_vault / "tools" / f"tool_{_pkg_entry('fetch_html')['id']}.json"
        first = body_path.read_bytes()

        vm.run(Path(empty_vault))

        assert body_path.read_bytes() == first  # converged: a no-op second boot

    def test_already_false_body_left_alone(self, empty_vault, pinned_version):
        _seed_vault_tool(empty_vault, "fetch_html", forged=False)
        (empty_vault / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")
        vm.run(Path(empty_vault))
        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is False


# ── (iii) THE SHA GUARD — a modified body stays forged ──────────────────────
class TestShaGuardIsLoadBearing:
    """This change RELAXES a deny. The sha match is the ONLY thing separating
    "vetted repo code" from "a body someone else wrote"."""

    def test_modified_impl_keeps_forged_flag(self, empty_vault, pinned_version):
        _seed_vault_tool(empty_vault, "fetch_html", forged=True, modified=True)
        (empty_vault / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")

        out = vm.run(Path(empty_vault))

        assert out["fast_path"] is True
        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True

    def test_modified_net_tool_stays_denied(self, empty_vault, pinned_version):
        """The end-to-end consequence of the guard: still refused at the gate."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True, modified=True)
        (empty_vault / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")
        vm.run(Path(empty_vault))

        tool = _vault_tool(empty_vault, "fetch_html")
        denial = forged_network_denied(
            tool, impl_path=str(_vault_impl(empty_vault, "fetch_html")))
        assert denial is not None and "egress_enforcer_unavailable" in denial

    def test_modified_shell_tool_denied_the_carve_out(self, empty_vault, pinned_version):
        _seed_vault_tool(empty_vault, "run_command", forged=True, modified=True)
        (empty_vault / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")
        vm.run(Path(empty_vault))

        tool = _vault_tool(empty_vault, "run_command")
        assert tool.forged_by_systemu is True
        assert _command_gate_already_scored(tool, "run_command", {"shell_exec"}) is False

    def test_full_pass_converges_after_impl_recopy(self, empty_vault, pinned_version):
        """A MODIFIED impl on a version-bump boot: the UPDATE path re-copies the
        package impl AND body, so the tool converges to unforged within one run."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True, modified=True)
        (empty_vault / ".seed_version").write_text("0.0.1", encoding="utf-8")

        out = vm.run(Path(empty_vault))

        assert out["fast_path"] is False
        assert out["updated"] >= 1
        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is False
        assert vm._file_sha256(_vault_impl(empty_vault, "fetch_html")) == vm._file_sha256(
            _pkg_root() / "tools" / "implementations" / "fetch_html.py")


# ── (iv) the live bug: seeded network tools must be runnable ────────────────
class TestNetworkSeedsNoLongerDenied:
    @pytest.mark.parametrize("name", NET_SEEDS)
    def test_seed_net_tool_not_denied(self, empty_vault, pinned_version, name):
        vm.run(Path(empty_vault))

        tool = _vault_tool(empty_vault, name)
        impl = _vault_impl(empty_vault, name)
        assert impl.exists(), f"{name} implementation did not land"
        assert forged_network_denied(tool, impl_path=str(impl)) is None


# ── (v) the command-gate carve-out ─────────────────────────────────────────
class TestCommandGateCarveOut:
    @pytest.mark.parametrize("name", SHELL_SEEDS)
    def test_seed_shell_tool_is_exempt(self, empty_vault, pinned_version, name):
        vm.run(Path(empty_vault))
        tool = _vault_tool(empty_vault, name)
        assert _command_gate_already_scored(tool, name, {"shell_exec"}) is True


# ── (vi) SECURITY PINS — the forge path is untouched ───────────────────────
class TestSecurityPins:
    """Nothing here may pass because the seeds were relabelled. These pin the
    behaviour for bodies an LLM actually wrote."""

    def test_forged_net_tool_is_still_denied(self, tmp_path):
        impl = tmp_path / "forged_net.py"
        impl.write_text(
            "import requests\n"
            "def run(url):\n"
            "    return requests.get(url).text\n",
            encoding="utf-8")
        tool = Tool(
            id="tool_forged_net", name="forged_net", description="forged",
            tool_type="api_call", forged_by_systemu=True,
            implementation_path=str(impl),
        )
        denial = forged_network_denied(tool, impl_path=str(impl))
        assert denial is not None and "egress_enforcer_unavailable" in denial

    def test_forged_tool_named_run_command_gets_no_exemption(self):
        """A forge picks its own name AND its own impl filename — a name-only
        exemption would let it pass the command gate with a benign string and
        then run whatever its BODY does."""
        tool = Tool(
            id="tool_impostor", name="run_command", description="impostor",
            tool_type="api_call", forged_by_systemu=True,
        )
        assert _command_gate_already_scored(tool, "run_command", {"shell_exec"}) is False

    def test_forged_tool_named_run_cli_command_gets_no_exemption(self):
        tool = Tool(
            id="tool_impostor2", name="run_cli_command", description="impostor",
            tool_type="api_call", forged_by_systemu=True,
        )
        assert _command_gate_already_scored(tool, "run_cli_command", {"shell_exec"}) is False

    def test_unknown_vault_tool_is_never_normalized(self, empty_vault, pinned_version):
        """A user-forged tool whose name is not in the package index is not a
        seed and must never be re-labelled by the migrator."""
        impl = _vault_impl(empty_vault, "my_custom")
        impl.write_bytes(b"import requests\ndef run(u):\n    return requests.get(u).text\n")
        idx_path = empty_vault / "tools" / "index.json"
        idx_path.write_text(json.dumps([{
            "id": "tool_custom", "name": "my_custom", "tool_type": "api_call",
            "status": "deployed", "enabled": True, "forged_by_systemu": True,
        }], indent=2) + "\n", encoding="utf-8")
        (empty_vault / "tools" / "tool_tool_custom.json").write_text(json.dumps({
            "id": "tool_custom", "name": "my_custom", "description": "mine",
            "tool_type": "api_call", "status": "deployed", "enabled": True,
            "forged_by_systemu": True, "version": 1,
        }, indent=2) + "\n", encoding="utf-8")
        (empty_vault / ".seed_version").write_text(TEST_VERSION, encoding="utf-8")

        vm.run(Path(empty_vault))

        assert _vault_tool(empty_vault, "my_custom").forged_by_systemu is True

    def test_forged_body_named_after_a_seed_is_sha_guarded(self, empty_vault, pinned_version):
        """A forged tool that NAMES ITSELF after a seed does not inherit the
        seed's label — its body does not match the package's.

        (Single-entry case. The genuine name-COLLISION — two index entries, one
        name — is `TestNameCollisionIsAmbiguous`; this test never built one.)
        """
        _seed_vault_tool(empty_vault, "fetch_html", forged=True, modified=True)
        impl = _vault_impl(empty_vault, "fetch_html")
        impl.write_bytes(b"import requests\ndef run(u):\n    return requests.get(u).text\n")
        _fast_path_vault(empty_vault)

        vm.run(Path(empty_vault))

        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True


# ── (vi-a) F1: TWO index entries, ONE name ⇒ refuse, don't guess ────────────
class TestNameCollisionIsAmbiguous:
    """`vault._update_index` upserts on ``id``, so two entries can share a NAME.
    The normalizer keyed a LAST-WINS ``{name: entry}`` dict, hashed
    ``implementations/{name}.py``, and then wrote the flag to
    ``tool_{vault_entry['id']}.json`` — i.e. it could write "vetted" onto a
    record whose body it had never hashed.

    Reachable chain: `governor._materialise_forge` creates a second
    ``fetch_html`` under a new id (no existing-name check); `tool_forge` writes
    ``implementations/fetch_html.py``, clobbering the seed impl — the sha guard
    correctly skips while that holds. A version-bump UPDATE then restores the
    PACKAGE bytes over ``fetch_html.py`` and replaces only the FIRST name match,
    so the impostor entry survives with package bytes on disk. Next boot the
    last-wins dict picks the impostor, the hash matches, and the impostor's flag
    is cleared. A later `tool_recalibrator` reforge rewrites ``fetch_html.py``
    with fresh LLM code and nothing re-stamps the flag.
    """

    def _collide(self, vault_dir):
        """Genuine seed (byte-identical impl) + an impostor entry, same name,
        different id — the impostor LAST so the last-wins dict selects it."""
        _seed_vault_tool(vault_dir, "fetch_html", forged=True)
        tid = _plant_impostor(vault_dir, "fetch_html")
        _fast_path_vault(vault_dir)
        return tid

    def test_impostor_flag_is_not_cleared(self, empty_vault, pinned_version):
        tid = self._collide(empty_vault)

        vm.run(Path(empty_vault))

        assert _vault_tool_by_id(empty_vault, tid).forged_by_systemu is True

    def test_impostor_stays_denied_and_isolated(self, empty_vault, pinned_version):
        """The consequences that make this HIGH: clearing the flag lifts the
        network hard-deny AND stops forcing subprocess isolation, so LLM-authored
        egress code would run in-process with the deny lifted."""
        tid = self._collide(empty_vault)

        vm.run(Path(empty_vault))

        impostor = _vault_tool_by_id(empty_vault, tid)
        denial = forged_network_denied(
            impostor, impl_path=str(_vault_impl(empty_vault, "fetch_html")))
        assert denial is not None and "egress_enforcer_unavailable" in denial
        assert requires_subprocess_isolation(impostor) is True

    def test_genuine_entry_is_also_left_alone(self, empty_vault, pinned_version):
        """Fail-closed means refusing the NAME, not picking a winner — the guard
        has no evidence about which entry the operator meant."""
        self._collide(empty_vault)

        vm.run(Path(empty_vault))

        genuine_id = _pkg_entry("fetch_html")["id"]
        assert _vault_tool_by_id(empty_vault, genuine_id).forged_by_systemu is True

    def test_ambiguity_is_counted_and_not_miscounted(self, empty_vault, pinned_version):
        """Its own counter — an ambiguous name is not a "modified body", and an
        operator reading the log should be able to tell the two apart."""
        self._collide(empty_vault)

        out = vm.normalize_seed_forged_flags(Path(empty_vault))

        assert out["skipped_ambiguous"] == 1
        assert out["skipped_modified"] == 0
        assert out["normalized"] == 0
        assert out["errors"] == []


# ── (vi-b) F2: hash the file that EXECUTES, not the file the name implies ──
class TestHashesTheExecutedFile:
    """The guard hashed ``implementations/{name}.py`` unconditionally. The file
    the runtime actually loads is ``tool.implementation_path``
    (`tool_sandbox.execute_tool`, anchored at the vault root's PARENT). The
    module's own sibling `backfill_effect_tags` already reads that field, so the
    module knew which one is authoritative.

    No in-repo writer currently emits a divergent path — all five checked emit
    ``{name}.py`` — so this is defence-in-depth. The guard exists precisely so
    it does not depend on an unenforced invariant spread across four files.
    """

    def test_declared_path_is_hashed_not_the_name(self, empty_vault, pinned_version):
        """``fetch_html.py`` is byte-identical to the package; the body declares
        that ``payload.py`` is what runs. Hashing by name blesses a body whose
        executable was never hashed."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        (empty_vault / "tools" / "implementations" / "payload.py").write_bytes(
            b"import requests\ndef run(u):\n    return requests.get(u).text\n")
        _declare_impl_path(
            empty_vault, "fetch_html", "vault/tools/implementations/payload.py")
        _fast_path_vault(empty_vault)

        vm.run(Path(empty_vault))

        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True

    def test_prefix_named_sibling_dir_is_refused(self, empty_vault, pinned_version):
        """Containment must be by PATH COMPONENT. ``implementations_evil`` is a
        raw string PREFIX match of ``implementations`` — this repo already
        shipped that exact bug class (``C:/Radiology/x`` counted as inside
        ``C:/R``).

        The planted file is BYTE-IDENTICAL to the package impl, so the sha
        compare PASSES: only the containment check can refuse this.
        """
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        evil = empty_vault / "tools" / "implementations_evil"
        evil.mkdir(parents=True, exist_ok=True)
        (evil / "fetch_html.py").write_bytes(_pkg_impl_bytes("fetch_html"))
        _declare_impl_path(
            empty_vault, "fetch_html", "vault/tools/implementations_evil/fetch_html.py")
        _fast_path_vault(empty_vault)

        vm.run(Path(empty_vault))

        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True

    def test_traversal_out_of_impl_dir_is_refused(self, empty_vault, pinned_version):
        """``..`` segments must be resolved BEFORE the containment test."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        outside = empty_vault.parent / "outside.py"
        outside.write_bytes(_pkg_impl_bytes("fetch_html"))   # byte-identical again
        _declare_impl_path(
            empty_vault, "fetch_html",
            "vault/tools/implementations/../../../outside.py")
        _fast_path_vault(empty_vault)

        out = vm.normalize_seed_forged_flags(Path(empty_vault))

        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True
        assert out["errors"] == []          # a clean refusal, not a swallowed raise

    def test_absolute_path_inside_impl_dir_is_accepted(self, empty_vault, pinned_version):
        """Absolute paths are legal (`execute_tool` uses them as-is) — the rule
        is containment, not relativeness."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        _declare_impl_path(
            empty_vault, "fetch_html", str(_vault_impl(empty_vault, "fetch_html")))
        _fast_path_vault(empty_vault)

        vm.run(Path(empty_vault))

        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is False

    def test_absolute_path_outside_impl_dir_is_refused(self, empty_vault, pinned_version):
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        outside = empty_vault.parent / "elsewhere.py"
        outside.write_bytes(_pkg_impl_bytes("fetch_html"))
        _declare_impl_path(empty_vault, "fetch_html", str(outside))
        _fast_path_vault(empty_vault)

        vm.run(Path(empty_vault))

        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True


# ── (vi-c) malformed input must never reach daemon boot ────────────────────
class TestMalformedDeclaredPathNeverRaises:
    """`normalize_seed_forged_flags` runs on EVERY boot. A malformed body must
    produce a refusal, never an exception that the outer catch-all has to eat
    (that would take the whole normalization pass down with it)."""

    @pytest.mark.parametrize("declared", [42, ["fetch_html.py"], {"p": "x.py"}, True])
    def test_non_string_declared_path_is_refused_cleanly(
            self, empty_vault, pinned_version, declared):
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        _declare_impl_path(empty_vault, "fetch_html", declared)
        _fast_path_vault(empty_vault)

        out = vm.normalize_seed_forged_flags(Path(empty_vault))

        assert "error" not in out                  # outer catch-all never fired
        assert out["errors"] == []                 # per-tool except never fired
        assert out["skipped_impl_path"] == 1       # refused, not silently ignored
        assert _raw_body_flag(empty_vault, "fetch_html") is True

    def test_declared_path_naming_the_impl_dir_itself(self, empty_vault, pinned_version):
        """A directory passes containment (it IS the root) — hashing it would
        raise into the per-tool handler. Refused as "no file, no provenance"."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        _declare_impl_path(empty_vault, "fetch_html", "vault/tools/implementations/")
        _fast_path_vault(empty_vault)

        out = vm.normalize_seed_forged_flags(Path(empty_vault))

        assert out["errors"] == []          # a clean skip, not a swallowed raise
        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is True

    def test_redundant_but_legal_path_spelling_still_resolves(self, empty_vault, pinned_version):
        """`.`/`..` segments that land back on the REAL file are a legal spelling
        of it, not an escape — resolving before the containment test is what
        keeps this from being a false refusal."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        _declare_impl_path(
            empty_vault, "fetch_html",
            "vault/tools/implementations/./../implementations/fetch_html.py")
        _fast_path_vault(empty_vault)

        vm.run(Path(empty_vault))

        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is False

    @pytest.mark.parametrize("declared", [None, "", "   "])
    def test_absent_declared_path_falls_back_to_the_name(
            self, empty_vault, pinned_version, declared):
        """The documented fallback — `backfill_effect_tags` uses the same one.
        An absent field is not evidence of tampering, so the repair proceeds."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        _declare_impl_path(empty_vault, "fetch_html", declared)
        _fast_path_vault(empty_vault)

        out = vm.normalize_seed_forged_flags(Path(empty_vault))

        assert out["errors"] == []
        assert _vault_tool(empty_vault, "fetch_html").forged_by_systemu is False


# ── (vi-d) the fix must not silently disable the repair ────────────────────
class TestRepairStillWorks:
    """Both fixes REFUSE things. A guard that refuses everything would resurrect
    the live bug it was written to fix: ``fetch_html`` / ``fetch_json`` /
    ``api_call_get`` / ``download_file`` hard-denied in a stock install.

    This is also the pin that catches an anchor mistake: the shipped bodies
    declare ``vault/tools/implementations/<n>.py`` — resolved relative to the
    vault root's PARENT. Anchoring that string at the implementations dir
    instead (the sibling `backfill_effect_tags` does exactly that) yields a
    path that does not exist, and every seed would be silently skipped.
    """

    @pytest.mark.parametrize("name", NET_SEEDS)
    def test_honest_single_entry_is_still_normalized(self, empty_vault, pinned_version, name):
        _seed_vault_tool(empty_vault, name, forged=True)
        _fast_path_vault(empty_vault)

        out = vm.normalize_seed_forged_flags(Path(empty_vault))

        assert out["normalized"] == 1
        assert out["skipped_ambiguous"] == 0 and out["skipped_modified"] == 0
        tool = _vault_tool(empty_vault, name)
        assert tool.forged_by_systemu is False
        assert forged_network_denied(
            tool, impl_path=str(_vault_impl(empty_vault, name))) is None

    def test_index_header_still_normalized(self, empty_vault, pinned_version):
        """The index header is derived from the body; the two must not drift."""
        _seed_vault_tool(empty_vault, "fetch_html", forged=True)
        _fast_path_vault(empty_vault)

        out = vm.normalize_seed_forged_flags(Path(empty_vault))

        idx = json.loads(
            (empty_vault / "tools" / "index.json").read_text(encoding="utf-8"))
        assert out["index_normalized"] == 1
        assert [e for e in idx if e["name"] == "fetch_html"][0]["forged_by_systemu"] is False


# ── (vii) isolation policy ─────────────────────────────────────────────────
class TestSubprocessIsolation:
    def test_seed_runs_in_process_after_migration(self, empty_vault, pinned_version):
        vm.run(Path(empty_vault))
        assert requires_subprocess_isolation(_vault_tool(empty_vault, "fetch_html")) is False

    def test_forged_tool_still_isolated(self):
        tool = Tool(
            id="tool_forged_iso", name="forged_iso", description="forged",
            tool_type="api_call", forged_by_systemu=True,
        )
        assert requires_subprocess_isolation(tool) is True

    def test_none_tool_still_isolated(self):
        assert requires_subprocess_isolation(None) is True
