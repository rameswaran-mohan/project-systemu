# Systemu

> **Teach your computer your job — by doing it once.**
> Record any task on your screen. Systemu turns the recording into a
> repeatable workflow, staffs it with an AI specialist, and runs it under
> your approval — every action gated, logged, and local.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

## Why Systemu

Chat assistants wait to be asked, and agent frameworks need you to write
skills by hand. Systemu learns the way a colleague does — **by watching
you work**. Do the task once; it writes the step-by-step playbook, asks
your approval, and from then on it's one click to run again. That makes
it the LLM-native answer to RPA: no selectors, no scripts, no consultant.

And unlike the viral personal-assistant agents, Systemu is built for work
that has consequences:

* **Approval gates with un-automatable floors** — installing packages,
  running freshly written code, destructive steps: each lands as one card
  in your Inbox with a plain-English summary and a safe default. Most
  approvals are one click; none are silent.
* **Local-first** — your recordings, workflows, memory, and results live
  in a vault on your machine. API keys are never typed into the browser.
* **Honest by construction** — tool results are verified (a no-output
  call is a failure, not a phantom success), outcomes report file paths
  you can open, and "couldn't do it" is never dressed up as done.

## Quick start

```bash
pip install systemu
```

In your chosen working directory:

```bash
sharing_on init           # seeds the starter catalog (41 tools, idempotent)
sharing_on setup          # pick your LLM provider + model preset, store keys securely
sharing_on daemon start
```

`sharing_on setup` walks you through choosing a provider (OpenRouter, Google,
OpenAI, Anthropic, or a local Ollama) per tier and stores the keys in a local
`.env` — entered hidden, never echoed, never typed into a browser. Skip it and
`daemon start` runs the same flow on first launch.

Open <http://localhost:8765>. A short setup wizard and guided tour take it from
there: confirm your models, say who you are, run a starter task — then hit
**Record** and teach it something real.

**The one-page guide:** [OPERATOR-SOP.md](OPERATOR-SOP.md) — the
record → approve → run → results loop, what each approval card means, and
a troubleshooting table. New to the vocabulary?
[docs/glossary.md](docs/glossary.md) maps Systemu terms to industry ones.

Docker (Postgres-backed) and enterprise (Redis-scaled) modes:

```bash
git clone <this repo> && cd <repo>
python install.py --mode docker-local     # or docker-enterprise
```

## What's in the box

- **Sharing-On** (`sharing_on`) — the capture engine: records screenshots,
  window switches, file changes, and input while you demonstrate a task,
  then turns the recording into accurate plain-English instructions.
- **Systemu runtime** — executes workflows through AI **Shadow** agents
  (specialists created per job, with your approval), a curated 41-tool
  registry that works out of the box, MCP connector support, episodic
  memory, and an evolution engine that proposes improvements from real runs.
  A Reverse-Harness Governor arbitrates the capabilities a running agent asks
  for, writes a per-run decision-audit trail, and — opt-in — can fan a
  decomposed goal out to a bounded fleet of parallel sub-agents.
- **Bring your own model** — choose a provider per tier: OpenRouter, Google,
  OpenAI, Anthropic (native SDK), or a local **Ollama** (keyless, on-device).
  Presets — budget / balanced / quality — set the cost/quality dial in one
  keystroke; `sharing_on setup` or Settings stores the keys.
- **The dashboard** — a command center: **Home · Work · Shadows · Build ·
  Insights · Settings**, a persistent *Needs you* + *Live* rail, and one
  Decisions Inbox where every approval lands. Quick tasks answer in
  seconds from Chat; recorded workflows re-run in one click.

**📚 More:**
[Getting Started](docs/getting-started.md) ·
[Architecture](ARCHITECTURE.md) ·
[User Guide](USER_GUIDE.md) ·
[Contributing](CONTRIBUTING.md)

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
  inferred from the click sequence              (v0.6.0)
          │
          ▼
Scroll refiner turns the intent + abstracted
  steps into a structured Scroll with objectives
          │
          ▼
Pre-flight scroll validator (opt-in) checks
  satisfiability + intent-vs-tool fit;          (v0.4.0 + v0.6.0)
  surfaces a side-by-side remediation card
  with a proposed_revision when blocked         (v0.6.0)
          │
          ▼
Activity extractor selects tools and skills
  via data-flow reasoning (schemas in headers,
  not just keyword name match)                  (v0.6.0)
          │
          ▼
Missing tools forged with intent context →
  dry-run validation gate (Gate 3.5)            (v0.5.0)
          │
          ▼
Shadow decision picks an existing specialist OR
  creates a new one, scoring on semantic intent
  match plus skill/tool ID overlap              (v0.6.0)
          │
          ▼
Supervisor dispatches the Shadow.  Intelligent
  Supervisor (opt-in) intervenes between
  iterations with bounded actions including
  RECALIBRATE_TOOL / RECALIBRATE_SKILL when
  capabilities are structurally inadequate
          │
          ▼
Reverse-Harness Governor arbitrates capability
  requests the running Shadow PULLs — a missing
  tool, a dependency, an escalation, or a fan-out
  to parallel sub-agents (opt-in).  Under the
  default risk-tiered gate mode it auto-grants
  low-risk requests and escalates the rest to the
  Decisions Inbox; on approval the run resumes.
  Every iteration's decision is written to a
  per-run decision-audit ledger
          │
          ▼
Dashboard shows live progress, results,
  per-shadow + per-tool metrics, memory, and the
  Decisions Inbox for every operator gate
```

A deeper walkthrough of every stage lives in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Dashboard

The web dashboard (default <http://localhost:8765>) is organised as a
six-spine command center. The left sidebar has exactly six entries:

| Spine | Route | What it holds |
|---|---|---|
| **Home** | `/` | Overview — stat cards, the workflow pipeline, and the live activity feed |
| **Work** | `/work` | The workflow-centric view; Scrolls + Activities fold in here |
| **Shadows** | `/shadows` | The Shadow roster (agent personas) and their per-shadow memory |
| **Build** | `/tools` | Tool registry (with an *agent-built* filter for tools Systemu forged itself); Skills and Evolution proposals fold in here |
| **Insights** | `/insights` | Memory, the capability flywheel, and the event stream (tabbed) |
| **Settings** | `/settings` | LLM tier config, the gate-mode dial, and approval defaults |

Two surfaces are present on **every** page:

- **Right rail** — a persistent panel showing what *Needs you* (a glance
  at pending gates) and *Live* (a feed of in-flight runs). On narrow
  viewports it collapses to a "Needs you (N)" badge in the header.
- **Decisions Inbox** (`/inbox`) — the single place every approval gate
  lands as one unified card: scroll-approval, dependency, tool-forge,
  evolution, harness-escalation, and recovery gates. **Approve executes**
  — approving a card runs the same action the CLI would (e.g. approving a
  scroll triggers activity extraction).

### Gate modes

Settings exposes a gate-mode dial that controls how the runtime handles
approval gates:

| Mode | Behaviour |
|---|---|
| **Risk-tiered** (default) | The Governor auto-grants low-risk requests and escalates the rest to the Inbox |
| **Approve-only** | Every gate waits for the operator |
| **Bypass** | Auto-grants every gate **except** the safety floor (dependency/recovery gates) — dev/test only |

A safety **floor** keeps dependency and recovery gates interactive even
under Bypass unless explicitly disabled. The same dial is available from
the CLI via `sharing_on decisions mode`.

> **Legacy URLs still work.** `/army` redirects to `/shadows`;
> `/systemu-chat`, `/memory`, `/flywheel`, and `/notifications` redirect
> into their merged tabs. The old `/workshop` route is gone — its scroll
> rebuild is now an in-place dialog on the Scrolls view.

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

Systemu calls models in three tiers and you choose a **provider per tier** — mix
and match, or use one everywhere. You need credentials for **at least one** of:

- [OpenRouter](https://openrouter.ai) (free tier works) — the default; one key reaches many models
- [Google AI Studio](https://aistudio.google.com) (free)
- [OpenAI](https://platform.openai.com)
- [Anthropic](https://console.anthropic.com) — native SDK (install the `anthropic` extra)
- A local [Ollama](https://ollama.com) instance on `:11434` — keyless, on-device

`sharing_on setup` collects the keys (hidden entry, stored in `.env`) and the
Settings page lets you switch providers, models, and the budget / balanced /
quality preset anytime.

---

## Quick Start

Full walkthrough lives in [docs/getting-started.md](docs/getting-started.md).
The headline:

```bash
git clone https://github.com/rameswaran-mohan/project-systemu-pro.git
cd project-systemu-pro
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

To upgrade an existing install to the latest release (v0.6.4+): `./update.sh`
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

> **Windows note (v0.7.3):** Use **Ctrl+C** directly in the same terminal where
> `sharing_on record` is running. Sending SIGINT from another process via
> `kill -INT <pid>` (e.g. from Git Bash or a background script) may not
> deliver the signal to the Python child reliably — the session may stop
> without writing its final `end_time`, leaving `session.json` looking
> half-complete. Events in `events.db` are still complete and the session
> is fully usable by `sharing_on analyze`.

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

### Legacy / advanced Docker profiles

The original profiles are still in `docker-compose.yml` for backwards compatibility:

```bash
docker compose up systemu                          # legacy file backend
docker compose --profile docker-sandbox up systemu-docker   # tool sandbox
```

---

## Migrating from a pre-pivot install

If you already have a JSON-vault deployment from before the holistic-enterprise
pivot and want to move to **docker-local** or **docker-enterprise**, run the
one-shot migration tool after spinning up the new Postgres:

```bash
# 1. Start the new stack so Postgres is up + tables created
./install.sh --mode docker-enterprise --skip-pull --pg-password=<your-pg> --redis-password=<your-redis>
docker compose --profile enterprise up -d postgres
alembic upgrade head     # creates tables in the new Postgres

# 2. Dry-run — see what would migrate
python -m systemu.migrations.json_to_db \
    --source ./systemu/vault --dry-run

# 3. Run for real
python -m systemu.migrations.json_to_db \
    --source ./systemu/vault \
    --target "postgresql://systemu:<pg-password>@localhost:5432/systemu"
```

The migration is **idempotent** — re-running it after fixing any errors leaves
already-migrated rows untouched.  See `systemu/migrations/json_to_db.py` for
the source list (scrolls, shadows, tools, skills, activities, evolutions,
chat history).

For Redis topologies beyond the default standalone (TLS, Sentinel, custom CA),
see [`docs/redis-topologies.md`](docs/redis-topologies.md).

---

## Configuration Reference

All settings go in your `.env` file. Copy `.env.example` as a starting point.

### API Keys

You need credentials for **at least one** provider — not all of them. Which keys
matter depends on the provider you pick per tier (`sharing_on setup` or Settings).
Keys are stored in `.env`; they are never typed into the browser.

| Variable | Provider | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter | Default; one key reaches many models. Free tier at [openrouter.ai](https://openrouter.ai) |
| `GOOGLE_API_KEY` | Google AI Studio | Free at [aistudio.google.com](https://aistudio.google.com) |
| `OPENAI_API_KEY` | OpenAI | [platform.openai.com](https://platform.openai.com) |
| `ANTHROPIC_API_KEY` | Anthropic (native) | Needs the extra: `pip install "systemu[anthropic]"` |
| `OLLAMA_URL` | Ollama (local) | Default `http://localhost:11434` — no key required |
| `OPENROUTER_BASE_URL` | OpenRouter | Optional — override the OpenRouter base URL |

### LLM models & providers

Defaults are the **budget** preset (`deepseek/deepseek-v4-flash` on every tier).
Set `SYSTEMU_MODEL_PRESET` to `balanced` or `quality`, or override any tier
explicitly — an explicit tier model always wins over the preset.

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_MODEL_PRESET` | _(budget)_ | `budget` \| `balanced` \| `quality` — one-keystroke cost/quality dial |
| `SYSTEMU_TIER1_MODEL` | `deepseek/deepseek-v4-flash` | Deep reasoning — scroll refinement, shadow decisions |
| `SYSTEMU_TIER2_MODEL` | `deepseek/deepseek-v4-flash` | Structured output — tool forge, execution planning |
| `SYSTEMU_TIER3_MODEL` | `deepseek/deepseek-v4-flash` | Fast formatting — log-to-instructions conversion |
| `SYSTEMU_TIER1_PROVIDER` | _(auto)_ | Provider for tier 1: `openrouter` \| `google` \| `openai` \| `anthropic` \| `ollama` (empty = auto-detect from the model id) |
| `SYSTEMU_TIER2_PROVIDER` | _(auto)_ | Provider for tier 2 |
| `SYSTEMU_TIER3_PROVIDER` | _(auto)_ | Provider for tier 3 |
| `SHARING_ON_MODEL` | `deepseek/deepseek-v4-flash` | LLM used during sharing_on analysis |

The **quality** preset uses `anthropic/claude-sonnet-4.5` (tier 1),
`google/gemini-3-flash-preview` (tier 2), `deepseek/deepseek-v4-flash` (tier 3);
**balanced** uses `google/gemini-3-flash-preview` on tier 1 and the budget model
elsewhere. A model id a provider later rejects degrades to the tier's budget
default **with a visible flag**, rather than bricking the run.

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
| `SYSTEMU_DB_BIND` | `127.0.0.1:5432` (docker-local) / empty (docker-enterprise) | **v0.6.6+** docker modes only. Host bind for the Postgres container. Required for `sharing_on record` from host to reach the container's vault. Loopback-only by default. Set to `0.0.0.0:5432` to expose on all interfaces (NOT recommended on shared hosts). To fully unpublish in docker-local, remove the `ports:` section via `docker-compose.override.yml`. |

### Deployment mode

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_MODE` | `local` | `local` \| `docker-local` \| `docker-enterprise` — written by `install.py`; `start.sh`/`start.bat` read it |
| `SYSTEMU_DASHBOARD_HOST` | _(unset → 127.0.0.1)_ | Bind host for the NiceGUI dashboard |
| `SYSTEMU_DASHBOARD_PORT` | `8765` | Dashboard port |
| `SYSTEMU_DECISION_QUEUE` | _(unset)_ | **v0.8.0:** When `true`, operator-decision prompts in non-TTY contexts are persisted to the dashboard `/insights → Pending Actions` queue instead of being silently auto-picked. Recommended for dashboard-driven workflows. |
| `SYSTEMU_HEADLESS` | _(unset)_ | **Deprecated in v0.8.0** (use `SYSTEMU_DECISION_QUEUE` instead). When `1`, forces non-interactive mode at the `notify_user` layer (same effect as `SYSTEMU_NON_INTERACTIVE`) — silently auto-picks the safe-default `actions[0]`. |
| `SYSTEMU_OUTPUT_DIR` | `~/Documents` | Where Shadow-generated files are saved |
| `SYSTEMU_EXECUTION_RETENTION` | _(unset)_ | Max execution audit dirs to keep on disk; older pruned during save |

### Behaviour & approval

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_NON_INTERACTIVE` | `false` | Auto-pick `actions[0]` (the safe-by-default choice) in every `notify_user` prompt. **Renamed from `SYSTEMU_AUTO_APPROVE_SCROLLS` in v0.6.1** — the old name lied about scope and is no longer recognised |
| `SYSTEMU_AUTO_FORGE_TOOLS` | `false` | **Dev only** — auto-enables LLM-generated tools without review (bypasses Gate 2/3) |
| `SYSTEMU_APPROVAL_TIMEOUT` | _(unset)_ | Seconds before a queued approval auto-resolves (sqlite_approval_gate) |

### Tool execution

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_TOOL_BACKEND` | `local` | `local` \| `docker` \| `ssh` \| `wsl` (ssh/wsl are stubs) |
| `SYSTEMU_DOCKER_TOOL_TIMEOUT` | `300` | Per-tool timeout (seconds) when `SYSTEMU_TOOL_BACKEND=docker` |
| `SYSTEMU_TOOL_DEP_INSTALL_MODE` | `auto` | `auto` \| `off` \| `prompt` \| `always` — how the runtime handles tool pip deps |
| `SYSTEMU_PREWARM_TOOL_DEPS` | `false` | Install all deployed-tool deps on daemon start instead of on first call |

### Sub-agent fan-out (opt-in)

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_DELEGATE_USE_PARALLEL` | `false` | When `true`, a granted SUBAGENT request runs a goal's independent sub-tasks as a **bounded fleet of concurrent child agents** (one level deep), collated so partial success is reported honestly (what each child produced + what's missing) — never a blanket "everything failed". Off by default; sequential delegation is the safe, deterministic path. |

### Intelligent Supervisor (v0.4.0+)

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

### Pre-flight validators (v0.6.0)

| Variable | Default | Description |
|---|---|---|
| `SYSTEMU_SCROLL_VALIDATOR` | _(off; on when supervisor on)_ | Run the intent-aware scroll validator before activity extraction |
| `SYSTEMU_SKILL_VALIDATOR` | _(off; on when scroll validator on)_ | Run the GUI-codification skill validator at extraction time |

### Recalibration auto-approve (v0.5.1 + v0.6.0)

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

`install.py` writes `SYSTEMU_STORAGE=sqlite` to `.env` for `local` mode and `postgres` for `docker-local` / `docker-enterprise`. The in-process default when no env is set is `file` (kept for backward compat with pre-v0.3 installs).

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
project-systemu-pro/
├── sharing_on/                         — Capture engine + analyser
│   ├── collectors/                       — Screen, clipboard, file, window monitors
│   ├── analyzer/                         — Step detector, narrative generator
│   │   ├── intent_extractor.py             — v0.6.0 Tier-2 pre-pass that infers
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
│   │   ├── scroll_remediator.py            — v0.6.0 side-by-side fix card
│   │   ├── activity_extractor.py           — Stage 3 — schema-aware extraction
│   │   ├── skill_validator.py              — v0.6.0 GUI-codification check
│   │   ├── skill_recalibrator.py           — v0.6.0 re-author instructions_md
│   │   ├── tool_forge.py                   — Spec → code → save (Gate 1/2)
│   │   ├── tool_dry_run.py                 — v0.5.0 Gate 3.5 validation
│   │   ├── tool_recalibrator.py            — v0.5.0 bump-vs-fork pipeline
│   │   ├── tool_inadequacy_diagnosis.py    — v0.5.0 supervisor diagnosis
│   │   ├── shadow_decision.py              — Stage 5 — intent-aware tiebreak
│   │   ├── refinery.py                     — Post-execution memory consolidation
│   │   ├── evolution_engine.py             — Long-term shadow/skill evolution
│   │   ├── memory_consolidator.py          — Tiered memory consolidation
│   │   ├── cross_shadow_patterns.py        — Promotion of recurring lessons
│   │   └── workshop_module.py              — Operator-driven scroll/shadow edit
│   ├── runtime/                          — Shadow ReAct loop + Supervisor
│   │   ├── shadow_runtime.py               — Per-shadow execute loop
│   │   ├── supervisor.py                   — Activity queue + worker pool
│   │   ├── execution_mind.py               — Intelligent Supervisor (v0.4.0)
│   │   ├── execution_snapshot.py           — v0.5.1 true snapshot resume
│   │   ├── failure_classifier.py           — 10-category failure taxonomy
│   │   ├── tool_metrics.py / shadow_metrics.py — per-id telemetry
│   │   ├── affinity_log.py                 — Activity-shadow routing memory
│   │   ├── inadequacy_tracker.py           — Cross-shadow tool-inadequacy clustering
│   │   ├── rejection_store.py              — Operator-feedback learning
│   │   ├── governor.py                      — Reverse-Harness Governor (arbitrate + materialise capability PULLs)
│   │   ├── harness_arbiter.py               — Deterministic GRANT/DENY/ESCALATE policy
│   │   ├── subagent_fleet.py / subagent_harness.py — opt-in parallel child fan-out + partial-success collation
│   │   ├── decision_audit.py                — per-iteration decision ledger (executions/<id>/decision_audit.jsonl)
│   │   ├── gate_mode_settings.py            — Gate-mode dial (bypass / risk-tiered / approve-only) + floor
│   │   ├── tool_sandbox.py                 — Subprocess / docker / wsl / ssh exec
│   │   └── tool_registry.py                — Runtime tool loader
│   ├── interface/                        — NiceGUI dashboard + REST API
│   │   ├── pages/                          — Home, Work, Shadows, Build, Insights, Settings, Inbox, Chat
│   │   ├── command/                         — Shared command layer (Inbox queue, gates, verbs)
│   │   └── cli_commands.py                  — Systemu CLI groups (scrolls/army/tools/skills/decisions/…)
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
├── alembic/versions/                   — DB schema migrations (0001–0010)
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

## CLI reference

Everything in the dashboard is also driven from the `sharing_on` CLI.
Run `sharing_on --help` (or `sharing_on <group> --help`) for the full
surface; the headline groups:

| Command | Purpose |
|---|---|
| `sharing_on record` / `analyze` | Capture a workflow / re-analyze a recorded session |
| `sharing_on init` | Seed the working-directory vault from the bundled starter catalog |
| `sharing_on setup` | Pick the LLM provider + model preset per tier and store keys securely (hidden entry → `.env`); auto-runs on first `daemon start` if unconfigured |
| `sharing_on daemon start` / `stop` / `status` | Run the background daemon + web dashboard |
| `sharing_on doctor <id>` | Diagnose pending gates/blockers for a scroll/activity/shadow/tool (`--apply` to auto-fix) |
| `sharing_on scrolls list` / `show` / `refine` / `approve` | Manage Scrolls (refined SOPs) |
| `sharing_on army list` / `show` / `awaken` / `execute` | Manage and run Shadows |
| `sharing_on tools list` / `forge` / `dry-run` / `enable` / `recalibrate` | Manage the tool registry + its forge gates |
| `sharing_on skills list` / `export` / `deprecate` | Manage Skills (export to a portable Agent Skill) |
| `sharing_on evolve run` / `show-pending` / `apply` | Run and apply the Evolution Engine |
| `sharing_on decisions list` / `mode` / `resolve` | The Decisions Inbox from the terminal; `mode` sets the gate-mode dial |
| `sharing_on chat submit` / `history` | Run a free-text task through the full pipeline |
| `sharing_on settings show` / `set` | Inspect / write allow-listed configuration |
| `sharing_on session` · `capability` · `skill` · `user` | Inspect episodic memory, the capability ledger, bundled skills, and your profile |

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

* Report bugs / suggest features → [issue tracker](https://github.com/rameswaran-mohan/project-systemu-pro/issues)
* Security disclosures → [`SECURITY.md`](SECURITY.md)
* Community expectations → [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
* Release notes → [`CHANGELOG.md`](CHANGELOG.md)

---

## Project status

Pre-1.0.  Current release: **v0.9.30** (see [`release-notes/`](release-notes/) for what's new).  The table below summarises what's shipped vs. what's next.

### Shipped

| Version | Scope |
|---|---|
| **v0.2** | `silentgrasper` → `sharing_on` capture rename + identity split |
| **v0.3** | Three-mode pivot (local / docker-local / docker-enterprise), identity_split, Postgres backend, Telegram messaging gateway |
| **v0.4.0** | Intelligent Supervisor MVP — bounded action vocabulary, supervisor-flash bus, cost ledger, scroll validator |
| **v0.4.1** | Per-shadow supervisor opt-in, TERMINATE resolution UX, operator-feedback learning, strategy-stream UI |
| **v0.4.2** | Activity-shadow affinity routing |
| **v0.4.3** | Shadow-level success metrics, `Shadow.specialty` routing tag, cost-pressure surfacing |
| **v0.4.4** | Per-tool success-rate tracking, operator dashboard surfaces, specialty auto-suggest |
| **v0.5.0** | Tool readiness pipeline (Gate 3.5 dry-run validation), mid-execution `RECALIBRATE_TOOL`, bump-vs-fork decision, operator-approved resume |
| **v0.5.1** | Recalibration deferred items — override actions, spec-diff visualisation, low-risk auto-approve, cross-shadow inadequacy clustering, true snapshot-based resume |
| **v0.6.0** | Intent-aware extraction pipeline (root-cause fix) — capture intent extractor, intent-aware scroll validator + remediation, schema-aware tool/skill selection, skill intent contracts + recalibration, intent-driven tool forge, intent-aware shadow tiebreak |
| **v0.6.1** | Post-v0.6.0 hardening — `Tool.name` path-traversal guard, `SYSTEMU_AUTO_APPROVE_SCROLLS` → `SYSTEMU_NON_INTERACTIVE` rename with safe-action ordering, `RECALIBRATE_SKILL` runtime wiring, catalog N+1 fix, batched `save_skill` resolution |
| **v0.7.x** | PyPI/Docker packaging, native LLM provider plugins (Anthropic/OpenAI), `export-skill` wedge + spec-conformant `SKILL.md` writer, episodic-memory + capability ledger |
| **v0.8.x** | Decisions Inbox + gate-mode dial, `SYSTEMU_DECISION_QUEUE` for dashboard-driven approvals, wheel-bundled starter vault (`sharing_on init`) |
| **v0.9.x** | Reverse-Harness Governor (capability PULL arbitration, TOOL provisioner + escalate→approve→resume); the six-spine command-center dashboard with the persistent right rail; **pip-first onboarding** (`sharing_on setup`); **per-tier provider selection** (OpenRouter/Google/OpenAI/Anthropic/Ollama) with native Anthropic + local Ollama invocation and model presets; **plan-first quick lane** with honest partial answers; web-search snippets; geocode robustness; Telegram mobile alerts; MCP connector support |
| **v0.10.0** *(on `main`)* | Opt-in **parallel sub-agent fan-out** — a granted SUBAGENT spawns a bounded concurrent fleet (one level deep) with partial-success-aware collation; a per-run **decision-audit ledger**; lease-lifecycle + tool provenance (the *agent-built* badge); harness-run metrics |

### What's next

The next-phase work is open for design.  Likely candidates (not yet scheduled):

- Auto-recalibration without operator approval for low-risk **skill** patterns (telemetry-gated promotion)
- The remaining harness provisioners — SKILL / ACCESS / COMPUTE (TOOL ships; SUBAGENT fan-out ships opt-in; ACCESS isolation is currently Docker-only / future work)
- Recursive sub-agent decomposition (today's fleet is one level deep)
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

Cause: the env var was renamed to `SYSTEMU_NON_INTERACTIVE` in v0.6.1.  Hard cut, no alias.

Fix: edit `.env`, replace `SYSTEMU_AUTO_APPROVE_SCROLLS` with `SYSTEMU_NON_INTERACTIVE`, restart the daemon.

`install.py` and the daemon both emit warnings when the old key is detected.

### Daemon crashes with `OperationalError: no such column`

Symptom: dashboard loads but every page returns 500; `logs/daemon.log` shows `sqlalchemy.exc.OperationalError: no such column: ...`.

Cause: DB schema is behind the code.  Happens when you `git pull` a release with a new migration but skip re-running `install.py`.

Fix: `start.bat` / `start.sh` (v0.6.1+) auto-runs `alembic upgrade head` on every start.  If you're on an older start script:

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

`install.py` (v0.6.3+) prints this guide automatically on macOS; the daemon does not detect the missing grant at runtime.

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

Then re-run with the new interpreter: `python3.11 install.py`. v0.6.3+ prints these hints automatically.

### `Invalid key (HTTP 401 from OpenRouter)` during install

Symptom: install.py rejects the OpenRouter key with a 401 message and re-prompts.

Cause: the key was mistyped, revoked, or doesn't have model access enabled.

Fix: generate a fresh key at <https://openrouter.ai/keys> — the installer (v0.6.3+) probe-validates it before writing to `.env`. After 3 attempts the installer stores the key anyway; correct it manually in `.env` later, then restart the daemon.

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

`install.py` (v0.6.3+) echoes the detected proxy URL (with password masked) at the top of the install log. If no proxy line appears, the vars weren't exported into the shell that ran `install.py`.

### Apple Silicon (M1 / M2 / M3 / M4) — install or Playwright errors

Symptom: install or Playwright fails with architecture-mismatch errors on an M-series Mac.

Cause: some PyObjC-using deps or Chromium binaries lag the ARM64 build cycle.

Fix: re-run install under Rosetta:

```bash
arch -x86_64 python install.py
```

`install.py` (v0.6.4+) prints an info banner on Apple Silicon listing this and other known caveats. Most installs complete natively without intervention.

### Docker mode — captured scroll never appears on dashboard (v0.6.6+)

Symptom: `sharing_on record` completes, you see `intent.json` + `instructions.md` in the capture directory, but no scroll lands on `/scrolls`.

Cause: the host's `analyze` cannot reach the container's Postgres.

Fix: confirm `SYSTEMU_DB_BIND` is set in `.env`:

- **docker-local** (default): `SYSTEMU_DB_BIND=127.0.0.1:5432` — loopback-only binding. Pre-v0.6.6 installs and operators who manually edited `.env` may have this missing. Re-run `install.py --mode docker-local` to refresh.
- **docker-enterprise**: not published by default. To enable for development, set `SYSTEMU_DB_BIND=127.0.0.1:5432` AND add a `ports:` block to the `postgres` service via a `docker-compose.override.yml`. Not recommended for production.

After editing: `docker compose down && docker compose --profile <local|enterprise> up -d`.

### Docker mode — dashboard shows different scrolls than the worker writes (pre-v0.6.6 only)

Symptom: dashboard `/scrolls` lists fewer scrolls than `psql` shows in Postgres. Activities in the database are not visible in the dashboard's activity feed.

Cause: pre-v0.6.6 dashboard fell back to FileVault when `SYSTEMU_REDIS_URL` was missing (docker-local intentionally has no Redis). Dashboard wrote to `/data/vault/*.json` while the worker wrote to Postgres. Split-brain.

Fix: upgrade to v0.6.6+ via `./update.sh` (or `update.bat`). The AppState fix ([commit `v0.6.6-c`](#)) gates the Redis URL requirement on `SYSTEMU_QUEUE_BROKER=redis` (enterprise only).

### Docker mode — elder/shadow memory disappears after `docker compose down -v` (pre-v0.6.6 only)

Symptom: every container rebuild loses all consolidated learnings. `ELDER_MEMORY.md` and `shadow_<id>/memory/` files are empty on the new container.

Cause: pre-v0.6.6 SqliteVault defaulted `memory_dir` to `/tmp/systemu_memory` for Postgres URLs. `/tmp` in a container is the writable layer, not a volume mount, so it's lost on rebuild.

Fix: upgrade to v0.6.6+. The new default is `${SYSTEMU_VAULT_DIR}/memory`, which is volume-mounted and persistent.

---

## License

MIT — see [`LICENSE`](LICENSE).
