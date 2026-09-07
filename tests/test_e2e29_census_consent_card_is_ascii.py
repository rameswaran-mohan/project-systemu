"""D4 -- the census consent card an operator must read is ASCII.

WITNESSED DEFECT (v0.10.29, a real ConPTY)
    ``systemu census grant cloud_sync_roots`` renders a disclosure card that an
    operator has to read before granting a STANDING permission -- one that
    re-scans indefinitely and whose findings are sent to a model provider.  The
    card's prose carried U+2014 EM DASH.  On the console it landed as a
    replacement character, so the sentence explaining that the findings leave
    the machine read as damaged text at exactly the moment it had to be read.

RULING
    The card text is ASCII.  ``--`` where an em dash was.

THE PROPERTY PINNED HERE
    Every string the PRODUCTION ``consent_card`` returns -- key and value, at
    any depth -- encodes as ASCII.  ``cloud_sync_roots`` is the category the
    shipped surface can actually reach, so it is asserted on its own as well as
    in the sweep over all three.

A TRAP THIS TEST DELIBERATELY AVOIDS
    ``json.dumps(card)`` defaults to ``ensure_ascii=True``: it ESCAPES every
    non-ASCII character, so a check written as "dump the card and look for
    non-ASCII bytes" passes on a card that is full of em dashes.  That check
    was tried while writing this file and reported the defective card clean.
    The walk below therefore inspects the str objects themselves, and
    :func:`test_the_naive_json_check_would_have_passed` keeps the reason for
    that choice standing in the suite rather than in a comment.

NOTHING IS WRITTEN.  ``consent_card`` is a pure projection of module data; no
vault, no consent store and no filesystem is touched.
"""
from __future__ import annotations

import json

import pytest

from systemu.runtime.census_consent import (
    CATEGORIES, SURFACED_CATEGORIES, UnknownCensusCategory, consent_card,
)


def _strings(value, path="card"):
    """Every ``(path, str)`` inside ``value``, keys included, at any depth."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield path + ".<key>", key
            yield from _strings(item, path + "." + str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, path + "[" + str(index) + "]")


def _non_ascii(card) -> list:
    """``(path, offending characters)`` for every string that is not ASCII."""
    bad = []
    for path, text in _strings(card):
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            offenders = sorted({ch for ch in text if ord(ch) > 127})
            bad.append((path, [hex(ord(ch)) for ch in offenders],
                        text.encode("ascii", "backslashreplace").decode("ascii")))
    return bad


def test_the_shipped_consent_card_is_ascii():
    """The one category the shipped surface can grant."""
    assert "cloud_sync_roots" in SURFACED_CATEGORIES, (
        "fixture premise: this test pins the card the operator actually sees")
    bad = _non_ascii(consent_card("cloud_sync_roots"))
    assert not bad, (
        "the consent card an operator must read before granting a standing "
        "permission is not ASCII; on a real console these render as "
        "replacement characters:\n"
        + "\n".join("  {0}: {1} in {2!r}".format(p, c, t) for p, c, t in bad))


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_every_consent_card_is_ascii(category):
    """The ruling is about the card, not about one category of it.

    ``transmission_notice`` is assembled once for every category, so a fix
    aimed only at the shipped card would leave the other two carrying the same
    defective sentence -- and the commit message would then be false.
    """
    bad = _non_ascii(consent_card(category))
    assert not bad, (
        "the consent card for " + category + " is not ASCII:\n"
        + "\n".join("  {0}: {1} in {2!r}".format(p, c, t) for p, c, t in bad))


def test_the_unknown_category_refusal_is_ascii():
    """The refusal an operator reads when no card can be rendered."""
    with pytest.raises(UnknownCensusCategory) as caught:
        consent_card("no_such_category")
    message = str(caught.value)
    message.encode("ascii")  # raises UnicodeEncodeError if it is not ASCII
    assert "no_such_category" in message, message


def test_the_naive_json_check_would_have_passed():
    """Why the walk above does not go through ``json.dumps``.

    ``ensure_ascii=True`` is the default, so a dump of a card full of em dashes
    contains no non-ASCII byte at all.  A check built on it is vacuous, and it
    reported the defective card clean.  Pinned so nobody rewrites the walk into
    the cheaper form.
    """
    # Built with chr() so THIS file's own bytes stay ASCII; the value it
    # constructs carries a real em dash, which is what the walk has to see.
    defective = {"how": "Directory existence only " + chr(0x2014)
                        + " nothing inside is read."}

    dumped = json.dumps(defective)
    assert all(ord(ch) < 128 for ch in dumped), (
        "premise of this test: json.dumps escapes non-ASCII by default")

    assert _non_ascii(defective), (
        "the walk failed to see an em dash that json.dumps had hidden; it has "
        "been rewritten into the vacuous form this test exists to forbid")
