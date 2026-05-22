# Intelligent Supervisor — Operator Guide

The Intelligent Supervisor (v0.4.0) is a per-Shadow coordinator that watches each execution, decides when a course correction would help, and writes lessons learned into shadow + global memory in real time. It is **opt-in** during the v0.4.0 rollout (`intelligent_supervisor_enabled=false` by default).

## What it does

For each running Shadow, the Supervisor:

1. **Subscribes** to the Shadow's event stream (tool calls, observations, thoughts, failures).
2. **Maintains a hypothesis** about what the Shadow is doing and where it's stuck.
3. **At boundary points** (tool failure, snapshot tick, stall), picks ONE action from a bounded vocabulary.
4. **Writes lessons live** to the Shadow's memory buffer so later executions benefit immediately.
5. **Logs every decision** with its reasoning to a per-execution audit file.

## The action vocabulary

The Supervisor can only choose from these 9 actions. No free-form intervention.

| Action | When it fires | Effect on Shadow |
|---|---|---|
| `DO_NOTHING` | Shadow is making progress, or recovery is already in flight. **Most common.** | None — Shadow proceeds. |
| `NUDGE` | A short hint would help (e.g. "schema requires snake_case"). | Hint appended to next iteration's prompt. |
| `INJECT_REFLECTION` | Tool failure that the LLM hasn't adjusted to. | Structured Reflection block in next prompt. |
| `FORCE_REFLECT` | 3+ consecutive failures on the same objective. | Next Shadow decision MUST be `REFLECT`. |
| `ROLLBACK` | Path since the last snapshot is a clear dead end. | Context rewinds; sticky notes preserved. |
| `SWAP_SHADOW` | Wrong specialist for the work. | Activity re-queued with a different shadow. |
| `ESCALATE` | Recovery budget hit; operator judgment needed. | Surfaces an approval card; activity pauses. |
| `TERMINATE` | Scroll unsatisfiable; further work is wasted tokens. | Marks dead-letter, fires post-mortem. |
| `SET_THINK_BUDGET` | Shadow doing productive deep planning, needs more THINK headroom. | Raises `max_consecutive_think` for this run. |

`SWAP_SHADOW` and `TERMINATE` require operator approval when the Shadow has made measurable progress.

## Tier mix (cost discipline)

- **Routine directives** (`DO_NOTHING`, `NUDGE`, `SET_THINK_BUDGET`) → Tier-3 (cheap / free)
- **Interventions** (`INJECT_REFLECTION`, `FORCE_REFLECT`, `ROLLBACK`, `SWAP_SHADOW`, `ESCALATE`, `TERMINATE`) → Tier-1 (highest reasoning)

Expected cost: ~3× Tier-3 + ~0.5× Tier-1 calls per execution, vs the naïve 8× Tier-1 design.

## Configuration

All knobs are env-var-overridable and inert when the master switch is off.

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| Master switch | `SYSTEMU_INTELLIGENT_SUPERVISOR` | `false` | Enable Supervisor for new executions |
| THINK ceiling | `SYSTEMU_MAX_CONSECUTIVE_THINK` | `5` | Max consecutive THINK before forcing action |
| Evaluation cadence | `SYSTEMU_SUPERVISOR_CADENCE` | `auto` | `every_failure` / `every_snapshot` / `every_n_iterations:N` |
| LLM budget / run | `SYSTEMU_SUPERVISOR_BUDGET_RUN` | `10` | Cap total supervisor LLM calls per execution |
| Routine tier | `SYSTEMU_SUPERVISOR_TIER_ROUTINE` | `tier_3` | Tier for DO_NOTHING / NUDGE etc. |
| Intervention tier | `SYSTEMU_SUPERVISOR_TIER_INTERVENTION` | `tier_1` | Tier for ROLLBACK / SWAP / etc. |
| Directive timeout | `SYSTEMU_SUPERVISOR_TIMEOUT_S` | `5.0` | Hard timeout on LLM directive; returns DO_NOTHING |
| Hourly cost cap | `SYSTEMU_SUPERVISOR_BUDGET_HOUR_USD` | `5.0` | Per-hour USD cap; auto-trips kill switch |
| Daily cost cap | `SYSTEMU_SUPERVISOR_BUDGET_DAY_USD` | `50.0` | Per-day USD cap; auto-trips kill switch |

## Audit trail

Every Supervisor decision is logged at `data/audit/exec_<execution_id>/supervisor.jsonl`. Each row:

```json
{
  "ts":            "2026-05-14T15:32:01+00:00",
  "execution_id":  "exec_abc123",
  "shadow_id":     "shadow_xyz",
  "iteration":     7,
  "trigger":       "tool_failure",
  "classifier":    "param_error",
  "consec_failures": 2,
  "action":        "INJECT_REFLECTION",
  "rationale":     "Tool failed twice with same params — reflection block warranted",
  "hypothesis": {
    "trying":         "create Word doc with custom filename",
    "struggling_on":  "filename param accepts only literal strings, not templates",
    "confidence":     0.78
  },
  "budget_remaining": {"calls": 7, "high_impact_calls": 3}
}
```

## Cost governance

The cost ledger at `data/supervisor_cost_ledger.json` tracks hourly + daily spend. When either cap is breached:

- Kill switch trips automatically
- All future Supervisor calls return `DO_NOTHING` until the bucket rolls over (hour) or operator resets (day)
- Per-hour rollover is automatic; per-day reset is manual

Operator override:

```bash
# Inspect current state
cat data/supervisor_cost_ledger.json

# Reset the day kill switch
python -c "from systemu.runtime.supervisor_cost_ledger import get_ledger; get_ledger().reset_kill_switch()"
```

## Bad-lesson rollback

If the Supervisor writes a wrong lesson to a Shadow's memory:

```bash
# Inspect what was written
python -m sharing_on shadow-memory show <shadow_id>

# Expunge entries matching a predicate (advanced)
python -c "
from systemu.vault.vault import Vault
from sharing_on.config import Config
v = Vault(Config().vault_dir)
count = v.expunge_memory_entry(
    'shadow_xyz',
    predicate=lambda e: e.get('_pattern_signature') == 'param_error|tool_x|kw',
    reason='operator_correction',
)
print(f'expunged {count}')
"
```

Audit trail at `data/audit/expunged_lessons.jsonl` records every removal.

## Failure-mode telemetry (v0.4.0-0)

Use to ground tuning decisions in real data:

```bash
# All failures, top 20 by event_type × error_type × tool_name
python -m sharing_on debug failure-histogram

# Filter to just tool failures
python -m sharing_on debug failure-histogram --event-types tool_failure

# Custom grouping
python -m sharing_on debug failure-histogram --group-by shadow_id,error_type
```

## Rollout

| Week | Setting |
|---|---|
| 0 | `intelligent_supervisor_enabled=false` (default). v0.4.0-0 telemetry runs automatically. |
| 1 | Per-shadow opt-in via vault metadata `shadow.supervisor_enabled=true`. Pick 1–2 trusted Shadows. |
| 2–3 | Default ON for non-critical activities. Compare failure-mode histograms week-over-week. |
| 4+ | Default ON globally. Kill switch becomes opt-OUT rather than opt-IN. |

Rollback at any step: flip `SYSTEMU_INTELLIGENT_SUPERVISOR=false` and restart the daemon.
