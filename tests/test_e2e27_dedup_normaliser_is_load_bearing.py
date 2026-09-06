"""e2e-v0.10.27, mutation cover for the CAP-6b normaliser's two internal controls.

``tests/test_e2e27_near_duplicate_advisory.py`` pins the WITNESSED defect table.
This file pins the two normaliser controls that that table does NOT reach at the
advisory surface: the TRAILING VERSION-SUFFIX STRIP and the VERB-SYNONYM FOLD.
Delete either one and a case here fails on the operator-visible advisory line --
not merely on a token-set unit assertion.

WHY THIS FILE EXISTS (measured against the real 41-tool seed catalog, not assumed).
Removing each control and re-running the witnessed table was expected to redden it.
It does not:

  * With the version strip removed, ``fetch_json_v2`` STILL names ``fetch_json``.
    The candidate rule accepts a subset in EITHER direction, and version noise is
    purely ADDITIVE to the proposal, so ``{read, json}`` stays a subset of
    ``{read, json, v, 2}``. For the ``N`` / ``N_v2`` shape the strip therefore can
    never be load-bearing -- which also means the whole-catalog coverage pin
    (every seed tool vs its own ``_v2``) cannot measure it either.
  * With the synonym map emptied, ``save_text_file`` STILL names
    ``write_text_file``. Its DESCRIPTION ("Write raw text content to a file at a
    given path") supplies the ``write`` token the name's ``save`` no longer folds
    to. That is the description earning its keep, but it leaves the NAME-side fold
    unmeasured.

Both controls only bite when the EXISTING tool holds a token the proposal does not.
Then the version noise is no longer additive, no subset holds in either direction,
and the Jaccard drops under the 0.5 floor -- so the advisory goes silent. Those are
the cases below, each paired with a grounding test proving the case really is
control-dependent rather than passing for some unrelated reason.

CORPUS: the packaged seed catalog, read READ-ONLY and copied into ``tmp_path``.
Nothing here writes into the worktree's ``systemu/vault/``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import systemu
from systemu.runtime import capability_index as ci
from systemu.runtime import capability_slots as cs
from systemu.vault.vault import Vault

SEED_INDEX = Path(systemu.__file__).resolve().parent / "vault" / "tools" / "index.json"

#: Proposals that reach their neighbour ONLY because a trailing version suffix is
#: stripped: in each pair the existing tool carries an extra object token
#: (``file``, ``sheet``), so the un-stripped proposal is a subset of nothing and
#: its Jaccard falls under the floor.
VERSION_STRIP_CASES = [
    ("write_csv_v2", "write_csv_file"),
    ("read_excel_v2", "read_excel_sheet"),
]

#: Proposals that reach their neighbour ONLY because the name's verb folds to a
#: similarity class. Descriptions are deliberately EMPTY here -- the point is the
#: name-side fold, and a description would mask it exactly as it masks
#: ``save_text_file`` in the witnessed table.
SYNONYM_FOLD_CASES = [
    ("save_text", "write_text_file"),
    ("save_file", "file_write"),
    ("grab_clipboard", "clipboard_read"),
]


# --------------------------------------------------------------------------- #
# corpus fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def seed_headers():
    headers = json.loads(SEED_INDEX.read_text(encoding="utf-8"))
    assert len(headers) >= 40, "precondition: the packaged seed catalog is present"
    return headers


@pytest.fixture
def seed_vault(tmp_path, seed_headers):
    vault = Vault(str(tmp_path))
    (tmp_path / "tools" / "index.json").write_text(
        json.dumps(seed_headers), encoding="utf-8")
    rows = ci.derive_index(vault)
    assert len(rows) >= 40, (
        "precondition: the seed catalog must index, else every case below is vacuous")
    return vault


def _seed_names(seed_headers):
    return {h.get("name") for h in seed_headers}


# --------------------------------------------------------------------------- #
# 1. THE VERSION-SUFFIX STRIP, pinned at the advisory surface
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("proposed,expected", VERSION_STRIP_CASES)
def test_version_suffix_strip_is_load_bearing_at_the_advisory(
        seed_vault, proposed, expected):
    """MUTATION PIN: make ``strip_version_suffix`` a passthrough and THIS NAMED
    TEST goes red -- the advisory falls silent on a proposal that is an existing
    tool plus a revision marker."""
    adv = ci.near_duplicate_advisory(seed_vault, proposed, "")
    assert expected in adv, (
        "proposal " + proposed + " forged with no mention of " + expected
        + "; advisory=" + repr(adv))


@pytest.mark.parametrize("proposed,expected", VERSION_STRIP_CASES)
def test_the_version_strip_cases_really_depend_on_the_strip(proposed, expected):
    """GROUNDING for the pin above: with the trailing version tokens left in
    place these pairs clear NEITHER candidate bar, so the green result can only
    be coming from the strip.

    (Neither name contains a connector word, so folding the raw split is exactly
    the un-stripped token set ``similarity_tokens`` would have produced.)"""
    unstripped = {cs.canonical_similarity_token(t)
                  for t in cs.raw_similarity_tokens(proposed)}
    existing = cs.similarity_tokens(expected)
    assert not (unstripped <= existing or existing <= unstripped), (
        proposed, sorted(unstripped), sorted(existing))
    assert cs.token_jaccard(unstripped, existing) < 0.5, (
        proposed, sorted(unstripped), sorted(existing))
    # ...and WITH the strip both bars are cleared, by subset.
    assert cs.similarity_tokens(proposed) <= existing, (
        proposed, sorted(cs.similarity_tokens(proposed)), sorted(existing))


def test_the_witnessed_v2_table_cannot_measure_the_strip(seed_headers):
    """The finding this file records, as an executable statement: for the
    ``N``/``N_v2`` shape the existing tool's tokens are ALWAYS a subset of the
    un-stripped proposal's, so no ``_v2`` coverage pin can ever redden when the
    strip is removed. If this ever stops holding, the note at the top of this
    file is stale."""
    for name in _seed_names(seed_headers):
        unstripped = {cs.canonical_similarity_token(t)
                      for t in cs.raw_similarity_tokens(str(name) + "_v2")}
        assert cs.similarity_tokens(name) <= unstripped, name


# --------------------------------------------------------------------------- #
# 2. THE VERB-SYNONYM FOLD, pinned at the advisory surface
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("proposed,expected", SYNONYM_FOLD_CASES)
def test_verb_synonym_fold_is_load_bearing_at_the_advisory(
        seed_vault, proposed, expected):
    """MUTATION PIN: empty ``_SIM_VERB_CANON`` and THIS NAMED TEST goes red on
    every case -- a synonym verb in the NAME, with no description to fall back
    on, stops reaching its neighbour."""
    adv = ci.near_duplicate_advisory(seed_vault, proposed, "")
    assert expected in adv, (
        "proposal " + proposed + " forged with no mention of " + expected
        + "; advisory=" + repr(adv))


@pytest.mark.parametrize("proposed,expected", SYNONYM_FOLD_CASES)
def test_the_synonym_cases_really_depend_on_the_fold(proposed, expected):
    """GROUNDING: unfolded, each of these pairs clears neither bar."""
    unfolded = set(cs.strip_version_suffix(cs.raw_similarity_tokens(proposed)))
    existing = cs.similarity_tokens(expected)
    assert not (unfolded <= existing or existing <= unfolded), (
        proposed, sorted(unfolded), sorted(existing))
    assert cs.token_jaccard(unfolded, existing) < 0.5, (
        proposed, sorted(unfolded), sorted(existing))


def test_a_synonym_proposal_still_earns_no_unrelated_neighbours(seed_vault):
    """The fold widens matching; it must not make it indiscriminate."""
    adv = ci.near_duplicate_advisory(seed_vault, "save_text", "")
    for unrelated in ["parse_json", "format_date", "image_resize", "find_places"]:
        assert unrelated not in adv, (unrelated, adv)


# --------------------------------------------------------------------------- #
# 3. THE LIMIT - a broad proposal must not list half the catalog
# --------------------------------------------------------------------------- #

def test_a_broad_proposal_names_at_most_three_token_neighbours(seed_vault):
    """``save_file`` folds to ``{write, file}``, which is a subset of several
    seeded write-to-a-file tools. The advisory names the three best, not all of
    them -- an unbounded list is an ignored list."""
    adv = ci.near_duplicate_advisory(seed_vault, "save_file", "")
    assert "file_write" in adv, adv
    assert adv.count("(shares: ") == 3, adv
