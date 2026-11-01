"""P19 / R-W2 -- WHO CAN WRITE CENSUS CONSENT, pinned as an exact set (AST).

The census reads the operator's own machine, and a grant is a STANDING permission whose
findings are sent to the model provider. So "grant is an operator action only" is the
property that matters, and it is checked here on the SOURCE, as a set of
(file, enclosing function, symbol) references -- not as a text grep, and not as an
allowlist that tolerates additions.

THE SYMBOLS. Everything that can reach ``CensusConsentStore._write`` (the consent file's
sole writer) or ``_bump_consent_epoch`` (the generation anchor's):

  * the library verbs ``grant_category`` / ``revoke_category`` / ``pause_category``;
  * ``run_census`` -- it calls ``mark_ran``, which REWRITES the file but can only stamp a
    row that already exists (``test_mark_ran_never_creates_a_grant``); so it is a writer
    that can never create consent, and is pinned here so a second caller is a decision;
  * ``CensusConsentStore`` itself (a mutable handle) and ``_bump_consent_epoch``;
  * the CLI entry points that wrap the verbs: ``run_census_grant`` / ``run_census_revoke``
    / ``run_census_pause`` / ``run_census_resume`` / ``_census_set_paused``.

THE PROOF that every writer is an operator surface (dashboard page, CLI, or env):

  * every reference outside the two census modules is in this exact set;
  * every entry point of that set is a ``systemu census ...`` click command (asserted on
    its decorator), i.e. typed by the operator in a terminal -- or is ``run_census`` on the
    survey seam, which cannot create a grant;
  * no environment variable switches the census on;
  * nothing reaches a writer by a STRING (``getattr(m, "grant_category")``,
    ``import_module("...ambient_census")``), and no writer is imported under an alias,
    both of which would walk around a name-based pin.

No registered tool, MCP path, elicitation handler or chat command appears in the set, so
no model-driven path in systemu's own code can write consent. (Generic code execution --
an approved shell command, a reviewed forged tool -- runs as the operator's own user and is
outside what a source pin can bound; see the slice report.)

Changing the set is a consent decision, not a refactor: add the new reference here with
the operator surface it belongs to, or do not add it.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
SHIPPED_ROOTS = ("systemu", "sharing_on", "extension", "plugins")
DEFINING = {"systemu/runtime/ambient_census.py", "systemu/runtime/census_consent.py"}

WRITER_SYMBOLS = frozenset({
    "grant_category", "revoke_category", "pause_category", "run_census",
    "CensusConsentStore", "_bump_consent_epoch",
    "run_census_grant", "run_census_revoke", "run_census_pause", "run_census_resume",
    "_census_set_paused",
})

#: (file, enclosing function, symbol) -> the operator surface it belongs to.
EXPECTED = {
    ("sharing_on/cli.py", "census_grant_cmd", "run_census_grant"):
        "CLI: systemu census grant",
    ("sharing_on/cli.py", "census_revoke_cmd", "run_census_revoke"):
        "CLI: systemu census revoke",
    ("sharing_on/cli.py", "census_pause_cmd", "run_census_pause"):
        "CLI: systemu census pause",
    ("sharing_on/cli.py", "census_resume_cmd", "run_census_resume"):
        "CLI: systemu census resume",
    ("systemu/interface/cli_commands.py", "run_census_grant", "grant_category"):
        "CLI implementation of census grant",
    ("systemu/interface/cli_commands.py", "run_census_revoke", "revoke_category"):
        "CLI implementation of census revoke",
    ("systemu/interface/cli_commands.py", "_census_set_paused", "pause_category"):
        "CLI implementation of census pause/resume",
    ("systemu/interface/cli_commands.py", "run_census_pause", "_census_set_paused"):
        "CLI implementation of census pause",
    ("systemu/interface/cli_commands.py", "run_census_resume", "_census_set_paused"):
        "CLI implementation of census resume",
    ("systemu/interface/pages/table_census.py", "_census_panel_controls._withdraw",
     "revoke_category"): "dashboard: the /table census panel's Withdraw dialog",
    ("systemu/interface/pages/table_census.py", "_census_panel_controls._pause_or_resume",
     "pause_category"): "dashboard: the /table census panel's Pause / Resume",
    ("systemu/runtime/shadow_runtime.py", "ShadowRuntime.execute", "run_census"):
        "the survey seam -- stamps last_ran_at on an EXISTING grant, never creates one",
}

#: The click commands that are the operator's entry points, by file.
CLICK_ENTRY_POINTS = {"census_grant_cmd", "census_revoke_cmd", "census_pause_cmd",
                      "census_resume_cmd"}


def _shipped_files():
    for root in SHIPPED_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(REPO).as_posix()
            yield rel, p.read_text(encoding="utf-8", errors="replace")


class _Refs(ast.NodeVisitor):
    def __init__(self):
        self.stack = []
        self.refs = set()
        self.aliases = []
        self.string_access = []

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _where(self):
        return ".".join(self.stack) or "<module>"

    def visit_Name(self, node):
        if node.id in WRITER_SYMBOLS and isinstance(node.ctx, ast.Load):
            self.refs.add((self._where(), node.id))

    def visit_Attribute(self, node):
        if node.attr in WRITER_SYMBOLS and isinstance(node.ctx, ast.Load):
            self.refs.add((self._where(), node.attr))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name in WRITER_SYMBOLS and a.asname and a.asname != a.name:
                self.aliases.append((self._where(), a.name, a.asname))

    def visit_Call(self, node):
        for arg in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                v = arg.value
                if v in WRITER_SYMBOLS or v.endswith(("ambient_census", "census_consent")):
                    self.string_access.append((self._where(), v))
        self.generic_visit(node)


def _scan():
    refs, aliases, strings = set(), [], []
    for rel, text in _shipped_files():
        if rel in DEFINING:
            continue
        v = _Refs()
        v.visit(ast.parse(text))
        refs |= {(rel, where, sym) for where, sym in v.refs}
        aliases += [(rel,) + a for a in v.aliases]
        strings += [(rel,) + s for s in v.string_access]
    return refs, aliases, strings


def test_the_scan_sees_the_shipped_tree():
    """Anti-vacuity: a scan that walked nothing would find an empty set and, below, a
    missing-reference failure -- but pin the size so a broken walk is named as one."""
    files = list(_shipped_files())
    assert len(files) > 200
    assert any(rel == "sharing_on/cli.py" for rel, _ in files)


def test_every_consent_writer_reference_is_a_declared_operator_surface():
    refs, _aliases, _strings = _scan()
    unexpected = sorted(refs - set(EXPECTED))
    missing = sorted(set(EXPECTED) - refs)
    assert not unexpected and not missing, (
        "\nTHE CENSUS CONSENT WRITER SET CHANGED.\n"
        f"  new references (not an operator surface on record): {unexpected}\n"
        f"  declared references that disappeared: {missing}\n"
        "A new writer of census consent is a consent decision: name the operator surface "
        "it belongs to in EXPECTED, or remove it. A model-reachable writer must not be "
        "added at all.")


def test_every_entry_point_is_a_census_click_command():
    """The set's roots in sharing_on/cli.py are operator-typed terminal commands."""
    tree = ast.parse((REPO / "sharing_on" / "cli.py").read_text(encoding="utf-8"))
    decorated = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in CLICK_ENTRY_POINTS:
            decos = [ast.unparse(d) for d in node.decorator_list]
            decorated[node.name] = decos
    assert set(decorated) == CLICK_ENTRY_POINTS
    for name, decos in decorated.items():
        assert any(d.startswith("census_group.command(") for d in decos), (name, decos)


def test_no_writer_is_reached_by_a_string_or_an_alias():
    _refs, aliases, strings = _scan()
    assert aliases == [], f"a consent writer is imported under another name: {aliases}"
    assert strings == [], f"a consent writer is reached by a string: {strings}"


def test_no_environment_variable_switches_the_census_on():
    """'env' is the third legitimate operator surface; the census has none, and gaining
    one would be a new writer this pin has to see."""
    hits = []
    for rel, text in _shipped_files():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.startswith("SYSTEMU_") and "CENSUS" in node.value:
                hits.append((rel, node.value))
    assert hits == [], hits


def test_the_survey_seam_writer_cannot_create_consent(tmp_path):
    """The one non-operator entry in the set, witnessed rather than asserted: the write it
    reaches (``mark_ran``) stamps an existing grant and refuses to create one."""
    from types import SimpleNamespace

    from systemu.runtime import ambient_census as ac
    from systemu.runtime import census_consent as cc

    store = cc.CensusConsentStore(tmp_path)
    assert store.mark_ran("cloud_sync_roots") is False
    assert not (tmp_path / "census_consent.json").exists()
    summary = ac.run_census(SimpleNamespace(root=tmp_path))
    assert summary["scanned"] == []
    assert store.list_grants() == []
