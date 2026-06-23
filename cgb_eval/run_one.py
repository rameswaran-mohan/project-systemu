"""Run ONE model's planned scope for the v0.9.34.1 paper run (real API spend).

!!! MAKES REAL API CALLS !!!  Writes to a PER-MODEL results file so several models
can run in PARALLEL (separate processes) without racing on one file; the analysis
step combines them. Resume-safe + per-model token-capped.

Usage:  python -m cgb_eval.run_one <model_key>
  e.g.  python -m cgb_eval.run_one deepseek_v4_pro

Per-model scope (sized to REAL OpenRouter pricing x the observed 87/13 in/out split,
~$10 total): two full-coverage models (all 22 tasks x 3 conditions incl. the
judge-on/off ablation) + a frontier model on a 1-task-per-family push/pull subset as
a capability spot-check.
"""
import sys
from pathlib import Path

from cgb_eval.runner import run_matrix
from cgb_eval.tasks import ALL_TASKS

# One representative task per family for the frontier-model spot-check.
_SUBSET_IDS = {
    # one per family, with BOTH a hash (tool-01) and the non-hash compression task
    # (tool-05) so the frontier spot-check covers two different synthesis operations.
    "tool-01-sha256", "tool-05-zlib", "skill-01-release-notes",
    "access-01-policy-read-low", "compute-01-reproduce12",
    "subagent-01-budget-regions", "mcp-01-lookup3",
}
_ALL = ["push", "pull", "pull_min_governance"]
_CORE = ["push", "pull"]
_SUBSET = [t for t in ALL_TASKS if t.task_id in _SUBSET_IDS]

# model_key -> (tasks, conditions, per-model token cap). Three vendors (Google/OpenAI/
# Anthropic); caps sized to real OpenRouter pricing x the 87/13 in/out split. The
# agent persona is in the SHIPPED prompt (not eval-injected), so it applies to every
# condition here -- no persona flag. Run each model as its own process (parallel-safe;
# fast frontier models do not contend like deepseek's JSON-repair churn did).
# TWO full-coverage models (gemini + deepseek) for the per-family RQ1/RQ2 + judge
# ablation; THREE subset spot-checks (gpt/opus/glm) for vendor diversity on the
# push-vs-pull core. Launch deepseek SOLO (its response-repair churn starves it under
# parallelism); the four fast models run concurrently without contending.
PLAN = {
    # caps raised to 6.5M for the full-coverage models: real usage is ~80-88k/cell
    # (push flailing + forge trials), so 69 cells need ~5.4-6.1M. Resume-safe -> re-running
    # continues cells dropped at the old 4.5M cap. Still ~$18 total (gemini ~$0.83/M,
    # deepseek ~$0.49/M). opus cap 1.0M->1.3M so its 14th cell (mcp-01) isn't dropped.
    "gemini_3_flash":  (ALL_TASKS, _ALL, 6_500_000),   # full-coverage backbone (fast) + judge ablation
    # deepseek 6.5M->7.5M: an early launch capped it at 51/69 on the stale 4.5M cap;
    # resume needs headroom for the remaining 18 cells (SUBAGENT is suspend/churn-heavy,
    # ~110k/cell), and 7.5M*$0.49/M=$3.7 stays well inside the $25 budget. Resume-safe.
    "deepseek_v4_pro": (ALL_TASKS, _ALL, 7_500_000),   # 2nd full-coverage model (cheap; run SOLO)
    "gpt_5_4":         (_SUBSET, _CORE, 800_000),       # frontier spot-check (OpenAI); cap trimmed for the $15 v0.9.41 re-run ($4.13/M)
    "claude_opus_4_8": (_SUBSET, _CORE, 1_300_000),     # frontier spot-check (Anthropic; pricey ~$7.6/M — INCLUDED in the v0.9.41 re-run after budget raised to $22, so all 5 models are single-version
    "glm_5_2":         (_SUBSET, _CORE, 1_000_000),     # spot-check (Z-AI; limited)
}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in PLAN:
        raise SystemExit(f"usage: python -m cgb_eval.run_one <{'|'.join(PLAN)}>")
    model = sys.argv[1]
    tasks, conditions, cap = PLAN[model]
    res = Path(f"cgb_results/v0941_{model}.jsonl")
    wd = Path(f"cgb_results/v0941_{model}_work")
    print(f"*** v0.9.34.1 RUN — {model}: {len(list(tasks))} tasks x {len(conditions)} "
          f"conditions, cap {cap:,} -> {res} ***")
    n = run_matrix(tasks, conditions, [model], trials=1,
                   results_path=res, workdir_root=wd, per_model_token_budget=cap)
    print(f"=== {model}: {n} new trials ===")


if __name__ == "__main__":
    main()
