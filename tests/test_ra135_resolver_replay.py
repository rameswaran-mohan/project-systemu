"""R-A13.5 — the ASK-side resolver replay + the `fixtures/field/` scenario corpus.

§10/IMPL-15 requires the avoidable-ask audit to be a DETERMINISTIC POST-HOC REPLAY
("re-run inventory/discovery/resolver with the operator's answer known — never an
LLM judgment"). The forge side already was one. The ask side was an AGGREGATOR over
`ask_corpus.jsonl`, and said so honestly in its own docstring.

These tests pin the replay that closes that gap, and — as importantly — they pin the
BOUNDARY: what the replay can decide, what it cannot, and why the aggregator it sits
beside is still not a replay.

DEC-27 shape (this packet): the DENOMINATOR is a committed `roster.json`, never the
observed glob; `definitive` is a DERIVED property of :class:`ReplayReport`, never a
stored flag; both directions of roster/disk drift (`missing`, `extra`) fail closed;
and `format_resolver_replay` accepts ONLY a `ReplayReport` — a bare dict/None is a
`TypeError` at the boundary.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from systemu.runtime import resolver_replay as rr
from systemu.runtime.resolver_replay import (
    AVOIDABLE,
    NECESSARY,
    ROSTER_FILENAME,
    UNASSESSABLE,
    FixtureError,
    ReplayReport,
    default_corpus_dir,
    format_resolver_replay,
    load_corpus,
    load_roster,
    load_scenario,
    replay_scenario,
    resolver_replay_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CORPUS = REPO_ROOT / "fixtures" / "field"


# ── helpers ──────────────────────────────────────────────────────────────────
def _fixture(**over):
    """A minimal VALID fixture dict; override any key."""
    base = {
        "schema_version": 1,
        "id": "t-scenario",
        "title": "t",
        "objective": {"id": 1, "goal": "do the thing", "success_criteria": "done"},
        "capability": {
            "name": "t_tool",
            "parameters_schema": {"widget": {"type": "string", "description": "a widget"}},
            "effect_tags": [],
        },
        "situation": {"services": [], "capabilities": [], "roots": [],
                      "credentials": [], "profile": {}, "declared_intents": []},
        "asks": [{"schema_path": "widget", "class": "decision",
                  "operator_answer": "w-1", "label": "necessary"}],
    }
    base.update(over)
    return base


def _write(dirpath: Path, name: str, payload) -> Path:
    p = dirpath / name
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload,
                 encoding="utf-8")
    return p


def _roster(dirpath: Path, names) -> Path:
    """Write a `roster.json` declaring exactly `names` (the committed denominator)."""
    return _write(dirpath, ROSTER_FILENAME,
                  {"schema_version": 1, "fixtures": sorted(names)})


def _corpus(dirpath: Path, files: dict) -> Path:
    """Write each `{name: payload}` fixture AND a roster.json that matches them
    exactly. This is the DEC-27 model of a *valid* corpus: fixtures plus a
    committed roster. Drift tests deliberately break the match afterwards."""
    for name, payload in files.items():
        _write(dirpath, name, payload)
    _roster(dirpath, list(files))
    return dirpath


def _copy_real_corpus(dst: Path) -> Path:
    """Copy the shipped corpus — fixtures AND roster.json — into a writable dir."""
    for p in sorted(REAL_CORPUS.glob("*.json")):
        (dst / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def _a_shipped_fixture_name(corpus_dir: Path) -> str:
    """One fixture filename from a corpus directory (never the roster)."""
    return next(p.name for p in sorted(corpus_dir.glob("*.json"))
               if p.name != ROSTER_FILENAME)


def _verdict_for(report, scenario_id, schema_path):
    for d in report.details:
        if d["scenario_id"] == scenario_id and d["schema_path"] == schema_path:
            return d
    raise AssertionError(f"no verdict for {scenario_id}:{schema_path}")


# ── 1. the shipped corpus is real, loads clean, and agrees with itself ───────
def test_real_field_corpus_exists_and_loads_without_error():
    """`fixtures/field/` is the DEC-11 corpus. It did not exist before R-A13.5 —
    the R-A9/R-A10 backfill debt. It exists now and every fixture parses."""
    assert REAL_CORPUS.is_dir(), f"{REAL_CORPUS} must exist (DEC-11 / PLAN-13)"
    scenarios, errors, declared, missing, extra = load_corpus(REAL_CORPUS)
    assert not errors, f"corpus load errors: {errors}"
    assert missing == frozenset() and extra == frozenset(), (missing, extra)
    assert len(scenarios) >= 8, "the backfill should carry the R-A9/R-A10 scenarios"


def test_real_corpus_replays_with_zero_mislabelled_and_zero_errors():
    """THE TRIPWIRE. Every fixture declares the verdict the spec says it should get;
    the replay computes its own. A disagreement means either the corpus drifted from
    reality or the resolver regressed — and that is the corpus's whole value."""
    report = resolver_replay_report(REAL_CORPUS)
    assert report.errors == (), report.errors
    assert report.mislabelled == (), (
        "a fixture's declared label disagrees with the replayed verdict; investigate "
        f"which side is wrong before editing either: {report.mislabelled}"
    )
    assert report.assessable > 0


# ── 1b. DEC-27: the roster IS the denominator, and it must match the fixtures ─
def test_field_roster_matches_fixtures():
    """DEC-27 drift tripwire. The committed roster and the fixtures on disk must be
    the SAME SET in BOTH directions. Any add/delete/rename that forgets to update
    `roster.json` (or vice-versa) turns THIS into a red suite at commit time, rather
    than a wrong denominator discovered later at replay.

    Detecting the drift never requires knowing WHICH file diverged — a single
    non-empty set-difference is enough — which is exactly why the denominator is an
    independent roster and not the observed glob (whose set-difference with itself is
    always empty)."""
    declared = load_roster(REAL_CORPUS)
    observed = rr._observed_fixtures(REAL_CORPUS)
    assert declared - observed == frozenset(), f"rostered but absent on disk: {declared - observed}"
    assert observed - declared == frozenset(), f"present on disk but unrostered: {observed - declared}"

    # and the same through the report path the operator actually reads
    report = resolver_replay_report(REAL_CORPUS)
    assert report.missing == frozenset() and report.extra == frozenset()
    assert report.declared == report.scenarios, "every rostered fixture must have loaded"
    assert report.declared == len(observed)


def test_declared_is_the_roster_size_not_the_observed_scenario_count(tmp_path):
    """`declared` must come from the roster, NOT from `len(scenarios)`. If it were
    the observed count they would move together and a dropped file could never read
    as `declared > scenarios` — the shrinkage would be invisible, which is the
    precise defect DEC-27 closes."""
    _corpus(tmp_path, {"a.json": _fixture(id="a"), "b.json": _fixture(id="b")})
    # delete b.json but leave it in the roster: declared stays 2, scenarios drops to 1
    (tmp_path / "b.json").unlink()
    r = resolver_replay_report(tmp_path)
    assert r.declared == 2, "the denominator is the committed roster, not the survivors"
    assert r.scenarios == 1
    assert r.missing == frozenset({"b.json"})
    assert r.definitive is False


# ── 2. THE discriminating case: this is what makes it a replay, not a proxy ──
def test_replay_separates_two_structurally_identical_asks_by_the_answer():
    """Two scenarios whose SITUATIONS are the same shape — both bind `output_dir`
    from the profile spine, both reach state='have', source='operator_profile'.

    They differ ONLY in what the operator answered. The replay must call one
    avoidable (the binder held exactly that value) and the other necessary (the
    binder held a DIFFERENT value, so asking was correct and a silent bind would
    have written to the wrong directory).

    No signal derived from "did it try to resolve?" can separate these two, which
    is precisely why §10 demands a replay.
    """
    report = resolver_replay_report(REAL_CORPUS)
    same = _verdict_for(report, "ra10-profile-spine-output-dir", "output_dir")
    diff = _verdict_for(report, "ra135-binder-held-a-different-value", "output_dir")

    # the two binds are genuinely indistinguishable on every non-value axis
    assert same["bound_state"] == diff["bound_state"] == "have"
    assert same["bound_source"] == diff["bound_source"] == "operator_profile"
    assert same["class"] == diff["class"]

    # ...yet the verdicts differ, and only the answer comparison could do that
    assert same["verdict"] == AVOIDABLE
    assert same["reason"] == rr.R_BOUND_THE_ANSWER
    assert diff["verdict"] == NECESSARY
    assert diff["reason"] == rr.R_BOUND_OTHER_VALUE


def test_the_aggregator_records_nothing_that_could_separate_that_pair():
    """The other half of the split verdict, as a TEST rather than a claim.

    `replay_metrics.record_ask` writes {kind, attempts_before, tool_attempts,
    blocked_signals, confidence}. There is no schema_path, no situation, no answer
    — the row does not even identify WHICH requirement was asked. So the two asks
    above serialise to byte-identical corpus rows and `avoidable_ask_report` cannot
    tell them apart at any sample size. That is a property of the recorded data, not
    of the counting, and it is why that module's "NON-DEFINITIVE PROXY" docstring is
    accurate and must survive.
    """
    from systemu.runtime import replay_metrics as rm

    fields = set(rm.record_ask.__kwdefaults__ or {})
    assert "schema_path" not in fields
    assert "situation" not in fields
    assert "answer" not in fields

    # and the honest docstring is still there, unedited by this release
    doc = rm.avoidable_ask_report.__doc__ or ""
    assert "NON-DEFINITIVE" in doc and "not a strict bound" in doc


# ── 3. the failure path must not be able to emit the healthy value ──────────
def test_rate_is_none_never_zero_when_the_corpus_cannot_be_measured(tmp_path):
    """`0.0` is the HEALTHY reading ("no avoidable asks"). An empty / missing /
    rosterless corpus must therefore not be able to produce it. A directory with no
    `roster.json` has no committed denominator, so it reads as INCOMPLETE, never as a
    clean 0%."""
    empty = tmp_path / "empty"
    empty.mkdir()                       # exists, but no roster.json and no fixtures
    r = resolver_replay_report(empty)
    assert r.rate is None, "a rosterless corpus must not read as a perfect 0%"
    assert r.assessable == 0
    assert r.definitive is False
    lines = "\n".join(format_resolver_replay(r))
    assert "INCOMPLETE" in lines and "not 0%" in lines.lower()
    assert not re.search(r"=\s*\d+%", lines), lines

    missing = resolver_replay_report(tmp_path / "does-not-exist")
    assert missing.rate is None
    assert missing.error_count == 1     # "corpus directory not found"
    assert missing.definitive is False


def test_all_unassessable_corpus_is_definitive_but_reports_no_rate(tmp_path):
    """Unassessable is excluded from numerator AND denominator, so a COMPLETE corpus
    of nothing but unassessable asks has no rate at all — not 0%, not 100% — yet it
    is genuinely `definitive` (it fully reconciled against its roster; there was
    simply nothing to score). This is the ONLY path to the "NO ASSESSABLE ASKS"
    render."""
    _corpus(tmp_path, {"s.json": _fixture(asks=[{
        "schema_path": "api_token", "class": "credential",
        "operator_answer": "STANDIN", "answer_is_synthetic": True,
        "label": "unassessable"}])})
    r = resolver_replay_report(tmp_path)
    assert r.unassessable_count == 1
    assert r.assessable == 0
    assert r.rate is None
    assert r.mislabelled == ()
    assert r.definitive is True, "a fully-reconciled corpus is definitive even with no rate"
    lines = "\n".join(format_resolver_replay(r))
    assert "NO ASSESSABLE ASKS" in lines and "not 0%" in lines.lower()


def test_unassessable_is_excluded_from_both_numerator_and_denominator(tmp_path):
    _corpus(tmp_path, {
        "a.json": _fixture(id="a", asks=[
            {"schema_path": "widget", "class": "decision",
             "operator_answer": "w-1", "label": "necessary"}]),
        "b.json": _fixture(id="b", asks=[
            {"schema_path": "api_token", "class": "credential",
             "operator_answer": "STANDIN", "answer_is_synthetic": True,
             "label": "unassessable"}]),
    })
    r = resolver_replay_report(tmp_path)
    assert r.total_asks == 2
    assert r.assessable == 1          # the credential ask is NOT in the denominator
    assert r.rate == 0.0             # a real, EARNED 0% — one necessary ask, definitive
    assert r.definitive is True
    assert r.unassessable_count == 1


# ── 4. the never-record-a-credential rule ───────────────────────────────────
@pytest.mark.parametrize("path,kind", [
    ("api_key", "input"),
    ("auth/api_key", "input"),
    ("db_password", "decision"),
    ("widget", "credential"),           # benign NAME, but credential-kind
])
def test_plaintext_answer_on_a_secret_path_is_refused_loudly(tmp_path, path, kind):
    """A fixture may never carry a credential value. The guard reuses the codebase's
    canonical secret marker (elicitation.is_secret_field), not a bespoke pattern, and
    it RAISES rather than silently dropping the ask — a silent drop would shrink the
    denominator and move the rate."""
    p = _write(tmp_path, "bad.json", _fixture(asks=[{
        "schema_path": path, "class": kind,
        "operator_answer": "hunter2-real-secret", "label": "unassessable"}]))
    with pytest.raises(FixtureError) as exc:
        load_scenario(p)
    assert "never record a credential" in str(exc.value)


def test_synthetic_standin_is_accepted_and_replays_as_unassessable(tmp_path):
    """The escape hatch is a DECLARED stand-in — and it does not buy a verdict:
    the binder never digests a secret leaf, so the ask stays unassessable.

    The capability must actually DECLARE the leaf and the situation must let it
    bind, or the ask would come back `no_requirement_emitted` / `missing` and this
    test would pass for the wrong reason.
    """
    p = _write(tmp_path, "ok.json", _fixture(
        capability={"name": "jira_sync",
                    "parameters_schema": {"jira_api_key": {"type": "string"}},
                    "effect_tags": []},
        situation={"services": [], "capabilities": [], "roots": [],
                   "credentials": ["jira"], "profile": {}, "declared_intents": []},
        asks=[{"schema_path": "jira_api_key", "class": "credential",
               "operator_answer": "SYNTHETIC", "answer_is_synthetic": True,
               "label": "unassessable"}]))
    sc = load_scenario(p)
    res = replay_scenario(sc)
    assert res.error == ""
    # the leaf really did bind — otherwise the SECRET reason is unreachable
    assert res.verdicts[0].bound_state == "have"
    assert res.verdicts[0].verdict == UNASSESSABLE
    assert res.verdicts[0].reason == rr.R_SECRET


def test_a_standin_on_an_ordinary_path_does_not_claim_to_be_a_secret(tmp_path):
    """The stand-in short-circuit is allowed on any path (it stops a fabricated value
    ever being scored against a bind), so its REASON must stay true. A stand-in on an
    ordinary leaf reports `answer_is_a_declared_standin`, not the secret code."""
    p = _write(tmp_path, "s.json", _fixture(
        situation={"services": [], "capabilities": [], "roots": [], "credentials": [],
                   "profile": {"default_output_dir": "{root}/out"},
                   "declared_intents": []},
        capability={"name": "write_doc",
                    "parameters_schema": {"output_dir": {"type": "string"}},
                    "effect_tags": []},
        asks=[{"schema_path": "output_dir", "class": "input",
               "operator_answer": "{root}/out", "answer_is_synthetic": True,
               "label": "unassessable"}]))
    res = replay_scenario(load_scenario(p))
    assert res.verdicts[0].bound_state == "have"     # it really did bind...
    assert res.verdicts[0].verdict == UNASSESSABLE   # ...and is still not scored
    assert res.verdicts[0].reason == rr.R_STANDIN
    assert res.verdicts[0].reason != rr.R_SECRET


def test_a_standin_is_never_scored_even_when_it_matches_the_bind(tmp_path):
    """The integrity property behind that short-circuit: the stand-in above is
    byte-identical to what the binder holds. Scoring it would report `avoidable` off
    a value the operator never actually gave."""
    p = _write(tmp_path, "s.json", _fixture(
        situation={"services": [], "capabilities": [], "roots": [], "credentials": [],
                   "profile": {"default_output_dir": "{root}/out"},
                   "declared_intents": []},
        capability={"name": "write_doc",
                    "parameters_schema": {"output_dir": {"type": "string"}},
                    "effect_tags": []},
        asks=[{"schema_path": "output_dir", "class": "input",
               "operator_answer": "{root}/out", "answer_is_synthetic": True,
               "label": "unassessable"}]))
    assert replay_scenario(load_scenario(p)).verdicts[0].verdict == UNASSESSABLE

    # ...whereas the SAME fixture without the stand-in flag scores avoidable, which
    # is what proves the flag (not some unrelated guard) is doing the work.
    p2 = _write(tmp_path, "s2.json", _fixture(
        id="s2",
        situation={"services": [], "capabilities": [], "roots": [], "credentials": [],
                   "profile": {"default_output_dir": "{root}/out"},
                   "declared_intents": []},
        capability={"name": "write_doc",
                    "parameters_schema": {"output_dir": {"type": "string"}},
                    "effect_tags": []},
        asks=[{"schema_path": "output_dir", "class": "input",
               "operator_answer": "{root}/out", "label": "avoidable"}]))
    assert replay_scenario(load_scenario(p2)).verdicts[0].verdict == AVOIDABLE


def test_a_secret_leaf_the_resolver_could_not_bind_is_still_a_definitive_necessary(tmp_path):
    """Ordering check. `missing` is decided BEFORE the synthetic short-circuit, and
    deliberately: proving the resolver produced NOTHING needs no value comparison, so
    that ask is provably necessary even for a secret leaf. Only a secret leaf that DID
    bind is unassessable (we cannot tell whether the bind matched)."""
    p = _write(tmp_path, "s.json", _fixture(
        capability={"name": "jira_sync",
                    "parameters_schema": {"jira_api_key": {"type": "string"}},
                    "effect_tags": []},
        situation={"services": [], "capabilities": [], "roots": [],
                   "credentials": [], "profile": {}, "declared_intents": []},
        asks=[{"schema_path": "jira_api_key", "class": "credential",
               "operator_answer": "SYNTHETIC", "answer_is_synthetic": True,
               "label": "necessary"}]))
    res = replay_scenario(load_scenario(p))
    assert res.verdicts[0].bound_state == "missing"
    assert res.verdicts[0].verdict == NECESSARY
    assert res.verdicts[0].reason == rr.R_BOUND_NOTHING


def test_the_secret_guard_is_not_so_broad_it_refuses_ordinary_asks(tmp_path):
    """A guard that refused everything would also pass the test above. Pin that
    ordinary leaves still load with plaintext answers."""
    for path in ("output_dir", "source_path", "account_id", "repo", "format"):
        p = _write(tmp_path, f"{path}.json", _fixture(
            id=path,
            capability={"name": "t", "parameters_schema": {path: {"type": "string"}},
                        "effect_tags": []},
            asks=[{"schema_path": path, "class": "decision",
                   "operator_answer": "v", "label": "necessary"}]))
        assert load_scenario(p).asks[0].operator_answer == "v"


# ── 5. a bad fixture is an error, never a silent skip ───────────────────────
@pytest.mark.parametrize("payload,needle", [
    ("{not json", "invalid JSON"),
    ({"schema_version": 99, "id": "x"}, "schema_version"),
    ({"schema_version": 1, "id": ""}, "non-empty id"),
    ({"schema_version": 1, "id": "x"}, "capability"),
])
def test_malformed_fixture_surfaces_as_an_error(tmp_path, payload, needle):
    # roster declares bad.json, so the file is IN the denominator — the only failure
    # is the load error, not roster drift.
    _corpus(tmp_path, {"bad.json": payload})
    scenarios, errors, declared, missing, extra = load_corpus(tmp_path)
    assert scenarios == []
    assert missing == frozenset() and extra == frozenset(), (missing, extra)
    assert len(errors) == 1 and needle in errors[0]

    r = resolver_replay_report(tmp_path)
    assert r.error_count == 1
    assert r.rate is None            # and it does NOT read as a healthy 0%


def test_bad_label_is_refused(tmp_path):
    p = _write(tmp_path, "bad.json", _fixture(asks=[{
        "schema_path": "widget", "class": "decision",
        "operator_answer": "w", "label": "probably-fine"}]))
    with pytest.raises(FixtureError) as exc:
        load_scenario(p)
    assert "label" in str(exc.value)


def test_duplicate_scenario_ids_are_reported(tmp_path):
    _corpus(tmp_path, {"a.json": _fixture(id="same"), "b.json": _fixture(id="same")})
    _, errors, _declared, _missing, _extra = load_corpus(tmp_path)
    assert any("duplicate scenario id" in e for e in errors)


def test_files_path_cannot_escape_the_scenario_root(tmp_path):
    p = _write(tmp_path, "bad.json", _fixture(files=[{"path": "../../etc/x", "bytes": 1}]))
    with pytest.raises(FixtureError) as exc:
        load_scenario(p)
    assert "escape" in str(exc.value)


def test_one_bad_fixture_does_not_suppress_the_good_ones(tmp_path):
    """A single malformed file must not abort the corpus — nor be swept under the rug."""
    _corpus(tmp_path, {"good.json": _fixture(id="good"), "bad.json": "{oops"})
    scenarios, errors, _declared, missing, extra = load_corpus(tmp_path)
    assert [s.scenario_id for s in scenarios] == ["good"]
    assert missing == frozenset() and extra == frozenset()
    assert len(errors) == 1


# ── 6. it really is the production resolver being re-run ────────────────────
def test_replay_actually_invokes_the_real_resolver(monkeypatch, tmp_path):
    """MUTATION PIN on the CALL SITE, not the guard body.

    `replay_scenario` imports `compute_requirements` at call time, so replacing the
    module attribute replaces what the replay runs. If the call site were ever
    removed — or the verdict computed from the fixture's own label instead of a real
    bind — this test would keep passing only if the replay were a sham. Here we make
    the real resolver return a bound-to-something-else requirement and assert the
    verdict FOLLOWS the resolver rather than the label.
    """
    calls = []
    real = None

    from systemu.runtime import requirement_binder as rb
    real = rb.compute_requirements

    def spy(objective, capability, situation, ctx, provided_params=None, vault=None):
        calls.append((getattr(capability, "name", None), bool(vault)))
        return real(objective, capability, situation, ctx,
                    provided_params=provided_params, vault=vault)

    monkeypatch.setattr(rb, "compute_requirements", spy)

    p = _write(tmp_path, "s.json", _fixture(
        situation={"services": [], "capabilities": [], "roots": [], "credentials": [],
                   "profile": {"default_output_dir": "{root}/out"}, "declared_intents": []},
        capability={"name": "write_doc",
                    "parameters_schema": {"output_dir": {"type": "string"}},
                    "effect_tags": []},
        asks=[{"schema_path": "output_dir", "class": "input",
               "operator_answer": "{root}/out", "label": "avoidable"}]))
    res = replay_scenario(load_scenario(p))
    assert calls, "the replay must call compute_requirements — that IS the replay"
    assert calls[0][0] == "write_doc"
    assert calls[0][1] is True, "vault must be threaded, or no bind carries a digest"
    assert res.verdicts[0].verdict == AVOIDABLE


def test_verdict_follows_the_resolver_not_the_declared_label(monkeypatch, tmp_path):
    """If the resolver stops binding, an 'avoidable'-labelled ask must flip to
    necessary and show up as MISLABELLED. This is what makes the corpus a
    regression tripwire instead of a self-fulfilling assertion."""
    from systemu.runtime import requirement_binder as rb
    monkeypatch.setattr(rb, "compute_requirements",
                        lambda *a, **k: [])          # resolver binds nothing at all

    _corpus(tmp_path, {"s.json": _fixture(asks=[{
        "schema_path": "widget", "class": "decision",
        "operator_answer": "w-1", "label": "avoidable"}])})
    r = resolver_replay_report(tmp_path)
    assert r.avoidable_count == 0
    assert len(r.mislabelled) == 1
    assert r.mislabelled[0]["declared"] == AVOIDABLE
    assert r.mislabelled[0]["replayed"] == UNASSESSABLE


def test_replay_grants_roots_through_the_real_store(tmp_path):
    """Source #1 fail-closes on a None GrantedRootsStore, so a replay that skipped
    granting would report every granted-root file bind as `necessary` — a silent,
    plausible, wholly wrong rate. Pin that the grant actually happens by asserting
    the bind lands."""
    report = resolver_replay_report(REAL_CORPUS)
    v = _verdict_for(report, "ra10-ac6-content-derived-salient-path", "source_path")
    assert v["verdict"] == AVOIDABLE
    assert v["bound_source"] == "situation", \
        "the granted-root file bind (source #1) must fire; it fail-closes without a store"


def test_replay_builds_the_concrete_execution_context_production_uses(tmp_path):
    """The production call site passes a real ExecutionContext (which carries
    files_produced and NO vault). Source #2 reads files_produced off it, so a
    duck-typed stand-in could hide a shape mismatch."""
    report = resolver_replay_report(REAL_CORPUS)
    v = _verdict_for(report, "ra10-run-context-produced-file", "attachment_path")
    assert v["bound_source"] == "run_context"
    assert v["verdict"] == AVOIDABLE


# ── 7. the comparison itself ────────────────────────────────────────────────
def test_a_reshaped_answer_still_confirms_via_the_canonical_digest(tmp_path):
    """The binder stamps an exact digest AND a form-insensitive canonical twin. An
    answer that differs only in separator/case must still read as a confirm — the
    directional-error lesson from the R-A16 F2 fix."""
    _corpus(tmp_path, {"s.json": _fixture(
        situation={"services": [], "capabilities": [], "roots": [], "credentials": [],
                   "profile": {"default_output_dir": "{root}/out/reports"},
                   "declared_intents": []},
        capability={"name": "write_doc",
                    "parameters_schema": {"output_dir": {"type": "string"}},
                    "effect_tags": []},
        asks=[{"schema_path": "output_dir", "class": "input",
               # same location, different FORM (backslashes + doubled separator)
               "operator_answer": "{root}\\\\out\\\\reports", "label": "avoidable"}])})
    r = resolver_replay_report(tmp_path)
    assert r.avoidable_count == 1, r.details


def test_exact_and_canonical_confirms_are_reported_apart(tmp_path):
    """The two comparison branches must be DISTINGUISHABLE, not an `a or b` whose
    result hides which fired.

    Mutation testing found the original `exact or canonical` form to be a single
    path wearing two names: deleting the exact branch changed nothing observable,
    because every exact match is also a canonical match. Recording `match_basis`
    fixes that by surfacing the difference the fold actually makes — a canonical
    -only confirm rests on a lossier comparison (casefold + separator fold) and is
    weaker evidence than an exact one.
    """
    # identical form → the EXACT branch must claim it
    _corpus(tmp_path, {"exact.json": _fixture(
        id="exact",
        situation={"services": [], "capabilities": [], "roots": [], "credentials": [],
                   "profile": {"default_output_dir": "{root}/out/reports"},
                   "declared_intents": []},
        capability={"name": "write_doc",
                    "parameters_schema": {"output_dir": {"type": "string"}},
                    "effect_tags": []},
        asks=[{"schema_path": "output_dir", "class": "input",
               "operator_answer": "{root}/out/reports", "label": "avoidable"}])})
    r = resolver_replay_report(tmp_path)
    assert r.details[0]["match_basis"] == "exact"
    assert r.match_bases == {"exact": 1}

    # a form-only difference the EXACT digest CANNOT match → the CANONICAL branch,
    # and the report must name it as such. (`ra135-form-only-difference` uses a
    # PLAIN string leaf where normcase never applies, so both platforms agree.)
    (tmp_path / "exact.json").unlink()
    _corpus(tmp_path, {"canon.json": _fixture(
        id="canon",
        capability={"name": "fmt",
                    "parameters_schema": {"title": {"type": "string",
                                                    "default": "Monthly Report"}},
                    "effect_tags": []},
        asks=[{"schema_path": "title", "class": "decision",
               "operator_answer": "monthly report", "label": "avoidable"}])})
    r2 = resolver_replay_report(tmp_path)
    assert r2.avoidable_count == 1, r2.details
    assert r2.details[0]["match_basis"] == "canonical", r2.details
    assert r2.match_bases == {"canonical": 1}, r2.match_bases


def test_an_ambiguous_suffix_scores_nothing_rather_than_guessing(tmp_path):
    """`_find_requirement` falls back to a SUFFIX match so a fixture can name a
    nested leaf by its readable tail (`path` for `src.path`). When that tail is
    ambiguous — here BOTH `src.path` and `dst.path` end in `.path` — it must resolve
    to NOTHING and score `unassessable`.

    Picking the first candidate would be worse than useless: `src.path` and
    `dst.path` bind DIFFERENT values, so an arbitrary pick manufactures a verdict
    about a leaf the operator was never asked about — an `avoidable` or `necessary`
    reading with no relationship to the ask. In a module whose only value is that
    its verdicts are exact, guessing is the one unacceptable failure. Pinned because
    relaxing the guard to `tail[0] if tail else None` survived the whole file.
    """
    _corpus(tmp_path, {"amb.json": _fixture(
        id="amb",
        objective={"id": 1, "goal": "copy the file", "success_criteria": "done"},
        capability={"name": "cp", "effect_tags": [], "parameters_schema": {
            "src": {"type": "object", "required": ["path"],
                    "properties": {"path": {"type": "string", "default": "a.txt"}}},
            "dst": {"type": "object", "required": ["path"],
                    "properties": {"path": {"type": "string", "default": "b.txt"}}}}},
        asks=[{"schema_path": "path", "class": "decision",
               "operator_answer": "a.txt", "label": "unassessable"}])})
    report = resolver_replay_report(tmp_path)

    v = _verdict_for(report, "amb", "path")
    assert v["verdict"] == UNASSESSABLE, report.details
    assert v["reason"] == rr.R_NO_REQUIREMENT, report.details
    # and it must not have silently scored one of the two real leaves
    assert report.avoidable_count == 0, report.details
    assert report.necessary_count == 0, report.details
    assert report.rate is None


def test_an_unambiguous_suffix_still_resolves(tmp_path):
    """The guard above must not be so broad it kills the fallback it protects: a
    tail matching exactly ONE leaf still resolves, so a fixture can keep naming a
    nested leaf readably."""
    _corpus(tmp_path, {"one.json": _fixture(
        id="one",
        objective={"id": 1, "goal": "render", "success_criteria": "done"},
        capability={"name": "cp", "effect_tags": [], "parameters_schema": {
            "src": {"type": "object", "required": ["path"],
                    "properties": {"path": {"type": "string", "default": "a.txt"}}}}},
        asks=[{"schema_path": "path", "class": "decision",
               "operator_answer": "a.txt", "label": "avoidable"}])})
    report = resolver_replay_report(tmp_path)

    v = _verdict_for(report, "one", "path")
    assert v["verdict"] == AVOIDABLE, report.details
    assert v["reason"] == rr.R_BOUND_THE_ANSWER, report.details


def test_materialise_actually_creates_the_declared_files(tmp_path):
    """`files[]` is written to disk. Pinned directly because — see the README and the
    packet report — NO source the corpus currently exercises stats the filesystem:
    `reference_resolver` reads pre-surveyed salient handles only ("NO disk walk, NO
    stat"). So declaring a file cannot change any verdict today, and a broken
    `_materialise` would otherwise be invisible.
    """
    from systemu.runtime.resolver_replay import _materialise

    sc = load_scenario(_write(tmp_path, "s.json", _fixture(files=[
        {"path": "a/b.txt", "text": "hello"},
        {"path": "c/d.bin", "bytes": 5},
    ])))
    wd = tmp_path / "wd"
    wd.mkdir()
    _materialise(sc, wd)
    assert (wd / "a" / "b.txt").read_text(encoding="utf-8") == "hello"
    assert (wd / "c" / "d.bin").read_bytes() == b"xxxxx"


def test_a_genuinely_different_value_does_not_confirm(tmp_path):
    """The negative half of the test above — canonicalisation must not fold two
    genuinely different values together."""
    _corpus(tmp_path, {"s.json": _fixture(
        situation={"services": [], "capabilities": [], "roots": [], "credentials": [],
                   "profile": {"default_output_dir": "{root}/out/reports"},
                   "declared_intents": []},
        capability={"name": "write_doc",
                    "parameters_schema": {"output_dir": {"type": "string"}},
                    "effect_tags": []},
        asks=[{"schema_path": "output_dir", "class": "input",
               "operator_answer": "{root}/out", "label": "necessary"}])})
    r = resolver_replay_report(tmp_path)
    assert r.necessary_count == 1
    assert r.details[0]["reason"] == rr.R_BOUND_OTHER_VALUE


def test_missing_leaf_is_necessary_not_unassessable(tmp_path):
    """A resolver that produced NOTHING is a definitive negative, not an unknown.
    Collapsing the two would let a broken resolver quietly leave the denominator."""
    _corpus(tmp_path, {"s.json": _fixture()})
    r = resolver_replay_report(tmp_path)
    assert r.necessary_count == 1
    assert r.details[0]["reason"] == rr.R_BOUND_NOTHING
    assert r.details[0]["bound_state"] == "missing"


# ── 8. the report is honest about what it is ────────────────────────────────
def test_report_states_it_does_not_measure_the_live_ask_stream():
    lines = "\n".join(format_resolver_replay(resolver_replay_report(REAL_CORPUS)))
    assert "DEFINITIVE" in lines
    assert "does" in lines.lower() and "not measure the live ask stream" in lines.lower()


# ── 8b. DEC-27: the renderer takes ONLY a ReplayReport ──────────────────────
@pytest.mark.parametrize("bad", [
    None,
    {},
    {"rate": 0.5, "assessable": 2, "avoidable_count": 1, "complete": True},
    {"definitive": True, "rate": 1.0},
    "not a report",
])
def test_format_resolver_replay_refuses_anything_but_a_report(bad):
    """The `report or {}` bug: the old renderer coerced None/{}/a legacy dict and read
    `.get("complete")`, printing the DEFINITIVE banner over nothing. A report's trust
    lives in its DERIVED `definitive` property, which a bare dict cannot spell, so the
    boundary now refuses everything that is not a ReplayReport — loudly."""
    with pytest.raises(TypeError):
        format_resolver_replay(bad)


def test_a_real_report_is_accepted_by_the_renderer():
    """The other side of the boundary: a genuine ReplayReport renders fine."""
    lines = format_resolver_replay(resolver_replay_report(REAL_CORPUS))
    assert isinstance(lines, list) and lines
    assert isinstance(resolver_replay_report(REAL_CORPUS), ReplayReport)


# ── 9. the operator surface actually renders ────────────────────────────────
def test_debug_resolver_replay_cli_renders_the_report(tmp_path):
    """RENDER CALL-SITE pin. A report nobody prints is indistinguishable from one
    nobody computed — deleting the echo loop must not leave the suite green.
    Invoked through the real `debug_group`, so command registration is pinned too.
    """
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    res = CliRunner().invoke(cli_commands.debug_group,
                             ["resolver-replay", "--corpus", str(REAL_CORPUS)])
    assert res.exit_code == 0, res.output
    assert "resolver replay" in res.output
    assert "DEFINITIVE" in res.output
    # the honesty clause has to reach the operator, not just the docstring
    assert "not measure the live ask stream" in res.output.lower()
    assert "scenarios=" in res.output


def test_debug_resolver_replay_cli_needs_no_vault(monkeypatch):
    """The replay reads the checked-in corpus, not the vault. If it ever started
    resolving a vault, this would fail — which is the point: an operator running a
    metric must not be able to touch live state."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    def _boom(ctx):
        raise AssertionError("resolver-replay must not resolve a vault")

    monkeypatch.setattr(cli_commands, "_get_vault_and_config", _boom)
    res = CliRunner().invoke(cli_commands.debug_group,
                             ["resolver-replay", "--corpus", str(REAL_CORPUS)])
    assert res.exit_code == 0, res.output


def test_debug_resolver_replay_cli_reports_a_rosterless_corpus_as_incomplete(tmp_path):
    """A directory with no `roster.json` has no committed denominator: it must render
    as INCOMPLETE (never a percentage) and exit NON-ZERO — it is not the clean "no
    assessable asks" state, which requires a matching roster."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    res = CliRunner().invoke(cli_commands.debug_group,
                             ["resolver-replay", "--corpus", str(tmp_path)])
    assert res.exit_code != 0, res.output
    assert "INCOMPLETE" in res.output
    # no "<n>/<m> = <p>%" rate is rendered anywhere
    assert not re.search(r"=\s*\d+%", res.output), res.output


def test_debug_resolver_replay_cli_reports_no_assessable_asks_over_a_definitive_corpus(tmp_path):
    """The "NO ASSESSABLE ASKS" path is reachable ONLY when the corpus reconciles
    against its roster and simply has nothing to score — and it exits ZERO."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    _corpus(tmp_path, {"s.json": _fixture(asks=[{
        "schema_path": "api_token", "class": "credential",
        "operator_answer": "STANDIN", "answer_is_synthetic": True,
        "label": "unassessable"}])})
    res = CliRunner().invoke(cli_commands.debug_group,
                             ["resolver-replay", "--corpus", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "NO ASSESSABLE ASKS" in res.output
    assert not re.search(r"=\s*\d+%", res.output), res.output


def test_default_corpus_dir_points_at_the_repo_corpus(monkeypatch):
    monkeypatch.delenv("SYSTEMU_FIELD_CORPUS", raising=False)
    assert default_corpus_dir() == REAL_CORPUS


def test_env_override_redirects_the_corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEMU_FIELD_CORPUS", str(tmp_path))
    assert default_corpus_dir() == tmp_path


# ── 9b. PARTIAL failure must not be able to emit a healthy-looking rate ───────
#
# A dropped scenario leaves the DENOMINATOR, and the report still speaks — confidently
# and wrongly. `rate` is gated on `definitive`, which is DERIVED (declared>0, no
# errors, no roster drift), so a partial run cannot reach a number at all.
def _avoidable_fx(i):
    """A scenario source #0 binds from provided_params -> replays AVOIDABLE."""
    return _fixture(id=f"ok{i}", provided_params={"widget": f"w-{i}"},
                    asks=[{"schema_path": "widget", "class": "decision",
                           "operator_answer": f"w-{i}", "label": "avoidable"}])


def _necessary_fx(i):
    """No source can supply the leaf -> replays NECESSARY."""
    return _fixture(id=f"nn{i}",
                    asks=[{"schema_path": "widget", "class": "decision",
                           "operator_answer": f"n-{i}", "label": "necessary"}])


def test_partial_load_failure_suppresses_the_rate_entirely(tmp_path):
    """Two good scenarios + one malformed, all three DECLARED in the roster (so the
    only failure is the load error, not drift). The good ones still replay, so
    `assessable` is non-zero and a naive `avoidable/assessable` would sail past."""
    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1), "ok2.json": _avoidable_fx(2)})
    healthy = resolver_replay_report(tmp_path)
    assert healthy.rate == 1.0 and healthy.error_count == 0
    assert healthy.definitive is True

    _write(tmp_path, "bad.json", "{not json")
    _roster(tmp_path, ["ok1.json", "ok2.json", "bad.json"])   # declared, so it's a load failure
    r = resolver_replay_report(tmp_path)

    assert r.error_count == 1, r.errors
    assert r.assessable == 2, "the surviving scenarios still replayed"
    assert r.missing == frozenset() and r.extra == frozenset(), "drift is not the cause here"
    # The whole point: a number WAS computable, and must not be reported.
    assert r.rate is None, (
        "a partial failure reshapes the denominator; it must not emit a rate")
    assert r.definitive is False


def test_partial_failure_can_never_emit_the_healthy_zero(tmp_path):
    """Non-negotiable: where a healthy signal is a SPECIFIC value, the failure
    path must not be able to emit that value. `0.0` == "no avoidable asks" ==
    the best possible reading. With only NECESSARY scenarios surviving, a naive
    expression would produce exactly 0/2 = 0.0 alongside a load error."""
    _corpus(tmp_path, {"nn1.json": _necessary_fx(1), "nn2.json": _necessary_fx(2),
                       "bad.json": "{not json"})

    r = resolver_replay_report(tmp_path)
    assert r.avoidable_count == 0 and r.assessable == 2, r.details
    assert r.error_count == 1
    assert r.rate is not 0.0            # noqa: F632 - identity is the point
    assert r.rate != 0.0, "0.0 is the HEALTHY value; a broken run must not reach it"
    assert r.rate is None

    line = format_resolver_replay(r)[0]
    # No rendered "<n>/<m> = <p>%" at all. (Matching a bare "0%" would trip on
    # the line's own "this is NOT 0%" disclaimer.)
    assert not re.search(r"=\s*\d+%", line), line
    assert "INCOMPLETE" in line, line


def test_a_real_replay_time_failure_is_recorded_and_suppresses_the_rate(tmp_path):
    """Pins the REPLAY-time error appender specifically — a different site from
    the load-time one, and the one left unpinned.

    No monkeypatching: `files` declares `a/b` as a FILE and then `a/b/c`, whose
    parent must be a DIRECTORY. `_materialise` raises for real, inside
    `replay_scenario`'s own try, and comes back as `ScenarioResult.error`."""
    _corpus(tmp_path, {
        "ok1.json": _avoidable_fx(1),
        "boom.json": _fixture(
            id="boom",
            files=[{"path": "a/b", "text": "i am a file, not a directory"},
                   {"path": "a/b/c", "text": "so creating me must fail"}]),
    })

    scenarios, load_errors, _declared, missing, extra = load_corpus(tmp_path)
    assert load_errors == [], "both fixtures must LOAD cleanly - the failure is at REPLAY"
    assert missing == frozenset() and extra == frozenset(), "no drift - the failure is at REPLAY"
    assert sorted(s.scenario_id for s in scenarios) == ["boom", "ok1"]

    boom = [s for s in scenarios if s.scenario_id == "boom"][0]
    assert replay_scenario(boom).error, "this scenario must fail at replay time"

    r = resolver_replay_report(tmp_path)
    # The appender is what records it. If it were `pass`, `errors` would be empty
    # and - now that `rate` is gated on it - a rate would reappear.
    #
    # Assert WHICH error, not just how many. A bare `== 1` says nothing about
    # whether the one error is the one this test is about, and it fails
    # uninterpretably if anything ever adds a second.
    assert [e for e in r.errors if "boom" in e], r.errors
    assert [e for e in r.errors if "ok1" in e] == [], (
        f"the healthy scenario must have replayed cleanly: {r.errors}")
    assert r.error_count == len(r.errors) == 1, r.errors
    assert r.rate is None
    assert r.definitive is False


def test_definitive_is_derived_from_inputs_not_hardcoded(tmp_path):
    """`definitive` was once the constant `True`; flipping it survived the whole file
    because nothing asserted on it in either state. It is now a DERIVED property, and
    these two states must disagree."""
    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1)})
    clean = resolver_replay_report(tmp_path)
    assert clean.definitive is True, "a clean, fully-reconciled corpus IS definitive"

    _write(tmp_path, "bad.json", "{not json")
    _roster(tmp_path, ["ok1.json", "bad.json"])
    dirty = resolver_replay_report(tmp_path)
    assert dirty.definitive is False, "a corpus that dropped a scenario is NOT"


def test_definitive_is_a_property_that_cannot_be_overwritten():
    """It is DERIVED, not stored: there is no settable `definitive` field to poke, so
    no caller (or bug) can stamp `definitive: True` onto a broken report."""
    r = resolver_replay_report(REAL_CORPUS)
    with pytest.raises(AttributeError):
        r.definitive = False            # frozen dataclass + property, doubly unsettable


def test_the_word_definitive_is_not_rendered_over_an_incomplete_run(tmp_path):
    """The render printed its "(DEFINITIVE over the fixtures/field corpus...)"
    banner unconditionally - including over a run that dropped scenarios."""
    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1)})
    assert any("DEFINITIVE" in l for l in
               format_resolver_replay(resolver_replay_report(tmp_path)))

    _write(tmp_path, "bad.json", "{not json")
    _roster(tmp_path, ["ok1.json", "bad.json"])
    lines = format_resolver_replay(resolver_replay_report(tmp_path))
    assert not any("DEFINITIVE" in l for l in lines), lines
    assert any("NOT definitive" in l for l in lines), lines
    assert not re.search(r"=\s*\d+%", "\n".join(lines)), lines


def test_an_incomplete_run_is_not_reported_as_no_assessable_asks(tmp_path):
    """Two distinct conditions, two distinct messages. Collapsing them would
    tell an operator the corpus was empty when it was actually broken."""
    _corpus(tmp_path, {"nn1.json": _necessary_fx(1), "bad.json": "{not json"})
    line = format_resolver_replay(resolver_replay_report(tmp_path))[0]
    assert "INCOMPLETE REPLAY" in line, line
    assert "NO ASSESSABLE ASKS" not in line, line


# ── 10. every declared path is contained; `files[]` was not the only one ─────
#
# `files[].path` was guarded (relative, no `..`). `granted_roots` and
# `files_produced` were not, and both are consumed:
#   * `granted_roots` is mkdir'd at replay time and seeds the GrantedRootsStore
#     that resolver source #1 re-gates through -- an absolute entry created a
#     directory anywhere on disk, with ZERO load errors reported;
#   * `files_produced` is handed to the binder as "a file this run produced"
#     (source #2), so an unconstrained entry aims the resolver at any path.
@pytest.mark.parametrize("field", ["granted_roots", "files_produced"])
@pytest.mark.parametrize("bad", [
    "/etc/systemu_escape",              # POSIX-absolute
    r"C:\Windows\Temp\systemu_escape",  # Windows drive-absolute
    r"\\somehost\share\systemu_escape",  # UNC
    r"\rooted_no_drive",                # rooted, driveless: is_absolute() is False
                                        # on Windows, but the join still escapes
    "C:relative_to_drive_cwd",          # drive-relative
    "../escaped",
    "sub/../../escaped",
    "{root}/../escaped",                # the token form must not be a bypass
])
def test_an_escaping_path_is_refused_at_load(tmp_path, field, bad):
    d = tmp_path / "corpus"
    d.mkdir()
    # roster declares esc.json, so the only failure is the escape, not drift.
    _corpus(d, {"esc.json": _fixture(**{field: [bad]})})

    scenarios, errors, _declared, missing, extra = load_corpus(d)
    assert [s.scenario_id for s in scenarios] == [], f"{field}={bad!r} must not load"
    assert missing == frozenset() and extra == frozenset(), (missing, extra)
    # WHICH error, not how many: name the field AND echo the offending value, so
    # a failure here reads as a diagnosis instead of an arithmetic disagreement.
    assert len(errors) == 1, errors
    assert field in errors[0], errors
    # the loader reports the offending value with `!r`, so match its repr
    assert repr(bad) in errors[0], errors


@pytest.mark.parametrize("field", ["granted_roots", "files_produced"])
@pytest.mark.parametrize("ok", ["g", "out/prior.md", "{root}/out/prior.md"])
def test_the_legitimate_contained_forms_still_load(tmp_path, field, ok):
    """The real corpus uses a bare relative segment and the `{root}/` idiom;
    the guard must not break either.

    NOTE this asserts a ROUND-TRIP through the loader and nothing more. That is
    deliberately weak, and on its own it BLESSED the `{root}` defect: the broken
    code round-tripped the string perfectly while joining it verbatim at replay.
    What the value authorises is pinned in section 13, not here.
    """
    d = tmp_path / "corpus"
    d.mkdir()
    _corpus(d, {"ok.json": _fixture(**{field: [ok]})})
    scenarios, errors, _declared, missing, extra = load_corpus(d)
    assert errors == [] and missing == frozenset() and extra == frozenset(), (errors, missing, extra)
    assert getattr(scenarios[0], field) == (ok,)


def test_an_absolute_granted_root_creates_nothing_outside_the_workdir(tmp_path):
    """The defect end-to-end, not just its guard: before the fix this scenario
    loaded clean and `mkdir`'d the target for real."""
    escape = tmp_path / "ESCAPED_OUTSIDE_ANY_WORKDIR"
    assert not escape.exists()

    _corpus(tmp_path, {"esc.json": _fixture(granted_roots=[str(escape)])})
    r = resolver_replay_report(tmp_path)

    assert not escape.exists(), "the replay created a directory outside its workdir"
    assert r.error_count == 1, r.errors
    assert r.rate is None and r.definitive is False


def test_the_shipped_corpus_satisfies_the_containment_rule():
    """The guard is only worth having if the real corpus already obeys it."""
    scenarios, errors, _declared, missing, extra = load_corpus(REAL_CORPUS)
    assert errors == [], errors
    assert missing == frozenset() and extra == frozenset()
    assert any(s.granted_roots for s in scenarios), "corpus must exercise granted_roots"
    assert any(s.files_produced for s in scenarios), "corpus must exercise files_produced"


# ── 11. corpus integrity: the citations and the credential rule ─────────────
def _corpus_payloads():
    for p in sorted(REAL_CORPUS.glob("*.json")):
        if p.name == ROSTER_FILENAME:   # the roster is not a scenario
            continue
        yield p, json.loads(p.read_text(encoding="utf-8"))


def test_every_provenance_citation_resolves_or_declares_itself_authored():
    """A fixture earns its place by citing the test whose behaviour it encodes.
    Without this, renaming a test silently orphans the citation and the corpus
    rots into a pile of unexplained inputs.

    A scenario written FOR the replay has no upstream test to cite; it must say
    so in words rather than name a file that does not exist."""
    unresolved = []
    for p, d in _corpus_payloads():
        cite = str((d.get("provenance") or {}).get("source_test", "") or "").strip()
        if not cite:
            unresolved.append(f"{p.name}: no provenance.source_test")
            continue
        ref = cite.split()[0]
        if not ref.startswith("tests/"):
            # a self-describing citation ("authored for ...") is legitimate,
            # but it must not look like a path that simply does not exist
            assert "/" not in ref and "\\" not in ref, (
                f"{p.name}: {cite!r} looks like a path but is not under tests/")
            continue
        path, _, testname = ref.partition("::")
        f = REPO_ROOT / path
        if not f.is_file():
            unresolved.append(f"{p.name}: cites missing file {path}")
            continue
        if testname and f"def {testname}" not in f.read_text(encoding="utf-8"):
            unresolved.append(f"{p.name}: cites missing test {testname} in {path}")
    assert not unresolved, unresolved


def test_no_fixture_records_a_plaintext_credential():
    """The rule the loader enforces, asserted over the SHIPPED corpus too — the
    loader can only refuse what someone tries to load."""
    from systemu.runtime.replay_metrics import _is_secret_path

    for p, d in _corpus_payloads():
        for ask in d.get("asks") or []:
            if ask.get("operator_answer") is None:
                continue
            if _is_secret_path(str(ask.get("schema_path", "")),
                               str(ask.get("class", ""))):
                assert ask.get("answer_is_synthetic") is True, (
                    f"{p.name}: secret-classified ask {ask.get('schema_path')!r} "
                    f"carries an undeclared answer")


def test_every_fixture_declares_a_label_the_replay_checks():
    """The corpus is a tripwire only because every ask is labelled."""
    for p, d in _corpus_payloads():
        asks = d.get("asks") or []
        assert asks, f"{p.name}: no asks"
        for ask in asks:
            assert ask.get("label") in {AVOIDABLE, NECESSARY, UNASSESSABLE}, (
                f"{p.name}: bad label {ask.get('label')!r}")


# ── 12. DEC-27: the ROSTER is the denominator — drift fails closed both ways ──
#
# The predecessor skipped `_`-prefixed files with a bare `continue` that appended to
# NOTHING, while the completeness gate was `not errors and not skipped`. Renaming six
# fixtures (content untouched) rendered a true 6/9=67% as a healthy "0/3 = 0%" with
# `definitive: True`. The fix is not a third channel — it is an INDEPENDENT roster:
# the denominator no longer comes from the observed set that a rename can shrink.
def test_renaming_fixtures_cannot_manufacture_the_healthy_zero_percent(tmp_path):
    """THE REGRESSION, retold against the roster. Hide fixtures behind the old skip
    convention (`_`-prefix) and the run must go non-definitive — the rate VANISHES,
    it does not read 0%.

    In the OLD model the `_x` twins were SKIPPED, the numerator collapsed to 0/3, and
    0% rendered with `definitive: True`. Against the roster the `_x` twins are EXTRA
    and the originals they replaced are MISSING, so the run is non-definitive from the
    DRIFT alone — whatever the surviving verdicts say. Either way the healthy 0% is
    now unreachable, and by a denominator the rename cannot touch.
    """
    _copy_real_corpus(tmp_path)
    base = resolver_replay_report(tmp_path)
    assert base.definitive is True and base.rate is not None
    assert base.missing == frozenset() and base.extra == frozenset()

    hidden = {d["scenario_id"] for d in base.details if d["verdict"] == AVOIDABLE}
    assert hidden, "the corpus must contain avoidable verdicts for this to bite"
    n = 0
    for p in sorted(tmp_path.glob("*.json")):
        if p.name == ROSTER_FILENAME:
            continue
        if json.loads(p.read_text(encoding="utf-8")).get("id") in hidden:
            p.rename(tmp_path / ("_" + p.name))
            n += 1
    assert n == len(hidden)

    r = resolver_replay_report(tmp_path)
    # ...the report must refuse to speak rather than speak healthily
    assert r.rate is None, "a drifted corpus emitted a rate"
    assert r.definitive is False
    # the originals are gone (MISSING) and their `_x` twins are unrostered (EXTRA)
    assert len(r.missing) == n, r.missing
    assert len(r.extra) == n, r.extra
    line = format_resolver_replay(r)[0]
    assert "INCOMPLETE" in line, line
    assert not re.search(r"=\s*\d+%", line), line


def test_deleting_one_rostered_fixture_is_non_definitive(tmp_path):
    """DEC-27 acceptance: delete one fixture ⇒ nonzero + non-definitive. Detecting it
    needs no knowledge of WHICH file — a single non-empty `missing` is enough."""
    _copy_real_corpus(tmp_path)
    victim = _a_shipped_fixture_name(tmp_path)
    (tmp_path / victim).unlink()

    r = resolver_replay_report(tmp_path)
    assert r.definitive is False
    assert r.rate is None
    assert victim in r.missing
    assert r.extra == frozenset()


def test_renaming_a_rostered_fixture_to_bak_is_non_definitive(tmp_path):
    """DEC-27 acceptance: rename to `*.json.bak` ⇒ same. The `.bak` no longer matches
    the `*.json` glob, so the rostered original is simply MISSING."""
    _copy_real_corpus(tmp_path)
    victim = _a_shipped_fixture_name(tmp_path)
    (tmp_path / victim).rename(tmp_path / (victim + ".bak"))

    r = resolver_replay_report(tmp_path)
    assert r.definitive is False
    assert victim in r.missing
    assert r.extra == frozenset(), "a .bak is not a *.json, so it is not an extra either"


def test_moving_a_rostered_fixture_into_a_subdir_is_non_definitive(tmp_path):
    """DEC-27 acceptance: move to a subdir ⇒ same. The glob is non-recursive, so a
    fixture in `sub/` leaves the observed set and reads MISSING."""
    _copy_real_corpus(tmp_path)
    victim = _a_shipped_fixture_name(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / victim).rename(sub / victim)

    r = resolver_replay_report(tmp_path)
    assert r.definitive is False
    assert victim in r.missing


def test_adding_an_unrostered_fixture_is_non_definitive(tmp_path):
    """DEC-27 acceptance: add an unrostered fixture ⇒ nonzero + non-definitive. An
    unrostered fixture is a registration defect; tolerating it would reopen
    "de-roster a scenario to drop it from the denominator"."""
    _copy_real_corpus(tmp_path)
    _write(tmp_path, "intruder.json", _fixture(id="intruder"))

    r = resolver_replay_report(tmp_path)
    assert r.definitive is False
    assert r.rate is None
    assert "intruder.json" in r.extra
    assert r.missing == frozenset()


def test_an_empty_roster_over_a_full_corpus_is_non_definitive(tmp_path):
    """DEC-27 acceptance: empty roster ⇒ non-definitive. With `declared == 0` every
    observed fixture becomes an `extra`, and the run cannot be definitive."""
    _copy_real_corpus(tmp_path)
    _roster(tmp_path, [])               # empty the committed denominator

    r = resolver_replay_report(tmp_path)
    assert r.declared == 0
    assert len(r.extra) == 11, r.extra
    assert r.definitive is False
    assert r.rate is None


def test_an_empty_roster_with_no_fixtures_is_non_definitive_solely_from_declared(tmp_path):
    """MUTATION PIN on the `declared > 0` limb of `definitive`. Roster present but
    empty, and NO fixtures on disk: no missing, no extra, no errors — ONLY
    `declared == 0` can suppress definitive here. Dropping that limb would call an
    empty corpus definitive with a rate of None, i.e. a clean run over nothing."""
    _roster(tmp_path, [])
    r = resolver_replay_report(tmp_path)
    assert r.declared == 0
    assert r.missing == frozenset() and r.extra == frozenset()
    assert r.error_count == 0
    assert r.definitive is False        # solely because declared == 0
    assert r.rate is None


def test_the_missing_limb_is_independent_of_errors(tmp_path):
    """MUTATION PIN on the `not missing` limb. A rostered-but-absent fixture with NO
    load error (every file present loads fine) must still be non-definitive. If the
    limb reverted to `error_count == 0` alone, this is the test that fails, and it
    cannot be satisfied by the error path — there is no error here."""
    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1)})
    _roster(tmp_path, ["ok1.json", "ghost.json"])   # ghost.json is declared but never written

    r = resolver_replay_report(tmp_path)
    assert r.errors == (), "no load/replay error may be involved in this test"
    assert r.error_count == 0
    assert r.missing == frozenset({"ghost.json"})
    assert r.extra == frozenset()
    assert r.definitive is False and r.rate is None


def test_the_extra_gate_is_independent_of_the_error_gate(tmp_path):
    """MUTATION PIN on the `not extra` limb. A well-formed but UNROSTERED fixture
    loads with zero errors, yet must still suppress the rate. Only the `extra` limb
    can do that here — the error gate is not involved."""
    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1)})
    _write(tmp_path, "extra.json", _avoidable_fx(2))   # valid fixture, NOT in roster

    r = resolver_replay_report(tmp_path)
    assert r.errors == (), "the extra fixture loads cleanly; no error path is involved"
    assert r.error_count == 0
    assert r.extra == frozenset({"extra.json"})
    assert r.missing == frozenset()
    assert r.definitive is False and r.rate is None


def test_an_unrostered_file_is_reported_by_name_not_merely_counted(tmp_path):
    """The operator has to know WHICH file drifted, not just that something did — a
    count is not actionable, the name is. This also pins that an `extra` does not
    masquerade as a load error."""
    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1)})
    _write(tmp_path, "stray.json", _avoidable_fx(2))

    scenarios, errors, _declared, missing, extra = load_corpus(tmp_path)
    assert sorted(s.scenario_id for s in scenarios) == ["ok1", "ok2"]
    assert errors == [], "an unrostered file is not a load error"
    assert extra == frozenset({"stray.json"})
    assert missing == frozenset()

    lines = format_resolver_replay(resolver_replay_report(tmp_path))
    assert any("stray.json" in l for l in lines), lines
    assert any("unrostered" in l.lower() for l in lines), lines
    assert not any("DEFINITIVE" in l for l in lines), lines


# ── 12b. the roster itself is validated ─────────────────────────────────────
def test_a_missing_roster_is_an_error_not_a_silent_pass(tmp_path):
    """No `roster.json` at all: there is no committed denominator, so the run is
    non-definitive and every fixture on disk is an unrostered `extra`."""
    _write(tmp_path, "a.json", _fixture(id="a"))   # a fixture, but NO roster.json
    scenarios, errors, declared, missing, extra = load_corpus(tmp_path)
    assert declared == frozenset()
    assert any("roster.json not found" in e for e in errors), errors
    assert extra == frozenset({"a.json"})
    r = resolver_replay_report(tmp_path)
    assert r.definitive is False and r.rate is None


def test_a_malformed_roster_is_an_error(tmp_path):
    _write(tmp_path, "a.json", _fixture(id="a"))
    _write(tmp_path, ROSTER_FILENAME, "{not json")
    _scenarios, errors, declared, _missing, _extra = load_corpus(tmp_path)
    assert declared == frozenset()
    assert any(ROSTER_FILENAME in e and "invalid JSON" in e for e in errors), errors
    assert resolver_replay_report(tmp_path).definitive is False


@pytest.mark.parametrize("entry,needle", [
    ("sub/a.json", "path separators"),
    ("a.txt", "*.json"),
    ("roster.json", "roster.json itself"),
    ("", "empty"),
])
def test_roster_rejects_a_malformed_entry(tmp_path, entry, needle):
    """The roster names bare `*.json` fixture filenames — not paths, not the roster
    itself. A malformed entry is refused loudly, not silently coerced."""
    _write(tmp_path, ROSTER_FILENAME, {"schema_version": 1, "fixtures": [entry]})
    with pytest.raises(FixtureError) as exc:
        load_roster(tmp_path)
    assert needle in str(exc.value)


def test_roster_rejects_a_duplicate_entry(tmp_path):
    _write(tmp_path, ROSTER_FILENAME,
           {"schema_version": 1, "fixtures": ["a.json", "a.json"]})
    with pytest.raises(FixtureError) as exc:
        load_roster(tmp_path)
    assert "duplicate" in str(exc.value)


def test_roster_json_is_not_itself_counted_as_a_scenario(tmp_path):
    """`roster.json` matches `*.json` but is the denominator, never a scenario. It
    must be excluded from both the observed set and the load loop — otherwise it would
    be an `extra` (unrostered) AND a malformed fixture (no schema_version)."""
    _corpus(tmp_path, {"a.json": _fixture(id="a")})
    scenarios, errors, declared, missing, extra = load_corpus(tmp_path)
    assert [s.scenario_id for s in scenarios] == ["a"]
    assert errors == [], errors
    assert ROSTER_FILENAME not in extra
    assert declared == frozenset({"a.json"})
    assert missing == frozenset() and extra == frozenset()


# ── 13. the `{root}` idiom means ONE thing, and the replay proves WHERE ──────
#
# `_contained` claimed a `{root}`-prefixed value was "contained by construction"
# because `_expand` expanded it. That was true of `files_produced` only. In
# `files[].path` and `granted_roots[]` the token was joined VERBATIM, so a
# fixture using the documented idiom created a real directory NAMED `{root}` and
# granted `<workdir>/{root}/out` to the store source #1 re-gates through.
#
# The pre-existing test parametrized `{root}/out/prior.md` over both fields and
# asserted only that the raw string ROUND-TRIPPED through the loader - which the
# broken code did perfectly. Round-tripping a value says nothing about what the
# value AUTHORISES.
def test_the_root_idiom_lands_inside_the_workdir_not_in_a_literal_root_dir(tmp_path):
    """Replays the idiom and checks WHERE things landed, not what the string was."""
    wd = tmp_path / "wd"
    wd.mkdir()
    p = _write(tmp_path, "s.json", _fixture(
        files=[{"path": "{root}/out/f.txt", "text": "hi"}],
        granted_roots=["{root}/out"],
        files_produced=["{root}/out/f.txt"]))

    res = replay_scenario(load_scenario(p), workdir=wd)
    assert res.error == "", res.error

    assert not (wd / "{root}").exists(), (
        "the token was joined verbatim: a literal '{root}' directory was created")
    assert (wd / "out" / "f.txt").is_file(), sorted(
        q.relative_to(wd).as_posix() for q in wd.rglob("*"))


def test_the_root_idiom_grants_the_expanded_root_to_the_real_store(tmp_path):
    """The granted root is the half that matters: source #1 re-gates through it.

    Asserted against the REAL `GrantedRootsStore` the replay wrote to, not
    against the directory tree, because "a folder exists" and "this path is
    granted" are different claims.
    """
    from systemu.runtime.granted_roots import GrantedRootsStore
    from systemu.vault.vault import Vault

    wd = tmp_path / "wd"
    wd.mkdir()
    p = _write(tmp_path, "s.json", _fixture(granted_roots=["{root}/out"]))
    res = replay_scenario(load_scenario(p), workdir=wd)
    assert res.error == "", res.error

    roots = list(GrantedRootsStore(base_dir=Vault(str(wd / "_vault")).root).list_roots())
    assert roots, "the replay granted nothing"
    assert not any("{root}" in str(x) for x in roots), (
        f"a literal '{{root}}' segment was granted: {roots}")
    assert any(Path(str(x)).name == "out" for x in roots), roots


def test_the_two_root_spellings_are_now_the_same_path(tmp_path):
    """`{root}/out` and `out` must materialise identically. One idiom, one meaning
    - the defect was that ONE helper validated three fields that then disagreed."""
    made = {}
    for label, spelling in (("token", "{root}/out/f.txt"), ("plain", "out/f.txt")):
        wd = tmp_path / label
        wd.mkdir()
        p = _write(tmp_path, f"{label}.json", _fixture(
            id=label, files=[{"path": spelling, "text": "hi"}]))
        assert replay_scenario(load_scenario(p), workdir=wd).error == ""
        made[label] = sorted(q.relative_to(wd).as_posix()
                             for q in wd.rglob("*") if "_vault" not in q.parts)
    assert made["token"] == made["plain"] == ["out", "out/f.txt"], made


def test_a_root_prefixed_escape_is_still_refused_at_replay(tmp_path):
    """Expanding the token must not become a way to LEAVE the workdir.

    `_contained` refuses `{root}/../x` at load; this pins the replay-time half so
    the containment is by CHECK rather than by the accident of a relative join.
    """
    from systemu.runtime.resolver_replay import _expanded_within

    wd = tmp_path / "wd"
    wd.mkdir()
    tokens = {"root": str(wd)}
    # the contained form comes back EXPANDED (this is the half `files_produced`
    # always had and the other two fields did not)
    assert _expanded_within(wd, "{root}/out", tokens, "f", "w") == f"{wd}/out"
    assert _expanded_within(wd, "out", tokens, "f", "w") == "out", (
        "a plain relative path must pass through untouched")
    with pytest.raises(FixtureError):
        _expanded_within(wd, "{root}/../sneaky", tokens, "granted_roots[]", "w")
    with pytest.raises(FixtureError):
        _expanded_within(wd, str(tmp_path / "outside"), tokens, "files[].path", "w")


@pytest.mark.parametrize("field", ["files", "granted_roots", "files_produced"])
def test_replay_time_containment_holds_when_the_load_guard_is_bypassed(tmp_path, field):
    """Pins the containment CHECK AT EACH CALL SITE, for all three fields.

    `_contained` refuses an escaping value at LOAD, so no loadable fixture can
    reach the replay-time check — which means a corpus-driven test cannot tell a
    live check from a deleted one. So the Scenario is built DIRECTLY, the way a
    caller holding the dataclass would, and the replay must still refuse.

    This is the only pin `files_produced` has: its string was already expanded
    before this change, so removing its check alters nothing a round-trip or a
    digest comparison could see.
    """
    from systemu.runtime.resolver_replay import Scenario

    wd = tmp_path / "wd"
    wd.mkdir()
    escape = tmp_path / "ESCAPED"
    if field == "files":
        kw = {"files": ({"path": str(escape / "f.txt"), "text": "x"},)}
    else:
        kw = {field: (str(escape),)}

    sc = Scenario(
        scenario_id="bypass", title="t", source_path=tmp_path / "none.json",
        objective={"id": 1, "goal": "g", "success_criteria": "s"},
        capability={"name": "t_tool",
                    "parameters_schema": {"widget": {"type": "string"}}},
        situation={"services": [], "capabilities": [], "roots": [],
                   "credentials": [], "profile": {}, "declared_intents": []},
        asks=(), **kw)

    res = replay_scenario(sc, workdir=wd)
    assert res.error, f"{field}: an escaping path replayed without error"
    assert "outside the replay workdir" in res.error, res.error
    assert not escape.exists(), f"{field}: the replay touched {escape}"


def test_the_shipped_corpus_does_not_rely_on_the_broken_spelling():
    """The headline 6/9 must not have been resting on the accident. If any shipped
    fixture had used `{root}` in `files[]`/`granted_roots`, the fix would MOVE
    where its files land - so this states the precondition explicitly."""
    for p, d in _corpus_payloads():
        for entry in d.get("files") or []:
            assert "{root}" not in str(entry.get("path", "")), p.name
        for g in d.get("granted_roots") or []:
            assert "{root}" not in str(g), p.name


# ── 14. the CLI's exit status, in the one state the command exists for ──────
def test_debug_resolver_replay_cli_exits_nonzero_on_an_incomplete_corpus(tmp_path):
    """A scripted caller reads `$?`, not prose. Exiting 0 over a NON-DEFINITIVE
    corpus reports success in exactly the state this command was built to
    surface — and it is the only render path a script can act on."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1)})
    _write(tmp_path, "bad.json", "{not json")
    _roster(tmp_path, ["ok1.json", "bad.json"])

    res = CliRunner().invoke(cli_commands.debug_group,
                             ["resolver-replay", "--corpus", str(tmp_path)])
    assert "INCOMPLETE" in res.output, res.output
    assert res.exit_code != 0, (
        f"incomplete corpus exited {res.exit_code}; a script cannot see the failure")


def test_debug_resolver_replay_cli_exits_nonzero_on_roster_drift(tmp_path):
    """Roster drift reaches the exit status too — otherwise the silent-drop defect
    stays invisible to precisely the caller that cannot read the banner. Here the
    drift is an unrostered `extra` with ZERO load errors, so ONLY the roster
    reconciliation can drive the non-zero exit."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    _corpus(tmp_path, {"ok1.json": _avoidable_fx(1)})
    _write(tmp_path, "hidden.json", _avoidable_fx(2))   # unrostered => extra, no load error

    res = CliRunner().invoke(cli_commands.debug_group,
                             ["resolver-replay", "--corpus", str(tmp_path)])
    assert res.exit_code != 0, res.output
    assert "hidden.json" in res.output, res.output


def test_debug_resolver_replay_cli_still_exits_zero_on_a_healthy_corpus():
    """The other side of the gate: a complete run must stay a success."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    res = CliRunner().invoke(cli_commands.debug_group,
                             ["resolver-replay", "--corpus", str(REAL_CORPUS)])
    assert res.exit_code == 0, res.output
    assert "DEFINITIVE" in res.output
