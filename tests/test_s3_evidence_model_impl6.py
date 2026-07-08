"""S3 wave1 Step 0 — extend ExternalEvidence for IMPL-6 (additive fields only).

S4 defined ExternalEvidence (objective_id / confirmed / method / detail /
stamped_at). S3 adds three IMPL-6 fields that a later wave (loop wiring) will
populate as part of the deterministic pre/post-submit idempotency check:

  * ``idempotency_key``  — the idempotency token the submit was tagged with.
  * ``pre_submit_absent`` — a deterministic readback confirmed the effect was
    ABSENT before the submit (the "it wasn't already there" half of a
    create-once proof).
  * ``presubmit_tokens``  — the distinguishing tokens observed absent pre-submit
    (echoed back post-submit ⇒ our action created it, not a pre-existing state).

These are ADDITIVE pydantic fields with defaults. There is NO snapshot bump and
NO migration: the persisted store keeps plain v5 dicts; S4's read helper
(_read_external_ok) only ever inspects the ``confirmed`` key, and _persist writes
model_dump(mode="json") — so extra keys ride along and missing keys default.
"""
from __future__ import annotations

from systemu.core.models import ExternalEvidence


def test_impl6_fields_round_trip_via_json():
    ev = ExternalEvidence(
        objective_id=1,
        idempotency_key="abc",
        pre_submit_absent=True,
        presubmit_tokens=["INV-42", "conf-xyz"],
    )
    dumped = ev.model_dump(mode="json")
    assert dumped["idempotency_key"] == "abc"
    assert dumped["pre_submit_absent"] is True
    assert dumped["presubmit_tokens"] == ["INV-42", "conf-xyz"]
    re = ExternalEvidence.model_validate(dumped)
    assert re == ev
    assert re.idempotency_key == "abc"
    assert re.pre_submit_absent is True
    assert re.presubmit_tokens == ["INV-42", "conf-xyz"]


def test_impl6_field_defaults():
    ev = ExternalEvidence(objective_id=2)
    assert ev.idempotency_key == ""
    assert ev.pre_submit_absent is False
    assert ev.presubmit_tokens == []
    # confirmed still fail-closed False (S3 must not weaken the S4 contract)
    assert ev.confirmed is False


def test_old_v5_style_dict_still_validates_backward_compat():
    """An OLD v5-era persisted dict (no IMPL-6 keys) must still validate — the new
    fields default, so the pre-existing store loads unchanged (no migration)."""
    old = {
        "objective_id": 5,
        "confirmed": True,
        "method": "api_readback",
        "detail": "x",
        "stamped_at": "2026-07-07T00:00:00+00:00",
    }
    ev = ExternalEvidence.model_validate(old)
    assert ev.objective_id == 5
    assert ev.confirmed is True
    assert ev.method == "api_readback"
    # the new IMPL-6 fields take their defaults
    assert ev.idempotency_key == ""
    assert ev.pre_submit_absent is False
    assert ev.presubmit_tokens == []


def test_presubmit_tokens_default_is_not_shared_across_instances():
    """A mutable-default list must not alias across instances (pydantic copies
    per-instance, but assert it so a future refactor can't reintroduce aliasing)."""
    a = ExternalEvidence(objective_id=1)
    b = ExternalEvidence(objective_id=2)
    a.presubmit_tokens.append("leak")
    assert b.presubmit_tokens == []
