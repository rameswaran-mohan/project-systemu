"""DEC-32c -- ``debug resolver-replay`` output is ASCII-only.

The NO CORPUS render was made ASCII when the corpus started shipping in the
wheel, but the main render -- including the DEFINITIVE banner -- still printed
an em dash and a warning-sign character. Verdict-carrying output is ASCII-only:
a Windows console on a legacy code page, or a log captured with the wrong
encoding, turns those into mojibake or a UnicodeEncodeError.

THE RULING: ``--`` for the em dash, ``WARNING:`` for the warning sign, and every
interpolated value escaped (a scenario id or error may carry any character).
Pinned on every branch of ``format_resolver_replay`` and on the real CLI.
"""
from __future__ import annotations

import pytest

from systemu.runtime import resolver_replay as rr

_EM_DASH = "\u2014"
_WARNING_SIGN = "\u26a0"


def _report(**over):
    base = dict(declared=2, scenarios=2, avoidable_count=1, necessary_count=1,
                unassessable_count=0, reasons={}, by_class={},
                match_bases={"exact": 1, "canonical": 1}, mislabelled=(),
                errors=(), missing=frozenset(), extra=frozenset(), details=())
    base.update(over)
    return rr.ReplayReport(**base)


def _assert_ascii(lines):
    bad = [ln for ln in lines if not ln.isascii()]
    assert not bad, f"resolver-replay lines must be ASCII-only: {bad!r}"
    text = "\n".join(lines)
    assert _EM_DASH not in text and _WARNING_SIGN not in text
    return text


def test_the_shipped_corpus_definitive_render_is_ascii(monkeypatch):
    monkeypatch.delenv("SYSTEMU_FIELD_CORPUS", raising=False)
    report = rr.resolver_replay_report()
    assert report.definitive, rr.format_resolver_replay(report)
    text = _assert_ascii(rr.format_resolver_replay(report))
    assert "DEFINITIVE" in text
    assert "operator's answer known -- not a proxy" in text


def test_a_definitive_render_with_every_warning_block_is_ascii():
    lines = rr.format_resolver_replay(_report(mislabelled=(
        {"scenario_id": "s1", "schema_path": "a.b", "declared": "avoidable",
         "replayed": "necessary", "reason": "different value"},)))
    text = _assert_ascii(lines)
    assert "(canonical is the lossier fold -- it casefolds" in text
    warn = [ln for ln in lines if "disagree with their fixture label" in ln]
    assert warn and warn[0].lstrip().startswith("WARNING: 1 ask(s)"), warn


def test_a_non_definitive_render_is_ascii_and_every_block_says_warning():
    lines = rr.format_resolver_replay(_report(
        declared=3, missing=frozenset({"gone.json"}), extra=frozenset({"stray.json"}),
        errors=("s9: could not load",)))
    text = _assert_ascii(lines)
    assert lines[0].startswith("Avoidable-ask rate (resolver replay): INCOMPLETE REPLAY -- ")
    assert "NOT 0%" in text
    for needle in ("rostered fixture(s) MISSING", "unrostered fixture(s) present",
                   "corpus error(s)"):
        hit = [ln for ln in lines if needle in ln and "WARNING:" in ln]
        assert hit, f"no WARNING: line for {needle!r}: {lines!r}"


def test_an_empty_roster_render_is_ascii():
    lines = rr.format_resolver_replay(_report(declared=0, scenarios=0,
                                              avoidable_count=0, necessary_count=0))
    text = _assert_ascii(lines)
    assert "NO committed roster (roster.json empty or absent) -- no" in text


def test_no_assessable_asks_render_is_ascii():
    lines = rr.format_resolver_replay(_report(avoidable_count=0, necessary_count=0,
                                              unassessable_count=2))
    _assert_ascii(lines)
    assert lines[0] == ("Avoidable-ask rate (resolver replay): NO ASSESSABLE ASKS "
                        "-- no rate (this is NOT 0%)")


@pytest.mark.parametrize("field, value", [
    ("errors", (f"s1 {_EM_DASH} caf\u00e9 failed",)),
    ("missing", frozenset({f"caf\u00e9{_WARNING_SIGN}.json"})),
    ("mislabelled", ({"scenario_id": "s\u00e9", "schema_path": "p",
                      "declared": "a", "replayed": "n", "reason": _EM_DASH},)),
])
def test_interpolated_values_are_escaped_not_printed_raw(field, value):
    lines = rr.format_resolver_replay(_report(**{field: value}))
    text = _assert_ascii(lines)
    assert "\\u2014" in text or "\\xe9" in text or "\\u26a0" in text


def test_the_cli_command_prints_ascii(monkeypatch):
    from click.testing import CliRunner
    from systemu.interface.cli_commands import debug_resolver_replay
    monkeypatch.delenv("SYSTEMU_FIELD_CORPUS", raising=False)
    res = CliRunner().invoke(debug_resolver_replay, [])
    assert res.exit_code == 0, res.output
    assert "DEFINITIVE" in res.output
    _assert_ascii(res.output.splitlines())


def test_the_drift_blocks_read_as_written_not_as_escapes():
    """The literals themselves are ASCII: escaping is for interpolated data
    only, so a fixed sentence never renders as a backslash escape."""
    lines = rr.format_resolver_replay(_report(
        declared=3, missing=frozenset({"gone.json"}), extra=frozenset({"stray.json"})))
    text = _assert_ascii(lines)
    assert "\\u" not in text and "\\x" not in text, text
    assert "absent on disk -- their asks are gone" in text
    assert "roster.json -- de-rostering" in text


def test_the_refusal_message_is_ascii():
    with pytest.raises(TypeError) as exc:
        rr.format_resolver_replay({})
    assert str(exc.value).isascii(), str(exc.value)
