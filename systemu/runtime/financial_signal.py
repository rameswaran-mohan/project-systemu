"""S3 / R-A7 — the deterministic financial-signal detector + money-move
disjunction (spec UNIFIED-v2 §5.7 param-layer rule).

Two pure functions, no I/O, no LLM, import-cycle-free (imports only
:mod:`systemu.runtime.effect_tags` for :class:`EffectTag`):

  * :func:`is_financial_signal` — a whole-token scan of the objective text for a
    money verb, plus an amount-shaped param field / currency-symbol number. It is
    a SIGNAL (advisory), positive-only and escalate-never-clear: a positive
    result raises the effect class toward money-move; it never *clears* one.

  * :func:`money_move_net_applies` — the BLOCKER-3 disjunction:

        MONEY_MOVE in effect_tags
          OR ((requires_external OR the effect is UNKNOWN) AND is_financial_signal)

    An UNKNOWN-effect objective carrying a financial signal MUST get the
    money-move net — a false-negative here routes an unclassified money-move to
    the WEAK local-verifier path (double-submit hazard). We therefore bias toward
    classifying-as-financial when money tokens are present but the effect class is
    ambiguous.

Design bias (BLOCKER-3): when ambiguous, prefer the money-move classification.
Whole-token matching (word boundaries) avoids substring false-hits — "charger"
must not trip "charge", "transfer" of a FILE (no amount) is not financial.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from systemu.runtime.effect_tags import EffectTag, coerce

# ── money verbs / nouns (whole-token) ───────────────────────────────────────
# Deliberately narrow + high-precision. "balance"/"account" are NOT here (a read
# of a balance page is not a money move). "transfer" is included but is NOT
# sufficient alone — see the file-transfer guard below.
_MONEY_TOKENS = frozenset({
    "charge", "charged",
    "wire", "wired",
    "pay", "paid", "payment", "payments",
    "remit", "remittance",
    "invoice", "invoiced",
    "checkout",
    "purchase", "purchased",
    "refund", "refunded",
    # BLOCKER-3 (a): everyday money verbs/nouns the narrow allowlist omitted. A
    # plain "settle the outstanding balance" (no currency/amount) IS a money move.
    "settle", "settled", "settles", "settling", "settlement",
    "withdraw", "withdrew", "withdrawn", "withdrawal",
    "disburse", "disbursed", "disbursement",
    "debit", "debited",
    "deposit", "deposited",
})

# "transfer" is money-ish ONLY with an amount/currency OR a money-context noun in
# play — a file/data transfer is not financial. Treated separately from
# _MONEY_TOKENS.
_TRANSFER_TOKENS = frozenset({"transfer", "transfers", "transferred", "transferring"})
# money-context nouns that qualify a bare "transfer" (or otherwise signal a money
# move) even without a numeric amount: "transfer FUNDS" / "move MONEY".
_MONEY_CONTEXT_NOUNS = frozenset({"funds", "money", "cash", "payment", "payments", "dollars"})

# amount-shaped param KEYS (whole-key or suffix match, case-insensitive)
_AMOUNT_KEY_HINTS = ("amount", "total", "price", "cost", "sum", "fee", "balance_due")

# currency-symbol-prefixed or -suffixed number, e.g. "$500", "€120", "500 USD"
_CURRENCY_RE = re.compile(
    r"(?:[$€£¥₹]\s?\d[\d.,]*)"                         # $500 / €120
    r"|(?:\b\d[\d.,]*\s?(?:usd|eur|gbp|jpy|inr)\b)",   # 500 USD
    re.IGNORECASE,
)

# whole-token splitter (letters/digits runs); lowercased for matching
_TOKEN_RE = re.compile(r"[a-zA-Z]+")


def _tokens(text: str) -> "set[str]":
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _amount_shaped_value(v: Any) -> bool:
    """True if a param VALUE looks like a monetary amount (a number, or a numeric
    string possibly with a currency symbol)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return False
        if _CURRENCY_RE.search(s):
            return True
        # a bare numeric string like "19.99" / "500"
        if re.fullmatch(r"\d[\d.,]*", s):
            return True
    return False


def _params_have_amount(params: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(params, Mapping):
        return False
    for k, v in params.items():
        key = str(k).lower()
        if any(hint in key for hint in _AMOUNT_KEY_HINTS) and _amount_shaped_value(v):
            return True
    return False


def is_financial_signal(text: Optional[str], params: Optional[Mapping[str, Any]]) -> bool:
    """Deterministic, positive-only financial-signal detector.

    Returns True when the objective carries a money-move signal:
      * a whole-token money verb/noun in ``text`` (charge/wire/pay/remit/invoice/
        checkout/purchase/refund), OR
      * a bare "transfer" verb ACCOMPANIED by an amount/currency (a file transfer
        alone is not financial), OR
      * a currency-symbol number anywhere in ``text``, OR
      * an amount-shaped param field (key like amount/total/price + numeric value).

    Never raises; None/odd inputs ⇒ False. Escalate-only: a True result should
    only ever RAISE the effect classification, never clear it.
    """
    try:
        text = text or ""
        toks = _tokens(text)
        has_currency = bool(_CURRENCY_RE.search(text))
        has_amount_param = _params_have_amount(params)
        has_amount = has_currency or has_amount_param

        # a direct money verb/noun is sufficient on its own
        if toks & _MONEY_TOKENS:
            return True
        # "transfer" only counts WITH an amount/currency OR a money-context noun
        # ("transfer funds") — a bare file/data transfer is not financial.
        if (toks & _TRANSFER_TOKENS) and (has_amount or (toks & _MONEY_CONTEXT_NOUNS)):
            return True
        # a currency-symbol number, or an amount-shaped param, is sufficient alone
        if has_amount:
            return True
        return False
    except Exception:
        # a detector must never break the caller; a failure is not a clear.
        return False


def _is_unknown_effect(effect_tags: Optional[Iterable[Any]]) -> bool:
    """True if the effect class is UNKNOWN: an empty/None set (nothing classified)
    or a set whose only meaningful member coerces to UNKNOWN. Any KNOWN non-unknown
    tag present ⇒ not UNKNOWN-effect."""
    if not effect_tags:
        return True
    values = {coerce(t) for t in effect_tags}
    non_unknown = values - {EffectTag.UNKNOWN.value}
    return len(non_unknown) == 0


def _has_money_move_tag(effect_tags: Optional[Iterable[Any]]) -> bool:
    if not effect_tags:
        return False
    return any(coerce(t) == EffectTag.MONEY_MOVE.value for t in effect_tags)


def money_move_net_applies(
    effect_tags: Optional[Iterable[Any]],
    text: Optional[str],
    params: Optional[Mapping[str, Any]],
    requires_external: bool = False,
) -> bool:
    """The money-move disjunction (BLOCKER-3).

    Returns True iff:
        MONEY_MOVE in effect_tags
          OR ((requires_external OR the effect is UNKNOWN) AND is_financial_signal)
          OR (requires_external AND the effect is UNKNOWN/unclassified)   ← fallback

    ``effect_tags`` accepts EffectTag members or plain strings (the store
    round-trips tags to strings). Never raises.

    The third disjunct is the FAIL-CLOSED broad-net fallback (§5.8, "dangerous-
    until-proven for the credit net"): a ``requires_external`` objective whose
    effect is UNKNOWN/unclassified defaults to money-move-gated even with NO
    detected financial signal — so an advisory strategy can NEVER confirm an
    unclassified external effect; it fails closed onto the strong hardened channel
    (``_api_readback`` / ``_email_confirm``). This fires ONLY for UNKNOWN effects:
    a KNOWN non-money ``NET_MUTATE`` stays advisory-confirmable (not over-gated).
    """
    try:
        if _has_money_move_tag(effect_tags):
            return True
        unknown_effect = _is_unknown_effect(effect_tags)
        gate = bool(requires_external) or unknown_effect
        if gate and is_financial_signal(text, params):
            return True
        # FAIL-CLOSED fallback: an external objective with an UNKNOWN/unclassified
        # effect is money-move-gated even without a detected financial signal.
        if bool(requires_external) and unknown_effect:
            return True
        return False
    except Exception:
        # fail toward NOT applying the net only on an internal error; callers that
        # need fail-closed behaviour gate credit elsewhere. This function is a
        # classifier, not the credit gate.
        return False
