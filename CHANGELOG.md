# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once a 1.0 release is cut.  Pre-1.0 releases may include breaking
changes between minor versions; each is called out explicitly below.

## [Unreleased]

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
