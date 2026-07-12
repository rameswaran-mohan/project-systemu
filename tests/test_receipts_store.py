"""Durable, display-only receipts store (fold-in #3)."""
from __future__ import annotations

from systemu.runtime import receipts_store as rs


def test_write_read_round_trip_is_durable(tmp_path):
    rs.write_receipt("exec-1", 1, {"objective_id": 1, "confirmed": True,
                                   "method": "api_readback", "detail": "host-pinned https, fresh",
                                   "stamped_at": "2026-07-13T00:00:00+00:00"}, data_dir=tmp_path)
    got = rs.read_receipts("exec-1", data_dir=tmp_path)
    assert got["1"]["confirmed"] is True
    assert got["1"]["method"] == "api_readback"
    # the file lives OUTSIDE the resume_snapshot (which is deleted on completion),
    # so it survives — read again after a simulated "completion" (no delete).
    assert rs.read_receipts("exec-1", data_dir=tmp_path)["1"]["confirmed"] is True


def test_merge_by_objective_id_keeps_both(tmp_path):
    rs.write_receipt("e", 1, {"objective_id": 1, "confirmed": True, "method": "api_readback"}, data_dir=tmp_path)
    rs.write_receipt("e", 2, {"objective_id": 2, "confirmed": False, "method": "api_readback"}, data_dir=tmp_path)
    got = rs.read_receipts("e", data_dir=tmp_path)
    assert set(got) == {"1", "2"}
    assert got["1"]["confirmed"] is True and got["2"]["confirmed"] is False


def test_confirmed_is_coerced_to_a_real_bool(tmp_path):
    # anything non-True ⇒ claimed (fail-closed): a truthy non-True never reads verified.
    rs.write_receipt("e", 1, {"objective_id": 1, "confirmed": "yes", "method": "m"}, data_dir=tmp_path)
    assert rs.read_receipts("e", data_dir=tmp_path)["1"]["confirmed"] is False


def test_only_display_fields_are_stored_no_secrets(tmp_path):
    rs.write_receipt("e", 1, {"objective_id": 1, "confirmed": True, "method": "api_readback",
                              "detail": "ok", "idempotency_key": "SECRET", "presubmit_tokens": ["t"],
                              "readback_url": "https://x/1"}, data_dir=tmp_path)
    stored = rs.read_receipts("e", data_dir=tmp_path)["1"]
    assert set(stored) <= {"objective_id", "confirmed", "method", "detail", "stamped_at"}
    assert "idempotency_key" not in stored and "readback_url" not in stored


def test_missing_and_corrupt_file_return_empty(tmp_path):
    assert rs.read_receipts("nope", data_dir=tmp_path) == {}
    target = tmp_path / "audit" / "exec_bad" / "receipts.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    assert rs.read_receipts("bad", data_dir=tmp_path) == {}


def test_falsy_eid_and_bad_input_are_noops(tmp_path):
    rs.write_receipt(None, 1, {"confirmed": True}, data_dir=tmp_path)     # no eid → no-op
    rs.write_receipt("e", 1, "not a dict", data_dir=tmp_path)             # bad input → no-op
    assert rs.read_receipts("e", data_dir=tmp_path) == {}
    assert rs.read_receipts(None, data_dir=tmp_path) == {}


def test_persist_external_evidence_writes_durable_receipt_and_leaves_credit_path(monkeypatch):
    """The write hook is ADDITIVE: _persist_external_evidence still writes the live
    context._external_evidence (the credit source, UNCHANGED) AND also writes the
    durable display receipt — best-effort, never affecting the credit path."""
    from systemu.runtime import shadow_runtime as sr
    from systemu.runtime import receipts_store
    from systemu.runtime.chat_submission_ctx import set_execution_id
    from systemu.core.models import ExternalEvidence

    calls = []
    monkeypatch.setattr(receipts_store, "write_receipt",
                        lambda eid, oid, r, **k: calls.append((eid, oid, r)))
    set_execution_id("exec-receipt")

    class _Ctx:
        pass

    ctx = _Ctx()
    ev = ExternalEvidence(objective_id=5, confirmed=True, method="api_readback", detail="ok")
    try:
        sr._persist_external_evidence(ctx, ev)
        # (1) credit path UNCHANGED — the live evidence rides the context.
        assert ctx._external_evidence["5"]["confirmed"] is True
        assert ctx._external_evidence["5"]["method"] == "api_readback"
        # (2) durable DISPLAY copy written with the projected receipt.
        assert calls and calls[0][0] == "exec-receipt" and calls[0][1] == 5
        assert calls[0][2]["confirmed"] is True and calls[0][2]["method"] == "api_readback"
    finally:
        set_execution_id(None)


def test_write_hook_no_op_without_execution_id(monkeypatch):
    """A receipt persisted outside any run (no ambient execution_id) writes NO
    durable receipt (no orphan file) — but the credit path still records it."""
    from systemu.runtime import shadow_runtime as sr
    from systemu.runtime import receipts_store
    from systemu.runtime.chat_submission_ctx import set_execution_id
    from systemu.core.models import ExternalEvidence

    calls = []
    monkeypatch.setattr(receipts_store, "write_receipt",
                        lambda *a, **k: calls.append(a))
    set_execution_id(None)
    ctx = type("C", (), {})()
    sr._persist_external_evidence(ctx, ExternalEvidence(objective_id=1, confirmed=False))
    assert ctx._external_evidence["1"]["confirmed"] is False   # credit path still records
    assert calls == []                                          # no durable write without an eid


def test_receipt_badges_for_render_data(tmp_path):
    rs.write_receipt("e", 2, {"objective_id": 2, "confirmed": False, "method": "api_readback"}, data_dir=tmp_path)
    rs.write_receipt("e", 1, {"objective_id": 1, "confirmed": True, "method": "api_readback",
                              "detail": "host-pinned https, fresh"}, data_dir=tmp_path)
    badges = rs.receipt_badges_for("e", data_dir=tmp_path)
    # sorted by objective_id; verified→Verified, unconfirmed→Claimed.
    assert [b["objective_id"] for b in badges] == ["1", "2"]
    assert badges[0]["verified"] is True and badges[0]["label"] == "Verified"
    assert "receipts, not self-report" in badges[0]["tooltip"]
    assert badges[1]["verified"] is False and badges[1]["label"] == "Claimed"


def test_receipt_badges_empty_when_no_receipts(tmp_path):
    # a run with no external effect → no badges (→ no panel; never a fabricated one).
    assert rs.receipt_badges_for("none", data_dir=tmp_path) == []


def test_malicious_eid_cannot_traverse_out_of_the_audit_dir(tmp_path):
    """The eid lands in a filesystem path; a traversal attempt (../) must be
    sanitized so the receipt stays under the audit dir (defense-in-depth)."""
    rs.write_receipt("../../evil", 1, {"objective_id": 1, "confirmed": True}, data_dir=tmp_path)
    # nothing was written outside tmp_path (no escape)
    escaped = (tmp_path / ".." / ".." / "evil").resolve()
    assert not (escaped / "receipts.json").exists()
    # and it reads back through the SAME sanitization
    assert rs.read_receipts("../../evil", data_dir=tmp_path).get("1", {}).get("confirmed") is True
    # the on-disk dir name is sanitized (no '..', no separators)
    audit = tmp_path / "audit"
    dirs = [p.name for p in audit.iterdir()] if audit.exists() else []
    assert dirs and all(".." not in d and "/" not in d and "\\" not in d for d in dirs)
