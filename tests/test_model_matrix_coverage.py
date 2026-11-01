"""DEC-12 coverage: does every production LLM call name a registered stage?

The routing pins in ``test_model_matrix_routing.py`` prove that a TAGGED call
routes per the matrix. They cannot see a call that never carried a tag: an
untagged ``llm_call_json(tier=1, ...)`` bypasses the matrix and every one of
those tests stays green. DEC-12 also rules that a new LLM-dependent stage ships
only after it has a matrix row. This module is the structural check for both.

It parses (never imports) every shipped module under ``systemu/`` and
``sharing_on/`` and reports each place an LLM call can leave the process
without a registered stage:

* ``untagged``      -- a router entry point (``llm_call``, ``llm_call_json``,
  ``async_llm_call_json``) called without ``stage=``. That covers a direct call
  and a call that hands the entry point to a runner as its first argument
  (``ctx.run(llm_call_json, ...)``, ``partial(llm_call_json, ...)``), where the
  runner's keywords are what reach the router.
* ``dynamic_stage`` -- ``stage=`` is present but is not a string literal, so no
  static check can say which row it names.
* ``escape``        -- an entry point used as a VALUE (returned, stored,
  passed in a non-first position). The eventual call happens somewhere this
  scan cannot follow.
* ``sdk_bypass``    -- a provider SDK client class constructed outside the
  router and the provider adapters: a call that never reaches the router.

A literal ``stage=`` that names an UNREGISTERED stage is always fatal and can
never be excepted. So is ``tier_beside_stage``: a call that passes ``stage=``
AND its own ``tier=``. The stage is the single source of the tier; a second,
hand-written tier beside it is the stale hard-coding the matrix replaced. The
one accepted form is a tier DERIVED from the same row,
``tier=resolve_stage_tier("<same stage>", ...)``, which cannot disagree.

``EXCEPTIONS`` is the committed baseline for the calls that predate the rule,
keyed ``(kind, path, scope)`` with a count and a reason (the DEC-32c shape).
BOTH mismatch directions fail: a new finding fails, and a finding that went
away fails until its entry is deleted. ``_CEILING`` caps the total, so growing
the set is a visible two-place edit, never a quiet one.

What this does NOT see, stated so nobody reads more into a green run: router
access through ``getattr``/``importlib`` on the module, raw HTTP to a provider
endpoint without one of the SDK client classes in ``_SDK_CLIENTS``, and the
stage an injected ``llm_fn`` is eventually called with (that case is listed as
an ``escape`` with the place the stage is applied named in its reason).

The tests that read the source tree are marked ``source_sensitive`` by hand:
the conftest auto-tagger only detects ``inspect.getsource``, and an unmarked
file-reading test breaks the EDIT-SAFE gate's promise (GATE-TIER / DEC-14).
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

from sharing_on import model_matrix as mm

_ROUTER_MODULE = "systemu.core.llm_router"
_ROUTER_FILE = "systemu/core/llm_router.py"
_ENTRY_POINTS = frozenset({"llm_call", "llm_call_json", "async_llm_call_json"})
_SDK_CLIENTS = frozenset({"OpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic"})
_SDK_MODULES = frozenset({"openai", "anthropic"})

#: Files allowed to construct an SDK client: the router and the provider
#: adapters it dispatches through.
_SDK_ALLOWED_PREFIXES = (_ROUTER_FILE, "systemu/llm/providers/")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGES = ("systemu", "sharing_on")


# --------------------------------------------------------------------------
# the scanner
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    scope: str
    line: int
    detail: str = ""


@dataclass(frozen=True)
class Tag:
    path: str
    scope: str
    line: int
    stage: str


class _Scanner(ast.NodeVisitor):
    """One module's router calls, escapes and SDK constructions.

    Name binding is collected for the whole file before any call is examined,
    and is not scope-aware: a name bound to an entry point ANYWHERE in the file
    counts everywhere in it. That over-approximates, which can only add
    findings, never hide one.
    """

    def __init__(self, path: str, tree: ast.AST, registered: Set[str]):
        self.path = path
        self.registered = registered
        self.entry_names: Dict[str, str] = {}
        self.router_modules: Set[str] = set()
        self.sdk_names: Set[str] = set()
        self.sdk_modules: Set[str] = set()
        self._collect_bindings(tree)
        self._scope: List[str] = []
        self._consumed: Set[int] = set()
        self.findings: List[Finding] = []
        self.tags: List[Tag] = []
        self.unregistered: List[Finding] = []
        self.visit(tree)

    # ---- bindings --------------------------------------------------------
    def _collect_bindings(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == _ROUTER_MODULE:
                    for a in node.names:
                        if a.name in _ENTRY_POINTS:
                            self.entry_names[a.asname or a.name] = a.name
                elif node.module == "systemu.core":
                    for a in node.names:
                        if a.name == "llm_router":
                            self.router_modules.add(a.asname or a.name)
                elif node.module in _SDK_MODULES:
                    for a in node.names:
                        if a.name in _SDK_CLIENTS:
                            self.sdk_names.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == _ROUTER_MODULE:
                        self.router_modules.add(a.asname or a.name)
                    elif a.name in _SDK_MODULES:
                        self.sdk_modules.add(a.asname or a.name)

    # ---- predicates ------------------------------------------------------
    def _is_entry_ref(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.entry_names
        if isinstance(node, ast.Attribute) and node.attr in _ENTRY_POINTS:
            return ast.unparse(node.value) in self.router_modules
        return False

    def _is_sdk_ctor(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.sdk_names
        if isinstance(node, ast.Attribute) and node.attr in _SDK_CLIENTS:
            return ast.unparse(node.value) in self.sdk_modules
        return False

    def _where(self) -> str:
        return ".".join(self._scope) or "<module>"

    # ---- scopes ----------------------------------------------------------
    def _push(self, node) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _push
    visit_AsyncFunctionDef = _push
    visit_ClassDef = _push

    # ---- calls -----------------------------------------------------------
    def _router_call(self, node: ast.Call, *, passthrough: bool = False) -> None:
        stage: Optional[ast.expr] = None
        tier: Optional[ast.expr] = None
        for kw in node.keywords:
            if kw.arg == "stage":
                stage = kw.value
            elif kw.arg == "tier":
                tier = kw.value
        # A positional tier is the router's first parameter; through a runner
        # it is the argument after the entry point.
        first = 1 if passthrough else 0
        if tier is None and len(node.args) > first:
            tier = node.args[first]
        where = self._where()
        if stage is None:
            self.findings.append(Finding("untagged", self.path, where, node.lineno))
            return
        if not (isinstance(stage, ast.Constant) and type(stage.value) is str):
            self.findings.append(Finding("dynamic_stage", self.path, where,
                                         node.lineno, ast.unparse(stage)))
            return
        if stage.value not in self.registered:
            self.unregistered.append(Finding("unregistered", self.path, where,
                                             node.lineno, stage.value))
            return
        if tier is not None and not _derived_from(tier, stage.value):
            self.unregistered.append(Finding("tier_beside_stage", self.path, where,
                                             node.lineno, ast.unparse(tier)))
        self.tags.append(Tag(self.path, where, node.lineno, stage.value))

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_entry_ref(node.func):
            self._consumed.add(id(node.func))
            self._router_call(node)
        elif node.args and self._is_entry_ref(node.args[0]):
            self._consumed.add(id(node.args[0]))
            self._router_call(node, passthrough=True)
        if self._is_sdk_ctor(node.func) and not self.path.startswith(_SDK_ALLOWED_PREFIXES):
            self.findings.append(Finding("sdk_bypass", self.path, self._where(),
                                         node.lineno, ast.unparse(node.func)))
        self.generic_visit(node)

    def _maybe_escape(self, node: ast.AST) -> None:
        if self._is_entry_ref(node) and id(node) not in self._consumed:
            self.findings.append(Finding("escape", self.path, self._where(),
                                         node.lineno, ast.unparse(node)))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._maybe_escape(node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self._maybe_escape(node)
        self.generic_visit(node)


def _derived_from(tier: ast.expr, stage: str) -> bool:
    """True when ``tier`` is ``resolve_stage_tier("<stage>", ...)``: a tier
    read off the same matrix row, which therefore cannot disagree with it."""
    if not isinstance(tier, ast.Call) or not tier.args:
        return False
    fn = tier.func
    name = fn.id if isinstance(fn, ast.Name) else (
        fn.attr if isinstance(fn, ast.Attribute) else "")
    arg = tier.args[0]
    return (name == "resolve_stage_tier" and isinstance(arg, ast.Constant)
            and arg.value == stage)


def scan_source(path: str, source: str,
                registered: Optional[Iterable[str]] = None) -> _Scanner:
    """Scan one module's source. ``path`` is its repo-relative posix path."""
    reg = set(mm.registered_stages() if registered is None else registered)
    return _Scanner(path, ast.parse(source), reg)


def _production_files() -> List[Tuple[str, Path]]:
    out = []
    for pkg in _PACKAGES:
        for p in sorted((_REPO_ROOT / pkg).rglob("*.py")):
            rel = p.relative_to(_REPO_ROOT).as_posix()
            out.append((rel, p))
    return out


def scan_tree() -> Tuple[List[Finding], List[Tag], List[Finding]]:
    """(findings, tags, unregistered) over every shipped module.

    The router module itself is skipped for router-call findings: its own
    internal ``llm_call(tier=...)`` from the JSON wrapper runs AFTER the stage
    was resolved, on the tier the stage resolved to.
    """
    findings: List[Finding] = []
    tags: List[Tag] = []
    unregistered: List[Finding] = []
    for rel, p in _production_files():
        s = scan_source(rel, p.read_text(encoding="utf-8"))
        if rel == _ROUTER_FILE:
            findings.extend(f for f in s.findings if f.kind == "sdk_bypass")
            continue
        findings.extend(s.findings)
        tags.extend(s.tags)
        unregistered.extend(s.unregistered)
    return findings, tags, unregistered


# --------------------------------------------------------------------------
# the committed exception set (shrink-only)
# --------------------------------------------------------------------------

_PACKAGE_BOUNDARY = "package boundary: sharing_on does not import systemu"

#: (kind, path, scope) -> (count, reason). DEC-32c shape: BOTH mismatch
#: directions fail. Delete an entry the moment its call is tagged.
EXCEPTIONS: Dict[Tuple[str, str, str], Tuple[int, str]] = {
    # ---- ruled exceptions --------------------------------------------------
    ("untagged", "systemu/runtime/execution_mind.py", "ExecutionMind._call_llm"): (1,
        "runtime escalation ladder: the supervisor picks the tier per call; "
        "a stage default does not apply"),
    ("sdk_bypass", "sharing_on/analyzer/generator.py", "generate_instructions"): (1,
        _PACKAGE_BOUNDARY),
    ("sdk_bypass", "sharing_on/analyzer/intent_extractor.py", "extract_intent"): (1,
        _PACKAGE_BOUNDARY),
    ("escape", "systemu/runtime/table_consult.py", "default_llm_fn"): (1,
        "Returns llm_call_json as the injectable parse hop. Its only production "
        "consumer (systemu/interface/pages/table.py) hands it to "
        "parse_area_answers, which calls it with stage='consult_parse'; that is "
        "pinned at the wire by test_consult_parse_call_site_honours_parser_tier."),
    ("untagged", "systemu/runtime/mcp/sdk/sampling.py", "_llm_call"): (1,
        "owned by a concurrent slice; tag on merge"),
}

#: The total number of excepted findings when this set was committed. It may
#: only go DOWN. Raising it is the second half of a deliberate two-place edit.
_CEILING = 5

_KINDS = frozenset({"untagged", "dynamic_stage", "escape", "sdk_bypass"})


def _fmt(findings: Iterable[Finding]) -> str:
    return "\n".join(f"  {f.kind:13s} {f.path}:{f.line}  [{f.scope}] {f.detail}"
                     for f in sorted(findings, key=lambda f: (f.path, f.line)))


@pytest.fixture(scope="module")
def tree_scan():
    return scan_tree()


# --------------------------------------------------------------------------
# the tree, against the matrix and the baseline
# --------------------------------------------------------------------------

@pytest.mark.source_sensitive
def test_scan_is_not_vacuous(tree_scan):
    """A scanner that finds nothing would pass every check below. Prove it sees
    the tagged sites the matrix already claims and a real number of calls."""
    findings, tags, _ = tree_scan
    tagged_stages = {t.stage for t in tags}
    assert {"planner", "refiner", "binder_assist", "desk_extraction"} <= tagged_stages, tags
    assert len(tags) + len(findings) >= 40, "the scan saw implausibly few LLM calls"


@pytest.mark.source_sensitive
def test_no_production_call_names_an_unregistered_stage(tree_scan):
    """Never excepted: a stage the matrix does not know raises at runtime."""
    _, _, fatal = tree_scan
    unregistered = [f for f in fatal if f.kind == "unregistered"]
    assert not unregistered, (
        "production call(s) tag a stage with no MODEL-MATRIX row; add the row "
        "to sharing_on/model_matrix.py first (DEC-12):\n" + _fmt(unregistered))


@pytest.mark.source_sensitive
def test_no_production_call_passes_its_own_tier_beside_a_stage(tree_scan):
    """Never excepted: the stage is the single source of the tier. A literal
    tier= beside it can only agree by accident and silently loses to the
    matrix when it does not."""
    _, _, fatal = tree_scan
    beside = [f for f in fatal if f.kind == "tier_beside_stage"]
    assert not beside, (
        "call(s) pass tier= beside stage=; drop the tier, or derive it with "
        "resolve_stage_tier(<same stage>, config):\n" + _fmt(beside))


@pytest.mark.source_sensitive
def test_the_fatal_kinds_are_the_only_fatal_findings(tree_scan):
    _, _, fatal = tree_scan
    assert {f.kind for f in fatal} <= {"unregistered", "tier_beside_stage"}


@pytest.mark.source_sensitive
def test_every_untagged_llm_call_is_in_the_committed_exception_set(tree_scan):
    """A NEW production call that reaches the router without a registered stage
    (or bypasses it) fails here. Tag it with stage=, adding its matrix row
    first; do not add it to EXCEPTIONS."""
    findings, _, _ = tree_scan
    new = [f for f in findings if (f.kind, f.path, f.scope) not in EXCEPTIONS]
    assert not new, (
        "LLM call(s) with no registered MODEL-MATRIX stage (DEC-12):\n"
        + _fmt(new))
    found = Counter((f.kind, f.path, f.scope) for f in findings)
    grown = {k: (n, EXCEPTIONS[k][0]) for k, n in found.items() if n > EXCEPTIONS[k][0]}
    assert not grown, (
        "an excepted scope gained untagged call(s) (found, allowed):\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(grown.items()))
        + "\n" + _fmt(f for f in findings if (f.kind, f.path, f.scope) in grown))


@pytest.mark.source_sensitive
def test_the_exception_set_has_no_stale_entries(tree_scan):
    """The other direction: an entry whose call was tagged (or removed) must be
    deleted, so the set records only what is really still untagged."""
    findings, _, _ = tree_scan
    found = Counter((f.kind, f.path, f.scope) for f in findings)
    stale = {k: (found.get(k, 0), n) for k, (n, _) in EXCEPTIONS.items()
             if found.get(k, 0) < n}
    assert not stale, (
        "EXCEPTIONS lists more than is there (found, listed); shrink it:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(stale.items())))


def test_the_exception_set_only_shrinks():
    total = sum(n for n, _ in EXCEPTIONS.values())
    assert total <= _CEILING, (
        f"EXCEPTIONS grew to {total} (ceiling {_CEILING}). The set may only "
        "shrink: tag the call with a registered stage instead.")
    for key, (n, reason) in EXCEPTIONS.items():
        assert key[0] in _KINDS, key
        assert type(n) is int and n >= 1, key
        assert type(reason) is str and len(reason.strip()) >= 20, (
            f"{key} needs a real reason")


def _passes_stage_literal(path: Path, stage: str) -> bool:
    """True when some call in ``path`` passes ``stage=<stage>`` as a literal."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(n, ast.Call) and any(
            kw.arg == "stage" and isinstance(kw.value, ast.Constant)
            and kw.value.value == stage for kw in n.keywords)
        for n in ast.walk(tree))


@pytest.mark.source_sensitive
def test_every_wired_row_is_tagged_at_its_call_site():
    """``wired=True`` is a claim; EVERY file a row lists must carry the tag.

    Looser than the router-call scan on purpose: table_consult applies its tag
    on the injected ``llm_fn(stage=...)`` call, not on a router name.
    """
    missing = []
    for stage in mm.wired_stages():
        row = mm.require_stage(stage)
        assert row.call_sites, f"{stage} is wired but lists no call site"
        for site in row.call_sites:
            path = _REPO_ROOT / site
            assert path.is_file(), f"{stage}: call site {site!r} does not exist"
            if not _passes_stage_literal(path, stage):
                missing.append((stage, site))
    assert not missing, (
        "matrix rows list call sites that pass no matching stage= literal: "
        f"{missing}")


@pytest.mark.source_sensitive
def test_every_file_tagging_a_stage_is_listed_on_its_row(tree_scan):
    """The provenance the other way: a file that tags a stage at a router call
    must be listed on that row, so the row's site list is complete."""
    _, tags, _ = tree_scan
    unlisted = sorted({(t.stage, t.path) for t in tags
                       if t.path not in mm.require_stage(t.stage).call_sites})
    assert not unlisted, f"tagged here but not listed on the row: {unlisted}"


@pytest.mark.source_sensitive
def test_every_stage_tagged_at_a_router_call_is_marked_wired(tree_scan):
    """The reverse: a stage a router call really uses must not be recorded as
    unwired, or the matrix under-reports what the operator's knob moves."""
    _, tags, _ = tree_scan
    unmarked = sorted({(t.stage, t.path) for t in tags
                       if not mm.require_stage(t.stage).wired})
    assert not unmarked, f"tagged at a router call but wired=False: {unmarked}"


# --------------------------------------------------------------------------
# the scanner itself, on synthetic sources (no source-tree read)
# --------------------------------------------------------------------------

_REG = {"planner", "desk_extraction"}


def _kinds(src: str, path: str = "systemu/x.py"):
    s = scan_source(path, src, _REG)
    return ([(f.kind, f.scope) for f in s.findings],
            [(t.stage, t.scope) for t in s.tags],
            [f.detail for f in s.unregistered])


def test_scanner_flags_a_direct_untagged_call():
    src = ("from systemu.core.llm_router import llm_call_json\n"
           "def f(c):\n    return llm_call_json(tier=1, system='s', user='u', config=c)\n")
    assert _kinds(src) == ([("untagged", "f")], [], [])


def test_scanner_accepts_a_registered_literal_stage():
    src = ("from systemu.core.llm_router import llm_call_json\n"
           "def f(c):\n    return llm_call_json(stage='planner', system='s', config=c)\n")
    assert _kinds(src) == ([], [("planner", "f")], [])


def test_scanner_follows_an_import_alias():
    src = ("from systemu.core.llm_router import llm_call_json as _llm\n"
           "def f(c):\n    return _llm(tier=1, config=c)\n")
    assert _kinds(src) == ([("untagged", "f")], [], [])


def test_scanner_follows_module_attribute_calls():
    src = ("from systemu.core import llm_router\n"
           "import systemu.core.llm_router as lr\n"
           "import systemu.core.llm_router\n"
           "async def f(c):\n"
           "    await llm_router.llm_call(1, 's', 'u', c)\n"
           "    lr.llm_call_json(stage='planner', config=c)\n"
           "    systemu.core.llm_router.async_llm_call_json(tier=2, config=c)\n")
    found, tags, _ = _kinds(src)
    assert found == [("untagged", "f"), ("untagged", "f")]
    assert tags == [("planner", "f")]


def test_scanner_reads_the_keywords_of_a_runner_pass_through():
    src = ("from systemu.core.llm_router import llm_call_json\n"
           "class R:\n"
           "    def go(self, ctx, c):\n"
           "        ctx.run(llm_call_json, tier=2, config=c)\n"
           "        ctx.run(llm_call_json, stage='planner', config=c)\n")
    found, tags, _ = _kinds(src)
    assert found == [("untagged", "R.go")]
    assert tags == [("planner", "R.go")]


def test_scanner_flags_an_unregistered_and_a_dynamic_stage():
    src = ("from systemu.core.llm_router import llm_call_json\n"
           "def f(c, s):\n"
           "    llm_call_json(stage='planer', config=c)\n"
           "    llm_call_json(stage=s, config=c)\n")
    found, tags, unreg = _kinds(src)
    assert unreg == ["planer"]
    assert found == [("dynamic_stage", "f")]
    assert tags == []


def test_scanner_flags_an_entry_point_used_as_a_value():
    src = ("from systemu.core.llm_router import llm_call_json\n"
           "def hop():\n    return llm_call_json\n"
           "FN = llm_call_json\n")
    found, _, _ = _kinds(src)
    assert found == [("escape", "hop"), ("escape", "<module>")]


def test_scanner_ignores_unrelated_names():
    src = ("def llm_call_json(**kw):\n    return kw\n"
           "def f():\n    return llm_call_json(tier=1)\n")
    assert _kinds(src) == ([], [], [])


def test_scanner_flags_sdk_clients_outside_the_router_and_providers():
    src = ("from openai import OpenAI, AsyncOpenAI as AO\n"
           "import anthropic\n"
           "def f():\n"
           "    OpenAI(); AO(); anthropic.AsyncAnthropic()\n")
    found, _, _ = _kinds(src, "sharing_on/x.py")
    assert found == [("sdk_bypass", "f")] * 3
    assert _kinds(src, "systemu/llm/providers/x.py")[0] == []
    assert _kinds(src, _ROUTER_FILE)[0] == []


def test_scanner_flags_a_tier_beside_a_stage():
    src = ("from systemu.core.llm_router import llm_call_json, llm_call\n"
           "def f(c, ctx):\n"
           "    llm_call_json(tier=1, stage='planner', config=c)\n"
           "    llm_call(3, 's', 'u', c, stage='planner')\n"
           "    ctx.run(llm_call_json, 2, stage='planner', config=c)\n")
    s = scan_source("systemu/x.py", src, _REG)
    assert [(f.kind, f.detail) for f in s.unregistered] == [
        ("tier_beside_stage", "1"), ("tier_beside_stage", "3"),
        ("tier_beside_stage", "2")]
    assert [t.stage for t in s.tags] == ["planner"] * 3


def test_scanner_accepts_a_tier_derived_from_the_same_row():
    src = ("from systemu.core.llm_router import llm_call_json\n"
           "from sharing_on.model_matrix import resolve_stage_tier\n"
           "def f(c):\n"
           "    llm_call_json(tier=resolve_stage_tier('planner', c),\n"
           "                  stage='planner', config=c)\n"
           "    llm_call_json(tier=resolve_stage_tier('desk_extraction', c),\n"
           "                  stage='planner', config=c)\n")
    s = scan_source("systemu/x.py", src, _REG)
    assert [(f.kind, f.line) for f in s.unregistered] == [("tier_beside_stage", 6)]
