"""R-W1 — survey watermark + READ-SIDE derived staleness.

The store is append-only/confirm-in-place: nothing is ever marked stale or removed by a
writer. So "is this fact still true?" is answered at READ time, by comparing a fact's
``last_confirmed`` against what the last survey actually COVERED.

Why coverage matters (this is the whole point): the survey is scope-varying — roots are
grant-scoped, sources have timeouts, and file facts are capped per run. A naive
"not re-seen ⇒ stale" rule would mass-stale valid facts on a slow or narrowed run. These
tests pin that absence is only treated as evidence when the survey genuinely looked.
"""
from __future__ import annotations

from types import SimpleNamespace

from systemu.runtime import world_model as wm
from systemu.runtime.world_model import Fact, FactStore, SurveyWatermark
from systemu.runtime.world_model_populator import populate_from_situation, _coverage
from systemu.runtime.situational_inventory import (
    SituationReport, ConnectedService, CapabilityRef, RootSurvey, FileHandleLite,
)

_T0 = "2026-07-18T00:00:00+00:00"
_T1 = "2026-07-18T06:00:00+00:00"


def _fact(kind="service", value="github", last_confirmed=_T0):
    return Fact(fact_id=f"{kind}:x", kind=kind, value=value,
                origin_class="operator", last_confirmed=last_confirmed)


# ── the three honest verdicts ────────────────────────────────────────────────

def test_a_fact_reseen_by_the_latest_survey_is_confirmed():
    survey = SurveyWatermark(at=_T0, kinds_surveyed=["service"])
    assert wm.staleness_of(_fact(last_confirmed=_T1), survey) == "confirmed"


def test_a_fact_the_survey_covered_but_did_not_resee_is_unconfirmed():
    # the honest "this may be gone" signal — e.g. a service the operator disconnected,
    # while OTHER services were still seen (so the kind genuinely was surveyed).
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["service"])
    assert wm.staleness_of(_fact(last_confirmed=_T0), survey) == "unconfirmed"


def test_a_kind_the_survey_did_not_cover_is_never_called_stale():
    # the case a naive rule gets WRONG: the survey never looked for this kind, so its
    # absence is not evidence of anything.
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["capability"])
    assert wm.staleness_of(_fact(kind="service", last_confirmed=_T0), survey) == "not_surveyed"


def test_no_survey_recorded_yet_reads_unknown():
    assert wm.staleness_of(_fact(), None) == "unknown"


# ── file facts: coverage is scope-sensitive, not just kind-sensitive ─────────

def test_a_file_fact_outside_the_surveyed_roots_is_not_called_stale():
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["data_location"],
                             roots_covered=["C:/Users/me/Invoices"])
    outside = _fact(kind="data_location", value="D:/Other/thing.pdf", last_confirmed=_T0)
    assert wm.staleness_of(outside, survey) == "not_surveyed"
    inside = _fact(kind="data_location", value="C:/Users/me/Invoices/jan.pdf", last_confirmed=_T0)
    assert wm.staleness_of(inside, survey) == "unconfirmed"


def test_a_sibling_directory_sharing_a_prefix_is_not_treated_as_covered():
    # A raw string-prefix test would call all of these "covered" and then stale them.
    # Containment is by path COMPONENT, mirroring the confinement layer.
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["data_location"], roots_covered=["C:/R"])
    for outside in ("C:/Radiology/scan.pdf", "C:/Rentals/lease.pdf", "C:/Rx.txt"):
        f = _fact(kind="data_location", value=outside, last_confirmed=_T0)
        assert wm.staleness_of(f, survey) == "not_surveyed", outside
    assert wm.staleness_of(
        _fact(kind="data_location", value="C:/R/real.pdf", last_confirmed=_T0), survey) == "unconfirmed"


def test_root_matching_normalises_separators_and_trailing_slash():
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["data_location"],
                             roots_covered=["C:\\Users\\me\\Docs\\"])
    f = _fact(kind="data_location", value="C:/Users/me/Docs/a.pdf", last_confirmed=_T0)
    assert wm.staleness_of(f, survey) == "unconfirmed"       # same location, different spelling


# ── timestamps are compared as instants, not as strings ─────────────────────

def test_a_differing_utc_offset_does_not_false_stale():
    # 10:00-05:00 is 15:00Z — AFTER the survey. A lexicographic compare reads "1" < "2"
    # and calls it stale.
    survey = SurveyWatermark(at="2026-07-18T13:00:00+00:00", kinds_surveyed=["service"])
    assert wm.staleness_of(_fact(last_confirmed="2026-07-18T10:00:00-05:00"), survey) == "confirmed"


def test_a_naive_timestamp_is_read_as_utc_not_as_stale():
    survey = SurveyWatermark(at="2026-07-18T13:00:00+00:00", kinds_surveyed=["service"])
    assert wm.staleness_of(_fact(last_confirmed="2026-07-18T13:00:00"), survey) == "confirmed"


def test_an_unparseable_timestamp_is_unknown_never_stale():
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["service"])
    assert wm.staleness_of(_fact(last_confirmed="not-a-timestamp"), survey) == "unknown"


def test_a_never_confirmed_fact_is_unknown_not_gone():
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["service"])
    assert wm.staleness_of(_fact(last_confirmed=None), survey) == "unknown"


def test_latest_survey_is_by_timestamp_not_write_order(tmp_path):
    s = FactStore(SimpleNamespace(root=tmp_path))
    s.record_survey(SurveyWatermark(at=_T1, kinds_surveyed=["service"]))
    s.record_survey(SurveyWatermark(at=_T0, kinds_surveyed=["capability"]))   # written later, older
    assert s.latest_survey().at == _T1


def test_a_watermark_row_without_a_timestamp_is_skipped_not_defaulted(tmp_path):
    # Defaulting `at` would make a malformed row validate to read-time NOW — newer than
    # every fact — staling the entire store in one read.
    import json
    s = FactStore(SimpleNamespace(root=tmp_path))
    s.record_survey(SurveyWatermark(at=_T0, kinds_surveyed=["service"]))
    bad = {"version": 1, "surveys": [{"kinds_surveyed": ["service"]}]}       # no `at`
    s._surveys_path.write_text(json.dumps(bad), encoding="utf-8")
    assert s.all_surveys() == [] and s.latest_survey() is None


def test_a_truncated_file_survey_never_stales_file_facts():
    # the per-run cap means the survey stopped early — it cannot support any conclusion
    # about what it did not reach.
    survey = SurveyWatermark(at=_T1, kinds_surveyed=["data_location"],
                             roots_covered=["C:/Users/me/Invoices"], data_location_cap_hit=True)
    f = _fact(kind="data_location", value="C:/Users/me/Invoices/jan.pdf", last_confirmed=_T0)
    assert wm.staleness_of(f, survey) == "not_surveyed"


# ── the watermark the populator records ──────────────────────────────────────

def test_coverage_marks_only_kinds_that_actually_produced_an_entry():
    # CONSERVATIVE by design: an empty slice is indistinguishable from a timed-out one,
    # so zero entries never counts as coverage.
    rep = SituationReport(
        services=[ConnectedService(name="https://api.github.com", auth_kind="oauth", has_live_token=True)],
        roots=[RootSurvey(path="C:/R", salient=[
            FileHandleLite(path="C:/R/a.pdf", name="a.pdf", ext=".pdf", size=1, mtime=1.0)])],
    )
    from systemu.runtime.world_model_populator import _facts_from_report
    cov = _coverage(rep, _facts_from_report(rep))
    assert cov.kinds_surveyed == ["data_location", "service"]     # capability/credential absent
    assert cov.roots_covered == ["C:/R"]
    assert cov.data_location_cap_hit is False


def test_a_truncated_root_marks_coverage_truncated(tmp_path):
    # THE reproduced defect: the surveyor emits only the top-N files per root, so a root
    # with more than N files is ALWAYS truncated — but the truncation was invisible, and
    # every file beyond N read "may be gone" while sitting on disk. Truncation must come
    # from the surveyor, not be inferred from how many facts we produced.
    from systemu.runtime import situational_inventory as si
    root = tmp_path / "busy"
    root.mkdir()
    for i in range(si._MAX_SALIENT_PER_ROOT + 5):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")

    class _Grants:
        def list_roots(self): return [str(root)]
        def is_within_granted(self, p): return str(p).startswith(str(root))
    surveys = si.build_roots(_Grants())
    assert surveys[0].truncated is True                      # the surveyor says so
    rep = SituationReport(roots=surveys)
    from systemu.runtime.world_model_populator import _facts_from_report
    cov = _coverage(rep, _facts_from_report(rep))
    assert cov.data_location_cap_hit is True
    # …so no file fact is called stale on a listing we know was cut short
    f = _fact(kind="data_location", value=str(root / "f0.txt"), last_confirmed=_T0)
    assert wm.staleness_of(f, SurveyWatermark(**{**cov.model_dump(), "at": _T1})) == "not_surveyed"


def test_an_unreadable_root_is_not_reported_as_covered():
    # The surveyor still emits a row for a vanished/unreadable root so the planner sees
    # the grant — but an empty listing there means "we could not look", never "it's empty".
    rep = SituationReport(roots=[
        RootSurvey(path="C:/live", salient=[
            FileHandleLite(path="C:/live/a.pdf", name="a.pdf", ext=".pdf", size=1, mtime=1.0)]),
        RootSurvey(path="C:/dead_mount", salient=[], truncated=True)])
    from systemu.runtime.world_model_populator import _facts_from_report
    cov = _coverage(rep, _facts_from_report(rep))
    assert cov.roots_covered == ["C:/live"]                  # the dead mount is NOT claimed
    gone = _fact(kind="data_location", value="C:/dead_mount/archive.zip", last_confirmed=_T0)
    assert wm.staleness_of(gone, SurveyWatermark(**{**cov.model_dump(), "at": _T1})) == "not_surveyed"


def test_populate_records_a_watermark_and_it_round_trips(tmp_path):
    rep = SituationReport(
        services=[ConnectedService(name="https://api.github.com", auth_kind="oauth", has_live_token=True)])
    assert populate_from_situation(rep, SimpleNamespace(root=tmp_path)) == 1
    survey = FactStore(SimpleNamespace(root=tmp_path)).latest_survey()
    assert survey is not None and survey.kinds_surveyed == ["service"]


def test_recording_a_survey_never_touches_the_facts_file(tmp_path):
    # record_survey must not become a reader/writer of facts.json — that is what keeps
    # the populator a non-reader of the store.
    s = FactStore(SimpleNamespace(root=tmp_path))
    s.record_survey(SurveyWatermark(at=_T1, kinds_surveyed=["service"]))
    assert not s._facts_path.exists()
    assert s.latest_survey().kinds_surveyed == ["service"]


def test_watermarks_are_bounded_on_disk(tmp_path):
    s = FactStore(SimpleNamespace(root=tmp_path))
    for i in range(wm._MAX_SURVEYS + 10):
        s.record_survey(SurveyWatermark(at=f"2026-07-18T00:00:{i:02d}+00:00"))
    assert len(s.all_surveys()) == wm._MAX_SURVEYS          # oldest dropped, not unbounded


def test_a_disconnected_service_reads_unconfirmed_end_to_end(tmp_path):
    # the scenario this whole mechanism exists for: two services, then one goes away.
    v = SimpleNamespace(root=tmp_path)
    two = SituationReport(services=[
        ConnectedService(name="https://api.github.com", auth_kind="oauth", has_live_token=True),
        ConnectedService(name="https://api.other.com", auth_kind="oauth", has_live_token=True)])
    populate_from_situation(two, v)
    one = SituationReport(services=[
        ConnectedService(name="https://api.github.com", auth_kind="oauth", has_live_token=True)])
    populate_from_situation(one, v)
    store = FactStore(v)
    survey = store.latest_survey()
    verdicts = {f.value: wm.staleness_of(f, survey) for f in store.all_facts()}
    assert verdicts["https://api.github.com"] == "confirmed"
    assert verdicts["https://api.other.com"] == "unconfirmed"    # honestly flagged, not deleted
