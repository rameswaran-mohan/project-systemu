"""R-A16 / IMPL-5 — the taint-match corpus BOUND and the reshape canonicalisation.

Two halves of one change; they pull against each other and are pinned together.

THE BOUND. ``_tainted_fact_texts`` built its corpus from ``build_profile``, which
carries EVERY non-superseded fact — so the clamp matched against every content_derived
fact ever recorded. Measured on a 40-value panel of ordinary parameter values ("json",
"csv", "admin", "editor", "report", ...) against ordinary English facts, with no
adversarial construction, the clamp rate ran::

     facts |  BEFORE  |  AFTER
    -------+----------+--------
         0 |    0.0%  |    0.0%
        20 |   57.5%  |   57.5%
        40 |   85.0%  |   65.0%
        80 |   92.5%  |   50.0%
       320 |   92.5%  |   55.0%

and on an AGED vault — old facts about everything the operator ever did, plus a current
20 about something else — BEFORE reached 92.5% while the prompts only ever carried the
recent 20. Because ``_UNTRUSTED_ABSENT_SOURCES`` includes ``auto_extract``, that corpus
is non-empty in any install where extraction ever ran, and the growth is
self-amplifying (over-clamp -> more asks -> §5.9 promotes more content_derived answers
-> bigger corpus). The bound is PRINCIPLED, not a tuning knob: a value cannot have
laundered through the model unless the model SAW it, so the corpus is the
prompt-rendered set.

THE CANONICALISATION. The raw-text match caught only case and surrounding whitespace.
Separator swaps, a trailing period, quotes, URL-encoding and a zero-width space all
LAUNDERED — none of which is exotic (quoting and URL-encoding are ordinary model
behaviour, so the clamp failed open on BENIGN output), and each is attacker-reachable
because whoever authors the content picks the stored form.

The rows at corpus size <= 20 above are the tuning evidence: the window is inactive
there, so those columns isolate the canonicalisation, and it cost ZERO additional false
positives while closing seven reshape classes.
"""
from __future__ import annotations

import pytest

from systemu.runtime import requirement_binder as rb
from systemu.runtime import situational_inventory as si


def _bc(facts, *, origin="content_derived", source="auto_extract", vault=None):
    """A minimal bind context carrying ``facts`` as user_facts."""
    class _BC:
        pass
    bc = _BC()
    bc.situation = {"profile": {"user_facts": [
        {"fact": f, "source": source, "origin_class": origin,
         "confidence": 0.9, "tags": []} for f in facts]}}
    bc.vault = vault
    bc.tool_name = "t"
    return bc


STORED = "acct-99-attacker"
FACT = f"The transfer account on file is {STORED} for this vendor."


# ── the SECURITY direction: a laundered verbatim value must still clamp ────────

@pytest.mark.parametrize("label,emitted", [
    ("verbatim",          "acct-99-attacker"),
    ("uppercase",         "ACCT-99-ATTACKER"),
    ("surrounding space", "  acct-99-attacker  "),
    ("separator - to _",  "acct_99_attacker"),
    ("separator - to sp", "acct 99 attacker"),
    ("trailing period",   "acct-99-attacker."),
    ("trailing comma",    "acct-99-attacker,"),
    ("double-quoted",     '"acct-99-attacker"'),
    ("single-quoted",     "'acct-99-attacker'"),
    ("smart-quoted",      "“acct-99-attacker”"),
    ("backticked",        "`acct-99-attacker`"),
    ("url-encoded",       "acct%2D99%2Dattacker"),
    ("zero-width space",  "acct-99-​attacker"),
    ("zero-width joiner", "acct‍-99-attacker"),
    ("quoted + period",   '"acct-99-attacker".'),
    ("upper + underscore", "ACCT_99_ATTACKER"),
    # URL-encoding that REVEALS A WRAPPER once decoded (``%22``→``"``, ``%27``→``'``,
    # ``%2E``→``.``). The peel that removes such a wrapper runs only AFTER the URL-decode
    # — the SECOND ``_strip_wrappers`` call in ``_canonical_taint_form``. The plain
    # ``url-encoded`` row above decodes to a SEPARATOR, which the separator fold collapses
    # regardless, so it never exercised that second peel; each of these does.
    ("url + double-quote",   "%22acct-99-attacker%22"),
    ("url + single-quote",   "%27acct-99-attacker%27"),
    ("url + trailing dot",   "acct-99-attacker%2E"),
    ("url wrap + encoded -", "%22acct%2D99%2Dattacker%22"),
])
def test_reshaped_value_still_clamps(label, emitted):
    """Every one of these LAUNDERED before the canonicalisation. A model that requotes,
    re-separates, punctuates or URL-encodes a tainted value must not thereby launder it
    into a trusted silent bind."""
    assert rb._provided_value_is_content_seeded(_bc([FACT]), emitted), label


def test_security_direction_survives_the_bound():
    """The corpus bound must not drop a fact the PROMPTS still carry. The tainted fact
    sits at the newest end — inside every renderer window — so the clamp holds no matter
    how much older history precedes it."""
    older = [f"An unrelated ordinary fact number {i}." for i in range(500)]
    assert rb._provided_value_is_content_seeded(_bc(older + [FACT]), STORED)


# ── the LINE: canonicalisation must not become a substring match ──────────────

@pytest.mark.parametrize("label,emitted", [
    ("sibling value",      "acct-98-attacker"),
    ("separator DELETED",  "acct99attacker"),
    ("superstring",        "acct-99-attacker-extra"),
    ("unrelated",          "totally-different-value"),
    ("interior collapsed", "acct.99.attacker"),
])
def test_canonicalisation_does_not_become_a_substring_match(label, emitted):
    """THE LINE THIS MUST NOT CROSS. Every canonicalisation step is a total rewrite to a
    canonical string; none DELETES a structural character, and the match over the result
    stays token-delimited. A value that merely resembles the stored one must not clamp —
    otherwise the loosening manufactures exactly the over-asks the bound just paid to
    remove.

    ``acct99attacker`` is the load-bearing case: folding the separator CLASS is what
    makes ``-``/``_``/space interchangeable, and the tempting over-reach is to fold the
    separator away entirely. That would make every de-separated form of every tainted
    token match, which is a substring match wearing a canonicaliser's clothes."""
    assert not rb._provided_value_is_content_seeded(_bc([FACT]), emitted), label


def test_value_inside_a_longer_word_does_not_clamp():
    """The pre-existing token-delimiting guarantee, re-pinned AFTER canonicalisation:
    the separator fold must not have destroyed the boundary check."""
    bc = _bc(["The accounting department reconciles this monthly."])
    assert not rb._provided_value_is_content_seeded(bc, "account")
    assert not rb._provided_value_is_content_seeded(bc, "count")


def test_canonical_form_never_collapses_path_structure():
    """``/`` is deliberately NOT in the separator class.

    A path separator is STRUCTURE, not form: ``out/report.md`` and ``out-report.md`` are
    two different values, and a directory boundary is not interchangeable with a hyphen
    the way ``-``/``_``/space are. Folding ``/`` into the class would equate them — and
    since every path leaf in every schema flows through here, that is the step that would
    put the over-ask rate straight back where the bound just brought it down from."""
    canon = rb._canonical_taint_form
    assert canon("out/report.md") != canon("out")
    assert canon("out/report.md") != canon("report.md")
    assert canon("out/report.md") != canon("outreport.md")
    # the load-bearing pair: a path separator must not fold onto the separator CLASS
    assert canon("out/report.md") != canon("out-report.md")
    assert canon("out/report.md") != canon("out report.md")
    assert canon("a/b/c") != canon("a_b_c")
    # ...but a backslash path and a forward-slash path ARE the same value.
    assert canon(r"out\report.md") == canon("out/report.md")


def test_path_value_does_not_clamp_against_a_separator_reshaped_fact():
    """The same rule end-to-end: a fact mentioning ``exports-2026-q3`` must not clamp a
    genuine path ``exports/2026/q3``."""
    bc = _bc(["The archive label is exports-2026-q3 for this cycle."])
    assert not rb._provided_value_is_content_seeded(bc, "exports/2026/q3")


def test_canonical_form_is_never_empty_for_punctuation_only_values():
    """``_strip_wrappers`` must never strip to "".

    An empty needle is found inside EVERY fact in the corpus, so an all-punctuation value
    would clamp every leaf in the schema at once. The length gate in
    ``_provided_value_is_content_seeded`` also rejects "", so this is defence in depth —
    but it is the layer that keeps the invariant true for any future caller of
    ``_canonical_taint_form`` that does not re-check length."""
    for v in ("...", "().,;", "?!?!?!", "'''", '"""'):
        assert rb._canonical_taint_form(v) != "", v


# ── the wrapper-peeling half has ONE owner, and this file is routed to it ─────
#
# ``requirement_binder`` used to redeclare ``_QUOTE_PAIRS``/``_TRAILING_PUNCT`` and
# redefine ``_strip_wrappers`` with the SAME values and logic, under a comment asking a
# future reader to keep the constants "byte-identical" to ``replay_metrics``'. That ask
# was never a control — and overclaimed even as written: the two copies' comments and the
# helper's docstring already differed, so only the executable logic was ever identical,
# and either could drift with nothing to fail. The duplicate is now deleted and the helper
# imported, which is checked from two DIFFERENT directions below — an agreement check
# ("both copies behave the same") is deliberately NOT one of them, because it stays
# green whether the code is shared or merely coincidentally equal, which is precisely
# how the duplication survived this long.

def test_strip_wrappers_is_the_one_shared_object_not_a_local_copy():
    """IDENTITY PIN — nothing can hide a reimplementation behind the name.

    Fails the moment ``requirement_binder`` grows its own ``_strip_wrappers`` again, even
    one that behaves identically today. The two constants are pinned absent for the same
    reason: re-declaring them is the first half of how the fork previously reappeared."""
    from systemu.runtime import replay_metrics as rm
    assert rb._strip_wrappers is rm._strip_wrappers, (
        "requirement_binder has re-forked _strip_wrappers; it must be the imported one")
    assert "_QUOTE_PAIRS" not in vars(rb), "a mirrored _QUOTE_PAIRS is back"
    assert "_TRAILING_PUNCT" not in vars(rb), "a mirrored _TRAILING_PUNCT is back"


def test_canonical_taint_form_actually_routes_through_the_shared_helper(monkeypatch):
    """CALL-SITE PIN — nothing can keep the name while routing past it.

    The identity pin alone would stay green if someone kept the import and INLINED the
    peeling, leaving the shared helper imported-but-unused. So mutate the helper the
    module resolves and require the canonical form to move with it. Both call sites are
    covered: the plain path, and the second peel after URL-decoding.

    Each site is pinned for CALLED and for RESULT-USED separately — a call whose return
    value is thrown away is exactly the "routes past it" shape and passes a CALLED-only
    check. RESULT-USED needs care at site #2: the plain peel (site #1) has already stamped
    one marker onto the very string site #2 receives, so the marker's mere PRESENCE proves
    nothing there. The sentinel stamps once per call, so site #2's result being used shows
    up as the marker appearing TWICE; a mutation that keeps the decode but drops the peel
    leaves it once."""
    calls = []

    def _sentinel(s):
        calls.append(s)
        return s + "ZZQQ"

    monkeypatch.setattr(rb, "_strip_wrappers", _sentinel)

    out = rb._canonical_taint_form("'acct-99-attacker'")
    assert calls, "_canonical_taint_form never called _strip_wrappers"
    assert "zzqq" in out, f"_strip_wrappers' result was discarded: {out!r}"

    # Site #2 — the peel AFTER url-decode. A url-encoded WRAPPER (``%22`` → ``"``) is what
    # reaches it: the wrapper only appears once the ``%`` is decoded, so site #1 cannot
    # have removed it. CALLED is ``len(calls) >= 2``; RESULT-USED is the marker COUNT, not
    # its presence — site #1 already stamped one ``zzqq`` onto this same string, so
    # ``"zzqq" in out2`` stays true even when site #2's return is discarded. Two stamps
    # means site #2's result reached the output; one means the decode was kept but the
    # peel dropped.
    calls.clear()
    out2 = rb._canonical_taint_form("%22acct-99-attacker%22")
    assert len(calls) >= 2, (
        f"the post-URL-decode peel no longer routes through the helper: {calls!r}")
    assert out2.count("zzqq") >= 2, (
        f"site #2's _strip_wrappers result was discarded (decode kept, peel dropped): {out2!r}")


def test_short_values_still_bypass_the_match():
    """``_MIN_TAINT_MATCH_LEN`` is measured on the CANONICAL form, so a value that only
    reaches the threshold via its wrapper characters is still too short to match."""
    assert not rb._provided_value_is_content_seeded(_bc(["The id is ab here."]), '"ab"')


# ── the residuals, CHARACTERIZED so closing one is a visible edit ─────────────

def test_residual_value_split_across_two_facts_still_launders():
    """DOCUMENTED RESIDUAL, pinned so it cannot be mistaken for a closed case.

    An author controlling TWO stored facts can split a value across them and have the
    model rejoin it. Closing this means matching against CONCATENATIONS of the corpus,
    which is not a smaller version of canonicalisation: joining two facts manufactures
    adjacencies present in neither, so every seam is a new false-match site and the
    candidate set grows quadratically in a corpus this change just paid to bound.

    If a future change closes it, this test fails — which is the point. Flip it to an
    assertTrue then, deliberately, rather than discovering the behavior drifted."""
    bc = _bc(["The account prefix on file is acct-99.",
              "The vendor suffix for that account is attacker."])
    assert not rb._provided_value_is_content_seeded(bc, STORED)


def test_residual_plus_encoded_space_still_launders():
    """DOCUMENTED RESIDUAL. ``unquote`` does not decode ``+`` (only ``unquote_plus``
    does), and applying the plus rule unconditionally would fold genuine ``+``
    characters — a version string, an email tag address — into separators. Narrow, and
    the safe direction."""
    bc = _bc(["The build label is acct 99 attacker for this run."])
    assert not rb._provided_value_is_content_seeded(bc, "acct+99+attacker")


def test_fragment_of_a_compound_value_clamps_by_design():
    """Not a residual — a COST, named so it is not rediscovered as a bug.

    ``-``/``_`` are token delimiters, so a prefix or suffix of a stored compound value
    matches. That is the fail-untrusted reading (a fragment lifted out of tainted content
    is itself content-derived) and it pre-dates this change, but it is a real contributor
    to the measured clamp rate and belongs in the record."""
    bc = _bc([FACT])
    assert rb._provided_value_is_content_seeded(bc, "acct-99")
    assert rb._provided_value_is_content_seeded(bc, "99-attacker")


# ── the BOUND itself ──────────────────────────────────────────────────────────

def test_corpus_is_bounded_to_the_prompt_window():
    """The over-ask cost must stop growing with vault age. A tainted value that has aged
    out of EVERY prompt window no longer clamps — the model was never shown it, so it
    cannot have laundered through the prompt channel this clamp exists to close."""
    aged = [FACT] + [f"A newer unrelated fact number {i}." for i in
                     range(rb._PROMPT_FACT_WINDOW + 5)]
    assert not rb._provided_value_is_content_seeded(_bc(aged), STORED)


def test_window_is_applied_before_the_taint_filter():
    """Slicing must select the most-recent N of ALL facts, then filter to tainted — the
    order the renderers use. Taking the last N TAINTED facts instead would silently
    re-widen the corpus past what any prompt carried.

    Here the tainted fact is old and is followed by a full window of CLEAN facts. If the
    window were applied after the taint filter, the tainted fact would survive as the
    only tainted row and still clamp."""
    class _BC:
        pass
    bc = _BC()
    facts = [{"fact": FACT, "source": "auto_extract",
              "origin_class": "content_derived", "confidence": 0.9, "tags": []}]
    facts += [{"fact": f"Operator-authored fact {i}.", "source": "explicit_user",
               "origin_class": "operator", "confidence": 1.0, "tags": []}
              for i in range(rb._PROMPT_FACT_WINDOW + 5)]
    bc.situation = {"profile": {"user_facts": facts}}
    bc.vault = None
    bc.tool_name = "t"
    assert not rb._provided_value_is_content_seeded(bc, STORED)


def test_corpus_never_exceeds_the_window():
    many = [f"Tainted ordinary fact number {i} about acct-{i}-x." for i in range(200)]
    assert len(rb._tainted_fact_texts(_bc(many))) <= rb._PROMPT_FACT_WINDOW


def test_render_cap_matches_binder_window():
    """THE LOAD-BEARING PIN. The bound is only sound because no renderer shows the model
    a fact outside the window. ``render_situation_for_prompt`` json-dumps the whole
    profile, so it had to be capped too; if either constant is raised alone, the binder
    starts skipping facts the planner still sees and the cost bound becomes a laundering
    hole. Fails on drift, in either direction."""
    assert si._PROMPT_FACT_BUDGET == rb._PROMPT_FACT_WINDOW


def test_render_caps_the_profile_without_mutating_the_report():
    """Only the PROMPT view narrows. The report object is shared with the binder's
    source #4 and cached on the execution snapshot — mutating it would silently stop
    older facts from BINDING, a behavior regression well outside this change."""
    facts = [{"fact": f"Fact {i}.", "source": "explicit_user"} for i in range(60)]
    report = {"profile": {"user_facts": facts, "name": "R"}, "services": []}
    out = si.render_situation_for_prompt(report)

    assert len(report["profile"]["user_facts"]) == 60, "caller's report was MUTATED"
    assert "Fact 59." in out, "the most-recent fact must survive the cap"
    assert "Fact 0." not in out, "an out-of-window fact must not reach the prompt"
    assert '"name": "R"' in out or '"name":"R"' in out, "spine fields must survive"


def test_render_leaves_small_and_malformed_profiles_untouched():
    small = {"profile": {"user_facts": [{"fact": "only one"}]}}
    assert "only one" in si.render_situation_for_prompt(small)
    for bad in ({}, {"profile": None}, {"profile": {"user_facts": "nope"}}, None):
        si.render_situation_for_prompt(bad)          # must not raise


# ── taint definition unchanged, and defensive behavior preserved ──────────────

def test_untrusted_absent_source_still_taints_within_the_window():
    """The legacy unstamped ``auto_extract`` corpus stays tainted — the bound narrows
    WHICH facts are considered, never the definition of taint."""
    bc = _bc([FACT], origin=None, source="auto_extract")
    assert rb._provided_value_is_content_seeded(bc, STORED)


def test_operator_facts_are_not_taint():
    bc = _bc([FACT], origin="operator", source="explicit_user")
    assert not rb._provided_value_is_content_seeded(bc, STORED)


def test_malformed_profile_never_raises_and_never_clamps():
    for situation in ({}, {"profile": None}, {"profile": {"user_facts": None}},
                      {"profile": {"user_facts": [None, 5, "str"]}}):
        class _BC:
            pass
        bc = _BC()
        bc.situation = situation
        bc.vault = None
        bc.tool_name = "t"
        assert rb._provided_value_is_content_seeded(bc, STORED) is False


def test_unstringable_value_never_raises():
    class _Boom:
        def __str__(self):
            raise RuntimeError("no")
    assert rb._canonical_taint_form(_Boom()) == ""
    assert not rb._provided_value_is_content_seeded(_bc([FACT]), _Boom())


def test_mangled_percent_escape_does_not_mojibake_two_values_together():
    """``unquote`` runs with errors="strict" so a broken escape leaves the value alone
    rather than silently folding two distinct values onto one canonical form."""
    assert rb._canonical_taint_form("100% cotton") == "100% cotton".casefold()
    assert "%appdata%" in rb._canonical_taint_form("%APPDATA%/x")


# ── the realized-clamp count is surfaced ──────────────────────────────────────

def test_clamp_is_counted_and_surfaced_in_the_ask_report(tmp_path):
    """The clamp's friction was previously invisible: nothing counted it, so the only
    way to notice it had begun firing on ordinary values was for systemu to feel more
    annoying. Growth must be readable as a number."""
    from systemu.runtime import replay_metrics as rm

    class _V:
        root = tmp_path
    vault = _V()

    assert rm.taint_clamp_report(vault)["clamp_count"] == 0
    assert rb._provided_value_is_content_seeded(_bc([FACT], vault=vault), STORED)

    rep = rm.taint_clamp_report(vault)
    assert rep["clamp_count"] == 1
    assert rep["window"] == rb._PROMPT_FACT_WINDOW
    assert rep["max_corpus_size"] >= 1

    lines = rm.format_taint_clamp(rep)
    assert any("taint clamp" in ln.lower() for ln in lines)


def test_recorded_clamp_never_stores_the_value(tmp_path):
    """A clamped value is content-derived by definition and may hold anything, including
    a secret. The corpus records the FACT of a clamp, never the value."""
    from systemu.runtime import replay_metrics as rm

    class _V:
        root = tmp_path
    vault = _V()
    secret = "acct-99-attacker"
    rb._provided_value_is_content_seeded(_bc([FACT], vault=vault), secret)

    blob = (tmp_path / "audit" / "taint_clamp_corpus.jsonl").read_text(encoding="utf-8")
    assert secret not in blob
    assert "attacker" not in blob


def test_recording_failure_never_changes_the_bind_decision(monkeypatch):
    """The recorder sits on a SAFETY path. A metrics hiccup must not raise into the
    per-leaf handler — which would degrade the leaf to a spurious gap, turning an
    observability failure into a BEHAVIOR change (a clamp that should have fired
    becoming a missing-leaf gap).

    Patched at the recorder itself rather than handed a broken vault: ``record_taint_clamp``
    swallows its own failures, so a broken vault never reaches the binder's handler and
    the test would pass no matter what the binder did. Two independent swallows, and this
    pins the BINDER's one."""
    from systemu.runtime import replay_metrics as rm

    def _boom(*a, **k):
        raise RuntimeError("metrics backend is gone")
    monkeypatch.setattr(rm, "record_taint_clamp", _boom)

    class _V:
        root = "/nonexistent"
    bc = _bc([FACT], vault=_V())
    assert rb._provided_value_is_content_seeded(bc, STORED) is True


def test_recorder_swallows_a_broken_vault():
    """And the recorder's OWN swallow, pinned separately — a vault whose root raises
    must not turn into an exception on the bind path."""
    class _ExplodingVault:
        @property
        def root(self):
            raise RuntimeError("vault is gone")
    bc = _bc([FACT], vault=_ExplodingVault())
    assert rb._provided_value_is_content_seeded(bc, STORED) is True


def test_record_taint_clamp_is_defensive_on_its_own():
    """``record_taint_clamp`` pinned DIRECTLY, not through the binder.

    The binder's ``_record_clamp`` swallows too, so the two guards are redundant and no
    end-to-end test can observe the inner one failing — a mutation removing it passes
    every behavioral pin. Called here with no binder in the way, so the inner guard has
    its own reason to exist and its own failure."""
    from systemu.runtime import replay_metrics as rm

    class _ExplodingVault:
        @property
        def root(self):
            raise RuntimeError("vault is gone")

    rm.record_taint_clamp(_ExplodingVault(), corpus_size=1, tool_name="t")
    rm.record_taint_clamp(None, corpus_size=1, tool_name="t")
    rm.record_taint_clamp(object(), corpus_size=1, tool_name="t")
    # and the readers, on a vault whose corpus does not exist
    assert rm.load_taint_clamp_corpus(_ExplodingVault()) == []
    assert rm.taint_clamp_report(_ExplodingVault())["clamp_count"] == 0


def test_corrupt_corpus_row_never_breaks_the_shipped_ask_metric(tmp_path):
    """A corrupt row in this observability-only sidecar must not take down
    ``avoidable_ask_report`` — a SHIPPED DEC-7 metric that documents "Never raises".

    ``record_taint_clamp`` writes ints, but this reads a file on disk that can be
    truncated or hand-edited like any other corpus here, and a bare
    ``int(row["corpus_size"])`` over a non-numeric row raises ValueError straight through
    the report."""
    from systemu.runtime import replay_metrics as rm

    class _V:
        root = tmp_path
    vault = _V()

    audit = tmp_path / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "taint_clamp_corpus.jsonl").write_text(
        '{"corpus_size": 3, "tool_name": "a"}\n'
        '{"corpus_size": "not-a-number", "tool_name": "b"}\n'
        '{"corpus_size": null}\n'
        'not json at all\n'
        '{"corpus_size": 7, "tool_name": "c"}\n',
        encoding="utf-8")

    rep = rm.taint_clamp_report(vault)
    assert rep["clamp_count"] == 4          # the 4 parseable rows
    assert rep["max_corpus_size"] == 7      # the bad row costs its own count, nothing more
    rm.avoidable_ask_report(vault)          # must not raise
    rm.format_taint_clamp(rep)


def test_no_vault_still_clamps():
    """The clamp is the control; the count is only observability. A run with no vault
    threaded must still clamp."""
    assert rb._provided_value_is_content_seeded(_bc([FACT], vault=None), STORED)
