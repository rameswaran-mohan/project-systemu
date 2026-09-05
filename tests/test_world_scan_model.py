"""P2c world scan, Task 1 - the scan model.

The Table page's "Scan a folder" card makes exactly two claims to the operator:

    "Reads file names and extensions only - never file contents.
     Nothing leaves this machine."

Both sentences are load-bearing copy, so both get a test.  The first is pinned
by `test_the_module_never_reads_a_file_body` (a source-purity grep) plus a
behavioural pin that an unreadable-on-open file is still counted; the second by
`test_the_module_has_no_network_reach`.  A scan that opened one file would make
the card a lie, and a lie on a privacy card is the worst defect this surface
can carry.

The rest pins the fence shape:

  * REFUSALS ARE VALUES (DEC-32) - a missing path / a file / an unlistable
    folder come back as a `ScanResult` carrying a refusal code, never as a
    raise.  Nothing between the scan and the page can swallow a returned value.
  * CONTAINMENT IS PER ENTRY - an entry whose real location is outside the
    named folder is skipped and counted, so a symlink cannot walk the scan out
    of the folder the operator consented to.
  * THE CAP IS DISCLOSED - `cap` rides in every result and the summary line
    states it, so a truncated count can never read as a total.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from systemu.interface import world_scan
from systemu.interface.world_scan import ENTRY_CAP, ScanResult, scan_folder


def _touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_text("x", encoding="utf-8")


def _symlinks_supported(tmp_path) -> bool:
    probe_t = tmp_path / "_probe_target"
    probe_t.mkdir(exist_ok=True)
    link = tmp_path / "_probe_link"
    try:
        os.symlink(str(probe_t), str(link), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        return False
    try:
        return link.is_symlink()
    finally:
        try:
            link.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  Counting - names and extensions, one level down, nothing else
# ---------------------------------------------------------------------------


class TestCounting:
    def test_top_level_extensions_are_counted_case_insensitively(self, tmp_path):
        _touch(tmp_path, "a.csv", "b.CSV", "c.Md", "d.md", "e.png")
        r = scan_folder(str(tmp_path))
        assert r.ok is True
        assert r.counts == {".csv": 2, ".md": 2, ".png": 1}
        assert r.files == 5

    def test_one_directory_level_down_is_included(self, tmp_path):
        _touch(tmp_path, "top.csv")
        _touch(tmp_path / "sub", "one.csv", "two.pdf")
        r = scan_folder(str(tmp_path))
        assert r.counts == {".csv": 2, ".pdf": 1}

    def test_the_second_level_down_is_not_included(self, tmp_path):
        _touch(tmp_path, "top.csv")
        _touch(tmp_path / "sub" / "deeper", "hidden.csv")
        r = scan_folder(str(tmp_path))
        assert r.counts == {".csv": 1}, "the scan must stop one folder deep"

    def test_a_file_without_an_extension_counts_under_the_empty_key(self, tmp_path):
        _touch(tmp_path, "README", ".gitignore", "notes.txt")
        r = scan_folder(str(tmp_path))
        assert r.counts == {"": 2, ".txt": 1}

    def test_directories_are_not_counted_as_files(self, tmp_path):
        _touch(tmp_path, "a.csv")
        (tmp_path / "sub.d").mkdir()
        r = scan_folder(str(tmp_path))
        assert r.counts == {".csv": 1}
        assert r.files == 1

    def test_an_empty_folder_scans_to_nothing_but_still_succeeds(self, tmp_path):
        r = scan_folder(str(tmp_path))
        assert r.ok is True
        assert r.counts == {}
        assert r.files == 0
        assert r.refusal == ""


# ---------------------------------------------------------------------------
#  The two copy claims - names only, and nothing leaves this machine
# ---------------------------------------------------------------------------


class TestReadsNamesOnly:
    def test_a_file_the_process_cannot_open_is_still_counted(self, tmp_path):
        """The behavioural half of "never file contents": if the scan opened
        anything, a body it cannot read would break it.  It does not open, so
        the name is all it needs."""
        _touch(tmp_path, "a.csv")
        bad = tmp_path / "locked.csv"
        bad.write_text("secret", encoding="utf-8")
        os.chmod(str(bad), 0o000)
        try:
            r = scan_folder(str(tmp_path))
            assert r.counts == {".csv": 2}
        finally:
            os.chmod(str(bad), 0o600)

    def test_the_module_never_reads_a_file_body(self):
        """Source purity.  The card says "never file contents" - so the module
        may not contain a single call that could produce one."""
        src = Path(world_scan.__file__).read_text(encoding="utf-8")
        for forbidden in ("open(", "read_text", "read_bytes", "readlines",
                          "fileinput", "mmap"):
            assert forbidden not in src, (
                f"world_scan.py contains {forbidden!r} - the Table card promises "
                "it never reads file contents"
            )

    def test_the_module_has_no_network_reach(self):
        """Source purity.  The card says "nothing leaves this machine"."""
        src = Path(world_scan.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "httpx", "socket", "http",
                          "aiohttp", "smtplib"):
            assert forbidden not in src, (
                f"world_scan.py contains {forbidden!r} - the Table card promises "
                "nothing leaves this machine"
            )

    def test_the_module_writes_nothing_at_all(self):
        """Session-transient: a scan result is rendered and forgotten.  No new
        durable writer, no facts, and never the OnTheTable store - the
        reconciler stays its sole writer (DEC-10)."""
        src = Path(world_scan.__file__).read_text(encoding="utf-8")
        for forbidden in ("add_fact", "user_profile", "table_store",
                          "save_items", "write_text", "json.dump", "TableItem"):
            assert forbidden not in src, (
                f"world_scan.py contains {forbidden!r} - a scan is transient and "
                "persists nothing"
            )

    def test_the_module_is_ascii_only(self):
        assert Path(world_scan.__file__).read_text(encoding="utf-8").isascii()


# ---------------------------------------------------------------------------
#  Refusals are VALUES, not raises (DEC-32)
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_missing_path_is_a_returned_refusal(self, tmp_path):
        r = scan_folder(str(tmp_path / "nope"))
        assert type(r) is ScanResult
        assert r.ok is False
        assert r.refusal == world_scan.NO_SUCH_PATH
        assert r.counts == {}

    def test_a_file_is_not_a_folder(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_text("x", encoding="utf-8")
        r = scan_folder(str(f))
        assert r.ok is False
        assert r.refusal == world_scan.NOT_A_DIRECTORY

    def test_a_blank_path_is_refused_before_the_filesystem_is_touched(self):
        for blank in ("", "   ", None):
            r = scan_folder(blank)
            assert r.ok is False
            assert r.refusal == world_scan.NO_PATH

    def test_every_refusal_code_mints_an_operator_sentence(self):
        for code in (world_scan.NO_PATH, world_scan.NO_SUCH_PATH,
                     world_scan.NOT_A_DIRECTORY, world_scan.UNREADABLE):
            msg = world_scan.refusal_message(ScanResult(ok=False, refusal=code))
            assert msg and msg.isascii()

    def test_an_unknown_refusal_code_still_mints_a_sentence(self):
        msg = world_scan.refusal_message(ScanResult(ok=False, refusal="martian"))
        assert msg and msg.isascii()
        assert "martian" not in msg, "a refusal code is not operator copy"

    def test_a_successful_result_has_no_refusal_sentence(self, tmp_path):
        assert world_scan.refusal_message(scan_folder(str(tmp_path))) == ""

    def test_a_hostile_path_value_is_refused_rather_than_raising(self):
        """DEC-36 - the operand's concrete type is pinned before any operation,
        so a non-string cannot dispatch its way into the scan."""
        for junk in (7, [], {}, object()):
            r = scan_folder(junk)
            assert type(r) is ScanResult
            assert r.ok is False
            assert r.refusal == world_scan.NO_PATH


# ---------------------------------------------------------------------------
#  Containment - a symlink cannot walk the scan out of the named folder
# ---------------------------------------------------------------------------


class TestContainment:
    def test_a_symlinked_file_pointing_outside_is_skipped_and_counted(self, tmp_path):
        if not _symlinks_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        outside = tmp_path / "outside"
        _touch(outside, "secret.pdf")
        named = tmp_path / "named"
        _touch(named, "mine.csv")
        os.symlink(str(outside / "secret.pdf"), str(named / "leak.pdf"))

        r = scan_folder(str(named))
        assert r.ok is True
        assert r.counts == {".csv": 1}, "a file outside the folder was counted"
        assert r.outside == 1

    def test_a_symlinked_directory_pointing_outside_is_never_descended(self, tmp_path):
        if not _symlinks_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        outside = tmp_path / "outside"
        _touch(outside, "a.pdf", "b.pdf", "c.pdf")
        named = tmp_path / "named"
        _touch(named, "mine.csv")
        os.symlink(str(outside), str(named / "escape"), target_is_directory=True)

        r = scan_folder(str(named))
        assert r.counts == {".csv": 1}
        assert r.outside == 1

    def test_a_symlink_that_stays_inside_the_folder_is_kept(self, tmp_path):
        if not _symlinks_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        named = tmp_path / "named"
        _touch(named, "mine.csv")
        os.symlink(str(named / "mine.csv"), str(named / "alias.csv"))

        r = scan_folder(str(named))
        assert r.counts == {".csv": 2}
        assert r.outside == 0

    def test_the_named_folder_is_the_containment_root_even_via_a_symlink(self, tmp_path):
        """The operator named a path; the scan resolves it once and everything
        is measured against THAT, so reaching the folder through a link does
        not turn its own contents into escapes."""
        if not _symlinks_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        real = tmp_path / "real"
        _touch(real, "a.csv", "b.csv")
        link = tmp_path / "link"
        os.symlink(str(real), str(link), target_is_directory=True)

        r = scan_folder(str(link))
        assert r.ok is True
        assert r.counts == {".csv": 2}
        assert r.outside == 0


# ---------------------------------------------------------------------------
#  The cap is stated, not implied
# ---------------------------------------------------------------------------


class TestCap:
    def test_the_cap_is_two_thousand_entries(self):
        assert ENTRY_CAP == 2000

    def test_every_result_carries_the_cap(self, tmp_path):
        r = scan_folder(str(tmp_path))
        assert r.cap == ENTRY_CAP
        assert r.capped is False

    def test_a_folder_over_the_cap_stops_and_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(world_scan, "ENTRY_CAP", 5)
        _touch(tmp_path, *[f"f{i}.csv" for i in range(12)])
        r = scan_folder(str(tmp_path))
        assert r.cap == 5
        assert r.capped is True
        assert r.entries == 5
        assert sum(r.counts.values()) == 5

    def test_the_truncation_is_deterministic(self, tmp_path, monkeypatch):
        """Same folder, same partial answer - a cap that returned a different
        subset per run would make the derived proposals flicker."""
        monkeypatch.setattr(world_scan, "ENTRY_CAP", 4)
        _touch(tmp_path, *[f"f{i}.csv" for i in range(9)])
        _touch(tmp_path, *[f"g{i}.pdf" for i in range(9)])
        assert scan_folder(str(tmp_path)).counts == scan_folder(str(tmp_path)).counts

    def test_the_summary_line_states_the_cap_and_the_depth(self, tmp_path):
        _touch(tmp_path, "a.csv", "b.pdf")
        line = world_scan.summary_line(scan_folder(str(tmp_path)))
        assert str(ENTRY_CAP) in line, "the cap must be visible to the operator"
        assert "one folder deep" in line
        assert line.isascii()

    def test_the_summary_line_says_when_the_count_is_partial(self, tmp_path, monkeypatch):
        monkeypatch.setattr(world_scan, "ENTRY_CAP", 3)
        _touch(tmp_path, *[f"f{i}.csv" for i in range(9)])
        line = world_scan.summary_line(scan_folder(str(tmp_path)))
        assert "Stopped" in line, "a truncated count must never read as a total"

    def test_the_summary_line_reports_entries_the_fence_skipped(self, tmp_path):
        """The containment refusal is not silent: an entry the scan would not
        follow is counted and said out loud, so the operator can tell a small
        folder from a fenced one."""
        if not _symlinks_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        outside = tmp_path / "outside"
        _touch(outside, "secret.pdf")
        named = tmp_path / "named"
        _touch(named, "mine.csv")
        os.symlink(str(outside / "secret.pdf"), str(named / "leak.pdf"))
        line = world_scan.summary_line(scan_folder(str(named)))
        assert "skipped" in line

    def test_a_refused_scan_has_no_summary_line(self, tmp_path):
        assert world_scan.summary_line(scan_folder(str(tmp_path / "nope"))) == ""


# ---------------------------------------------------------------------------
#  Nothing is persisted
# ---------------------------------------------------------------------------


def test_a_scan_leaves_no_trace_on_disk(tmp_path):
    """Session-transient means exactly that: scanning a folder must not create
    a sidecar, an index or a cache anywhere under it."""
    _touch(tmp_path, "a.csv", "b.pdf")
    before = sorted(p.name for p in tmp_path.iterdir())
    scan_folder(str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_result_names_the_folder_it_scanned(tmp_path):
    r = scan_folder(str(tmp_path))
    assert Path(r.folder) == Path(str(tmp_path)).resolve()


# ---------------------------------------------------------------------------
#  What the card renders
# ---------------------------------------------------------------------------


class TestCountRows:
    def test_rows_are_ordered_by_count_then_extension(self, tmp_path):
        _touch(tmp_path, "a.csv", "b.csv", "c.pdf", "d.md", "e.md", "f.md")
        rows = world_scan.count_rows(scan_folder(str(tmp_path)))
        assert rows == [(".md", 3), (".csv", 2), (".pdf", 1)]

    def test_extensionless_files_get_a_readable_label(self, tmp_path):
        _touch(tmp_path, "README", "notes.txt")
        assert world_scan.count_rows(scan_folder(str(tmp_path))) == [
            ("(no extension)", 1), (".txt", 1),
        ]

    def test_a_refused_scan_has_no_rows(self, tmp_path):
        assert world_scan.count_rows(scan_folder(str(tmp_path / "nope"))) == []
