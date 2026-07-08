"""S3 wave1 Step 1 — the financial-signal detector + money-move disjunction.

Two pure, deterministic functions (§5.7 param-layer rule: positive-only,
escalate-never-clear):

  * ``is_financial_signal(text, params)`` — a whole-token scan for money verbs
    (charge/wire/pay/payment/transfer/remit/invoice/checkout) PLUS an amount-
    shaped field/number-with-currency-symbol in params or text.

  * ``money_move_net_applies(effect_tags, text, params, requires_external)`` —
    the disjunction that decides whether the money-move net catches an objective:
        MONEY_MOVE in effect_tags
          OR ((requires_external OR UNKNOWN-effect) AND is_financial_signal)

BLOCKER-3: an UNKNOWN-effect objective that carries a financial signal MUST get
the money-move net (a false-negative here routes an unclassified money-move to
the WEAK local-verifier path — a double-submit hazard). This file tests that arm
explicitly, both positive (UNKNOWN ∩ financial ⇒ True) and negative
(UNKNOWN ∩ no-financial ⇒ False).
"""
from __future__ import annotations

import pytest

from systemu.runtime.effect_tags import EffectTag
from systemu.runtime.financial_signal import is_financial_signal, money_move_net_applies


# ── is_financial_signal ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,params",
    [
        ("wire $500 to X", {}),
        ("charge the card", {}),
        ("pay invoice 42", {}),
        ("transfer funds", {}),
        ("please remit payment now", {}),
        ("proceed to checkout", {}),
        # an amount-shaped param field (no money verb needed on its own? — here we
        # pair it with a neutral text but a clearly financial param key+value)
        ("do the thing", {"amount": 500}),
        ("do the thing", {"total": "19.99"}),
        ("do the thing", {"price": 42}),
        # currency-symbol number in text
        ("send them €120", {}),
    ],
)
def test_positive_financial_signals(text, params):
    assert is_financial_signal(text, params) is True


@pytest.mark.parametrize(
    "text",
    [
        # BLOCKER-3 gap: everyday money VERBS the narrow allowlist missed. Each of
        # these is a money-move with NO currency symbol / amount — the classifier
        # must still recognise the verb alone.
        "settle the outstanding balance",
        "settled the account",
        "please settle this",
        "settling the invoice",
        "the settlement completed",
        "withdraw from the account",
        "he withdrew the money",
        "funds were withdrawn",
        "process the withdrawal",
        "disburse the grant",
        "disbursed the funds",
        "start the disbursement",
        "debit the account",
        "the amount was debited",
        "deposit into the account",
        "deposited the cheque",
    ],
)
def test_money_verbs_extended_settle_withdraw_disburse_debit_deposit(text):
    """The narrow allowlist omitted everyday money verbs — a plain 'settle the
    outstanding balance' (no currency/amount) previously read as NON-financial,
    which let an advisory web_assertion confirm the money-move. These must now be
    financial signals on the verb alone."""
    assert is_financial_signal(text, {}) is True


@pytest.mark.parametrize(
    "text,params",
    [
        ("send an email", {}),
        ("read the balance page", {}),          # "balance" is not a money verb; a READ
        ("transfer the file to the server", {}),  # whole-token "transfer" of a FILE ≠ financial
        ("summarize the report", {}),
        ("open the invoicing docs", {}),        # "invoicing" ≠ whole-token "invoice"? see note
        ("", {}),
        ("charger la batterie", {}),            # "charger" must not substring-hit "charge"
    ],
)
def test_negative_non_financial(text, params):
    assert is_financial_signal(text, params) is False


def test_whole_token_avoids_substring_false_hits():
    # "transfer" as a whole token appears — but the OBJECT is a file, and there is
    # no amount/currency — so a bare non-financial "transfer" must NOT trip alone.
    assert is_financial_signal("transfer the document", {}) is False
    # but "transfer $50" (verb + amount) IS financial
    assert is_financial_signal("transfer $50", {}) is True
    # substring guard: "prepayment" / "charger" must not hit "pay"/"charge"
    assert is_financial_signal("recharger le compte", {}) is False


def test_amount_field_alone_is_financial():
    # An amount-shaped param key is a financial signal even with neutral text —
    # bias toward classifying-as-financial (BLOCKER-3 conservatism).
    assert is_financial_signal("neutral text", {"amount": 10}) is True
    assert is_financial_signal("neutral text", {"grand_total": "9.99"}) is True


def test_none_and_bad_inputs_do_not_raise():
    assert is_financial_signal(None, None) is False
    assert is_financial_signal("", {}) is False
    assert is_financial_signal("pay now", None) is True


# ── money_move_net_applies (the disjunction) ─────────────────────────────────

def test_money_move_tag_always_applies():
    # arm 1: an explicit MONEY_MOVE effect tag ⇒ net applies regardless of signal
    assert money_move_net_applies(
        {EffectTag.MONEY_MOVE}, "neutral", {}, requires_external=False
    ) is True
    # tag can be a plain string too (store round-trips to strings)
    assert money_move_net_applies(
        {"money_move"}, "neutral", {}, requires_external=False
    ) is True


def test_requires_external_and_financial_applies():
    # arm 2a: requires_external + a financial signal ⇒ net applies
    assert money_move_net_applies(
        {EffectTag.NET_MUTATE}, "pay invoice 42", {}, requires_external=True
    ) is True


def test_unknown_effect_and_financial_applies_BLOCKER3():
    # arm 2b (BLOCKER-3): an UNKNOWN-effect objective WITH a financial signal ⇒
    # gets the money-move net. This is the load-bearing false-negative guard.
    assert money_move_net_applies(
        {EffectTag.UNKNOWN}, "wire $500 to acme", {}, requires_external=False
    ) is True
    # UNKNOWN via an empty/unclassified effect set also counts as UNKNOWN-effect
    assert money_move_net_applies(
        set(), "charge the card", {}, requires_external=False
    ) is True


def test_unknown_effect_without_financial_does_not_apply_BLOCKER3():
    # BLOCKER-3 negative arm: UNKNOWN-effect but NO financial signal ⇒ net does
    # NOT apply (we don't over-trigger on every unclassified objective).
    assert money_move_net_applies(
        {EffectTag.UNKNOWN}, "read the balance page", {}, requires_external=False
    ) is False
    assert money_move_net_applies(
        set(), "send an email", {}, requires_external=False
    ) is False


def test_known_nonmoney_effect_without_external_or_financial_does_not_apply():
    # a plainly-classified non-money effect, no external requirement, no signal ⇒
    # the money-move net does NOT apply.
    assert money_move_net_applies(
        {EffectTag.LOCAL_WRITE}, "save the file", {}, requires_external=False
    ) is False


def test_requires_external_without_financial_does_not_apply():
    # requires_external alone (no financial signal, known non-money effect) ⇒ the
    # MONEY-MOVE net specifically does not apply (external verification still may,
    # but that's a different gate — this function is the money-move disjunction).
    assert money_move_net_applies(
        {EffectTag.NET_MUTATE}, "post a comment", {}, requires_external=True
    ) is False


# ── the FAIL-CLOSED broad-net fallback (dangerous-until-proven, §5.8 ~L509) ──

def test_requires_external_unknown_effect_fails_closed_to_money_net():
    """The robust fallback: a requires_external objective whose effect is UNKNOWN/
    unclassified defaults to money-move-gated even with NO detected financial
    signal — so an advisory strategy can NEVER confirm an unclassified external
    effect (it fails closed onto the strong hardened channel)."""
    # UNKNOWN effect + requires_external + NO financial signal ⇒ still gated.
    assert money_move_net_applies(
        {EffectTag.UNKNOWN}, "do the external thing", {}, requires_external=True
    ) is True
    # an empty/unclassified effect set is UNKNOWN too.
    assert money_move_net_applies(
        set(), "reconcile the ledger", {}, requires_external=True
    ) is True


def test_known_nonmoney_net_mutate_external_is_NOT_over_gated():
    """The fallback fires ONLY for UNKNOWN/unclassified effects. A KNOWN non-money
    NET_MUTATE that requires_external but carries no financial signal stays OUT of
    the money net (advisory-confirmable per spec) — we must not over-gate it."""
    assert money_move_net_applies(
        {EffectTag.NET_MUTATE}, "post the update", {}, requires_external=True
    ) is False
    assert money_move_net_applies(
        {EffectTag.SEND_MESSAGE}, "send the notification", {}, requires_external=True
    ) is False


def test_unknown_effect_fallback_needs_requires_external():
    """The UNKNOWN-effect fallback is anchored on requires_external — an UNKNOWN
    effect with NO external requirement AND no financial signal is NOT swept in
    (we don't gate every unclassified local objective)."""
    assert money_move_net_applies(
        {EffectTag.UNKNOWN}, "do the local thing", {}, requires_external=False
    ) is False
