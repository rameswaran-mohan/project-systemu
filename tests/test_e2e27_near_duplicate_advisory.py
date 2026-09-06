"""e2e-v0.10.27 defect: the near-duplicate advisory is wired into all five forge
paths but is, in substance, nearly SILENT.

WITNESSED against the real 41-tool seed catalog. The advisory's only detector was
``capability_slots.slots_from_name``, which derives a slot from NAME TOKENS ALONE
(first token = verb, LAST token = target):

    fetch_json            slots=read:json   advisory names fetch_json
    fetch_json_v2         slots=read:v2     advisory names NOTHING  (a version suffix defeats it)
    fetch_json_from_url   slots=read:url    advisory names NOTHING
    fetch_json_api        slots=read:api    advisory names NOTHING
    json_fetch            slots=(none)      advisory names NOTHING  (first token is not a verb)
    http_get_json         slots=(none)      advisory names NOTHING
    read_file             slots=read:file   advisory names download_file, NOT file_read

and 23 of the 41 seeded tools carry NO slot at all, so nothing could EVER be
flagged as their duplicate.

THE FIX under test: a second, deterministic name+description token-similarity
detector, UNIONED with the slot detector. No LLM, no I/O beyond the index the
slot detector already reads.

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


# --------------------------------------------------------------------------- #
# corpus fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def seed_headers():
    """The packaged 41-tool seed catalog, read READ-ONLY."""
    headers = json.loads(SEED_INDEX.read_text(encoding="utf-8"))
    assert len(headers) >= 40, "precondition: the packaged seed catalog is present"
    return headers


@pytest.fixture
def seed_vault(tmp_path, seed_headers):
    """A throwaway vault in tmp_path carrying a COPY of the seed catalog."""
    vault = Vault(str(tmp_path))
    (tmp_path / "tools" / "index.json").write_text(
        json.dumps(seed_headers), encoding="utf-8")
    rows = ci.derive_index(vault)
    assert len(rows) >= 40, (
        "precondition: the seed catalog must index, else every case below is vacuous")
    return vault


def _desc_of(seed_headers, name):
    for h in seed_headers:
        if h.get("name") == name:
            return h.get("description") or ""
    raise AssertionError("no such seed tool: " + name)


def _advisory(vault, name, description=""):
    return ci.near_duplicate_advisory(vault, name, description)


# --------------------------------------------------------------------------- #
# 1. THE WITNESSED fetch_json FAMILY - six proposals, none of them flagged today
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("proposed,description", [
    ("fetch_json_v2", "HTTP GET a JSON API endpoint and return the parsed data"),
    ("fetch_json_from_url", "HTTP GET a JSON API endpoint and return the parsed data"),
    ("fetch_json_api", "HTTP GET a JSON API endpoint and return the parsed data"),
    ("json_fetch", "HTTP GET a JSON API endpoint and return the parsed data"),
    ("http_get_json", "HTTP GET a JSON API endpoint and return the parsed data"),
    ("FetchJSON2", "HTTP GET a JSON API endpoint and return the parsed data"),
])
def test_fetch_json_family_is_named_as_a_near_duplicate(
        seed_vault, proposed, description):
    """Each of these is the SAME capability as the shipped ``fetch_json``. Today
    every one of them forges silently."""
    adv = _advisory(seed_vault, proposed, description)
    assert "fetch_json" in adv, (
        "proposal " + proposed + " forged with no mention of fetch_json; advisory="
        + repr(adv))


def test_fetch_json_family_advisory_carries_the_shared_tokens_as_a_reason(
        seed_vault):
    """The line must SAY WHY, not just name a neighbour."""
    adv = _advisory(seed_vault, "http_get_json", "get a json document over http")
    assert "shares: read, json" in adv, repr(adv)


# --------------------------------------------------------------------------- #
# 2. THE WRONG-NEIGHBOUR CASE - read_file names download_file but not file_read
# --------------------------------------------------------------------------- #

def test_read_file_names_the_real_neighbour_file_read(seed_vault):
    """``read_file`` derives slot ``read:file``, which the seed catalog's
    ``download_file`` happens to occupy - so the operator was pointed at the
    WRONG tool while ``file_read`` (slot ``create:read``) went unmentioned."""
    adv = _advisory(seed_vault, "read_file", "Read a text file and return its content")
    assert "file_read" in adv, (
        "read_file was not told about file_read; advisory=" + repr(adv))


# --------------------------------------------------------------------------- #
# 3. THE OTHER WITNESSED FAMILIES (verb synonyms + token order)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("proposed,description,expected", [
    ("save_text_file", "Write raw text content to a file at a given path",
     "write_text_file"),
    ("screenshot_take", "Capture a screenshot of the full screen",
     "take_screenshot"),
    ("capture_screenshot", "Capture a screenshot of the full screen",
     "take_screenshot"),
    ("search_web", "Search the web and return results with title and URL",
     "web_search"),
    ("json_parse", "Parse a JSON string and return the data structure",
     "parse_json"),
])
def test_verb_synonym_and_token_order_families_are_named(
        seed_vault, proposed, description, expected):
    adv = _advisory(seed_vault, proposed, description)
    assert expected in adv, (
        "proposal " + proposed + " did not name " + expected + "; advisory="
        + repr(adv))


# --------------------------------------------------------------------------- #
# 4. NEGATIVE - no false neighbours across unrelated objects
# --------------------------------------------------------------------------- #

def test_screenshot_proposal_does_not_name_parse_json(seed_vault):
    adv = _advisory(seed_vault, "take_screenshot",
                    "Capture a screenshot of the full screen or a region")
    assert "parse_json" not in adv, repr(adv)


def test_format_date_proposal_does_not_name_image_resize(seed_vault):
    adv = _advisory(seed_vault, "format_date",
                    "Parse a date string and reformat it from one format to another")
    assert "image_resize" not in adv, repr(adv)


# --------------------------------------------------------------------------- #
# 5. COVERAGE PIN over the WHOLE seed catalog
#
# This is the pin that the 23 slot-less seeded tools are now covered: for EVERY
# seeded tool N, a proposal named ``N_v2`` must yield an advisory naming N.
# Slot-only detection cannot pass this (a "_v2" suffix moves the slot target, and
# 23 of the 41 have no slot to move).
# --------------------------------------------------------------------------- #

def test_every_seed_tool_is_covered_by_its_own_v2_proposal(seed_vault, seed_headers):
    missed = []
    for header in seed_headers:
        name = header.get("name") or ""
        adv = _advisory(seed_vault, name + "_v2", header.get("description") or "")
        if name not in adv:
            missed.append(name)
    assert missed == [], (
        "these seeded tools can never be flagged as a duplicate: " + repr(missed))


def test_the_23_slotless_seed_tools_really_have_no_slot(seed_headers):
    """Grounding for the pin above: these are the seeded tools the SLOT detector
    is structurally blind to, so the coverage pin is not measuring the slot half."""
    slotless = sorted(h["name"] for h in seed_headers
                      if not cs.slots_from_name(h["name"]))
    assert len(slotless) >= 20, slotless
    assert "write_text_file" in slotless and "parse_json" in slotless


# --------------------------------------------------------------------------- #
# 6. THE UNION - the slot detector is kept, not replaced
# --------------------------------------------------------------------------- #

def test_union_keeps_the_slot_half(seed_vault):
    """``read_file`` collides on slot ``read:file`` with ``download_file``. That
    slot hit must survive the union - the new detector ADDS, it never subtracts."""
    cols = ci.slot_collisions(seed_vault, "read_file")
    assert {c["name"] for c in cols} == {"download_file"}, cols
    adv = _advisory(seed_vault, "read_file", "Read a text file")
    assert "download_file" in adv and "file_read" in adv, repr(adv)


def test_union_names_each_tool_at_most_once(seed_vault):
    """De-duplicated: a tool found by BOTH detectors is named once."""
    adv = _advisory(seed_vault, "download_file_v2", "Download a file from a URL")
    assert adv.count("download_file") == 1, repr(adv)


# --------------------------------------------------------------------------- #
# 7. BYTE-IDENTICAL FLOORS - the shipped slot-only wording must not move
# --------------------------------------------------------------------------- #

SLOT_ONLY = (
    "Heads up: this shares the create:issue capability with existing tool(s): "
    "create_issue. Consider extending one of those instead of forging a "
    "duplicate.")


def test_slot_only_advisory_is_byte_identical(tmp_path):
    """The v0.10.27 pinned wording: when the token detector finds nothing extra,
    the line is byte-for-byte what every shipped path already emits."""
    vault = Vault(str(tmp_path))
    (tmp_path / "tools" / "index.json").write_text(json.dumps([{
        "id": "tool_existing", "name": "create_issue",
        "description": "create an issue", "enabled": True, "status": "deployed",
        "tool_type": "python_function", "dependencies": [],
    }]), encoding="utf-8")
    assert ci.near_duplicate_advisory(
        vault, "open_issue", "opens an issue") == SLOT_ONLY


def test_no_candidate_at_all_is_still_the_empty_string(tmp_path):
    vault = Vault(str(tmp_path))
    assert ci.near_duplicate_advisory(vault, "open_issue", "opens an issue") == ""


def test_forge_dedup_advisory_two_arg_call_is_unchanged():
    """The shipped 2-positional-arg producer keeps its exact contract (the
    v0.10.27 all-paths spy calls it with exactly two positional args)."""
    assert ci.forge_dedup_advisory(
        "open_issue", [{"tool_id": "t1", "name": "create_issue",
                        "slots": ["create:issue"]}]) == SLOT_ONLY
    assert ci.forge_dedup_advisory("x", []) == ""


# --------------------------------------------------------------------------- #
# 8. THE DESCRIPTION IS LOAD-BEARING, not decoration
# --------------------------------------------------------------------------- #

def test_description_tokens_break_a_rank_tie(tmp_path):
    """Two existing tools tie on every name-derived signal. The description the
    forge call sites now pass through is what orders them - so ``description=``
    is not an argument that could be deleted without changing an outcome."""
    vault = Vault(str(tmp_path))
    (tmp_path / "tools" / "index.json").write_text(json.dumps([
        {"id": "t_a", "name": "grab_json", "description": "unrelated wording",
         "enabled": True, "status": "deployed", "tool_type": "python_function",
         "dependencies": []},
        {"id": "t_b", "name": "pull_json", "description": "over http from an api endpoint",
         "enabled": True, "status": "deployed", "tool_type": "python_function",
         "dependencies": []},
    ]), encoding="utf-8")
    ranked = ci.near_duplicates(vault, "fetch_json",
                                "over http from an api endpoint")
    assert [r["name"] for r in ranked] == ["pull_json", "grab_json"], ranked


def test_proposal_description_can_carry_the_match_when_the_name_cannot(tmp_path):
    """A vague NAME with an honest description still finds its neighbour - the
    reason the description is threaded through the forge call sites."""
    vault = Vault(str(tmp_path))
    (tmp_path / "tools" / "index.json").write_text(json.dumps([
        {"id": "t_a", "name": "fetch_json", "description": "HTTP GET a JSON API",
         "enabled": True, "status": "deployed", "tool_type": "python_function",
         "dependencies": []},
    ]), encoding="utf-8")
    assert "fetch_json" not in ci.near_duplicate_advisory(vault, "helper_thing", "")
    assert "fetch_json" in ci.near_duplicate_advisory(
        vault, "helper_thing", "fetch json from a url")


# --------------------------------------------------------------------------- #
# 9. THE NORMALISER's own units (the three mutation targets)
# --------------------------------------------------------------------------- #

def test_version_suffix_is_stripped():
    assert cs.similarity_tokens("fetch_json_v2") == cs.similarity_tokens("fetch_json")
    assert cs.similarity_tokens("FetchJSON2") == cs.similarity_tokens("fetch_json")
    assert cs.similarity_tokens("fetch_json_new") == cs.similarity_tokens("fetch_json")


def test_a_version_ish_word_that_is_the_real_verb_is_kept():
    """``file_copy`` uses ``copy`` as its VERB. Stripping it would collapse the
    tool to the single token ``file`` and manufacture false neighbours."""
    assert "copy" in cs.similarity_tokens("file_copy")


def test_verb_synonyms_fold_to_one_class():
    for name in ["fetch_json", "get_json", "download_json", "pull_json", "load_json"]:
        assert cs.similarity_tokens(name) == {"read", "json"}, name
    for name in ["write_text", "save_text", "store_text", "create_text"]:
        assert cs.similarity_tokens(name) == {"write", "text"}, name


def test_connectors_are_dropped():
    assert cs.similarity_tokens("fetch_json_from_url") == {"read", "json", "url"}


def test_object_tokens_are_kept_as_is():
    assert cs.similarity_tokens("write_csv_file") == {"write", "csv", "file"}
