"""UX-10 Evidence Receipts (AC-U10) -- the receipt behind the DEC-13 badge.

AC-U10: clicking a verified badge opens a receipt with strategy / host /
timestamp / redacted token / artifact; a claimed outcome renders the
anti-receipt; zero network calls on open and no secret in any render (both
asserted). The durable receipt keeps no host, token or artifact body, so those
rows must SAY so (DEC-27) rather than invent a value or go blank.
"""
from __future__ import annotations

import builtins
import socket

import pytest

import _dec13_fixtures as fx
from systemu.interface.components import receipt_panel as rp
from systemu.runtime import verification_badge as vb


def _badge(tmp_path, monkeypatch, case, eid="exec_r"):
    status, receipts = fx.CASES[case]
    fx.point_audit_root(monkeypatch, tmp_path)
    fx.plant(tmp_path, eid, receipts)
    return vb.run_badge(status, eid)


def _rows(eff):
    return dict(eff["rows"])


# ── what each state's receipt shows ──────────────────────────────────────────

def test_a_verified_receipt_says_what_how_when_where(tmp_path, monkeypatch):
    m = rp.receipt_panel_model(_badge(tmp_path, monkeypatch, "verified"))
    assert m["state"] == vb.VERIFIED and m["title"].startswith("Receipt: independently")
    (eff,) = m["effects"]
    rows = _rows(eff)
    assert rows["How"].startswith("re-read from the service's API")
    assert "api_readback" in rows["How"]
    assert rows["When"].endswith("(local time)") and rows["When"][:4] == "2026"
    assert rows["Where (host)"] == "not recorded by this receipt"
    assert rows["Submission token"] == "not recorded by this receipt"
    assert "no artifact body" in rows["Artifact"]
    assert len(rows["Fingerprint"]) == 64
    assert eff["note_lines"] == ["row 1 created"]


def test_the_fingerprint_is_the_one_the_export_carries(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import json
    from systemu.runtime import ledger
    b = _badge(tmp_path, monkeypatch, "verified", eid="exec_fp")
    fp = _rows(rp.receipt_panel_model(b)["effects"][0])["Fingerprint"]
    root = tmp_path / "v"
    (root / "audit").mkdir(parents=True)
    (root / "audit" / "actions.jsonl").write_text(json.dumps({
        "ts": "2026-10-01T09:59:00Z", "execution_id": "exec_fp", "objective_id": 1,
        "action": "create_row", "params": {}, "success": True}) + "\n", encoding="utf-8")
    rows = ledger.project(SimpleNamespace(root=root))
    assert {r.outcome["evidence_fingerprint"] for r in rows} == {fp}


def test_a_claimed_outcome_renders_the_anti_receipt(tmp_path, monkeypatch):
    m = rp.receipt_panel_model(_badge(tmp_path, monkeypatch, "no_evidence"))
    assert m["state"] == vb.CLAIMED and m["title"].startswith("Anti-receipt")
    assert any("No independent artifact exists" in line for line in m["anti"])
    assert m["record_note"] == "No external effect is on record for this run."


def test_an_unconfirmed_effect_says_what_was_not_verified(tmp_path, monkeypatch):
    m = rp.receipt_panel_model(_badge(tmp_path, monkeypatch, "claimed"))
    (eff,) = m["effects"]
    assert eff["state"] == vb.CLAIMED and "Not independently verified" in eff["anti"]
    assert m["anti"] == ["Not independently confirmed: 1 of 1 recorded effect "
                         "(each is marked below)."]


def test_a_measurement_says_it_credited_nothing(tmp_path, monkeypatch):
    m = rp.receipt_panel_model(_badge(tmp_path, monkeypatch, "measured"))
    (eff,) = m["effects"]
    assert m["state"] == vb.MEASURED and "did not credit" in eff["anti"]
    assert "would have credited" in eff["anti"]


def test_a_handoff_lists_the_committed_effects_each_badged(tmp_path, monkeypatch):
    m = rp.receipt_panel_model(_badge(tmp_path, monkeypatch, "handoff"))
    assert m["state"] == vb.HANDOFF
    assert m["effects_heading"].startswith("Committed so far")
    assert [(e["objective"], e["label"]) for e in m["effects"]] == [
        ("Objective 1", "Verified"), ("Objective 2", "Claimed")]


def test_an_unreadable_record_is_named_and_shows_nothing_verified(tmp_path, monkeypatch):
    m = rp.receipt_panel_model(_badge(tmp_path, monkeypatch, "unreadable"))
    assert m["state"] == vb.NOT_RECORDED and "could not be read" in m["summary"]
    assert "not recorded" in m["record_note"] and m["effects"] == []
    ui = fx.FakeUI()
    rp.render_receipt_panel(m, _ui=ui)
    assert vb.VERIFIED_ICON not in ui.icons()


def test_a_handoff_with_an_unreadable_record_says_so_inline(tmp_path, monkeypatch):
    fx.point_audit_root(monkeypatch, tmp_path)
    fx.plant(tmp_path, "exec_hu", b"{bad")
    b = vb.run_badge("partial", "exec_hu")
    ui = fx.FakeUI()
    rp.render_committed_effects_list(b, _ui=ui)
    assert any("Committed effects: not recorded" in t for t in ui.texts())


def test_a_proxy_renders_the_not_recorded_receipt():
    for proxy in (None, {"state": "verified"}, "verified"):
        m = rp.receipt_panel_model(proxy)
        assert m["state"] == vb.NOT_RECORDED and m["icon"] != vb.VERIFIED_ICON


def test_a_long_note_collapses_beyond_twenty_lines(tmp_path, monkeypatch):
    fx.point_audit_root(monkeypatch, tmp_path)
    note = "\n".join(f"line {i}" for i in range(30))
    fx.plant(tmp_path, "exec_long", {"1": fx.live(1, detail=note)})
    ui = fx.FakeUI()
    rp.render_receipt_panel(rp.receipt_panel_model(vb.run_badge("success", "exec_long")),
                            _ui=ui)
    assert ("expansion", "Show 10 more line(s)") in [(r["kind"], r["text"]) for r in ui.log]


# ── the badge is a button; one click opens the receipt ───────────────────────

def test_the_badge_opens_the_receipt_in_one_click(tmp_path, monkeypatch):
    b = _badge(tmp_path, monkeypatch, "verified")
    ui = fx.FakeUI()
    rp.render_badge(b, _ui=ui)
    (btn,) = ui.buttons()
    assert btn["text"] == "Verified" and btn["icon"] == vb.VERIFIED_ICON
    assert "s-pill--success" in btn["classes"]
    opened = []
    monkeypatch.setattr(rp, "open_receipt_dialog", lambda badge, **k: opened.append(badge))
    btn["on_click"]()
    assert opened == [b]


def test_no_badge_while_the_run_claims_no_outcome():
    ui = fx.FakeUI()
    rp.render_badge(vb.run_badge("running", "exec_x"), _ui=ui)
    assert ui.log == []


@pytest.mark.parametrize("case", sorted(fx.CASES))
def test_the_filled_check_renders_for_verified_alone(tmp_path, monkeypatch, case):
    b = _badge(tmp_path, monkeypatch, case, eid=f"exec_{case}")
    ui = fx.FakeUI()
    rp.render_badge(b, _ui=ui)
    rp.open_receipt_dialog(b, _ui=ui)
    has_check = vb.VERIFIED_ICON in ui.icons()
    if fx.EXPECTED_RUN_STATE[case] != vb.VERIFIED:
        # a run that is not verified may still LIST a verified effect inside its
        # receipt (a handoff's committed effect) -- but its own badge never shows it
        (btn,) = ui.buttons()[:1]
        assert btn["icon"] != vb.VERIFIED_ICON, case
        if case not in ("failed", "handoff"):
            assert not has_check, case
    else:
        assert has_check


# ── zero network, zero file reads, no secret on open (AC-U10, asserted) ──────

def test_opening_the_receipt_touches_no_network_and_no_file(tmp_path, monkeypatch):
    b = _badge(tmp_path, monkeypatch, "handoff")

    def _no_net(*a, **k):
        raise AssertionError("the receipt panel made a network call on open")

    def _no_open(*a, **k):
        raise AssertionError("the receipt panel read a file on open")

    monkeypatch.setattr(socket.socket, "connect", _no_net)
    monkeypatch.setattr(socket, "create_connection", _no_net)
    monkeypatch.setattr(builtins, "open", _no_open)
    from systemu.runtime import external_verifier
    monkeypatch.setattr(external_verifier.ExternalVerifier, "verify", _no_net,
                        raising=False)
    ui = fx.FakeUI()
    dialog = rp.open_receipt_dialog(b, _ui=ui)
    assert dialog.rec.get("opened") is True
    assert "Committed so far, each effect badged" in ui.texts()


_SECRET = "ghp_" + "Zx9Yw8Vu7Ts6" * 6          # longer than every cap on the path


def test_no_secret_reaches_any_render(tmp_path, monkeypatch):
    fx.point_audit_root(monkeypatch, tmp_path)
    fx.plant(tmp_path, "exec_s", {
        "1": fx.live(1, detail=f"created with token {_SECRET} ok",
                     method=f"api_readback Authorization: Bearer {_SECRET}"),
        _SECRET: fx.live(2, confirmed=False, detail=f"password={_SECRET}"),
    })
    for status in ("success", "pending_decision"):
        b = vb.run_badge(status, "exec_s")
        ui = fx.FakeUI()
        rp.render_badge(b, _ui=ui)
        rp.open_receipt_dialog(b, _ui=ui)
        rp.render_committed_effects_list(b, _ui=ui)
        rendered = ui.all_text() + repr(rp.receipt_panel_model(b))
        assert _SECRET not in rendered and _SECRET[:16] not in rendered, status
        text = vb.badge_text(b) + "\n".join(vb.effect_lines(b))
        assert _SECRET[:16] not in text


def test_operator_copy_in_the_receipt_is_ascii(tmp_path, monkeypatch):
    for case in fx.CASES:
        b = _badge(tmp_path, monkeypatch, case, eid=f"exec_a_{case}")
        m = rp.receipt_panel_model(b)
        for key in ("title", "summary", "counts", "record_note", "effects_heading"):
            m[key].encode("ascii")
        for line in m["anti"]:
            line.encode("ascii")
        for eff in m["effects"]:
            for name, value in eff["rows"]:
                (name + value).encode("ascii")
