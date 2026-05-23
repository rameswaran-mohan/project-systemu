# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once a 1.0 release is cut.  Pre-1.0 releases may include breaking
changes between minor versions; each is called out explicitly below.

## [Unreleased]

## [0.7.2] - 2026-05-23

### Changed
- **Dashboard side menu reorganised.** 14 flat nav items → 9 routes in
  3 collapsible groups (Run / Build / System). Daily-driver pages stay
  expanded by default; Build + System collapse on first load. The active
  route's group is force-expanded so the operator always sees where
  they currently are.
- `Shadow Army` label shortened to `Shadows` (URL `/army` preserved).
- The duplicate ⚙️ sidebar icon (Flywheel + Settings shared it pre-v0.7.2)
  is resolved automatically: Flywheel is no longer a top-level entry —
  it's now a tab inside `/insights` with its own 🔁 tab icon, leaving
  ⚙️ exclusively for Settings.

### Added
- **`/insights` page** with three tabs (Memory / Flywheel / Events) that
  merge the formerly-separate `/memory`, `/flywheel`, `/notifications`
  routes into one read-only analytics surface. Direct-link to a tab
  via `?tab=memory|flywheel|events`.
- **Chat is now tabbed:** `/chat` hosts both `Compose` (the existing
  free-text task entry) and `Live Events` (the former /systemu-chat
  supervisor feed). Direct-link to a tab via `?tab=compose|live`.

### Deprecated
- The standalone `/systemu-chat`, `/memory`, `/flywheel`, and
  `/notifications` URLs are preserved as thin redirect handlers that
  forward to the new tabbed parents. Bookmarks, notification deep-links,
  and recovery panel "Fix URL" links continue to work unchanged.

### Removed
- Nothing functional. This release is a visual + URL reorganisation
  only; no page logic, no vault schema, no pipeline code touched.

## [0.7.1] - 2026-05-23

### Added
- **`sharing_on capture export-skill <session_dir> --output <dir>`** — one
  command turns a finished `sharing_on record` capture into a portable
  Anthropic Agent Skill bundle. Re-uses the existing scroll-refiner +
  activity-extractor + skill-exporter pipeline; no new privileged path.
  Respects `SYSTEMU_HEADLESS=1` for non-interactive use.
- New orchestrator module `systemu/pipelines/capture_to_skill.py`
  sequencing refine_scroll → extract_and_process → export_skill, with
  idempotent reuse when a Skill already exists for the scroll.

### Changed
- **`vault.save_skill()` now emits spec-conformant SKILL.md natively.**
  On-disk layout switches from `skills/skill_<id>/SKILL.md` to
  `skills/<kebab-name>/SKILL.md` with a `metadata:` block for the
  Systemu-internal fields (category, proficiency_level, required_tools).
  The skill migrator (still runs at daemon boot) becomes a one-time
  backfill — new skills are born conformant.
- The 22 starter-vault skills under `systemu/vault/skills/` are now
  shipped in spec-conformant kebab-cased directories. The migrator no
  longer rewrites them on first boot.

### Security
- No new gates. The v0.6.8-d `tool_dep_approvals` allow-list is the same
  surface — the new `capture export-skill` command surfaces the existing
  approval prompt at export time instead of at execution time.

## [0.7.0] - 2026-05-23

First public release.

### Added

- **Distribution.** Published to PyPI as `systemu`; multi-arch (amd64 + arm64) Docker image at `ghcr.io/rameswaran-mohan/systemu`.
- **CI matrix.** Tests across Ubuntu / Windows / macOS × Python 3.10 / 3.11 / 3.12.
- **Standards conformance.** Skills published in the project's vault follow the [Anthropic Agent Skills Standard](https://agentskills.io) format; layout auto-migrates on first daemon boot.
- **Export portable Skills.** `sharing_on skills export <skill_id> --output <dir>` produces a spec-conformant Agent Skill bundle that any compatible runtime (Claude Code, ChatGPT/Codex, JetBrains Junie, AWS Kiro, etc.) can load.
- **Multi-provider LLM.** Native providers for Anthropic, OpenAI, OpenRouter, Google AI Studio, and Ollama (local).  Auto-detected from the model name; override via `SYSTEMU_TIER{1,2,3}_PROVIDER`.
- **Plugin system.** Third-party tools register via a `plugins/<name>/` directory or setuptools entry-points group `systemu.tools`.  Per-plugin error isolation.
- **Browser-Use plugin.** Opt-in (`pip install systemu[browser-use]`) — exposes 4 web tools (navigate, extract_text, click, fill_form).
- **Pluggable memory backend.** Default filesystem; Mem0 opt-in (`pip install systemu[mem0]` + `SYSTEMU_MEMORY_BACKEND=mem0`).
- **Community foundations.** GitHub Discussions enabled, contributor guide, governance charter (v1.0 API contract), good-first-issue template.

### Core features

- **Record any computer workflow** — `sharing_on record` captures screen, window, clipboard, file, and browser events and distills them into an intent-aware structured Scroll.
- **Autonomous Shadow agents** — each Scroll routes to a Shadow with its own identity, memory, tools, and a five-tier memory model.
- **Bounded-action Supervisor** — eleven discrete control actions, per-run and per-day cost ceilings, full audit log.
- **Operator recovery panel** — every blocked execution surfaces an actionable repair URL; one-click approve/install for pending dependencies, disabled tools, and consolidated memory resets.
- **Three deployment modes** — `local` (SQLite + Huey-SQLite), `docker-local` (Postgres + Huey-SQLite), `docker-enterprise` (Postgres + Redis).

[Unreleased]: https://github.com/rameswaran-mohan/project-systemu/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/rameswaran-mohan/project-systemu/releases/tag/v0.7.0
