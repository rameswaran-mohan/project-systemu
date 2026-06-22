"""v0.9.7 Phase 1.4b — goal-level verification must be WIRED into the COMPLETE
gate behind SYSTEMU_INTENT_ENGINE (default off). getsource + flag guards."""
import inspect


def test_intent_engine_flag_default_on(monkeypatch):
    """Phase 4.4 (graduated): the intent engine is now default ON when neither
    the env var nor a config attr is set."""
    from systemu.runtime.shadow_runtime import _intent_engine_enabled
    monkeypatch.delenv("SYSTEMU_INTENT_ENGINE", raising=False)

    class _Cfg:  # no intent_engine_enabled attr → env/default path
        pass
    assert _intent_engine_enabled(_Cfg()) is True


def test_intent_engine_flag_env_opt_out(monkeypatch):
    """SYSTEMU_INTENT_ENGINE=false is the explicit opt-out to the legacy engine."""
    from systemu.runtime.shadow_runtime import _intent_engine_enabled
    monkeypatch.setenv("SYSTEMU_INTENT_ENGINE", "false")

    class _Cfg:
        pass
    assert _intent_engine_enabled(_Cfg()) is False


def test_intent_engine_config_attr_opt_out():
    """An explicit config attr still wins over the new default."""
    from systemu.runtime.shadow_runtime import _intent_engine_enabled

    class _Cfg:
        intent_engine_enabled = False
    assert _intent_engine_enabled(_Cfg()) is False


def test_intent_engine_flag_env_on(monkeypatch):
    from systemu.runtime.shadow_runtime import _intent_engine_enabled
    monkeypatch.setenv("SYSTEMU_INTENT_ENGINE", "true")

    class _Cfg:
        pass
    assert _intent_engine_enabled(_Cfg()) is True


def test_intent_engine_flag_config_attr_wins():
    from systemu.runtime.shadow_runtime import _intent_engine_enabled

    class _Cfg:
        intent_engine_enabled = True
    assert _intent_engine_enabled(_Cfg()) is True


def test_complete_gate_wires_goal_verifier():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert "_intent_engine_enabled(self.config)" in src
    assert "goal_verifier" in src and "verify_goal(" in src
    # The rejection gate must be bypassable by a goal-level pass.
    assert "and not _goal_ok" in src


def test_goal_uses_raw_request_over_intent():
    """Decision 0.1 #2: the verifier goal is the raw user message (Scroll.raw_request),
    falling back to the refiner's intent only when absent."""
    from systemu.runtime.shadow_runtime import ShadowRuntime, _intent_goal_success
    import inspect
    for src in (inspect.getsource(_intent_goal_success), inspect.getsource(ShadowRuntime.execute)):
        if 'getattr(scroll, "raw_request"' in src:
            assert 'getattr(scroll, "intent"' in src  # fallback present
    # at least one site must prefer raw_request
    assert 'getattr(scroll, "raw_request"' in inspect.getsource(_intent_goal_success) \
        or 'getattr(scroll, "raw_request"' in inspect.getsource(ShadowRuntime.execute)


def test_stuck_park_wires_goal_success():
    """Goal-level acceptance must also fire at the stuck-park (the run parks via
    the no-progress guard BEFORE reaching COMPLETE on per-objective-fragile tasks)."""
    from systemu.runtime.shadow_runtime import ShadowRuntime, _intent_goal_success
    src = inspect.getsource(ShadowRuntime.execute)
    assert "_intent_goal_success(" in src, "stuck-park must check goal-level success"
    assert callable(_intent_goal_success)
