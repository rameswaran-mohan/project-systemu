# Systemu

> **The open-source way to record any computer workflow, export it as a portable Anthropic Agent Skill, and run it locally with a supervised agent runtime.**

<!-- hero gif placeholder -->
<!-- ![Systemu dashboard](assets/dashboard-hero.gif) -->
<!-- Until the gif is recorded, this comment block keeps the README rendering clean on github.com. -->

## Install

```bash
pip install systemu                                                       # PyPI (post-tag)
docker run -p 8765:8765 ghcr.io/rameswaran-mohan/systemu:0.7.0            # Docker (post-tag)
```

Open the dashboard at <http://localhost:8765>.

> New to the vocabulary? See [docs/glossary.md](docs/glossary.md) for a Systemu → industry-terms map.

---

[![tests](https://github.com/rameswaran-mohan/project-systemu/actions/workflows/test.yml/badge.svg)](https://github.com/rameswaran-mohan/project-systemu/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

> **An AI runtime that turns recorded workflows into autonomous agents you operate from a dashboard.**

Systemu combines two components:

- **Sharing-On** (`sharing_on`) records what you do on your computer
  and converts the recording into a structured workflow specification
  using an LLM.
- **Systemu runtime** executes that specification autonomously through
  AI **Shadow** agents, a curated tool registry, and a NiceGUI
  dashboard.

**📚 Read these first:**
[Getting Started](docs/getting-started.md) ·
[Architecture](ARCHITECTURE.md) ·
[User Guide](USER_GUIDE.md) ·
[Contributing](CONTRIBUTING.md) ·
[Governance](docs/governance.md)

---

## How it works

```
You perform a task on your computer
          │
          ▼
Sharing-On records: screenshots, window switches,
  file changes, clipboard, process events
          │
          ▼
Intent extractor (Tier-2 LLM) infers what you
  actually wanted — written to intent.json, not
  inferred from the click sequence
          │
          ▼
Scroll refiner turns the intent + abstracted
  steps into a structured Scroll with objectives
          │
          ▼
Pre-flight scroll validator (opt-in) checks
  satisfiability + intent-vs-tool fit;
  surfaces a side-by-side remediation card
  with a proposed_revision when blocked
          │
          ▼
Activity extractor selects tools and skills
  via data-flow reasoning (schemas in headers,
  not just keyword name match)
          │
          ▼
Missing tools forged with intent context →
  dry-run validation gate (Gate 3.5)
          │
          ▼
Shadow decision picks an existing specialist OR
  creates a new one, scoring on semantic intent
  match plus skill/tool ID overlap
          │
          ▼
Supervisor dispatches the Shadow.  Intelligent
  Supervisor (opt-in) intervenes between
  iterations with bounded actions including
  RECALIBRATE_TOOL / RECALIBRATE_SKILL when
  capabilities are structurally inadequate
          │
          ▼
Dashboard shows live progress, results,
  per-shadow + per-tool metrics, memory, and
  approval cards for any operator decisions
```

A deeper walkthrough of every stage lives in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Prerequisites

### Resource minimums (verified during the manual smoke run)

| Resource    | `local`        | `docker-local` | `docker-enterprise`              |
|-------------|----------------|----------------|----------------------------------|
| CPU cores   | 2              | 2              | 4                                |
| Free RAM    | 4 GB           | 6 GB           | 8 GB (Redis + Postgres + workers)|
| Free disk   | 2 GB           | 4 GB           | 6 GB                             |
| Network     | LLM API access | + Postgres     | + Redis                          |

### Software

| Requirement   | Version | Notes |
|---------------|---------|-------|
| Python        | 3.10+ (3.12 tested) | Required for all modes |
| pip           | latest  | `pip install --upgrade pip` |
| Git           | 2.30+   | Required for `./install.sh` |
| Docker        | 24+ / Desktop 4.x | Required for `docker-local` and `docker-enterprise` |
| Node.js + npm | 18+     | Optional — only for the Chrome capture extension |

### OS support

| OS                  | Native capture | docker-* modes  |
|---------------------|----------------|-----------------|
| Windows 10 / 11     | ✅ verified    | ✅ verified     |
| macOS 13+           | ⚠️ partial     | ✅              |
| Ubuntu 22.04+       | ⚠️ needs `xdotool xclip` | ✅      |

**Linux capture extras:**

```bash
sudo apt install xdotool xclip      # Debian / Ubuntu
sudo dnf install xdotool xclip      # Fedora
```

### LLM access

You need at least one of:

- An [OpenRouter](https://openrouter.ai) key (free tier works)
- A [Google AI Studio](https://aistudio.google.com) key (free)
- A local Ollama instance reachable on `:11434`

---

## Quick Start

Full walkthrough lives in [docs/getting-started.md](docs/getting-started.md).
The headline:

```bash
git clone https://github.com/rameswaran-mohan/project-systemu.git
cd project-systemu
./install.sh        # Linux/macOS    (or  install.bat  on Windows)
./start.sh          # Linux/macOS    (or  start.bat    on Windows)
```

`install.sh` asks which deployment mode you want and sets everything up. Three options:

| Mode | What you get | Best for |
|---|---|---|
| **local** | Native venv. Daemon + worker run as detached subprocesses. SQLite vault + Huey-SQLite broker. | Single-machine dev / personal use. |
| **docker-local** | docker-compose. Postgres vault + Huey-SQLite broker. One worker container. | Hobbyist self-hosting on one box. |
| **docker-enterprise** | docker-compose. Postgres vault + Redis broker. N worker containers (scale via `WORKER_REPLICAS`). | Production / multi-host. |

The dashboard runs at [http://localhost:8765](http://localhost:8765) in every mode.
`./stop.sh` (or `stop.bat`) shuts everything down cleanly.

To re-run installer after changing your mind: `./install.sh` will detect the existing
install and offer **reconfigure** / **upgrade-deps** / **quit**.

To upgrade an existing install to the latest release: `./update.sh`
(or `update.bat`). It stops the daemon, `git pull --ff-only`s, reinstalls deps,
runs alembic migrations, and restarts. Pass `--yes` / `/y` for non-interactive
CI / cron usage. Refuses on a dirty working tree.

### Non-interactive install (CI / automation)

```bash
./install.sh --mode docker-enterprise --non-interactive \
    --pg-password=hunter2 --redis-password=hunter3 \
    --worker-replicas=4 \
    --openrouter-key=sk-... --google-key=AIza...
```

### Record a workflow (optional)

After `./start.sh`:

```bash
sharing_on record --name "My workflow"
# Press Ctrl+C when done — Systemu converts the recording into a Scroll
```

### Export a recorded workflow as a portable Agent Skill

Once a recording has been analyzed, one command turns it into a portable
[Anthropic Agent Skill](https://www.anthropic.com/news/skills) bundle that
any Agent-Skills-compatible runtime (Claude Code, Cursor, etc.) can load:

```bash
sharing_on capture export-skill ./captures/<your_session_dir> \
           --output ./my-skill
# -> ./my-skill/<kebab-name>/SKILL.md
```

Validate the bundle with `skills-ref validate ./my-skill/<kebab-name>`.

For Redis topologies beyond the default standalone (TLS, Sentinel, custom CA),
see [`docs/redis-topologies.md`](docs/redis-topologies.md).

---

## Configuration Reference

All settings go in your `.env` file. Copy `.env.example` as a starting point.

### API Keys

You need at least one provider key.  The router auto-detects which
provider to use from the model name (see [LLM Models](#llm-models) below).

| Variable | When required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Default models (deepseek-v4-flash) | OpenRouter key.  Free tier available at [openrouter.ai](https://openrouter.ai). |
| `ANTHROPIC_API_KEY` | Only if a tier is configured for a `claude-*` model | Direct Anthropic API.  Get one at [console.anthropic.com](https://console.anthropic.com). |
| `OPENAI_API_KEY` | Only if a tier is configured for a `gpt-*` or `o1-*` / `o3-*` model | Direct OpenAI API. |
| `GOOGLE_API_KEY` | Only if a tier is configured for a `gemini-*` model | Google AI Studio key — free at [aistudio.google.com](https://aistudio.google.com). |
| `OLLAMA_URL` | Only if a tier is configured for an `ollama/*` model | Defaults to `http://localhost:11434`. |

### LLM Models

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_TIER1_MODEL` | `deepseek/deepseek-v4-flash` | Deep reasoning — scroll refinement, shadow decisions |
| `SYSTEMU_TIER2_MODEL` | `deepseek/deepseek-v4-flash` | Structured output — tool forge, execution planning |
| `SYSTEMU_TIER3_MODEL` | `z-ai/glm-4.5-air:free` | Fast formatting — log-to-instructions conversion |
| `SYSTEMU_TIER1_PROVIDER` | _(auto from model)_ | Force a specific provider: `openrouter` \| `anthropic` \| `openai` \| `google` \| `ollama` |
| `SYSTEMU_TIER2_PROVIDER` | _(auto from model)_ | Same as above |
| `SYSTEMU_TIER3_PROVIDER` | _(auto from model)_ | Same as above |
| `SHARING_ON_MODEL` | `z-ai/glm-4.5-air:free` | LLM used during sharing_on analysis |

### Storage

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_STORAGE` | `file` | Backend: `file` (JSON vault), `sqlite`, or `postgres` |
| `SYSTEMU_DATABASE_URL` | _(empty)_ | SQLAlchemy URL — required for `sqlite` or `postgres` mode |
| `SYSTEMU_VAULT_DIR` | `systemu/vault` | Path to JSON vault (file mode only) |

### Queue

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_QUEUE` | _(empty)_ | Leave empty for the in-process Supervisor queue. Set `huey` to route through Huey. |
| `SYSTEMU_QUEUE_BROKER` | `sqlite` | Huey broker selection: `sqlite` (default) or `redis`. |
| `SYSTEMU_REDIS_URL` | _(empty)_ | Required when `SYSTEMU_QUEUE_BROKER=redis`. e.g. `redis://:pass@redis:6379/0` |
| `HUEY_WORKERS` | `4` | Huey thread count per worker process. |
| `WORKER_REPLICAS` | `2` | docker-enterprise only — number of worker containers. |
| `SYSTEMU_DB_BIND` | `127.0.0.1:5432` (docker-local) / empty (docker-enterprise) | Docker modes only. Host bind for the Postgres container. Required for `sharing_on record` from host to reach the container's vault. Loopback-only by default. Set to `0.0.0.0:5432` to expose on all interfaces (NOT recommended on shared hosts). To fully unpublish in docker-local, remove the `ports:` section via `docker-compose.override.yml`. |

### Deployment mode

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_MODE` | `local` | `local` \| `docker-local` \| `docker-enterprise` — written by `install.py`; `start.sh`/`start.bat` read it |
| `SYSTEMU_DASHBOARD_HOST` | _(unset → 127.0.0.1)_ | Bind host for the NiceGUI dashboard |
| `SYSTEMU_DASHBOARD_PORT` | `8765` | Dashboard port |
| `SYSTEMU_HEADLESS` | _(unset)_ | When `1`, forces non-interactive mode at the `notify_user` layer (same effect as `SYSTEMU_NON_INTERACTIVE`) |
| `SYSTEMU_OUTPUT_DIR` | `~/Documents` | Where Shadow-generated files are saved |
| `SYSTEMU_EXECUTION_RETENTION` | _(unset)_ | Max execution audit dirs to keep on disk; older pruned during save |

### Behaviour & approval

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_NON_INTERACTIVE` | `false` | Auto-pick `actions[0]` (the safe-by-default choice) in every `notify_user` prompt. |
| `SYSTEMU_AUTO_FORGE_TOOLS` | `false` | **Dev only** — auto-enables LLM-generated tools without review (bypasses Gate 2/3) |
| `SYSTEMU_APPROVAL_TIMEOUT` | _(unset)_ | Seconds before a queued approval auto-resolves (sqlite_approval_gate) |

### Tool execution

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_TOOL_BACKEND` | `local` | `local` \| `docker` \| `ssh` \| `wsl` (ssh/wsl are stubs) |
| `SYSTEMU_DOCKER_TOOL_TIMEOUT` | `300` | Per-tool timeout (seconds) when `SYSTEMU_TOOL_BACKEND=docker` |
| `SYSTEMU_TOOL_DEP_INSTALL_MODE` | `auto` | `auto` \| `off` \| `prompt` \| `always` — how the runtime handles tool pip deps |
| `SYSTEMU_PREWARM_TOOL_DEPS` | `false` | Install all deployed-tool deps on daemon start instead of on first call |

### Intelligent Supervisor

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_INTELLIGENT_SUPERVISOR` | `false` | Master kill switch for the Tier-1/2/3 intervention layer + scroll validator |
| `SYSTEMU_MAX_CONSECUTIVE_THINK` | `5` | Hard cap on THINK-only iterations before the supervisor force-reflects |
| `SYSTEMU_SUPERVISOR_CADENCE` | `auto` | How often the supervisor evaluates — `auto` \| `every` \| `slow` |
| `SYSTEMU_SUPERVISOR_TIMEOUT_S` | `5.0` | Per-directive LLM timeout |
| `SYSTEMU_SUPERVISOR_BUDGET_RUN` | `10` | Max supervisor LLM calls per shadow run |
| `SYSTEMU_SUPERVISOR_BUDGET_HOUR_USD` | `5.0` | Hourly USD ceiling for supervisor LLM spend |
| `SYSTEMU_SUPERVISOR_BUDGET_DAY_USD` | `50.0` | Daily USD ceiling |
| `SYSTEMU_SUPERVISOR_TIER_ROUTINE` | `tier_3` | Tier used for routine supervisor checks |
| `SYSTEMU_SUPERVISOR_TIER_INTERVENTION` | `tier_1` | Tier used for high-stakes interventions |

### Pre-flight validators

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_SCROLL_VALIDATOR` | _(off; on when supervisor on)_ | Run the intent-aware scroll validator before activity extraction |
| `SYSTEMU_SKILL_VALIDATOR` | _(off; on when scroll validator on)_ | Run the GUI-codification skill validator at extraction time |

### Recalibration auto-approve

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_AUTO_APPROVE_LOW_RISK_RECAL` | `false` | Auto-apply low-risk **tool** recalibrations (fork-mode + dry-run passed + confidence=high + non-destructive). Otherwise surfaces operator card on `/tools`. |
| `SYSTEMU_AUTO_APPROVE_LOW_RISK_SKILL_RECAL` | `false` | Auto-apply low-risk **skill** recalibrations (fork-mode + confidence=high + no `side_effect` in `produces` + non-destructive name). Otherwise surfaces operator card on `/skills`. |

### Persona defaults

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_PERSONA_CREATIVITY` | `50` | Default persona dial (0–100) when shadows are auto-created |
| `SYSTEMU_PERSONA_PROFESSIONALISM` | `50` | Default persona dial |
| `SYSTEMU_PERSONA_TECHIE` | `50` | Default persona dial |
| `SYSTEMU_PERSONA_THINKING` | `50` | Default persona dial |

### sharing_on Capture

| Variable | Default | Description |
|---|---|---|
| `SHARING_ON_SCREENSHOT_INTERVAL` | `3` | Seconds between screenshots |
| `SHARING_ON_SCREENSHOT_WIDTH` | `1280` | Max screenshot width (pixels) |
| `SHARING_ON_TELEGRAM_BOT_TOKEN` | _(unset)_ | Optional — when set, the daemon spins up a Telegram bot for chat-based submissions + approvals. See [docs/messaging.md](docs/messaging.md) |
| `SHARING_ON_TELEGRAM_ALLOWED_USER_IDS` | _(unset)_ | Required when bot token is set — strict allowlist (refuses to start if empty) |

---

## Storage Modes

`install.py` writes `SYSTEMU_STORAGE=sqlite` to `.env` for `local` mode and `postgres` for `docker-local` / `docker-enterprise`. The in-process default when no env is set is `file` (kept for backward compat with pre-installs).

### `SYSTEMU_STORAGE=sqlite` (default for `local` mode)

- SQLite database at `SYSTEMU_DATABASE_URL`, e.g. `sqlite:///./data/systemu.db`
- Durable task queue with crash recovery + orphan requeue
- Dashboard and worker run as separate processes
- Alembic migrations run automatically on first start
- Recommended for single-machine deployments

### `SYSTEMU_STORAGE=postgres` (default for `docker-local` / `docker-enterprise`)

- PostgreSQL backend (managed by docker-compose)
- Multi-machine / multi-worker deployments
- Same Alembic migrations as SQLite

### `SYSTEMU_STORAGE=file` (legacy)

- State stored as JSON files in `systemu/vault/`
- Zero external dependencies
- Kept for backward compatibility; use the migration tool below to move to SQLite or Postgres

**Migrating from file → SQLite or Postgres:**

```bash
SYSTEMU_STORAGE=sqlite SYSTEMU_DATABASE_URL=sqlite:///./data/systemu.db \
  python -m systemu.migrations.json_to_db --source ./systemu/vault --dry-run
```

See the `Migrating from a pre-pivot install` section below for the Postgres path.

---

## Project Structure

```
project-systemu/
├── sharing_on/                         — Capture engine + analyser
│   ├── collectors/                       — Screen, clipboard, file, window monitors
│   ├── analyzer/                         — Step detector, narrative generator
│   │   ├── intent_extractor.py  — Tier-2 pre-pass that infers
│   │   │                                     outcome-oriented intent before the
│   │   │                                     narrative LLM runs (intent.json)
│   │   └── prompts/                        — Analyzer prompt library
│   ├── output/                           — instructions.md renderer
│   └── cli.py                            — `sharing_on` command entry point
│
├── systemu/                            — Systemu runtime
│   ├── core/                             — Pydantic models (Shadow, Scroll,
│   │                                       Activity, Tool, Skill, Objective…)
│   ├── pipelines/                        — Stage 1→6 transformations
│   │   ├── scroll_refiner.py               — Stage 2 — intent + objectives
│   │   ├── scroll_validator.py             — Pre-flight intent-aware check
│   │   ├── scroll_remediator.py  — side-by-side fix card
│   │   ├── activity_extractor.py           — Stage 3 — schema-aware extraction
│   │   ├── skill_validator.py  — GUI-codification check
│   │   ├── skill_recalibrator.py  — re-author instructions_md
│   │   ├── tool_forge.py                   — Spec → code → save (Gate 1/2)
│   │   ├── tool_dry_run.py  — Gate 3.5 validation
│   │   ├── tool_recalibrator.py  — bump-vs-fork pipeline
│   │   ├── tool_inadequacy_diagnosis.py  — supervisor diagnosis
│   │   ├── shadow_decision.py              — Stage 5 — intent-aware tiebreak
│   │   ├── refinery.py                     — Post-execution memory consolidation
│   │   ├── evolution_engine.py             — Long-term shadow/skill evolution
│   │   ├── memory_consolidator.py          — Tiered memory consolidation
│   │   ├── cross_shadow_patterns.py        — Promotion of recurring lessons
│   │   └── workshop_module.py              — Operator-driven scroll/shadow edit
│   ├── runtime/                          — Shadow ReAct loop + Supervisor
│   │   ├── shadow_runtime.py               — Per-shadow execute loop
│   │   ├── supervisor.py                   — Activity queue + worker pool
│   │   ├── execution_mind.py               — Intelligent Supervisor
│   │   ├── execution_snapshot.py  — true snapshot resume
│   │   ├── failure_classifier.py           — 10-category failure taxonomy
│   │   ├── tool_metrics.py / shadow_metrics.py — per-id telemetry
│   │   ├── affinity_log.py                 — Activity-shadow routing memory
│   │   ├── inadequacy_tracker.py           — Cross-shadow tool-inadequacy clustering
│   │   ├── rejection_store.py              — Operator-feedback learning
│   │   ├── tool_sandbox.py                 — Subprocess / docker / wsl / ssh exec
│   │   └── tool_registry.py                — Runtime tool loader
│   ├── interface/                        — NiceGUI dashboard + REST API
│   │   └── pages/                          — /chat, /scrolls, /tools, /skills, /workflow
│   ├── messaging/                        — Optional Telegram gateway
│   ├── prompts/                          — Tier-1/2/3 prompt library
│   ├── queue/                            — In-process / SQLite / Redis priority queues
│   ├── storage/sqlite/                   — SQLite + Postgres vault (SQLAlchemy)
│   ├── vault/                            — File-based vault + starter pack
│   │   ├── tools/                          — Starter tool implementations
│   │   ├── shadow_army/                    — Starter Shadow configurations
│   │   └── skills/                         — Starter SKILL.md files (Anthropic
│   │                                         Agent Skills Standard compatible)
│   ├── scheduler/                        — Daemon + recurring jobs
│   └── worker.py                         — Background worker entry point
│
├── alembic/versions/                   — DB schema migrations (0001–0007)
├── extension/                          — Chrome extension for web-event capture
├── docs/                               — Architecture, getting-started, messaging
├── tests/                              — pytest suite
├── docker-compose.yml
├── Dockerfile
├── install.py / install.sh / install.bat
├── start.sh / start.bat / stop.sh / stop.bat
└── .env.example
```

---

## sharing_on Capture

sharing_on records what you do and produces:

```
captures/
└── my_task_cap_YYYYMMDD_HHMMSS/
    ├── instructions.md       ← Step-by-step workflow guide
    ├── session.json          ← Session metadata
    ├── events.db             ← Raw captured events
    └── assets/               ← Screenshots embedded in instructions.md
```

The `instructions.md` is converted into a Systemu **Scroll** when you submit the capture to the dashboard.

**Privacy:** keystrokes are NOT recorded; clipboard auto-redacts secrets; no data leaves your machine until the LLM analysis step.

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Apply database migrations
alembic upgrade head

# Generate a new migration after model changes
alembic revision --autogenerate -m "describe_change"
```

---

## Contributing

Pull requests are welcome — from humans **and** AI agents.  See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution flow,
including the explicit guidelines for AI-authored PRs.

* Report bugs / suggest features → [issue tracker](https://github.com/rameswaran-mohan/project-systemu/issues)
* Security disclosures → [`SECURITY.md`](SECURITY.md)
* Community expectations → [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
* Release notes → [`CHANGELOG.md`](CHANGELOG.md)

---

## Project status

Pre-1.0.  Current release: **v0.7.0** — see [CHANGELOG](CHANGELOG.md) for what shipped.

### What's next

The next-phase work is open for design.  Likely candidates (not yet scheduled):

- Auto-recalibration without operator approval for low-risk **skill** patterns (telemetry-gated promotion)
- Visual workflow editor on the dashboard (today's UI is read-mostly)
- Multi-tenant deployment + per-operator vaults
- Hosted catalog of community-contributed tools / skills

If you want to contribute, [`CONTRIBUTING.md`](CONTRIBUTING.md) is the contribution flow.

---

## Troubleshooting

Common operator-environment issues and their fixes.

### Windows — "The system cannot find the drive specified" during `start.bat`

Cosmetic stderr from cmd.exe or PowerShell walking PATH when it contains a stale entry pointing to an unmounted drive (typically an old mapped network drive).  Doesn't affect daemon startup.

Diagnose:

```powershell
$env:Path -split ";" | ForEach-Object {
    if ($_ -match "^[A-Z]:") {
        $drive = $_.Substring(0, 2)
        if (-not (Test-Path $drive)) { Write-Output "STALE: $_" }
    }
}
```

Fix: remove the offending entry from System Properties → Environment Variables → PATH.

### Windows — PowerShell ExecutionPolicy blocks start.bat

`start.bat` spawns daemon + worker via embedded PowerShell `Start-Process`.  On corporate-locked machines this can be blocked by Group Policy even with `-ExecutionPolicy Bypass`.

Diagnose:

```powershell
Get-ExecutionPolicy -List
```

Fix: ask your IT department to whitelist the project directory, OR run start.bat from an elevated terminal where execution policy is unrestricted.

### Linux — Capture records empty event streams (Wayland)

Symptom: `sharing_on record` runs, produces a session folder, but `events.db` is empty or near-empty.  Dashboard works.

Cause: pynput requires X11.  Ubuntu 22.04+ and Fedora Workstation default to Wayland.

Fix: log out and select an X11/Xorg session at the login screen (gear icon next to the password field).  Daemon, dashboard, and tool execution work fine on Wayland — only capture is affected.

### Linux — Missing capture deps (xdotool / xclip)

Symptom: capture produces some events but clipboard/keyboard events are empty.

Fix:

```bash
sudo apt install xdotool xclip      # Debian / Ubuntu
sudo dnf install xdotool xclip      # Fedora
```

`install.py` warns about these at install time but doesn't auto-install (sudo prompt would block the installer).

### Stale `SYSTEMU_AUTO_APPROVE_SCROLLS` in `.env` after upgrade

Symptom: you set `SYSTEMU_AUTO_APPROVE_SCROLLS=true` expecting non-interactive mode; the daemon prompts you anyway.

Cause: the env var was renamed to `SYSTEMU_NON_INTERACTIVE` .  Hard cut, no alias.

Fix: edit `.env`, replace `SYSTEMU_AUTO_APPROVE_SCROLLS` with `SYSTEMU_NON_INTERACTIVE`, restart the daemon.

`install.py` and the daemon both emit warnings when the old key is detected.

### Daemon crashes with `OperationalError: no such column`

Symptom: dashboard loads but every page returns 500; `logs/daemon.log` shows `sqlalchemy.exc.OperationalError: no such column: ...`.

Cause: DB schema is behind the code.  Happens when you `git pull` a release with a new migration but skip re-running `install.py`.

Fix: `start.bat` / `start.sh` auto-runs `alembic upgrade head` on every start.  If you're on an older start script:

```bash
python scripts/upgrade_db.py
```

Or just re-run `install.bat` / `./install.sh` — it migrates as part of setup.

### macOS — capture silently records empty events

Symptom: install completes, daemon runs, but sharing_on session captures contain empty event streams.

Cause: macOS requires explicit Accessibility (for pynput keyboard/clipboard) and Screen Recording (for screenshots) grants.

Fix:
1. System Settings → Privacy & Security → **Accessibility** → click +, add Terminal (or whichever app runs `./start.sh`)
2. System Settings → Privacy & Security → **Screen Recording** → click +, add Terminal
3. Restart the daemon: `./stop.sh && ./start.sh`

`install.py` prints this guide automatically on macOS; the daemon does not detect the missing grant at runtime.

### `Python 3.10+ required` on Debian 11 / older systems

Symptom: `install.py` exits with `Python 3.10+ required (you have 3.9)`.

Cause: Debian 11 ships 3.9 by default; Python 3.10+ is required.

Fix: install 3.11 from the system package manager:

```bash
sudo apt install python3.11 python3.11-venv       # Debian / Ubuntu
sudo dnf install python3.11                       # Fedora / RHEL
brew install python@3.11                          # macOS
winget install Python.Python.3.11                 # Windows
```

Then re-run with the new interpreter: `python3.11 install.py`. Newer install.py revisions print these hints automatically.

### `Invalid key (HTTP 401 from OpenRouter)` during install

Symptom: install.py rejects the OpenRouter key with a 401 message and re-prompts.

Cause: the key was mistyped, revoked, or doesn't have model access enabled.

Fix: generate a fresh key at <https://openrouter.ai/keys> — the installer probe-validates it before writing to `.env`. After 3 attempts the installer stores the key anyway; correct it manually in `.env` later, then restart the daemon.

### Behind a corporate proxy

Symptom: install hangs at `Upgrading pip …`, `Installing dependencies …`, or `Validating OpenRouter key …`.

Cause: pip, Playwright, and the OpenRouter validator all need `HTTP_PROXY` / `HTTPS_PROXY` env vars set.

Fix: export the vars before running `install.py`:

```bash
export HTTPS_PROXY=http://user:pass@proxy.corp.example:3128
export HTTP_PROXY=http://user:pass@proxy.corp.example:3128
python install.py
```

```powershell
# Windows PowerShell
$env:HTTPS_PROXY = "http://user:pass@proxy.corp.example:3128"
$env:HTTP_PROXY = "http://user:pass@proxy.corp.example:3128"
python install.py
```

`install.py` echoes the detected proxy URL (with password masked) at the top of the install log. If no proxy line appears, the vars weren't exported into the shell that ran `install.py`.

### Apple Silicon (M1 / M2 / M3 / M4) — install or Playwright errors

Symptom: install or Playwright fails with architecture-mismatch errors on an M-series Mac.

Cause: some PyObjC-using deps or Chromium binaries lag the ARM64 build cycle.

Fix: re-run install under Rosetta:

```bash
arch -x86_64 python install.py
```

`install.py` prints an info banner on Apple Silicon listing this and other known caveats. Most installs complete natively without intervention.

### Docker mode — captured scroll never appears on dashboard

Symptom: `sharing_on record` completes, you see `intent.json` + `instructions.md` in the capture directory, but no scroll lands on `/scrolls`.

Cause: the host's `analyze` cannot reach the container's Postgres.

Fix: confirm `SYSTEMU_DB_BIND` is set in `.env`:

- **docker-local** (default): `SYSTEMU_DB_BIND=127.0.0.1:5432` — loopback-only binding. Pre-installs and operators who manually edited `.env` may have this missing. Re-run `install.py --mode docker-local` to refresh.
- **docker-enterprise**: not published by default. To enable for development, set `SYSTEMU_DB_BIND=127.0.0.1:5432` AND add a `ports:` block to the `postgres` service via a `docker-compose.override.yml`. Not recommended for production.

After editing: `docker compose down && docker compose --profile <local|enterprise> up -d`.

### Docker mode — dashboard shows different scrolls than the worker writes (pre-only)

Symptom: dashboard `/scrolls` lists fewer scrolls than `psql` shows in Postgres. Activities in the database are not visible in the dashboard's activity feed.

Cause: pre-dashboard fell back to FileVault when `SYSTEMU_REDIS_URL` was missing (docker-local intentionally has no Redis). Dashboard wrote to `/data/vault/*.json` while the worker wrote to Postgres. Split-brain.

Fix: upgrade to v0.7.0 via `./update.sh` (or `update.bat`). The AppState fix ([commit on the public repo](#)) gates the Redis URL requirement on `SYSTEMU_QUEUE_BROKER=redis` (enterprise only).

### Docker mode — elder/shadow memory disappears after `docker compose down -v` (pre-only)

Symptom: every container rebuild loses all consolidated learnings. `ELDER_MEMORY.md` and `shadow_<id>/memory/` files are empty on the new container.

Cause: pre-SqliteVault defaulted `memory_dir` to `/tmp/systemu_memory` for Postgres URLs. `/tmp` in a container is the writable layer, not a volume mount, so it's lost on rebuild.

Fix: upgrade to v0.7.0. The new default is `${SYSTEMU_VAULT_DIR}/memory`, which is volume-mounted and persistent.

---

## License

MIT — see [`LICENSE`](LICENSE).
