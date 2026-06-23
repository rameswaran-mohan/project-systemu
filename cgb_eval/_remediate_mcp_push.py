"""Remediate the cross-trial MCP-attachment leak: re-run the 4 contaminated
mcp-02/03 PUSH cells (gemini, deepseek) in THIS push-only process (no pull runs,
so nothing attaches the 'lookup' server -> no in-memory leak), then surgically
replace the contaminated records in each model's results file.

The contamination inflated the push baseline (a server attached in mcp-01's pull
trial persisted in the process-global MCP registry into later same-process push
trials), so correcting it can only *widen* the pull-vs-push gap.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from cgb_eval.runner import run_trial
from cgb_eval.tasks import ALL_TASKS

CONTAMINATED = [
    ("mcp-02-lookup4", "gemini_3_flash"),
    ("mcp-03-lookup5", "gemini_3_flash"),
    ("mcp-02-lookup4", "deepseek_v4_pro"),
    ("mcp-03-lookup5", "deepseek_v4_pro"),
]
_TASK = {t.task_id: t for t in ALL_TASKS}


def main() -> None:
    wd = Path("cgb_results/_remediate_work")
    clean: dict = {}
    for task_id, model in CONTAMINATED:
        rec = run_trial(_TASK[task_id], "push", model, 0, workdir_root=wd)
        clean[(task_id, "push", model, 0)] = asdict(rec)
        print(f"RERUN {task_id} push {model}: oracle={rec.oracle_passed} "
              f"status={rec.runtime_status} tok={rec.tokens_total}")

    # surgically replace in each model file
    for model in {m for _, m in CONTAMINATED}:
        p = Path(f"cgb_results/v09344_{model}.jsonl")
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        n_rep = 0
        for i, r in enumerate(rows):
            key = (r["task_id"], r["condition"], r["model_profile"], r["trial"])
            if key in clean:
                rows[i] = clean[key]
                n_rep += 1
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        print(f"  {model}: replaced {n_rep} records -> {p}")


if __name__ == "__main__":
    main()
