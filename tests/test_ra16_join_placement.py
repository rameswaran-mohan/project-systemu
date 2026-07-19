"""R-A16 §5.9 — source-level pins for the avoidable-ask answer-join.

Isolated in its own module ON PURPOSE: it reads source via ``inspect.getsource``, and
``tests/conftest.py`` auto-tags a WHOLE MODULE ``source_sensitive`` on that. Folded
into ``test_ra16_avoidable_ask_signal.py`` it would have dropped all the behavioural
pins there — the secret-exclusion ones included — out of the edit-safe gate
(``pytest -m "not source_sensitive"``, GATE-TIER / DEC-14).
"""
from __future__ import annotations

import inspect
import re


def test_join_fires_after_the_idempotency_stamp_not_at_coercion():
    """``reconcile_resolved_harness_grants`` RETRIES any row whose dispatch raised —
    ``harness_grant_dispatched`` is stamped only after a successful
    ``resume_after_grant``. Recording the avoidable-ask outcome at the answer-coercion
    point would therefore double-count the same answer on every retry, and would
    record an answer that was never applied to the run. The join must sit AFTER the
    stamp, and must appear exactly once."""
    from systemu.scheduler import jobs
    src = inspect.getsource(jobs.reconcile_resolved_harness_grants)
    assert src.count("record_bundled_ask_outcomes(") == 1, (
        "exactly one call site — a second one (e.g. left behind at the coercion "
        "point) restores the double-count")
    stamp = src.index('decision.context["harness_grant_dispatched"] = True')
    call = src.index("record_bundled_ask_outcomes(")
    assert call > stamp, (
        "record_bundled_ask_outcomes must be called AFTER the "
        "harness_grant_dispatched stamp, otherwise a retried dispatch "
        "double-records the same answer")


def test_the_key_id_parser_does_not_silently_duplicate_guard_3():
    """Keeps the untrusted-``candidate_ref`` shape check INDEPENDENTLY killable.

    ``_ref_key_id`` used to re-validate via ``_is_value_ref``. The corpus stayed safe,
    but deleting guard 3 then changed nothing observable — no behavioural test could
    hold it in place, and a reviewer reading the recorder would see a guard that was
    already dead. This is a source pin because the property IS "the check appears
    once": there is no behaviour to assert while the redundancy is present."""
    from systemu.runtime import replay_metrics as rm
    src = inspect.getsource(rm._ref_key_id)
    assert "_is_value_ref" not in src, (
        "_ref_key_id must stay a pure parser. Re-validating the ref shape here makes "
        "the recorder's guard-3 check unkillable — remove it and no test fails.")
    body = inspect.getsource(rm.record_ask_avoidable)
    assert body.count("_is_value_ref(cand_ref)") == 1, (
        "guard 3 must be exactly one check on the untrusted candidate_ref")


def test_binder_ref_prefix_allowlist_covers_every_bind_return_site():
    """The OTHER half of the fixture-realism guard (F1's meta-lesson).

    ``test_ra16_avoidable_ask_signal.BINDER_REF_PREFIXES`` is the set of
    ``bound_value_ref`` namespaces a REAL bind source emits; the sibling module uses it
    to reject any fixture built on a shape production never produces. That list is only
    as good as its agreement with the binder, so pin it against the binder's ACTUAL
    return sites: a NEW bind source introducing a new namespace fails here until the
    allowlist is updated deliberately (and the realism pin regains its teeth)."""
    from systemu.runtime import requirement_binder as rb
    from test_ra16_avoidable_ask_signal import BINDER_REF_PREFIXES

    src = inspect.getsource(rb)
    # every f-string return site of the form  f"<namespace>:...
    emitted = {f"{ns}:" for ns in re.findall(r'f"([a-z][a-z0-9_]*):', src)}
    # the one computed namespace: f"schema_{schema_value_kind or 'value'}:{key}"
    if 'f"schema_{schema_value_kind' in src:
        emitted |= {"schema_default:", "schema_const:", "schema_enum:", "schema_value:"}

    unknown = emitted - set(BINDER_REF_PREFIXES)
    assert not unknown, (
        f"the binder emits bound_value_ref namespace(s) the realism allowlist does not "
        f"know about: {sorted(unknown)}. Add them to BINDER_REF_PREFIXES in "
        f"test_ra16_avoidable_ask_signal.py, and make sure the new source stamps a "
        f"bound_value_digest (or deliberately does not).")
    assert len(emitted) >= 8, (
        "the scan found suspiciously few bind namespaces — the regex has gone blind "
        "and the realism pin is no longer anchored to the binder")
