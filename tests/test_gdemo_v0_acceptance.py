"""R-A14a slice 4 — the §12A G-DEMO v0 acceptance fixture ("receipts, not
self-report"). MASTER-SPEC §12A, MASTER-PLAN R-A15 / PLAN-5.

WHY THIS FIXTURE EXISTS (the anti-dormancy tripwire)
────────────────────────────────────────────────────
No OTHER test joins the two LIVE seams. Every current test STUBS one side — the
credit-wiring harness monkeypatches ``_handle_tool_call`` (fake tool RESULT), and
the off-mode regression injects a stubbed tool result rather than a real MCP call.
So the real spine

    REAL FastMCP create_issue over stdio
      → FastMCP structuredContent
      → manager._result_to_envelope
      → L4 _guard_mcp_output
      → mcp_modality._unwrap_payload
      → _synthesize_directive
      → REAL ProdReadbackClient.readback (mock-REST transport ONLY)
      → ExternalEvidence(confirmed=True, method="api_readback")

has NEVER run end-to-end. If FastMCP nests the payload, the L4 banner mangles it,
or ``_synthesize_directive`` finds no https URL, the receipt silently degrades to
CLAIMED and a naive ``status=="success"`` check would still pass. THIS fixture is
the tripwire: it asserts ``confirmed is True`` AND ``method == "api_readback"``,
which FORCES both live seams to actually run. It STUBS NEITHER:
``_handle_tool_call`` / ``call_mcp_tool`` / ``mcp_call_tool`` / ``capture_evidence``
all run for real; the ONLY mocks are the readback TRANSPORT (an ``httpx``
MockTransport injected into the runtime's OWN ProdReadbackClient) and the LLM
DECISION (a scripted TOOL_CALL — no real model).

PLAN-5 ACCEPTANCE MATRIX (declared, not implied)
────────────────────────────────────────────────
  * M-API (this file) — the "open a GitHub issue → independently read it back →
    machine-verified receipt" acceptance is **CI-automatable** via a hermetic
    FastMCP stdio server + a local mock-REST readback transport. No network, no
    real token, no DNS (a public-IP-literal host). Runs with the verification net
    OFF (default) — the ``mcp`` modality carries its OWN per-actuation obligation.
  * The live COLD-INSTALL variant (a real github MCP connector + a real GitHub
    token hitting api.github.com) is **operator-tryout-only** — it needs a
    credential and a real outbound call, so it is NOT run in CI. This file is the
    automatable half of that matrix; the cold install is the manual half.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx
import pytest

# sibling imports (no tests/__init__.py) — reuse the hermetic vault builder, the
# soft-pass completion outcome, the snapshot-IO redirect, and the MCP fixtures.
from test_s3_credit_wiring import (
    _build_entities_objs, _softpass_outcome, _redirect_snapshot_io,
)
from test_ra14a_mcp_credit_link import _mcp_tool, _external_obj
from test_ra14a_off_mode_regression import _patch_prod_client_transport, _PUB_IP
from _github_reference_server import issue_id_for   # the distinctive id formula

REF_SERVER = str(Path(__file__).resolve().parent / "_github_reference_server.py")


# ─────────────────────────────────────────────────────────────────────────────
#  the mock-REST readback handler — STATELESS + deterministic. It parses ``n``
#  from the readback path (the create's html_url, which _synthesize_directive
#  copies to readback_url) and ECHOES the same n as id/number → every expected
#  token is present → _tokens_all_present passes. (Freshness comes from the
#  synthesized directive's pre_submit_absent for the NON-money create.)
# ─────────────────────────────────────────────────────────────────────────────

def _make_readback_handler(seen):
    def _handler(request):
        seen.append(str(request.url))
        m = re.search(r"/(?:issues|payments)/(\d+)", request.url.path)
        n = int(m.group(1)) if m else 0
        # Echo the DISTINCTIVE id (issue_id_for(n)) the create returned, not just n.
        # This is what makes the token match load-bearing: a wrong readback (a
        # different n) yields a different distinctive id that is genuinely ABSENT
        # from the expected-token set, so the match fails — unlike a small "n" that
        # would coincidentally substring-match the IP host. (Proven by
        # test_gdemo_wrong_readback_stays_claimed.)
        return httpx.Response(
            200, headers={"content-type": "application/json"},
            json={"id": issue_id_for(n), "number": n, "state": "open",
                  "html_url": str(request.url)})
    return _handler


# ─────────────────────────────────────────────────────────────────────────────
#  the REAL-path drive harness — stubs NEITHER live seam.
#
#  Unlike test_s3_credit_wiring._drive_live_credit (which monkeypatches
#  ``_handle_tool_call`` with a fake RESULT), this lets the REAL _handle_tool_call
#  → v2 dispatch → registry_bridge handler → call_mcp_tool (L2/L3/L4 gate) → REAL
#  FastMCP stdio create_issue run, and lets _mcp_actuation_link drive the REAL
#  ProdReadbackClient (mock TRANSPORT only). The only patches are the scripted LLM
#  decision + the local soft-pass verifier (both hermetic, no model, no network).
# ─────────────────────────────────────────────────────────────────────────────

def _drive_real_mcp_credit(tmp_path, monkeypatch, *, server, tool, objectives,
                           claim_obj_id, params, annotations, readback_seen):
    """Stand up a REAL MCP (server, tool) over stdio, pre-satisfy L2/L3, inject the
    mock-REST readback transport, and drive the real execute() loop with a scripted
    ``TOOL_CALL mcp__<server>__<tool>``. Returns ``(runtime, result, context,
    cleanup)``. STUBS NEITHER the MCP-call seam NOR the readback seam."""
    import asyncio
    from unittest.mock import patch
    from sharing_on.config import Config
    from systemu.runtime.mcp import connections as mcp_conn
    from systemu.runtime.mcp.sdk import registry_bridge
    from systemu.runtime import command_approvals as ca
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr

    namespaced = f"mcp__{server}__{tool}"
    # a vault Tool entity satisfies the activity's required_tool_ids; real dispatch
    # goes through the v2 MCP registry entry (below), not this vault Tool.
    vault, shadow, activity = _build_entities_objs(
        tmp_path, objectives, tool=_mcp_tool(namespaced))

    # ── register the REAL MCP tool: transport + allowlist + v2 bridge ──
    stdio = {"transport": "stdio", "command": sys.executable,
             "args": [REF_SERVER], "env": {}}
    mcp_conn.add_server(vault, server, transport=stdio)
    schema = {"type": "object",
              "properties": {k: {"type": "string"} for k in params},
              "required": [k for k in params if k != "body"]}
    mcp_conn.set_tool_enabled(vault, server, tool, True, description=f"{tool} tool",
                              schema=schema, annotations=annotations)
    registry_bridge.register_server_tools(vault, server, [{
        "name": tool, "description": f"{tool} tool",
        "parameters_schema": schema, "annotations": annotations}])

    # ── pre-satisfy the L3 MCP gate WITHOUT a live operator: seed an
    #    "Always allow" for mcp_signature(server, tool). init_default_store forces
    #    the process singleton to OUR tmp store so _gate_mcp_call's
    #    ``get_default_store() or init_default_store("data")`` reads THIS approval.
    data_dir = tmp_path / "approvals"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = ca.init_default_store(data_dir)
    store.approve(ca.mcp_signature(server, tool), command=f"mcp:{server}:{tool}")

    # ── inject the mock-REST readback TRANSPORT into the runtime's OWN
    #    ProdReadbackClient (the ONLY permitted mock). MUST precede construction.
    _patch_prod_client_transport(monkeypatch, _make_readback_handler(readback_seen))

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    _redirect_snapshot_io(monkeypatch, tmp_path / "snap_data")

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    runtime = ShadowRuntime(cfg, vault)           # injects the ProdReadbackClient itself
    monkeypatch.setattr(_sr, "process_completion_claim", _softpass_outcome)

    # capture the ExecutionContext the run built.
    _ctx = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _resolve_spy(**kw):
        objs, sj = orig_resolve(**kw)
        _ctx["context"] = kw.get("context")
        return objs, sj
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _resolve_spy)

    decisions = [
        {"action": "TOOL_CALL", "tool_name": namespaced, "parameters": dict(params),
         "completes_objective": claim_obj_id, "reasoning": "do the external effect"},
        {"action": "FAIL", "reason": "reached only if the objective was NOT credited"},
    ]

    def _cleanup():
        try:
            registry_bridge.unregister_server_tools(server)
        finally:
            ca.reset_default_store_for_tests()

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(shadow, activity))
    return runtime, result, _ctx.get("context"), _cleanup


# ─────────────────────────────────────────────────────────────────────────────
#  Step 2 — THE HERO. A REAL create_issue + a REAL readback (neither stubbed) →
#  a MACHINE-VERIFIED receipt: confirmed is True AND method == "api_readback".
# ─────────────────────────────────────────────────────────────────────────────

def test_gdemo_issue_is_machine_verified(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)   # net OFF (default)
    seen: list = []
    runtime, result, ctx, cleanup = _drive_real_mcp_credit(
        tmp_path, monkeypatch,
        server="github", tool="create_issue",
        objectives=[_external_obj("open a GitHub issue for the login bug")],
        claim_obj_id=1,
        params={"repo": "octocat/hello", "title": "login button 500s", "body": "repro"},
        annotations={"readOnlyHint": False},   # action tier (non-destructive)
        readback_seen=seen)
    try:
        assert result.get("status") == "success", (
            "a REAL create_issue whose REAL independent readback confirms the token "
            f"must credit net-OFF; got {result.get('status')}")

        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        # THE tripwire assertions — a stubbed/degraded path would report success but
        # leave the receipt CLAIMED (confirmed False) or a non-readback method.
        assert ev is not None, f"an ExternalEvidence receipt must be persisted; store={store}"
        assert ev["confirmed"] is True, (
            "the receipt must be MACHINE-VERIFIED (confirmed True) through the REAL "
            f"readback — not merely CLAIMED; ev={ev}")
        assert ev["method"] == "api_readback", (
            "confirmation must come from the INDEPENDENT api_readback seam, not a "
            f"self-report/attest/local verdict; ev={ev}")
        assert not ev.get("shadow"), "a LIVE credit, not a shadow-meter record"

        # PROOF the readback seam actually ran (receipts, NOT self-report): the
        # runtime's own ProdReadbackClient issued an independent GET on the created
        # issue's URL — the confirmation did NOT ride the create's own output.
        assert seen, "the independent readback transport was NEVER hit — the receipt would be self-reported"
        assert any("/issues/" in u for u in seen), (
            f"the readback re-read the created ISSUE resource; urls seen={seen}")
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  Step 3 — §12A framing: the receipt is EXTERNAL-VERIFICATION grounded (not
#  incidental) and NOTHING was fabricated (the confirmed tokens equal the REAL
#  created issue's id/number), and the persisted receipt carries NO secret.
# ─────────────────────────────────────────────────────────────────────────────

def test_gdemo_receipt_is_grounded_and_unfabricated(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)
    seen: list = []
    runtime, result, ctx, cleanup = _drive_real_mcp_credit(
        tmp_path, monkeypatch,
        server="github", tool="create_issue",
        objectives=[_external_obj("open a GitHub issue for the login bug")],
        claim_obj_id=1,
        params={"repo": "octocat/hello", "title": "login button 500s"},
        annotations={"readOnlyHint": False},
        readback_seen=seen)
    try:
        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        assert ev and ev["confirmed"] is True

        # (a) EXTERNAL-VERIFICATION grounded, not incidental: the confirmation came
        #     from the INDEPENDENT machine readback (api_readback), and the readback
        #     transport was genuinely exercised (a self-report path would leave it
        #     untouched — cf. the off-mode dormant-client regression).
        assert ev["method"] == "api_readback"
        assert len(seen) >= 1, "the receipt must be gated on a real independent re-read"

        # (b) NOTHING FABRICATED: the readback re-read the EXACT url the create
        #     returned, and the number it verified is the REAL created issue number
        #     (parsed from that url) — the confirmed token is the real resource's id,
        #     not an invented one.
        readback_url = seen[-1]
        m = re.search(r"/issues/(\d+)", readback_url)
        assert m, f"the readback url must carry the created issue number; url={readback_url}"
        created_n = m.group(1)
        # the create's html_url host is the SSRF-safe public-IP literal (DNS-free).
        assert readback_url.startswith(f"https://{_PUB_IP}/repos/"), readback_url
        # the receipt confirmed on the HARDENED, host-pinned, https, FRESH readback
        # path (the create-once proof) — proven by the deterministic detail note.
        assert "host-pinned https" in ev.get("detail", ""), ev
        assert "fresh" in ev.get("detail", ""), ev
        # the confirmed evidence is for THIS objective and this real resource number.
        assert ev["objective_id"] == 1
        assert created_n in readback_url

        # (c) MASK / §12A step F: the PERSISTED receipt exposes only receipt fields
        #     — no raw evidence / headers / cookies. It carries NO real secret VALUE:
        #     masking preserves every load-bearing field (method/detail/confirmed/
        #     objective_id), and the ONLY field the MASK key-scrub even touches is the
        #     ``idempotency_key`` name, whose value is empty (nothing sensitive
        #     persisted). The ``detail`` note is contractually never a secret and
        #     survives the value-shape scrub unchanged.
        from systemu.runtime.external_verifier import _mask_evidence
        masked = _mask_evidence(ev)
        assert masked["method"] == ev["method"] == "api_readback"
        assert masked["detail"] == ev["detail"], "detail is never a secret (survives MASK)"
        assert masked["confirmed"] is True and masked["objective_id"] == 1
        assert not ev.get("idempotency_key"), (
            "no secret material is persisted on the receipt (idempotency_key empty); "
            f"ev={ev}")
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  Step 4 — the money-move NEGATIVE control. A REAL create_payment over the SAME
#  live path whose readback ECHOES the id — yet the receipt STAYS CLAIMED
#  (confirmed is False), fail-closed: there is no independent MCP pre-submit probe
#  to prove freshness, so a money-move can NEVER be faux-verified via self-report.
# ─────────────────────────────────────────────────────────────────────────────

def test_gdemo_money_move_stays_claimed(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)
    seen: list = []
    runtime, result, ctx, cleanup = _drive_real_mcp_credit(
        tmp_path, monkeypatch,
        server="pay", tool="create_payment",
        objectives=[_external_obj("pay the $500 invoice via the payments API")],
        claim_obj_id=1,
        params={"payee": "acme-vendor", "amount": "500"},
        annotations={"readOnlyHint": False},
        readback_seen=seen)
    try:
        # fail-closed: a money-move MCP mutation with NO independent fresh probe must
        # NOT credit — even though the readback would echo the token.
        assert result.get("status") != "success", (
            "a money-move MCP mutation must NOT credit without an independent fresh "
            f"pre-submit probe (none exists for MCP); got {result.get('status')}")
        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        # a receipt may be persisted (best-effort provenance) but must stay CLAIMED.
        assert not (ev and ev.get("confirmed") is True), (
            "a money-move can NEVER be faux-verified via a self-reported readback; "
            f"receipt must stay confirmed=False. store={store}")
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  Step 5 — the WRONG-READBACK negative control: PROVES the RECEIPT is
#  load-bearing (the hero's confirmed=True is NOT spurious). An independent re-read
#  that returns a DIFFERENT resource (different distinctive id, number, and url)
#  MUST flip the receipt to CLAIMED (confirmed=False) — otherwise the token match
#  "confirmed" on a coincidence (e.g. a tiny token substring-matching the IP host)
#  and the whole "machine-verified" claim would be a false positive.
#
#  NOTE — this is the DISTINCTIVE-ID guard. It is the reason ``_github_reference_
#  server.create_issue`` returns a large opaque ``id`` (issue_id_for(n)) distinct
#  from the small human ``number``: with a tiny token like "1", a wrong readback
#  would substring-match "1" inside the IP host and spuriously confirm. The
#  distinctive id makes the match genuinely load-bearing.
#
#  The RUN still reports success here: a NON-money MCP credit is deliberately
#  NON-GATING (v0.9.78 — the tool's own success credits the objective; the
#  independent readback ADDS a machine-verified receipt when it confirms). Only a
#  MONEY-MOVE fail-closes the credit on the receipt (proven above). So the
#  wrong-readback effect is precisely a CLAIMED receipt, not an un-credit — which
#  is exactly the "receipt quality" the demo showcases.
# ─────────────────────────────────────────────────────────────────────────────

def test_gdemo_wrong_readback_stays_claimed(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)

    # a readback of a DIFFERENT issue: id/number AND the echoed url all carry a
    # foreign number, so NONE of the real created issue's expected tokens (its
    # distinctive id, its number) appear anywhere in the response.
    def _wrong_handler_factory(seen):
        def _h(request):
            seen.append(str(request.url))
            other = 88888
            return httpx.Response(
                200, headers={"content-type": "application/json"},
                json={"id": issue_id_for(other), "number": other, "state": "open",
                      "html_url": f"https://{_PUB_IP}/repos/x/y/issues/{other}"})
        return _h

    monkeypatch.setattr(
        sys.modules[__name__], "_make_readback_handler", _wrong_handler_factory)

    seen: list = []
    runtime, result, ctx, cleanup = _drive_real_mcp_credit(
        tmp_path, monkeypatch,
        server="github", tool="create_issue",
        objectives=[_external_obj("open a GitHub issue for the login bug")],
        claim_obj_id=1,
        params={"repo": "octocat/hello", "title": "login button 500s"},
        annotations={"readOnlyHint": False},
        readback_seen=seen)
    try:
        assert seen, "the readback transport must still have been hit"
        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        # THE anti-spuriousness gate: a non-matching readback must NOT machine-verify
        # the receipt — the token match must be load-bearing, not an IP-substring
        # coincidence. (A CLEAN comparison to the hero, which confirms=True with the
        # matching readback — the ONLY difference is whether the readback echoes the
        # created resource's distinctive tokens.)
        assert ev is not None, f"a receipt should still be persisted (as CLAIMED); store={store}"
        assert ev.get("confirmed") is not True, (
            "SPURIOUS! a readback of a DIFFERENT resource still machine-verified the "
            f"receipt — the token match is not load-bearing; ev={ev}")
        assert ev.get("method") == "api_readback", (
            f"the receipt still routed through the readback seam (just unconfirmed); ev={ev}")
    finally:
        cleanup()
