"""D2 -- an empty denominator is NOT MEASURED, never a fabricated ``0/0 = 0%``.

WITNESSED DEFECT (v0.10.27, a from-scratch home)
    ``systemu debug avoidable-ask`` on a vault that had never recorded an ask
    printed FOUR blocks that disagreed with each other about the same emptiness::

        No-prior-attempt asks: 0/0 = 0%
          . DEFINITIVE avoidable (resolvable-confirmed): 0/0 = 0%
        Inventory-hit: NOT MEASURED -- ... NO RATE (this is NOT 0%)
        Quick-lane asks: NOT MEASURED (0 recorded asks; ...)

    The bottom two already carried the honest form. The top two rendered a
    PERCENTAGE over an empty population -- and ``0%`` on an avoidable-ask metric
    reads as the best possible score. A quotable headline ("systemu asks nothing
    avoidably") invented out of a corpus with nothing in it.

THE RULE THIS PINS
    A rate whose denominator is zero has no value, and the report says so in the
    same words its honest siblings already use. Both directions are pinned: the
    fabricated form must be absent on an empty corpus, and a POPULATED corpus must
    still render its rate -- a "fix" that simply stopped printing rates would pass
    the first half and destroy the surface.
"""
from __future__ import annotations

import json
import re

from click.testing import CliRunner

from systemu.interface import cli_commands as cc
from systemu.runtime import replay_metrics as rm

#: ``<n>/<m> = <p>%`` -- the exact shape a zero denominator must never produce.
_RENDERED_RATE = re.compile(r"\d+\s*/\s*\d+\s*=\s*\d+%")


class _Vault:
    """The stand-in every replay_metrics test in this repo uses: the reports read
    ``vault.root`` and nothing else."""

    def __init__(self, root):
        self.root = root


def _report(vault, monkeypatch) -> str:
    monkeypatch.setattr(cc, "_get_vault_and_config", lambda ctx: (None, vault))
    res = CliRunner().invoke(cc.debug_avoidable_ask, obj={})
    assert res.exit_code == 0, res.output
    return res.output


# --------------------------------------------------------------------------- #
# THE PIN: nothing recorded => no rate anywhere, and every block says so
# --------------------------------------------------------------------------- #

def test_a_fresh_vault_renders_no_rate_at_all(tmp_path, monkeypatch):
    """Not one ``n/m = p%`` on a corpus that has never held a row."""
    out = _report(_Vault(tmp_path), monkeypatch)

    assert "0/0" not in out, (
        "a rate over an empty denominator is still being rendered:\n" + out)
    assert not _RENDERED_RATE.search(out), (
        f"rendered rate {_RENDERED_RATE.search(out).group()!r} over an empty "
        f"corpus:\n{out}")


def test_every_block_of_a_fresh_report_says_not_measured(tmp_path, monkeypatch):
    """FOUR blocks, ONE verdict. The defect was that two of them disagreed with the
    other two about the same empty corpus."""
    out = _report(_Vault(tmp_path), monkeypatch)

    for block in ("No-prior-attempt asks:",
                  "DEFINITIVE avoidable (resolvable-confirmed):",
                  "Inventory-hit:",
                  "Quick-lane asks:"):
        line = next((ln for ln in out.splitlines() if block in ln), None)
        assert line is not None, f"the {block!r} block vanished from the report:\n{out}"
        assert "NOT MEASURED" in line, (
            f"{block!r} does not report an empty population honestly: {line!r}")
        # A rendered RATE, not a bare "%": the honest form ends in "this is NOT 0%",
        # and a pin that banned the character outright would forbid the disclaimer.
        assert not _RENDERED_RATE.search(line), (
            f"{block!r} still carries a rate over nothing: {line!r}")


def test_the_unmeasured_lines_say_this_is_not_a_zero(tmp_path, monkeypatch):
    """The sibling blocks spell out that NOT MEASURED is not a good score; the two
    repaired ones must not be quieter about it than the two that were already
    right, or the reader learns the distinction from only half the report."""
    out = _report(_Vault(tmp_path), monkeypatch)

    for block in ("No-prior-attempt asks:",
                  "DEFINITIVE avoidable (resolvable-confirmed):"):
        line = next(ln for ln in out.splitlines() if block in ln)
        assert "NOT 0%" in line, (
            f"{block!r} does not disclaim the zero reading: {line!r}")


# --------------------------------------------------------------------------- #
# POSITIVE CONTROL: a populated corpus still reports its rate
# --------------------------------------------------------------------------- #

def test_a_populated_corpus_still_renders_the_no_prior_attempt_rate(
        tmp_path, monkeypatch):
    """Through the PRODUCTION recorder and the real CLI. Deleting the rate would
    satisfy the pins above; this is what stops that from being a fix."""
    vault = _Vault(tmp_path)
    rm.record_ask(vault, kind="capability", attempts_before=0, tool_attempts=0)
    rm.record_ask(vault, kind="capability", attempts_before=1, tool_attempts=2)

    out = _report(vault, monkeypatch)

    head = next(ln for ln in out.splitlines() if "No-prior-attempt asks:" in ln)
    assert head == "No-prior-attempt asks: 1/2 = 50%", head
    assert "NOT MEASURED" not in head, (
        "a measured population is being reported as unmeasured: " + head)


def test_a_populated_answer_linked_corpus_still_renders_its_definitive_rate(
        tmp_path, monkeypatch):
    """The other repaired rate, over the corpus its own loader reads."""
    vault = _Vault(tmp_path)
    path = tmp_path / "audit" / "ask_avoidable.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in (
            {"class": "input", "resolution": "resolvable_confirmed",
             "schema_path": "a/x", "scoring_version": 2},
            {"class": "input", "resolution": "resolvable_overridden",
             "schema_path": "a/y", "scoring_version": 2},
        )) + "\n", encoding="utf-8")
    assert rm.answer_linked_ask_report(vault)["total"] == 2, "fixture premise"

    out = _report(vault, monkeypatch)

    assert "DEFINITIVE avoidable (resolvable-confirmed): 1/2 = 50%" in out, out
