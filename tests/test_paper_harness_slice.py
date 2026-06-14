"""Plan 0 Task 1.7(c) — harness-usage metric slice (additive, harness-only)."""
from systemu.runtime.shadow_metrics import get_shadow_metrics


def test_note_harness_usage_populates_slice(tmp_path):
    m = get_shadow_metrics(force_path=tmp_path / "m.json")
    m.note_harness_usage(shadow_id="s", intent_hash="h", used_harness=True, success=True)
    m.note_harness_usage(shadow_id="s", intent_hash="h", used_harness=True, success=False)
    m.note_harness_usage(shadow_id="s", intent_hash="h", used_harness=False, success=True)  # no-op
    e = m.get(shadow_id="s", intent_hash="h")
    assert e.harness_runs == 2
    assert e.harness_successes == 1
    assert e.harness_success_rate == 0.5


def test_note_harness_usage_is_additive_not_base(tmp_path):
    """Harness-only: it must NOT touch executions/successes (so it can run at
    finalize without double-counting the base recorder)."""
    m = get_shadow_metrics(force_path=tmp_path / "m.json")
    m.note_harness_usage(shadow_id="s", intent_hash="h", used_harness=True, success=True)
    e = m.get(shadow_id="s", intent_hash="h")
    assert e.executions == 0          # base counters untouched
    assert e.successes == 0
    assert e.harness_runs == 1


def test_note_harness_usage_safe_inputs(tmp_path):
    m = get_shadow_metrics(force_path=tmp_path / "m.json")
    m.note_harness_usage(shadow_id="", intent_hash="h", used_harness=True, success=True)
    m.note_harness_usage(shadow_id="s", intent_hash="", used_harness=True, success=True)
    assert m.get(shadow_id="s", intent_hash="h").harness_runs == 0
