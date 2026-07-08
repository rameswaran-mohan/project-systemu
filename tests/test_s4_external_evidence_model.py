"""S4 wave1 Step 1 — the ExternalEvidence model.

ExternalEvidence is the substrate for fail-closed external-effect credit: a
persisted record that a deterministic matcher (S3 / R-A7, a LATER wave) has
observed real external ground-truth for an objective's effect. S4 NEVER sets
``confirmed=True`` — it only defines the model, the v5 store, and the
fail-closed read helper. ``confirmed`` is a plain bool set ONLY by a
deterministic matcher; the gate never trusts an LLM to set it.
"""
from __future__ import annotations

from systemu.core.models import ExternalEvidence


def test_defaults_confirmed_false():
    ev = ExternalEvidence(objective_id=1)
    assert ev.confirmed is False          # fail-closed default — no credit until proven
    assert ev.method == ""
    assert ev.detail == ""
    assert ev.stamped_at is None
    assert ev.objective_id == 1


def test_round_trips_via_json():
    ev = ExternalEvidence(
        objective_id=1, confirmed=True, method="api_readback", detail="x",
        stamped_at="2026-07-07T00:00:00+00:00",
    )
    dumped = ev.model_dump(mode="json")
    re = ExternalEvidence.model_validate(dumped)
    assert re == ev
    assert re.objective_id == 1
    assert re.confirmed is True
    assert re.method == "api_readback"
    assert re.detail == "x"


def test_confirmed_is_plain_bool():
    """confirmed is a plain bool — S4 never coerces/derives it; it is set ONLY by
    a deterministic matcher (S3/R-A7), NEVER by an LLM. Assert the type contract."""
    ev = ExternalEvidence(objective_id=7, confirmed=True)
    assert isinstance(ev.confirmed, bool)
    assert ev.confirmed is True
    ev2 = ExternalEvidence(objective_id=7)
    assert isinstance(ev2.confirmed, bool)
    assert ev2.confirmed is False
