# R-P3b (slice 1) — spend caps: a run that reaches its budget halts honestly

R-P3a shipped cost *visibility* (per-run + daily totals) with an explicit "no caps"
note. This adds the caps: an optional **per-task** and **per-day** LLM-spend ceiling,
so a run can no longer quietly burn budget (the burrito tryout ground through 22
iterations of model calls with nothing stopping it).

## What it does

- **Set caps from the CLI:** `sharing-on spend-caps set task 0.50`,
  `sharing-on spend-caps set day 5.00`, `sharing-on spend-caps show`,
  `sharing-on spend-caps clear task`. (Or the `SYSTEMU_SPEND_CAP_TASK` /
  `SYSTEMU_SPEND_CAP_DAY` env vars, which take precedence.) Caps are **OFF by
  default** — with none set, behaviour is byte-identical to R-P3a.
- **Honest halt:** at the top of each agent iteration, before the next model call,
  the run checks its spend against the caps. On reaching a cap it **stops cleanly**
  with a plain message ("Spend cap reached for this task: $0.02 spent of the $0.02
  cap. Raise the cap and re-run to continue.") — never a silent overrun (RUL-6). The
  halt is terminal-non-retrying, so it won't retry-storm against the same cap.

## Honesty guarantees (test-pinned)

- **Never guess (RUL-1):** an *unknown* cost — an unpriced model, or mixed
  currencies — **never** trips a cap. We halt only on a cost we can actually compute;
  a cost we can't price is shown, never halted on.
- **Currency-safe:** a cap only compares against spend in the same currency; a
  mismatch is an honest no-halt.
- **Fail-safe wiring:** a spend-cap check that itself errors fails **open** (does not
  halt) — a broken budget meter must never stop a legitimate run. A bad cap value
  degrades to "no cap", never a crash and never a silent zero (which would halt
  everything).

## Hardened by adversarial review

Review of the enforcement caught (and this slice fixes) several real defects the
green suite couldn't see:
- **A halt no longer fires an LLM post-mortem.** The halt returns a dedicated
  terminal status the supervisor treats like a clean stop (no retry, no dead-letter,
  no Tier-1 `_analyze_failure` call) — a post-mortem *after* a spend cap would spend
  more, not less. This is the highest-severity fix.
- **A cap of `0` is now "no cap", not "halt everything".** `<= 0` means no cap (a 0
  cap would otherwise trip at iteration 1 on a fresh run's known-zero cost and wedge
  the daemon); the CLI rejects `set … 0`.
- **Resumes are exempt from the gate** — a resume finishes already-authorized parked
  work whose snapshot was already deleted, so halting it at iteration 1 would strand
  it. Fresh runs are what get capped.
- **Env caps honor the active pricing currency** (price overrides), not always USD.

## Scope + honest limitations

This is slice 1 of R-P3b (which also covers the action ledger + the privacy page).
The enforcement is a clean **halt** (raise the cap + re-run); the richer AC2 variant
(a resumable in-run "raise and continue" without re-running) and the action-ledger row
for the halt are follow-up slices. Two limitations to know:
- **"per-task" is per-execution**, so a task that fans out sub-agents can spend up to
  N×the per-task cap across N children (each child has its own execution). The
  **per-day** cap bounds the aggregate — use it as the real ceiling for fan-out work.
- **"per-day" is in-process** (spend since the last daemon start; it does not yet sum
  the durable per-run cost files), so a mid-day restart re-zeros the day counter. A
  durable daily aggregate is a follow-up.
