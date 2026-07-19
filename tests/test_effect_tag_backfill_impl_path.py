"""The effect-tag backfill must resolve a tool body's implementation the way the
RUNTIME resolves it — anchored at the vault root's PARENT, not at the
implementations dir.

Every shipped seed body declares ``vault/tools/implementations/<name>.py``.
That is how the value is WRITTEN (``tool_forge`` / ``tool_recalibrator``:
``impl_path.relative_to(vault_dir.parent)``) and how it is READ at execution
time (``tool_sandbox.execute_tool``: ``vault_root.parent / implementation_path``).

``backfill_effect_tags`` anchored that same string at the implementations dir,
yielding ``<vault>/tools/implementations/vault/tools/implementations/<name>.py``
— a path that never exists. The read is wrapped defensively, so the failure was
SILENT: every tool was stamped ``effect_tags: []`` while the pass reported
``stamped=41, errors=[]``.

That empty stamp is not inert. The backfill's own MONOTONIC money-move floor
(``any_money_move_signal``) only runs inside ``if source:`` — so the same
unreadability that erased the classification also suppressed the floor that
exists to make money-moves impossible to miss.

The sibling ``normalize_seed_forged_flags`` was fixed for exactly this
(``_resolve_vault_impl``); this module holds the backfill to the same anchoring
AND the same containment discipline.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from systemu.runtime import vault_migrator as vm
from systemu.runtime.effect_tags import EffectTag

_OMIT = object()

# Source with an unmistakable, independently-detectable effect.
_DELETES = "import os\ndef run(p):\n    os.remove(p)\n"
_PAYS = "import stripe\ndef run(**k):\n    return stripe.PaymentIntent.create(**k)\n"


@pytest.fixture
def vault(tmp_path):
    """A vault dir literally NAMED ``vault`` — the shipped
    ``vault/tools/implementations/...`` shape only resolves against a parent."""
    v = tmp_path / "vault"
    (v / "tools" / "implementations").mkdir(parents=True)
    return v


def _seed(vault: Path, tid: str, name: str, source: str, *, declared=_OMIT) -> Path:
    """Write a tool the way the package ships one. ``declared`` defaults to the
    REAL shipped shape; pass a value to override, or ``None`` to omit the key."""
    impl = vault / "tools" / "implementations"
    (impl / f"{name}.py").write_text(source, encoding="utf-8")

    body = {"id": tid, "name": name, "description": "d",
            "tool_type": "python", "status": "deployed"}
    if declared is _OMIT:
        body["implementation_path"] = f"vault/tools/implementations/{name}.py"
    elif declared is not None:
        body["implementation_path"] = declared

    body_path = vault / "tools" / f"tool_{tid}.json"
    body_path.write_text(json.dumps(body), encoding="utf-8")

    idx = vault / "tools" / "index.json"
    entries = json.loads(idx.read_text(encoding="utf-8")) if idx.exists() else []
    entries.append({"id": tid, "name": name})
    idx.write_text(json.dumps(entries), encoding="utf-8")
    return body_path


def _tags(body_path: Path):
    return json.loads(body_path.read_text(encoding="utf-8")).get("effect_tags")


# ── the defect itself ──────────────────────────────────────────────────────
class TestShippedShapeIsResolved:

    def test_shipped_relative_shape_is_classified(self, vault):
        """THE regression pin. Before the fix this stamped ``[]``."""
        body = _seed(vault, "d1", "deleter", _DELETES)

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == [EffectTag.LOCAL_DELETE.value]
        assert out["errors"] == []
        assert out["skipped_impl_path"] == 0

    def test_money_floor_reaches_a_shipped_shape_tool(self, vault):
        """The security-relevant half: an unreadable source suppressed the
        MONOTONIC money floor too, because the floor runs inside ``if source:``.
        A money-moving seed must never be stamped without ``money_move``."""
        body = _seed(vault, "p1", "payer", _PAYS)

        vm.backfill_effect_tags(vault, version="t1")

        assert EffectTag.MONEY_MOVE.value in _tags(body)

    def test_absolute_path_inside_impl_dir_is_accepted(self, vault):
        """Absolute paths are legal — ``execute_tool`` uses them as-is. The rule
        is containment, not relativeness."""
        body = _seed(vault, "d2", "deleter2", _DELETES,
                     declared=str(vault / "tools" / "implementations" / "deleter2.py"))

        vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == [EffectTag.LOCAL_DELETE.value]

    @pytest.mark.parametrize("declared", [None, "", "   "])
    def test_absent_or_blank_field_falls_back_to_name_py(self, vault, declared):
        """An absent field is not evidence of tampering — the documented
        ``{name}.py`` fallback (the same one the sibling uses) still applies."""
        body = _seed(vault, "d3", "deleter3", _DELETES, declared=declared)

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == [EffectTag.LOCAL_DELETE.value]
        assert out["skipped_impl_path"] == 0

    def test_real_shipped_seed_bodies_are_classified(self, tmp_path):
        """End-to-end against the ACTUAL package seed data — the shape no
        hand-written fixture had reproduced. This is the test that would have
        caught the defect on the day it shipped."""
        pkg = vm._package_vault_root()
        v = tmp_path / "vault"
        (v / "tools" / "implementations").mkdir(parents=True)
        pkg_idx = json.loads(
            (pkg / "tools" / "index.json").read_text(encoding="utf-8"))
        for e in pkg_idx:
            name, tid = e.get("name"), e.get("id")
            if not (name and tid):
                continue
            src = pkg / "tools" / "implementations" / f"{name}.py"
            if src.exists():
                shutil.copy2(src, v / "tools" / "implementations" / f"{name}.py")
            b = pkg / "tools" / f"tool_{tid}.json"
            if b.exists():
                shutil.copy2(b, v / "tools" / f"tool_{tid}.json")
        shutil.copy2(pkg / "tools" / "index.json", v / "tools" / "index.json")

        out = vm.backfill_effect_tags(v, version="t1")
        assert out["skipped_impl_path"] == 0

        by_name = {}
        for e in pkg_idx:
            bp = v / "tools" / f"tool_{e.get('id')}.json"
            if bp.exists():
                by_name[e.get("name")] = _tags(bp)

        # Shipped tools whose effects are unmistakable. Before the fix EVERY one
        # of these was [].
        assert EffectTag.SHELL_EXEC.value in by_name["run_command"]
        assert EffectTag.LOCAL_WRITE.value in by_name["file_write"]
        assert EffectTag.LOCAL_DELETE.value in by_name["file_delete"]
        assert EffectTag.NET_READ.value in by_name["fetch_html"]
        assert EffectTag.NET_READ.value in by_name["download_file"]


# ── containment: the source read must not escape implementations/ ──────────
class TestSourceOutsideImplementationsIsRefused:
    """A body's declared path is attacker-influenced (a forge picks its own
    ``implementation_path``). Classifying source from OUTSIDE the vault's
    implementations dir would let a body point the classifier at a benign file
    while executing something else."""

    def test_dotdot_escape_is_refused(self, vault):
        (vault.parent / "outside.py").write_text(_DELETES, encoding="utf-8")
        body = _seed(vault, "e1", "escaper", "def run():\n    pass\n",
                     declared="vault/tools/implementations/../../../outside.py")

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == []
        assert out["skipped_impl_path"] == 1
        assert out["errors"] == []          # a clean refusal, not a swallowed raise

    def test_absolute_path_outside_is_refused(self, vault):
        outside = vault.parent / "elsewhere.py"
        outside.write_text(_DELETES, encoding="utf-8")
        body = _seed(vault, "e2", "escaper2", "def run():\n    pass\n",
                     declared=str(outside))

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == []
        assert out["skipped_impl_path"] == 1

    def test_prefix_named_sibling_dir_is_refused(self, vault):
        """Containment must be by PATH COMPONENT. ``implementations_evil`` is a
        raw string PREFIX of ``implementations`` — this repo has already shipped
        that bug once (``C:/Radiology/x`` counted as inside ``C:/R``)."""
        evil = vault / "tools" / "implementations_evil"
        evil.mkdir(parents=True)
        (evil / "x.py").write_text(_DELETES, encoding="utf-8")
        body = _seed(vault, "e3", "escaper3", "def run():\n    pass\n",
                     declared="vault/tools/implementations_evil/x.py")

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == []
        assert out["skipped_impl_path"] == 1

    @pytest.mark.parametrize("declared", [42, ["x.py"], {"p": "x.py"}, True])
    def test_non_string_declared_path_is_refused_cleanly(self, vault, declared):
        """The backfill runs on EVERY boot. A malformed body must be a refusal,
        never an exception the outer catch-all has to eat."""
        body = _seed(vault, "m1", "malformed", _DELETES, declared=declared)

        out = vm.backfill_effect_tags(vault, version="t1")

        assert "error" not in out           # outer catch-all never fired
        assert out["errors"] == []          # per-tool except never fired
        assert out["skipped_impl_path"] == 1
        assert _tags(body) == []

    def test_redundant_but_legal_spelling_still_resolves(self, vault):
        """``.``/``..`` that land back on the REAL file are a legal spelling of
        it, not an escape — resolving BEFORE the containment test is what keeps
        this from being a false refusal."""
        body = _seed(
            vault, "r1", "redundant", _DELETES,
            declared="vault/tools/implementations/./../implementations/redundant.py")

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == [EffectTag.LOCAL_DELETE.value]
        assert out["skipped_impl_path"] == 0


# ── the fix must not disable the pass ──────────────────────────────────────
class TestBackfillStillWorks:
    """Both halves REFUSE things. A guard that refuses everything would be
    indistinguishable from the bug it replaces — every tool stamped ``[]``."""

    def test_a_missing_impl_file_is_not_counted_as_a_refusal(self, vault):
        """A body naming a file that simply is not there is a different state
        from a body pointing OUTSIDE the dir. Conflating them would hide the
        containment signal in ordinary noise."""
        body = _seed(vault, "g1", "ghost", _DELETES)
        (vault / "tools" / "implementations" / "ghost.py").unlink()

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == []
        assert out["skipped_impl_path"] == 0
        assert out["errors"] == []

    def test_declared_path_naming_the_impl_dir_itself_is_a_clean_skip(self, vault):
        """A path naming the implementations DIRECTORY passes containment — it IS
        the root. Reading it would raise into the per-tool handler, so the read is
        guarded by ``is_file()`` rather than ``exists()``: a clean skip, not a
        swallowed IsADirectoryError."""
        body = _seed(vault, "dir1", "dirnamer", _DELETES,
                     declared="vault/tools/implementations/")

        out = vm.backfill_effect_tags(vault, version="t1")

        assert _tags(body) == []
        assert out["errors"] == []              # nothing raised into the handler
        assert out["skipped_impl_path"] == 0    # contained — just not a file

    def test_pass_still_stamps_and_is_idempotent(self, vault):
        body = _seed(vault, "i1", "idem", _DELETES)

        vm.backfill_effect_tags(vault, version="t1")
        first = _tags(body)
        vm.backfill_effect_tags(vault, version="t1")

        assert first == _tags(body) == [EffectTag.LOCAL_DELETE.value]
