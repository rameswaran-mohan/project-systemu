"""R-P3b slice 1 — spend caps (per-task + per-day) with honest halts.

The pure evaluation core: read the R-P3a cost ledger (costing.cost_of /
daily_total) and compare KNOWN totals to configured caps. Design invariants:
  * Caps OFF by default (no configured cap) → NEVER breached → byte-identical
    to R-P3a (which shipped "no caps").
  * An UNKNOWN total (unpriced model / mixed currency — costing.total_known is
    False) NEVER trips a cap: we halt only on a cost we can actually compute
    (RUL-1 — never guess).
  * A currency mismatch between spend and cap NEVER trips (honest skip).
  * "reached" semantics: spend >= cap halts (halt as soon as the cap is reached,
    to prevent FURTHER spend — AC2 "a 2-cent per-task cap halts").
"""
from __future__ import annotations

from decimal import Decimal

from systemu.runtime import spend_caps
from systemu.runtime.costing import CostSummary, Money


def _summary(amount, currency="USD", *, known=True):
    """A CostSummary whose total is `amount` USD (or Unknown when known=False)."""
    total = Money(amount=Decimal(str(amount)), currency=currency) if known else None
    return CostSummary(tokens_in=10, tokens_out=5, by_model=[], total=total,
                       total_known=known)


def _patch_costs(monkeypatch, *, task, day):
    # `_t`/`_d` default-arg capture avoids the daily_total(day=...) kwarg shadowing
    # the `day` CostSummary we want to return.
    monkeypatch.setattr(spend_caps.costing, "cost_of", lambda run, _t=task: _t)
    monkeypatch.setattr(spend_caps.costing, "daily_total",
                        lambda runs=None, day=None, _d=day: _d)


def _caps(task=None, day=None, currency="USD"):
    def _m(v):
        return None if v is None else Money(amount=Decimal(str(v)), currency=currency)
    return {"task": _m(task), "day": _m(day)}


# ── default-off ──────────────────────────────────────────────────────────────

def test_no_caps_configured_never_breaches(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("9.99"), day=_summary("99.99"))
    st = spend_caps.evaluate("exec_1", caps=_caps(task=None, day=None))
    assert st.breached is False
    assert st.task_breached is False and st.day_breached is False


# ── task cap ─────────────────────────────────────────────────────────────────

def test_task_spend_below_cap_ok(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("0.01"), day=_summary("0.01"))
    st = spend_caps.evaluate("exec_1", caps=_caps(task="0.02"))
    assert st.breached is False


def test_task_spend_reaches_cap_halts(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("0.02"), day=_summary("0.02"))
    st = spend_caps.evaluate("exec_1", caps=_caps(task="0.02"))
    assert st.task_breached is True
    assert st.breached is True
    assert "task" in (st.reason or "").lower()


def test_task_spend_over_cap_halts(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("0.05"), day=_summary("0.05"))
    st = spend_caps.evaluate("exec_1", caps=_caps(task="0.02"))
    assert st.task_breached is True


# ── day cap ──────────────────────────────────────────────────────────────────

def test_day_spend_reaches_cap_halts(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("0.01"), day=_summary("1.00"))
    st = spend_caps.evaluate("exec_1", caps=_caps(day="1.00"))
    assert st.day_breached is True
    assert "day" in (st.reason or "").lower()


# ── honesty: unknown cost + currency mismatch never trip ─────────────────────

def test_unknown_total_never_breaches(monkeypatch):
    # A run whose cost is Unknown (unpriced model) must NOT halt on any cap.
    _patch_costs(monkeypatch, task=_summary(None, known=False),
                 day=_summary(None, known=False))
    st = spend_caps.evaluate("exec_1", caps=_caps(task="0.01", day="0.01"))
    assert st.breached is False
    assert st.task_spend is None and st.day_spend is None


def test_currency_mismatch_never_breaches(monkeypatch):
    # Spend in EUR, cap in USD → cannot compare → honest no-halt.
    _patch_costs(monkeypatch, task=_summary("9.99", currency="EUR"),
                 day=_summary("9.99", currency="EUR"))
    st = spend_caps.evaluate("exec_1", caps=_caps(task="0.02", day="0.02"))
    assert st.breached is False


# ── config round-trip (env + overrides file) ────────────────────────────────

def test_set_and_load_caps_roundtrip(tmp_path):
    spend_caps.set_cap("task", "0.50", data_dir=tmp_path)
    spend_caps.set_cap("day", "5.00", data_dir=tmp_path)
    caps = spend_caps.load_caps(data_dir=tmp_path)
    assert caps["task"].amount == Decimal("0.50")
    assert caps["day"].amount == Decimal("5.00")
    spend_caps.clear_cap("task", data_dir=tmp_path)
    caps2 = spend_caps.load_caps(data_dir=tmp_path)
    assert caps2["task"] is None
    assert caps2["day"].amount == Decimal("5.00")


def test_env_cap_overrides_and_parses(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEMU_SPEND_CAP_TASK", "0.25")
    caps = spend_caps.load_caps(data_dir=tmp_path)
    assert caps["task"].amount == Decimal("0.25")


def test_env_invalid_cap_is_ignored_not_crashed(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEMU_SPEND_CAP_DAY", "not-a-number")
    caps = spend_caps.load_caps(data_dir=tmp_path)
    assert caps["day"] is None   # unparseable → no cap, never a crash


# ── halt_if_capped: the runtime enforcement seam ─────────────────────────────

def test_halt_if_capped_returns_message_on_breach(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("0.05"), day=_summary("0.05"))
    msg = spend_caps.halt_if_capped("exec_1", caps=_caps(task="0.02"))
    assert msg is not None
    assert "cap" in msg.lower()
    assert "re-run" in msg.lower()          # honest, actionable guidance


def test_halt_if_capped_returns_none_when_ok(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("0.01"), day=_summary("0.01"))
    assert spend_caps.halt_if_capped("exec_1", caps=_caps(task="0.02")) is None


def test_halt_if_capped_none_when_no_caps(monkeypatch):
    # The default-off path is the hot path — must be a cheap, clean None.
    _patch_costs(monkeypatch, task=_summary("9.99"), day=_summary("99.99"))
    assert spend_caps.halt_if_capped("exec_1", caps=_caps()) is None


# ── CLI layer (view / set / clear) ───────────────────────────────────────────

def test_cli_set_show_clear_roundtrip(tmp_path, capsys):
    from systemu.interface.cli_commands import (
        run_spend_caps_clear, run_spend_caps_set, run_spend_caps_show)
    assert run_spend_caps_set("task", "0.50", data_dir=tmp_path) == 0
    assert run_spend_caps_show(data_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "per-task" in out and "0.50" in out

    assert run_spend_caps_clear("task", data_dir=tmp_path) == 0
    run_spend_caps_show(data_dir=tmp_path)
    out2 = capsys.readouterr().out.lower()
    assert "no cap" in out2                     # per-task now shows "(no cap)"


def test_cli_set_rejects_bad_amount(tmp_path, capsys):
    from systemu.interface.cli_commands import run_spend_caps_set
    assert run_spend_caps_set("task", "not-a-number", data_dir=tmp_path) == 2
    assert "error" in capsys.readouterr().err.lower()


# ── enforcement contract: the halt shape must NOT retry-storm the same cap ────

def test_handle_result_spend_cap_is_terminal_with_no_postmortem(monkeypatch):
    """ADVERSARIAL FINDING 1 (HIGH): a spend-cap halt must NOT fall through to the
    dead-letter branch that fires an uncapped Tier-1 LLM post-mortem (which would
    spend MORE after a cap). Mirrors the cancelled/command-gate clean stop: mark
    terminal, no post-mortem, no dead-letter."""
    import threading
    from systemu.runtime import activity_completion
    from systemu.runtime.supervisor import Supervisor

    sup = Supervisor.__new__(Supervisor)
    sup.vault = object()
    sup._task_queue = None
    sup._running_lock = threading.Lock()
    sup._running = {}
    sup._dl_lock = threading.Lock()
    sup._dead_letters = []
    sup._publish = lambda *a, **k: None
    sup._aname = lambda aid: aid

    marks, analyzed = [], []
    monkeypatch.setattr(activity_completion, "mark_activity_failed",
                        lambda vault, aid, *, status="failed", summary="": marks.append((aid, status)) or True)
    monkeypatch.setattr(sup, "_analyze_failure", lambda payload, result: analyzed.append(payload))

    payload = {"activity_id": "act_sc", "shadow_id": "sh", "submission_id": "s1"}
    result = {"status": "spend_cap_reached", "error": "SpendCapReached",
              "final_summary": "Spend cap reached for this task."}
    sup._handle_result(payload, result)

    assert ("act_sc", "failed") in marks       # terminal (sweep-immune)
    assert analyzed == []                       # NO uncapped LLM post-mortem
    assert sup._dead_letters == []              # not dead-lettered


def test_spend_cap_halt_shape_does_not_retry():
    from systemu.runtime.supervisor import Supervisor
    # sanity on the retry contract the terminal branch relies on.
    assert Supervisor._should_retry("partial", 0, True) is False
    assert Supervisor._should_retry("partial", 0, False) is True


# ── adversarial findings 2 + 6: cap-0 safety + env-cap currency ──────────────

def test_cap_zero_or_negative_is_no_cap(monkeypatch, tmp_path):
    # FINDING 2 (HIGH): a 0 cap must be "no cap", NOT "halt every run at iter 1".
    monkeypatch.setenv("SYSTEMU_SPEND_CAP_TASK", "0")
    assert spend_caps.load_caps(data_dir=tmp_path)["task"] is None
    monkeypatch.setenv("SYSTEMU_SPEND_CAP_TASK", "-1")
    assert spend_caps.load_caps(data_dir=tmp_path)["task"] is None


def test_set_cap_zero_is_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        spend_caps.set_cap("task", "0", data_dir=tmp_path)


def test_default_currency_honors_active_price_overrides(monkeypatch):
    # FINDING 6: a bare env cap must be read in the ACTIVE currency (current_prices,
    # which honors overrides), not always shipped USD — else a non-USD override makes
    # every env cap a silent no-op via the currency-mismatch no-halt.
    from systemu.runtime import costing
    monkeypatch.setattr(costing, "current_prices", lambda: {"m": {"currency": "INR"}})
    assert spend_caps._default_currency() == "INR"


# ── adversarial finding 3 (tighter fix): baseline-relative per-task + day skip ─

def test_task_cap_is_baseline_relative(monkeypatch):
    # A resume/retry seeded with prior spend near the cap must measure only its OWN
    # (post-baseline) spend — so it proceeds past iteration 1 instead of stranding,
    # yet is still bounded.
    baseline = Money(amount=Decimal("0.018"), currency="USD")   # seeded prior cost
    _patch_costs(monkeypatch, task=_summary("0.019"), day=_summary("0.019"))
    st = spend_caps.evaluate("exec_1", caps=_caps(task="0.02"), task_baseline=baseline)
    assert st.task_breached is False                            # 0.019-0.018=0.001 < cap

    _patch_costs(monkeypatch, task=_summary("0.040"), day=_summary("0.040"))
    st2 = spend_caps.evaluate("exec_1", caps=_caps(task="0.02"), task_baseline=baseline)
    assert st2.task_breached is True                           # 0.040-0.018=0.022 >= cap


def test_enforce_day_false_skips_the_day_cap(monkeypatch):
    # A resume passes enforce_day=False: already-authorized work isn't day-halted
    # (bounded instead by its per-task cap). A fresh run enforces it.
    _patch_costs(monkeypatch, task=_summary("0.001"), day=_summary("99.0"))
    st = spend_caps.evaluate("exec_1", caps=_caps(day="1.00"), enforce_day=False)
    assert st.day_breached is False and st.day_spend is None
    st2 = spend_caps.evaluate("exec_1", caps=_caps(day="1.00"), enforce_day=True)
    assert st2.day_breached is True


def test_run_baseline_reads_current_task_cost(monkeypatch):
    _patch_costs(monkeypatch, task=_summary("0.07"), day=_summary("0.07"))
    b = spend_caps.run_baseline("exec_1")
    assert b is not None and b.amount == Decimal("0.07")
    assert spend_caps.run_baseline(None) is None               # no run → no baseline
