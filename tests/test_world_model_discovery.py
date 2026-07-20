"""R-W1 (W-A slice-2c) — the WM-2 discovery negative-fact loop + the fenced read surface.

Pins the three properties that make this slice safe to land:

  * untrusted content can NEVER assert absence (denial-of-discovery);
  * a negative fact is only written when we ACTUALLY looked (absence is not evidence);
  * a fact READ from the store can never carry a taint that permits a silent bind, and
    the launderable fields never reach a prompt at all.

FIXTURE DISCIPLINE: every ``DiscoveryResult`` below comes from RUNNING the real
``discovery_pass``, never from a hand-built stand-in — fixture unrealism is the dominant
defect class in this repo. :func:`test_fixture_shapes_come_from_the_real_producer` pins
that discipline directly.
"""
from __future__ import annotations

import inspect
import json

import pytest

from types import SimpleNamespace

from systemu.runtime import world_model as wm
from systemu.runtime import world_model_discovery as wmd
from systemu.runtime import world_query as wq
from systemu.runtime.discovery_pass import DiscoveryResult, discovery_pass
from systemu.runtime.world_model import Fact, FactStore, NegativeFact, ProvStep


def _vault(tmp_path):
    # FactStore touches exactly one attribute of a vault — ``.root`` (a Path). The real
    # SqliteVault sets ``self.root: Path`` for precisely this file-side contract, so this
    # stand-in is shape-faithful for what the store actually uses.
    return SimpleNamespace(root=tmp_path)


#: A realistic DEPLOYED+enabled catalog row, in the shape ``deployed_enabled_catalog``
#: emits (id/name/description/parameter_names).
_CATALOG = [{"id": "t1", "name": "read_pdf", "description": "read a pdf",
             "parameter_names": ["path"]}]


def _real_miss():
    """A genuine MISS, produced by running the real pass."""
    return discovery_pass("send_invoice", "email an invoice", _CATALOG)


def _real_hit():
    """A genuine HIT, produced by running the real pass."""
    return discovery_pass("read_pdf", "read a pdf", _CATALOG)


def _real_empty_catalog():
    """A pass over NO catalog — searched=0, i.e. we ranked nothing."""
    return discovery_pass("send_invoice", "email an invoice", [])


# ── fixture realism ───────────────────────────────────────────────────────────

def test_fixture_shapes_come_from_the_real_producer():
    """The producer must actually emit every field this module reads, and the fixtures
    above must be producer OUTPUT — not a hand-built shape the producer could never
    make. Fails loudly if ``DiscoveryResult`` is refactored out from under the loop."""
    miss, hit, empty = _real_miss(), _real_hit(), _real_empty_catalog()
    for r in (miss, hit, empty):
        assert isinstance(r, DiscoveryResult)
    produced = {f.name for f in __import__("dataclasses").fields(DiscoveryResult)}
    # every attribute the loop consumes must exist on the REAL producer output
    consumed = {"reuse_tool_id", "searched", "best_score", "floor"}
    assert consumed <= produced, f"loop reads fields the producer cannot emit: {consumed - produced}"
    # and the fixtures must genuinely differ in the way the loop branches on
    assert miss.reuse_tool_id is None and miss.searched > 0     # looked, found nothing
    assert hit.reuse_tool_id == "t1"                            # looked, found it
    assert empty.searched == 0                                  # looked at nothing


# ── rule 1: content can never assert absence ──────────────────────────────────

def test_a_negative_fact_may_not_be_content_derived():
    # Denial-of-discovery: content that can say "there is nothing here" would suppress a
    # real search for the whole TTL. Refused at CONSTRUCTION, not merely down-ranked.
    with pytest.raises(ValueError):
        NegativeFact(scope="capability:x", origin_class="content_derived")


def test_a_content_derived_negative_on_disk_is_dropped_on_read(tmp_path):
    # Defence in depth: even if such a row reaches the file by another route, loading it
    # DROPS it — the suppression vanishes and the caller re-searches. Fail-closed here
    # means failing towards LOOKING.
    s = FactStore(_vault(tmp_path))
    s.put_negative(NegativeFact(scope="capability:a", origin_class="systemu_authored"))
    raw = json.loads(s._negatives_path.read_text(encoding="utf-8"))
    raw["negatives"][0]["origin_class"] = "content_derived"      # poison it on disk
    s._negatives_path.write_text(json.dumps(raw), encoding="utf-8")
    assert s.all_negatives() == []
    assert s.query_negative("capability:a") is None              # suppression gone


def test_the_writer_stamps_systemu_authored_and_takes_no_origin_argument(tmp_path):
    v = _vault(tmp_path)
    neg = wmd.record_discovery_miss(v, "send_invoice", _real_miss())
    assert neg is not None and neg.origin_class == "systemu_authored"
    # STRUCTURAL: there is no parameter through which a caller could supply an origin,
    # so no call site can ever talk the writer into a weaker stamp.
    params = set(inspect.signature(wmd.record_discovery_miss).parameters)
    assert "origin_class" not in params and "origin" not in params


def test_the_default_negative_origin_is_not_content_derived():
    # A row written by an earlier slice carries no origin_class; it must default to a
    # TRUSTED stamp, never to the untrusted one.
    assert NegativeFact(scope="s").origin_class == "systemu_authored"
    assert "content_derived" not in wm.NEGATIVE_ORIGIN_CLASSES


# ── rule 2: absence is only recorded when we actually looked ──────────────────

def test_a_miss_over_a_real_catalog_is_recorded_with_what_and_when(tmp_path):
    v = _vault(tmp_path)
    neg = wmd.record_discovery_miss(v, "send_invoice", _real_miss())
    assert neg is not None
    stored = FactStore(v).query_negative("capability:send_invoice")
    assert stored is not None
    assert stored.probes and stored.recorded_at          # cites WHAT was probed and WHEN
    assert stored.ttl_seconds == wmd.DISCOVERY_MISS_TTL_SECONDS


def test_an_empty_catalog_records_NOTHING_because_absence_is_not_evidence(tmp_path):
    # searched=0 means we ranked nothing at all. Recording a negative here would
    # manufacture absence out of our OWN empty output — the exact shape of the bug that
    # once flagged ~20 present-on-disk files as "may be gone".
    v = _vault(tmp_path)
    assert wmd.record_discovery_miss(v, "send_invoice", _real_empty_catalog()) is None
    assert FactStore(v).all_negatives() == []


def test_a_hit_is_not_a_miss(tmp_path):
    v = _vault(tmp_path)
    assert wmd.record_discovery_miss(v, "read_pdf", _real_hit()) is None
    assert FactStore(v).all_negatives() == []


def test_a_nameless_request_records_no_catch_all_note(tmp_path):
    # A note keyed on the empty string would suppress every future unnamed search at once.
    v = _vault(tmp_path)
    assert wmd.scope_for("") == ""
    assert wmd.record_discovery_miss(v, "", _real_miss()) is None
    assert FactStore(v).all_negatives() == []


def test_the_citation_never_asserts_a_false_comparison_on_a_high_scoring_miss(tmp_path):
    """A miss routinely scores ABOVE the reuse floor — ``discovery_pass`` reuses only on
    an EXACT name match, so a strong fuzzy match under a different name is still a miss.
    The citation must therefore record the score as an observation, never as a claim that
    the score fell short. False-in-the-citation is worse than absent: a handoff is
    supposed to be able to trust a negative fact."""
    cat = [{"id": "t1", "name": "read_pdf", "description": "read a pdf file",
            "parameter_names": ["path"]}]
    r = discovery_pass("read_pdf_v2", "read a pdf file", cat)
    assert r.reuse_tool_id is None and r.best_score > r.floor     # a HIGH-scoring miss
    probes = wmd._probes_for(r)
    joined = " ".join(probes)
    assert f"{r.best_score:.1f}<" not in joined                   # no false "<floor" claim
    assert "no exact name match" in joined                        # the REAL reason
    # and the same holds once it is persisted
    neg = wmd.record_discovery_miss(_vault(tmp_path), "read_pdf_v2", r)
    assert neg is not None and f"{r.best_score:.1f}<" not in " ".join(neg.probes)


def test_scope_is_normalised_so_one_search_is_not_re_paid_under_a_variant():
    assert wmd.scope_for("Send_Invoice") == wmd.scope_for("send invoice") == "capability:send_invoice"


# ── rule 3: a note is invalidated on write, not only by TTL ───────────────────

def test_a_later_hit_clears_the_stale_note(tmp_path):
    v = _vault(tmp_path)
    wmd.record_discovery_miss(v, "read_pdf", discovery_pass("read_pdf", "x", []))  # no-op
    wmd.record_discovery_miss(v, "send_invoice", _real_miss())
    assert FactStore(v).query_negative("capability:send_invoice") is not None
    assert wmd.clear_discovery_miss(v, "send_invoice") is True
    # the capability now exists — the note must NOT wait out its TTL
    assert FactStore(v).query_negative("capability:send_invoice") is None


def test_clearing_an_absent_note_is_a_no_op(tmp_path):
    assert wmd.clear_discovery_miss(_vault(tmp_path), "nothing_here") is False


def test_the_note_expires_and_the_caller_researches(tmp_path):
    v = _vault(tmp_path)
    FactStore(v).put_negative(NegativeFact(
        scope="capability:send_invoice", recorded_at="2026-07-18T00:00:00+00:00",
        ttl_seconds=3600, origin_class="systemu_authored"))
    assert wmd.recent_discovery_miss(v, "send_invoice", now="2026-07-18T00:10:00+00:00") is not None
    assert wmd.recent_discovery_miss(v, "send_invoice", now="2026-07-18T02:00:00+00:00") is None


def test_a_discovery_miss_expires_faster_than_the_generic_default():
    # WM-2: absence expires fast; a local catalog can change the moment a forge lands.
    assert wmd.DISCOVERY_MISS_TTL_SECONDS <= wm.DEFAULT_NEGATIVE_TTL_SECONDS


def test_the_loop_is_fail_safe_on_a_broken_vault():
    # It hangs off the forge path — a store problem must never break discovery.
    broken = SimpleNamespace()                      # no .root at all
    assert wmd.record_discovery_miss(broken, "x", _real_miss()) is None
    assert wmd.clear_discovery_miss(broken, "x") is False
    assert wmd.recent_discovery_miss(broken, "x") is None


# ── the negative store is BOUNDED (slice-2c is its first production writer) ───

def test_expired_notes_are_pruned_on_write_not_accreted_forever(tmp_path):
    # Expired notes are already invisible to query_negative, so dropping them changes no
    # behaviour — it only stops the file growing with rows that can never be read again.
    s = FactStore(_vault(tmp_path))
    for i in range(wm._MAX_NEGATIVES + 5):
        s.put_negative(NegativeFact(scope=f"capability:old_{i}",
                                    recorded_at="2020-01-01T00:00:00+00:00",
                                    ttl_seconds=1))          # long expired
    s.put_negative(NegativeFact(scope="capability:fresh"))
    stored = {n.scope for n in s.all_negatives()}
    assert "capability:fresh" in stored                      # the live note survives
    assert len(stored) <= wm._MAX_NEGATIVES


def test_the_note_just_written_is_never_the_one_evicted(tmp_path):
    # Eviction is the safe direction (a re-paid search, never a missed one) but it must
    # never discard the note the caller just recorded.
    s = FactStore(_vault(tmp_path))
    for i in range(wm._MAX_NEGATIVES + 20):
        s.put_negative(NegativeFact(scope=f"capability:n_{i}"))   # all LIVE
    assert len(s.all_negatives()) <= wm._MAX_NEGATIVES
    assert s.query_negative(f"capability:n_{wm._MAX_NEGATIVES + 19}") is not None


def test_bounding_never_evicts_a_live_note_at_realistic_volume(tmp_path):
    # The bound is a last resort, not the primary mechanism — a realistic run must not
    # lose anything to it.
    s = FactStore(_vault(tmp_path))
    for i in range(50):
        s.put_negative(NegativeFact(scope=f"capability:n_{i}"))
    assert len(s.all_negatives()) == 50


# ── WM-4/WM-15: the fenced read surface ───────────────────────────────────────

def _operator_stamped_fact():
    """A fact stamped with the MOST trusted origin the store allows — the exact shape a
    poisoned or merely default-y survey produces (the service model defaults every
    service to ``operator``, and the populator stamps confidence 1.0 on everything)."""
    return Fact(fact_id="service:1", kind="service", value="github",
                origin_class="operator", confidence=1.0,
                source_chain=[ProvStep(source_kind="inventory", ref="github")])


def test_a_stored_fact_can_never_yield_a_taint_that_permits_a_silent_bind():
    # THE IMPL-5 pin. The populator copies each entry's DECLARED origin and the service
    # model defaults to `operator`; trusting that at a bind would flip an inventory value
    # from ask→silent. Taint is RE-DERIVED, mirroring requirement_binder._entry_origin.
    f = _operator_stamped_fact()
    assert f.origin_class == "operator"                     # the stored stamp says trusted…
    assert wq.bind_taint_of(f) == "content_derived"         # …the re-derived taint does not
    assert Fact(fact_id="x:1", kind="service", value="v",
                origin_class="systemu_authored").taint_permits_silent_bind is True
    # and the re-derived taint never clears, for any stored fact
    for oc in sorted(wm.ORIGIN_CLASSES):
        g = Fact(fact_id=f"k:{oc}", kind="service", value="v", origin_class=oc)
        assert wq.bind_taint_of(g) == "content_derived"


def test_the_fenced_row_omits_the_launderable_fields():
    # origin_class and confidence are ABSENT from a prompt-facing row, not merely
    # down-ranked — the populator stamps confidence=1.0 on every fact, so emitting it
    # would assert a certainty the store does not have.
    row = wq.fenced_row(_operator_stamped_fact(), None)
    for banned in wq.NEVER_FENCED_FIELDS:
        assert banned not in row
    assert set(row) == set(wq.FENCED_ROW_FIELDS)
    assert row["bind_taint"] == "content_derived"


def test_the_rendered_view_is_fenced_and_leaks_no_origin_or_confidence():
    out = wq.render_facts_for_prompt([_operator_stamped_fact()], query="github")
    assert "untrusted_inventory_data" in out            # the BLOCKER-2 fence is applied
    assert "origin_class" not in out and "confidence" not in out
    assert "github" in out                              # the value itself still renders


def test_staleness_in_a_fenced_row_comes_from_the_surveyor_not_from_our_output():
    # A fact whose kind the survey did NOT cover reads `not_surveyed`, never a claim that
    # it may be gone. Coverage is the surveyor's word, never inferred from our own output.
    f = Fact(fact_id="data_location:1", kind="data_location", value="C:/a/b.pdf",
             origin_class="content_derived", last_confirmed="2026-07-18T00:00:00+00:00")
    survey = wm.SurveyWatermark(at="2026-07-19T00:00:00+00:00",
                                kinds_surveyed=["service"], roots_covered=[])
    assert wq.fenced_row(f, survey)["staleness"] == "not_surveyed"


def test_a_negative_fact_renders_fenced_with_what_and_when():
    neg = NegativeFact(scope="capability:send_invoice", probes=["catalog(n=1)"],
                       recorded_at="2026-07-18T00:00:00+00:00")
    out = wq.render_negative_for_prompt(neg)
    assert "untrusted_inventory_data" in out
    assert "capability:send_invoice" in out and "2026-07-18T00:00:00+00:00" in out


def test_the_fenced_surface_never_raises_on_a_malformed_fact():
    assert "untrusted_inventory_data" in wq.render_facts_for_prompt([object()])
    assert "untrusted_inventory_data" in wq.render_negative_for_prompt(None)


# ── the inversion has LANDED — what the pin protects now ─────────────────────
# This slot used to hold `test_the_planner_input_builder_does_not_reference_the_fenced_
# read_surface`: a SEQUENCING pin asserting that `situational_inventory` never mentions
# world_query/world_model. Its stated purpose was "until the inversion lands, planner
# input must not change, so the rest of slice-2c is verifiable in isolation". W-A's final
# slice IS that inversion, so the premise is spent — the builder now reaches for
# world_query BY DESIGN (`compose_world_view` + the world block in
# `render_situation_for_prompt`).
#
# Deleting it outright would silently drop the protection it stood in for. What actually
# needed guarding was never "no import" — it was "a stored fact cannot reach the planner
# except as fenced, allowlisted, taint-clamped data". That is pinned below, and in
# tests/test_rw1_wa_final.py.


def test_the_planner_input_builder_reaches_the_store_ONLY_through_the_fence():
    """`situational_inventory` may now read the store — through `world_query` and
    nothing else. It must never open a `FactStore` itself: doing so would hand it live
    `Fact` objects carrying `origin_class`/`confidence`, i.e. the exact two fields the
    fenced row exists to withhold, one `json.dumps` away from the planner prompt."""
    import pathlib
    from systemu.runtime import situational_inventory as si
    text = pathlib.Path(si.__file__).read_text(encoding="utf-8", errors="replace")
    assert "world_query" in text                     # the fenced surface IS the seam…
    assert "FactStore(" not in text                  # …and it is the ONLY one
    assert "from systemu.runtime.world_model import" not in text
    assert "world_model.FactStore" not in text


def test_no_stored_fact_field_reaches_the_prompt_outside_the_row_allowlist():
    """The end-to-end property the deleted pin was standing in for, asserted on the
    REAL render rather than on an import graph: whatever the store holds, only the five
    allowlisted fields are rendered, and the taint is the clamped one."""
    import json
    from systemu.runtime import situational_inventory as si
    row = {"fact_id": "f", "kind": "service", "value": "acme",
           "bind_taint": "content_derived", "staleness": "confirmed",
           "origin_class": "operator", "confidence": 1.0, "secret": "hunter2"}
    out = si.render_situation_for_prompt({"services": [], "world_facts": [row]})
    body = out.split("# WORLD MODEL", 1)[1].split("---\n", 1)[1]
    body = body.rsplit("\n</untrusted_inventory_data", 1)[0]
    rendered = json.loads(body)["results"][0]
    assert set(rendered) == set(wq.FENCED_ROW_FIELDS)
    assert rendered["bind_taint"] == "content_derived"
    assert "hunter2" not in out and "confidence" not in out.split("# WORLD MODEL", 1)[1]


def test_the_discovery_loop_adds_nothing_to_the_harness_request_spec():
    # The loop is STORE-WRITE-ONLY. If it ever stashed its note into _req.spec, that dict
    # flows on into forge/approval surfaces — i.e. it could reach a prompt.
    #
    # Gate on the AST, not on the file text: this module's own docstring discusses
    # ``_req.spec`` to explain why it stays out of it, and a substring check would both
    # fail on that prose and be satisfiable by merely rewording it. The AST sees only
    # real attribute access, so the pin tracks BEHAVIOUR.
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(wmd.__file__).read_text(encoding="utf-8", errors="replace"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "spec" not in attrs, "the discovery loop must never read or write a request spec"
