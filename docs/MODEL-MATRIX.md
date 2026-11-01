# MODEL-MATRIX

**Decisions:** DEC-12 (the matrix + router enforcement), DEC-20a (the `locality` column).
**Code:** [`sharing_on/model_matrix.py`](../sharing_on/model_matrix.py) is the executable
half of this document. [`systemu/core/llm_router.py`](../systemu/core/llm_router.py)
enforces it. If this file and that module ever disagree, the module is right and this
file is a bug.

This document is cited by `sharing_on/config.py`, `sharing_on/model_presets.py`,
`tests/test_ra10_model_matrix.py`, and `release-notes/v0.9.60.md`.

---

## What this is

systemu makes LLM calls at very different kinds of work. Planning an open-world goal
needs the strongest model configured. Reshaping a free-text answer into a list of names
does not. The matrix is the table that says which is which, so a call site can declare
**what kind of work it is** — a *stage* — instead of hard-coding a tier number.

The tier a stage runs at then comes from an operator-facing `Config` knob. Change the
knob, and every call site tagged with a stage in that class moves to a different model.

## The table

| Stage | Tier class | `Config` knob | Default tier | Locality (DEC-20a) | Tagged at |
|---|---|---|---|---|---|
| `planner` | planner | `planner_tier` | 1 | `cloud_default` | `runtime/open_world_planner.py`, `pipelines/activity_extractor.py`, `pipelines/quick_task.py` (next-action choice), `pipelines/shadow_decision.py` (assignment tiebreak) |
| `refiner` | planner | `planner_tier` | 1 | `cloud_default` | `pipelines/scroll_refiner.py` |
| `binder_assist` | binder | `binder_tier` | 1 | `cloud_default` | `pipelines/fact_extractor.py` |
| `reasoning` | reasoning | `reasoning_tier` | 1 | `cloud_default` | `pipelines/evolution_engine.py`, `pipelines/refinery.py` (appraisal), `pipelines/shadow_decision.py` (persona), `pipelines/tool_inadequacy_diagnosis.py`, `runtime/auto_skill_extractor.py`, `runtime/supervisor.py`, `runtime/tools/delegate.py` |
| `judge` | judge | `judge_tier` | 1 | `cloud_default` | `pipelines/scroll_validator.py`, `pipelines/skill_validator.py`, `runtime/tools/mixture_of_agents.py` |
| `executor` | executor | `executor_tier` | 2 | `cloud_default` | `runtime/shadow_runtime.py` (run-loop decision), `runtime/web/act_loop.py` |
| `forge` | forge | `forge_tier` | 2 | `cloud_default` | `pipelines/tool_forge.py` (spec, code, preview, propose), `pipelines/tool_recalibrator.py`, `pipelines/skill_recalibrator.py`, `pipelines/workshop_module.py` |
| `consult_parse` | parser | `parser_tier` | 3 | `local_capable` | `runtime/table_consult.py` |
| `desk_extraction` | parser | `parser_tier` | 3 | `local_capable` | `runtime/extractor.py` |
| `summarize` | parser | `parser_tier` | 3 | `local_capable` | `pipelines/memory_consolidator.py`, `runtime/memory_consolidator.py`, `scheduler/jobs.py`, `pipelines/refinery.py` (memory extraction), `runtime/episodic_memory.py`, `pipelines/quick_task.py` (synthesis) |
| `parse` | parser | `parser_tier` | 3 | `local_capable` | `pipelines/tool_dry_run.py`, `runtime/context_builder.py` |
| `brief_phrasing` | parser | `parser_tier` | 3 | `local_capable` | none |
| `router_suggestion` | parser | `parser_tier` | 3 | `local_capable` | none |
| `slot_canonicalization` | parser | `parser_tier` | 3 | `local_capable` | none |
| `verification` | verifier | `verifier_tier` | 3 | `n/a` | `runtime/goal_verifier.py`, `runtime/objective_verifier.py`, `runtime/harness_judge.py`, `runtime/coach.py` |

All paths are under `systemu/`. The authoritative list is each row's `call_site` /
`more_call_sites` in `model_matrix.py`; `tests/test_model_matrix_coverage.py` checks it
both ways (every listed file tags the stage, every file that tags it is listed).

`planner` versus `reasoning`: a call is `planner` when it plans or decides the course of
a run (open-world replanning, which skills/tools an activity needs, the quick lane's
next action, which shadow takes a task). It is `reasoning` when it analyses or generates
something that informs a decision without making it (evolution proposals, appraisals,
personas, extracted skills, failure analysis, a delegated subagent's answer).

Environment overrides: `SYSTEMU_PLANNER_TIER`, `SYSTEMU_BINDER_TIER`,
`SYSTEMU_PARSER_TIER`, `SYSTEMU_VERIFIER_TIER`, `SYSTEMU_REASONING_TIER`,
`SYSTEMU_JUDGE_TIER`, `SYSTEMU_EXECUTOR_TIER`, `SYSTEMU_FORGE_TIER`. None of them is
shown in Settings. The tier a numeric label resolves to follows the one shipped idiom —
`1 if "1" in label else (3 if "3" in label else 2)` — so `tier1`, `tier_1` and `1` all
mean tier 1. A blank or missing knob falls back to the stage's class default in the table
above, **not** to tier 2.

Which model each tier actually is comes from `sharing_on/model_presets.py`
(`SYSTEMU_MODEL_PRESET`, or the explicit `SYSTEMU_TIER{1,2,3}_MODEL` overrides, which
always win). The matrix picks a *tier*; presets pick the *model* behind that tier.

### Behaviour change when every call was tagged

Each new row defaults to the tier its call sites already used, with one exception that
DEC-12 names: **summarizers moved from tier 1 to tier 3.** The shadow, global and
per-run memory consolidation, the memory-candidate extraction, and the episodic session
summary now run on the cheap tier (the `summarize` row, `parser_tier`). The quick lane's
synthesis was already on tier 3. To put them back, set `SYSTEMU_PARSER_TIER=tier1`
(this also moves every other parser-class stage).

Everything else routes exactly as before: the `verification` row reads the pre-existing
`verifier_tier`, and `reasoning` / `judge` / `executor` / `forge` ship at the tier their
calls used.

### Open question: forge on the strongest tier

`forge` stays at tier 2 because that is what its calls used. Whether tool generation
should default to the strongest configured model (the flash-tier forge-degradation debt)
is **not ruled**. `SYSTEMU_FORGE_TIER=tier1` moves every tagged forge call for anyone who
wants to measure it.

## What is honestly not wired

Three of the fifteen stages are **registered but untagged**: `brief_phrasing`,
`router_suggestion` and `slot_canonicalization`, named by DEC-20a. No call site in this
build performs them under those names, so they resolve correctly if asked for and
nothing asks.

### A caveat on `refiner`, which *is* tagged

`refiner` is wired: all five `llm_call_json` call sites in
`systemu/pipelines/scroll_refiner.py` now pass `stage="refiner"` instead of a literal
`tier=1`, so `planner_tier` moves them. But only **three of the five sit on a live
production path**:

| Call site | Live? |
|---|---|
| `refine_scroll` → `_call_refine` (the main refine + its self-check retry) | yes |
| `refine_scroll` → the inline GUI-rewrite retry | yes |
| `refine_from_text` → `elder_intake` (the chat path) | yes |
| `_refine_with_gui_guard` → main call | **no — nothing in production calls this function** |
| `_refine_with_gui_guard` → its rewrite retry | **no — same** |

`_refine_with_gui_guard` is a v0.6.5-c helper whose logic was later re-implemented inline
inside `refine_scroll`. Its only remaining callers are `tests/test_v065_stage2_gui_guard.py`
and `tests/test_v065_e2e_replay.py`. Both of its calls are tagged for consistency and are
pinned to route, but the `wired=True` claim rests on the three live ones: the tests behind
it drive `refine_scroll` and `refine_from_text` through the real router and assert the model
id that reached the wire.

That "production never calls it" claim is itself pinned twice, deliberately:
`test_production_paths_never_invoke_the_gui_guard_helper` spies on the helper while driving
both live entry points (edit-safe, and it cannot be fooled by a comment that merely names
the helper), and `test_no_call_to_the_gui_guard_helper_anywhere_in_the_module` covers the
branches the spy does not drive by reading the module text. The second one reads the file
under test, so it carries a manual `source_sensitive` marker — GATE-TIER/DEC-14's
auto-tagger only detects the `inspect.getsource` idiom, and an unmarked file-reading pin
silently breaks the EDIT-SAFE gate's "safe to run concurrently with source edits" promise.

The dead helper is filed, not fixed: deleting it is a separate change that has to rewrite
four existing tests.

There is also an asymmetry worth naming: `systemu/runtime/requirement_binder.py` — the
thing actually called "the binder" — makes **no LLM call at all**. It is deterministic.
So `binder_assist` is not the binder; it is the fact extraction that *feeds* the binder,
and it is the only genuine binder-class LLM stage in the build. `binder_tier` routes
exactly that one call site and nothing else.

## Coverage: which production calls carry a stage

DEC-12 wants a stage tag on every LLM call, and a new LLM-dependent stage to ship only
after it has a row. The router still *accepts* a `tier=`-only call, so that rule is
enforced statically, by `tests/test_model_matrix_coverage.py`. It parses every shipped
module under `systemu/` and `sharing_on/` and reports:

- **untagged**: a router entry point called with no `stage=`, directly or handed to a
  runner as its first argument (`ctx.run(llm_call_json, ...)`).
- **dynamic_stage**: `stage=` present but not a string literal.
- **escape**: an entry point used as a value, so the call happens out of the scan's sight.
- **sdk_bypass**: an OpenAI/Anthropic client built outside the router and the provider
  adapters, so the call never reaches the router.

A literal `stage=` naming an unregistered stage always fails. Everything else that
predates the rule is listed in that file's `EXCEPTIONS`, one entry per
`(kind, path, scope)` with a count and a reason. Both directions fail: a new finding
fails, and an entry whose call was tagged fails until the entry is deleted. A ceiling
constant caps the total, so the set can only shrink.

At the time of writing the set holds 5 findings:

- the supervisor's escalation ladder (`execution_mind`), which picks a tier per call so a
  stage default does not apply;
- two SDK bypasses in `sharing_on/analyzer/` (capture-time instruction generation and
  intent extraction): a package boundary, since `sharing_on` does not import `systemu`;
- one escape (`table_consult.default_llm_fn`, whose consumer applies
  `stage="consult_parse"`);
- the MCP sampling bridge, owned by a concurrent change.

**The stage is the single source of the tier.** A call that passes `stage=` and its own
`tier=` fails the coverage test (`tier_beside_stage`, never excepted): a hand-written tier
beside a stage can only agree by accident, and when it does not the matrix silently wins.
The one accepted form is a tier derived from the same row,
`tier=resolve_stage_tier("<same stage>", config)`, which cannot disagree. One call uses
it: `objective_verifier`, because an existing test pins that its call passes a `tier`.

The scan does not see router access through `getattr`/`importlib`, raw HTTP to a
provider without an SDK client class, or which stage an injected function is finally
called with.

`tests/test_model_matrix_coverage.py` also checks the `wired` column both ways: every
`wired=True` row's call site passes that stage, and every stage a router call really
passes is marked `wired=True`.

## Only one model configured

The matrix picks a tier; the tier's `tierN_model` picks the model. If a tier's model is
blank (empty, whitespace, or unset), `llm_router._configured_tier` makes the call
**borrow the nearest configured tier**, with ties going to the stronger tier
(1 → 2 → 3, 2 → 1 → 3, 3 → 2 → 1). So with one model configured, the strongest stages
and the cheap stages both run on it instead of sending an empty model id. The whole tier
is borrowed, so its provider override comes with its model. If no tier has a model,
nothing changes and the provider's own error surfaces as before.

The shipped defaults put the same model on all three tiers, which also resolves every
stage to that one model. `tests/test_model_matrix_stage_rows.py` pins both cases at the
wire.

## The `locality` column does not route anything

DEC-20a added `locality` per stage: `cloud_required | cloud_default | local_capable`.

**It participates in no routing decision, by design.** Nothing in `model_matrix.py` or
`llm_router.py` reads a locality to pick a model. It is a *declaration*: the record of
which stages a local backend could serve first, so that Privacy-Complete Mode (PCM) can
be built later without re-auditing every call site.

Making locality route models **would be** PCM, and DEC-20b flags PCM as a candidate,
not a commitment — gated behind its own spec pass plus per-stage fixture
evidence, a Horizon-3 candidate. So the column stays declarative until that gate opens.
Query it with `locality_of_stage(stage)` / `stages_by_locality(...)`.

Do not confuse two similarly-named functions:

- `sharing_on.model_matrix.locality_of_stage(stage)` — classifies a **stage**.
- `sharing_on.model_presets.locality_of(model_id)` — classifies a **model id**
  (`ollama/*` ⇒ `local_capable`, `anthropic/claude-sonnet*` ⇒ `cloud_required`, else
  `cloud_default`). Its one consumer is `systemu/runtime/privacy.py`, which uses it to
  tell the operator whether their configured model runs on this machine.

They share a vocabulary and nothing else. Neither one routes.

**Interim honesty rule (DEC-20a).** Until PCM exists, prompts and file excerpts transit
the configured model provider. The local-first promise is about custody, verification,
and the vault — not zero egress. The privacy page renders the current reality.

## Using a stage from a call site

```python
from systemu.core.llm_router import llm_call_json

result = llm_call_json(
    stage="desk_extraction",      # the matrix decides the tier
    system=SYSTEM_PROMPT,
    user=payload,
    config=config,
)
```

Both `tier=` and `stage=` are accepted on `llm_call`, `async_llm_call_json` and
`llm_call_json`. Two rules keep this from drifting back into the bug it fixes:

1. **An unregistered stage name raises `ValueError`.** It never falls back to a default
   tier. Silently accepting a stage and routing it somewhere else is exactly the failure
   the matrix exists to remove, so a typo fails loudly.
2. **If a call passes both `stage=` and a disagreeing `tier=`, the matrix wins** and the
   override is logged at INFO. The stage tag is the statement of intent; a literal tier
   sitting next to it is the stale hard-coding being replaced.

Calls with neither `tier=` nor `stage=` raise. Calls with only `tier=` behave exactly as
they did before the matrix existed — that is the majority of the build and it was left
alone deliberately. A **new** one fails `tests/test_model_matrix_coverage.py` (see
"Coverage" above).

## Adding a stage

1. Add a `StageRow` to `MATRIX` in `sharing_on/model_matrix.py`, with `wired=False` and
   an honest `note`.
2. Tag the call site with `stage="..."` and flip `wired=True`, recording the file in
   `call_site`.
3. Add a row to the table above. `tests/test_model_matrix_routing.py` asserts the wired
   set matches the code, so a row that lies about being wired fails the suite.
4. If the call site was in `EXCEPTIONS` in `tests/test_model_matrix_coverage.py`, delete
   its entry and lower `_CEILING` by the same count.

## Why not "cheap everywhere"

The standing counterexample is the msoffcrypto incident: a cheap model's knowledge gap
produced a confidently wrong answer that cost far more than the tokens saved. Planning
and bind-judgment stay on the strongest configured model. Mechanical, schema-shaped
transforms — where the output is validated against a schema and a deterministic fallback
exists — are where cheap is safe.

## Credentials

Nothing in this document or in `model_matrix.py` reads, stores, or renders a provider
key. Tier and stage resolution operate on model *ids* and tier *labels* only. Keys are
resolved separately in `llm_router._get_client` / `_get_provider` and must never appear
in a log line, an error message, or a doc example.
