"""Configuration management — loads from .env and environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# v0.8.0.2: load .env from CWD FIRST so every CLI verb (analyze, init,
# scrolls, decisions, etc.) gets consistent dotenv behavior — not just
# `daemon start`.  Previously only the install-time _PROJECT_ROOT/.env was
# loaded, which silently ignored the user's working-directory .env on
# pip-installed setups.
#
# override=False: existing process env vars take precedence over .env values
# so subprocesses spawned with explicit env (e.g. JobManager) keep their
# parent's overrides.
# v0.8.0.3: tolerate UTF-8 BOM (Windows PowerShell's `Set-Content -Encoding
# utf8` writes UTF-8 with BOM, which python-dotenv mis-parses — the first
# key ends up as "﻿OPENROUTER_API_KEY" instead of "OPENROUTER_API_KEY"
# and os.environ.get(...) returns empty.  utf-8-sig strips the BOM if
# present and is safe for files written without one.
_CWD_ENV = Path.cwd() / ".env"
if _CWD_ENV.exists():
    load_dotenv(_CWD_ENV, override=False, encoding="utf-8-sig")

# Legacy: also load .env from the install dir (for git-clone / editable
# installs where the .env lives alongside the source).  override=False so
# CWD .env (loaded above) wins for keys that appear in both.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=False, encoding="utf-8-sig")


# Legacy env-var compatibility shim for silentgrasper_* names was
# removed in v0.3 per the deprecation window declared in v0.2's
# MIGRATION.md.  Operators must use the SHARING_ON_* names directly.


# W2.3: the second factor for auto-forge. Disabling all three tool security
# gates must take more than one .env line — the ack phrase is deliberately
# explicit about what is being signed away.
AUTO_FORGE_ACK_PHRASE = "I_UNDERSTAND_ALL_GATES_ARE_BYPASSED"


def _load_auto_forge_tools() -> bool:
    """Read SYSTEMU_AUTO_FORGE_TOOLS; require the explicit ack phrase (W2.3).

    ``SYSTEMU_AUTO_FORGE_TOOLS=true`` alone no longer enables the bypass —
    ``SYSTEMU_AUTO_FORGE_ACK`` must equal :data:`AUTO_FORGE_ACK_PHRASE` too.
    Without it, auto-forge stays OFF and the warning tells the operator
    exactly what is missing (instead of silently disarming every gate).
    """
    import sys
    requested = os.getenv("SYSTEMU_AUTO_FORGE_TOOLS", "false").lower() == "true"
    if not requested:
        return False
    acked = os.getenv("SYSTEMU_AUTO_FORGE_ACK", "") == AUTO_FORGE_ACK_PHRASE
    if not acked:
        print(
            "\n\033[93m⚠  SYSTEMU_AUTO_FORGE_TOOLS=true was set but NOT enabled.\n"
            "   Bypassing all three tool security gates requires the explicit ack:\n"
            f"   SYSTEMU_AUTO_FORGE_ACK={AUTO_FORGE_ACK_PHRASE}\n"
            "   Auto-forge remains OFF until both are set. DEV/TESTING ONLY.\033[0m\n",
            file=sys.stderr,
        )
        return False
    print(
        "\n\033[93m⚠  WARNING: SYSTEMU_AUTO_FORGE_TOOLS is enabled (ack received) — all\n"
        "   tool security gates are bypassed. LLM-generated code will be saved and\n"
        "   enabled without human review. DEV/TESTING MODE ONLY.\033[0m\n",
        file=sys.stderr,
    )
    return True


def _resolve_execution_adherence() -> str:
    """Read SYSTEMU_EXECUTION_ADHERENCE; fall back to 'auto' for invalid values."""
    raw = (os.getenv("SYSTEMU_EXECUTION_ADHERENCE") or "").strip().lower()
    if raw in {"auto", "free", "guided", "strict"}:
        return raw
    return "auto"


def _resolve_tool_backend() -> str:
    """Resolve the canonical tool-backend name from SYSTEMU_TOOL_BACKEND.

    Falls back to ``"local"`` when the env var is unset or unknown.
    Implemented inline (no runtime-package dependency) so config can be
    loaded before the runtime package is importable.
    """
    explicit = (os.getenv("SYSTEMU_TOOL_BACKEND") or "").strip().lower()
    if explicit in {"local", "docker", "ssh", "wsl"}:
        return explicit
    return "local"


@dataclass
class Config:
    """Runtime configuration for a capture session."""

    # --- LLM ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # W11.7: was z-ai/glm-4.5-air:free — OpenRouter 404s it now.
    llm_model: str = "deepseek/deepseek-v4-flash"
    google_api_key: str = ""   # Google AI Studio key — used for Tier 2 (Gemini direct)

    # --- Systemu LLM Tiers ---
    # v0.6.7: pinned to deepseek-v4-flash across all 3 modes (single OpenRouter
    # key + reliable rate limits + no separate Google AI Studio creds).  Override
    # via SYSTEMU_TIER{1,2,3}_MODEL env vars.
    tier1_model: str = "deepseek/deepseek-v4-flash"         # deep reasoning
    tier2_model: str = "deepseek/deepseek-v4-flash"         # structured / code
    tier3_model: str = "deepseek/deepseek-v4-flash"         # fast / formatting (W11.7: glm-4.5-air:free 404s)
    # v0.7-e: provider override per tier.  Empty string = auto-detect from
    # model name via systemu.llm.providers.resolve_provider_class.  Set to one
    # of {"openrouter","google","anthropic","openai","ollama"} to force.
    tier1_provider: str = ""
    tier2_provider: str = ""
    tier3_provider: str = ""
    # v0.6.1-b: renamed from auto_approve_scrolls.  The old name suggested
    # this only affected scroll approval, but in practice it cascaded to
    # every multi-action notify_user prompt (auto-forge tools, auto-create
    # shadows, auto-approve workshop edits, etc.).  New name reflects
    # actual behaviour: "auto-pick actions[0] in any prompt."  See the
    # action-ordering contract on notify_user — actions[0] must be the
    # safe-by-default choice for this flag to be safe to enable.
    non_interactive: bool = False                        # auto-pick actions[0] in every prompt
    # v0.8.21: stuck-loop guard — pauses run when the agent makes no progress.
    # Per-call env reads in shadow_runtime so changes via the Settings page
    # take effect on the NEXT iteration without a daemon restart.
    stuck_guard:        bool = True
    stuck_no_progress:  int  = 5        # iters without objective credit before pause
    stuck_tool_fails:   int  = 3        # consecutive same-tool failures before pause
    # v0.6.5-d: Stage 6 pre-flight validator on by default.  Catches intent/data-flow
    # mismatches before scrolls reach activity extraction.  Opt out via
    # SYSTEMU_SCROLL_VALIDATOR=false for legacy behavior.
    scroll_validator: bool = True
    # DEV/TESTING ONLY — collapses all three tool security gates (spec review, code review,
    # explicit enablement). LLM-generated code is saved and enabled without human review.
    # Never set this to True in production workflows.
    auto_forge_tools: bool = False
    vault_dir: str = "systemu/vault"                     # path to vault root
    tool_backend: str = "local"                          # "local" | "docker" | "ssh" | "wsl"; resolved from env at load time
    docker_tool_timeout: int = 300                       # per-tool timeout (s) in Docker mode; covers image pull + pip install + run
    # v0.9.1.1 hotfix: default per-tool wall-clock budget when the Tool
    # itself doesn't override via timeout_seconds. Bumped from the
    # historical hardcoded 30s — too aggressive for web tools.
    tool_default_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_TOOL_DEFAULT_TIMEOUT_SECONDS", "60"))
    )

    # v0.9.2 (Layer 2 — Episodic Memory): per-run summarization + cross-session recall.
    episodic_memory_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_EPISODIC_MEMORY_ENABLED", "true").lower() != "false"
    )
    summarize_after_run: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_SUMMARIZE_AFTER_RUN", "true").lower() != "false"
    )
    episodic_search_default_limit: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_EPISODIC_SEARCH_DEFAULT_LIMIT", "5"))
    )
    episodic_summary_max_chars: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_EPISODIC_SUMMARY_MAX_CHARS", "800"))
    )
    episodic_tags_max_count: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_EPISODIC_TAGS_MAX_COUNT", "8"))
    )

    # v0.9.3 (Layer 3 — Capability Ledger + Tool Registry v2):
    capability_ledger_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_CAPABILITY_LEDGER_ENABLED", "true").lower() != "false"
    )
    capability_track_outcomes: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_CAPABILITY_TRACK_OUTCOMES", "true").lower() != "false"
    )
    check_fn_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_CHECK_FN_CACHE_TTL_SECONDS", "30"))
    )
    tool_output_max_chars_default: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_TOOL_OUTPUT_MAX_CHARS_DEFAULT", "100000"))
    )

    # v0.9.4 (Layer 5 — Recipe Fast-Paths): bundled + user SKILL.md catalogs.
    skill_loader_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_SKILL_LOADER_ENABLED", "true").lower() != "false"
    )
    skills_bundled_dir: str = field(
        default_factory=lambda: os.getenv(
            "SYSTEMU_SKILLS_BUNDLED_DIR",
            os.path.join(os.path.dirname(__file__), "..", "systemu", "skills").replace("\\", "/"),
        )
    )
    skills_user_dir: str = field(
        default_factory=lambda: os.getenv("SYSTEMU_SKILLS_USER_DIR", "")
    )

    # v0.9.5 (Layer 6 — Goal-Level Orchestration): delegate.spawn_subagent caps.
    delegate_max_depth: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_DELEGATE_MAX_DEPTH", "3"))
    )
    delegate_max_concurrent_children: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_DELEGATE_MAX_CONCURRENT_CHILDREN", "2"))
    )
    delegate_max_turns_per_child: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_DELEGATE_MAX_TURNS_PER_CHILD", "20"))
    )

    # v0.9.5 (Layer 6 — MCP integration): comma-separated MCP server URLs.
    # Empty by default — operator opts in by setting SYSTEMU_MCP_SERVER_URLS.
    # OAuth-protected servers deferred to v0.9.6+.
    mcp_server_urls: str = field(
        default_factory=lambda: os.getenv("SYSTEMU_MCP_SERVER_URLS", "")
    )

    # v0.9.6 (Layer 7 — Proactive Surfacing): inactivity-triggered curator.
    curator_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_CURATOR_ENABLED", "true").lower() != "false"
    )
    curator_interval_hours: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_CURATOR_INTERVAL_HOURS", "168"))  # 7 days
    )
    curator_min_idle_minutes: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_CURATOR_MIN_IDLE_MINUTES", "120"))  # 2 hours
    )

    # v0.9.6 (Layer 7 — Proactive Surfacing): auto-skill-extraction (Odysseus).
    auto_skill_extract_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_AUTO_SKILL_EXTRACT_ENABLED", "true").lower() != "false"
    )
    auto_skill_extract_min_confidence: float = field(
        default_factory=lambda: float(os.getenv("SYSTEMU_AUTO_SKILL_EXTRACT_MIN_CONFIDENCE", "0.6"))
    )

    # v0.9.6 (Layer 7 — Proactive Surfacing): memory consolidation pass.
    memory_consolidation_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_MEMORY_CONSOLIDATION_ENABLED", "true").lower() != "false"
    )

    # v0.9.7 (Phase 3.2 — Intent Engine): execution-adherence dial.
    # Controls how tightly the agent follows recorded SOPs vs. exercising
    # autonomous judgment.  One of auto|free|guided|strict.
    # "auto" (default): chat→free, record/sop→guided (or the SOP's saved value).
    # Invalid values silently fall back to "auto".
    execution_adherence: str = field(
        default_factory=lambda: _resolve_execution_adherence()
    )

    execution_retention_count: int = 50                  # max execution dirs kept in vault/executions/

    # --- Capture ---
    capture_screenshots: bool = False        # opt-in; images are never used by the LLM pipeline
    screenshot_interval: float = 3.0        # seconds between screenshots (when capture_screenshots=True)
    screenshot_max_width: int = 1280         # downscale screenshots
    window_poll_interval: float = 1.0        # active window polling
    process_poll_interval: float = 2.0       # process list polling
    clipboard_poll_interval: float = 1.5     # clipboard polling
    step_idle_threshold: float = 10.0        # seconds of inactivity = step boundary

    # --- Filesystem watcher ---
    watch_dirs: List[str] = field(default_factory=list)
    ignore_patterns: List[str] = field(default_factory=lambda: [
        "*.pyc", "__pycache__", ".git", "node_modules", ".DS_Store",
        "*.swp", "*.swo", "*~", "Thumbs.db", "*.tmp", "*.log",
    ])

    # --- Output ---
    output_base_dir: str = ""   # set at runtime (capture sessions)

    # Where Shadow-generated files land (reports, exports, downloads).
    # In Docker:  /app/systemu/outputs  (bind-mounted → visible on Windows host)
    # Natively:   ~/Documents           (user's documents folder)
    # Override:   SYSTEMU_OUTPUT_DIR env var
    output_dir: str = ""

    # --- Privacy ---
    redact_emails: bool = True
    redact_ips: bool = False    # often needed in instructions
    redact_api_keys: bool = True

    # --- Deployment / runtime backends ---
    # Picked by install.py and surfaced here so application code never has to
    # re-parse env vars.  All four are also still honoured at the env level by
    # the modules that historically read them directly (worker.py, huey_app.py).
    systemu_mode: str = "local"          # "local" | "docker-local" | "docker-enterprise"
    storage_backend: str = "file"        # "file" | "sqlite" | "postgres"
    queue_backend: str = ""              # "" (Supervisor) | "huey"
    queue_broker: str = "sqlite"         # "sqlite" | "redis"
    database_url: str = ""               # SQLAlchemy URL (sqlite/postgres modes)
    redis_url: str = ""                  # redis:// URL (docker-enterprise)
    # --- Tool dependency installer (v0.3.3+) ---
    # Controls whether the tool registry auto-installs declared pip deps.
    # Values: "auto" (resolve from systemu_mode), "off", "prompt", "always".
    # See systemu/runtime/dependency_installer.py for resolution rules.
    tool_dep_install_mode: str = "auto"

    # v0.8.18 — Credential-resolution policy read by Gate-4.  Controls whether
    # a missing tool credential prompts the operator (asked once via the
    # decision-queue dedup, then degrades if the operator skips) or silently
    # degrades the tool call.
    credential_policy: str = "prompt"   # v0.8.18: "prompt" (ask once via queue dedup, then degrade if skipped) | "degrade"

    # v0.3.5 — When true, the daemon walks enabled tools at start and
    # ensures every declared dep is installed.  Trades a small startup
    # cost for predictable first-call latency under PROMPT/ALWAYS modes.
    # Set via SYSTEMU_PREWARM_TOOL_DEPS=true.
    prewarm_tool_deps: bool = False

    # --- v0.9.8: keyless layered web stack (systemu/runtime/web_access.py) ---
    # Jina Reader (extract) + Jina-on-DDG (search) + OSM Overpass (local POIs),
    # all keyless with rate-limit/cache/attribution guardrails. Optional key/
    # self-host backends are OFF by default.
    web_stack_v2_enabled: bool = True        # SYSTEMU_WEB_STACK_V2 — master flag
    web_reader_backend: str = "auto"         # auto | jina | raw
    web_search_backend: str = "auto"         # auto | ddg | brave | tavily | searxng
    web_cache_ttl_seconds: int = 900
    nominatim_user_agent: str = "systemu/0.9.8 (+https://github.com/rameswaran-mohan/project-systemu)"
    brave_api_key: str = ""                  # optional — OFF by default
    tavily_api_key: str = ""                 # optional — OFF by default
    searxng_url: str = ""                     # optional self-host — OFF by default

    # --- v0.9.8: autonomous mid-run coach ---
    # On a no-progress stall, generate a corrective steer via the supervisor/
    # governor LLM and inject it, escalating to a human only after N self-steers.
    auto_coach_enabled: bool = True          # SYSTEMU_AUTO_COACH
    auto_coach_max_steers: int = 2           # self-steers before operator escalation
    # v0.9.8 (B2): research-loop convergence steer — force the agent to write its
    # deliverable after N consecutive read-only research calls (web_search/web_read/
    # web_extract/fetch_json) with nothing produced, capped per run.
    research_loop_threshold: int = 5         # SYSTEMU_RESEARCH_LOOP_THRESHOLD
    research_loop_max_steers: int = 2        # SYSTEMU_RESEARCH_LOOP_MAX_STEERS

    # --- v0.4.0 Intelligent Supervisor (plumbing only at this phase) ---
    # The supervisor itself ships in v0.4.0-d; these knobs are read by code
    # added in subsequent phases.  All inert when intelligent_supervisor_enabled
    # is False (the default for the v0.4.0 rollout).
    max_consecutive_think:                int   = 5      # THINK-throttle ceiling
    intelligent_supervisor_enabled:       bool  = False  # master kill switch
    supervisor_evaluation_cadence:        str   = "auto" # auto|every_failure|every_snapshot|every_n_iterations:N
    supervisor_llm_budget_per_run:        int   = 10     # Tier-3+Tier-1 supervisor calls per execution
    supervisor_tier_routine:              str   = "tier_3"
    supervisor_tier_intervention:         str   = "tier_1"
    supervisor_directive_timeout_s:       float = 5.0
    supervisor_llm_budget_per_hour_usd:   float = 5.0
    supervisor_llm_budget_per_day_usd:    float = 50.0
    # v0.5.1-c: when True, low-risk RECALIBRATE_TOOL outcomes (fork mode,
    # passing dry-run, non-destructive tool, high-confidence diagnosis)
    # auto-approve and resume without operator interaction.  Default OFF
    # so operators stay in the loop until they explicitly trust the
    # supervisor's recalibrations.
    auto_approve_low_risk_recalibrations: bool = False
    # v0.6.0-d.5: same pattern for RECALIBRATE_SKILL — when True, low-risk
    # skill recalibrations (fork mode, confidence=high, no side-effects in
    # produces, non-destructive name) auto-approve and resume.  Default OFF
    # so operators stay in the loop.  Mirrors the tool-recal knob above.
    auto_approve_low_risk_skill_recalibrations: bool = False

    # v0.9.0 (Layer 1): auto-extract user facts from chat conversations
    # via a Tier-1 LLM call after each chat task resolves. Default ON;
    # operators can opt out with SYSTEMU_AUTO_EXTRACT_USER_FACTS=false.
    auto_extract_user_facts: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_AUTO_EXTRACT_USER_FACTS", "true").lower() != "false"
    )

    # v0.9.1 (Layer 4): Durable-outcome contract via fresh-context verifier.
    # Each completion claim is judged by an independent Tier-1 LLM against a
    # state delta. Magic constants live here so each deployment mode can tune
    # without code edits.
    verifier_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_VERIFIER_ENABLED", "true").lower() != "false"
    )
    verifier_per_turn_cap: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_VERIFIER_PER_TURN_CAP", "2"))
    )  # max verifier calls per LLM turn before "fresh effectful work" gate engages
    verifier_rejection_budget: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_VERIFIER_REJECTION_BUDGET", "3"))
    )  # consecutive verifier rejections per objective before stuck-loop escalation
    verifier_max_calls_per_run: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_VERIFIER_MAX_CALLS_PER_RUN", "50"))
    )  # safety cap on total verifier calls per execution (enterprise budget control)
    verifier_tier: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_VERIFIER_TIER", "3"))
    )  # 1 = Tier-1 (deep reasoning) | 2 = Tier-2 | 3 = Tier-3 (default).
    # v0.9.8 (B6): default Tier-3. The verdict task (judge a StateDelta yes/no) does
    # NOT need deep reasoning, and Tier-1 reasoning models wrap output in prose so the
    # JSON parser fails (-> spurious soft-passes that disable the verifier). Tier-3
    # (z-ai/glm-4.5-air:free) follows "return strict JSON" reliably AND is free.
    audit_log_enabled: bool = field(
        default_factory=lambda: os.getenv("SYSTEMU_AUDIT_LOG_ENABLED", "true").lower() != "false"
    )  # global on/off for the action-audit log at vault/audit/actions.jsonl (NOT system logging)
    state_delta_file_preview_chars: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_STATE_DELTA_FILE_PREVIEW_CHARS", "200"))
    )
    state_delta_max_files_per_section: int = field(
        default_factory=lambda: int(os.getenv("SYSTEMU_STATE_DELTA_MAX_FILES_PER_SECTION", "50"))
    )

    @classmethod
    def from_env(cls) -> "Config":
        """Build config from environment variables with sensible defaults."""
        from sharing_on.model_presets import resolve_preset
        _preset_tiers = resolve_preset(os.environ)
        instance = cls(
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            google_api_key=os.getenv("GOOGLE_API_KEY", ""),
            llm_model=os.getenv("SHARING_ON_MODEL", "deepseek/deepseek-v4-flash"),
            # Systemu uses tier3 for log→instructions (existing analyze step)
            # W8.1: defaults route through the preset (SYSTEMU_MODEL_PRESET);
            # explicit per-tier env vars always win. No preset ⇒ today's
            # defaults byte-for-byte (resolve_preset's budget fallback).
            tier1_model=os.getenv("SYSTEMU_TIER1_MODEL", _preset_tiers["tier1"]),
            tier2_model=os.getenv("SYSTEMU_TIER2_MODEL", _preset_tiers["tier2"]),
            tier3_model=os.getenv("SYSTEMU_TIER3_MODEL", _preset_tiers["tier3"]),
            # v0.7-e: optional provider override per tier (empty = auto-detect)
            tier1_provider=os.getenv("SYSTEMU_TIER1_PROVIDER", ""),
            tier2_provider=os.getenv("SYSTEMU_TIER2_PROVIDER", ""),
            tier3_provider=os.getenv("SYSTEMU_TIER3_PROVIDER", ""),
            # v0.6.1-b: hard rename — old SYSTEMU_AUTO_APPROVE_SCROLLS is no longer read.
            non_interactive=os.getenv("SYSTEMU_NON_INTERACTIVE", "false").lower() == "true",
            stuck_guard       = (os.getenv("SYSTEMU_STUCK_GUARD", "on") or "on").lower() != "off",
            stuck_no_progress = int(os.getenv("SYSTEMU_STUCK_NO_PROGRESS", "5") or "5"),
            stuck_tool_fails  = int(os.getenv("SYSTEMU_STUCK_TOOL_FAILS", "3") or "3"),
            scroll_validator=os.getenv("SYSTEMU_SCROLL_VALIDATOR", "true").lower() == "true",
            auto_forge_tools=_load_auto_forge_tools(),
            tool_backend=_resolve_tool_backend(),
            docker_tool_timeout=int(os.getenv("SYSTEMU_DOCKER_TOOL_TIMEOUT", "300")),
            vault_dir=os.getenv("SYSTEMU_VAULT_DIR", "systemu/vault"),
            output_dir=os.getenv(
                "SYSTEMU_OUTPUT_DIR",
                str(Path.home() / "Documents"),   # native Windows/Mac default
            ),
            execution_retention_count=int(os.getenv("SYSTEMU_EXECUTION_RETENTION", "50")),
            capture_screenshots=os.getenv("SHARING_ON_CAPTURE_SCREENSHOTS", "false").lower() == "true",
            screenshot_interval=float(
                os.getenv("SHARING_ON_SCREENSHOT_INTERVAL", "3")
            ),
            screenshot_max_width=int(
                os.getenv("SHARING_ON_SCREENSHOT_WIDTH", "1280")
            ),
            systemu_mode=os.getenv("SYSTEMU_MODE", "local").lower(),
            storage_backend=os.getenv("SYSTEMU_STORAGE", "file").lower(),
            queue_backend=os.getenv("SYSTEMU_QUEUE", "").lower(),
            queue_broker=os.getenv("SYSTEMU_QUEUE_BROKER", "sqlite").lower(),
            database_url=os.getenv("SYSTEMU_DATABASE_URL", ""),
            redis_url=os.getenv("SYSTEMU_REDIS_URL", ""),
            tool_dep_install_mode=os.getenv("SYSTEMU_TOOL_DEP_INSTALL_MODE", "auto").lower(),
            credential_policy=os.getenv("SYSTEMU_CREDENTIAL_POLICY", "prompt").lower(),
            prewarm_tool_deps=os.getenv("SYSTEMU_PREWARM_TOOL_DEPS", "false").lower() == "true",
            # v0.9.8 keyless web stack
            web_stack_v2_enabled=os.getenv("SYSTEMU_WEB_STACK_V2", "true").lower() == "true",
            web_reader_backend=os.getenv("SYSTEMU_WEB_READER_BACKEND", "auto").lower(),
            web_search_backend=os.getenv("SYSTEMU_WEB_SEARCH_BACKEND", "auto").lower(),
            web_cache_ttl_seconds=int(os.getenv("SYSTEMU_WEB_CACHE_TTL", "900")),
            nominatim_user_agent=os.getenv(
                "SYSTEMU_NOMINATIM_UA",
                "systemu/0.9.8 (+https://github.com/rameswaran-mohan/project-systemu)"),
            brave_api_key=os.getenv("SYSTEMU_BRAVE_API_KEY", ""),
            tavily_api_key=os.getenv("SYSTEMU_TAVILY_API_KEY", ""),
            searxng_url=os.getenv("SYSTEMU_SEARXNG_URL", ""),
            # v0.9.8 autonomous coach
            auto_coach_enabled=os.getenv("SYSTEMU_AUTO_COACH", "true").lower() == "true",
            auto_coach_max_steers=int(os.getenv("SYSTEMU_AUTO_COACH_MAX_STEERS", "2")),
            research_loop_threshold=int(os.getenv("SYSTEMU_RESEARCH_LOOP_THRESHOLD", "5")),
            research_loop_max_steers=int(os.getenv("SYSTEMU_RESEARCH_LOOP_MAX_STEERS", "2")),
            # v0.4.0 supervisor knobs (env overrides)
            max_consecutive_think=int(os.getenv("SYSTEMU_MAX_CONSECUTIVE_THINK", "5")),
            intelligent_supervisor_enabled=os.getenv(
                "SYSTEMU_INTELLIGENT_SUPERVISOR", "false").lower() == "true",
            supervisor_evaluation_cadence=os.getenv(
                "SYSTEMU_SUPERVISOR_CADENCE", "auto").lower(),
            supervisor_llm_budget_per_run=int(
                os.getenv("SYSTEMU_SUPERVISOR_BUDGET_RUN", "10")),
            supervisor_tier_routine=os.getenv(
                "SYSTEMU_SUPERVISOR_TIER_ROUTINE", "tier_3").lower(),
            supervisor_tier_intervention=os.getenv(
                "SYSTEMU_SUPERVISOR_TIER_INTERVENTION", "tier_1").lower(),
            supervisor_directive_timeout_s=float(
                os.getenv("SYSTEMU_SUPERVISOR_TIMEOUT_S", "5.0")),
            supervisor_llm_budget_per_hour_usd=float(
                os.getenv("SYSTEMU_SUPERVISOR_BUDGET_HOUR_USD", "5.0")),
            supervisor_llm_budget_per_day_usd=float(
                os.getenv("SYSTEMU_SUPERVISOR_BUDGET_DAY_USD", "50.0")),
            auto_approve_low_risk_recalibrations=os.getenv(
                "SYSTEMU_AUTO_APPROVE_LOW_RISK_RECAL", "false").lower() == "true",
            auto_approve_low_risk_skill_recalibrations=os.getenv(
                "SYSTEMU_AUTO_APPROVE_LOW_RISK_SKILL_RECAL", "false").lower() == "true",
            execution_adherence=_resolve_execution_adherence(),
        )
        cls._warn_environment_issues()
        return instance

    @staticmethod
    def _warn_environment_issues() -> None:
        """v0.6.2: print one-time runtime warnings for environment issues
        that won't crash startup but will silently degrade behaviour.

        Today's warnings:
          * Wayland on Linux → pynput-based capture records empty streams.
          * Stale SYSTEMU_AUTO_APPROVE_SCROLLS env var → silently no-ops
            since v0.6.1's rename to SYSTEMU_NON_INTERACTIVE.
        """
        import os as _os, sys as _sys
        if _sys.platform.startswith("linux"):
            if (_os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland":
                print(
                    "[Config] WARNING: Wayland session detected — pynput-based "
                    "capture will record empty event streams.  Daemon + "
                    "dashboard + tool execution are unaffected.",
                    file=_sys.stderr,
                )
        if _os.environ.get("SYSTEMU_AUTO_APPROVE_SCROLLS"):
            # v0.7.3 Bug #20: tell the operator HOW to suppress this warning,
            # not just that it appeared. Previous wording said "Update your
            # .env" which led people to add SYSTEMU_NON_INTERACTIVE without
            # removing the deprecated line, so the warning kept firing.
            print(
                "[Config] WARNING: SYSTEMU_AUTO_APPROVE_SCROLLS is set but "
                "this env var was renamed to SYSTEMU_NON_INTERACTIVE in "
                "v0.6.1 and is now ignored. ACTION: delete the "
                "SYSTEMU_AUTO_APPROVE_SCROLLS= line from your .env to "
                "suppress this warning. If you want the auto-pick-safe-default "
                "behaviour, set SYSTEMU_NON_INTERACTIVE=true instead.",
                file=_sys.stderr,
            )

    def validate(self) -> List[str]:
        """Return a list of validation errors (empty = valid)."""
        errors = []
        if not self.openrouter_api_key:
            errors.append(
                "OPENROUTER_API_KEY not set. "
                "Copy .env.example to .env and add your key."
            )
        return errors
