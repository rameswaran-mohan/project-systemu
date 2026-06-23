"""Pilot run — PLUMBING VALIDATION GATE.

!!! MAKES REAL API CALLS !!!  Run this DELIBERATELY (the parent launches it as a
gated step), not as part of the test suite.  It exercises one TOOL task across
{push, pull} on ONE model for 2 trials (4 real runs), using the OpenRouter key in
.env.

Validates: env application, vault seeding, REAL execution, ledger capture, oracle
grading, resume.  After it runs, inspect:
  * cgb_results/pilot.jsonl
  * cgb_results/pilot_work/<trial>/vault/harness_ledger/<exec>.jsonl  (REQUEST_HARNESS emitted?)
  * cgb_results/pilot_work/<trial>/vault/executions/<exec>/decision_audit.jsonl  (RQ1 signals?)
to confirm the pull condition emitted REQUEST_HARNESS, tokens were captured
(nonzero), and the pull_decision field is populated (not all-zero).

Run from repo root:
  python -m cgb_eval.run_pilot
"""
from pathlib import Path

from cgb_eval.analysis import load_results, render_markdown, summarize
from cgb_eval.runner import run_matrix
from cgb_eval.tasks import ALL_TASKS

PILOT_TASK_ID = "tool-01-sha256"
PILOT_MODEL = "gemini_3_flash"
RESULTS = Path("cgb_results/pilot.jsonl")
WORKDIR = Path("cgb_results/pilot_work")


def main() -> None:
    print("*** CGB PILOT — THIS MAKES REAL API CALLS (OpenRouter key from .env) ***")
    task = next(t for t in ALL_TASKS if t.task_id == PILOT_TASK_ID)
    n = run_matrix([task], ["push", "pull"], [PILOT_MODEL], trials=2,
                   results_path=RESULTS, workdir_root=WORKDIR)
    print(f"\nran {n} new trials (resume-safe; re-run to continue)")
    print(render_markdown(summarize(load_results(RESULTS))))


if __name__ == "__main__":
    main()
