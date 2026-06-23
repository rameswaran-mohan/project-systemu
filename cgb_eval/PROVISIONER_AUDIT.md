# Provisioner-family characterization (code-verified)

Verified against the live code on `paper/reverse-harness`
(pro/main **v0.9.34** + paper-readiness builds). This is a **code-verification** of
the already-decided family characterization, not a scope gate. All line numbers
below were re-derived against the current files (not carried over from v0.9.30).

Threat model: systemu is a **single-owner, local deputy** — governance is
proportionate to irreversible risk, NOT multi-tenant containment.

## Materialise dispatch (per kind)

`Governor.materialise()` acts only on `verdict.decision == GRANT` and routes to a
kind-specific provisioner via `_dispatch` (`systemu/runtime/governor.py:381-407`).

| Kind | Provisioner | Effect of a GRANT (code) | Materialised this run? | Benchmark oracle |
|---|---|---|---|---|
| **TOOL** | `_provision_tool` (`governor.py:409-527`) | Builds a PROPOSED `Tool`, calls `forge_proposed_tools(activity, config, vault)` (forge → dry-run → deploy), mints a lease. Tool is callable this run. | **Yes** (end-to-end) | end-to-end **artifact** (file produced) |
| **COMPUTE** | `_provision_compute` (`governor.py:655-724`) | Returns `compute_grant={"extra_iterations", "extra_think"}` clamped to `policy.max_compute_ceiling` (per-field caps: iterations ≤ `ceiling*100`, think ≤ `ceiling*32000`); the loop applies `iter_budget += extra_iterations`. | **Yes** (budget grant the loop consumes) | **artifact ONLY** — `ordered_inputs_reproduced` (`cgb_eval/oracle.py:112-141`) recomputes the expected output from the actual unguessable inputs and requires the finished, correctly-ordered file. **No "or a request exists" escape** (`cgb_eval/tasks/compute_family.py:11-15`). A pull trial passes iff the extra budget actually let it complete what push could not. |
| **SKILL** | `_provision_skill` (`governor.py:529-602`) | Authors + persists a SKILL.md via `persist_skill_candidate` (skills_dir = `config.skills_user_dir` or `<vault_root>/skills`). The agent must then `LOAD_RESOURCE` to use it. | Indirect (persist-then-load) | end-to-end **artifact** |
| **SUBAGENT** | `_provision_subagent` (`governor.py:726-810`) | Returns a spawn directive `{task, depth_cap, budget_fraction}` clamped to policy; the async loop routes it to the **real parallel fleet** (`systemu/runtime/subagent_fleet.py` + `subagent_harness.py`) under the enforced concurrency cap, collated result. | **Yes** (real + parallel; gated by `SYSTEMU_DELEGATE_USE_PARALLEL=true`) | **collated / partial-success** (merged deliverable, credits partial) |
| **ACCESS** | `_provision_access` (`governor.py:604-653`) | **v0.9.34 (Bug 5 / D.2):** records an **advisory** lease and returns the access spec only. Does **NOT** open any fs/network resource, and the dead `apply` sandbox-policy patch was **removed** — there is no sandbox boundary to apply one to and nothing in the loop ever consumed it. The grant message is now honest: the agent is told the lease is advisory, that **no sandbox boundary is enforced** on the local single-owner backend, and to **proceed with its existing tools** (`shadow_runtime.py:2395-2408`). | **Arbitrated + logged only** (materialisation out of scope by single-owner design) | **arbitration** — "completed-OR-proper-ACCESS-request" via the harness ledger |
| **MCP** *(new, P3)* | `_provision_mcp` (`governor.py:812-971`) | Single seam `ConnectionManager().connect_and_discover_sync(server_id, spec, allowed_hosts=, require_tls=)` — connect + DNS/SSRF/TLS precheck + discover in one call (`manager.py:269-281`). For each discovered (and `tool_filter`-allowed) tool: **sanitize** the server-supplied description, **pin** the canonical def-hash via `set_tool_hash` (rug-pull defence), **enable** it via `set_tool_enabled` (honours the P2 exposure budget), then `set_server_meta(connected=True)`. Mints a lease carrying `mcp_server_id`. Returns FULL tool dicts. On GRANT-apply the loop calls `register_server_tools(vault, server, tool_dicts)` (`shadow_runtime.py:2420-2467`) → each tool becomes a callable **v2-registered** `mcp__<server>__<tool>` whose handler routes through the gated chokepoint `call_mcp_tool`. OAuth → `{"materialised": False, "reason": "oauth_pending", "authorize_url": ...}` (P4 wires the handoff). | **Yes** — discovered tools are callable **this same run** by their namespaced names; the catalog builder picks them up automatically. Each namespaced tool's `check_fn` is `is_tool_enabled`, so a disable/revoke drops it from the catalog. On terminal state `revoke_leases` unregisters the server's namespaced tools (`governor.py:355-366`). | **(eval family TBD)** — no benchmark MCP family exists yet. |

### MCP rug-pull protection (verified)

Two-stage, fail-closed:
1. **Pin at connect** — `_provision_mcp` computes `tool_def_hash(name, sanitized_description, input_schema)` and stores it via `connections.set_tool_hash` (`governor.py:923-924`).
2. **Re-hash on use** — every call through the chokepoint re-derives the current def-hash and compares it via `check_and_pin_hash`; on drift the tool is **disabled (fail-closed)** and the call is refused with a re-approval prompt rather than executing a silently-changed definition (`systemu/runtime/mcp/dispatch.py:346-377`). The description is sanitized to the SAME canonical form at both pin-time and re-check-time so the hashes are apples-to-apples (the v0.9.37 fix — otherwise every connected tool auto-disabled on first call).

External MCP output is additionally wrapped as untrusted data (`_guard_mcp_output`, banner + marker-strip + size-cap, `dispatch.py:249-296`).

## Arbitration risk bands (`systemu/runtime/harness_arbiter.py`)

The auto-grant flags only affect MEDIUM-band kinds. HIGH-band requests escalate
**regardless** of the flags — this is RQ4's bounded-safety property (verified in
`tests/test_cgb_safety_properties.py`):

- **TOOL** new code → HIGH → ESCALATE (`_arbitrate_tool`, `harness_arbiter.py:97-127`); reuse of an already-enabled tool → LOW → GRANT.
- **ACCESS** write/secret/network (or a resource whose name contains secret/credential/passwd/token) → HIGH → ESCALATE (`_arbitrate_access`, `harness_arbiter.py:163-195`); whitelisted read → LOW → GRANT; non-whitelisted read → MEDIUM → ESCALATE (needs_llm_judgment).
- **COMPUTE** over ceiling → HIGH → ESCALATE; within ceiling + `auto_grant_compute` → LOW → GRANT (`_arbitrate_compute`, `harness_arbiter.py:198-245`).
- **SUBAGENT** beyond depth or budget → HIGH → ESCALATE; within both + `auto_grant_subagent` → MEDIUM → GRANT (`_arbitrate_subagent`, `harness_arbiter.py:248-310`). The depth guard takes `max(model-claimed spec.depth, actual_next_depth = ctx["subagent_depth"]+1)` so a child cannot undercut its true depth by lying low (v0.9.33 Bug 3).
- **SKILL** new procedural text → MEDIUM → GRANT*/ESCALATE; reuse existing → LOW → GRANT (`_arbitrate_skill`, `harness_arbiter.py:130-160`).
- **INPUT** → always MEDIUM → ESCALATE (never auto-skipped; the run was told to pause for a human answer).
- **MCP** *(new)* (`_arbitrate_mcp`, `harness_arbiter.py:341-392`), rules in order:
  1. **SSRF literal-IP → DENY HIGH.** An `http`/`sse` URL whose host is a private/loopback/link-local/metadata **IP literal** (incl. `169.254.169.254`) and is NOT in `policy.allowed_mcp_hosts` is refused (`_is_ssrf_ip_literal`, `harness_arbiter.py:327-338`). This precedes everything — a "connected" claim cannot whitelist a loopback/metadata literal. DNS-resolution of *hostnames* to private IPs is enforced at connect time in `_provision_mcp` (keeping this pure layer network-free).
  2. **Re-attach / allowlisted → GRANT LOW.** `server_id` already in `ctx["connected_mcp_servers"]` OR in `policy.allowed_mcp_servers` → no re-prompt after restart.
  3. **New external server → ESCALATE HIGH** (operator approval — new external capability source). The shared post-process downgrades a non-blocking ESCALATE HIGH/MEDIUM → DENY HIGH so the run continues (mirrors TOOL).

### Per-run request cap (verified)

`arbitrate()` enforces a per-run cap **before** kind-specific logic
(`harness_arbiter.py:452-466`): when `ctx["requests_this_run"] >= policy.max_requests_per_run`
the request is DENY (non-blocking) or ESCALATE (blocking). The default is
**`max_requests_per_run = 8`** (`harness_policy.py:135`, also the `from_config`
default at `:245`). It is **actually enforced**: the loop threads the live count
via `_harness_arbitration_context(pre_inc_count, subagent_depth)`
(`shadow_runtime.py:379-392`) into `Governor.arbitrate(_req, context=_arb_ctx)`
(`shadow_runtime.py:3867-3871`, with parallel call sites at `:4099-4102`). The
count passed is the **pre-increment** value, so the cap fires at exactly the
`max_requests_per_run`-th request, not one early.

> **Honest nuance (verified):** `_harness_arbitration_context` threads only
> `requests_this_run` and `subagent_depth`. It does **not** populate
> `enabled_tools`, `existing_skills`, `connected_mcp_servers`, or
> `baseline_tokens`. So in the live loop the context-driven LOW-GRANT fast-paths
> (TOOL-reuse, SKILL-reuse, MCP **re-attach via `connected_mcp_servers`**) cannot
> fire from context — MCP re-attach LOW GRANT currently depends on
> `policy.allowed_mcp_servers` only, and COMPUTE banding uses the
> `baseline_tokens` default (100k). The arbiter SUPPORTS these context keys
> (`harness_arbiter.py:425-433`); the loop simply does not feed them yet.

## Ledger shape consumed by the RQ1 extractor (`cgb_eval/pull_decision.py`)

`{vault}/harness_ledger/<exec>.jsonl` carries (verified in
`Governor._ledger_entry` + `reconcile_outcomes`, `governor.py:1027-1136`):

- **arbitration rows**: nested `request` (`attempts_before`, `confidence`, `kind`, `spec`)
  + `verdict` (`decision`, `decided_by` ∈ {deterministic, llm}, `lease_id`, `risk_band`) + `outcome`.
- **`request-outcome` events**: `outcome` ∈ {granted_used, granted_unused,
  denied_fallback_ok, denied_fallback_failed, escalate_unresolved} +
  `pull_failure_category` ∈ {premature_request, wasted_request, unused_grant, unknown}
  (from `systemu/runtime/failure_classifier.classify_pull_failure`).
- `lease-mint` / `lease-revoke` event rows (skipped by the extractor). MCP leases
  also carry `mcp_server_id` so `revoke_leases` can unregister the live namespaced
  tools (`governor.py:317-368`, `975-998`).

`{vault}/executions/<exec>/decision_audit.jsonl` carries per-iteration blockage
signals + REQUEST_HARNESS instrumentation (`systemu/runtime/decision_audit.py`,
`IterationDecision`: `loop_guard_active`, `stuck_round_count`,
`consec_research_reads`, `consec_tool_failures`, `is_request_harness`,
`harness_kind`, `harness_confidence`, `harness_attempts_before`).

## Discrepancies vs the plan sketch (and how the CGB adapted)

1. **Outcome location.** The plan sketch put `request_outcome` / `decided_by` flat
   on a "verdict" row. The shipped ledger nests them under the arbitration row's
   `request`/`verdict` dicts and carries the terminal usage outcome on a separate
   `request-outcome` event. `pull_decision.py` reads the **real** shapes (nested
   arbitration rows for `decided_by`/`attempts_before`; `request-outcome` events
   for `outcome`/`pull_failure_category`) and also tolerates the flat shape for
   synthetic test fixtures.
2. **`vault.list_tools()` returns dicts, not `Tool` objects** — the seed test reads
   `t["name"]`, not `t.name`.
3. **Pull-failure taxonomy is authoritative from the ledger.** When
   `request-outcome` events exist, the extractor uses the ledger's own
   `pull_failure_category` (premature/wasted/unused) rather than re-deriving
   premature from `decision_audit`; it falls back to the audit-derived premature
   count only when no event rows exist (push runs / pre-Build-1).
4. **`mcp_search_tools` is advertised but has NO dispatch handler (open bug).**
   The exposure-budget overflow affordance `_MCP_SEARCH_AFFORDANCE`
   (`shadow_runtime.py:501-518`) advertises a `mcp_search_tools` tool to the model,
   but no v2 registration / handler / dispatch branch exists for it anywhere in
   `systemu/`. Only `mcp_call_tool` is registered (`mcp/client.py:205-216` with
   `_mcp_handler`). Calling `mcp_search_tools` therefore falls through to the
   "Tool '…' not found" path (`shadow_runtime.py:4994-4998`). Track separately —
   does not affect the per-kind characterization above.
