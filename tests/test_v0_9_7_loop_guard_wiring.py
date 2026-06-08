"""v0.9.7 Phase 0b — loop_guard must be WIRED into the execute loop, not just exist.

inspect.getsource guards so a future refactor can't silently drop the wiring
(the recurring failure mode in this codebase: green module + dead production).
"""
import inspect


def test_shadow_runtime_imports_loop_guard():
    import systemu.runtime.shadow_runtime as sr
    src = inspect.getsource(sr)
    assert "from systemu.runtime.loop_guard import LoopGuard" in src


def test_execute_instantiates_and_records_loop_guard():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert "LoopGuard(self.config)" in src, "execute() must instantiate the loop guard"
    assert "loop_guard.record(" in src, "execute() must record each tool call"
    assert "loop_guard_notice" in src, "execute() must inject the guard verdict into the prompt"
    assert "loop_guard_force_finalize" in src, "block verdict must force a finalize turn"
