"""Capability-Gap Benchmark (CGB) — research evaluation for the Reverse-Harness paper.

Not part of the shipped ``systemu`` package. See
docs/superpowers/specs/2026-06-09-reverse-harness-whitepaper-design.md §5 and
docs/superpowers/plans/2026-06-10-cgb-evaluation.md.

The package constructs gap-bearing seed vaults per trial, runs
``ShadowRuntime.execute()`` directly under controlled conditions, accounts tokens
via a patch on ``systemu.core.llm_router.llm_call``, reads the shipped
pull-decision instrumentation (decision_audit.jsonl + harness ledger) to compute
the PRIMARY RQ1 metrics, grades success with EXTERNAL oracles (never the system's
own goal-verifier), and aggregates with stdlib-only statistics.
"""
