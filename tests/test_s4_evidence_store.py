"""S4 wave1 Step 3 — external-evidence store helpers (deterministic, fail-closed).

``_read_external_ok(context, objective_id)`` is the fail-closed gate read: it
returns True ONLY when a persisted ExternalEvidence for that id has
``confirmed is True``. It returns FALSE on absent id, a non-dict store, a
malformed entry, or ANY exception — it NEVER raises (mirroring
``_recredit_blocked_ids``'s defensive posture). No LLM path.

``_persist_external_evidence(context, ExternalEvidence(...))`` writes the
evidence into ``context._external_evidence[str(objective_id)]`` (JSON round-trips
dict keys to str, so the store is str-keyed; the read helper normalises int-vs-str).
"""
from __future__ import annotations

from systemu.core.models import ExternalEvidence
from systemu.runtime.shadow_runtime import (
    _read_external_ok,
    _persist_external_evidence,
)


class _Ctx:
    """Minimal context carrier — just the _external_evidence store attr."""
    def __init__(self, store=None, has_attr=True):
        if has_attr:
            self._external_evidence = store if store is not None else {}


# ── _read_external_ok: True ONLY on a persisted confirmed=True entry ──────────
def test_read_ok_true_only_when_confirmed():
    ctx = _Ctx({"1": {"objective_id": 1, "confirmed": True, "method": "m", "detail": ""}})
    assert _read_external_ok(ctx, 1) is True


def test_read_ok_false_when_confirmed_false():
    ctx = _Ctx({"1": {"objective_id": 1, "confirmed": False}})
    assert _read_external_ok(ctx, 1) is False


def test_read_ok_false_on_absent_id():
    ctx = _Ctx({"2": {"objective_id": 2, "confirmed": True}})
    assert _read_external_ok(ctx, 1) is False


def test_read_ok_false_on_empty_store():
    ctx = _Ctx({})
    assert _read_external_ok(ctx, 1) is False


def test_read_ok_false_on_missing_attr():
    """A context that never grew _external_evidence ⇒ fail-closed False, no raise."""
    ctx = _Ctx(has_attr=False)
    assert _read_external_ok(ctx, 1) is False


def test_read_ok_false_on_non_dict_store():
    ctx = _Ctx(store="corrupt")
    assert _read_external_ok(ctx, 1) is False


def test_read_ok_false_on_malformed_entry():
    """A non-dict entry (or one missing 'confirmed') ⇒ fail-closed False, no raise."""
    assert _read_external_ok(_Ctx({"1": "not-a-dict"}), 1) is False
    assert _read_external_ok(_Ctx({"1": ["also", "wrong"]}), 1) is False
    assert _read_external_ok(_Ctx({"1": {"objective_id": 1}}), 1) is False   # no 'confirmed'
    assert _read_external_ok(_Ctx({"1": None}), 1) is False


def test_read_ok_false_on_none_context():
    """A None context ⇒ fail-closed False, never raises."""
    assert _read_external_ok(None, 1) is False


def test_read_ok_int_vs_str_key_normalised():
    """JSON persists dict keys as str; the read helper normalises so an int
    objective_id still matches a str-keyed store (and vice-versa)."""
    ctx = _Ctx({"5": {"objective_id": 5, "confirmed": True}})
    assert _read_external_ok(ctx, 5) is True        # int arg vs str key
    assert _read_external_ok(ctx, "5") is True       # str arg vs str key


def test_read_ok_only_truthy_bool_confirms():
    """confirmed must be actually True — a truthy non-bool (e.g. 1, 'yes') is a
    malformed entry the fail-closed read must NOT trust as a confirmation."""
    assert _read_external_ok(_Ctx({"1": {"confirmed": 1}}), 1) is False
    assert _read_external_ok(_Ctx({"1": {"confirmed": "yes"}}), 1) is False


# ── _persist_external_evidence: writes into context._external_evidence[str(oid)] ─
def test_persist_writes_str_keyed_entry():
    ctx = _Ctx({})
    ev = ExternalEvidence(objective_id=3, confirmed=True, method="api_readback")
    _persist_external_evidence(ctx, ev)
    assert "3" in ctx._external_evidence
    entry = ctx._external_evidence["3"]
    assert isinstance(entry, dict)
    assert entry["objective_id"] == 3
    assert entry["confirmed"] is True


def test_persist_then_read_round_trip():
    ctx = _Ctx({})
    _persist_external_evidence(ctx, ExternalEvidence(objective_id=9, confirmed=True))
    assert _read_external_ok(ctx, 9) is True
    assert _read_external_ok(ctx, 10) is False


def test_persist_creates_store_when_missing_attr():
    """_persist on a context with no _external_evidence attr creates it (fail-safe,
    never raises)."""
    ctx = _Ctx(has_attr=False)
    _persist_external_evidence(ctx, ExternalEvidence(objective_id=1, confirmed=True))
    assert getattr(ctx, "_external_evidence", {}).get("1", {}).get("confirmed") is True


def test_persist_never_raises_on_bad_input():
    """A None context / bad evidence must not raise (fail-safe)."""
    _persist_external_evidence(None, ExternalEvidence(objective_id=1))
    _persist_external_evidence(_Ctx({}), None)  # bad evidence — swallowed
