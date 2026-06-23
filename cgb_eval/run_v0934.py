"""v0.9.34.1 scored matrix — the paper's current numbers (real API spend).

!!! MAKES REAL API CALLS !!!  Resume-safe + per-model token-capped, so spend is
bounded and an interrupted run continues without double-spending. All 22 tasks
(six families incl. the new MCP attachment family) x conditions x the model set.

Strategy (breadth-first for a pilot): trials=1 so the cap buys task/condition
COVERAGE rather than depth; per-cell trial count is reported as realized. Models
have very different per-token prices, so each runs as its OWN ``run_matrix`` call
with its OWN cap (into the one resume-safe results file) rather than a single
shared-cap sweep. nemotron-3-ultra:free is excluded — it returns malformed
responses that crash the runtime LLM parser (documented).

Run:  python -m cgb_eval.run_v0934
"""
from pathlib import Path

from cgb_eval.analysis import load_results, render_markdown, summarize
from cgb_eval.runner import run_matrix
from cgb_eval.tasks import ALL_TASKS

# (model, conditions, per-model token cap). Executed IN ORDER into the one results
# file; resume-safe, so re-running with more entries only adds the new cells.
# Caps are sized to the model's price so total spend stays within budget:
#   deepseek ~$0.65/M, gemini ~$2/M (high output price), gpt ~$5/M.
RUN_PLAN = [
    # Backbone: full coverage (all conditions incl. the judge-on/off ablation).
    ("deepseek_v4_pro", ["push", "pull", "pull_min_governance"], 4_500_000),
    # Diversity models — uncomment to extend (resume-safe). Push+pull only to
    # control spend; the ablation is carried by the backbone model.
    # ("gemini_3_flash", ["push", "pull"], 2_500_000),
    # ("gpt_5_4",        ["push", "pull"], 1_200_000),
]
RESULTS = Path("cgb_results/v09341.jsonl")
TABLES = Path("cgb_results/v09341_tables.md")
WORKDIR = Path("cgb_results/v09341_work")


def main() -> None:
    print("*** CGB v0.9.34.1 SCORED MATRIX — REAL API CALLS ***")
    print(f"*** {len(ALL_TASKS)} tasks; run plan: "
          f"{[(m, len(c), f'{cap/1e6:.1f}M') for m, c, cap in RUN_PLAN]} ***")
    total_new = 0
    for model, conditions, cap in RUN_PLAN:
        print(f"\n--- {model}: {len(conditions)} conditions, cap {cap:,} ---")
        total_new += run_matrix(ALL_TASKS, conditions, [model], trials=1,
                                results_path=RESULTS, workdir_root=WORKDIR,
                                per_model_token_budget=cap)
    print(f"\n=== {total_new} new trials this invocation ===")
    md = render_markdown(summarize(load_results(RESULTS)))
    TABLES.write_text(md, encoding="utf-8")
    print(md)
    print(f"\ntables -> {TABLES}")


if __name__ == "__main__":
    main()
