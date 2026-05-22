# Governance

This document declares the public-API contract Systemu will honor with
semver from v1.0.

## Public API surface (frozen for v1.x; modifications require RFC)

| Surface | Contract |
|---|---|
| `SKILL.md` frontmatter | Anthropic Agent Skills spec compliant; auto-migration safe for at least one minor version |
| Tool implementation contract | `def run(**kwargs) -> {"success": bool, "output_path": str, "error": str?}` |
| Shadow identity | `identity_block: str`, `available_tool_ids: list[str]`, `skill_ids: list[str]`, `execution_log: list[dict]` |
| Supervisor action vocabulary | 11 bounded actions: `DO_NOTHING` / `NUDGE` / `FORCE_REFLECT` / `RECALIBRATE_TOOL` / `RECALIBRATE_SKILL` / `SWAP_SHADOW` / `TERMINATE` / `OBSERVE` / `PROBE` / `INTERVENE` / `RESUME` |
| LLM provider plugin interface | `BaseLLMProvider` ABC + `LLMResponse` dataclass (see `systemu/llm/providers/base.py`) |
| Memory backend interface | `BaseMemoryBackend` ABC (see `systemu/runtime/memory_backends/base.py`) |
| Tool plugin protocol | A package under `plugins/<name>/` (or installed via entry-point `systemu.tools`) that exposes `register_tools(registry)` |

## RFC process

Any change that breaks the contract above requires an RFC PR:

1. Open a PR adding `docs/rfcs/<YYYY-MM-DD>-<short-name>.md`.
2. The RFC document explains: motivation, proposed change, migration
   path, backwards-compatibility window.
3. Reviewers + maintainers comment for at least **48 hours** (the "lazy
   consensus" window).  Silence = assent.
4. Disputes that block consensus escalate to the repo owner.
5. After acceptance, the implementation lands in the next minor version
   with a deprecation notice for the prior contract.  Old contract stays
   functional for one minor version after the new one ships.

## Decision-making

- Maintainers decide PRs by lazy consensus (48-hour quiet window).
- Major scope decisions (RFCs, governance changes) require explicit
  approval from at least 2 maintainers.
- Disputes that can't be resolved by lazy consensus escalate to the
  repo owner (see CODEOWNERS).
- Disputes about RFCs require a second 48-hour quiet window after any
  substantive change to the RFC text.

## Quarterly community calls

Notes posted to `docs/community-calls/<YYYY-MM>.md`.  Open agenda;
topics queued via GitHub Discussions.

## Roles + commit access

See `CONTRIBUTING.md` "Earning commit access".  Roles are tracked in
`CODEOWNERS`:

- `triage` — close stale issues, label PRs, add reviewers
- `maintain` — merge PRs, manage milestones, edit Discussions
- `owner` — transfer the repo, change governance, issue final say on
  RFC escalation
