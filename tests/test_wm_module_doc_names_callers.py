"""R-W1 — ``world_model``'s own description of its reach must be TRUE (DEC-34c).

The module docstring used to describe the store as a pure substrate with no run-loop
reader or writer. That stopped being true with slice-2a (the per-run populator) and was
plainly false by W-A's final slice (the planner view, the ``world_query`` tool, the
discovery negative-fact loop, the census). A false claim of inertness is a defect of the
DEC-34c class: it tells the next reader a control does not need auditing because nothing
reaches it.

So the docstring now NAMES its callers, and this file pins each name against the tree:
every direct caller it names really imports the module, every caller it names as
reaching the store through ``world_query`` really exists and really uses
``world_query``, and no direct importer in the package is left unnamed.
"""
from __future__ import annotations

import importlib
import pathlib
import re

from systemu.runtime import world_model as wm

_PKG_ROOT = pathlib.Path(wm.__file__).parent.parent            # systemu/
_REPO_ROOT = _PKG_ROOT.parent

#: A direct import of the world-model module, in either spelling the tree uses.
_DIRECT_IMPORT = re.compile(
    r"from systemu\.runtime\.world_model import|from systemu\.runtime import world_model\b")

#: Bullet forms the docstring uses for its caller lists.
_DIRECT_BULLET = re.compile(r"^\s*- ``(systemu/[\w/]+\.py)``", re.MULTILINE)
_VIA_BULLET = re.compile(r"^\s*- ``(\w+)\.(\w+)``", re.MULTILINE)


def _doc() -> str:
    doc = wm.__doc__ or ""
    assert doc.strip(), "world_model must carry a module docstring"
    return doc


def _section(doc: str, start: str, end: str) -> str:
    assert start in doc, f"docstring section {start!r} is missing"
    body = doc.split(start, 1)[1]
    return body.split(end, 1)[0] if end in body else body


# ── the false claim is gone ──────────────────────────────────────────────────

def test_the_docstring_no_longer_claims_the_module_is_inert():
    doc = _doc()
    for pattern in (r"\binert\b", r"\bunused\b", r"nothing in the run loop",
                    r"no run-loop module", r"PURE substrate"):
        assert not re.search(pattern, doc, re.IGNORECASE), \
            f"world_model's docstring still claims inertness: {pattern!r}"


def test_the_survey_watermark_no_longer_claims_only_operator_readers():
    """The same false-inertness shape, one level down: the watermark is read by
    ``world_query`` on every composed view and every tool answer (staleness)."""
    assert "read only by the operator" not in (wm.SurveyWatermark.__doc__ or "")
    src = pathlib.Path(wm.__file__).read_text(encoding="utf-8")
    assert "read: operator surfaces" not in src


# ── every named caller is real ───────────────────────────────────────────────

def test_the_docstring_names_at_least_one_direct_caller_and_each_really_imports_it():
    named = _DIRECT_BULLET.findall(_section(_doc(), "CALLERS.", "Reached through"))
    assert named, "the docstring must name at least one real caller"
    for rel in named:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"named caller does not exist: {rel}"
        assert _DIRECT_IMPORT.search(path.read_text(encoding="utf-8")), \
            f"named caller {rel} does not import world_model"


def test_every_direct_importer_in_the_package_is_named():
    """Completeness. A caller list that silently omits one is the same defect as the
    inertness claim it replaces — it understates the module's reach."""
    named = set(_DIRECT_BULLET.findall(_section(_doc(), "CALLERS.", "Reached through")))
    actual = set()
    for p in _PKG_ROOT.rglob("*.py"):
        if p.name == "world_model.py":
            continue
        if _DIRECT_IMPORT.search(p.read_text(encoding="utf-8", errors="replace")):
            actual.add(p.relative_to(_REPO_ROOT).as_posix())
    assert actual, "precondition: the module has direct importers"
    assert actual == named, (f"unnamed: {sorted(actual - named)}; "
                             f"named but not importing: {sorted(named - actual)}")


def test_every_caller_named_as_reaching_it_through_world_query_is_real():
    via = _VIA_BULLET.findall(_section(_doc(), "Reached through", "TRUST."))
    assert via, "the docstring must name the callers that reach it via world_query"
    for mod_name, attr in via:
        mod = None
        for prefix in ("systemu.runtime.", "systemu.runtime.tools."):
            try:
                mod = importlib.import_module(prefix + mod_name)
                break
            except ImportError:
                continue
        assert mod is not None, f"named module {mod_name!r} does not exist"
        assert callable(getattr(mod, attr, None)), f"{mod_name}.{attr} does not exist"
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "world_query" in src, f"{mod_name} does not reach the store via world_query"
