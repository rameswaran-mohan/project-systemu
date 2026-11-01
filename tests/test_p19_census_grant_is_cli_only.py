"""Ruled: a census GRANT is taken only by the CLI. Nothing reachable from
``systemu/interface/pages/`` may call it.

WHY. A grant is a standing permission to read the operator's machine, and what it finds is
sent to the model provider. The roots grant was ruled CLI-only (a terminal, the printed
consequences, a typed y/N, a refusal with no terminal), and the census grant was built the
same way on purpose. A grant button on a dashboard page is reachable by anything that can
drive the page; withdrawing is the safe direction and may be one click there.

HOW THIS IS CHECKED -- a static call graph, by AST, over the shipped tree. Every function,
class and module body under ``systemu/interface/pages/`` is an entry point. From each,
every name it references is resolved through the imports in scope (module-level AND
function-local, absolute and relative) to a top-level function or class of a shipped
module, and followed. Reaching any of these is a failure, and the message prints the path
that got there:

  * every function in ``ambient_census`` that CALLS the consent store's ``.grant(`` --
    derived from the source, and pinned to be exactly ``grant_category`` so the set
    cannot silently grow or empty. (The store itself is constructed by the read and
    withdraw paths too, so reaching the CLASS is legitimate; calling ``grant`` is not.)
  * ``cli_commands.run_census_grant`` and ``sharing_on.cli.census_grant_cmd`` -- the CLI
    grant, which no page may drive either.

WHAT IT DOES NOT FOLLOW (stated, not implied -- DEC-34). Calls on objects whose type is
not statically known (``x.method()``), ``getattr`` by string and dynamic imports. Those
holes are closed from the other side by ``tests/test_p19_census_consent_writer_set.py``:
``grant_category`` is referenced from exactly one place (``run_census_grant``), that from
exactly one place (``census_grant_cmd``), ``CensusConsentStore`` is constructed only in
``ambient_census`` (the CONC-MAP writer guard), and no writer is reached by a string or an
alias. So the only road to a grant runs through the symbols this test proves no page can
reach.

A positive control runs the same analysis over pages that DO grant, so a traversal that
silently followed nothing cannot pass.
"""
from __future__ import annotations

import ast
import functools
import pathlib
from collections import deque

REPO = pathlib.Path(__file__).resolve().parent.parent
SHIPPED_ROOTS = ("systemu", "sharing_on", "extension", "plugins")
PAGES_PREFIX = "systemu.interface.pages"
CENSUS_LIB = "systemu.runtime.ambient_census"
CLI_GRANT = {("systemu.interface.cli_commands", "run_census_grant"),
             ("sharing_on.cli", "census_grant_cmd")}
MODULE_BODY = "<module>"


# ── parsing and binding ──────────────────────────────────────────────────────

def _modname(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class _Module:
    def __init__(self, rel: str, text: str):
        self.name = _modname(rel)
        is_pkg = rel.endswith("/__init__.py")
        self.package = self.name if is_pkg else self.name.rpartition(".")[0]
        self.tree = ast.parse(text)
        self.bind = {}
        self.defs = {n.name: n for n in self.tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    def resolve_from(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = self.package.split(".") if self.package else []
        if node.level > 1:
            base = base[: len(base) - (node.level - 1)]
        return ".".join(base + ([node.module] if node.module else []))


def _bindings(mod: _Module, stmts, modules) -> dict:
    """name -> ("module", dotted) | ("symbol", module, name)."""
    out = {}
    for node in stmts:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = ("module", a.name)
                else:
                    head = a.name.split(".")[0]
                    out[head] = ("module", head)
        elif isinstance(node, ast.ImportFrom):
            base = mod.resolve_from(node)
            for a in node.names:
                full = f"{base}.{a.name}" if base else a.name
                out[a.asname or a.name] = (("module", full) if full in modules
                                           else ("symbol", base, a.name))
    return out


def _dotted(expr):
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return list(reversed(parts))
    return None


def _refs_of(mod: _Module, bodies, modules, local_imports) -> set:
    local = _bindings(mod, local_imports, modules) if local_imports else {}
    out = set()

    def bind(name):
        return local.get(name) or mod.bind.get(name) or (
            ("symbol", mod.name, name) if name in mod.defs else None)

    def add(module, name):
        target = modules.get(module)
        if target is None:
            return
        if name in target.defs:
            out.add((module, name))
        elif f"{module}.{name}" in modules:
            out.add((f"{module}.{name}", MODULE_BODY))
        else:                                    # a re-export: one hop through its import
            b = target.bind.get(name)
            if b and b[0] == "symbol" and b[1] in modules and b[2] in modules[b[1]].defs:
                out.add((b[1], b[2]))

    for body in bodies:
        for node in ast.walk(body):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                b = bind(node.id)
                if b and b[0] == "symbol":
                    add(b[1], b[2])
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                chain = _dotted(node)
                b = bind(chain[0]) if chain else None
                if not b or b[0] != "module":
                    continue
                full = [*b[1].split("."), *chain[1:]]
                for cut in range(len(full) - 1, 0, -1):
                    if ".".join(full[:cut]) in modules:
                        add(".".join(full[:cut]), full[cut])
                        break
    return out


def _nodes_of(mod: _Module, modules) -> dict:
    top = [n for n in mod.tree.body
           if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    graph = {(mod.name, MODULE_BODY): _refs_of(mod, top, modules, [])}
    for dname, dnode in mod.defs.items():
        imports = [n for n in ast.walk(dnode) if isinstance(n, (ast.Import, ast.ImportFrom))]
        graph[(mod.name, dname)] = _refs_of(mod, [dnode], modules, imports)
    return graph


@functools.lru_cache(maxsize=1)
def _shipped():
    """(modules, graph) for the shipped tree -- built once per test session."""
    modules = {}
    for root in SHIPPED_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            m = _Module(p.relative_to(REPO).as_posix(),
                        p.read_text(encoding="utf-8", errors="replace"))
            modules[m.name] = m
    for m in modules.values():
        m.bind = _bindings(m, m.tree.body, modules)
    graph = {}
    for m in modules.values():
        graph.update(_nodes_of(m, modules))
    return modules, graph


def _with_extra_page(rel: str, text: str):
    """The shipped graph plus one more page module (for the positive controls)."""
    base_modules, base_graph = _shipped()
    modules = dict(base_modules)
    extra = _Module(rel, text)
    modules[extra.name] = extra
    extra.bind = _bindings(extra, extra.tree.body, modules)
    graph = dict(base_graph)
    graph.update(_nodes_of(extra, modules))
    return modules, graph


# ── the analysis ─────────────────────────────────────────────────────────────

def grant_callers(modules) -> set:
    """Functions in the census library whose body CALLS ``<something>.grant(``."""
    lib = modules[CENSUS_LIB]
    out = set()
    for name, node in lib.defs.items():
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr == "grant":
                out.add((CENSUS_LIB, name))
    return out


def _reach(graph, entries):
    parent = {}
    q = deque()
    for e in entries:
        if e in graph and e not in parent:
            parent[e] = None
            q.append(e)
    while q:
        cur = q.popleft()
        for nxt in set(graph.get(cur, ())) | {(cur[0], MODULE_BODY)}:
            if nxt in graph and nxt not in parent:
                parent[nxt] = cur
                q.append(nxt)
    return parent


def _path(parent, node):
    out = []
    while node is not None:
        out.append(f"{node[0]}:{node[1]}")
        node = parent[node]
    return " <- ".join(out)


def _analyse(modules, graph):
    entries = [n for n in graph
               if n[0] == PAGES_PREFIX or n[0].startswith(PAGES_PREFIX + ".")]
    parent = _reach(graph, entries)
    forbidden = grant_callers(modules) | CLI_GRANT
    return entries, parent, {n: _path(parent, n) for n in forbidden if n in parent}


# ── the pins ─────────────────────────────────────────────────────────────────

def test_nothing_reachable_from_the_pages_can_grant_census_consent():
    _entries, _parent, hits = _analyse(*_shipped())
    assert not hits, (
        "\nA dashboard page can reach the census GRANT. Grant is CLI-only (a terminal, the "
        "full card, a typed y/N); a page may withdraw, pause or resume, never grant.\n"
        + "\n".join(f"  {k[0]}.{k[1]}:\n    {v}" for k, v in sorted(hits.items())))


def test_the_grant_callers_are_exactly_the_library_grant():
    """Anti-drift for the derived target set: if it emptied, the pin above would pass
    vacuously; if it grew, a second grant path exists and needs a ruling."""
    modules, _graph = _shipped()
    assert grant_callers(modules) == {(CENSUS_LIB, "grant_category")}


def test_the_traversal_really_follows_imports_out_of_the_pages():
    """Anti-vacuity: the walk must reach what the pages genuinely call -- including the
    function-local import in the panel's control region -- or 'reached nothing
    forbidden' means nothing."""
    entries, parent, _hits = _analyse(*_shipped())
    assert len(entries) > 50
    for must in ((CENSUS_LIB, "census_overview"),
                 (CENSUS_LIB, "revoke_category"),
                 (CENSUS_LIB, "pause_category"),
                 ("systemu.runtime.table_store", "add_tombstone")):
        assert must in parent, f"the traversal never reached {must}"


def test_a_page_with_a_grant_handler_is_caught():
    """Positive control: a page that grants -- directly, through a module alias, or by
    driving the CLI grant through a relative import -- must be reported."""
    for body in (
        "def _grant(vault, cat):\n"
        "    from systemu.runtime.ambient_census import grant_category\n"
        "    return grant_category(vault, cat)\n",
        "from systemu.runtime import ambient_census as _ac\n"
        "def _grant(vault, cat):\n"
        "    return _ac.grant_category(vault, cat)\n",
        "from ..cli_commands import run_census_grant\n"
        "def _grant(vault, cat):\n"
        "    return run_census_grant(vault, cat, assume_yes=True)\n",
    ):
        _e, _p, hits = _analyse(*_with_extra_page(
            "systemu/interface/pages/zz_grant_probe.py", body))
        assert hits, f"the analysis missed a page that grants:\n{body}"


def test_no_page_spawns_the_cli_grant():
    """The string road: a page running `systemu census grant ...` in a subprocess. The
    panel SHOWS that command as text, so only process-spawning calls are checked."""
    spawners = {"run", "Popen", "call", "check_call", "check_output", "system",
                "create_subprocess_exec", "create_subprocess_shell", "startfile"}
    modules, _graph = _shipped()
    hits = []
    for name, mod in modules.items():
        if not (name == PAGES_PREFIX or name.startswith(PAGES_PREFIX + ".")):
            continue
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname in spawners:
                blob = ast.unparse(node)
                if "census" in blob and "grant" in blob:
                    hits.append((name, node.lineno, blob[:120]))
    assert hits == [], hits
