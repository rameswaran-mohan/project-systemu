"""Phase 4 / R-A4 -- the granted-roots store's operator surface (grant + revoke).

WHAT WAS HALF-BUILT
    `systemu/runtime/granted_roots.py` has been complete and hardened since G2:
    `grant` (:140), `revoke` (:148), `is_within_granted` (:119), `canonicalize`
    (:68). The READ path is production-wired -- `situational_inventory.build_roots`
    surveys every granted root, and `requirement_binder` re-gates resolver source #1
    through `is_within_granted`. The WRITE path was NOT: `grant` had exactly one
    caller (the replay-fixture materializer in `resolver_replay`) and `revoke` had
    ZERO. `docs/CONC-MAP.md` said so in as many words -- "(no live writer today;
    read-only)".

    So an operator whose folder had been granted had no way to take it back. The
    confinement primitive was enforcing a decision nobody could revise.

WHAT THIS SLICE PINS
    `sharing_on roots revoke <path>` is that first writer, and the test that
    matters is the END-TO-END one below: grant a root through the store API, watch
    the REAL consumer (`build_roots`) survey it, run the REAL CLI revoke through
    `CliRunner`, and watch the same consumer STOP surveying it. Store semantics
    alone (already pinned in tests/test_granted_roots.py:120) would not have
    caught a CLI that wrote to a different vault root, canonicalized the argument
    its own way, or never reached `.revoke` at all.

    The remaining tests fence the three ways this could rot into a claim:
      * the group must be REACHABLE from the installed CLI (a group defined but
        never `add_command`ed is the same inert shape this slice exists to fix);
      * the revoke CALL SITE must survive refactoring (AST pin -- verified by
        mutation: deleting the `store.revoke(...)` call turns it red);
      * the path must be canonicalized by the STORE'S OWN `canonicalize` and by
        nothing else. A second normalizer in the CLI (`abspath`, `resolve`,
        `.lower()`, ...) is the classic way a revoke silently misses: the
        operator's string canonicalizes one way for the CLI and another way for
        the store, so the grant stays and the CLI says it is gone.

B1b -- THE GRANT HALF
    The revoke half shipped first and its CONC-MAP row made a prediction: risk goes
    MED the moment a second writer exists. `roots grant` IS that second writer, so it
    arrives with the lock that keeps the prediction from coming true, and with the
    consent gate that a permission-CREATING verb needs and a revoking one does not.

    Granting is not symmetric with revoking and the tests treat it that way. A
    revoke can be wrong and costs the operator a re-grant; a grant that was never
    consciously agreed to costs them a folder's worth of file names in somebody
    else's prompt log. So: the consent copy is printed BEFORE anything is written,
    the default is NO, an unanswerable stdin refuses instead of defaulting, and a
    path that is not an existing directory is refused before consent is even asked.
"""
from __future__ import annotations

import ast
from pathlib import Path

from click.testing import CliRunner

from systemu.interface import cli_commands
from systemu.runtime.granted_roots import GrantedRootsStore, canonicalize
from systemu.runtime.situational_inventory import build_roots


class _FakeVault:
    """Same stand-in the in-repo CLI precedent uses (tests/test_ra13b2iii_cli_report.py):
    the command only needs `vault.root`, and pointing it at tmp is what makes the
    store the CLI writes and the store the survey reads THE SAME FILE."""

    def __init__(self, root):
        self.root = root


def _vault_at(monkeypatch, root):
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), _FakeVault(root)))


# --------------------------------------------------------------------------- #
# THE END-TO-END PIN -- revoke -> deny, through a real consumer
# --------------------------------------------------------------------------- #

def test_cli_revoke_stops_the_read_path_from_surveying_the_root(tmp_path, monkeypatch):
    """The point of the slice, in one test.

    Deliberately does NOT assert on the store file or on `list_roots()`: those
    would pin the writer against itself. The witness is `build_roots` -- the
    production read path (`situational_inventory._slice_builder_call`, "roots")
    that decides which of the operator's files the planner gets to see.
    """
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    (granted / "bills.pdf").write_text("x")

    # (1) grant a root via the store API into a tmp vault
    GrantedRootsStore(base_dir=vault_root).grant(str(granted))

    # (2) the READ path surveys it -- the grant is honored end to end
    before = build_roots(GrantedRootsStore(base_dir=vault_root))
    assert [s.path for s in before] == [canonicalize(str(granted))]
    assert any(fh.name == "bills.pdf" for fh in before[0].salient)

    # (3) the REAL CLI revoke
    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(cli_commands.roots_group, ["revoke", str(granted)])
    assert res.exit_code == 0, res.output

    # (4) the SAME consumer stops surveying it on the next call
    after = build_roots(GrantedRootsStore(base_dir=vault_root))
    assert after == []
    assert GrantedRootsStore(base_dir=vault_root).is_within_granted(
        str(granted / "bills.pdf")) is False


def test_the_full_consent_lifecycle_through_the_real_consumer(tmp_path, monkeypatch):
    """B1b: the same pin driven from BOTH ends by the real CLI.

    grant (consented) -> `build_roots` SURVEYS the folder -> revoke -> it stops.
    Nothing here touches the store's write methods directly: every state change
    goes through a command an operator can actually type, and every observation
    comes from the production read path.
    """
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    (granted / "bills.pdf").write_text("x")
    _vault_at(monkeypatch, vault_root)
    runner = CliRunner()

    # nothing is surveyed before the grant
    assert build_roots(GrantedRootsStore(base_dir=vault_root)) == []

    # GRANT, with a typed consent
    res = runner.invoke(cli_commands.roots_group, ["grant", str(granted)], input="y\n")
    assert res.exit_code == 0, res.output

    # the production read path picks it up
    surveyed = build_roots(GrantedRootsStore(base_dir=vault_root))
    assert [s.path for s in surveyed] == [canonicalize(str(granted))]
    assert any(fh.name == "bills.pdf" for fh in surveyed[0].salient)

    # REVOKE, and it stops
    res = runner.invoke(cli_commands.roots_group, ["revoke", str(granted)])
    assert res.exit_code == 0, res.output
    assert build_roots(GrantedRootsStore(base_dir=vault_root)) == []


# --------------------------------------------------------------------------- #
# `roots grant` -- the consent gate
# --------------------------------------------------------------------------- #

def test_grant_prints_the_consent_copy_before_it_writes_anything(tmp_path, monkeypatch):
    """The copy has to say what granting LITERALLY does, and it has to appear on a
    run that grants nothing -- otherwise it is a receipt, not a consent prompt."""
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    _vault_at(monkeypatch, vault_root)

    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", str(granted)], input="n\n")
    low = res.output.lower()
    # the two claims the operator is actually consenting to
    assert "situational inventory" in low
    assert "model provider" in low
    assert "prompt" in low
    # ...printed on a run that wrote NOTHING
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


def test_grant_refused_at_the_prompt_does_not_grant_and_exits_nonzero(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    _vault_at(monkeypatch, vault_root)

    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", str(granted)], input="n\n")
    assert res.exit_code == 1, res.output   # 1 = refused; 2 would be a usage error
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []
    assert build_roots(GrantedRootsStore(base_dir=vault_root)) == []


def test_grant_defaults_to_no_on_a_bare_enter(tmp_path, monkeypatch):
    """y/N, not Y/n. Someone holding return through a series of prompts must not
    hand over a folder by momentum."""
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    _vault_at(monkeypatch, vault_root)

    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", str(granted)], input="\n")
    assert res.exit_code == 1, res.output   # 1 = refused; 2 would be a usage error
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


def test_grant_with_an_unanswerable_stdin_refuses_and_names_the_yes_flag(tmp_path, monkeypatch):
    """A non-interactive caller gets a REFUSAL naming `--yes`, never the default.

    Empty stdin is the honest reproduction of "nobody can answer this": click's
    prompt hits EOF exactly as it does under a cron job or a CI runner. Falling
    through to the N default would be safe-by-accident; it would also tell the
    script nothing about how to proceed deliberately."""
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    _vault_at(monkeypatch, vault_root)

    res = CliRunner().invoke(cli_commands.roots_group, ["grant", str(granted)], input="")
    assert res.exit_code == 1, res.output   # 1 = refused; 2 would be a usage error
    assert "--yes" in res.output
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


def test_grant_with_the_yes_flag_grants_without_prompting(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    _vault_at(monkeypatch, vault_root)

    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", str(granted), "--yes"], input="")
    assert res.exit_code == 0, res.output
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == [canonicalize(str(granted))]
    # --yes is an assertion of consent, not a way to not be told what was agreed to
    assert "model provider" in res.output.lower()


def test_grant_refuses_a_path_that_does_not_exist(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    _vault_at(monkeypatch, vault_root)

    missing = tmp_path / "NotThere"
    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", str(missing), "--yes"], input="")
    assert res.exit_code == 1, res.output   # 1 = refused; 2 would be a usage error
    assert "does not exist" in res.output.lower()
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


def test_grant_refuses_a_file(tmp_path, monkeypatch):
    """A grant names a ROOT. Accepting a file would record a "root" the survey can
    never walk, so the grant would read as live while granting reach to nothing."""
    vault_root = tmp_path / "vault"
    f = tmp_path / "bills.pdf"
    f.write_text("x")
    _vault_at(monkeypatch, vault_root)

    res = CliRunner().invoke(cli_commands.roots_group, ["grant", str(f), "--yes"], input="")
    assert res.exit_code == 1, res.output   # 1 = refused; 2 would be a usage error
    assert "not a directory" in res.output.lower()
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


def test_regranting_says_so_and_exits_zero_without_asking_again(tmp_path, monkeypatch):
    """Consent was already given and nothing changes, so re-asking would train the
    operator to type y at a prompt that means nothing. Empty stdin here is the
    proof: if it prompted, click would hit EOF and this would be nonzero."""
    vault_root = tmp_path / "vault"
    granted = tmp_path / "Documents"
    granted.mkdir()
    _vault_at(monkeypatch, vault_root)
    runner = CliRunner()

    first = runner.invoke(cli_commands.roots_group, ["grant", str(granted), "--yes"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli_commands.roots_group, ["grant", str(granted)], input="")
    assert second.exit_code == 0, second.output
    assert "already granted" in second.output.lower()
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == [canonicalize(str(granted))]


# --------------------------------------------------------------------------- #
# `roots list`
# --------------------------------------------------------------------------- #

def test_list_renders_every_granted_root(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    a = tmp_path / "Documents"
    b = tmp_path / "Downloads"
    a.mkdir()
    b.mkdir()
    st = GrantedRootsStore(base_dir=vault_root)
    st.grant(str(a))
    st.grant(str(b))

    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(cli_commands.roots_group, ["list"])
    assert res.exit_code == 0, res.output
    assert canonicalize(str(a)) in res.output
    assert canonicalize(str(b)) in res.output


def test_list_does_not_invent_a_granted_at_the_store_never_recorded(tmp_path, monkeypatch):
    """`granted_roots.json` is `{"version": 1, "roots": [...]}` -- a bare path set.

    There is no grant timestamp to render, so the listing must SAY there is none
    rather than print a plausible-looking one (a file mtime would be the mtime of
    the LAST revoke, not of this grant)."""
    vault_root = tmp_path / "vault"
    a = tmp_path / "Documents"
    a.mkdir()
    GrantedRootsStore(base_dir=vault_root).grant(str(a))

    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(cli_commands.roots_group, ["list"])
    assert res.exit_code == 0, res.output
    assert "no grant timestamp" in res.output.lower()


def test_list_when_empty_names_how_a_root_comes_to_exist(tmp_path, monkeypatch):
    """An empty listing has to name the way OUT of it.

    Before B1b this copy said the CLI could not create a grant, which was true
    and is now false -- so it is asserted here rather than left to rot. An empty
    state that does not name its own remedy is where a half-built surface hides."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(cli_commands.roots_group, ["list"])
    assert res.exit_code == 0, res.output
    low = res.output.lower()
    assert "no folders are granted" in low
    assert "roots grant" in low
    # the stale pre-B1b claim must not survive anywhere in the copy
    assert "cannot create one" not in low


# --------------------------------------------------------------------------- #
# `roots revoke` -- honest verdicts, script-readable exit codes
# --------------------------------------------------------------------------- #

def test_revoke_reports_the_canonical_path_it_removed(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    a = tmp_path / "Documents"
    a.mkdir()
    GrantedRootsStore(base_dir=vault_root).grant(str(a))

    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(cli_commands.roots_group, ["revoke", str(a)])
    assert res.exit_code == 0, res.output
    assert "revoked" in res.output.lower()
    assert canonicalize(str(a)) in res.output


def test_revoke_of_an_ungranted_path_exits_nonzero(tmp_path, monkeypatch):
    """"Not granted" and "revoked" must be distinguishable by a script, not only
    by a human reading prose -- otherwise a revoke that silently missed (wrong
    path, wrong vault) looks exactly like a revoke that worked."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    other = tmp_path / "Elsewhere"
    other.mkdir()

    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(cli_commands.roots_group, ["revoke", str(other)])
    assert res.exit_code == 1, res.output   # 1 = refused; 2 would be a usage error
    assert "not granted" in res.output.lower()


def test_second_revoke_of_the_same_root_exits_nonzero(tmp_path, monkeypatch):
    """The store's `revoke` is idempotent-returning-False (test_granted_roots.py:127).
    The CLI must surface that difference rather than flatten both to success."""
    vault_root = tmp_path / "vault"
    a = tmp_path / "Documents"
    a.mkdir()
    GrantedRootsStore(base_dir=vault_root).grant(str(a))

    _vault_at(monkeypatch, vault_root)
    runner = CliRunner()
    first = runner.invoke(cli_commands.roots_group, ["revoke", str(a)])
    second = runner.invoke(cli_commands.roots_group, ["revoke", str(a)])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 1, second.output


def test_revoke_canonicalizes_the_operator_string_through_the_store(tmp_path, monkeypatch):
    """A `..`-shaped argument naming the same directory must revoke it.

    This is the behavioural half of the "no second normalization" fence: a raw
    string compare against the stored canonical form would report "not granted"
    here and leave the grant standing."""
    vault_root = tmp_path / "vault"
    a = tmp_path / "Documents"
    (a / "sub").mkdir(parents=True)
    GrantedRootsStore(base_dir=vault_root).grant(str(a))

    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(
        cli_commands.roots_group, ["revoke", str(a / "sub" / "..")])
    assert res.exit_code == 0, res.output
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


def test_revoke_of_a_path_inside_a_granted_root_is_not_a_revoke(tmp_path, monkeypatch):
    """Confinement is not membership. A FILE inside a granted root is
    `is_within_granted`, but it is not itself a granted root, so revoking it must
    report "not granted" and must leave the root alone -- a CLI that quietly
    revoked the enclosing root instead would take away more than was asked."""
    vault_root = tmp_path / "vault"
    a = tmp_path / "Documents"
    a.mkdir()
    inside = a / "bills.pdf"
    inside.write_text("x")
    GrantedRootsStore(base_dir=vault_root).grant(str(a))

    _vault_at(monkeypatch, vault_root)
    res = CliRunner().invoke(cli_commands.roots_group, ["revoke", str(inside)])
    assert res.exit_code == 1, res.output   # 1 = refused; 2 would be a usage error
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == [canonicalize(str(a))]


# --------------------------------------------------------------------------- #
# Reachability -- the group, and the call site inside it
# --------------------------------------------------------------------------- #

def test_the_roots_group_is_registered_on_the_installed_cli():
    """A group that is never `add_command`ed is exactly the half-built shape this
    slice exists to close: the code lands, the wiring does not, and no operator
    can reach it."""
    from sharing_on.cli import cli
    assert "roots" in cli.commands, sorted(cli.commands)
    assert set(cli.commands["roots"].commands) == {"list", "revoke", "grant"}


def test_the_roots_group_grants_only_as_explicit_operator_consent():
    """B1b replaces `test_the_roots_group_has_no_grant_verb`.

    That test pinned a TRUE claim ("there is no grant verb here") which B1b makes
    false. The claim that replaces it is the one that now needs guarding: the verb
    exists, and the docstring's promise about HOW it can fire -- explicit operator
    consent, never automatically -- is the whole reason it is safe to ship. A
    docstring is where that promise is read; a test is where it is kept."""
    assert set(cli_commands.roots_group.commands) == {"list", "revoke", "grant"}
    doc = (cli_commands.roots_group.__doc__ or "").lower()
    assert "explicit operator consent" in doc
    assert "never automatic" in doc


_CLI_PATH = Path(cli_commands.__file__)
_CLI_SRC = _CLI_PATH.read_text(encoding="utf-8")
_CLI_TREE = ast.parse(_CLI_SRC)


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(_CLI_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is not defined in {_CLI_PATH}")


def _called_names(fn: ast.FunctionDef) -> set:
    out = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            out.add(node.func.id)
    return out


def test_the_store_revoke_call_site_is_reachable_from_the_cli_command():
    """AST reachability pin. Mutation-checked: deleting the `store.revoke(...)`
    call from `roots_revoke` turns this red (and the end-to-end pin with it)."""
    assert "revoke" in _called_names(_fn("roots_revoke")), (
        "roots_revoke() no longer calls .revoke() -- the granted-roots store is "
        "back to having ZERO writers and docs/CONC-MAP.md is lying about it."
    )


# Every name that would be a SECOND, independent normalization of the operator's
# path. The store's `canonicalize` is the only authority: it resolves the FINAL
# path (realpath + 8.3 expansion + normcase), and any of these applied alongside
# it can produce a string that disagrees -- at which point the CLI reports a
# revoke the store never performed.
_FORBIDDEN_NORMALIZERS = {
    "abspath", "realpath", "normpath", "normcase", "resolve", "absolute",
    "expanduser", "expandvars", "lower", "casefold", "upper",
}


def test_the_grant_call_site_is_reachable_from_the_cli_command():
    """Same AST reachability pin as revoke's, on the second writer."""
    assert "grant" in _called_names(_fn("roots_grant")), (
        "roots_grant() no longer calls .grant() -- the consent prompt would then be "
        "theatre: the operator agrees and nothing is recorded."
    )


def test_the_roots_group_never_normalizes_the_path_a_second_time():
    for name in ("roots_revoke", "roots_grant", "roots_list", "_granted_roots_store"):
        offenders = _called_names(_fn(name)) & _FORBIDDEN_NORMALIZERS
        assert not offenders, (
            f"{name}() calls {sorted(offenders)} -- a second normalization of a path "
            f"whose only canonical form is granted_roots.canonicalize(). Two "
            f"normalizers is how a revoke reports success on a grant it never removed."
        )


def test_the_only_canonicalizer_is_the_stores_own():
    imported = {
        alias.name
        for node in ast.walk(_CLI_TREE)
        if isinstance(node, ast.ImportFrom)
        and node.module == "systemu.runtime.granted_roots"
        for alias in node.names
    }
    assert "canonicalize" in imported, (
        "the roots CLI must import canonicalize from systemu.runtime.granted_roots "
        "-- the store's own function -- and not roll its own."
    )
    assert "canonicalize" in _called_names(_fn("roots_revoke"))
    assert "canonicalize" in _called_names(_fn("roots_grant"))
