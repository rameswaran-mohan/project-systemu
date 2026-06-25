# tests/test_loop_guard_varied.py
from systemu.runtime.loop_guard import LoopGuard


def _lg():
    class _Cfg: pass
    return LoopGuard(_Cfg())


def test_varied_args_same_failing_tool_eventually_blocks():
    lg = _lg()
    verdict = None
    # Same tool, DIFFERENT args each call, all unsuccessful → must escalate.
    for i in range(9):
        verdict = lg.record("file_list_dir", {"path": f"/dir/{i}"}, result=False)
    assert verdict is not None
    assert verdict.get("level") in ("warn", "block")


def test_varied_args_with_success_does_not_block():
    lg = _lg()
    last = None
    for i in range(9):
        last = lg.record("file_list_dir", {"path": f"/dir/{i}"}, result=True)
    assert last is None or last.get("level") != "block"
