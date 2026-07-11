"""R-A13b-2ii-a TASK 5 — committed FIXTURE effectful tools + the LIVE-CONSUMER ACs.

The anti-dormancy tripwire (design §"LIVE-CONSUMER AC"). NO shipped seed tool matches any
money/send signal, so a curated map alone classifies ZERO live tools (an R-A12c trap:
machinery, no consumer). These fixtures are exercised through the REAL backfill→classify→
meter path (reusing the drive-execute harness), and NOT shipped as enabled seed tools.

Two ACs:
  * classification-live — a fixture the curated map classifies as send_message/money_move
    → the REAL backfill tags it → the SHADOW meter buckets it under THAT class (send_message
    is impossible without the tag, so it is distinct from the empty-tags disjunct-3
    money_move default) → the arm-verdict reasons reflect it.
  * safety-live (residual 3, LOAD-BEARING) — the money_move fixture, classified money_move,
    under a BENIGN objective (no money words, requires_external NOT relied on) still hard-
    gates at BOTH money-move seams. The tool tag ALONE carries money-move-ness. If this
    can't pass, 2ii-a's fail-closed guarantee failed → STOP.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

# the REAL drive-execute + meter harness (no tests/__init__.py → top-level imports)
from test_ra13b1_shadow_meter import (
    _shadow_obj, _stamp_shadow_on_resolve, _metrics_snapshot)
from test_s3_credit_wiring import _drive_live_credit

from systemu.runtime.effect_tags import classify_source, EffectTag
from systemu.runtime.financial_signal import money_move_net_applies
from systemu.runtime.shadow_runtime import _classify_external_effect, _is_money_move_seam
from systemu.runtime.external_verifier import ExternalVerifier

# ── the committed fixture SOURCES (a money-move + a send-message effectful tool) ──
_MONEY_SRC = (
    "import stripe\n"
    "def run(**kwargs):\n"
    "    return stripe.PaymentIntent.create(amount=kwargs.get('amount'), currency='usd')\n"
)
_SEND_SRC = (
    "import smtplib\n"
    "def run(to, body):\n"
    "    s = smtplib.SMTP('smtp.example.com')\n"
    "    return s.sendmail('me@x', to, body)\n"
)

_MONEY = EffectTag.MONEY_MOVE.value
_SEND = EffectTag.SEND_MESSAGE.value


def _seed_and_backfill(vault: Path, tid: str, name: str, source: str) -> list:
    """Seed a fixture tool + run the REAL vault backfill; return the effect_tags the
    classifier+floor stamped onto the tool body (the backfill→classify half of the AC)."""
    from systemu.runtime import vault_migrator as vm
    tools = vault / "tools"
    (tools / "implementations").mkdir(parents=True, exist_ok=True)
    (tools / "implementations" / f"{name}.py").write_text(source, encoding="utf-8")
    body = {"id": tid, "name": name, "description": "fixture", "tool_type": "python",
            "implementation_path": f"{name}.py", "status": "deployed"}
    (tools / f"tool_{tid}.json").write_text(json.dumps(body), encoding="utf-8")
    (tools / "index.json").write_text(json.dumps([{"id": tid, "name": name}]), encoding="utf-8")
    vm.backfill_effect_tags(vault, version="0.9.73")
    return json.loads((tools / f"tool_{tid}.json").read_text(encoding="utf-8"))["effect_tags"]


def _fixture_tool(tid: str, name: str, effect_tags):
    from systemu.core.models import Tool, ToolStatus, ToolType
    return Tool(id=tid, name=name, description="fixture",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True, effect_tags=list(effect_tags),
                implementation_path=f"vault/tools/implementations/{name}.py")


# ─────────────────────────────────────────────────────────────────────────────
#  AC — classification-live (the anti-dormancy tripwire)
# ─────────────────────────────────────────────────────────────────────────────

def test_backfill_classifies_the_committed_fixtures(tmp_path):
    """The backfill→classify half: the committed fixture sources tag money_move /
    send_message through the REAL vault backfill (no synthetic tags)."""
    money = _seed_and_backfill(tmp_path / "m", "fx_pay", "payer", _MONEY_SRC)
    send = _seed_and_backfill(tmp_path / "s", "fx_msg", "mailer", _SEND_SRC)
    assert _MONEY in money, money
    assert _SEND in send and _MONEY not in send, send


def test_classification_live_send_message_fixture_buckets_send_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    tags = _seed_and_backfill(tmp_path / "bf", "fx_msg", "mailer", _SEND_SRC)
    assert _SEND in tags
    tool = _fixture_tool("tool_send", "mailer", tags)
    runtime, result, ctx = _drive_live_credit(
        tmp_path / "run", monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True}, tool=tool)
    # record-only: the run still completes (the meter never parks the run).
    assert result.get("status") == "success", result
    snap = _metrics_snapshot(runtime)
    # bucketed under send_message — IMPOSSIBLE for an empty-tags tool (which buckets
    # money_move via disjunct-3), so this proves the classification is LIVE + distinct.
    assert snap.get("send_message", {}).get("would_stamp", 0) >= 1, snap
    assert _MONEY not in snap, f"a send-message fixture must NOT bucket money_move; {snap}"
    # the arm-verdict reasons reflect the newly-classified class (its dead channel).
    from systemu.runtime.s4_activation import s4_shadow_arm_verdict
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=1)
    assert any("send_message" in r for r in reasons), reasons


def test_classification_live_money_move_fixture_buckets_money_move(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    tags = _seed_and_backfill(tmp_path / "bf", "fx_pay", "payer", _MONEY_SRC)
    assert _MONEY in tags
    tool = _fixture_tool("tool_pay", "payer", tags)
    runtime, result, ctx = _drive_live_credit(
        tmp_path / "run", monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True}, tool=tool)
    assert result.get("status") == "success", result
    snap = _metrics_snapshot(runtime)
    assert snap.get("money_move", {}).get("would_stamp", 0) >= 1, snap
    # (design meter-delta (d)) a money_move bucket with no independent client PARKS —
    # a Stage-3 concern, expected in 2ii; do NOT "fix" it here.
    assert snap["money_move"]["would_park"] >= 1, snap


def test_meter_delta_before_empty_after_classified(tmp_path, monkeypatch):
    """The GATE-MOVING meter-delta, through the REAL meter path: the SAME send-message
    source buckets money_move BEFORE classification (empty tags → disjunct-3 default)
    and send_message AFTER (the classified tag). 2ii-a moves the bucket."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)

    # BEFORE — an empty-tags tool (pre-2ii classification of the same tool)
    rt_b, _, _ = _drive_live_credit(
        tmp_path / "before", monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True}, tool=_fixture_tool("t_before", "mailer_b", []))
    before = _metrics_snapshot(rt_b)

    # AFTER — the REAL backfilled send_message tag on the same source
    tags = _seed_and_backfill(tmp_path / "bf", "fx_msg", "mailer", _SEND_SRC)
    rt_a, _, _ = _drive_live_credit(
        tmp_path / "after", monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True}, tool=_fixture_tool("t_after", "mailer_a", tags))
    after = _metrics_snapshot(rt_a)

    assert "money_move" in before and "send_message" not in before, before
    assert "send_message" in after, after


# ─────────────────────────────────────────────────────────────────────────────
#  AC — safety-live (residual 3 — LOAD-BEARING; if this fails, STOP)
# ─────────────────────────────────────────────────────────────────────────────

def test_safety_live_money_move_fixture_hard_gates_objective_text_independent():
    """The residual-(3) PROVING invariant: the money_move fixture, classified
    money_move, under a BENIGN objective (NO money words) with requires_external NOT
    relied on, is money-move at BOTH seams — the tool TAG ALONE hard-gates it."""
    classified = {t.value for t in classify_source(_MONEY_SRC)}
    assert _MONEY in classified, classified

    benign = "post the row to the external api and record the result"   # NO money tokens
    # verbatim design proving test: tag alone, no objective text, no requires_external.
    assert money_move_net_applies(
        classified, benign, None, requires_external=False) is True

    objective = SimpleNamespace(goal=benign, success_criteria="row visible",
                                requires_external_verification=False, effect_tags=[])
    tool = SimpleNamespace(name="payer", effect_tags=sorted(classified))
    decision = {"parameters": {}}

    # branch-selection seam (_is_money_move_seam) — objective-text-independent.
    assert _is_money_move_seam(objective, decision, tool) is True
    # the effect classified for the verify gate is money_move …
    effect_class = _classify_external_effect(objective, decision, tool)
    assert effect_class == _MONEY
    # … and the verify seam (_is_money_move) hard-gates it too.
    ev = ExternalVerifier(api_client=None)
    assert ev._is_money_move(objective, effect_class) is True


def test_safety_live_contrast_empty_tags_tool_is_not_money_move():
    """CONTRAST proving the money-move-ness comes from the CLASSIFIED tag, not the
    objective: the SAME benign objective with an EMPTY-tags tool is NOT money-move
    (requires_external NOT relied on)."""
    benign = "post the row to the external api and record the result"
    empty_tool = SimpleNamespace(name="x", effect_tags=[])
    objective = SimpleNamespace(goal=benign, success_criteria="row visible",
                                requires_external_verification=False, effect_tags=[])
    assert _is_money_move_seam(objective, {"parameters": {}}, empty_tool) is False
    assert money_move_net_applies(set(), benign, None, requires_external=False) is False


# ─────────────────────────────────────────────────────────────────────────────
#  META-CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def test_dec24_stamp_gate_still_stamps_newly_classified_effects():
    """No UNDER-stamp: the newly-classified money_move/send_message tools are in the
    DEC-24 stamp set, so the S1/stamp gate still marks them dangerous. A benign
    net_read tool still does NOT stamp."""
    from systemu.runtime.requirement_binder import _effect_tags_are_dangerous
    assert _effect_tags_are_dangerous({"effect_tags": [_MONEY]}) is True
    assert _effect_tags_are_dangerous({"effect_tags": [_SEND]}) is True
    assert _effect_tags_are_dangerous({"effect_tags": ["net_read"]}) is False


def test_money_move_monotonic_both_money_and_net_keeps_money():
    """A source that is money on TWO axes (host POST) keeps money_move alongside
    net_mutate — a UNION with money_move is the safe both-match case."""
    tags = {t.value for t in classify_source(
        "import requests\nrequests.post('https://api.stripe.com/v1/charges', json={})")}
    assert _MONEY in tags and "net_mutate" in tags, tags


def test_no_benign_tool_newly_stamps_money_or_send():
    """A plain requests.get / os.path tool must NOT newly stamp money_move/send_message."""
    for src in ("import requests\nrequests.get('https://example.com/x')",
                "import os\nos.path.join('a', 'b')"):
        tags = {t.value for t in classify_source(src)}
        assert _MONEY not in tags and _SEND not in tags, (src, tags)
