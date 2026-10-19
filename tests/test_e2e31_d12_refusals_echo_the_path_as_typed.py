"""D-12 -- a `roots` refusal echoes the path the operator actually typed.

THE WITNESS (0.10.31 human-style e2e)
-------------------------------------
`systemu roots grant <a path that does not exist>` answered::

    Refused: that path does not exist: c:\\users\\...\\documents\\notthere

-- all lower case. That is not the string the operator typed, and it is not a
string they can recognise: `canonicalize` case-folds through `os.path.normcase`
because CONFINEMENT needs a comparable form, and the refusal was echoing the
comparison key instead of the input.

Why it matters on a refusal specifically. "That path does not exist" is a claim
ABOUT WHAT THE OPERATOR SAID. The one thing that makes such a claim checkable
is seeing your own words come back: a typo is visible in `Documnets`, and
invisible once the whole string has been rewritten into a form nothing on the
screen or in the shell history matches. Case-folding also erases the one clue
that would explain a REAL failure -- a path pasted from somewhere with a
different case is a different path on a case-sensitive mount.

THE RULING
    Refusals echo the path AS TYPED. The canonical form is used only for the
    store -- and `roots list` may keep saying it canonicalised, because there
    the canonical form IS the fact being reported.

NOT CHANGED, deliberately: `Granted:`, `Already granted:` and `Revoked:` still
name the canonical path. Those report what the STORE now holds, which is the
canonical string and nothing else; echoing the typed form there would be the
mirror-image defect -- a claim about the store that does not match the store.

NO SECOND NORMALIZER. Nothing here derives a display form. The typed string is
printed exactly as click received it, and `canonicalize` remains the only
authority on what the path IS -- the P4 fence
(`test_the_roots_group_never_normalizes_the_path_a_second_time`) still holds.
"""
from __future__ import annotations

from click.testing import CliRunner

from systemu.interface import cli_commands
from systemu.interface import consent_prompt
from systemu.runtime.granted_roots import GrantedRootsStore, canonicalize


def _vault_at(monkeypatch, vault_root):
    """Point the CLI's store at a vault under tmp_path, exactly as the P4 CLI
    tests do. Nothing is written into the worktree's own vault."""
    monkeypatch.setattr(
        cli_commands, "_granted_roots_store",
        lambda ctx: GrantedRootsStore(base_dir=str(vault_root)))


def _typed(tmp_path, *parts) -> str:
    """A path in a shape whose canonical form differs on EVERY platform.

    Mixed case is what the witness carried, and on Windows `normcase` is what
    destroys it. On a case-sensitive filesystem mixed case survives
    canonicalization untouched, which would make this fixture vacuous there --
    so the path also travels through a `..` segment, which `realpath` resolves
    everywhere. Both halves are the operator's own string; neither is exotic.
    """
    return str(tmp_path.joinpath("Sub", "..", *parts))


def _premise(typed: str) -> None:
    assert canonicalize(typed) != typed, (
        "fixture premise: the canonical form must differ from the typed form, "
        "or this test cannot tell them apart")


# --------------------------------------------------------------------------- #
# `roots grant` -- the two refusals that come BEFORE consent is mentioned
# --------------------------------------------------------------------------- #

def test_a_missing_path_is_echoed_as_typed(tmp_path, monkeypatch):
    """THE REPRO."""
    vault_root = tmp_path / "vault"
    _vault_at(monkeypatch, vault_root)
    (tmp_path / "Sub").mkdir()
    typed = _typed(tmp_path, "Documents", "NotThere")
    _premise(typed)

    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", typed, "--yes"], input="")

    assert res.exit_code == 1, res.output
    assert "does not exist" in res.output.lower(), res.output
    assert typed in res.output, (
        "the refusal echoed a rewritten path instead of the operator's own:\n"
        + res.output)
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


def test_a_file_is_echoed_as_typed(tmp_path, monkeypatch):
    """A grant names a folder. The refusal still has to say which string it is
    talking about."""
    vault_root = tmp_path / "vault"
    _vault_at(monkeypatch, vault_root)
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Bills.pdf").write_text("x", encoding="utf-8")
    typed = _typed(tmp_path, "Bills.pdf")
    _premise(typed)

    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", typed, "--yes"], input="")

    assert res.exit_code == 1, res.output
    assert "not a directory" in res.output.lower(), res.output
    assert typed in res.output, res.output


def test_the_no_terminal_refusal_re_runs_with_what_was_typed(tmp_path,
                                                             monkeypatch):
    """The refusal that names a command to re-run. A path rewritten inside that
    command is a command the operator cannot recognise as their own."""
    vault_root = tmp_path / "vault"
    _vault_at(monkeypatch, vault_root)
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Documents").mkdir()
    typed = _typed(tmp_path, "Documents")
    _premise(typed)
    monkeypatch.setattr(consent_prompt, "ask_for_consent",
                        lambda *a, **k: consent_prompt.CONSENT_NO_TERMINAL)

    res = CliRunner().invoke(cli_commands.roots_group, ["grant", typed])

    assert res.exit_code == consent_prompt.CONSENT_NO_TERMINAL, res.output
    assert "no terminal" in res.output.lower(), res.output
    assert typed in res.output, res.output
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []


# --------------------------------------------------------------------------- #
# `roots revoke` -- the same rule on the other verb
# --------------------------------------------------------------------------- #

def test_an_ungranted_path_is_echoed_as_typed(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _vault_at(monkeypatch, vault_root)
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Elsewhere").mkdir()
    typed = _typed(tmp_path, "Elsewhere")
    _premise(typed)

    res = CliRunner().invoke(cli_commands.roots_group, ["revoke", typed])

    assert res.exit_code == 1, res.output
    assert "not granted" in res.output.lower(), res.output
    assert typed in res.output, res.output


# --------------------------------------------------------------------------- #
# and the STORE is untouched: canonical in, canonical reported
# --------------------------------------------------------------------------- #

def test_a_grant_still_records_and_reports_the_canonical_path(tmp_path,
                                                              monkeypatch):
    """The mirror-image defect. `Granted:` reports what the store now holds,
    and the store holds exactly one string: the canonical one."""
    vault_root = tmp_path / "vault"
    _vault_at(monkeypatch, vault_root)
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Documents").mkdir()
    typed = _typed(tmp_path, "Documents")
    _premise(typed)

    res = CliRunner().invoke(
        cli_commands.roots_group, ["grant", typed, "--yes"], input="")

    assert res.exit_code == 0, res.output
    assert canonicalize(typed) in res.output, res.output
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == [
        canonicalize(typed)]


def test_a_revoke_still_reports_the_canonical_path_it_removed(tmp_path,
                                                              monkeypatch):
    vault_root = tmp_path / "vault"
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Documents").mkdir()
    typed = _typed(tmp_path, "Documents")
    _premise(typed)
    GrantedRootsStore(base_dir=vault_root).grant(typed)
    _vault_at(monkeypatch, vault_root)

    res = CliRunner().invoke(cli_commands.roots_group, ["revoke", typed])

    assert res.exit_code == 0, res.output
    assert canonicalize(typed) in res.output, res.output
    assert GrantedRootsStore(base_dir=vault_root).list_roots() == []
