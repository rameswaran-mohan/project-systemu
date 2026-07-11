"""R-A13b-2ii-a — the curated effect-CLASSIFICATION signal tables + lookups.

`effect_signals.py` is the pure-data + lookup layer (modeled on `reference_synonyms`)
the `_EffectVisitor` consults to emit MONEY_MOVE / SEND_MESSAGE — the semantic classes
the structural AST scan STRUCTURALLY cannot reach (which SDK / host / endpoint). Three
curated tables per class (import roots, URL hosts host-suffix, attr/method chains) plus
`any_money_move_signal(source)` — the MONOTONIC money-move floor the backfill re-derives.

Load-bearing property: money-move is fail-closed. A money signal on any of the three axes
must resolve to `money_move` (never dropped), and the lookups never raise.
"""
from __future__ import annotations

import pytest

from systemu.runtime import effect_signals as es
from systemu.runtime.effect_tags import EffectTag, classify_source

_MONEY = EffectTag.MONEY_MOVE.value
_SEND = EffectTag.SEND_MESSAGE.value


# ── import/module roots ──────────────────────────────────────────────────────

@pytest.mark.parametrize("root", ["stripe", "paypalrestsdk", "square", "braintree", "razorpay"])
def test_class_for_import_money(root):
    assert es.class_for_import(root) == _MONEY


@pytest.mark.parametrize(
    "root", ["twilio", "sendgrid", "slack_sdk", "slack", "discord", "telegram", "smtplib", "mailgun"])
def test_class_for_import_send(root):
    assert es.class_for_import(root) == _SEND


@pytest.mark.parametrize("root", ["requests", "os", "httpx", "shutil", "json", "", None])
def test_class_for_import_benign_is_none(root):
    assert es.class_for_import(root) is None


# ── URL hosts (host-suffix match) ────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "api.stripe.com",
    "https://api.stripe.com/v1/charges",
    "https://api.paypal.com/v2/checkout/orders",
    "https://api.squareup.com/v2/payments",
    "https://eu.api.stripe.com/v1/charges",   # sub-domain still suffix-matches
])
def test_class_for_host_money(url):
    assert es.class_for_host(url) == _MONEY


@pytest.mark.parametrize("url", [
    "https://api.twilio.com/2010-04-01/Messages.json",
    "https://api.sendgrid.com/v3/mail/send",
    "https://hooks.slack.com/services/XXX",
    "https://api.telegram.org/bot123/sendMessage",
    "https://api.mailgun.net/v3/x/messages",
    "email-smtp.us-east-1.amazonaws.com",     # SES SMTP wildcard region
    "https://email-smtp.eu-west-2.amazonaws.com/",
])
def test_class_for_host_send(url):
    assert es.class_for_host(url) == _SEND


@pytest.mark.parametrize("url", [
    "https://example.com/x",
    "http://api/x",
    "https://notapi.stripe.com.evil.com/x",    # dot-guard: not a real stripe suffix
    "w",                                        # a bare open() mode string
    "",
    None,
])
def test_class_for_host_benign_is_none(url):
    assert es.class_for_host(url) is None


# ── attr/method chains ───────────────────────────────────────────────────────

@pytest.mark.parametrize("chain", [
    "PaymentIntent.create", "Charge.create", "Transfer.create", "Payout.create"])
def test_class_for_attrchain_money(chain):
    assert es.class_for_attrchain(chain) == _MONEY


@pytest.mark.parametrize("chain", [
    "messages.create", "chat_postMessage", "sendmail", "send_message"])
def test_class_for_attrchain_send(chain):
    assert es.class_for_attrchain(chain) == _SEND


@pytest.mark.parametrize("chain", ["create", "run", "get", "post", "", None])
def test_class_for_attrchain_benign_is_none(chain):
    # a bare generic ".create"/".get" must NOT hit — only the qualified chain does.
    assert es.class_for_attrchain(chain) is None


# ── any_money_move_signal (the MONOTONIC backfill floor) ─────────────────────

def test_any_money_move_signal_true_import():
    assert es.any_money_move_signal(
        "import stripe\ndef run(**k):\n    return stripe.PaymentIntent.create(**k)") is True


def test_any_money_move_signal_true_host():
    assert es.any_money_move_signal(
        "import requests\nrequests.post('https://api.stripe.com/v1/charges', json={})") is True


def test_any_money_move_signal_false_plain_post():
    assert es.any_money_move_signal(
        "import requests\nrequests.post('https://example.com/x', json={})") is False


def test_any_money_move_signal_false_send_only():
    # a send-message tool is NOT a money-move (the floor is money-only).
    assert es.any_money_move_signal(
        "import smtplib\ns.sendmail('a', 'b', 'body')") is False


def test_any_money_move_signal_never_raises_on_bad_source():
    assert es.any_money_move_signal("def run(:\n not python") is False
    assert es.any_money_move_signal("") is False
    assert es.any_money_move_signal(None) is False


# ═════════════════════════════════════════════════════════════════════════════
#  R-A13b-2ii hardening — BROADENED curated money/send tables (open-world
#  residual (1) mitigation). New payment/transfer/crypto providers + send hosts.
# ═════════════════════════════════════════════════════════════════════════════

# ── broadened money import roots (distinctive, payments-only) ─────────────────

@pytest.mark.parametrize("root", [
    "plaid", "dwolla", "dwollav2", "gocardless", "gocardless_pro", "mollie",
    "adyen", "Adyen", "authorizenet", "coinbase", "coinbase_commerce"])
def test_class_for_import_money_broadened(root):
    assert es.class_for_import(root) == _MONEY


@pytest.mark.parametrize("root", [
    "plaid.api", "coinbase.wallet.client", "gocardless_pro.services"])
def test_class_for_import_money_broadened_dotted_root(root):
    # the ROOT (first dotted component) resolves; sub-modules ride the root.
    assert es.class_for_import(root) == _MONEY


@pytest.mark.parametrize("root", [
    # ambiguous/generic names DELIBERATELY kept out of the roots (covered by HOSTS):
    "wise", "transferwise", "checkout", "worldpay",
    # unrelated packages whose name merely contains a provider substring:
    "plaidml", "wiser", "coinbase_pro_unofficial_lookalike", "adyenish"])
def test_class_for_import_broadened_ambiguous_or_lookalike_is_none(root):
    assert es.class_for_import(root) is None


# ── broadened money hosts (dot-guarded exact/suffix) ─────────────────────────

@pytest.mark.parametrize("url", [
    "https://production.plaid.com/transfer/create",  # *.plaid.com suffix
    "https://sandbox.plaid.com/transfer/create",
    "plaid.com",                                     # bare brand domain
    "https://api.dwolla.com/transfers",
    "https://api-sandbox.dwolla.com/transfers",
    "https://api.wise.com/v1/transfers",
    "https://api.transferwise.com/v1/transfers",
    "https://api.gocardless.com/payments",
    "https://api.mollie.com/v2/payments",
    "https://checkout-test.adyen.com/v70/payments",  # *.adyen.com suffix
    "https://pal-test.adyen.com/pal/servlet/Payment",
    "https://api.authorize.net/xml/v1/request.api",
    "https://apitest.authorize.net/xml/v1/request.api",
    "https://api.checkout.com/payments",
    "https://api.sandbox.checkout.com/payments",
    "https://access.worldpay.com/payments",
    "https://api.worldpay.com/v1/payments",
    "https://api.coinbase.com/v2/accounts/x/transactions",
    "https://api.commerce.coinbase.com/charges",
])
def test_class_for_host_money_broadened(url):
    assert es.class_for_host(url) == _MONEY


@pytest.mark.parametrize("url", [
    "https://notplaid.com/x",              # dot-guard: not a *.plaid.com host
    "https://plaid.com.evil.com/x",        # dot-guard: brand as a left label
    "https://checkout.example.com/x",      # unrelated 'checkout' host
    "https://worldpay.evil.com/x",
    "https://mycoinbase.com/x",            # not *.coinbase.com
])
def test_class_for_host_money_broadened_dotguard_is_none(url):
    assert es.class_for_host(url) is None


# ── broadened send hosts (dedicated transactional-message APIs, send-only) ───

@pytest.mark.parametrize("url", [
    "https://api.postmarkapp.com/email",
    "https://api.resend.com/emails",
])
def test_class_for_host_send_broadened(url):
    assert es.class_for_host(url) == _SEND


# ── broadened money attr chain (Plaid transfer_create) ───────────────────────

def test_class_for_attrchain_transfer_create_money():
    assert es.class_for_attrchain("transfer_create") == _MONEY


# ── the MONOTONIC floor sees each new money provider's source ────────────────

_MONEY_PROVIDER_SOURCES = {
    "plaid_import": "import plaid\ndef run(**k):\n    return client.transfer_create(**k)",
    "plaid_host":   "import requests\nrequests.post('https://production.plaid.com/transfer/create', json={})",
    "dwolla":       "import dwollav2\nc = dwollav2.Client(id='a', secret='b')",
    "gocardless":   "import gocardless_pro\nc = gocardless_pro.Client(access_token='x')",
    "mollie":       "import requests\nrequests.post('https://api.mollie.com/v2/payments', json={})",
    "adyen":        "import Adyen\nAdyen.Adyen()",
    "authorizenet": "import authorizenet\n",
    "coinbase":     "import requests\nrequests.post('https://api.coinbase.com/v2/accounts/x/transactions', json={})",
    "wise_host":    "import requests\nrequests.post('https://api.wise.com/v1/transfers', json={})",
    "worldpay_host":"import requests\nrequests.post('https://access.worldpay.com/payments', json={})",
    "checkout_host":"import requests\nrequests.post('https://api.checkout.com/payments', json={})",
}


@pytest.mark.parametrize("name,src", sorted(_MONEY_PROVIDER_SOURCES.items()))
def test_any_money_move_signal_true_for_each_broadened_provider(name, src):
    assert es.any_money_move_signal(src) is True, name


def test_any_money_move_signal_false_for_benign_lookalike_package():
    # a non-payment package that merely CONTAINS a provider substring must not hit.
    assert es.any_money_move_signal("import plaidml\nplaidml.run()") is False
    assert es.any_money_move_signal("import wiser\nwiser.advise()") is False


# ── through classify_source (the finder's examples: plaid + coinbase) ────────

def test_classify_source_plaid_is_money_move():
    tags = {t.value for t in classify_source(
        "import plaid\ndef run(**k):\n    return client.transfer_create(access_token=k['t'])")}
    assert _MONEY in tags, tags


def test_classify_source_coinbase_host_is_money_move():
    # the finder's exact scenario: money via an (uncurated-transport) requests.post to
    # a CURATED money host — classify_source stamps money_move (union w/ net_mutate).
    tags = {t.value for t in classify_source(
        "import requests\nrequests.post('https://api.coinbase.com/v2/accounts/x/transactions', json={})")}
    assert _MONEY in tags and "net_mutate" in tags, tags


def test_classify_source_benign_lookalike_not_money_via_broadened_table():
    # a plain search/get tool that references a non-payment 'checkout.example.com'
    # must NOT be money-stamped by the broadened table.
    tags = {t.value for t in classify_source(
        "import requests\nrequests.get('https://checkout.example.com/status')")}
    assert _MONEY not in tags and _SEND not in tags, tags
