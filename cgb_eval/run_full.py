"""Full CGB matrix run — PAPER DATA.

!!! MAKES REAL API CALLS !!!  Run this DELIBERATELY (the parent launches it after
the pilot gate + an explicit budget go/no-go), not as part of the test suite.

Matrix: 16 tasks x 3 conditions x 5 models x 5 trials = 1200 cells, but each model
is HARD-CAPPED at PER_MODEL_TOKEN_BUDGET (400,000 tokens) cumulative — once a
model crosses the cap the runner stops scheduling further trials for it, so total
spend is bounded at ~5 x 400k = ~2M tokens regardless of cell count.

Resume-safe: re-running skips completed cells AND re-seeds each model's spent-token
total from the existing results, so an interrupted run continues without
double-spending past the cap.

Run from repo root:
  python -m cgb_eval.run_full
"""
from pathlib import Path

from cgb_eval.analysis import load_results, render_markdown, summarize
from cgb_eval.conditions import CONDITIONS, MODEL_PROFILES
from cgb_eval.runner import PER_MODEL_TOKEN_BUDGET, run_matrix
from cgb_eval.tasks import ALL_TASKS

RESULTS = Path("cgb_results/full.jsonl")
TABLES = Path("cgb_results/tables.md")
WORKDIR = Path("cgb_results/full_work")
TRIALS = 5


def main() -> None:
    print("*** CGB FULL RUN — THIS MAKES REAL API CALLS (OpenRouter key from .env) ***")
    print(f"*** Per-model token cap: {PER_MODEL_TOKEN_BUDGET:,} tokens "
          f"({len(MODEL_PROFILES)} models) ***")
    run_matrix(ALL_TASKS, list(CONDITIONS), list(MODEL_PROFILES), trials=TRIALS,
               results_path=RESULTS, workdir_root=WORKDIR,
               per_model_token_budget=PER_MODEL_TOKEN_BUDGET)
    md = render_markdown(summarize(load_results(RESULTS)))
    TABLES.write_text(md, encoding="utf-8")
    print(md)
    print(f"\ntables -> {TABLES}")


if __name__ == "__main__":
    main()
