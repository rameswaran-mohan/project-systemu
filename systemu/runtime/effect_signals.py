# systemu/runtime/effect_signals.py
"""R-A13b-2ii §5.7 — curated import/host/attr → effect-CLASS signal tables.

The `_EffectVisitor` (`effect_tags.py`) is a STRUCTURAL AST scan: it can see a POST but
not WHICH endpoint, so it structurally cannot emit the SEMANTIC classes MONEY_MOVE /
SEND_MESSAGE / OAUTH_CALL (Stripe/Twilio/Slack/Google-OAuth all look like generic POSTs).
This module is the curated semantic map it consults — pure data + a few pure lookups,
exactly as `reference_synonyms` split the phrase→extension table out of the classifier.

R-A13b-2ii-b added OAUTH_CALL signals (CONSERVATIVE — prefer HOSTS over generic method
names; oauth is NOT money, so `any_money_move_signal` is unaffected).

Cycle-free: imports ONLY `ast` (stdlib) and the `EffectTag` enum. `effect_tags` must
import THIS module LAZILY (inside `classify_source`) so there is no import cycle — see
the cycle note in the module docstring of `effect_tags.py`.

Money-move is FAIL-CLOSED (load-bearing, residual-3): a money signal on ANY axis resolves
to `money_move`, and `any_money_move_signal` (the backfill floor) re-derives it
independently of the structural scan. All lookups are defensive and never raise.
"""
from __future__ import annotations

import ast
import re
from typing import Optional
from urllib.parse import urlsplit

from systemu.runtime.effect_tags import EffectTag

_MONEY = EffectTag.MONEY_MOVE.value
_SEND = EffectTag.SEND_MESSAGE.value
_OAUTH = EffectTag.OAUTH_CALL.value


# ── import / module roots ────────────────────────────────────────────────────
# Keyed by the ROOT package (first dotted component); e.g. `twilio.rest` → "twilio".
_IMPORT_ROOTS: "dict[str, str]" = {
    # money-move SDKs
    "stripe": _MONEY,
    "paypalrestsdk": _MONEY,
    "square": _MONEY,
    "braintree": _MONEY,
    "razorpay": _MONEY,
    # money-move SDKs — broadened (R-A13b-2ii hardening). Each root below is a
    # DISTINCTIVE, payments/transfer/crypto-only package name (no common non-payment
    # package shares it). Ambiguous/generic roots (wise, transferwise, checkout,
    # worldpay) are deliberately NOT here — they are covered by HOSTS only, since
    # over-classifying via a shared root would be a false money-stamp.
    "plaid": _MONEY,              # Plaid — bank transfers / ACH (client.transfer_create)
    "dwolla": _MONEY,             # Dwolla ACH — legacy package root
    "dwollav2": _MONEY,           # Dwolla ACH — current SDK (import dwollav2)
    "gocardless": _MONEY,         # GoCardless bank debit — legacy root
    "gocardless_pro": _MONEY,     # GoCardless — current SDK (import gocardless_pro)
    "mollie": _MONEY,             # Mollie — EU payments
    "adyen": _MONEY,              # Adyen — lower-case import guard
    "Adyen": _MONEY,             # Adyen — official SDK root is capitalized (import Adyen)
    "authorizenet": _MONEY,       # Authorize.Net SDK
    "coinbase": _MONEY,           # Coinbase — crypto == money-move
    "coinbase_commerce": _MONEY,  # Coinbase Commerce checkout SDK
    # send-message SDKs / stdlib
    "twilio": _SEND,
    "sendgrid": _SEND,
    "slack_sdk": _SEND,
    "slack": _SEND,
    "discord": _SEND,
    "telegram": _SEND,
    "smtplib": _SEND,
    "mailgun": _SEND,
    # oauth-call SDKs (R-A13b-2ii-b). Each root below is a DISTINCTIVE, OAuth-only
    # package name (no common non-oauth package shares it). CONSERVATIVE — oauth
    # over-classification is lower-stakes than money, but a false hit on a benign
    # tool is still avoided; the bare `oauth` root is deliberately absent (too broad).
    "requests_oauthlib": _OAUTH,   # OAuth1Session / OAuth2Session
    "oauthlib": _OAUTH,            # the lower-level OAuth library
    "authlib": _OAUTH,            # Authlib client/server
    "google_auth_oauthlib": _OAUTH,  # google-auth-oauthlib flow
    "msal": _OAUTH,               # Microsoft Authentication Library
}

# ── URL hosts (exact or host-suffix match, dot-guarded) ──────────────────────
_HOSTS: "dict[str, str]" = {
    "api.stripe.com": _MONEY,
    "api.paypal.com": _MONEY,
    "api.squareup.com": _MONEY,
    # money-move hosts — broadened (R-A13b-2ii hardening). Dot-guarded exact/suffix
    # match: an entry X hits `host == X` or `host.endswith("." + X)`, so a bare
    # brand-domain entry (`plaid.com`) covers all of its subdomains while
    # `notplaid.com` / `plaid.com.evil.com` cannot match. All are payments/transfer/
    # crypto-only domains — over-classifying toward money is the SAFE direction.
    "plaid.com": _MONEY,              # *.plaid.com (production/sandbox/development)
    "api.dwolla.com": _MONEY,         # Dwolla ACH — production
    "api-sandbox.dwolla.com": _MONEY,  # Dwolla ACH — sandbox (separate host)
    "api.wise.com": _MONEY,           # Wise money transfer (root ambiguous → host only)
    "api.transferwise.com": _MONEY,   # Wise — legacy host
    "api.gocardless.com": _MONEY,     # GoCardless bank debit
    "api.mollie.com": _MONEY,         # Mollie payments
    "adyen.com": _MONEY,              # *.adyen.com (checkout-test / pal / classic hosts)
    "api.authorize.net": _MONEY,      # Authorize.Net — production
    "apitest.authorize.net": _MONEY,  # Authorize.Net — sandbox
    "api.checkout.com": _MONEY,       # Checkout.com (root "checkout" too generic → host)
    "api.sandbox.checkout.com": _MONEY,  # Checkout.com — sandbox
    "access.worldpay.com": _MONEY,    # Worldpay Access (no stable SDK root → host only)
    "api.worldpay.com": _MONEY,       # Worldpay — legacy host
    "api.coinbase.com": _MONEY,       # Coinbase — crypto == money-move
    "api.commerce.coinbase.com": _MONEY,  # Coinbase Commerce
    # send-message hosts
    "api.twilio.com": _SEND,
    "api.sendgrid.com": _SEND,
    "hooks.slack.com": _SEND,
    "api.telegram.org": _SEND,
    "api.mailgun.net": _SEND,
    # send-message hosts — broadened (dedicated transactional-message APIs, send-only)
    "api.postmarkapp.com": _SEND,     # Postmark transactional email
    "api.resend.com": _SEND,          # Resend transactional email
    # oauth-call hosts (R-A13b-2ii-b) — DEDICATED OAuth/identity endpoints only.
    # `github.com` is DELIBERATELY absent: its token endpoint shares the bare
    # `github.com` host, so curating it would false-hit every GitHub API tool.
    "accounts.google.com": _OAUTH,        # Google OAuth consent + token
    "oauth2.googleapis.com": _OAUTH,      # Google OAuth2 token endpoint
    "login.microsoftonline.com": _OAUTH,  # Microsoft identity platform
}

# hosts that need a pattern rather than a fixed suffix (AWS SES SMTP, region-varying).
_HOST_PATTERNS: "list[tuple[re.Pattern[str], str]]" = [
    (re.compile(r"^email-smtp\.[a-z0-9-]+\.amazonaws\.com$", re.IGNORECASE), _SEND),
]

# ── attr / method chains (qualified; a bare generic verb must NOT hit) ───────
_ATTR_CHAINS: "dict[str, str]" = {
    # money-move
    "PaymentIntent.create": _MONEY,
    "Charge.create": _MONEY,
    "Transfer.create": _MONEY,
    "Payout.create": _MONEY,
    # money-move — broadened (R-A13b-2ii). Distinctive snake_case method that a bare
    # generic verb never collides with (Plaid: client.transfer_create(...)).
    "transfer_create": _MONEY,
    # send-message
    "messages.create": _SEND,      # Twilio: client.messages.create(...)
    "chat_postMessage": _SEND,     # Slack SDK: client.chat_postMessage(...)
    "sendmail": _SEND,             # smtplib: server.sendmail(...)
    "send_message": _SEND,
    # oauth-call (R-A13b-2ii-b) — DISTINCTIVE OAuth-flow methods only. A bare generic
    # token verb (`refresh_token`/`get_token`/`token`) is deliberately absent — it is a
    # dict key / attribute everywhere and would over-hit benign code.
    "fetch_token": _OAUTH,             # requests_oauthlib / authlib: session.fetch_token(...)
    "authorize_access_token": _OAUTH,  # authlib: oauth.<provider>.authorize_access_token()
}


def class_for_import(root) -> Optional[str]:
    """The effect class of an import ROOT package (first dotted component), else None.
    Case-sensitive on the package name; defensive on non-str / blank."""
    if not isinstance(root, str) or not root:
        return None
    return _IMPORT_ROOTS.get(root.split(".", 1)[0].strip())


def _host_of(host_or_url) -> str:
    """Extract the bare host from a URL or a raw host string (no scheme / port / creds).
    Never raises; returns "" on anything unusable."""
    if not isinstance(host_or_url, str):
        return ""
    s = host_or_url.strip()
    if not s:
        return ""
    try:
        if "//" in s:
            netloc = urlsplit(s).netloc
        elif "/" in s:
            netloc = s.split("/", 1)[0]
        else:
            netloc = s
        # strip any userinfo@ and :port
        netloc = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
        return netloc.strip().lower()
    except Exception:
        return ""


def class_for_host(host_or_url) -> Optional[str]:
    """The effect class implied by a URL / host string, by exact-or-suffix host match
    (dot-guarded so ``notapi.stripe.com`` never matches ``api.stripe.com``), else None.
    Never raises."""
    host = _host_of(host_or_url)
    if not host:
        return None
    for entry, cls in _HOSTS.items():
        if host == entry or host.endswith("." + entry):
            return cls
    for pat, cls in _HOST_PATTERNS:
        if pat.match(host):
            return cls
    return None


def class_for_attrchain(chain) -> Optional[str]:
    """The effect class of a QUALIFIED attr/method chain (e.g. ``PaymentIntent.create``
    or the single ``chat_postMessage``), by exact match, else None. A bare generic verb
    (``create``/``get``) is deliberately absent so it never over-hits. Never raises."""
    if not isinstance(chain, str) or not chain:
        return None
    return _ATTR_CHAINS.get(chain.strip())


class _MoneyScan(ast.NodeVisitor):
    """A minimal, money-ONLY AST scan for the backfill floor — independent of the
    structural `_EffectVisitor` (defense in depth: the floor is re-derived, not
    inherited). Sets ``self.hit`` True on the FIRST money-move signal on any axis."""

    def __init__(self) -> None:
        self.hit = False

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 (ast API)
        for alias in node.names:
            if class_for_import(alias.name) == _MONEY:
                self.hit = True
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module and class_for_import(node.module) == _MONEY:
            self.hit = True
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if isinstance(node.value, str) and class_for_host(node.value) == _MONEY:
            self.hit = True
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        attr = node.attr
        if class_for_attrchain(attr) == _MONEY:
            self.hit = True
        if isinstance(node.value, ast.Attribute):
            if class_for_attrchain(f"{node.value.attr}.{attr}") == _MONEY:
                self.hit = True
        self.generic_visit(node)


def any_money_move_signal(source) -> bool:
    """True iff the tool SOURCE carries ANY money-move signal (import root, URL host, or
    attr chain). The MONOTONIC backfill floor — a hit always wins, so a money-move tool
    can never be stamped WITHOUT ``money_move``. Never raises (⇒ False on parse error)."""
    if not isinstance(source, str) or not source.strip():
        return False
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    try:
        scan = _MoneyScan()
        scan.visit(tree)
        return bool(scan.hit)
    except Exception:
        return False
