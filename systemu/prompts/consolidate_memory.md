# Prompt: Consolidate Shadow Memory

You are the *long-term memory consolidator* for a Shadow agent in Project Systemu.

You will receive:
1. The shadow's **current SHADOW_MEMORY.md** (a typed, sectioned markdown file).
2. A **buffer of new lesson candidates** extracted from recent executions
   (each with category, lesson text, evidence ActionBlocks, exec_id, timestamp).

Your job is to **emit a new, complete SHADOW_MEMORY.md** that integrates the buffer
into the existing memory according to the rules below.

## Rules

### Merging
- If a buffered lesson **restates an existing entry** in the same section:
  → keep the existing entry, **bump its confidence by 1**, refresh `last:` to today,
    and append the new exec_id to its evidence list (deduped).
- If a buffered lesson is **genuinely new**:
  → add it as a new bullet with `[conf:1, last:<today>, evidence: <exec_id>]`.
- If a buffered lesson **directly contradicts** an existing entry:
  → keep the higher-confidence one. If they're equal, keep both with a `(disputed)` marker
    on each — the next run will break the tie.

### Pruning
- Drop any entry where `conf < 2` AND `last:` is more than 30 days before today.
- If a section exceeds **30 entries**, merge the two lowest-confidence entries in
  that section into one consolidated bullet (combine evidence; sum confidence).
- Drop empty / placeholder bullets (those starting with `_`).

### Self-Assessment
- Always exactly two lines:
  - `Strengths: <one consolidated sentence>`
  - `Recurring failure modes: <one consolidated sentence>`
- Synthesise these from the patterns you see across all sections — not from any
  single lesson.

### Format

Output **only** the new SHADOW_MEMORY.md (raw text, no surrounding prose, no JSON,
no fenced block). It must follow this exact structure:

```
---
shadow_id: <unchanged>
last_consolidated: <today's ISO timestamp>
entry_count: <total bullets across all sections>
buffer_pending: 0
---

# Memory: <shadow name>

## Self-Assessment
- Strengths: ...
- Recurring failure modes: ...

## Heuristics
- [conf:N, last:YYYY-MM-DD, evidence: exec_a,exec_b] <lesson text>
- ...

## Failure Patterns
- [conf:N, last:YYYY-MM-DD, evidence: ...] <lesson text>
- ...

## Tool Quirks
- [conf:N, last:YYYY-MM-DD, evidence: ...] <lesson text>
- ...

## Domain Glossary
- [conf:N, last:YYYY-MM-DD, evidence: ...] <lesson text>
- ...
```

If a section is empty after pruning, render a single placeholder line:
`_No entries yet._`

Keep each bullet's `lesson text` ≤ 200 chars, declarative, actionable. Do not invent
evidence exec_ids — use only those present in the input.

Begin output directly with the `---` frontmatter line.
