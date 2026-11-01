"""D1 -- the shippable tree names no private place, machine or person.

WITNESSED DEFECT (v0.10.29, and one hit already live on PyPI in 0.10.28)
    The sdist ships ``tests/``.  Test text carried the development machine's
    private directory names and the operator's own first name, so a published
    artefact told every downloader where the author's source tree lives and
    what the author is called.  Neither fact is part of the product.

THE PROPERTY PINNED HERE
    No file git tracks under the SHIPPABLE roots (``systemu/``, ``sharing_on/``,
    ``tests/``, ``pyproject.toml``) contains any of a fixed list of private
    place-, machine- and person-names, case-insensitively -- nor the shape of a
    development worktree codename.

    ``README.md`` and ``docs/`` are deliberately OUT of scope: they are curated
    per channel and the public channel carries its own scrubbed copies.

SAMPLE DATA IN A SHIPPED FILE IS SYNTHETIC
    The list also bans the operator's home city and neighbourhood.  Those
    reached the tree as *sample* data -- placeholder locations in test
    fixtures, in a shipped extraction prompt and in a shipped seed-tool
    parameter description -- which is exactly how a real place-name survives
    review: nobody reads an example as a disclosure.  A published wheel
    carrying them still tells every downloader which city, and which locality
    within it, the author lives in.  So sample places are synthetic
    (``Springfield`` / ``Riverside District``), with no exception.

    An IANA timezone identifier is NOT sample data -- it is a functional key
    into the zone database and does real work -- so these rules name
    settlements only, and a control below pins that they spare it.

WHY THE PATTERNS ARE ASSEMBLED FROM FRAGMENTS
    This file lives in ``tests/`` and is therefore SCANNED BY ITSELF.  Writing
    a banned token as one literal would make the fence permanently red on its
    own source.  The alternative -- excluding this file from the scan -- would
    open exactly the hole the fence exists to close, because the excluded file
    is the one an author edits when adding a pattern.  So every banned token is
    built from two fragments at import time: the whole token never appears in
    these bytes, and nothing is excluded from the walk.

    The same reasoning applies to the failure message: it names ``path:line``
    and the human label of the rule, and replaces the matched span itself with
    a placeholder, so the fence's own output can never become a fresh copy of
    the token it just found.

    That masking is ALL-RULES and ALL-OCCURRENCES, and it runs before the
    excerpt is trimmed.  Masking only the reporting rule's span was not enough:
    a line tripping two rules is listed once per rule and each listing printed
    the other rule's token in clear.  Parametrised controls below carry explicit
    ids for the same reason -- pytest builds a node id out of the parameter
    value, so an unnamed case republishes its own sample.

FRAGMENTS OF A NAME ARE THE NAME (added after a docstring split a codename)
    The rules above match a token WHOLE.  A docstring that split a development
    worktree codename across two backticked fragments -- the first ending
    mid-word, the second beginning with the tail of that word and then the
    distinctive half -- matched none of them and shipped for several releases.
    So the denylist is also matched as FRAGMENTS: every hyphen- or
    underscore-separated segment of five or more characters of a multi-segment
    plain token in ``_BANNED`` is banned as a bare case-insensitive SUBSTRING,
    anywhere in a shipped file.  The segments are DERIVED from ``_BANNED`` (no
    second list of names to keep in step), and nothing new is named here.

    Two honest limits, both pinned below.  (1) A segment that is an ordinary
    word -- one that is a substring of common English or appears all over the
    tree -- cannot be banned as a substring without flagging the product's own
    prose, so ``_GENERIC_SEGMENTS`` carries those; each entry must be a segment
    that really is derived (no dead entries) and every codename keeps at least
    one distinctive fragment.  (2) THIS file is exempt from the FRAGMENT pass
    only (by its own path, derived from ``__file__``): its denylist is made of
    exactly those fragments, and splitting every one below five characters
    would mean rewriting the sample strings of the existing controls.  The
    whole-token rules still scan it, so the dangerous thing -- a complete name
    -- still cannot hide here.

NOTHING IS WRITTEN.  The scan is read-only and touches no vault.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


#: Repository root: this file is ``<root>/tests/<name>.py``.
_ROOT = Path(__file__).resolve().parents[1]

#: The roots that end up inside a published wheel or sdist.
_SHIPPABLE_ROOTS = ("systemu", "sharing_on", "tests", "pyproject.toml")

#: Extensions whose bytes are not text; matching them is meaningless and
#: decoding them is noise.  Everything else is read and scanned.
_BINARY_SUFFIXES = (
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp",
    ".pdf", ".zip", ".gz", ".tar", ".whl", ".so", ".pyd", ".dll", ".exe",
)

#: (label, regex) for every banned token.  See the module docstring for why the
#: literals are split.  Matching is case-insensitive.
#:
#: The last rule is a SHAPE, not a name: development worktrees here are named
#: ``<word>-<word>-<6 hex>``, and new ones are minted constantly, so listing
#: them one at a time cannot terminate.
#:
#: That shape rule is anchored with ``(?<![-\\w])`` / ``(?![-\\w])`` rather than
#: ``\\b``.  ``\\b`` treats a hyphen as a boundary, so the bare shape matched the
#: TAIL of any longer hyphenated token -- it flagged the synthetic API key in
#: ``tests/test_secrets_at_rest.py`` (a five-segment string whose last three
#: segments happen to fit the shape).  Requiring that no hyphen or word
#: character sits on either side makes the rule mean "the WHOLE hyphenated
#: token is a two-word codename with a hex suffix", which is the real shape and
#: which the synthetic key is not.
_BANNED: tuple = (
    ("private repository name (snake case)", "project" + "_systemu_pro"),
    ("private repository name (kebab case)", "project" + "-systemu-pro"),
    ("private source-tree root", "anti" + "gravity"),
    ("development worktree directory", r"\.claude[/\\]+worktrees"),
    ("development worktree directory (flattened)", "dot_claude" + "_worktrees"),
    ("operator's personal name", "ramesh" + "waran"),
    ("operator's personal name (spelling variant)", "rames" + "waran"),
    ("operator's home city", "banga" + "lore"),
    ("operator's home city (alternative spelling)", "benga" + "luru"),
    ("operator's home neighbourhood", "indira" + "nagar"),
    ("internal programme codename", "go" + "getter"),
    ("development worktree codename", "quirky-" + "goodall"),
    ("development worktree codename", "pensive-" + "tesla"),
    ("development worktree codename", "objective-" + "wescoff"),
    ("development worktree codename shape",
     r"(?<![-\w])[a-z]+-[a-z]+-[0-9a-f]{6}(?![-\w])"),
)

_COMPILED: tuple = tuple(
    (label, pattern, re.compile(pattern, re.IGNORECASE)) for label, pattern in _BANNED
)

# ---------------------------------------------------------------------------
# Fragment rule -- see "FRAGMENTS OF A NAME ARE THE NAME" in the docstring.
# ---------------------------------------------------------------------------

#: The shortest segment banned as a bare substring.  Shorter segments are
#: ordinary letter runs ("pro", "dot") that would flag half the tree.
_FRAGMENT_MIN_LEN = 5

#: Segments of a banned token that are ORDINARY WORDS, so banning them as a
#: substring would flag the product's own prose: a substring of common English
#: ("expensive"), a normal word of the domain ("project", "objective") or a
#: normal identifier of the product ("systemu", "claude", "worktrees").  Each
#: entry must be a segment that ``_derive_fragments`` really meets (pinned by
#: ``test_every_generic_segment_is_a_live_exclusion``) -- a dead entry is a
#: stale excuse -- and every codename must keep a distinctive fragment
#: (``test_every_codename_keeps_a_distinctive_fragment``).
_GENERIC_SEGMENTS = frozenset({
    "objective", "pensive", "project", "systemu", "claude", "worktrees",
})

#: A pattern carrying any of these is a REGEX (the shape rule, the worktree
#: directory rule), not a plain token; only plain tokens have segments.
_REGEX_METACHARS = re.compile(r"[\\.\[\](){}*+?^$|]")

_FRAGMENT_LABEL = "development worktree codename fragment"

#: The one file exempt from the FRAGMENT pass: this one, because its denylist
#: IS the fragments.  Derived from ``__file__`` so a rename keeps the pairing;
#: the whole-token rules still scan it.
_SELF = Path(__file__).resolve().relative_to(_ROOT).as_posix()
_FRAGMENT_EXEMPT = frozenset({_SELF})


def _plain_tokens() -> tuple:
    """The plain-string (non-regex) tokens of ``_BANNED``, as written."""
    return tuple(pattern for _label, pattern in _BANNED
                 if _REGEX_METACHARS.search(pattern) is None)


def _segments(token: str) -> list:
    """The hyphen/underscore/space-separated segments of ``token``, lowered."""
    return [part for part in re.split(r"[^a-z0-9]+", token.lower()) if part]


def _derive_fragments() -> tuple:
    """Every segment of a multi-segment plain token worth banning on its own.

    A single-segment token is its own only segment, so the whole-token rule
    already covers it and a fragment rule would only double-report it.
    """
    found: list = []
    for token in _plain_tokens():
        parts = _segments(token)
        if len(parts) < 2:
            continue
        for part in parts:
            if (len(part) >= _FRAGMENT_MIN_LEN and part not in _GENERIC_SEGMENTS
                    and part not in found):
                found.append(part)
    return tuple(found)


_FRAGMENTS: tuple = _derive_fragments()

# An empty alternation compiles to a pattern that matches EVERYTHING.  Refuse
# to build the rule at all rather than ship a fence that is inert or absurd.
assert _FRAGMENTS, (
    "no codename fragments derived from the denylist; the fragment rule would be inert")

_COMPILED = _COMPILED + ((
    _FRAGMENT_LABEL, "|".join(_FRAGMENTS),
    re.compile("|".join(re.escape(part) for part in _FRAGMENTS), re.IGNORECASE),
),)


def _ascii(text: str) -> str:
    """ASCII-only rendering of ``text`` -- verdict strings never carry UTF-8."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _redact(line: str) -> str:
    """``line`` with EVERY match of EVERY rule replaced by a placeholder.

    Not "the span the reporting rule matched" -- all of them.  A line that
    trips two rules is reported once per rule, and masking only the reporting
    rule's span printed the other rule's token in clear; the same line matching
    one rule twice printed the second copy.  Both make the failure message a
    fresh copy of what the fence just found, in a CI log that outlives it.

    Runs BEFORE the caller trims or truncates (DEC-31): a token past the
    excerpt cap must be MASKED, not merely cut off by the slice.
    """
    for _label, _pattern, rx in _COMPILED:
        line = rx.sub("<<REDACTED>>", line)
    return line


def _tracked_files() -> list:
    """Every git-tracked path under the shippable roots, repo-relative.

    Fails CLOSED: an enumeration that produces nothing is an unreadable index,
    not an empty tree, and an unreadable index must never read as "clean".
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--"] + list(_SHIPPABLE_ROOTS),
        cwd=str(_ROOT), capture_output=True,
    )
    out = proc.stdout.decode("utf-8", errors="backslashreplace")
    err = proc.stderr.decode("utf-8", errors="backslashreplace")
    assert proc.returncode == 0, _ascii(
        "cannot enumerate the shippable tree; the fence cannot report clean: "
        + err.strip())
    return [name for name in out.split("\0") if name]


def _offenders() -> tuple:
    """``(offenders, files_scanned)`` for the whole shippable tree.

    Each offender is ``(path, line_number, label, redacted_excerpt)``.
    """
    offenders = []
    scanned = 0
    for name in _tracked_files():
        lowered = name.lower()
        if any(lowered.endswith(suffix) for suffix in _BINARY_SUFFIXES):
            continue
        path = _ROOT / name
        if not path.is_file():
            continue
        with open(path, "rb") as handle:
            raw = handle.read()
        scanned += 1
        text = raw.decode("utf-8", errors="backslashreplace")
        offenders.extend(_text_offenders(name, text))
    return tuple(offenders), scanned


def _text_offenders(name: str, text: str) -> list:
    """Every ``(path, line, label, redacted_excerpt)`` hit in one file's text.

    The per-file core of :func:`_offenders`, split out so a control can scan a
    PLANTED text under any path.  The fragment rule alone is skipped for a path
    in ``_FRAGMENT_EXEMPT`` (this file); every other rule applies to every path.
    """
    hits = []
    fragment_exempt = name in _FRAGMENT_EXEMPT
    for number, line in enumerate(text.splitlines(), 1):
        for label, _pattern, rx in _COMPILED:
            if fragment_exempt and label == _FRAGMENT_LABEL:
                continue
            if rx.search(line) is None:
                continue
            excerpt = _redact(line)
            hits.append((name, number, label, _ascii(excerpt.strip())[:160]))
    return hits


def test_the_shippable_tree_names_no_private_place_or_person():
    offenders, scanned = _offenders()
    if offenders:
        listing = "\n".join(
            "  {0}:{1}: {2} -- {3}".format(path, number, label, excerpt)
            for path, number, label, excerpt in offenders
        )
        pytest.fail(_ascii(
            "{0} private name(s) survive in the shippable tree (scanned {1} "
            "files under {2}); a published sdist would carry every one of "
            "them:\n{3}".format(
                len(offenders), scanned, "/ ".join(_SHIPPABLE_ROOTS), listing)))


# ---------------------------------------------------------------------------
# Positive controls -- a fence that scans nothing, or whose rules match
# nothing, passes for the wrong reason.
# ---------------------------------------------------------------------------

def test_the_scan_actually_reads_the_tree():
    """A clean verdict must come from reading files, not from reading none."""
    _offenders_unused, scanned = _offenders()
    assert scanned > 500, _ascii(
        "the fence read only {0} files; a clean verdict from a walk this small "
        "is not evidence of anything".format(scanned))


def test_a_constructed_offender_matches_the_pattern_set():
    """Bytes that DO carry a private place-name must be caught."""
    planted = (b"    # vault under D:/" + b"Anti" + b"gravity" + b"/"
               + b"Project" + b"_systemu_pro/x")
    line = planted.decode("utf-8", errors="backslashreplace")
    hits = [label for label, _pattern, rx in _COMPILED if rx.search(line)]
    assert hits, _ascii(
        "a deliberately constructed offending byte string matched no rule; "
        "the pattern set is inert")
    assert len(hits) >= 2, _ascii("expected both rules to fire, got: " + repr(hits))


#: Explicit ids again: the sample IS the banned token, so an id derived from it
#: would print every one of them on a green run.
@pytest.mark.parametrize("label, sample", [
    pytest.param("private repository name (snake case)",
                 "d:/x/" + "Project" + "_systemu_pro/y", id="repo-snake"),
    pytest.param("private repository name (kebab case)",
                 "https://h/" + "project" + "-systemu-pro", id="repo-kebab"),
    pytest.param("private source-tree root",
                 "D:/" + "Anti" + "gravity" + "/x", id="tree-root"),
    pytest.param("development worktree directory",
                 "repo/." + "claude/work" + "trees/w", id="worktree-dir"),
    pytest.param("development worktree directory (flattened)",
                 "tmp/" + "dot_claude" + "_worktrees" + "/w", id="worktree-dir-flat"),
    pytest.param("operator's personal name",
                 "name=\"" + "Ramesh" + "waran" + "\"", id="person"),
    pytest.param("operator's personal name (spelling variant)",
                 "github.com/" + "rames" + "waran" + "-mohan", id="person-variant"),
    pytest.param("internal programme codename",
                 "the " + "go" + "getter" + " spec", id="programme-codename"),
    pytest.param("development worktree codename",
                 "/w/" + "quirky-" + "goodall" + "/x", id="codename-1"),
    pytest.param("development worktree codename",
                 "/w/" + "pensive-" + "tesla" + "/x", id="codename-2"),
    pytest.param("development worktree codename",
                 "/w/" + "objective-" + "wescoff" + "-1b02d7/x", id="codename-3"),
    pytest.param("development worktree codename shape",
                 "/w/" + "curious-" + "fermat" + "-9fe210/x", id="codename-shape"),
    pytest.param("operator's home city",
                 "\"user lives in " + "Banga" + "lore" + "\"", id="home-city"),
    pytest.param("operator's home city (alternative spelling)",
                 "location_text=\"" + "Benga" + "luru" + ", IN\"",
                 id="home-city-variant"),
    pytest.param("operator's home neighbourhood",
                 "near='" + "Indira" + "nagar" + ", <city>'", id="home-neighbourhood"),
])
def test_every_rule_catches_its_own_shape(label, sample):
    """Each rule fires on a sample of what it exists to ban."""
    matched = [name for name, _pattern, rx in _COMPILED if rx.search(sample)]
    assert label in matched, _ascii(
        "rule {0!r} did not fire on its own sample; it is inert".format(label))


#: Explicit ids: pytest derives a node id from the parameter VALUE, so an
#: unnamed case would print the planted token in clear in the test id -- the
#: exact republication this control exists to stop.
@pytest.mark.parametrize("planted", [
    # One line, two DIFFERENT rules.  The per-rule excerpt masked only the span
    # that rule matched, so the other token was printed in clear.
    pytest.param(
        "D:/" + "Anti" + "gravity" + "/" + "Project" + "_systemu_pro" + "/x",
        id="one-line-two-different-rules"),
    # One line, the SAME rule twice.  ``search`` finds the first match only, so
    # the second copy survived into the excerpt untouched.
    pytest.param(
        "cp /w/" + "quirky-" + "goodall" + "/a /w/" + "quirky-" + "goodall" + "/b",
        id="one-line-same-rule-twice"),
])
def test_a_redacted_excerpt_carries_no_banned_token(planted):
    """The fence's own output must never republish what it just found.

    A failure listing goes into CI logs and into commit messages.  Masking only
    the span of the rule being reported is not redaction: a line that trips two
    rules was emitted twice, and each copy printed the OTHER rule's token in
    clear.  So redaction is all-rules and all-occurrences, and it happens
    BEFORE the excerpt is trimmed or truncated.
    """
    assert [label for label, _pattern, rx in _COMPILED if rx.search(planted)], _ascii(
        "the planted sample tripped no rule, so this control proves nothing")
    redacted = _redact(planted)
    survivors = [label for label, _pattern, rx in _COMPILED if rx.search(redacted)]
    assert not survivors, _ascii(
        "rule(s) {0!r} still match the REDACTED excerpt; the fence's failure "
        "message is a fresh copy of the token it found".format(survivors))


def test_redaction_precedes_truncation():
    """A token past the 160-character cap must not be reachable by truncation.

    DEC-31: redact BEFORE any slice.  If the cap were applied to the raw line
    and the mask afterwards, a token sitting beyond the cap would be cut rather
    than masked -- which looks identical on a short line and fails open on a
    long one.  The planted token here sits well past the cap.
    """
    planted = ("x" * 400) + " " + "anti" + "gravity" + " tail"
    redacted = _redact(planted)
    assert "<<REDACTED>>" in redacted, _ascii(
        "a token beyond the excerpt cap was not masked; redaction ran after "
        "the slice")
    assert not [label for label, _pattern, rx in _COMPILED if rx.search(redacted)]


@pytest.mark.parametrize("identifier", [
    "Asia/" + "Kol" + "kata",
    'tz = ZoneInfo("Asia/' + "Kol" + 'kata")',
    "Asia/Tokyo",
    "America/New_York",
    "UTC",
])
def test_no_rule_flags_an_iana_timezone_identifier(identifier):
    """A zone id is a functional key, not a place-name disclosure.

    The city rules name settlements.  Widening one to a region or a country --
    the obvious "be thorough" edit -- would start flagging ``Asia/<zone>``, and
    the only way back to green would be to delete a timezone the product needs
    to do real work.  The boundary is pinned here rather than left to the next
    author's judgement.
    """
    hits = [label for label, _pattern, rx in _COMPILED if rx.search(identifier)]
    assert not hits, _ascii(
        "rule(s) {0!r} flagged the IANA timezone identifier {1!r}; a zone id is "
        "functional and must never be scrubbed".format(hits, identifier))


@pytest.mark.parametrize("legitimate", [
    "sk-super-secret-token-abcdef",
    "a-b-c-d-123456",
    "text-embedding-3-small",
    "application/x-www-form-urlencoded",
])
def test_the_codename_shape_rule_spares_legitimate_hyphenated_tokens(legitimate):
    """The shape rule means a WHOLE two-word codename, not any hyphenated tail.

    ``\\b`` let the bare shape match the last three segments of any longer
    hyphenated string; the synthetic API key in the secrets-at-rest tests was a
    real false positive of that form.
    """
    shape = [rx for name, _pattern, rx in _COMPILED
             if name == "development worktree codename shape"][0]
    found = shape.search(legitimate)
    assert found is None, _ascii(
        "the codename shape rule flagged the legitimate token {0!r} at {1!r}".format(
            legitimate, found.group(0) if found else ""))


# ---------------------------------------------------------------------------
# Fragment controls.  Appended; every control above is untouched.
#
# No assertion below lets pytest introspect a fragment or a codename into its
# failure output: values are reduced to booleans / indices first, for the same
# reason the fence masks its own excerpts.
# ---------------------------------------------------------------------------

#: The path a planted text is scanned under: any tracked-looking path that is
#: NOT the fence file.
_SIBLING = "tests/test_some_other_shipped_file.py"


def _codename_tokens() -> list:
    return [pattern for label, pattern in _BANNED
            if label == "development worktree codename"]


def _halves(token: str) -> tuple:
    """``token`` cut in the middle, the way a docstring split it."""
    mid = len(token) // 2
    return token[:mid], token[mid:]


def test_the_fragment_rule_is_derived_and_live():
    """The rule exists, is in the scan set, and is built only from the denylist."""
    plain = " ".join(_plain_tokens()).lower()
    derived_from_denylist = all(part in plain for part in _FRAGMENTS)
    long_enough = all(len(part) >= _FRAGMENT_MIN_LEN for part in _FRAGMENTS)
    not_generic = not any(part in _GENERIC_SEGMENTS for part in _FRAGMENTS)
    in_scan_set = _FRAGMENT_LABEL in [label for label, _p, _rx in _COMPILED]
    assert _FRAGMENTS and derived_from_denylist and long_enough and not_generic, _ascii(
        "the fragment set is empty, or holds something the denylist did not "
        "yield, or something shorter than {0} characters, or a generic word".format(
            _FRAGMENT_MIN_LEN))
    assert in_scan_set, _ascii("the fragment rule is not in the scan set")


def test_every_codename_keeps_a_distinctive_fragment():
    """No codename may end up caught ONLY whole.

    A codename whose every segment is an ordinary word has nothing to match as
    a fragment, which is the hole this rule exists to close.  If a new codename
    is added whose segments are all generic, this goes red -- decide it on
    purpose rather than letting the fence quietly under-cover it.
    """
    codenames = _codename_tokens()
    assert len(codenames) >= 3, _ascii("the codename denylist shrank below its known size")
    uncovered = [index for index, token in enumerate(codenames)
                 if not set(_segments(token)) & set(_FRAGMENTS)]
    assert not uncovered, _ascii(
        "codename entries {0!r} have no distinctive fragment".format(uncovered))


def test_every_generic_segment_is_a_live_exclusion():
    """An exclusion that excludes nothing is a stale excuse, not a rule."""
    met = set()
    for token in _plain_tokens():
        parts = _segments(token)
        if len(parts) < 2:
            continue
        met.update(part for part in parts if len(part) >= _FRAGMENT_MIN_LEN)
    dead = sorted(set(_GENERIC_SEGMENTS) - met)
    assert not dead, _ascii(
        "{0} entr(ies) of _GENERIC_SEGMENTS match no derived segment".format(len(dead)))


def test_a_planted_codename_half_is_caught_by_the_fragment_rule_alone():
    """The incident, re-enacted: a codename split across two backticked halves.

    Each half is planted on its own and both together, in a file that is NOT
    the fence.  Only the FRAGMENT rule may fire -- the whole-token rules and
    the shape rule cannot, which is exactly why the split shipped -- and the
    excerpt the fence would print must carry no rule's match.
    """
    codenames = _codename_tokens()
    assert codenames, _ascii("no codename tokens to plant")
    failures = []
    for index, token in enumerate(codenames):
        first, second = _halves(token)
        plants = {
            "both-halves": "see `" + first + "` / `" + second + "` in the notes",
            "second-half": second,
        }
        for kind, planted in plants.items():
            hits = _text_offenders(_SIBLING, planted)
            labels = {label for _path, _n, label, _x in hits}
            if labels != {_FRAGMENT_LABEL}:
                failures.append((index, kind, "wrong rules fired"))
                continue
            for _path, _n, _label, excerpt in hits:
                if any(rx.search(excerpt) for _l, _p, rx in _COMPILED):
                    failures.append((index, kind, "excerpt republishes a match"))
    assert not failures, _ascii(
        "planted half not caught cleanly: {0!r}".format(failures))


def test_a_fragment_inside_a_longer_word_is_still_caught():
    """Substring, not word: a fragment glued into other text is still a hit."""
    token = _codename_tokens()[0]
    fragment = [part for part in _segments(token) if part in _FRAGMENTS][0]
    planted = "xx" + fragment + "yy"
    hits = _text_offenders(_SIBLING, planted)
    assert hits, _ascii("a fragment embedded in a longer word was not caught")


def test_the_fragment_exemption_is_exactly_this_file_and_only_the_fragment_pass():
    """The exemption is one path, it is this file, and it spares fragments only."""
    assert _FRAGMENT_EXEMPT == frozenset({_SELF}), _ascii(
        "the fragment exemption grew beyond the fence file")
    assert (_ROOT / _SELF).resolve() == Path(__file__).resolve(), _ascii(
        "the exempt path is not this file")
    assert _SELF in _tracked_files(), _ascii("the fence file is not git-tracked")

    token = _codename_tokens()[0]
    fragment = [part for part in _segments(token) if part in _FRAGMENTS][0]
    # The fragment pass is skipped for this file ...
    assert _text_offenders(_SELF, fragment) == [], _ascii(
        "the fence file was not exempted from the fragment pass")
    # ... but a COMPLETE name in this file is still caught by the whole-token rule.
    whole = _text_offenders(_SELF, "x = '" + token + "'")
    assert any(label == "development worktree codename"
               for _path, _n, label, _x in whole), _ascii(
        "a complete codename in the fence file slipped past the whole-token rule")


def test_a_redacted_fragment_line_carries_no_match():
    """Fragments are masked by ``_redact`` like every other rule (DEC-31)."""
    token = _codename_tokens()[-1]
    fragment = [part for part in _segments(token) if part in _FRAGMENTS][0]
    planted = ("y" * 300) + " `" + fragment + "` tail"
    redacted = _redact(planted)
    assert "<<REDACTED>>" in redacted, _ascii("a fragment past the cap was not masked")
    survivors = [label for label, _p, rx in _COMPILED if rx.search(redacted)]
    assert not survivors, _ascii("a fragment survived redaction")


@pytest.mark.parametrize("ordinary", [
    pytest.param("an expensive and extensive objective of the project",
                 id="english-and-domain-words"),
    pytest.param("git worktrees at the same version are different code",
                 id="plural-worktrees"),
    pytest.param("the claude and systemu packages", id="product-identifiers"),
    pytest.param("Asia/Tokyo, America/New_York, UTC", id="zone-ids"),
])
def test_the_fragment_rule_spares_ordinary_prose(ordinary):
    """Generic segments stay out of the fragment set, or the fence cries wolf."""
    hits = _text_offenders(_SIBLING, ordinary)
    assert not hits, _ascii(
        "the fragment pass flagged ordinary prose ({0} hit(s))".format(len(hits)))
