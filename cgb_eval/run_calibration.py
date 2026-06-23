"""Calibration pilots — confirm each redesigned family's signal fires.

!!! MAKES REAL API CALLS (google/gemini-3-flash) !!!  Cheap (~$0.5-1). Run
DELIBERATELY. One representative task per redesigned family across {push, pull},
1 trial each, so we can see: COMPUTE push falls short / pull recovers; ACCESS
lands at the expected risk band (LOW auto-grant vs HIGH escalate); SKILL/SUBAGENT
emit the right harness request. Resume-safe — re-run to add trials.

Run from repo root:  python -m cgb_eval.run_calibration
"""
import json
from pathlib import Path

from cgb_eval.runner import run_matrix
from cgb_eval.tasks import ALL_TASKS

CALIB_TASK_IDS = [
    "tool-01-sha256",                # TOOL: request + forge + recover? (no v2-dispatch loop now)
    "compute-01-reproduce12",        # COMPUTE: with budget now PERCEIVABLE + no coaching, self-provision?
    "access-01-policy-read-low",     # ACCESS: LOW whitelisted read -> auto-grant?
    "access-03-secret-read-high",    # ACCESS: HIGH secret -> escalate?
    "skill-01-release-notes",        # SKILL: request + persist + apply?
    "subagent-01-budget-regions",    # SUBAGENT: breadth -> SUBAGENT request (now runtime-bounded, no cascade)?
]
# v0.9.34 validation uses a CAPABLE-but-cheap model as the mechanism smoke-test: it is
# smart enough to actually exercise request -> grant -> resume end to end, so a null
# (e.g. COMPUTE reqs=0) indicts the fix, not the model. The scored matrix then sweeps
# all models to show the capability gradient. Fresh result path so stale v0.9.31
# (gemini) rows are not mixed into the post-fix numbers.
MODEL = "deepseek_v4_pro"
RESULTS = Path("cgb_results/calibration_v0934.jsonl")
WORKDIR = Path("cgb_results/calibration_v0934_work")


def main() -> None:
    print("*** CGB CALIBRATION v0.9.34 — REAL API CALLS (deepseek-v4-pro) ***")
    tasks = [t for t in ALL_TASKS if t.task_id in CALIB_TASK_IDS]
    # Calibration only: a higher per-model cap than the scored run's 400k, so every
    # family's signal gets exercised in one pass (the scored run keeps the 400k cap).
    # trials=1 for a cheap first validation pass; resume-safe -- re-run to add trials.
    n = run_matrix(tasks, ["push", "pull"], [MODEL], trials=1,
                   results_path=RESULTS, workdir_root=WORKDIR,
                   per_model_token_budget=3_000_000)
    print(f"\nran {n} new trials (resume-safe)\n")

    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows.sort(key=lambda r: (r["task_id"], r["condition"]))
    print(f"{'task':28} {'cond':5} {'status':26} {'oracle':6} {'tok':>7}  pull-signal")
    print("-" * 100)
    for r in rows:
        pd = r.get("pull_decision") or {}
        sig = ""
        if r["condition"] != "push":
            sig = (f"reqs={pd.get('requests', 0)} "
                   f"blocked->pulled={pd.get('blocked_then_pulled', 0)} "
                   f"used-grant={pd.get('granted_used', 0)} "
                   f"det/llm={pd.get('decided_by_det', 0)}/{pd.get('decided_by_llm', 0)}")
        print(f"{r['task_id']:28} {r['condition']:5} {r['runtime_status']:26} "
              f"{str(r['oracle_passed']):6} {r['tokens_total']:>7}  {sig}")


if __name__ == "__main__":
    main()
