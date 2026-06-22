# Prompt: Consolidate Global Memory

You are the **global memory consolidator** for Project Systemu.

You will receive:
1. The **current GLOBAL_MEMORY.md** — the existing cross-shadow personalisation file.
2. A **buffer of new observations** from recent Wild Card executions and shadow runs
   (each with category, observation text, shadow_id, exec_id, timestamp).

Your job is to emit a **new, complete GLOBAL_MEMORY.md** that integrates the buffer
into the existing memory according to the rules below.

## Promotion rule

An observation enters GLOBAL_MEMORY only if it:
- Appeared in buffers from **≥ 2 distinct shadows** (cross-shadow pattern), OR
- Is explicitly marked `priority: high` in the buffer entry (Wild Card learned it)

Do NOT promote shadow-specific quirks — those belong in SHADOW_MEMORY.md.

## Consolidation rules

- **Merge**: if a buffered observation restates an existing entry → bump its `conf` by 1,
  refresh `last:`, append the shadow_id to its evidence list.
- **Add**: if genuinely new and passes the promotion rule → add with `[conf:1, last:<today>]`.
- **Contradict**: if it contradicts an existing entry of equal confidence → keep both with
  `(disputed)` marker; next run will break the tie.
- **Decay**: entries unreferenced for > 60 days with `conf < 3` → drop.
- **Budget**: total file MUST be ≤ 1000 tokens. If over budget after integration,
  evict entries by lowest `score = conf × recency` (recency = 1/(days_since_last+1)).

## Output format

Emit the **complete new GLOBAL_MEMORY.md** in raw markdown.
Begin directly with the `---` frontmatter line. No surrounding prose, no JSON, no fences.

```
---
last_consolidated: <today's ISO timestamp>
entry_count: <total bullets across all sections>
buffer_pending: 0
---

# Global Memory — Cross-Shadow Personalisation

## User Preferences
- [conf:N, last:YYYY-MM-DD, evidence: shadow_a,shadow_b] <preference text>

## Workflow Patterns
- [conf:N, last:YYYY-MM-DD, evidence: ...] <pattern text>

## Tool Affinities
- [conf:N, last:YYYY-MM-DD, evidence: ...] <affinity text>

## Recurring Variables
- [conf:N, last:YYYY-MM-DD, evidence: ...] <variable text>

## Personalisation Notes
- [conf:N, last:YYYY-MM-DD, evidence: ...] <note text>
```

If a section is empty, render: `_No entries yet._`

Keep each bullet's text ≤ 200 chars, declarative, actionable. Do not invent evidence.
