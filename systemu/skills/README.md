# systemu/skills/ — bundled SKILL.md recipes

This directory ships near-empty by design.

**Skills are EARNED via auto-extraction (v0.9.6 L7 `auto_skill_extractor`), not PRESCRIBED.**

After every successful run with ≥2 rounds or ≥2 tool calls, the runtime asks Tier-1 LLM whether the workflow is worth capturing as a SKILL.md. If confidence ≥ `SYSTEMU_AUTO_SKILL_EXTRACT_MIN_CONFIDENCE` (default 0.6), a candidate SKILL.md gets written to `SYSTEMU_SKILLS_USER_DIR` (operator-configurable). Over time, the user's skill library grows organically from their actual usage patterns.

We ship one genuinely generic starter (`summarize-page`) and a README. Two earlier overfit examples (`burrito-delivery` and `find-nearby`) live at `docs/skill-examples/` as format references.

This follows the code-side capability-registry plus auto-skill-extraction design: agents ship generic CAPABILITIES (file ops, web fetch, terminal, send_message, etc. — see `systemu/runtime/tools/` for the v0.9.3+ code-registered batch), then build a skill library from successful runs. Recipes that anticipate use cases will overfit; recipes that emerge from real usage scale.

## Where skills come from

1. **Auto-extraction (`v0.9.6 L7` — primary source)**: `auto_skill_extractor.extract_skill_candidate()` runs after every completed run that meets threshold, persists qualifying candidates via `persist_skill_candidate()`.
2. **User-installed** (`SYSTEMU_SKILLS_USER_DIR`): operators can drop their own SKILL.md files in a configured directory.
3. **Bundled** (this directory): kept intentionally minimal. Reserved for truly generic capabilities that benefit every user regardless of domain.

## Format reference

See `docs/skill-examples/` for examples of the SKILL.md shape — YAML frontmatter (name, description, version, platforms, prerequisites, metadata.systemu.{tags, related_skills}, requires_toolsets, fallback_for_toolsets) + markdown body (## When to Use / ## When NOT to Use / ## Procedure / ## Pitfalls / ## Verification).
