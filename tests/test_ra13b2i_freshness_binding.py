"""R-A13b-2i FIX 3 (C3) — a money-move's freshness proof must be BOUND to the exact
resource verify credits (the envelope readback_url/expected_tokens), not merely to
whatever the pre-submit probe happened to read.

The exploit: the pre-submit probe reads ``decision.parameters['external']`` (URL_A +
tokens_A) and returns only ``{presubmit_tokens, pre_submit_absent}`` — it DISCARDS
which url/tokens it probed. Verify then credits off the RESULT envelope's
``readback_url``/``expected_tokens`` (URL_B/tokens_B, from ``_external_from_result``).
Nothing enforced URL_A==URL_B. So an attacker probes a benign ABSENT URL_A
(pre_submit_absent=True) while the result envelope points at a PRE-EXISTING receipt
URL_B carrying a stale token: host-pin (envelope url vs envelope submit_host) passes,
the stale token matches, and ``_tokens_are_fresh`` sees pre_submit_absent=True (from
URL_A) ⇒ a replay would-CREDIT a money-move.

Driven through the REAL credit seam (the SHADOW would-credit/would-park meter →
``_run_external_verification``), NOT synthetic dicts.
"""
from __future__ import annotations

from test_s3_credit_wiring import _drive_live_credit, _CreateOnceReadbackClient
from test_ra13b1_shadow_meter import _shadow_obj, _stamp_shadow_on_resolve


class _UrlAwareStaleClient:
    """Models the C3 replay: the probed create-once target URL_A is ABSENT, but the
    result envelope points at a DIFFERENT, PRE-EXISTING receipt URL_B that already
    holds a stale token — independent of submit state. The pre-submit probe reads
    URL_A (absent); post-submit verify reads URL_B (stale-present)."""

    def __init__(self, *, url_b, stale_token):
        self._url_b = url_b
        self._stale = stale_token
        self.urls = []

    def readback(self, url):
        self.urls.append(url)
        if url == self._url_b:
            return {"observed_tokens": [self._stale],
                    "response_body": "pre-existing receipt: " + self._stale}
        return {"observed_tokens": [], "response_body": "not found (pre-submit)"}


# ── C3 EXPLOIT: probe URL_A (absent) but credit URL_B (stale) ⇒ would-PARK ──
def test_money_move_freshness_url_mismatch_would_park(tmp_path, monkeypatch):
    """The freshness proof was established at URL_A but the credit is at URL_B — the
    proof does not vouch for URL_B's stale token. Pre-fix this recorded
    would_credit=True (the replay exploit); the fix binds freshness to the credited
    envelope ⇒ mismatch ⇒ would-PARK."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "STALE-RECEIPT-1"
    url_a = "https://api.example.com/rows/benign"           # probed (absent)
    url_b = "https://api.example.com/receipts/preexisting"  # credited (stale-present)
    client = _UrlAwareStaleClient(url_b=url_b, stale_token=token)
    directive = {"readback_url": url_a, "expected_tokens": [token],
                 "submit_host": "api.example.com"}
    tool_parsed = {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": url_b, "submit_host": "api.example.com"}}
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client,
        decision_params={"external": directive})

    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("would_park") is True and ev.get("would_credit") is False, (
        "a freshness proof bound to a DIFFERENT url than the credited envelope must "
        f"NOT confirm a money-move ⇒ would-PARK; ev={ev}")
    # prove the exploit's shape actually ran: the probe hit URL_A, verify hit URL_B.
    assert url_a in client.urls and url_b in client.urls, client.urls


# ── C3 no-regression: probe url == envelope url + create-once ⇒ would-CREDIT ──
def test_money_move_freshness_same_url_would_credit(tmp_path, monkeypatch):
    """The legit mirror contract: the pre-submit directive and the result envelope
    name the SAME url + tokens, and the create-once resource is absent pre-submit →
    present post-submit. The freshness BINDS to the credited resource ⇒ would-CREDIT."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-bound-fresh-1"
    url = "https://api.example.com/rows/1"
    client = _CreateOnceReadbackClient(echo_tokens=[token])   # absent → present
    directive = {"readback_url": url, "expected_tokens": [token],
                 "submit_host": "api.example.com"}
    tool_parsed = {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": url, "submit_host": "api.example.com"}}
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client,
        decision_params={"external": directive})

    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("would_credit") is True and ev.get("would_park") is False, (
        f"the legit matching-url create-once path must still would-CREDIT; ev={ev}")
    assert client.urls == [url, url], client.urls
