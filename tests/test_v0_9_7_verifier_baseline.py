"""v0.9.7 RCA fix — per-objective verifier baseline timing.

BUG: ``ObjectiveState.baseline`` was never populated, so ``process_completion_claim``
fell through to ``state_delta.capture_baseline()`` at verify-time — i.e. AFTER the
agent's tool call had already written the deliverable that same turn. The baseline
absorbed the just-written file → ``compute_delta`` reported nothing new → the
verifier rejected a genuinely-completed objective ("no durable evidence"), trapping
the agent in a re-prove loop so it never reached later objectives.

FIX: capture ONE run-start baseline in ``execute()`` BEFORE the loop and assign it
to each ``ObjectiveState`` so ``compute_delta`` sees everything the run produces.
Applies to both engines (this is the v0.9.1 Layer-4 contract).
"""
import inspect
import pathlib
import tempfile
import types

from systemu.runtime import state_delta


class _StubVault:
    def query_action_audit(self, **kw):
        return []


_CFG = types.SimpleNamespace(
    state_delta_max_files_per_section=50,
    state_delta_file_preview_chars=200,
)


def test_state_delta_baseline_timing_invariant():
    """Pure proof: a baseline captured AFTER the write absorbs the file (empty
    delta — the bug); captured BEFORE, the file is detected (correct)."""
    # post-write baseline → empty (the bug)
    d1 = tempfile.mkdtemp()
    pathlib.Path(d1, "quotes.txt").write_text("a\nb\nc")
    base_after = state_delta.capture_baseline(
        vault=_StubVault(), execution_id="x", objective_id=1, default_output_dir=d1)
    delta_after = state_delta.compute_delta(
        baseline=base_after, vault=_StubVault(), default_output_dir=d1,
        chat_result=None, config=_CFG, execution_id="x")
    assert delta_after.files_added == []

    # pre-write baseline → file detected (correct)
    d2 = tempfile.mkdtemp()
    base_before = state_delta.capture_baseline(
        vault=_StubVault(), execution_id="y", objective_id=1, default_output_dir=d2)
    pathlib.Path(d2, "quotes.txt").write_text("a\nb\nc")
    delta_before = state_delta.compute_delta(
        baseline=base_before, vault=_StubVault(), default_output_dir=d2,
        chat_result=None, config=_CFG, execution_id="y")
    assert any(f["path"].endswith("quotes.txt") for f in delta_before.files_added)


def test_process_completion_claim_credits_with_run_start_baseline(monkeypatch):
    """With a pre-write baseline on the state, the delta handed to the verifier
    contains the new file → credited. With baseline=None (the bug), the helper
    captures post-write → empty delta → NOT credited."""
    from systemu.runtime import objective_verifier
    from systemu.runtime import shadow_runtime as sr

    def _fake_run(*, objective, delta, config):
        ok = bool(delta.files_added or delta.files_modified)
        return {"verified": ok, "reason": "files seen" if ok else "no durable evidence"}
    monkeypatch.setattr(objective_verifier, "run", _fake_run)

    obj = types.SimpleNamespace(id=1)
    cfg = types.SimpleNamespace(
        state_delta_max_files_per_section=50, state_delta_file_preview_chars=200,
        verifier_per_turn_cap=99, verifier_rejection_budget=3)

    # FIX path: baseline captured BEFORE the write, set on the state
    d = tempfile.mkdtemp()
    pre = state_delta.capture_baseline(
        vault=_StubVault(), execution_id="e", objective_id=1, default_output_dir=d)
    pathlib.Path(d, "quotes.txt").write_text("a\nb\nc")
    st = sr.ObjectiveState(baseline=pre)
    out = sr.process_completion_claim(
        objective=obj, vault=_StubVault(), config=cfg, execution_id="e",
        default_output_dir=d, chat_result=None, state=st)
    assert out.credited is True

    # BUG path: no baseline + file already written → captured post-write → empty
    d2 = tempfile.mkdtemp()
    pathlib.Path(d2, "quotes.txt").write_text("a\nb\nc")
    st2 = sr.ObjectiveState()
    out2 = sr.process_completion_claim(
        objective=obj, vault=_StubVault(), config=cfg, execution_id="e2",
        default_output_dir=d2, chat_result=None, state=st2)
    assert out2.credited is False


def test_execute_captures_run_start_baseline_and_assigns_it():
    """Wiring guard: execute() captures a run-start verifier baseline BEFORE the
    loop and assigns it to each ObjectiveState (so the lazy post-write capture
    can never run)."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert "_run_verifier_baseline" in src
    assert "capture_baseline(" in src
    assert (".baseline = _run_verifier_baseline" in src
            or "baseline=_run_verifier_baseline" in src)
