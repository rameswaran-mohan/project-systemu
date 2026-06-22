# Memory model

Systemu's runtime operates on a tiered memory system.  Most of what's
described here is already implemented across the existing vault and
runtime — this document formalises the **contract** so operators can
reason about it and contributors can extend it without creating split
state.

If you've read [`ARCHITECTURE.md`](../ARCHITECTURE.md) the high-level
picture is the same; this page goes deeper on what lives where and who
is allowed to write to it.

---

## The five tiers

| Tier | Lives in | What belongs here | Token cost | Status |
|---|---|---|---|---|
| **Identity** | `Shadow.identity_block` + `Shadow.accumulated_voice` (both persisted to the vault, both reloaded on every execution) | Name, role, expertise scope, communication style, hard constraints, demonstrated traits | Always in context (~200-400 tokens) | **Implemented in v0.3** — single `system_prompt` field was split into two; runtime composes them via a computed property |
| **Active Context** | `ExecutionContext` + scratchpad (existing) | Current execution's reasoning, tool calls, observations.  Cleared at end of execution. | Bounded by max-context budget; Tier 3 snapshot compaction (existing) | **Already implemented** — formalised here as the canonical name |
| **Shadow Memory** | `SHADOW_MEMORY.md` + `memory_buffer.jsonl` (per-shadow files under `vault/shadow_army/shadow_<id>/`) | Recurring patterns, failure modes, tool quirks, domain glossary — learned across executions of any Scroll, scoped to **this** Shadow | Loaded on shadow boot (~500-2000 tokens) | **Already implemented** — already has a consolidator |
| **Elder Memory** | `ELDER_MEMORY.md` + `elder/memory_buffer.jsonl` (cross-Shadow) | User preferences, naming conventions, output paths, recurring variables — facts that apply to **every** Shadow this operator runs | Loaded on every shadow boot (~500 tokens) | **Already implemented** — same as above, named here |
| **Archive** | Scroll repository + completed `executions/` directories + capture sessions on disk | Original instructions, raw events, completed-execution histories | Pay-on-demand via `LOAD_RESOURCE` | **Already implemented** across multiple stores — grouped here under one name + access pattern |

Of the five tiers, **four already exist** in the codebase under various
names.  The tier model isn't new infrastructure — it's a contract over
what's already there.  Only the **Identity** tier requires new schema
(planned for a follow-up PR), everything else is documentation plus a
few enforcement rules.

---

## The write contract

The biggest risk in a multi-tier memory system is **state divergence**:
the Shadow Memory says X, the Elder Memory says ¬X, and the live Active
Context contradicts both.  Three rules govern every write:

### Rule 1 — Single source of truth per claim type

Each *kind* of claim lives in exactly one tier:

| Claim kind | Tier | Examples |
|---|---|---|
| Tool quirks, failure modes, skill-specific lessons | **Shadow Memory** | "`file_write` fails with 'path is required' when…" |
| Verbal patterns, decision-making style | **Identity** (`accumulated_voice`) | "Prefers terse confirmations over re-stating the question" |
| User preferences, naming conventions, output paths | **Elder Memory** | "Output files go to `~/Documents/reports/`" |
| Original instructions, raw events | **Archive** | The unmodified `instructions.md` from sharing_on |
| Iteration-local reasoning, tool calls, observations | **Active Context** | The current execution's scratchpad |

A claim that fits in Shadow Memory **never** gets duplicated to Elder,
and vice versa.  Duplication is the road to split state.

### Rule 2 — Writes are always to the most-specific tier

Promotion from a narrow tier to a broader one is done **only** by the
memory consolidator, on a schedule, never per-step.  No runtime code
path writes directly to Elder Memory.

Practical implication: when the runtime learns something during an
execution, it appends to the Shadow's `memory_buffer.jsonl`.  Whether
that lesson eventually ends up in Elder Memory is the consolidator's
call — runtime callers never make that decision.

### Rule 3 — Reads cascade narrow-to-broad

A working-context lookup that misses falls back to Shadow Memory, then
Elder, then Archive:

```
Active Context  ←  current execution scratchpad
      │
      ▼  miss
Shadow Memory   ←  this Shadow's learned patterns
      │
      ▼  miss
Elder Memory    ←  cross-Shadow operator preferences
      │
      ▼  miss
Archive         ←  pay-on-demand via LOAD_RESOURCE
```

Cascading **reads** are cheap.  Cascading **writes** would create the
split we're trying to prevent.

---

## Identity — the new piece

Shipped in v0.3.  The Shadow's identity is stored in two separate
fields:

- **`Shadow.identity_block`** (operator-editable in Workshop; max
  ~500 tokens): name, role, expertise scope, communication style,
  hard constraints, destructive-action policy.  The contract the
  operator can read and audit.
- **`Shadow.accumulated_voice`** (consolidator-grown, append-only
  with rotation to fit token budget): traits the Shadow has
  demonstrated across executions — verbal patterns, decision-making
  style, recurring fallback behaviours.  The Shadow can read this
  but cannot write to it; the consolidator owns the writes.

The runtime `system_prompt` sent to the LLM is **composed** from the
two fields via a Pydantic `@computed_field`:

```
<identity_block>

<accumulated_voice>
```

Legacy callers reading `shadow.system_prompt` keep working —
the property returns the same string the field used to hold.  Legacy
callers writing to `shadow.system_prompt` now hit `AttributeError`;
write to `shadow.identity_block` instead.

Pre-`shadow.json` files are migrated transparently on load: a
Pydantic `model_validator` copies the legacy `system_prompt` value
into `identity_block` and leaves `accumulated_voice` empty.  SQLite /
Postgres backends use the `0003_identity_split` Alembic migration to
add the two new columns and backfill `identity_block = system_prompt`
for every existing row.

---

## Writing to the memory layer — the helper APIs

Two public vault methods are the **only** sanctioned writers to
buffer files:

```python
vault.append_shadow_memory_buffer(shadow_id, entry, *, source)
vault.append_elder_buffer(entry, *, source)
```

Both stamp the entry with tier provenance (`_tier="shadow"` or
`"elder"`) and a `_source` string identifying which caller produced
it (`refinery`, `evolution_engine`, `consolidator`, …).  Pipelines or
tests that need to write a buffer entry should call these — never the
underlying `append_memory_buffer` / `append_elder_memory_buffer`
methods directly.

As of v0.2.2, every direct caller in the codebase has been migrated
onto the helpers (audit pass completed).  The raw methods remain on
the vault but are documented as internal; new code that calls them
will silently bypass tier enforcement and should be considered a bug.

### What goes in a buffer entry

```python
{
    # Discriminator — canonical name is `category`.  The legacy `type`
    # alias is accepted for backwards compat but is DROPPED from the
    # persisted entry so consumers always see one canonical shape.
    # Setting both fields to different values is rejected as a footgun.
    "category":   "tool_quirks" | "user_preference" | "Workflow Patterns" | …,

    # Payload fields — vary by writer.  Refinery uses `lesson`,
    # evolution_engine uses `observation`, etc.
    "summary":    "short human-readable description",
    "details":    { …optional structured data… },
    "confidence": 0.0 to 1.0,

    # Stamped by the helper — do NOT set in caller code:
    "_tier":      "shadow" | "elder",
    "_source":    "refinery" | "evolution_engine" | "consolidator" | …,
    "_ts":        ISO-8601 timestamp,
}
```

The single source of truth for valid Shadow categories is
`systemu/core/memory_types.py:SHADOW_CLAIM_TYPES`.  Both
`pipelines/refinery.py` and every vault implementation import that
frozenset — the closed enum can never drift between the writer and the
validator.

### Strictness — asymmetric per tier

| Tier | Allowlist | Strictness |
|---|---|---|
| **Shadow** | `SHADOW_CLAIM_TYPES` (closed: `heuristics`, `failure_patterns`, `tool_quirks`, `domain_glossary`, `self_assessment`) | **Strict by default** — unknown categories are rejected with a clear error |
| **Elder** | Empty (LLM-driven, open-ended) | Always permissive — only the cross-tier wall enforces |

The asymmetry reflects how the data is produced: Shadow lessons come
from a closed enum baked into the refinery pipeline's LLM prompt, while
Elder observations come from the Evolution Engine where the LLM picks
the category from an open space.

To replay pre-audit data with ad-hoc Shadow categories, construct the
vault with `Vault(..., strict_tier_types=False)` (or
`SqliteVault(..., strict_tier_types=False)`).  No global mutable state
— the choice is per-vault-instance.

---

## Reading the memory layer

Pages and pipelines read memory through the vault's existing read
methods:

```python
md_text, buffer = vault.load_shadow_memory(shadow_id)   # Shadow tier
elder_md        = vault.load_elder_memory()             # Elder tier
elder_buffer    = vault.load_elder_memory_buffer()      # Elder tier (pending)
```

There's no special API for cascading reads — pipelines that need the
cascade ordering compose the calls themselves.  The
[`memory_recall`](../systemu/runtime/memory_recall.py) module is the
canonical helper for "give me everything relevant to this Shadow at
boot" and applies Rule 3 internally.

---

## What's deliberately not formalised yet

- **Archive cascade implementation.** `LOAD_RESOURCE` is defined as
  an action the Shadow can emit, but the Tier 1 prompt that teaches
  Shadows when to emit it is still being refined.
- **Identity split.** Schema + Workshop UI + Evolution scope
  extension all ship together in a follow-up PR — splitting them
  would create a half-migrated vault state we don't want operators
  to encounter.
- **Cross-Shadow conflict detection.** When two Shadows reach
  contradictory conclusions about a tool quirk, the current
  consolidator picks the later entry.  A proper conflict-resolution
  pass is on the roadmap.

---

## Operator workflow — what changes for you

The contract is enforced everywhere as of v0.2.2.  Day-to-day
nothing visible changes: the refinery and evolution_engine pipelines
were migrated onto the helpers transparently, and the entries they
produce now carry `_tier` / `_source` / `_ts` metadata.

When the Identity split lands, you'll see a new "Identity" tab in the
Workshop's Shadow editor with two fields (`identity_block` and
`accumulated_voice`), and a one-shot migration script will run on
first boot to populate `identity_block` from the existing
`system_prompt`.  No data loss.

---

## Contributor workflow — what to use when

- **Adding a new code path that writes to a buffer**: use the helper
  APIs.  Don't reach for `append_memory_buffer` or
  `append_elder_memory_buffer` directly.
- **Adding a new tier**: please open an issue first — the five
  tiers are by design exhaustive, and adding a sixth needs a wider
  discussion about claim-kind partitioning.
- **Adding a new consolidator pass**: route writes through the
  existing consolidator entry points and add your prompt under
  `systemu/prompts/`.  Don't create parallel writers.
