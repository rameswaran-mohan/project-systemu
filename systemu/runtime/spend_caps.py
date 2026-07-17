"""R-P3b — spend caps (per-task + per-day) with honest, resumable halts.

R-P3a shipped cost *visibility* (``costing.py``) with an explicit "NO caps — that
is R-P3b" note. This module adds the caps: a deterministic evaluation that reads
the R-P3a cost ledger and compares KNOWN totals to configured limits, so the
runtime can HALT a run that has reached its per-task or per-day spend cap and ask
the operator (raise-for-this-run / stop) instead of burning budget silently.

Design invariants (all test-pinned in ``tests/test_spend_caps.py``):
  * **Off by default.** No configured cap ⇒ never breached ⇒ byte-identical to
    R-P3a. Caps are opt-in (env or the overrides file).
  * **Never guess (RUL-1).** An UNKNOWN total (``costing.total_known is False`` —
    an unpriced model or mixed currencies) NEVER trips a cap. We halt only on a
    cost we can actually compute; a cost we can't price is shown, never halted.
  * **Currency-honest.** A cap only compares against a spend in the SAME
    currency; a mismatch is an honest no-halt, not a coerced comparison.
  * **"Reached" halts (AC2).** ``spend >= cap`` halts — as soon as the cap is
    reached, to prevent FURTHER spend (the money already spent is not clawed
    back; the halt stops the next call).

This module is a pure reader over the cost ledger + a tiny JSON config store; it
is NOT a durable ledger writer (RUL-7). It stays free of NiceGUI / runtime-loop
imports so it is trivially unit-testable; the runtime consumes ``halt_if_capped``
at the agent iteration boundary (``shadow_runtime``).

Known scope for slice 1 (documented boundaries, not silent surprises):
  * **Per-task is per-execution-attempt.** The cap is baseline-relative (see
    :func:`run_baseline`), so it bounds each run/resume/retry — but a task that
    FANS OUT sub-agents can spend up to N×the per-task cap across N children (each
    child is its own execution). Use the **day cap** as the aggregate ceiling for
    fan-out work.
  * **Per-day is cumulative but in-process.** ``daily_total`` sums this daemon's
    in-process ledger; it does NOT yet read the durable per-run cost files, so a
    mid-day daemon restart re-zeros the day counter. A durable daily aggregate is a
    follow-up; until then "per-day" means "since the last daemon start."
  * **The day cap governs the START of new work.** A resume (finishing already-
    authorized parked work — grants, operator answers, durable retries) passes
    ``enforce_day=False``, so it is not day-halted; it stays bounded by its own
    per-task cap. So the daily budget can be exceeded by at most the per-task cap
    per in-flight resume before the next FRESH run is blocked.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from systemu.runtime import costing
from systemu.runtime.costing import Money

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "spend_caps.json"
#: env override keys (a decimal amount, in the ledger's default currency).
_ENV = {"task": "SYSTEMU_SPEND_CAP_TASK", "day": "SYSTEMU_SPEND_CAP_DAY"}
_KINDS = ("task", "day")


# ── config store (env first, then the JSON overrides file) ───────────────────

def _config_path(data_dir=None) -> Path:
    return Path(data_dir or "data") / _CONFIG_FILENAME


def _default_currency() -> str:
    """The ledger's ACTIVE pricing currency (honors operator price overrides), so a
    bare env cap amount is compared in the same currency the run is actually priced
    in — else a non-USD override would make every env cap a silent no-op."""
    try:
        for line in costing.current_prices().values():
            cur = line.get("currency")
            if cur:
                return str(cur)
    except Exception:
        pass
    return "USD"


def _parse_amount(raw: Any, currency: Optional[str] = None) -> Optional[Money]:
    """Coerce a raw amount (str/number, or a ``{amount,currency}`` dict) to Money.

    Returns None on anything unparseable OR non-positive — a bad or zero cap is
    NEVER a crash and NEVER a "halt everything" (a 0 cap would trip at iteration 1
    on a fresh run's known-zero cost); ``<= 0`` simply means "no cap"."""
    if raw is None or raw == "":
        return None
    cur = currency or _default_currency()
    if isinstance(raw, dict):
        cur = str(raw.get("currency") or cur)
        raw = raw.get("amount")
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amount <= 0:
        return None          # 0/negative ⇒ no cap (never a daemon-wedging halt-all)
    return Money(amount=amount, currency=cur)


def load_caps(data_dir=None) -> Dict[str, Optional[Money]]:
    """Return ``{"task": Money|None, "day": Money|None}``.

    Precedence per kind: env var wins, else the overrides file, else None (off).
    Best-effort — a missing/corrupt file or bad value degrades to "no cap"."""
    file_cfg: Dict[str, Any] = {}
    try:
        p = _config_path(data_dir)
        if p.exists():
            file_cfg = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("[SpendCaps] could not read %s", _config_path(data_dir), exc_info=True)
        file_cfg = {}

    out: Dict[str, Optional[Money]] = {}
    for kind in _KINDS:
        env_raw = os.environ.get(_ENV[kind])
        if env_raw is not None:
            out[kind] = _parse_amount(env_raw)
        else:
            out[kind] = _parse_amount(file_cfg.get(kind))
    return out


def set_cap(kind: str, amount, currency: Optional[str] = None, *, data_dir=None) -> None:
    """Persist a cap to the overrides file. ``kind`` in {'task','day'}."""
    if kind not in _KINDS:
        raise ValueError(f"unknown cap kind {kind!r} (expected one of {_KINDS})")
    money = _parse_amount(amount, currency)
    if money is None:
        raise ValueError(f"invalid cap amount {amount!r}")
    p = _config_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg: Dict[str, Any] = {}
    try:
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    cfg[kind] = {"amount": str(money.amount), "currency": money.currency}
    _atomic_write_json(p, cfg)


def clear_cap(kind: str, *, data_dir=None) -> None:
    """Remove a cap from the overrides file (a no-op if absent)."""
    if kind not in _KINDS:
        raise ValueError(f"unknown cap kind {kind!r}")
    p = _config_path(data_dir)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        cfg = {}
    if kind in cfg:
        cfg.pop(kind, None)
        _atomic_write_json(p, cfg)


def _atomic_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ── evaluation ───────────────────────────────────────────────────────────────

@dataclass
class CapStatus:
    """The result of comparing a run's spend to the configured caps."""
    task_breached: bool
    day_breached: bool
    task_spend: Optional[Money]   # this run's KNOWN cost, or None if unknown
    day_spend: Optional[Money]    # today's KNOWN cost, or None if unknown
    task_cap: Optional[Money]     # configured per-task cap, or None if off
    day_cap: Optional[Money]      # configured per-day cap, or None if off
    reason: Optional[str]         # human breach message, or None

    @property
    def breached(self) -> bool:
        return self.task_breached or self.day_breached


def _known_total(summary) -> Optional[Money]:
    """A CostSummary's total ONLY when it is genuinely known (never a guess)."""
    if summary is None:
        return None
    return summary.total if getattr(summary, "total_known", False) else None


def _over(spend: Optional[Money], cap: Optional[Money]) -> bool:
    """True iff spend is KNOWN, cap is set, currencies MATCH, and spend >= cap."""
    if spend is None or cap is None:
        return False
    if spend.currency != cap.currency:
        return False   # can't compare across currencies — honest no-halt
    return spend.amount >= cap.amount


def _subtract(total: Optional[Money], baseline: Optional[Money]) -> Optional[Money]:
    """``total − baseline`` = THIS execution attempt's spend. None if total is
    unknown; total unchanged when no baseline or a currency mismatch (can't subtract
    across currencies). Floored at 0 (baseline should never exceed total)."""
    if total is None:
        return None
    if baseline is None or total.currency != baseline.currency:
        return total
    return Money(amount=max(Decimal(0), total.amount - baseline.amount),
                 currency=total.currency)


def run_baseline(execution_id: Optional[str]) -> Optional[Money]:
    """This run's STARTING known task cost — captured ONCE at run start so the
    per-task cap measures only THIS execution attempt: 0 for a fresh run, the seeded
    prior cost for a resume/retry. This is what lets a resumed run proceed past
    iteration 1 (no strand) while STILL being bounded (its own post-baseline spend is
    capped) — instead of exempting resumes wholesale."""
    return _known_total(costing.cost_of(execution_id)) if execution_id else None


def evaluate(execution_id: Optional[str], *, caps: Optional[Dict[str, Optional[Money]]] = None,
             day_runs: Optional[Iterable[Any]] = None, data_dir=None,
             task_baseline: Optional[Money] = None, enforce_day: bool = True) -> CapStatus:
    """Compare this run's + today's spend to the caps → a CapStatus.

    ``caps`` defaults to :func:`load_caps`. ``task_baseline`` (from
    :func:`run_baseline`, captured at run start) makes the per-task cap measure only
    THIS execution attempt's spend, so a resume/retry is bounded without being
    stranded. ``enforce_day=False`` skips the (cumulative, global) day cap — the
    runtime passes this for a resume so already-authorized work isn't day-halted (it
    is still bounded by its per-task cap). ``day_runs`` is forwarded to
    ``costing.daily_total``. Reads only — never writes the cost ledger."""
    if caps is None:
        caps = load_caps(data_dir=data_dir)
    task_cap = caps.get("task")
    day_cap = caps.get("day")

    task_total = _known_total(costing.cost_of(execution_id)) if execution_id else None
    task_spend = _subtract(task_total, task_baseline)
    day_spend = _known_total(costing.daily_total(day_runs)) if enforce_day else None

    task_breached = _over(task_spend, task_cap)
    day_breached = _over(day_spend, day_cap)

    reason = None
    if task_breached:
        reason = _breach_reason("task", task_spend, task_cap)
    elif day_breached:
        reason = _breach_reason("day", day_spend, day_cap)

    return CapStatus(
        task_breached=task_breached, day_breached=day_breached,
        task_spend=task_spend, day_spend=day_spend,
        task_cap=task_cap, day_cap=day_cap, reason=reason,
    )


def _breach_reason(kind: str, spend: Optional[Money], cap: Optional[Money]) -> str:
    sym = costing.currency_symbol(cap.currency) if cap else ""
    scope = "this task" if kind == "task" else "today"
    return (f"Spend cap reached for {scope}: {sym}{spend.amount} spent "
            f"of the {sym}{cap.amount} cap.")


def halt_if_capped(execution_id: Optional[str], *,
                   caps: Optional[Dict[str, Optional[Money]]] = None,
                   day_runs: Optional[Iterable[Any]] = None, data_dir=None,
                   task_baseline: Optional[Money] = None, enforce_day: bool = True) -> Optional[str]:
    """The runtime enforcement seam: return an honest halt MESSAGE if a spend cap
    is reached, else None.

    The runtime calls this at the iteration boundary; a non-None return means
    "stop before the next LLM call" (RUL-6 — gate, never silently proceed). Pass
    ``task_baseline`` (from :func:`run_baseline`) so the per-task cap measures this
    attempt's spend, and ``enforce_day=False`` for a resume. The default-off path
    (no caps) returns None cheaply. Pure over the cost ledger + config; never raises."""
    st = evaluate(execution_id, caps=caps, day_runs=day_runs, data_dir=data_dir,
                  task_baseline=task_baseline, enforce_day=enforce_day)
    if not st.breached:
        return None
    return ((st.reason or "Spend cap reached.")
            + " Raise the cap (`sharing-on spend-caps set …`) and re-run to continue.")
