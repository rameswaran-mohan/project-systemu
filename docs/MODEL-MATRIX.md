# MODEL-MATRIX

**Decisions:** DEC-12 (the matrix + router enforcement), DEC-20a (the `locality` column).
**Spec:** MASTER-SPEC §15.4.
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

| Stage | Tier class | `Config` knob | Default tier | Locality (DEC-20a) | Tagged at a call site? |
|---|---|---|---|---|---|
| `planner` | planner | `planner_tier` | 1 | `cloud_default` | **yes** — `systemu/runtime/open_world_planner.py` |
| `refiner` | planner | `planner_tier` | 1 | `cloud_default` | **yes** — `systemu/pipelines/scroll_refiner.py` |
| `binder_assist` | binder | `binder_tier` | 1 | `cloud_default` | **yes** — `systemu/pipelines/fact_extractor.py` |
| `consult_parse` | parser | `parser_tier` | 3 | `local_capable` | **yes** — `systemu/runtime/table_consult.py` |
| `desk_extraction` | parser | `parser_tier` | 3 | `local_capable` | **yes** — `systemu/runtime/extractor.py` |
| `brief_phrasing` | parser | `parser_tier` | 3 | `local_capable` | no |
| `router_suggestion` | parser | `parser_tier` | 3 | `local_capable` | no |
| `slot_canonicalization` | parser | `parser_tier` | 3 | `local_capable` | no |
| `verification` | verifier | `verifier_tier` | 3 | `n/a` | no — see below |

Environment overrides: `SYSTEMU_PLANNER_TIER`, `SYSTEMU_BINDER_TIER`,
`SYSTEMU_PARSER_TIER`, `SYSTEMU_VERIFIER_TIER`. The tier a numeric label resolves to
follows the one shipped idiom — `1 if "1" in label else (3 if "3" in label else 2)` —
so `tier1`, `tier_1` and `1` all mean tier 1. A blank or missing knob falls back to the
stage's class default in the table above, **not** to tier 2.

Which model each tier actually is comes from `sharing_on/model_presets.py`
(`SYSTEMU_MODEL_PRESET`, or the explicit `SYSTEMU_TIER{1,2,3}_MODEL` overrides, which
always win). The matrix picks a *tier*; presets pick the *model* behind that tier.

## What is honestly not wired

Read the last column literally. Four of the nine stages are **registered but untagged**:
they resolve correctly if you ask for them, and nothing asks for them.

- **`brief_phrasing`, `router_suggestion`, `slot_canonicalization`** — named by DEC-20a.
  No call site in this build performs them under those names.
- **`verification`** — `goal_verifier.py`, `objective_verifier.py`, `harness_judge.py`
  and `coach.py` each read `config.verifier_tier` directly. That predates the matrix and
  already works; it is registered here so the class is nameable, but those call sites
  were **not** re-routed through `stage=` in this slice. Changing `verifier_tier` does
  move them — through the old path, not through the matrix.

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

## The `locality` column does not route anything

DEC-20a added `locality` per stage: `cloud_required | cloud_default | local_capable`.

**It participates in no routing decision, by design.** Nothing in `model_matrix.py` or
`llm_router.py` reads a locality to pick a model. It is a *declaration*: the record of
which stages a local backend could serve first, so that Privacy-Complete Mode (PCM) can
be built later without re-auditing every call site.

Making locality route models **would be** PCM, and MASTER-SPEC §15.4 marks PCM
"FLAGGED, NOT COMMITTED" — gated behind its own spec pass plus per-stage fixture
evidence, a Horizon-3 candidate. So the column stays declarative until that gate opens.
Query it with `locality_of_stage(stage)` / `stages_by_locality(...)`.

Do not confuse two similarly-named functions:

- `sharing_on.model_matrix.locality_of_stage(stage)` — classifies a **stage**.
- `sharing_on.model_presets.locality_of(model_id)` — classifies a **model id**
  (`ollama/*` ⇒ `local_capable`, `anthropic/claude-sonnet*` ⇒ `cloud_required`, else
  `cloud_default`). Its one consumer is `systemu/runtime/privacy.py`, which uses it to
  tell the operator whether their configured model runs on this machine.

They share a vocabulary and nothing else. Neither one routes.

**Interim honesty rule (§15.4).** Until PCM exists, prompts and file excerpts transit
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
alone deliberately.

## Adding a stage

1. Add a `StageRow` to `MATRIX` in `sharing_on/model_matrix.py`, with `wired=False` and
   an honest `note`.
2. Tag the call site with `stage="..."` and flip `wired=True`, recording the file in
   `call_site`.
3. Add a row to the table above. `tests/test_model_matrix_routing.py` asserts the wired
   set matches the code, so a row that lies about being wired fails the suite.

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
