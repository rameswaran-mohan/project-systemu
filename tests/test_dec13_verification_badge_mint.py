"""DEC-13 / DEC-43 -- the ONE verification-badge mint (``runtime.verification_badge``).

The mint decides verified / claimed / measured / failed / handoff / not_recorded
for an outcome from its persisted evidence. These tests pin the decision table,
the witnessed read underneath it (absent and unreadable are different answers,
DEC-27) and the render fence (only a minted value renders a state, DEC-37).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from systemu.runtime import ledger, receipts_store
from systemu.runtime import verification_badge as vb


def _receipt(tmp_path, eid, oid, **fields):
    rec = {"objective_id": oid, "confirmed": True, "method": "api_readback",
           "detail": f"row {oid} created", "stamped_at": "2026-10-01T10:00:00Z",
           "shadow": False}
    rec.update(fields)
    receipts_store.write_receipt(eid, oid, rec, data_dir=tmp_path)


def _raw_file(tmp_path, eid, payload: bytes):
    target = tmp_path / "audit" / f"exec_{eid}" / "receipts.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


# ── the per-effect decision ──────────────────────────────────────────────────

@pytest.mark.parametrize("receipt,state", [
    ({"confirmed": True, "shadow": False}, vb.VERIFIED),
    ({"confirmed": True}, vb.VERIFIED),                  # legacy: written before the flag
    ({"confirmed": False, "shadow": False}, vb.CLAIMED),
    ({"confirmed": "yes", "shadow": False}, vb.CLAIMED),  # only the real bool True counts
    ({"confirmed": 1, "shadow": False}, vb.CLAIMED),
    ({}, vb.CLAIMED),
    ({"confirmed": True, "shadow": True}, vb.MEASURED),   # record-only: never verified
    ({"confirmed": False, "shadow": True}, vb.MEASURED),
    ("not a record", vb.NOT_RECORDED),
    (None, vb.NOT_RECORDED),
    (["confirmed", True], vb.NOT_RECORDED),
])
def test_mint_effect_decision_table(receipt, state):
    assert vb.mint_effect(1, receipt).state == state


def test_a_measurement_keeps_its_would_credit_bit_and_never_verifies():
    e = vb.mint_effect("2", {"confirmed": True, "shadow": True})
    assert e.state == vb.MEASURED and e.would_credit is True
    assert vb.view(e).icon != vb.VERIFIED_ICON


def test_live_marked_is_the_writers_explicit_statement_only():
    assert vb.mint_effect(1, {"confirmed": True, "shadow": False}).live_marked is True
    assert vb.mint_effect(1, {"confirmed": True}).live_marked is False


# ── the witnessed read ───────────────────────────────────────────────────────

def test_absent_is_only_a_missing_file(tmp_path):
    got = receipts_store.read_receipts_witnessed("exec_none", data_dir=tmp_path)
    assert got.status == receipts_store.READ_ABSENT


@pytest.mark.parametrize("payload,why", [
    (b"{not json", "not valid JSON"),
    (b"[1, 2]", "does not hold a record"),
    (b"\xff\xfe\x00garbage", "not UTF-8"),
])
def test_an_unreadable_record_is_its_own_answer(tmp_path, payload, why):
    _raw_file(tmp_path, "exec_bad", payload)
    got = receipts_store.read_receipts_witnessed("exec_bad", data_dir=tmp_path)
    assert got.status == receipts_store.READ_UNREADABLE and why in got.reason
    # the lenient reader's own contract is unchanged ({} for both)
    assert receipts_store.read_receipts("exec_bad", data_dir=tmp_path) == {}


def test_a_directory_where_the_file_should_be_is_unreadable_not_absent(tmp_path):
    target = tmp_path / "audit" / "exec_exec_dir" / "receipts.json"
    target.mkdir(parents=True)
    got = receipts_store.read_receipts_witnessed("exec_dir", data_dir=tmp_path)
    assert got.status == receipts_store.READ_UNREADABLE


def test_the_cache_follows_the_file(tmp_path):
    _receipt(tmp_path, "exec_c", 1, confirmed=False)
    assert vb.run_badge("success", "exec_c", data_dir=tmp_path).state == vb.CLAIMED
    _receipt(tmp_path, "exec_c", 1, confirmed=True)
    assert vb.run_badge("success", "exec_c", data_dir=tmp_path).state == vb.VERIFIED


# ── the run decision ─────────────────────────────────────────────────────────

def test_missing_evidence_is_claimed_never_verified(tmp_path):
    for status in ("success", "completed", "done", "executed"):
        b = vb.run_badge(status, "exec_nothing", data_dir=tmp_path)
        assert b.state == vb.CLAIMED and b.record == vb.RECORD_ABSENT, status
    assert vb.run_badge("success", None, data_dir=tmp_path).state == vb.CLAIMED
    assert vb.run_badge("success", "", data_dir=tmp_path).state == vb.CLAIMED


def test_every_effect_verified_reads_verified(tmp_path):
    _receipt(tmp_path, "exec_v", 1)
    _receipt(tmp_path, "exec_v", 2)
    b = vb.run_badge("success", "exec_v", data_dir=tmp_path)
    assert b.state == vb.VERIFIED and b.count(vb.VERIFIED) == 2
    assert vb.view(b).detail == "2 of 2 recorded effects verified"


def test_the_weakest_effect_decides(tmp_path):
    _receipt(tmp_path, "exec_mix", 1)
    _receipt(tmp_path, "exec_mix", 2, confirmed=False)
    assert vb.run_badge("success", "exec_mix", data_dir=tmp_path).state == vb.CLAIMED
    _receipt(tmp_path, "exec_meas", 1)
    _receipt(tmp_path, "exec_meas", 2, shadow=True)
    assert vb.run_badge("success", "exec_meas", data_dir=tmp_path).state == vb.MEASURED


def test_an_unreadable_record_renders_not_recorded_never_verified_or_blank(tmp_path):
    _raw_file(tmp_path, "exec_u", b"{not json")
    b = vb.run_badge("success", "exec_u", data_dir=tmp_path)
    assert b.state == vb.NOT_RECORDED and b.record == vb.RECORD_UNREADABLE
    v = vb.view(b)
    assert v.show and v.label == "Not recorded" and "could not be read" in v.tooltip
    assert v.icon != vb.VERIFIED_ICON


def test_one_malformed_entry_makes_the_run_not_recorded(tmp_path):
    _raw_file(tmp_path, "exec_m", json.dumps({"1": {"confirmed": True, "shadow": False},
                                              "2": "garbage"}).encode())
    b = vb.run_badge("success", "exec_m", data_dir=tmp_path)
    assert b.state == vb.NOT_RECORDED and "1 receipt entry" in b.reason


@pytest.mark.parametrize("status,state", [
    ("failed", vb.FAILED), ("failure", vb.FAILED), ("error", vb.FAILED),
    ("extraction_failed", vb.FAILED), ("skipped_no_shadow", vb.FAILED),
    ("partial", vb.HANDOFF), ("pending_decision", vb.HANDOFF),
    ("needs_input", vb.HANDOFF), ("waiting_on_tools", vb.HANDOFF),
    ("spend_cap_reached", vb.HANDOFF), ("cancelled", vb.HANDOFF),
    ("suspended_on_ask", vb.HANDOFF),
    ("running", vb.NO_OUTCOME), ("queued", vb.NO_OUTCOME),
    ("pending_approval", vb.NO_OUTCOME), ("unknown", vb.NO_OUTCOME),
    (None, vb.NOT_RECORDED), (42, vb.NOT_RECORDED),
])
def test_status_classes(tmp_path, status, state):
    assert vb.run_badge(status, "exec_s", data_dir=tmp_path).state == state


def test_a_handoff_keeps_every_recorded_effect_badged(tmp_path):
    _receipt(tmp_path, "exec_h", 1)
    _receipt(tmp_path, "exec_h", 2, confirmed=False)
    _receipt(tmp_path, "exec_h", 3, shadow=True)
    b = vb.run_badge("pending_decision", "exec_h", data_dir=tmp_path)
    assert b.state == vb.HANDOFF
    assert [e.state for e in b.effects] == [vb.VERIFIED, vb.CLAIMED, vb.MEASURED]
    lines = vb.effect_lines(b)
    assert lines[0].startswith("  [Verified] objective 1") and "[Claimed]" in lines[1]


def test_a_handoff_with_an_unreadable_record_says_so(tmp_path):
    _raw_file(tmp_path, "exec_hu", b"{nope")
    b = vb.run_badge("partial", "exec_hu", data_dir=tmp_path)
    assert b.state == vb.HANDOFF
    (line,) = vb.effect_lines(b)
    assert "committed effects: not recorded" in line and "not valid JSON" in line


def test_no_outcome_reads_nothing_and_renders_nothing(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("an in-flight row must not read receipts")
    monkeypatch.setattr(receipts_store, "read_receipts_witnessed", _boom)
    b = vb.run_badge("running", "exec_r", data_dir=tmp_path)
    assert b.state == vb.NO_OUTCOME and vb.view(b).show is False
    assert vb.badge_text(b) == ""


# ── the render fence (DEC-36 / DEC-37) ───────────────────────────────────────

class _SubBadge(vb.RunBadge):
    pass


@pytest.mark.parametrize("proxy", [
    None, "verified", {"state": "verified"}, SimpleNamespace(state="verified"),
    _SubBadge(state=vb.VERIFIED), vb.RunBadge(state="bogus"),
    vb.EffectBadge(state="bogus", objective_id="1"),
])
def test_only_a_minted_value_renders_a_state(proxy):
    v = vb.view(proxy)
    assert v.state == vb.NOT_RECORDED and v.icon != vb.VERIFIED_ICON


def test_the_filled_check_belongs_to_verified_alone():
    for s in vb.BADGE_STATES:
        v = vb.view(vb.RunBadge(state=s))
        assert (v.icon == vb.VERIFIED_ICON) is (s == vb.VERIFIED), s


def test_operator_copy_is_ascii():
    for s in vb.BADGE_STATES:
        b = vb.RunBadge(state=s, effects=(vb.mint_effect("1", {"confirmed": True}),))
        v = vb.view(b)
        for text in (v.label, v.tooltip, v.text, v.detail, vb.badge_text(b)):
            text.encode("ascii")


def test_persisted_text_is_masked_before_display():
    secret = "ghp_" + "A1b2C3d4" * 8
    e = vb.mint_effect("1", {"confirmed": True, "detail": f"token {secret} used",
                             "method": f"api_readback {secret}"})
    rendered = (vb.detail_text(e.detail) + vb.method_text(e.method)
                + vb.view(e).tooltip
                + "".join(vb.effect_lines(vb.RunBadge(state=vb.HANDOFF, effects=(e,)))))
    assert secret not in rendered and secret[:20] not in rendered


# ── the adapters that consume the mint ───────────────────────────────────────

def test_receipt_badges_for_names_an_unreadable_record(tmp_path):
    _raw_file(tmp_path, "exec_rb", b"{bad")
    (only,) = receipts_store.receipt_badges_for("exec_rb", data_dir=tmp_path)
    assert only["state"] == vb.NOT_RECORDED and only["verified"] is False


def _audit(eid, oid=1, ts="2026-10-01T10:00:00Z"):
    return {"ts": ts, "execution_id": eid, "objective_id": oid, "action": "create_row",
            "params": {"target": "x"}, "success": True, "error": None}


def _write_audit(root, entries):
    p = root / "audit" / "actions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def test_ledger_names_an_unreadable_record_not_recorded(tmp_path):
    root = tmp_path / "v"
    _write_audit(root, [_audit("exec_lu")])
    _raw_file(tmp_path, "exec_lu", b"{bad")
    (row,) = ledger.project(SimpleNamespace(root=root), data_dir=tmp_path)
    assert row.outcome["verification"] is None
    assert "could not be read" in row.outcome[ledger.NOT_RECORDED_KEY]["verification"]
    cells = dict(zip(ledger._COLUMNS, ledger._csv_cells(row)))
    assert cells["outcome_verification"] == ledger.NOT_RECORDED


def test_ledger_names_a_malformed_entry_not_recorded(tmp_path):
    root = tmp_path / "v"
    _write_audit(root, [_audit("exec_lm")])
    _raw_file(tmp_path, "exec_lm", json.dumps({"1": "garbage"}).encode())
    (row,) = ledger.project(SimpleNamespace(root=root), data_dir=tmp_path)
    cells = dict(zip(ledger._COLUMNS, ledger._csv_cells(row)))
    assert cells["outcome_verification"] == ledger.NOT_RECORDED


def test_ledger_fingerprint_is_unchanged_by_the_mint(tmp_path):
    """The evidence fingerprint is the same digest of the persisted fields it
    always was -- the export stays byte-stable across this refactor."""
    root = tmp_path / "v"
    _write_audit(root, [_audit("exec_fp")])
    _receipt(tmp_path, "exec_fp", 1)
    (eff, com) = [r for r in ledger.project(SimpleNamespace(root=root), data_dir=tmp_path)]
    want = ledger._digest({"oid": "1", "method": "api_readback",
                           "stamped_at": "2026-10-01T10:00:00Z"})
    assert eff.outcome["evidence_fingerprint"] == want == com.outcome["evidence_fingerprint"]
