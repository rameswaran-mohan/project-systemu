"""Aggregation + statistics for CGB results. Stdlib only (no scipy).

Reports the PRIMARY RQ1 pull-decision-quality table (precision/recall of
blocked->pulled, premature/wasted/unused-grant rates, used-vs-unused grants, and
the RQ3 deterministic-vs-LLM ``decided_by`` split) first, then the RQ2 efficacy /
RQ3 cost tables and the paired McNemar tests.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Tuple


def bootstrap_ci(values: List[float], n_boot: int = 5000, alpha: float = 0.05,
                 seed: int = 42) -> Tuple[float, float]:
    """Percentile bootstrap CI for the mean."""
    vals = [float(v) for v in values]
    if not vals:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(mean(rng.choices(vals, k=n)) for _ in range(n_boot))
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(int((1 - alpha / 2) * n_boot), n_boot - 1)]
    return lo, hi


def mcnemar_exact_p(b: int, c: int) -> float:
    """Exact McNemar (two-sided binomial) on discordant pairs.

    b = condition-B-only successes, c = condition-A-only successes.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n) * 2
    return min(1.0, p)


_PD_COUNT_KEYS = ("blocked_iters", "requests", "blocked_then_pulled", "premature",
                  "wasted", "unused_grant", "cap_exceeded", "granted_na",
                  "granted_used", "granted_unused", "decided_by_det", "decided_by_llm")


def _summarize_pull_decision(recs: List[dict]) -> dict:
    """RQ1 PRIMARY: sum pull-decision counts across pull runs (push runs carry
    pull_decision=None and are ignored), then derive precision/recall + rates."""
    totals = {k: 0 for k in _PD_COUNT_KEYS}
    n_runs = 0
    for r in recs:
        pd = r.get("pull_decision")
        if not pd:
            continue
        n_runs += 1
        for k in _PD_COUNT_KEYS:
            totals[k] += int(pd.get(k, 0) or 0)
    req = totals["requests"]
    blocked = totals["blocked_iters"]
    granted = totals["granted_used"] + totals["granted_unused"]
    out = dict(totals)
    out["n_pull_runs"] = n_runs
    # precision = requests that followed genuine blockage / all requests
    out["precision"] = (totals["blocked_then_pulled"] / req) if req else 0.0
    # recall = blocked iterations that led to a pull / all blocked iterations
    out["recall"] = (totals["blocked_then_pulled"] / blocked) if blocked else 0.0
    out["premature_rate"] = (totals["premature"] / req) if req else 0.0
    out["wasted_rate"] = (totals["wasted"] / req) if req else 0.0
    out["cap_exceeded_rate"] = (totals["cap_exceeded"] / req) if req else 0.0  # over-delegation
    out["unused_grant_rate"] = (totals["unused_grant"] / granted) if granted else 0.0
    out["used_grant_rate"] = (totals["granted_used"] / granted) if granted else 0.0
    return out


def summarize(records: Iterable[dict]) -> dict:
    recs = list(records)
    by_condition: Dict[str, dict] = {}
    for cond in sorted({r["condition"] for r in recs}):
        sub = [r for r in recs if r["condition"] == cond]
        toks = [r["tokens_total"] for r in sub]
        by_condition[cond] = {
            "n": len(sub),
            "success_rate": mean(1.0 if r["oracle_passed"] else 0.0 for r in sub),
            "token_mean": mean(toks),
            "token_ci": bootstrap_ci([float(t) for t in toks]) if len(toks) > 1
                        else (float(toks[0]), float(toks[0])),
            "llm_calls_mean": mean(r["llm_calls"] for r in sub),
        }

    # Paired push-vs-pull per (task, model): same trial index = a pair.
    paired: Dict[tuple, dict] = {}
    idx = {(r["task_id"], r["model_profile"], r["condition"], r["trial"]): r
           for r in recs}
    keys = {(r["task_id"], r["model_profile"]) for r in recs}
    for (task, model) in sorted(keys):
        b = c = 0
        trials = {r["trial"] for r in recs
                  if r["task_id"] == task and r["model_profile"] == model}
        for t in trials:
            push = idx.get((task, model, "push", t))
            pull = idx.get((task, model, "pull", t))
            if not push or not pull:
                continue
            if pull["oracle_passed"] and not push["oracle_passed"]:
                b += 1
            elif push["oracle_passed"] and not pull["oracle_passed"]:
                c += 1
        paired[(task, model)] = {"b": b, "c": c, "p": mcnemar_exact_p(b, c)}

    return {
        "pull_decision": _summarize_pull_decision(recs),  # RQ1 PRIMARY
        "by_condition": by_condition,                     # RQ2 efficacy / RQ3 cost
        "paired": paired,                                 # RQ2 paired tests
    }


def load_results(path: Path) -> List[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def render_markdown(summary: dict) -> str:
    pd = summary["pull_decision"]
    out = ["### RQ1 — Pull-decision quality (PRIMARY)", "",
           "| metric | value |", "|---|---|",
           f"| pull runs | {pd['n_pull_runs']} |",
           f"| harness requests | {pd['requests']} |",
           f"| blocked->pulled precision | {pd['precision']:.2f} |",
           f"| blocked->pulled recall | {pd['recall']:.2f} |",
           f"| premature-request rate | {pd['premature_rate']:.2f} |",
           f"| wasted-request rate | {pd['wasted_rate']:.2f} |",
           f"| used-grant rate | {pd['used_grant_rate']:.2f} |",
           f"| unused-grant rate | {pd['unused_grant_rate']:.2f} |",
           f"| decided_by deterministic / llm | "
           f"{pd['decided_by_det']} / {pd['decided_by_llm']} |",
           "",
           "### RQ2/RQ3 — Efficacy & governance cost", "",
           "| condition | n | success | token mean (95% CI) | calls |",
           "|---|---|---|---|---|"]
    for cond, s in summary["by_condition"].items():
        lo, hi = s["token_ci"]
        out.append(f"| {cond} | {s['n']} | {s['success_rate']:.2f} | "
                   f"{s['token_mean']:.0f} ({lo:.0f}-{hi:.0f}) | "
                   f"{s['llm_calls_mean']:.1f} |")
    out.append("")
    out.append("### RQ2 — Paired push-vs-pull (exact McNemar)")
    out.append("")
    out.append("| task | model | pull-only wins | push-only wins | McNemar p |")
    out.append("|---|---|---|---|---|")
    for (task, model), p in summary["paired"].items():
        out.append(f"| {task} | {model} | {p['b']} | {p['c']} | {p['p']:.3f} |")
    return "\n".join(out)
