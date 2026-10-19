"""P2d / E2 metric - "Time to first task" on /insights, from the funnel record.

The duration runs from the ``setup_finished`` stamp to the
``first_task_succeeded`` stamp, both written by the ONE existing writer
(``funnel.mark_milestone``) at the existing call sites. It is computed from the
full UTC timestamps, never from their dates: two stamps three minutes apart on
the same day are "3m 7s", not "0m 0s", and two stamps straddling midnight are
"2m 30s", not "24h".

Anything that is not two full, timezone-aware timestamps in the right order -
a missing stamp, an older date-only record, a naive timestamp, a first task
that predates setup - reads NOT MEASURED. Never a guess.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


class _FakeVault:
    def __init__(self, root):
        self.root = root


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return _FakeVault(root)


def _write(vault, **stamps) -> None:
    (Path(vault.root) / "funnel.json").write_text(json.dumps(stamps), encoding="utf-8")


# --- the writer already records full UTC timestamps --------------------------

@pytest.mark.parametrize("milestone", ["setup_finished", "first_task_succeeded"])
def test_the_writer_stamps_a_full_utc_timestamp(vault, milestone):
    from systemu.runtime import funnel

    assert funnel.mark_milestone(vault, milestone) == funnel.STAMPED
    raw = json.loads((Path(vault.root) / "funnel.json").read_text(encoding="utf-8"))
    stamp = datetime.fromisoformat(raw[milestone])
    assert "T" in raw[milestone], "a date-only stamp cannot carry a duration"
    assert stamp.utcoffset() == timedelta(0), "the stamp must be UTC"
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=5)


def test_the_duration_from_the_real_writer_end_to_end(vault, monkeypatch):
    """Both stamps through ``mark_milestone`` itself, with the clock it reads
    pinned - the path production takes, not a hand-written sidecar."""
    from systemu.runtime import funnel

    t0 = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    clock = iter([t0, t0 + timedelta(seconds=187)])
    monkeypatch.setattr(funnel, "_utc_now", lambda: next(clock))

    assert funnel.mark_milestone(vault, "setup_finished") == funnel.STAMPED
    assert funnel.mark_milestone(vault, "first_task_succeeded") == funnel.STAMPED

    assert funnel.time_to_first_task(vault).seconds == 187
    assert funnel.time_to_first_task_line(vault) == "Time to first task: 3m 7s"


# --- the duration is computed from the stamps, not their dates ---------------

def test_the_duration_is_the_difference_of_the_stamps(vault):
    from systemu.runtime import funnel

    _write(vault,
           setup_finished="2026-03-04T05:06:07+00:00",
           first_task_succeeded="2026-03-04T05:09:14+00:00")
    result = funnel.time_to_first_task(vault)

    assert result.measured is True
    assert result.seconds == 187
    assert funnel.time_to_first_task_line(vault) == "Time to first task: 3m 7s"


def test_a_duration_across_midnight_is_minutes_not_a_day(vault):
    """The mutation this pins: computing from DATES would read 24h here."""
    from systemu.runtime import funnel

    _write(vault,
           setup_finished="2026-03-04T23:59:00+00:00",
           first_task_succeeded="2026-03-05T00:01:30+00:00")
    assert funnel.time_to_first_task(vault).seconds == 150
    assert funnel.time_to_first_task_line(vault) == "Time to first task: 2m 30s"


def test_stamps_in_different_offsets_are_compared_as_instants(vault):
    from systemu.runtime import funnel

    _write(vault,
           setup_finished="2026-03-04T05:00:00+00:00",
           first_task_succeeded="2026-03-04T10:31:10+05:30")
    assert funnel.time_to_first_task(vault).seconds == 70


# --- NOT MEASURED, never a guess ---------------------------------------------

def test_an_old_date_only_record_is_not_measured_but_stays_readable(vault):
    """An older record shape holding only dates: the journey table still shows
    the dates; the duration is NOT MEASURED rather than 'a day' or '0m 0s'."""
    from systemu.runtime import funnel

    _write(vault, setup_finished="2026-03-04", first_task_succeeded="2026-03-05")

    result = funnel.time_to_first_task(vault)
    assert result.measured is False
    assert result.seconds is None
    assert funnel.time_to_first_task_line(vault) == "Time to first task: NOT MEASURED"

    rows = dict(funnel.journey_rows(vault))
    assert rows[funnel.MILESTONE_LABELS["setup_finished"]] == "2026-03-04"
    assert rows[funnel.MILESTONE_LABELS["first_task_succeeded"]] == "2026-03-05"


def test_one_date_only_stamp_is_enough_to_refuse(vault):
    from systemu.runtime import funnel

    _write(vault,
           setup_finished="2026-03-04",
           first_task_succeeded="2026-03-04T05:09:14+00:00")
    assert funnel.time_to_first_task(vault).measured is False


def test_a_naive_timestamp_is_not_measured(vault):
    """No offset: it might be UTC, it might be local. Guessing is the bug."""
    from systemu.runtime import funnel

    _write(vault,
           setup_finished="2026-03-04T05:06:07",
           first_task_succeeded="2026-03-04T05:09:14+00:00")
    assert funnel.time_to_first_task(vault).measured is False


@pytest.mark.parametrize("stamps", [
    {},
    {"setup_finished": "2026-03-04T05:06:07+00:00"},
    {"first_task_succeeded": "2026-03-04T05:09:14+00:00"},
])
def test_a_missing_stamp_is_not_measured(vault, stamps):
    from systemu.runtime import funnel

    _write(vault, **stamps)
    result = funnel.time_to_first_task(vault)
    assert result.measured is False
    assert result.reason
    assert funnel.time_to_first_task_line(vault) == "Time to first task: NOT MEASURED"


def test_a_first_task_before_setup_is_not_measured(vault):
    """An operator who skipped the wizard, ran a task, then finished setup.
    A negative duration is not a time-to-first-task."""
    from systemu.runtime import funnel

    _write(vault,
           setup_finished="2026-03-04T06:00:00+00:00",
           first_task_succeeded="2026-03-04T05:00:00+00:00")
    result = funnel.time_to_first_task(vault)
    assert result.measured is False
    assert "before" in result.reason


@pytest.mark.parametrize("bad", ["sometime last tuesday", 12345, None, ["x"], ""])
def test_an_unparseable_stamp_is_not_measured(vault, bad):
    from systemu.runtime import funnel

    _write(vault, setup_finished=bad, first_task_succeeded="2026-03-04T05:09:14+00:00")
    assert funnel.time_to_first_task(vault).measured is False


def test_never_raises_without_a_vault_or_with_a_broken_sidecar(vault):
    from systemu.runtime import funnel

    assert funnel.time_to_first_task(None).measured is False
    assert funnel.time_to_first_task(_FakeVault("")).measured is False
    (Path(vault.root) / "funnel.json").write_text("<<<broken>>>", encoding="utf-8")
    assert funnel.time_to_first_task_line(vault) == "Time to first task: NOT MEASURED"


@pytest.mark.parametrize("seconds, text", [
    (0, "0m 0s"),
    (59, "0m 59s"),
    (187, "3m 7s"),
    (3600, "1h 0m 0s"),
    (3725, "1h 2m 5s"),
    (90061, "25h 1m 1s"),
])
def test_format_duration(seconds, text):
    from systemu.runtime import funnel

    assert funnel.format_duration(seconds) == text


@pytest.mark.parametrize("bad", [-1, None, 1.5, True, "60"])
def test_format_duration_refuses_what_is_not_a_measured_duration(bad):
    from systemu.runtime import funnel

    assert funnel.format_duration(bad) == funnel.NOT_MEASURED
