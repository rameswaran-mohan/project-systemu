"""R-P3b action ledger — Part 1 (projection PRIMITIVES + export).

Pins the AC3/AC4/AC5 crux: a FROZEN canonical encoder, fixed-width ts (byte-stable
ordering + export), the PII-out-of-chain hash-chain (blanking raw_beside leaves the
hash unchanged — GDPR-erasure-compatible tamper-evidence), MASK (a secret never
reaches a digest or the export), and byte-stable SIEM export.
"""
from __future__ import annotations

from systemu.runtime import ledger
from systemu.runtime.ledger import LedgerRow


def _row(**over) -> LedgerRow:
    base = dict(
        source_kind="action_audit", source_id="exec_1|0|send_email|ts",
        ts="2026-07-16T10:00:00.000000Z", event_kind="effect",
        actor={"lane": "quick", "origin": "chat",
               "run_ref": {"execution_id": "exec_1", "activity_id": None,
                           "scroll_id": None, "shadow_id": None}},
        action={"tool": "send_email", "effect_tags": ["send_message"],
                "params_digest": "d" * 64, "host": "smtp.example.com"},
        gate={"verdict": "require_approval", "decision_id": "dec_1",
              "resolution_class": "remotely_resolvable", "resolved_via": None},
        outcome={"status": "success", "verification": "verified",
                 "evidence_ref": "exec_1#0", "evidence_fingerprint": "f" * 64,
                 "criteria_met": None, "criteria_total": None, "criteria": []},
        raw_beside={"params": {"to": "a@b.com"}, "detail": "sent to a@b.com",
                    "criteria_text": [], "evidence_body": {"msg_id": "X"}},
    )
    base.update(over)
    return LedgerRow(**base)


# ── 1. canonical_bytes ───────────────────────────────────────────────────────

def test_canonical_bytes_is_order_independent_and_compact():
    a = ledger.canonical_bytes({"b": 1, "a": {"y": 2, "x": [3, 1]}})
    b = ledger.canonical_bytes({"a": {"x": [3, 1], "y": 2}, "b": 1})
    assert a == b                                        # key order irrelevant
    assert b" " not in a and b"\n" not in a              # no insignificant whitespace
    assert a.startswith(b'{"a":')                        # sorted keys


def test_canonical_bytes_unicode_utf8():
    assert ledger.canonical_bytes({"k": "café"}) == '{"k":"café"}'.encode("utf-8")


# ── 2. norm_ts (fixed width) ─────────────────────────────────────────────────

def test_norm_ts_fixed_width_from_zero_and_full_micros():
    assert ledger.norm_ts("2026-07-16T10:00:05Z") == "2026-07-16T10:00:05.000000Z"
    assert ledger.norm_ts("2026-07-16T10:00:05.123456Z") == "2026-07-16T10:00:05.123456Z"
    assert ledger.norm_ts("2026-07-16T10:00:05+00:00") == "2026-07-16T10:00:05.000000Z"
    assert len(ledger.norm_ts("2026-07-16T10:00:05Z")) == 27


def test_norm_ts_makes_lexicographic_equal_chronological():
    raw = ["2026-07-16T10:00:05Z", "2026-07-16T10:00:05.000123Z", "2026-07-16T09:59:59Z"]
    normed = sorted(ledger.norm_ts(t) for t in raw)
    assert normed == ["2026-07-16T09:59:59.000000Z",
                      "2026-07-16T10:00:05.000000Z",
                      "2026-07-16T10:00:05.000123Z"]


def test_norm_ts_unparseable_is_returned_not_crashed():
    assert ledger.norm_ts("not-a-timestamp") == "not-a-timestamp"
    assert ledger.norm_ts("") == ""


# ── 3. compute_row_hash: determinism + linkage ──────────────────────────────

def test_row_hash_deterministic_and_excludes_hash_fields():
    r = _row()
    h1 = ledger.compute_row_hash("", r)
    r.prev_hash, r.row_hash = "abc", "def"              # hash fields must NOT affect the body
    h2 = ledger.compute_row_hash("", r)
    assert h1 == h2 and len(h1) == 64


def test_row_hash_links_on_prev_and_on_chained_fields():
    r = _row()
    genesis = ledger.compute_row_hash("", r)
    assert ledger.compute_row_hash(genesis, r) != genesis   # prev_hash changes it
    r2 = _row(seq=5)                                          # seq IS a chained field
    assert ledger.compute_row_hash("", r2) != genesis


# ── 4. PII-out-of-chain (the CMP-2 invariant) ────────────────────────────────

def test_blanking_raw_beside_leaves_the_hash_unchanged():
    r = _row()
    before = ledger.compute_row_hash("", r)
    r.raw_beside = {}                                    # a lawful erasure of PII
    after = ledger.compute_row_hash("", r)
    assert before == after                              # tamper-evidence survives erasure


# ── 5. AC4 — MASK before digest ──────────────────────────────────────────────

def test_mask_and_digest_never_encodes_a_secret():
    token = "ghp_deadbeefdeadbeefdeadbeefdeadbeef1234"
    masked, digest = ledger.mask_and_digest_params(
        {"api_key": token, "note": "call " + token})
    import json as _j
    blob = _j.dumps(masked)
    assert token not in blob                            # redacted in the masked form
    assert len(digest) == 64 and token not in digest    # digest is one-way, not the token


# ── 7. AC3 — byte-stable export; raw_beside NEVER emitted ────────────────────

def test_export_csv_is_byte_stable_and_has_25_columns():
    rows = [_row(), _row(source_id="exec_1|1|x|ts", ts="2026-07-16T10:00:01.000000Z")]
    a, b = ledger.export_csv(rows), ledger.export_csv(rows)
    assert a == b                                        # byte-stable
    header = a.decode("utf-8").splitlines()[0].split(",")
    assert len(header) == 25 and header[0] == "ts"


def test_export_jsonl_byte_stable_same_encoder_no_raw_beside():
    rows = [_row()]
    a, b = ledger.export_jsonl(rows), ledger.export_jsonl(rows)
    assert a == b
    assert b"raw_beside" not in a                        # PII store never exported
    assert b'"a@b.com"' not in a                         # a raw-beside value never leaks


def test_export_never_leaks_a_masked_secret_value():
    token = "sk-abcdefghijklmnopqrstuvwxyz012345"
    _masked, digest = ledger.mask_and_digest_params({"api_key": token})
    r = _row(action={"tool": "t", "effect_tags": [], "params_digest": digest, "host": None},
             raw_beside={})                              # nothing raw carried
    assert token.encode() not in ledger.export_csv([r])
    assert token.encode() not in ledger.export_jsonl([r])


# ── Part 2: the vault projection ─────────────────────────────────────────────

import json as _json
from pathlib import Path as _Path
from types import SimpleNamespace

from systemu.runtime import receipts_store


def _vault(tmp_path):
    root = tmp_path / "vault"
    (root / "audit").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(root=root)


def _write_audit(vault, entries):
    p = _Path(vault.root) / "audit" / "actions.jsonl"
    p.write_text("".join(_json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _audit(eid, oid, action, params, success=True, ts="2026-07-16T10:00:00Z"):
    return {"ts": ts, "execution_id": eid, "objective_id": oid,
            "action": action, "params": params, "success": success}


def test_ac1_one_effect_row_per_audit_success(tmp_path):
    v = _vault(tmp_path)
    _write_audit(v, [
        _audit("quick_1", 0, "send_email", {"to": "a@b.com"}, ts="2026-07-16T10:00:00Z"),
        _audit("quick_1", 0, "post_update", {"url": "https://x.com/p"}, ts="2026-07-16T10:00:01Z"),
        _audit("quick_1", 1, "send_email", {"to": "c@d.com"}, success=False,
               ts="2026-07-16T10:00:02Z"),   # FAILED → no row (2a: effects committed)
    ])
    rows = ledger.project(v, data_dir=tmp_path)
    effects = [r for r in rows if r.event_kind == "effect"]
    assert len(effects) == 2                                     # 2 successes; failure excluded
    assert all(r.source_kind == "action_audit" for r in effects)
    assert effects[0].actor["origin"] == "chat"                  # quick_ → chat token
    assert effects[0].actor["lane"] == "quick"
    assert effects[0].action["host"] is None                     # to= isn't a URL host
    assert effects[1].action["host"] == "x.com"                  # url= → hostname
    # chronological order (fixed-width ts)
    assert [r.ts for r in effects] == sorted(r.ts for r in effects)


def test_receipt_enrichment_verified_vs_claimed(tmp_path):
    v = _vault(tmp_path)
    _write_audit(v, [_audit("quick_2", 0, "send_email", {"to": "a@b.com"})])
    receipts_store.write_receipt("quick_2", 0, {"confirmed": True, "method": "api_readback"},
                                 data_dir=tmp_path)
    r = ledger.project(v, data_dir=tmp_path)[0]
    assert r.outcome["verification"] == "verified"
    assert r.outcome["evidence_ref"] == "quick_2#0"
    assert r.outcome["evidence_fingerprint"] and len(r.outcome["evidence_fingerprint"]) == 64

    # a receipt present but NOT confirmed → "claimed", never conflated with verified
    _write_audit(v, [_audit("quick_2b", 0, "send_email", {"to": "a@b.com"})])
    receipts_store.write_receipt("quick_2b", 0, {"confirmed": False, "method": "self_report"},
                                 data_dir=tmp_path)
    r2 = [x for x in ledger.project(v, data_dir=tmp_path)
          if x.actor["run_ref"]["execution_id"] == "quick_2b"][0]
    assert r2.outcome["verification"] == "claimed"


def test_ac4_mask_end_to_end_projection(tmp_path):
    token = "ghp_deadbeefdeadbeefdeadbeefdeadbeef1234"
    v = _vault(tmp_path)
    _write_audit(v, [_audit("quick_3", 0, "call_api", {"api_key": token, "note": "x"})])
    rows = ledger.project(v, data_dir=tmp_path)
    assert token.encode() not in ledger.export_csv(rows)         # not in the compliance export
    assert token.encode() not in ledger.export_jsonl(rows)
    assert token not in _json.dumps(rows[0].raw_beside)          # even the authed-UI store is masked
    assert len(rows[0].action["params_digest"]) == 64


def test_projection_export_is_byte_stable(tmp_path):
    v = _vault(tmp_path)
    _write_audit(v, [_audit("quick_5", 0, "a", {"k": 1}, ts="2026-07-16T10:00:00Z"),
                     _audit("quick_5", 1, "b", {"k": 2}, ts="2026-07-16T10:00:01Z")])
    rows = ledger.project(v, data_dir=tmp_path)
    assert ledger.export_csv(rows) == ledger.export_csv(ledger.project(v, data_dir=tmp_path))


def test_projection_robust_to_missing_and_corrupt(tmp_path):
    v = _vault(tmp_path)
    assert ledger.project(v, data_dir=tmp_path) == []            # no actions.jsonl → []
    (_Path(v.root) / "audit" / "actions.jsonl").write_text(
        '{"bad json\n' + _json.dumps(_audit("quick_4", 0, "x", {})) + "\n", encoding="utf-8")
    rows = ledger.project(v, data_dir=tmp_path)
    assert len(rows) == 1                                         # corrupt line skipped, good kept


# ── adversarial-review fixes ─────────────────────────────────────────────────

def test_f1_valid_json_non_object_line_does_not_crash(tmp_path):
    v = _vault(tmp_path)
    (_Path(v.root) / "audit" / "actions.jsonl").write_text(
        "[1,2,3]\n42\n\"str\"\n" + _json.dumps(_audit("quick_9", 0, "x", {})) + "\n",
        encoding="utf-8")
    rows = ledger.project(v, data_dir=tmp_path)                  # must NOT raise
    assert len(rows) == 1                                         # non-object lines skipped


def test_f2_non_file_backend_full_projection_fails_loudly(tmp_path):
    import pytest
    v = SimpleNamespace(_storage_backend="sqlite")               # no .root, DB backend
    with pytest.raises(NotImplementedError):
        ledger.project(v, data_dir=tmp_path)                     # NOT a silently-empty ledger
    # a specific execution_id still works via the backend query (stubbed empty here)
    v2 = SimpleNamespace(_storage_backend="sqlite",
                         query_action_audit=lambda **k: [])
    assert ledger.project(v2, data_dir=tmp_path, execution_id="exec_x") == []


def test_f3_action_host_never_leaks_raw_path_or_email(tmp_path):
    v = _vault(tmp_path)
    _write_audit(v, [
        _audit("quick_h1", 0, "call", {"endpoint": "/api/v2/users/john.doe@x.com/reset"},
               ts="2026-07-16T10:00:00Z"),
        _audit("quick_h2", 0, "call", {"host": "internal.corp"}, ts="2026-07-16T10:00:01Z"),
        _audit("quick_h3", 0, "call", {"url": "https://user:tok@h.example/p?e=a@b.com"},
               ts="2026-07-16T10:00:02Z"),
    ])
    by = {r.actor["run_ref"]["execution_id"]: r for r in ledger.project(v, data_dir=tmp_path)}
    assert by["quick_h1"].action["host"] is None                 # raw path/email NOT leaked
    assert by["quick_h2"].action["host"] == "internal.corp"      # a bare hostname is fine
    assert by["quick_h3"].action["host"] == "h.example"          # userinfo/path/query stripped
    # and no PII survived into the export
    csv_b = ledger.export_csv(ledger.project(v, data_dir=tmp_path))
    assert b"john.doe@x.com" not in csv_b and b"/api/v2/users" not in csv_b


def test_f4_receipt_attaches_once_per_objective(tmp_path):
    v = _vault(tmp_path)
    _write_audit(v, [
        _audit("quick_r", 0, "send_email", {"to": "a@b.com"}, ts="2026-07-16T10:00:00Z"),
        _audit("quick_r", 0, "send_email", {"to": "c@d.com"}, ts="2026-07-16T10:00:01Z"),
    ])
    receipts_store.write_receipt("quick_r", 0, {"confirmed": True, "method": "api_readback"},
                                 data_dir=tmp_path)
    rows = ledger.project(v, data_dir=tmp_path)
    verified = [r for r in rows if r.outcome["verification"] == "verified"]
    assert len(verified) == 1                                    # NOT fanned onto both effects
    assert sum(1 for r in rows if r.outcome["verification"] is None) == 1


def test_f5_one_bad_byte_does_not_empty_the_ledger(tmp_path):
    v = _vault(tmp_path)
    good = _json.dumps(_audit("quick_b", 0, "x", {})).encode("utf-8")
    (_Path(v.root) / "audit" / "actions.jsonl").write_bytes(b"\xff\xfe garbage\n" + good + b"\n")
    rows = ledger.project(v, data_dir=tmp_path)
    assert len(rows) == 1                                        # bad bytes localized, good row kept
