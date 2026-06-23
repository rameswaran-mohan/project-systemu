"""One-off: run a SINGLE (task, condition, model) trial in THIS fresh process to
test the cross-trial MCP-attachment leak hypothesis.  If mcp-02 push fails here
(fresh process, no prior attachment) but passed in the full single-process sweep,
the leak is an in-memory process-global MCP registry, not a real push capability.

Usage: python -m cgb_eval._isolation_check <task_id> <condition> <model>
"""
import sys
from pathlib import Path
from cgb_eval.runner import run_trial
from cgb_eval.tasks import ALL_TASKS


def main() -> None:
    task_id, cond, model = sys.argv[1], sys.argv[2], sys.argv[3]
    task = next(t for t in ALL_TASKS if t.task_id == task_id)
    wd = Path("cgb_results/_isolation_check_work")
    rec = run_trial(task, cond, model, 0, workdir_root=wd)
    print(f"ISOLATED {task_id} {cond} {model}: oracle={rec.oracle_passed} "
          f"status={rec.runtime_status} tok={rec.tokens_total}")
    print(f"  details: {rec.oracle_details[:160]}")


if __name__ == "__main__":
    main()
