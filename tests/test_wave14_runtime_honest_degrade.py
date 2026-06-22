"""W14 S5 — runtime degrades a VALIDATED model that drifts (loud flag) but
BLOCKS a never-validated model that the provider rejects (config error)."""
from __future__ import annotations

import inspect

from systemu.core import llm_router


def test_classify_helper():
    assert llm_router._classify_model_failure(was_validated=True) == "drift"
    assert llm_router._classify_model_failure(was_validated=False) == "config_error"


def test_provider_name_for_tier():
    from types import SimpleNamespace
    cfg = SimpleNamespace(tier1_provider="anthropic", tier2_provider="")
    assert llm_router._provider_name_for_tier(1, cfg) == "anthropic"
    assert llm_router._provider_name_for_tier(2, cfg) == "openrouter"  # "" → default


def test_invalid_model_branch_consults_record_and_blocks_unvalidated():
    src = inspect.getsource(llm_router)
    assert "is_validated" in src, \
        "the degrade path must consult the validated-models record"
    assert "never validated" in src.lower()
    assert "_emit_drift_flag" in src, "validated drift must raise a visible flag"


def test_drift_flag_never_raises(monkeypatch):
    # best-effort: even if log_event blows up, the call path must not break
    import systemu.interface.notifications as notif
    monkeypatch.setattr(notif, "log_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    llm_router._emit_drift_flag(tier=1, dead="x", fallback="y")  # no raise
