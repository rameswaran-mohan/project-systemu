"""R-P3a — cost visibility (per-run + daily total, NO caps).

The money-honesty invariant, applied to cost:

  * Money is ``Decimal``, never float — no binary-fraction drift on currency.
  * An **unknown model shows tokens + "unknown", never a guessed number**. A
    line whose model is not in the price table is left ``priced=False`` and the
    whole total degrades to ``total_known=False`` (``total=None``). We would
    rather show "— · 38k tok" than fabricate a number.

This module is the ONE new write path R-P3a adds:

  * ``record_usage(run_ref, model, tokens_in, tokens_out)`` — the accountant.
    Its SOLE caller is the LLM router, hooked at the per-call token-capture
    point (native + OpenRouter paths), reading the ambient ``execution_id`` so a
    call attributes to its owning run with no signature changes. A falsy
    ``run_ref`` (a call outside any run) is a NO-OP — never a phantom orphan
    row, never a crash. This single-writer discipline is pinned by
    ``tests/test_conc_map_writer_ownership.py`` (DEC-10 / SEQ-2).

  * ``cost_of(run)`` / ``daily_total(runs)`` — the read side the surfaces render.

There are NO caps here — that is R-P3b. This module only *shows* cost.

The live ledger is an in-process dict keyed by execution_id (mirroring the
in-memory ``WorkflowTracker`` the Work page already reads). It is NOT a durable
vault store, so it never mutates vault JSON during tests. Durability across a
resume rides on the run record's own ``cost`` field (``ExecutionSnapshot.cost``
/ the quick record), which ``cost_of`` also accepts directly.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Union

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Shipped price table (per 1,000 tokens). Operator-editable via the override
#  env var below (Settings → price editor). These are *estimates* the operator
#  is expected to tune — they are honest published-rate ballparks for the models
#  the router actually uses (sharing_on/model_presets.py), quoted in the
#  provider's real currency (USD). The honesty invariant is about UNKNOWN models,
#  not about pinning a vendor's exact rate: a model absent here is priced Unknown,
#  never guessed.
#
#  Money is Decimal from the ground up — the constants are Decimal, so no float
#  ever enters a currency computation.
# ─────────────────────────────────────────────────────────────────────────────

_D = Decimal


def _price(in_per_1k: str, out_per_1k: str, currency: str = "USD") -> Dict[str, Any]:
    return {"in_per_1k": _D(in_per_1k), "out_per_1k": _D(out_per_1k), "currency": currency}


# Keys are the exact model ids the router emits (provider/model). Exact match
# only — no fuzzy prefixing (fuzzy matching would be a form of guessing).
_SHIPPED_PRICES: Dict[str, Dict[str, Any]] = {
    # The budget default across all three tiers (proven live in the field).
    "deepseek/deepseek-v4-flash": _price("0.00027", "0.0011"),
    # The "balanced"/"quality" flash-class tier-1/2 brain.
    "google/gemini-3-flash-preview": _price("0.0003", "0.0012"),
    # The premium "quality" tier-1 reasoning opt-in.
    "anthropic/claude-sonnet-4.5": _price("0.003", "0.015"),
}

#: Env var carrying operator price overrides as a JSON object
#: ``{model_id: {"in_per_1k": "..", "out_per_1k": "..", "currency": ".."}}``.
#: Read on EVERY cost_of/daily_total so a Settings edit takes effect immediately
#: (AC3) with no restart and no cache to invalidate. Values are strings → Decimal.
PRICE_OVERRIDE_ENV = "SYSTEMU_MODEL_PRICES"

#: Currency of a zero-usage run's zero total (and the daily total's empty case).
DEFAULT_CURRENCY = "USD"

_CURRENCY_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "JPY": "¥"}


def currency_symbol(currency: str) -> str:
    """Human symbol for a currency code (falls back to ``CODE `` for the unknown)."""
    return _CURRENCY_SYMBOLS.get((currency or "").upper(), f"{currency} ")


def shipped_prices() -> Dict[str, Dict[str, Any]]:
    """The immutable shipped defaults (a fresh copy; callers must not mutate)."""
    return {m: dict(p) for m, p in _SHIPPED_PRICES.items()}


def _load_overrides() -> Dict[str, Dict[str, Any]]:
    """Parse operator overrides from the env var. A malformed blob degrades to
    ``{}`` (never blocks a run on bad pricing config)."""
    raw = os.environ.get(PRICE_OVERRIDE_ENV)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        logger.debug("[costing] ignoring malformed %s override JSON", PRICE_OVERRIDE_ENV)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for model, spec in data.items():
        if not isinstance(spec, dict):
            continue
        try:
            _in = _D(str(spec["in_per_1k"]))
            _out = _D(str(spec["out_per_1k"]))
            # Money honesty: a NEGATIVE price is not a valid rate — a valid
            # Decimal like "-5" would otherwise be accepted and fabricate a
            # NEGATIVE total. Reject it (mirrors settings.save_price_overrides)
            # so an unknown/garbage override degrades to the shipped default or
            # Unknown, never to an invented (negative) number.
            if _in < 0 or _out < 0:
                logger.debug("[costing] rejecting negative-price override for %r", model)
                continue
            out[str(model)] = {
                "in_per_1k": _in,
                "out_per_1k": _out,
                # Normalize the currency code (upper + strip) so an override of
                # "usd"/" USD " matches the shipped-default "USD" — otherwise a
                # single-currency run would compare unequal and render Unknown.
                "currency": (str(spec.get("currency") or DEFAULT_CURRENCY).strip().upper()
                             or DEFAULT_CURRENCY),
            }
        except (KeyError, InvalidOperation, ValueError, TypeError):
            logger.debug("[costing] skipping malformed override for %r", model)
            continue
    return out


def current_prices() -> Dict[str, Dict[str, Any]]:
    """The effective price table: shipped defaults with operator overrides on top.
    Recomputed on each call so a Settings edit is live (AC3)."""
    merged = shipped_prices()
    merged.update(_load_overrides())
    return merged


# ─────────────────────────────────────────────────────────────────────────────
#  Cost model (pydantic so model_dump(mode="json") serializes Decimal → str at
#  the render boundary — never a float in the payload).
# ─────────────────────────────────────────────────────────────────────────────


class Money(BaseModel):
    amount: Decimal
    currency: str


class ModelLine(BaseModel):
    model: str
    tokens_in: int
    tokens_out: int
    #: None ⇔ the model has no price (unknown) — see ``priced``.
    cost: Optional[Money] = None
    #: False ⇔ unknown model (tokens shown, money withheld — never guessed).
    priced: bool


class CostSummary(BaseModel):
    tokens_in: int
    tokens_out: int
    by_model: List[ModelLine]
    #: None ⇔ the total is Unknown (any unpriced line, or mixed currencies).
    total: Optional[Money] = None
    #: False ⇔ the total is Unknown (honesty flag the surfaces branch on).
    total_known: bool


# ─────────────────────────────────────────────────────────────────────────────
#  The live ledger (in-process, thread-safe). Keyed by execution_id.
# ─────────────────────────────────────────────────────────────────────────────

_LEDGER: Dict[str, List[Dict[str, Any]]] = {}
_LEDGER_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def record_usage(run_ref: Optional[str], model: Optional[str],
                 tokens_in: Optional[int], tokens_out: Optional[int]) -> None:
    """Attribute one LLM call's token usage to its owning run.

    The SOLE writer of the cost ledger (pinned by CONC-MAP). Called from the LLM
    router at the token-capture point with ``current_execution_id()`` as
    ``run_ref``. A falsy ``run_ref`` (a call outside any run) is a deliberate
    NO-OP — no phantom orphan row, no crash. Best-effort: accounting must never
    break an LLM call.
    """
    if not run_ref:
        return
    try:
        row = {
            "model": str(model or ""),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "at": _now_iso(),
        }
    except Exception:
        return
    with _LEDGER_LOCK:
        _LEDGER.setdefault(str(run_ref), []).append(row)


import os as _os
import re as _re
_UNSAFE_EID = _re.compile(r"[^A-Za-z0-9_.-]")

#: Base dir for durable cost files. Prod uses "data" (matching the snapshot store's
#: default). Tests set this to a tmp dir to redirect the durable IO without touching
#: real ./data (mirrors execution_snapshot's redirect pattern).
_DEFAULT_DATA_DIR = None


def _cost_path(execution_id: str, data_dir=None):
    """``<data_dir>/audit/exec_<eid>/cost.json`` — a DURABLE per-run cost file
    (co-located with the snapshot but NOT deleted on completion). The eid is
    path-sanitized (it becomes a filename)."""
    from pathlib import Path
    base = data_dir if data_dir is not None else (_DEFAULT_DATA_DIR or "data")
    eid = (_UNSAFE_EID.sub("_", str(execution_id)).replace("..", "_")) or "unknown"
    return Path(base) / "audit" / f"exec_{eid}" / "cost.json"


def persist_run_cost(execution_id: Optional[str], *, data_dir=None) -> None:
    """Write a run's usage rows to its DURABLE cost file so the per-run cost display
    survives a daemon restart (the in-process ledger is volatile; the
    ExecutionSnapshot cost is deleted on completion). Best-effort + atomic; a falsy
    eid or an empty ledger is a no-op (never an empty/orphan file)."""
    if not execution_id:
        return
    rows = usage_rows(execution_id)
    if not rows:
        return
    try:
        target = _cost_path(execution_id, data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
        _os.replace(tmp, target)
    except Exception:
        logger.debug("[costing] persist_run_cost failed (swallowed)", exc_info=True)


def _read_durable_cost(execution_id: Optional[str], *, data_dir=None) -> List[Dict[str, Any]]:
    """Read a run's durable cost rows, or [] when absent/corrupt. Never raises."""
    if not execution_id:
        return []
    try:
        target = _cost_path(execution_id, data_dir)
        if not target.exists():
            return []
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def seed_usage(run_ref: Optional[str], rows: Optional[List[Dict[str, Any]]]) -> None:
    """R-P3a: re-seed a run's ledger with its DURABLE (persisted) usage rows on
    RESUME, so post-resume LLM calls ACCUMULATE on top of the pre-suspend cost.

    Without this, a resumed run (execute() mints a fresh execution_id) starts with
    an empty ledger; the NEXT capture then writes ``snapshot.cost`` from the
    fresh-eid ledger (post-resume rows only) and OVERWRITES the durable pre-suspend
    cost — it is lost, and cost_of()/daily_total both undercount. Seeding the fresh
    eid from ``snapshot.cost`` makes the next capture emit the FULL (pre+post) set.

    Idempotent: only seeds when this run's ledger is currently EMPTY (a fresh
    process, or a fresh resume eid) — never double-seeds a live/accumulating run. A
    falsy run_ref or empty rows is a no-op."""
    if not run_ref or not rows:
        return
    try:
        seeded = [{"model": str(r.get("model") or ""),
                   "tokens_in": int(r.get("tokens_in") or 0),
                   "tokens_out": int(r.get("tokens_out") or 0),
                   "at": str(r.get("at") or _now_iso())}
                  for r in rows if isinstance(r, dict)]
    except Exception:
        return
    if not seeded:
        return
    with _LEDGER_LOCK:
        if not _LEDGER.get(str(run_ref)):
            _LEDGER[str(run_ref)] = seeded


def drop_usage(run_ref: Optional[str]) -> None:
    """R-P3a: drop a run's ledger entry. Used on RESUME to remove the STALE
    pre-resume execution_id once its rows have been migrated to the fresh eid
    (via ``seed_usage``) — so the ledger holds exactly ONE entry per logical run
    and the daily-total ledger-scan cannot double-count a resumed run. No-op for a
    falsy/absent key."""
    if not run_ref:
        return
    with _LEDGER_LOCK:
        _LEDGER.pop(str(run_ref), None)


def usage_rows(run_ref: Optional[str]) -> List[Dict[str, Any]]:
    """The recorded usage rows for a run (a copy; empty for an unknown/None ref)."""
    if not run_ref:
        return []
    with _LEDGER_LOCK:
        return [dict(r) for r in _LEDGER.get(str(run_ref), [])]


def reset_ledger() -> None:
    """Test hook — drop all recorded usage."""
    with _LEDGER_LOCK:
        _LEDGER.clear()


def ledger_run_ids() -> List[str]:
    """Every run id with recorded usage (a snapshot copy)."""
    with _LEDGER_LOCK:
        return list(_LEDGER.keys())


# ─────────────────────────────────────────────────────────────────────────────
#  Read side — cost_of / daily_total
# ─────────────────────────────────────────────────────────────────────────────


def _rows_of(run: Any) -> List[Dict[str, Any]]:
    """Resolve a ``run`` argument to its usage rows.

    Accepts (most-specific first):
      * a list of usage-row dicts (already resolved),
      * an execution_id string (looked up in the live ledger),
      * a mapping/object carrying a ``cost`` list (the durable record path),
      * a mapping/object carrying an ``execution_id`` (ledger lookup),
      * None → [].
    """
    if run is None:
        return []
    if isinstance(run, list):
        return run
    if isinstance(run, str):
        # Live ledger first (freshest, in-process). If empty — a completed run in a
        # FRESH process (daemon restarted → ledger cleared), or a run whose eid is
        # off-process — fall back to the DURABLE per-run cost so the cost display
        # survives a restart (the ExecutionSnapshot cost is deleted on completion).
        rows = usage_rows(run)
        return rows if rows else _read_durable_cost(run)
    # dict-like record
    if isinstance(run, dict):
        if isinstance(run.get("cost"), list):
            return run["cost"]
        eid = run.get("execution_id")
        return usage_rows(eid) if eid else []
    # object with attributes
    cost_attr = getattr(run, "cost", None)
    if isinstance(cost_attr, list):
        return cost_attr
    eid = getattr(run, "execution_id", None)
    return usage_rows(eid) if eid else []


def _summarize(rows: Iterable[Dict[str, Any]]) -> CostSummary:
    prices = current_prices()

    # Aggregate token counts per model, preserving first-seen order.
    agg: Dict[str, Dict[str, int]] = {}
    order: List[str] = []
    for r in rows or []:
        model = str((r or {}).get("model") or "")
        if model not in agg:
            agg[model] = {"tokens_in": 0, "tokens_out": 0}
            order.append(model)
        agg[model]["tokens_in"] += int((r or {}).get("tokens_in") or 0)
        agg[model]["tokens_out"] += int((r or {}).get("tokens_out") or 0)

    by_model: List[ModelLine] = []
    total_tin = total_tout = 0
    running = Decimal(0)
    currencies: set[str] = set()
    all_priced = True

    for model in order:
        tin = agg[model]["tokens_in"]
        tout = agg[model]["tokens_out"]
        total_tin += tin
        total_tout += tout
        price = prices.get(model)
        if price is None:
            all_priced = False
            by_model.append(ModelLine(model=model, tokens_in=tin, tokens_out=tout,
                                      cost=None, priced=False))
            continue
        amount = (price["in_per_1k"] * (Decimal(tin) / Decimal(1000))
                  + price["out_per_1k"] * (Decimal(tout) / Decimal(1000)))
        cur = price["currency"]
        currencies.add(cur)
        running += amount
        by_model.append(ModelLine(model=model, tokens_in=tin, tokens_out=tout,
                                   cost=Money(amount=amount, currency=cur), priced=True))

    # Empty run → a KNOWN zero (AC5), not Unknown.
    if not by_model:
        return CostSummary(tokens_in=0, tokens_out=0, by_model=[],
                           total=Money(amount=Decimal(0), currency=DEFAULT_CURRENCY),
                           total_known=True)

    # Total is honest only if every line is priced AND all share one currency
    # (you cannot add USD to INR without inventing an exchange rate).
    if all_priced and len(currencies) == 1:
        total = Money(amount=running, currency=next(iter(currencies)))
        total_known = True
    else:
        total = None
        total_known = False

    return CostSummary(tokens_in=total_tin, tokens_out=total_tout,
                       by_model=by_model, total=total, total_known=total_known)


def cost_of(run: Any) -> CostSummary:
    """Price a run's usage → a CostSummary (tokens + by-model + total|Unknown).

    ``run`` may be an execution_id, a list of usage rows, or a record carrying a
    ``cost`` field / ``execution_id`` (see ``_rows_of``).
    """
    return _summarize(_rows_of(run))


def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


def daily_total(runs: Optional[Iterable[Any]] = None, *, day: Optional[str] = None) -> CostSummary:
    """The day's cost, summed over both lanes.

    * ``runs`` given → sum the usage across exactly those run refs/records
      (the Home page enumerates today's runs and passes them). This is the
      reconciling path: ``daily_total(runs)`` == Σ ``cost_of(run)``.
    * ``runs`` None → sum every ledger row stamped with ``day`` (default: today,
      UTC). A best-effort in-process fallback when the caller has no run list.
    """
    if runs is not None:
        rows: List[Dict[str, Any]] = []
        for run in runs:
            rows.extend(_rows_of(run))
        return _summarize(rows)

    target = day or _today_utc()
    rows = []
    for rid in ledger_run_ids():
        for r in usage_rows(rid):
            at = str(r.get("at") or "")
            if not at or at[:10] == target:
                rows.append(r)
    return _summarize(rows)


# ─────────────────────────────────────────────────────────────────────────────
#  Pure render helpers (used by the surfaces; unit-testable without NiceGUI)
# ─────────────────────────────────────────────────────────────────────────────


def format_tokens(n: int) -> str:
    """Compact token count: 999 → "999", 1500 → "1.5k", 38000 → "38k"."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    thousands = n / 1000.0
    if thousands < 10 and n % 1000 != 0:
        return f"{thousands:.1f}k"
    return f"{round(thousands)}k"


def format_money(money: Optional[Money]) -> str:
    """A Money → "₹4.20" / "$0.42"; None (Unknown) → "—" (never a fake number)."""
    if money is None:
        return "—"
    return f"{currency_symbol(money.currency)}{money.amount:.2f}"


def chip_text(summary: CostSummary) -> str:
    """The Work-row chip: "$4.20 · 38k tok"; an Unknown total → "— · 38k tok".

    Cost is SEPARATE chrome from verified/claimed — this string never implies
    the run succeeded, only what it spent.
    """
    total_tok = int(summary.tokens_in) + int(summary.tokens_out)
    money = format_money(summary.total) if summary.total_known else "—"
    return f"{money} · {format_tokens(total_tok)} tok"


def cost_chip_for(run: Any) -> Optional[str]:
    """The per-run cost chip for a surface, or ``None`` when the run had no LLM
    usage (so a zero-cost run shows no chip). ``run`` is anything ``cost_of``
    accepts (an execution_id, a record, or usage rows). Used by BOTH lanes — the
    Work row and the quick-lane chat entry — so neither is a cost blind spot."""
    summary = cost_of(run)
    if (summary.tokens_in + summary.tokens_out) <= 0:
        return None
    return chip_text(summary)
