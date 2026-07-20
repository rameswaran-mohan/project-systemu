"""R-P3b — the compliance export, wired from Settings (MASTER-SPEC Part II §6).

``ledger.export_csv``/``export_jsonl`` were finished, frozen and tested with ZERO
callers. These pins cover the wiring, and they drive the REAL path: a real
``Vault``, the real ``FileVault`` adapter the dashboard actually holds, the real
single audit writer, and a real file on disk whose BYTES are asserted. A fixture
that stubs the vault would have hidden the defect these pins exist to catch —
``FileVault`` forwards ``.root`` but NOT ``append_action_audit``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import audit_log, ledger, ledger_export, receipts_store
from systemu.storage.file_vault import FileVault
from systemu.vault.vault import Vault

_TOKEN = "ghp_deadbeefdeadbeefdeadbeefdeadbeef1234"
_EMAIL = "payroll.person@example.com"


def _vault(tmp_path):
    """The REAL adapter stack the dashboard uses: FileVault wrapping Vault."""
    return FileVault(Vault(str(tmp_path / "vault")))


def _act(vault, eid="quick_1", oid=0, action="send_email", params=None, success=True):
    audit_log.append_action(getattr(vault, "_v", vault), execution_id=eid,
                            objective_id=oid, action=action,
                            params=params if params is not None else {"to": _EMAIL},
                            success=success)


def _maximal(tmp_path, vault):
    """The richest row this projection can build: success + host + confirmed receipt.
    Anything still blank here is blank STRUCTURALLY, not for want of data."""
    _act(vault, params={"url": "https://mail.example.com/send", "to": _EMAIL})
    receipts_store.write_receipt("quick_1", 0,
                                 {"confirmed": True, "method": "api_readback",
                                  "stamped_at": "2026-07-19T00:00:00Z"},
                                 data_dir=tmp_path)


# ── the wiring itself ────────────────────────────────────────────────────────

@pytest.mark.source_sensitive
def test_settings_page_actually_calls_the_export_card():
    """Structural, not textual: the card must be CALLED inside build_settings_page.
    A docstring or comment naming it must not be able to satisfy this.

    Marked source_sensitive BY HAND: conftest's auto-tagger keys on the literal
    substring ``getsource(`` (conftest.py:140), so reading a module's source any
    other way — as here, straight off ``__file__`` — is invisible to it. Untagged,
    this would run in the edit-safe tier and break under exactly the concurrent
    edits that tier exists to tolerate. The path comes from ``__file__`` too, so the
    result does not depend on pytest's working directory.
    """
    from systemu.interface.pages import settings as settings_mod
    src = Path(settings_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "build_settings_page")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "compliance_export_card" in called


def test_export_card_reaches_the_real_exporters():
    """The frozen exporters are the ones actually dispatched to — not a re-implementation."""
    assert ledger_export._EXPORTERS["csv"] is ledger.export_csv
    assert ledger_export._EXPORTERS["jsonl"] is ledger.export_jsonl


# ── real path: a real file, real bytes ───────────────────────────────────────

def test_real_export_writes_the_exporter_bytes_to_disk(tmp_path):
    v = _vault(tmp_path)
    _maximal(tmp_path, v)
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)

    out = Path(res["destination_path"])
    assert out.exists() and out.parent.name == ledger_export.EXPORT_DIRNAME
    written = out.read_bytes()
    assert len(written) == res["byte_count"] > 0
    # byte-for-byte the frozen exporter's output over the same range
    expected = ledger.export_csv(ledger.project(
        v, data_dir=tmp_path, since_ts=res["since_ts"], until_ts=res["until_ts"]))
    assert written == expected

    text = written.decode("utf-8")
    header, first = text.splitlines()[0], text.splitlines()[1]
    assert header.split(",") == list(ledger._COLUMNS)
    assert "send_email" in first and "mail.example.com" in first
    assert "verified" in first
    assert text.endswith("\n") and "\r\n" not in text     # UTF-8, \n endings


def test_real_jsonl_export_writes_one_canonical_line_per_row(tmp_path):
    v = _vault(tmp_path)
    _act(v, eid="quick_a")
    _act(v, eid="quick_b")
    res = ledger_export.write_export(v, fmt="jsonl", data_dir=tmp_path)
    lines = Path(res["destination_path"]).read_bytes().decode("utf-8").splitlines()
    assert len(lines) == res["row_count"] == 2
    assert all(json.loads(ln)["source_kind"] == "action_audit" for ln in lines)
    assert "raw_beside" not in lines[0]


def test_filevault_adapter_supports_both_the_read_and_the_write(tmp_path):
    """The anchor that a stub fixture would have hidden: FileVault exposes .root but
    has NO append_action_audit and no __getattr__, so the export event write must
    unwrap ._v. If it does not, event_recorded goes False and the ledger silently
    stops recording exports."""
    v = _vault(tmp_path)
    assert not hasattr(v, "append_action_audit")           # the hazard, pinned
    _act(v)
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    assert res["event_recorded"] is True


# ── §6: "the export event is itself a ledger row" ────────────────────────────

def test_export_event_becomes_a_ledger_row_with_an_honest_origin(tmp_path):
    v = _vault(tmp_path)
    _act(v)
    ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    rows = ledger.project(v, data_dir=tmp_path)
    ev = [r for r in rows if r.action["tool"] == ledger_export.EXPORT_ACTION]
    assert len(ev) == 1
    assert ev[0].actor["origin"] == "manual"                # a person, not the scheduler
    assert ev[0].outcome["status"] == "success"
    # the artifact is identified by digest, and no absolute path travels with it
    assert "\\" not in ev[0].source_id and "/" not in ev[0].source_id


def test_export_event_params_carry_no_absolute_path(tmp_path):
    """The event row is persisted in the vault and rendered in the authed UI. It
    identifies the artifact by filename + digest; the absolute path (which carries the
    OS account name) stays out of the durable record."""
    v = _vault(tmp_path)
    _act(v)
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    raw = (Path(v.root) / "audit" / "actions.jsonl").read_text(encoding="utf-8")
    entry = next(json.loads(ln) for ln in raw.splitlines()
                 if json.loads(ln)["action"] == ledger_export.EXPORT_ACTION)
    assert entry["params"]["artifact"] == res["filename"]
    assert str(tmp_path) not in json.dumps(entry)
    assert entry["params"]["sha256"] == res["sha256"]   # still fully identifiable


def test_export_event_can_be_suppressed_but_defaults_on(tmp_path):
    v = _vault(tmp_path)
    _act(v)
    ledger_export.write_export(v, fmt="csv", data_dir=tmp_path, record_event=False)
    tools = [r.action["tool"] for r in ledger.project(v, data_dir=tmp_path)]
    assert ledger_export.EXPORT_ACTION not in tools


# ── AC3 — byte-stable re-export of an unchanged range ────────────────────────

def _seed_past(v, entries):
    audit = Path(v.root) / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "actions.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def test_ac3_a_settled_range_re_exports_byte_identically(tmp_path):
    """AC3 proper: a range that has stopped changing re-exports byte-for-byte, even
    though each export appends its own event row to the ledger afterwards."""
    v = _vault(tmp_path)
    _seed_past(v, [
        {"ts": "2026-06-02T10:00:00Z", "execution_id": "quick_x", "objective_id": 0,
         "action": "send_email", "params": {"to": _EMAIL}, "success": True},
        {"ts": "2026-06-03T11:30:00Z", "execution_id": "wf_y", "objective_id": 1,
         "action": "http_post", "params": {"url": "https://api.example.org/v1"},
         "success": True},
    ])
    a = ledger_export.write_export(v, fmt="csv", since="2026-06-01", until="2026-06-30",
                                   data_dir=tmp_path)
    b = ledger_export.write_export(v, fmt="csv", since="2026-06-01", until="2026-06-30",
                                   data_dir=tmp_path)
    assert a["row_count"] == b["row_count"] == 2
    assert a["sha256"] == b["sha256"]
    assert Path(a["destination_path"]).read_bytes() == Path(b["destination_path"]).read_bytes()
    # both exports DID record themselves — they simply fall outside the settled range
    assert a["event_recorded"] and b["event_recorded"]
    assert sum(1 for r in ledger.project(v, data_dir=tmp_path)
               if r.action["tool"] == ledger_export.EXPORT_ACTION) == 2


def test_a_range_containing_now_absorbs_each_export_event(tmp_path):
    """The honest caveat to AC3, pinned rather than papered over: if the operator
    picks a window that extends to today, each export is itself an event inside it,
    so the next export of that window legitimately has one more row."""
    v = _vault(tmp_path)
    _act(v)
    a = ledger_export.write_export(v, fmt="csv", since="2026-01-01", until="2026-12-31",
                                   data_dir=tmp_path)
    b = ledger_export.write_export(v, fmt="csv", since="2026-01-01", until="2026-12-31",
                                   data_dir=tmp_path)
    assert b["row_count"] == a["row_count"] + 1
    assert b["sha256"] != a["sha256"]


def test_default_window_closes_at_now_so_the_event_lands_outside_it(tmp_path):
    """The export's own row must never fall inside the range it reports on — that is
    what makes the default (open-ended) export re-exportable at all."""
    v = _vault(tmp_path)
    _act(v)
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    again = ledger.project(v, data_dir=tmp_path,
                           since_ts=res["since_ts"], until_ts=res["until_ts"])
    assert ledger.export_csv(again) == Path(res["destination_path"]).read_bytes()


# ── AC4 — nothing sensitive reaches the artifact OR the manifest ─────────────

def test_ac4_secret_and_pii_never_reach_the_export_or_the_manifest(tmp_path):
    v = _vault(tmp_path)
    _act(v, params={"api_key": _TOKEN, "to": _EMAIL,
                    "body": "wire the payroll to " + _EMAIL})
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    blob = Path(res["destination_path"]).read_bytes()
    manifest = Path(res["manifest_path"]).read_bytes()
    for secret in (_TOKEN.encode(), _EMAIL.encode(), b"wire the payroll"):
        assert secret not in blob
        assert secret not in manifest
    assert len(res["sha256"]) == 64


def test_reported_row_count_is_derived_from_the_written_file(tmp_path):
    """A manifest row_count that disagrees with the artifact is a FALSE ATTESTATION,
    not a cosmetic bug. Pin the count against the file's own data lines — not against
    the preview that produced it — so the two paths cannot drift apart unnoticed."""
    v = _vault(tmp_path)
    for i in range(4):
        _act(v, eid=f"quick_{i}")
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    data_lines = Path(res["destination_path"]).read_bytes().decode().splitlines()[1:]
    assert res["row_count"] == len(data_lines) == 4
    assert res["manifest"]["row_count"] == len(data_lines)

    jres = ledger_export.write_export(v, fmt="jsonl", data_dir=tmp_path)
    jlines = Path(jres["destination_path"]).read_bytes().decode().splitlines()
    assert jres["row_count"] == jres["manifest"]["row_count"] == len(jlines)


def test_write_export_projects_exactly_once(tmp_path, monkeypatch):
    """The structural guarantee behind the count: ONE projection per export.

    A second projection is the drift vector — the reported count would describe rows
    the file does not contain. Asserting agreement cannot catch it (two projections of
    an unchanged ledger agree), so pin the call count instead.
    """
    v = _vault(tmp_path)
    _act(v)
    calls = []
    real = ledger.project
    monkeypatch.setattr(ledger, "project",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    ledger_export.write_export(v, fmt="csv", data_dir=tmp_path, record_event=False)
    assert len(calls) == 1


def test_reported_count_describes_the_file_even_if_the_ledger_shifts(tmp_path, monkeypatch):
    """A projection that returned something different on a second call must not be
    able to produce a manifest that misdescribes the artifact. With one projection
    this holds by construction; with two it would fail."""
    v = _vault(tmp_path)
    _act(v, eid="quick_1")
    _act(v, eid="quick_2")
    real = ledger.project
    state = {"n": 0}

    def _shifting(*a, **k):
        state["n"] += 1
        rows = real(*a, **k)
        return rows if state["n"] == 1 else rows[:1]     # a 2nd call sees FEWER rows

    monkeypatch.setattr(ledger, "project", _shifting)
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path, record_event=False)
    data_lines = Path(res["destination_path"]).read_bytes().decode().splitlines()[1:]
    assert res["row_count"] == len(data_lines)
    assert res["manifest"]["row_count"] == len(data_lines)


def test_reported_blank_columns_match_the_written_file(tmp_path):
    """Same drift guard for the column annotation: every column the manifest calls
    blank must actually be empty in every data row of the artifact, and no other.

    The rows here deliberately carry NO host and NO receipt, so this export's blank
    set is strictly LARGER than the curated UNPOPULATED_COLUMNS. That gap is what
    makes the assertion discriminating — with a maximally-populated row the two sets
    coincide and reporting the curated list instead of the real one passes unnoticed
    (a mutation proved exactly that).
    """
    v = _vault(tmp_path)
    _act(v, eid="quick_plain1", params={"to": _EMAIL})
    _act(v, eid="quick_plain2", params={"to": _EMAIL})
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    assert set(res["blank_columns"]) > set(ledger_export.UNPOPULATED_COLUMNS)

    import csv as _csv
    import io as _io
    text = Path(res["destination_path"]).read_bytes().decode()
    reader = list(_csv.reader(_io.StringIO(text)))
    header, data = reader[0], reader[1:]
    assert len(data) == 2
    from_file = {name for i, name in enumerate(header)
                 if all(row[i] == "" for row in data)}
    assert from_file == set(res["manifest"]["columns_blank_in_this_export"])
    assert from_file == set(res["blank_columns"])


def test_export_safety_does_not_depend_on_the_secret_mask_catching_it(tmp_path):
    """The load-bearing guarantee is STRUCTURAL, not fence-based.

    ``_mask_evidence`` redacts by secret-ish KEY NAME and by known value SHAPE, and
    ``credentials/resolver.py`` pulls values straight out of ``os.environ`` without
    registering them anywhere — so a secret under a neutral key with an unrecognised
    shape is masked by NOTHING. It still cannot reach the export, because no raw
    parameter value is ever emitted to a cell: the only param-derived column is a
    one-way sha256 digest. This pin asserts that property directly rather than
    trusting the mask.
    """
    sneaky = "Zq7Z-payroll-db-root-2026"          # neutral key, no recognised shape
    from systemu.runtime.external_verifier import _mask_evidence
    assert _mask_evidence({"code": sneaky})["code"] == sneaky   # the mask does NOT help

    v = _vault(tmp_path)
    _act(v, action="call", params={"code": sneaky, "note": sneaky})
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    jres = ledger_export.write_export(v, fmt="jsonl", data_dir=tmp_path)
    for path in (res["destination_path"], res["manifest_path"],
                 jres["destination_path"], jres["manifest_path"]):
        assert sneaky.encode() not in Path(path).read_bytes()

    # it survives ONLY in raw_beside, which neither exporter emits
    assert sneaky in json.dumps(ledger.project(v, data_dir=tmp_path)[0].raw_beside)


def test_no_raw_param_value_reaches_any_exported_cell(tmp_path):
    """The structural rule behind the pin above, stated once: of the 25 columns, the
    only param-derived ones are a sha256 digest and a regex-gated bare hostname."""
    v = _vault(tmp_path)
    _act(v, action="call",
         params={"url": "https://user:tok@h.example/secret/path?q=a@b.com",
                 "freeform": "MEMO: acquire NewCo for 4.2 crore"})
    rows = ledger.project(v, data_dir=tmp_path)
    cells = dict(zip(ledger._COLUMNS, ledger._csv_cells(rows[0])))
    assert cells["action_params_digest"] and len(cells["action_params_digest"]) == 64
    assert cells["action_host"] == "h.example"          # userinfo/path/query stripped
    for value in ("tok", "secret/path", "a@b.com", "NewCo", "4.2 crore"):
        assert value not in ",".join(cells.values())


def test_manifest_carries_no_absolute_path(tmp_path):
    """A manifest travels with the export to an auditor; the vault path carries the
    OS account name and has no business going with it."""
    v = _vault(tmp_path)
    _act(v)
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    manifest = json.loads(Path(res["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["artifact"] == res["filename"]
    assert str(tmp_path) not in json.dumps(manifest)
    assert manifest["sha256"] == res["sha256"]
    assert manifest["byte_count"] == len(Path(res["destination_path"]).read_bytes())


# ── S-1: wiring the UI must NOT switch the hash chain on ─────────────────────

def test_export_does_not_activate_the_reserved_hash_chain(tmp_path):
    v = _vault(tmp_path)
    _maximal(tmp_path, v)
    res = ledger_export.write_export(v, fmt="jsonl", data_dir=tmp_path)
    for line in Path(res["destination_path"]).read_bytes().decode().splitlines():
        row = json.loads(line)
        assert row["seq"] is None and row["chain_day"] is None
        assert row["prev_hash"] is None and row["row_hash"] is None
    assert res["manifest"]["hash_chain"].startswith("not activated")


# ── the always-blank columns (the honesty crux) ──────────────────────────────

def test_unpopulated_columns_two_way_pin_against_a_maximal_projection(tmp_path):
    """The curated list must equal the columns that are blank even on the RICHEST row
    the projection can produce. Fails if a producer lands (list is stale) AND if a new
    column goes structurally blank (list under-claims) — so the manifest cannot drift
    into lying either way."""
    v = _vault(tmp_path)
    _maximal(tmp_path, v)
    rows = ledger.project(v, data_dir=tmp_path)
    assert len(rows) == 1
    assert set(ledger_export.blank_columns(rows)) == set(ledger_export.UNPOPULATED_COLUMNS)


def test_blank_columns_is_derived_from_real_cells_not_a_constant(tmp_path):
    v = _vault(tmp_path)
    _act(v, params={"to": _EMAIL})            # no receipt → verification stays blank
    rows = ledger.project(v, data_dir=tmp_path)
    blank = set(ledger_export.blank_columns(rows))
    assert "outcome_verification" in blank                 # blank in THIS export
    assert "outcome_verification" not in ledger_export.UNPOPULATED_COLUMNS
    assert "action_host" in blank                          # no url/host in params
    assert "action_tool" not in blank


def test_manifest_names_every_unpopulated_column_with_a_reason(tmp_path):
    v = _vault(tmp_path)
    _maximal(tmp_path, v)
    res = ledger_export.write_export(v, fmt="csv", data_dir=tmp_path)
    m = json.loads(Path(res["manifest_path"]).read_text(encoding="utf-8"))
    never = m["columns_never_populated_by_this_build"]
    assert set(never) == set(ledger_export.UNPOPULATED_COLUMNS)
    assert all(isinstance(why, str) and why for why in never.values())
    # the specific misreading this guards against
    assert "does NOT mean no approval was required" in never["gate_verdict"]
    assert "undo_kind" in m["columns_constant_in_this_build"]
    assert "NOT that the event lacked that property" in m["reading_note"]


# ── fail loudly, never silently widen or empty ───────────────────────────────

@pytest.mark.parametrize("bad", ["last week", "2026-13-45", "notatimeZ", "yesterday"])
def test_unparseable_range_raises_and_writes_nothing(tmp_path, bad):
    v = _vault(tmp_path)
    _act(v)
    with pytest.raises(ValueError):
        ledger_export.preview(v, since=bad, data_dir=tmp_path)
    with pytest.raises(ValueError):
        ledger_export.write_export(v, since=bad, data_dir=tmp_path)
    assert not (tmp_path / "vault" / ledger_export.EXPORT_DIRNAME).exists()


def test_inverted_range_raises(tmp_path):
    v = _vault(tmp_path)
    with pytest.raises(ValueError, match="empty range"):
        ledger_export.preview(v, since="2026-06-01", until="2026-01-01", data_dir=tmp_path)


def test_unknown_format_raises_and_writes_nothing(tmp_path):
    v = _vault(tmp_path)
    _act(v)
    with pytest.raises(ValueError, match="unknown export format"):
        ledger_export.write_export(v, fmt="xlsx", data_dir=tmp_path)
    assert not (tmp_path / "vault" / ledger_export.EXPORT_DIRNAME).exists()


def test_non_file_backend_export_fails_loudly_not_emptily(tmp_path):
    """A silently-empty compliance export is the worst outcome; the projection's
    loud NotImplementedError must reach the export surface unswallowed."""
    v = SimpleNamespace(_storage_backend="sqlite", root=tmp_path / "v")
    with pytest.raises(NotImplementedError):
        ledger_export.preview(v, data_dir=tmp_path)


def test_destination_is_never_guessed_for_a_rootless_vault():
    """Tested on _dest_dir directly: via preview() the projection's own .root guard
    fires first, so this defence is only reachable at the helper. It stays because a
    compliance file must never land somewhere nobody named."""
    with pytest.raises(NotImplementedError, match="never guess"):
        ledger_export._dest_dir(SimpleNamespace(), None)
    # an explicit destination is honoured and needs no .root at all
    assert ledger_export._dest_dir(SimpleNamespace(), "D:/audits") == Path("D:/audits")


def test_rootless_vault_still_fails_loudly_through_preview(tmp_path):
    v = SimpleNamespace(_storage_backend="file")
    with pytest.raises(NotImplementedError):
        ledger_export.preview(v, data_dir=tmp_path)


# ── range filtering ──────────────────────────────────────────────────────────

def test_range_filter_excludes_rows_outside_the_window(tmp_path):
    v = _vault(tmp_path)
    audit = Path(v.root) / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "actions.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"ts": "2026-05-01T10:00:00Z", "execution_id": "quick_old", "objective_id": 0,
         "action": "a", "params": {}, "success": True},
        {"ts": "2026-06-15T10:00:00Z", "execution_id": "quick_mid", "objective_id": 0,
         "action": "b", "params": {}, "success": True},
        {"ts": "2026-07-30T10:00:00Z", "execution_id": "quick_new", "objective_id": 0,
         "action": "c", "params": {}, "success": True},
    ]), encoding="utf-8")
    res = ledger_export.write_export(v, fmt="csv", since="2026-06-01", until="2026-06-30",
                                     data_dir=tmp_path, record_event=False)
    body = Path(res["destination_path"]).read_bytes().decode()
    assert res["row_count"] == 1 and ",b," in body
    assert "quick_old" not in body and "quick_new" not in body


def test_range_bounds_are_inclusive_on_both_ends(tmp_path):
    v = _vault(tmp_path)
    audit = Path(v.root) / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "actions.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"ts": "2026-06-01T00:00:00Z", "execution_id": "quick_s", "objective_id": 0,
         "action": "s", "params": {}, "success": True},
        {"ts": "2026-06-30T23:59:59.999999Z", "execution_id": "quick_e", "objective_id": 0,
         "action": "e", "params": {}, "success": True},
    ]), encoding="utf-8")
    res = ledger_export.preview(v, since="2026-06-01", until="2026-06-30", data_dir=tmp_path)
    assert res["row_count"] == 2


def test_a_day_bound_covers_the_whole_utc_day(tmp_path):
    assert ledger_export.parse_bound("2026-06-01", end_of_day=False) == \
        "2026-06-01T00:00:00.000000Z"
    assert ledger_export.parse_bound("2026-06-01", end_of_day=True) == \
        "2026-06-01T23:59:59.999999Z"
    assert ledger_export.parse_bound("", end_of_day=False) is None
    assert ledger_export.parse_bound(None, end_of_day=True) is None


# ── the preview the operator confirms ────────────────────────────────────────

def test_preview_names_destination_row_count_and_the_blank_columns(tmp_path):
    v = _vault(tmp_path)
    _maximal(tmp_path, v)
    pv = ledger_export.preview(v, fmt="csv", data_dir=tmp_path)
    from systemu.interface.pages.settings import export_preview_lines
    lines = export_preview_lines(pv)
    joined = "\n".join(lines)
    assert pv["destination_path"] in joined                # destination is explicit
    assert "1 row(s), 25 columns, CSV format." in joined
    assert "ALWAYS blank in this build" in joined
    assert "gate_verdict" in joined                        # named, not just counted
    assert "credential" in joined.lower()                  # what is excluded


def test_preview_lines_say_so_when_the_range_is_empty(tmp_path):
    v = _vault(tmp_path)
    pv = ledger_export.preview(v, fmt="csv", data_dir=tmp_path)
    from systemu.interface.pages.settings import export_preview_lines
    joined = "\n".join(export_preview_lines(pv))
    assert pv["row_count"] == 0
    assert "no recorded actions" in joined                 # not passed off as a clean bill


# ── the rendered card, driven through its own click handlers ─────────────────
#
# Everything above tests the runtime. These drive the actual closures the operator's
# clicks reach, so a typo inside _preview/_write cannot pass by being untouched.


def _render_card(monkeypatch, vault, notes):
    from nicegui import Client, ui
    from systemu.interface.pages import settings as S

    monkeypatch.setattr(S, "export_vault", lambda: vault)
    monkeypatch.setattr(ui, "notify",
                        lambda msg, **kw: notes.append((str(msg), kw.get("type"))))
    client = Client(lambda: None)
    with client:
        with ui.column() as col:
            S.compliance_export_card()
    found = {}
    def _walk(e):
        for k in e.default_slot.children:
            t = type(k).__name__
            label = str(k.text if hasattr(k, "text") else "") or str(
                k._props.get("label", ""))
            found.setdefault(t, []).append((label, k))
            _walk(k)
    _walk(col)
    return found


def _click(el):
    for listener in el._event_listeners.values():
        if listener.type == "click":
            listener.handler(None)


def _fire_change(el):
    for listener in el._event_listeners.values():
        if listener.type == "change":
            listener.handler(None)


def test_card_click_flow_preview_then_write_lands_a_real_file(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    _act(v)
    notes: list = []
    found = _render_card(monkeypatch, v, notes)
    buttons = {label: el for label, el in found["Button"]}
    write_btn = buttons["Write export file"]

    assert not write_btn.enabled                       # nothing writable unconfirmed
    _click(write_btn)                                  # a click anyway must do NOTHING
    assert not (tmp_path / "vault" / ledger_export.EXPORT_DIRNAME).exists()
    assert notes and notes[-1][1] == "warning"

    _click(buttons["Preview export"])
    assert write_btn.enabled
    _click(write_btn)

    msg, kind = notes[-1]
    assert kind == "positive" and "Wrote 1 row(s)" in msg
    exports = list((tmp_path / "vault" / ledger_export.EXPORT_DIRNAME).glob("*.csv"))
    assert len(exports) == 1
    body = exports[0].read_bytes().decode()
    assert body.splitlines()[0].split(",") == list(ledger._COLUMNS)
    assert "send_email" in body
    assert exports[0].name in msg                      # destination named to the operator
    assert not write_btn.enabled                       # re-armed for the next confirmation


def test_card_changing_an_input_invalidates_the_confirmation(tmp_path, monkeypatch):
    """The operator must not approve one artifact and write another."""
    v = _vault(tmp_path)
    _act(v)
    notes: list = []
    found = _render_card(monkeypatch, v, notes)
    buttons = {label: el for label, el in found["Button"]}
    write_btn = buttons["Write export file"]

    _click(buttons["Preview export"])
    assert write_btn.enabled
    _fire_change(found["Input"][0][1])                 # operator edits the 'From' box
    assert not write_btn.enabled
    _click(write_btn)
    assert notes[-1][1] == "warning"
    assert not (tmp_path / "vault" / ledger_export.EXPORT_DIRNAME).exists()


def test_card_writes_exactly_the_previewed_range_not_a_re_resolved_one(tmp_path, monkeypatch):
    """The operator approves an artifact of N rows; N is what must land. 'To' defaults
    to now, so re-resolving the range at write time would silently widen it to include
    whatever happened while the operator was reading the preview."""
    v = _vault(tmp_path)
    _act(v, eid="quick_before")
    notes: list = []
    found = _render_card(monkeypatch, v, notes)
    buttons = {label: el for label, el in found["Button"]}

    _click(buttons["Preview export"])
    _act(v, eid="quick_during")                        # a run finishes mid-confirmation
    _click(buttons["Write export file"])

    body = next((tmp_path / "vault" / ledger_export.EXPORT_DIRNAME)
                .glob("*.csv")).read_bytes().decode()
    assert "quick_before" in body
    assert "quick_during" not in body                  # NOT silently widened
    assert "Wrote 1 row(s)" in notes[-1][0]


def test_card_surfaces_a_bad_range_and_stays_unwritable(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    _act(v)
    notes: list = []
    found = _render_card(monkeypatch, v, notes)
    buttons = {label: el for label, el in found["Button"]}
    found["Input"][0][1].value = "last week"
    _click(buttons["Preview export"])

    msg, kind = notes[-1]
    assert kind == "negative" and "Cannot export" in msg and "last week" in msg
    assert not buttons["Write export file"].enabled
    assert not (tmp_path / "vault" / ledger_export.EXPORT_DIRNAME).exists()


def test_card_surfaces_an_unsupported_backend_loudly(tmp_path, monkeypatch):
    notes: list = []
    v = SimpleNamespace(_storage_backend="sqlite", root=tmp_path / "v")
    found = _render_card(monkeypatch, v, notes)
    buttons = {label: el for label, el in found["Button"]}
    _click(buttons["Preview export"])

    msg, kind = notes[-1]
    assert kind == "negative" and "storage backend" in msg
    assert not buttons["Write export file"].enabled     # never a silently-empty export


def test_preview_writes_nothing(tmp_path):
    v = _vault(tmp_path)
    _act(v)
    ledger_export.preview(v, fmt="csv", data_dir=tmp_path)
    assert not (tmp_path / "vault" / ledger_export.EXPORT_DIRNAME).exists()
    assert ledger_export.EXPORT_ACTION not in [
        r.action["tool"] for r in ledger.project(v, data_dir=tmp_path)]
