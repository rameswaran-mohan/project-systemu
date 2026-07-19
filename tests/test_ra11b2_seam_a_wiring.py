# tests/test_ra11b2_seam_a_wiring.py
"""R-A11b-2 Task 4 — Seam A is wired into execute() BEFORE the kind=tool
arbitrate, guarded to kind=tool, fail-safe, and cites the pass. getsource
structural guards (the end-to-end reuse behavior is covered in Task 5)."""
import inspect

from systemu.runtime.shadow_runtime import ShadowRuntime


def _execute_src():
    return inspect.getsource(ShadowRuntime.execute)


def test_discovery_pass_imported_and_called():
    src = _execute_src()
    assert "discovery_pass" in src
    assert "deployed_enabled_catalog" in src


def test_seam_a_runs_before_the_forge_arbitrate():
    src = _execute_src()
    # the discovery call must appear before the main kind=tool arbitrate line
    i_disc = src.find("deployed_enabled_catalog(")
    i_arb = src.find("_verdict = _gov.arbitrate(_req, context=_arb_ctx)")
    assert 0 < i_disc < i_arb, "discovery pass must precede the forge arbitrate"


def test_seam_a_guarded_to_kind_tool():
    src = _execute_src()
    # the discovery block is guarded on the TOOL kind (never runs for INPUT/etc.)
    assert "HarnessKind.TOOL" in src
    # it populates enabled_tools and stashes reuse_tool_id
    assert "enabled_tools" in src
    assert "reuse_tool_id" in src


def test_seam_a_stashes_discovery_and_writes_miss_audit():
    src = _execute_src()
    assert "\"discovery\"" in src or "'discovery'" in src
    # the miss-audit reuses the sanctioned manual ledger-append idiom
    assert "_gov._ledger_append(" in src


def test_seam_a_records_the_world_model_discovery_note_both_ways():
    """R-W1 slice-2c — the WM-2 negative-fact loop hangs off this seam: a MISS persists
    'searched and did not find', a HIT invalidates any stale note (CAP-5)."""
    src = _execute_src()
    assert "world_model_discovery" in src
    assert "record_discovery_miss(" in src
    assert "clear_discovery_miss(" in src
    # the write must sit INSIDE the kind=tool discovery block, after the pass ran
    i_disc = src.find("deployed_enabled_catalog(")
    i_note = src.find("record_discovery_miss(")
    assert 0 < i_disc < i_note


def test_the_world_model_note_cannot_break_the_reuse_decision():
    """It hangs off the forge path, so it carries its OWN try/except — a store problem
    must never change whether a tool is reused."""
    src = _execute_src()
    i_note = src.find("world_model_discovery")
    assert i_note > 0
    # a `try:` opens between the reuse decision and the note, and the note's guard
    # precedes the arbitrate that consumes the decision
    assert "try:" in src[src.find("reuse_score"):i_note]
    assert i_note < src.find("_verdict = _gov.arbitrate(_req, context=_arb_ctx)")


def test_seam_a_does_not_feed_the_world_model_note_into_the_request_spec():
    """STORE-WRITE-ONLY: the note must not be stashed on _req.spec, which flows on into
    forge/approval surfaces. Planner input stays byte-identical this slice."""
    src = _execute_src()
    for leak in ("_req.spec[\"prior_miss\"]", "_req.spec['prior_miss']",
                 "_req.spec[\"negative\"]", "_req.spec['negative']"):
        assert leak not in src


def test_param_input_sites_are_not_touched():
    """The two INPUT arbitrate sites (:4886 scroll-params, :6210 missing-required)
    must NOT gain a discovery pass — reuse is a kind=tool-only concern."""
    src = _execute_src()
    # discovery must not appear adjacent to the missing-required INPUT arbitrate
    i_missing = src.find("result.parsed.get(\"harness_request\")")
    i_disc = src.find("deployed_enabled_catalog(")
    # discovery is in the REQUEST_HARNESS branch, which precedes the TOOL_CALL
    # missing-required branch
    assert 0 < i_disc < i_missing
