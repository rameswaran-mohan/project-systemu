"""2a - "This week: +N tools" on Home's "Your Systemu grows" card.

The odometer says how much capability the install HOLDS; the delta says how
much it GAINED. That needs one durable thing the odometer does not: a weekly
snapshot of `growth_counts` persisted as a single JSON sidecar under the vault
root (`<root>/growth_snapshot.json`).

Design locks pinned here:

  * PURE HELPERS - `snapshot_due` / `compute_delta` / `delta_line` are plain
    functions over plain data, so every rule below is tested with no NiceGUI
    runtime and no clock monkeypatching (`now` is an argument, never a global).
  * HONEST SILENCE - the line appears ONLY when a prior snapshot exists AND at
    least one counter went UP. No prior, all-zero, or a shrunken install all
    render nothing rather than a "+0" or a fabricated week.
  * A NEW DURABLE WRITER - atomic (tempfile + os.replace) so a crash mid-write
    cannot tear the file, and defensively read (a corrupt file is "no snapshot",
    never an exception on the Home shell). Registered in docs/CONC-MAP.md and in
    tests/test_conc_map_writer_ownership.py in the same commit.
  * NOT A FACT AND NOT A TABLE ITEM - this writes its own sidecar and nothing
    else; the OnTheTable reconciler keeps its sole-writer invariant.

The last block are WIRING pins: delete the production call site in
`console._build_growth_card` and a NAMED test here goes red.
"""
from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from systemu.interface import growth_snapshot as gs


_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


class _Vault:
    """The whole surface this module uses: a vault root on disk."""

    def __init__(self, root):
        self.root = str(root)


def _snap(counts, ts):
    return {"counts": dict(counts), "iso_ts": ts}


def _aged(days, counts=None):
    """A stored snapshot written `days` ago relative to _NOW."""
    return _snap(counts or {"tools": 1, "shadows": 1, "skills": 1},
                 (_NOW - timedelta(days=days)).isoformat())


# ---------------------------------------------------------------------------
#  snapshot_due - when the weekly stamp rolls over
# ---------------------------------------------------------------------------


class TestSnapshotDue:
    def test_no_snapshot_at_all_is_due(self):
        assert gs.snapshot_due(None, _NOW) is True

    def test_a_fresh_snapshot_is_not_due(self):
        assert gs.snapshot_due(_aged(0), _NOW) is False

    def test_six_days_is_not_due_but_seven_is(self):
        assert gs.snapshot_due(_aged(6), _NOW) is False
        assert gs.snapshot_due(_aged(7), _NOW) is True
        assert gs.snapshot_due(_aged(30), _NOW) is True

    def test_the_interval_is_a_week(self):
        assert gs.SNAPSHOT_INTERVAL_DAYS == 7

    def test_an_unreadable_timestamp_is_due(self):
        for bad in ("", "   ", "not-a-date", "2026-13-45T99:99", None, 17, []):
            assert gs.snapshot_due(_snap({}, bad), _NOW) is True, repr(bad)

    def test_a_missing_timestamp_is_due(self):
        assert gs.snapshot_due({"counts": {"tools": 1}}, _NOW) is True

    def test_a_naive_timestamp_is_due(self):
        """Stamps are written aware-UTC. A naive one is not comparable, so it is
        replaced rather than guessed at."""
        naive = (_NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
        assert gs.snapshot_due(_snap({}, naive), _NOW) is True

    def test_a_future_timestamp_is_due(self):
        """A clock that moved backwards must not freeze the sidecar until the
        stored future arrives."""
        assert gs.snapshot_due(_aged(-3), _NOW) is True

    def test_a_snapshot_that_is_not_a_dict_is_due(self):
        for bad in ([], "snapshot", 3, _NOW.isoformat()):
            assert gs.snapshot_due(bad, _NOW) is True, repr(bad)


# ---------------------------------------------------------------------------
#  compute_delta - current minus prior, per counter
# ---------------------------------------------------------------------------


class TestComputeDelta:
    def test_delta_is_current_minus_prior(self):
        prior = {"tools": 2, "shadows": 1, "skills": 0}
        now = {"tools": 5, "shadows": 2, "skills": 3}
        assert gs.compute_delta(prior, now) == {"tools": 3, "shadows": 1, "skills": 3}

    def test_no_prior_means_no_delta(self):
        assert gs.compute_delta(None, {"tools": 9, "shadows": 9, "skills": 9}) == \
            {"tools": 0, "shadows": 0, "skills": 0}

    def test_a_missing_counter_reads_as_zero(self):
        assert gs.compute_delta({"tools": 1}, {"skills": 2}) == \
            {"tools": -1, "shadows": 0, "skills": 2}

    def test_a_non_int_counter_reads_as_zero(self):
        """Values come off disk, so a string/None/float is possible; each is
        read as 0 rather than crashing or being coerced into a lie."""
        prior = {"tools": "2", "shadows": None, "skills": 1.5}
        assert gs.compute_delta(prior, {"tools": 3, "shadows": 1, "skills": 1}) == \
            {"tools": 3, "shadows": 1, "skills": 1}

    def test_a_bool_is_not_a_count(self):
        assert gs.compute_delta({"tools": True}, {"tools": True}) == \
            {"tools": 0, "shadows": 0, "skills": 0}

    def test_a_shrunken_install_reports_a_negative(self):
        assert gs.compute_delta({"tools": 5}, {"tools": 2})["tools"] == -3

    def test_the_keys_are_exactly_the_three_counters(self):
        assert set(gs.compute_delta({}, {})) == {"tools", "shadows", "skills"}


# ---------------------------------------------------------------------------
#  delta_line - the one operator-visible sentence
# ---------------------------------------------------------------------------


class TestDeltaLine:
    def test_all_three_counters_render_in_order(self):
        line = gs.delta_line({"tools": 2, "shadows": 3, "skills": 4})
        assert line == "This week: +2 tools, +3 shadows, +4 skills"

    def test_zero_terms_are_omitted(self):
        assert gs.delta_line({"tools": 2, "shadows": 0, "skills": 4}) == \
            "This week: +2 tools, +4 skills"

    def test_all_zero_is_honest_silence(self):
        assert gs.delta_line({"tools": 0, "shadows": 0, "skills": 0}) == ""

    def test_an_empty_delta_is_honest_silence(self):
        assert gs.delta_line({}) == ""
        assert gs.delta_line(None) == ""

    def test_negative_terms_never_render(self):
        assert gs.delta_line({"tools": -3, "shadows": 1, "skills": -1}) == \
            "This week: +1 shadow"

    def test_only_negatives_is_silence_not_a_minus_sign(self):
        assert gs.delta_line({"tools": -3, "shadows": -1, "skills": -1}) == ""

    def test_one_of_something_is_singular(self):
        assert gs.delta_line({"tools": 1, "shadows": 1, "skills": 1}) == \
            "This week: +1 tool, +1 shadow, +1 skill"

    def test_the_line_is_ascii(self):
        assert gs.delta_line({"tools": 2, "shadows": 3, "skills": 4}).isascii()


# ---------------------------------------------------------------------------
#  The sidecar itself - one JSON file, atomic, defensively read
# ---------------------------------------------------------------------------


class TestTheSidecar:
    def test_the_path_is_one_json_file_at_the_vault_root(self, tmp_path):
        v = _Vault(tmp_path)
        assert gs.snapshot_path(v) == Path(tmp_path) / "growth_snapshot.json"
        assert gs.SNAPSHOT_FILENAME == "growth_snapshot.json"

    def test_the_first_render_stores_a_snapshot_and_says_nothing(self, tmp_path):
        v = _Vault(tmp_path)
        assert gs.growth_delta_line(v, {"tools": 3, "shadows": 1, "skills": 0},
                                    now=_NOW) == ""
        stored = json.loads((Path(tmp_path) / "growth_snapshot.json")
                            .read_text(encoding="utf-8"))
        assert stored["counts"] == {"tools": 3, "shadows": 1, "skills": 0}
        assert stored["iso_ts"] == _NOW.isoformat()

    def test_the_stored_timestamp_is_aware_utc(self, tmp_path):
        v = _Vault(tmp_path)
        gs.growth_delta_line(v, {"tools": 1, "shadows": 0, "skills": 0})
        stored = gs.load_snapshot(v)
        parsed = datetime.fromisoformat(stored["iso_ts"])
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)

    def test_a_later_render_reports_the_delta_without_rewriting(self, tmp_path):
        v = _Vault(tmp_path)
        gs.growth_delta_line(v, {"tools": 3, "shadows": 1, "skills": 0}, now=_NOW)

        day2 = _NOW + timedelta(days=2)
        assert gs.growth_delta_line(v, {"tools": 5, "shadows": 1, "skills": 2},
                                    now=day2) == "This week: +2 tools, +2 skills"
        # the stored week is untouched: the delta accumulates until roll-over
        assert gs.load_snapshot(v)["iso_ts"] == _NOW.isoformat()
        assert gs.load_snapshot(v)["counts"]["tools"] == 3

    def test_roll_over_reports_the_prior_delta_before_overwriting(self, tmp_path):
        v = _Vault(tmp_path)
        gs.growth_delta_line(v, {"tools": 3, "shadows": 1, "skills": 0}, now=_NOW)

        week2 = _NOW + timedelta(days=8)
        assert gs.growth_delta_line(v, {"tools": 4, "shadows": 1, "skills": 0},
                                    now=week2) == "This week: +1 tool"
        assert gs.load_snapshot(v)["counts"]["tools"] == 4
        assert gs.load_snapshot(v)["iso_ts"] == week2.isoformat()

    def test_the_week_after_a_roll_over_starts_from_zero(self, tmp_path):
        v = _Vault(tmp_path)
        gs.growth_delta_line(v, {"tools": 3, "shadows": 1, "skills": 0}, now=_NOW)
        week2 = _NOW + timedelta(days=8)
        gs.growth_delta_line(v, {"tools": 4, "shadows": 1, "skills": 0}, now=week2)
        assert gs.growth_delta_line(v, {"tools": 4, "shadows": 1, "skills": 0},
                                    now=week2 + timedelta(days=1)) == ""

    def test_a_corrupt_file_is_no_snapshot_and_gets_replaced(self, tmp_path):
        path = Path(tmp_path) / "growth_snapshot.json"
        path.write_text("{not json at all", encoding="utf-8")
        v = _Vault(tmp_path)

        assert gs.load_snapshot(v) is None
        assert gs.growth_delta_line(v, {"tools": 2, "shadows": 0, "skills": 0},
                                    now=_NOW) == ""
        assert gs.load_snapshot(v)["counts"]["tools"] == 2

    def test_a_snapshot_of_the_wrong_shape_is_no_snapshot(self, tmp_path):
        path = Path(tmp_path) / "growth_snapshot.json"
        for junk in ("[1, 2, 3]", '"a string"', "null", '{"counts": 7}'):
            path.write_text(junk, encoding="utf-8")
            assert gs.load_snapshot(_Vault(tmp_path)) is None, junk

    def test_a_vault_with_no_usable_root_never_raises(self, tmp_path):
        class _Rootless:
            pass

        assert gs.snapshot_path(_Rootless()) is None
        assert gs.growth_delta_line(_Rootless(), {"tools": 1}) == ""
        assert gs.load_snapshot(_Rootless()) is None

        class _Blank:
            root = "   "

        assert gs.snapshot_path(_Blank()) is None
        assert gs.growth_delta_line(_Blank(), {"tools": 1}) == ""

    def test_an_unwritable_root_costs_the_line_not_the_page(self, tmp_path):
        """The vault root is a FILE here, so mkdir/replace cannot succeed."""
        blocked = Path(tmp_path) / "not_a_dir"
        blocked.write_text("x", encoding="utf-8")
        assert gs.growth_delta_line(_Vault(blocked / "vault"), {"tools": 1}) == ""

    def test_no_temp_files_are_left_behind(self, tmp_path):
        v = _Vault(tmp_path)
        gs.growth_delta_line(v, {"tools": 1, "shadows": 0, "skills": 0}, now=_NOW)
        gs.growth_delta_line(v, {"tools": 2, "shadows": 0, "skills": 0},
                             now=_NOW + timedelta(days=9))
        assert sorted(p.name for p in Path(tmp_path).iterdir()) == \
            ["growth_snapshot.json"]

    def test_reading_the_line_writes_nothing_once_the_week_is_stamped(self, tmp_path):
        v = _Vault(tmp_path)
        gs.growth_delta_line(v, {"tools": 1, "shadows": 0, "skills": 0}, now=_NOW)
        before = (Path(tmp_path) / "growth_snapshot.json").read_bytes()
        for _ in range(3):
            gs.growth_delta_line(v, {"tools": 4, "shadows": 0, "skills": 0},
                                 now=_NOW + timedelta(days=1))
        assert (Path(tmp_path) / "growth_snapshot.json").read_bytes() == before


# ---------------------------------------------------------------------------
#  Wiring pins + design locks
# ---------------------------------------------------------------------------


def test_the_growth_card_consults_the_snapshot_module():
    """Reachability pin (the idiom of tests/test_persona_switcher.py): remove the
    production call site and this goes red. A weekly snapshot nothing renders is
    the half-built shape this line exists to avoid."""
    from systemu.interface.pages import console
    tree = ast.parse(inspect.getsource(console._build_growth_card))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "growth_delta_line" in called, sorted(called)
    assert "growth_snapshot" in inspect.getsource(console)   # the module, not a copy


def test_the_delta_line_is_rendered_only_when_it_says_something():
    """Honest silence is a RENDER rule too: an empty line must not become an
    empty label sitting in the card."""
    from systemu.interface.pages import console
    src = inspect.getsource(console._build_growth_card)
    assert "if week_line:" in src


def test_the_snapshot_module_needs_no_nicegui():
    src = inspect.getsource(gs)
    assert "nicegui" not in src


def test_the_snapshot_is_not_a_fact_and_not_a_table_item():
    """It is its OWN sidecar: no user fact, and the OnTheTable store keeps its
    single writer (the reconciler)."""
    src = inspect.getsource(gs)
    for forbidden in ("add_fact", "table_store", "table_reconciler", "TableItem"):
        assert forbidden not in src, forbidden


def test_the_writer_is_atomic():
    src = inspect.getsource(gs)
    assert "os.replace(" in src and "mkstemp(" in src


def test_the_private_writer_has_no_caller_outside_its_module():
    """The CONC-MAP guard pins who may call `growth_delta_line`; this pins that
    the write cannot be reached AROUND it - a second writer would have to add
    itself to one of the two lists."""
    root = Path(inspect.getsourcefile(gs)).resolve().parent.parent
    mine = Path(inspect.getsourcefile(gs)).resolve()
    hits = []
    for py in root.rglob("*.py"):
        if py.resolve() == mine:
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        if "_write_growth_snapshot(" in text:
            hits.append(py.name)
    assert hits == [], hits
