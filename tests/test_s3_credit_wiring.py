"""S3 / R-A7 wave-3a — WIRING the ExternalVerifier into the credit seam.

The invariant under test (shadow_runtime.py, the LIVE per-iteration
``completes_objective`` credit, just before the S4 gate reads _external_ok):

  For a ``requires_external_verification`` objective, AFTER a successful effectful
  TOOL_CALL the runtime builds an ``evidence_input`` from ``result.parsed`` (the
  submission-unique token, the submit host, the readback url) + a PRE-SUBMIT
  freshness snapshot, classifies the effect (money-move via
  ``money_move_net_applies``), calls ``ExternalVerifier.verify(...)`` and PERSISTS
  the resulting ``ExternalEvidence`` into ``context._external_evidence`` via
  ``_persist_external_evidence`` — so the existing ``_read_external_ok`` gate then
  credits ONLY on a DETERMINISTIC, host-pinned + https-only + token-fresh
  ``api_readback`` match. The hook NEVER touches the S4 credit-decision code; it
  only populates the store. It runs an LLM NEVER (deterministic-only). It is
  guarded on ``_needs_external`` so a non-external objective is byte-identical.

These are test-first drive-execute() tests, modelled on
tests/test_s4_failclosed_primary.py (the same live-credit harness).
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
#  Harness (mirrors tests/test_s4_failclosed_primary.py)
# ─────────────────────────────────────────────────────────────────────────────

def _build_entities_objs(tmp_path, objectives, *, tool=None):
    """Build a hermetic vault with a Shadow + Tool + Scroll + Activity. ``tool``
    (R-A13b-2i) overrides the default bare ``api_tool`` with a caller-supplied Tool
    entity (e.g. a schema-bearing fixture effectful tool for the AC)."""
    from systemu.vault.vault import Vault
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType, Scroll,
    )
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    vault = Vault(str(tmp_path))

    shadow = Shadow(id="shadow_s3w", name="S3W Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    if tool is None:
        tool = Tool(id="tool_s3w", name="api_tool", description="t",
                    tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                    enabled=True,
                    implementation_path="vault/tools/implementations/api_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_s3w", name="S3W Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=objectives)
    vault.save_scroll(scroll)
    activity = Activity(id="act_s3w", name="S3W Activity", scroll_id=scroll.id,
                        required_tool_ids=[tool.id], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


def _redirect_snapshot_io(monkeypatch, data_dir):
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))


def _softpass_outcome(**kw):
    """A local verifier SOFT-PASS — advisory-only for an external objective. It
    NEVER credits an external objective on its own; only the persisted
    ExternalEvidence.confirmed bit (which the wiring sets) does."""
    from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
    return CompletionOutcome(credited=True, state=ObjectiveState())


def _external_obj(**overrides):
    from systemu.core.models import Objective
    base = dict(id=1, goal="POST the row to the external API",
                success_criteria="row visible via readback",
                requires_external_verification=True)
    base.update(overrides)
    return Objective(**base)


class _EchoReadbackClient:
    """A mock api_readback transport: ``readback(url)`` returns an envelope that
    ECHOES the submission token as an observed token — the deterministic ground
    truth the hardened path matches. Records the urls it was asked to read."""

    def __init__(self, echo_tokens):
        self._echo = list(echo_tokens)
        self.urls = []

    def readback(self, url):
        self.urls.append(url)
        return {"observed_tokens": list(self._echo),
                "response_body": "row present: " + " ".join(self._echo)}


class _CreateOnceReadbackClient:
    """A mock independent readback transport that models a CREATE-ONCE resource: the
    token is ABSENT until the effect is submitted (``mark_submitted``), then ECHOED.

    This is what lets the runtime's INDEPENDENT pre-submit probe prove freshness
    (absent pre-submit) and the post-submit verify match the echo — WITHOUT the tool
    self-attesting freshness (the R-A13b-2i money-move anti-replay contract). The
    drive-execute harness calls ``mark_submitted`` at the submit boundary."""

    def __init__(self, echo_tokens):
        self._echo = list(echo_tokens)
        self._submitted = False
        self.urls = []

    def mark_submitted(self):
        self._submitted = True

    def readback(self, url):
        self.urls.append(url)
        if not self._submitted:
            return {"observed_tokens": [], "response_body": "not found (pre-submit)"}
        return {"observed_tokens": list(self._echo),
                "response_body": "row present: " + " ".join(self._echo)}


def _drive_live_credit(tmp_path, monkeypatch, *, objectives, claim_obj_id,
                       tool_parsed, api_client=None, verify_should_raise=False,
                       completion_side_effect=_softpass_outcome, spy_obs=None,
                       decision_params=None, tool=None):
    """Drive execute() so the LLM issues one succeeding TOOL_CALL that CLAIMS
    ``claim_obj_id`` with ``result.parsed = tool_parsed``, then a deterministic
    terminal FAIL. Returns ``(runtime, result, context)``.

    ``api_client`` (optional) is injected onto the runtime as the external
    verifier's readback transport (``runtime._external_api_client``).
    ``verify_should_raise`` monkeypatches the router to raise on ANY LLM call so
    a test can prove the deterministic hook needs no model.
    ``decision_params`` (R-A13b-2i) seeds the TOOL_CALL's ``parameters`` — used to
    carry a pre-submit ``external`` directive so the runtime's independent
    pre-submit freshness probe can run. At the submit boundary the harness calls
    ``api_client.mark_submitted()`` (if present) so a create-once client flips from
    absent (pre-submit probe) to present (post-submit verify).
    """
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_entities_objs(tmp_path, objectives, tool=tool)
    _tool_name = tool.name if tool is not None else "api_tool"
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    runtime = ShadowRuntime(cfg, vault)
    if api_client is not None:
        runtime._external_api_client = api_client

    from systemu.runtime.tool_sandbox import ToolResult

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        # the submit boundary: a create-once readback client flips absent→present.
        _c = getattr(runtime, "_external_api_client", None)
        if _c is not None and hasattr(_c, "mark_submitted"):
            _c.mark_submitted()
        return ToolResult(success=True, parsed=dict(tool_parsed))
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    if completion_side_effect is not None:
        monkeypatch.setattr(_sr, "process_completion_claim", completion_side_effect)

    if spy_obs is not None:
        import systemu.runtime.context_builder as _cb
        _orig_add = _cb.ExecutionContext.add_observation

        def _spy_add(self, obs, ab):
            try:
                spy_obs.append(obs)
            except Exception:
                pass
            return _orig_add(self, obs, ab)
        monkeypatch.setattr(_cb.ExecutionContext, "add_observation", _spy_add)

    decisions = [
        {"action": "TOOL_CALL", "tool_name": _tool_name,
         "parameters": dict(decision_params or {}),
         "completes_objective": claim_obj_id, "reasoning": "do the external effect"},
        {"action": "FAIL", "reason": "reached only if the objective was NOT credited"},
    ]

    # An LLM router that raises would prove the deterministic hook needs no model,
    # but the loop's DECISION calls are also llm_call_json — so we drive the
    # decisions via side_effect and, when asked, additionally patch the ROUTER
    # (llm_router.route / any tier call the hook might reach) to raise.
    _ctx = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _resolve_spy(**kw):
        objs, sj = orig_resolve(**kw)
        _ctx["context"] = kw.get("context")
        return objs, sj
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _resolve_spy)

    cm = []
    if verify_should_raise:
        import systemu.core.llm_router as _lr

        def _boom(*a, **k):
            raise RuntimeError("router called — the deterministic hook must not do this")
        # patch the router entry points the verifier could conceivably touch.
        for _name in ("route", "route_json", "call", "complete"):
            if hasattr(_lr, _name):
                monkeypatch.setattr(_lr, _name, _boom, raising=False)

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(shadow, activity))
    return runtime, result, _ctx.get("context")


# ─────────────────────────────────────────────────────────────────────────────
#  1. happy path — fresh token + echoing api_readback ⇒ CREDITED
# ─────────────────────────────────────────────────────────────────────────────

def test_fresh_api_readback_echo_credits_external(tmp_path, monkeypatch):
    """(1) a succeeding effectful tool returns a FRESH submission token + the
    readback url/submit host; a mock api_readback client echoes the token on the
    PINNED https host, and a pre-submit snapshot proves freshness → the verifier
    CONFIRMS, the wiring persists it, and _read_external_ok credits the objective
    (the run finalizes success)."""
    token = "sub-XZ-777-unique"
    url = "https://api.example.com/rows/777"
    # R-A13b-2i: this is a money-move (external + unclassified) ⇒ freshness may come
    # ONLY from the runtime's independent pre-submit probe, never a tool self-report.
    # A create-once client is ABSENT pre-submit (probe) → PRESENT post-submit (echo).
    client = _CreateOnceReadbackClient(echo_tokens=[token])
    directive = {"readback_url": url, "expected_tokens": [token],
                 "submit_host": "api.example.com"}
    tool_parsed = {
        "ok": True,
        # what the tool exposes for external verification (DIRECTIVES only):
        "external": {
            "strategy": "api_readback",
            "expected_tokens": [token],
            "submission_token": token,
            "readback_url": url,
            "submit_host": "api.example.com",
        },
    }
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_external_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client,
        decision_params={"external": directive})

    assert result.get("status") == "success", (
        "a fresh, host-pinned, https, token-echoed api_readback (freshness proven by "
        f"the runtime probe) must confirm + credit; got {result.get('status')}")
    # the wiring took the HARDENED path: the injected client was read PRE-submit
    # (the freshness probe) AND POST-submit (the verify) on the pinned url.
    assert client.urls == [url, url], (
        "the runtime probe (pre-submit) + hardened readback (post-submit) must both "
        f"fetch via the injected client on the pinned https url; client saw {client.urls}")
    store = getattr(ctx, "_external_evidence", {})
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("confirmed") is True, (
        f"a confirmed ExternalEvidence must be persisted for obj 1; store={store}")
    assert ev.get("method") == "api_readback"


# ─────────────────────────────────────────────────────────────────────────────
#  2. money-move + web_assertion-only ⇒ NOT credited + UNVERIFIED_EXTERNAL
# ─────────────────────────────────────────────────────────────────────────────

def test_money_move_web_assertion_only_not_credited(tmp_path, monkeypatch):
    """(2) a MONEY-MOVE external objective whose tool returns only a
    web_assertion (a DOM text match) → the money-move hard gate demotes the
    advisory strategy, nothing is confirmed, the objective is NOT credited, and an
    UNVERIFIED_EXTERNAL observation is emitted. A web_assertion alone can NEVER
    credit a money move (double-submit hazard)."""
    obs = []
    tool_parsed = {
        "ok": True,
        "external": {
            "strategy": "web_assertion",
            "expected_text": "Payment complete",
            "observed_text": "Payment complete",
        },
    }
    # a money-move objective (the goal carries a money verb + amount).
    obj = _external_obj(goal="pay the $500 invoice via the checkout API",
                        success_criteria="payment confirmed")
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[obj], claim_obj_id=1,
        tool_parsed=tool_parsed, spy_obs=obs)

    assert result.get("status") != "success", (
        "a web_assertion alone must NOT credit a MONEY-MOVE external objective; "
        f"got {result.get('status')}")
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert unv, f"expected an UNVERIFIED_EXTERNAL observation; saw {obs}"
    store = getattr(ctx, "_external_evidence", {})
    ev = store.get("1") or store.get(1)
    assert not (ev and ev.get("confirmed") is True), (
        f"a web_assertion must not confirm a money move; store={store}")


# ─────────────────────────────────────────────────────────────────────────────
#  2b. "settle the balance" (money VERB, no currency/amount) + web_assertion-only
#      ⇒ NOT credited. The BLOCKER-3 end-to-end repro: the extended verb allowlist
#      makes this a money-move, so the advisory web_assertion cannot credit it.
# ─────────────────────────────────────────────────────────────────────────────

def test_settle_balance_web_assertion_only_not_credited(tmp_path, monkeypatch):
    """(2b) an external objective 'settle the outstanding balance' — a money-move
    by VERB with NO currency/amount, previously read as non-financial — whose tool
    returns only a web_assertion → the money-move hard gate demotes the advisory
    strategy, nothing is confirmed, and the objective is NOT credited. A
    web_assertion alone can NEVER credit a settle-the-balance money-move."""
    obs = []
    tool_parsed = {
        "ok": True,
        "external": {
            "strategy": "web_assertion",
            "expected_text": "Balance settled",
            "observed_text": "Balance settled",
        },
    }
    obj = _external_obj(goal="settle the outstanding balance for the vendor",
                        success_criteria="balance shows settled")
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[obj], claim_obj_id=1,
        tool_parsed=tool_parsed, spy_obs=obs)

    assert result.get("status") != "success", (
        "a web_assertion alone must NOT credit a 'settle the balance' money-move; "
        f"got {result.get('status')}")
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert unv, f"expected an UNVERIFIED_EXTERNAL observation; saw {obs}"
    store = getattr(ctx, "_external_evidence", {})
    ev = store.get("1") or store.get(1)
    assert not (ev and ev.get("confirmed") is True), (
        f"a web_assertion must not confirm a settle-the-balance money move; store={store}")


# ─────────────────────────────────────────────────────────────────────────────
#  3. stale token (present pre-submit) ⇒ freshness refuses ⇒ NOT credited
# ─────────────────────────────────────────────────────────────────────────────

def test_stale_token_present_presubmit_not_credited(tmp_path, monkeypatch):
    """(3) the readback echoes the expected token, host-pin + https pass, BUT the
    token was ALREADY present in the pre-submit snapshot (stale — it can't prove
    THIS run produced the effect) → freshness refuses → NOT credited."""
    obs = []
    token = "row-id-42"
    client = _EchoReadbackClient(echo_tokens=[token])
    tool_parsed = {
        "ok": True,
        "external": {
            "strategy": "api_readback",
            "expected_tokens": [token],
            "readback_url": "https://api.example.com/rows/42",
            "submit_host": "api.example.com",
            # STALE: the token was observed pre-submit too.
            "presubmit_tokens": [token],
            "pre_submit_absent": False,
        },
    }
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_external_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client, spy_obs=obs)

    assert result.get("status") != "success", (
        "a token already present pre-submit is STALE and must NOT credit; "
        f"got {result.get('status')}")
    store = getattr(ctx, "_external_evidence", {})
    ev = store.get("1") or store.get(1)
    assert not (ev and ev.get("confirmed") is True), (
        f"a stale token must not confirm; store={store}")


# ─────────────────────────────────────────────────────────────────────────────
#  4. deterministic-only — router raising still credits on the deterministic match
# ─────────────────────────────────────────────────────────────────────────────

def test_deterministic_only_router_raise_still_credits(tmp_path, monkeypatch):
    """(4) with the LLM router patched to RAISE on any call, a fresh echoed
    api_readback still confirms + credits — proving the verifier hook sets
    ``confirmed`` from a DETERMINISTIC token match, never from a model."""
    token = "det-only-999"
    url = "https://api.example.com/rows/999"
    client = _CreateOnceReadbackClient(echo_tokens=[token])   # absent → present
    directive = {"readback_url": url, "expected_tokens": [token],
                 "submit_host": "api.example.com"}
    tool_parsed = {
        "ok": True,
        "external": {
            "strategy": "api_readback",
            "expected_tokens": [token],
            "readback_url": url,
            "submit_host": "api.example.com",
        },
    }
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_external_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client, verify_should_raise=True,
        decision_params={"external": directive})

    assert result.get("status") == "success", (
        "the deterministic verifier hook must confirm/credit with NO LLM — a "
        f"raising router must not prevent it; got {result.get('status')}")
    store = getattr(ctx, "_external_evidence", {})
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("confirmed") is True


# ─────────────────────────────────────────────────────────────────────────────
#  5. non-external objective — credited via the normal S4 path, byte-identical
# ─────────────────────────────────────────────────────────────────────────────

def test_non_external_objective_credits_unchanged(tmp_path, monkeypatch):
    """(5) a requires_external_verification=False objective credits via the normal
    local-verifier soft-pass — the external hook is entirely skipped (no
    ExternalEvidence written), byte-identical to pre-S3 behaviour."""
    from systemu.core.models import Objective
    obj = Objective(id=1, goal="write the local report file",
                    success_criteria="file exists",
                    requires_external_verification=False)
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[obj], claim_obj_id=1,
        tool_parsed={"ok": True})

    assert result.get("status") == "success", (
        "a non-external objective must credit via the normal path; "
        f"got {result.get('status')}")
    store = getattr(ctx, "_external_evidence", {}) or {}
    assert not store, (
        "the external hook must not run for a non-external objective (no evidence "
        f"written); store={store}")
