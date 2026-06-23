"""Bulk CGB run on the CHEAP model set — first real paper numbers within credit.

!!! MAKES REAL API CALLS !!!  Three models that fit a small budget:
  * deepseek/deepseek-v4-pro   (in $0.43 / out $0.87 per 1M) — the capable cheap one
  * google/gemini-3-flash      (in $0.50 / out $3.00) — weak baseline
  * nvidia/nemotron-3-ultra    (FREE)

Per-model cap 2,000,000 tokens, so worst-case PAID spend is bounded:
  deepseek 2M ~= $0.9, gemini 2M ~= $1.4, nemotron $0  ->  ~$2.3 total.
Resume-safe: re-run to continue without double-spending past the cap.

Run from repo root:  python -m cgb_eval.run_bulk
"""
from pathlib import Path

from cgb_eval.analysis import load_results, render_markdown, summarize
from cgb_eval.conditions import CONDITIONS
from cgb_eval.runner import run_matrix
from cgb_eval.tasks import ALL_TASKS

# nemotron-3-ultra(:free) DROPPED: it returns malformed responses that crash the
# runtime's LLM parser ('NoneType' object is not subscriptable), looping every trial
# to no effect. The two cheap PAID models below both run cleanly.
MODELS = ["deepseek_v4_pro", "gemini_3_flash"]
CAP = 2_000_000
TRIALS = 1
RESULTS = Path("cgb_results/bulk.jsonl")
TABLES = Path("cgb_results/bulk_tables.md")
WORKDIR = Path("cgb_results/bulk_work")


def main() -> None:
    print("*** CGB BULK RUN (cheap models: deepseek + gemini + nemotron-free) ***")
    print(f"*** per-model cap {CAP:,} tokens; worst-case paid spend ~$2.3 ***")
    run_matrix(ALL_TASKS, list(CONDITIONS), MODELS, trials=TRIALS,
               results_path=RESULTS, workdir_root=WORKDIR,
               per_model_token_budget=CAP)
    md = render_markdown(summarize(load_results(RESULTS)))
    TABLES.write_text(md, encoding="utf-8")
    print(md)
    print(f"\ntables -> {TABLES}")


if __name__ == "__main__":
    main()
