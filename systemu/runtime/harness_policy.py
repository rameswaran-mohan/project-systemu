"""Harness Policy — per-run configuration for the HarnessArbiter.

Reads from a config dict and/or environment variables (``SYSTEMU_HARNESS_*``).
All fields have sane conservative defaults (default-deny posture).

Environment variables (all optional):
  SYSTEMU_HARNESS_AUTO_GRANT_TOOL=false          enable auto-grant for TOOL kind
  SYSTEMU_HARNESS_AUTO_GRANT_SKILL=true          enable auto-grant for SKILL kind
  SYSTEMU_HARNESS_AUTO_GRANT_ACCESS=false        enable auto-grant for ACCESS kind
  SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE=true        enable auto-grant for COMPUTE kind
  SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT=false      enable auto-grant for SUBAGENT kind
  SYSTEMU_HARNESS_MAX_REQUESTS_PER_RUN=8         hard cap on total requests per run
  SYSTEMU_HARNESS_MAX_COMPUTE_CEILING=1.0        max compute budget multiplier (1.0 = 100%)
  SYSTEMU_HARNESS_MAX_SUBAGENT_DEPTH=1           maximum sub-Shadow nesting depth
  SYSTEMU_HARNESS_MAX_SUBAGENT_BUDGET=0.5        max fraction of parent budget for subagent
  SYSTEMU_HARNESS_ALLOWED_RESOURCES=             comma-separated whitelisted resource names
  SYSTEMU_HARNESS_ALLOWED_PACKAGES=              comma-separated whitelisted pip packages
  SYSTEMU_HARNESS_ALLOWED_HOSTS=                 comma-separated whitelisted hostnames
  SYSTEMU_HARNESS_LLM_JUDGE=true                 let an LLM judge ambiguous MEDIUM-risk requests
  SYSTEMU_HARNESS_AUTO_GRANT_MCP=false           reserved auto-grant switch for MCP kind
  SYSTEMU_HARNESS_ALLOWED_MCP_SERVERS=           comma-separated allowlisted MCP server_ids
  SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS=             comma-separated hosts exempt from the SSRF literal-IP deny
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip())
    except (ValueError, AttributeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip())
    except (ValueError, AttributeError):
        return default


def _env_set(key: str, default: Set[str]) -> Set[str]:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    return {item.strip() for item in raw.split(",") if item.strip()}


@dataclass
class HarnessPolicy:
    """Arbitration policy controlling which HarnessRequests are auto-granted.

    All booleans use default-deny (False) for the dangerous kinds (TOOL,
    ACCESS, SUBAGENT) and conservative-allow for lighter ones (SKILL,
    COMPUTE), reflecting the design spec §8 risk posture.

    Fields
    ------
    auto_grant_tool
        Allow automatic GRANT for TOOL requests (forge new code). Very
        high risk — default False. Even if True, only low-risk tool ops
        (reuse existing) are auto-granted; forge (new code) always HIGH.
    auto_grant_skill
        Allow automatic GRANT for SKILL (new procedural text). Lower risk
        — default True when within policy limits.
    auto_grant_access
        Allow automatic GRANT for ACCESS requests. Default False. Read
        of a whitelisted resource may be granted at LOW band; anything
        else escalates.
    auto_grant_compute
        Allow automatic GRANT for COMPUTE budget requests within ceiling.
        Default True — adding more tokens/iterations is relatively safe.
    auto_grant_subagent
        Allow automatic GRANT for SUBAGENT requests. Default False.
        Within-budget sub-shadows are MEDIUM; beyond depth → always HIGH.
    max_requests_per_run
        Hard cap on total HarnessRequests accepted per run. Requests
        beyond this cap are DENY (non-blocking) or ESCALATE (blocking).
    max_compute_ceiling
        Maximum compute multiplier grantable without operator review
        (1.0 = 100% of baseline budget).
    max_subagent_depth
        Maximum allowed nesting depth for sub-Shadows.
    max_subagent_budget_fraction
        Max fraction of parent run budget a subagent may consume.
    allowed_resources
        Whitelist of resource names auto-grantable at LOW risk for
        ACCESS read requests (e.g. {"vault/tools", "vault/skills"}).
    allowed_packages
        Whitelist of pip packages that may be installed without
        escalation (empty = none allowed without review).
    allowed_hosts
        Whitelist of hostnames for network egress (empty = all escalate).
    llm_judge_enabled
        Allow the Governor to route genuinely-ambiguous MEDIUM-risk requests
        (the ``needs_llm_judgment`` cases the arbiter flags) to an LLM judge
        instead of always escalating. Default True. The judge is conservative:
        it only GRANTs when clearly safe, otherwise it ESCALATEs.
    auto_grant_mcp
        Reserved auto-grant switch for the MCP kind. Default False. Even when
        True, NEW external servers always ESCALATE (operator approval); only
        re-attach/allowlisted servers grant at LOW — mirroring auto_grant_tool.
    allowed_mcp_servers
        Allowlist of MCP ``server_id``s that may connect without escalation
        (LOW GRANT, treated like a re-attach). Empty = all new servers escalate.
    allowed_mcp_hosts
        Hosts exempt from the SSRF literal-IP deny in _arbitrate_mcp (e.g.
        ``{"127.0.0.1"}`` for a trusted local dev MCP server). Empty = none.
    """

    # ── per-kind auto-grant switches ──────────────────────────────────────────
    auto_grant_tool:      bool = False
    auto_grant_skill:     bool = True
    auto_grant_access:    bool = False
    auto_grant_compute:   bool = True
    auto_grant_subagent:  bool = False

    # ── LLM judgment for ambiguous MEDIUM-risk requests ───────────────────────
    llm_judge_enabled:    bool = True

    # ── ceiling / depth limits ────────────────────────────────────────────────
    max_requests_per_run:          int   = 8
    max_compute_ceiling:           float = 1.0
    max_subagent_depth:            int   = 1
    max_subagent_budget_fraction:  float = 0.5

    # ── allowlists ────────────────────────────────────────────────────────────
    allowed_resources: Set[str] = field(default_factory=set)
    allowed_packages:  Set[str] = field(default_factory=set)
    allowed_hosts:     Set[str] = field(default_factory=set)

    # ── MCP runtime-connect (P3) ──────────────────────────────────────────────
    auto_grant_mcp:      bool        = False
    allowed_mcp_servers: Set[str]    = field(default_factory=set)
    allowed_mcp_hosts:   Set[str]    = field(default_factory=set)

    # ── MCP remote-transport / OAuth policy (P4 ADDS ONLY THESE TWO) ───────────
    # (allowed_mcp_hosts is P3-owned; P4 must not declare it here.)
    mcp_require_tls:     bool        = True
    mcp_oauth_timeout_s: int         = 1800

    @classmethod
    def from_config(cls, config: Dict[str, Any] | None = None) -> "HarnessPolicy":
        """Build a HarnessPolicy from a config dict + environment overrides.

        Priority (highest wins):
          1. Environment variables (SYSTEMU_HARNESS_*)
          2. config dict keys (matching field names)
          3. Compiled-in defaults

        The config dict may use the same names as the dataclass fields, or
        the SYSTEMU_HARNESS_* env-var suffix lowercased
        (e.g. ``auto_grant_tool`` or ``AUTO_GRANT_TOOL``).
        """
        cfg = config
        _MISSING = object()

        def _get(key: str):
            """Look up ``key`` whether ``cfg`` is a dict, an object, or None.

            For objects (e.g. the runtime ``Config``), tries the bare field name
            and a ``harness_``-prefixed variant; returns ``_MISSING`` if absent.
            """
            if cfg is None:
                return _MISSING
            if isinstance(cfg, dict):
                return cfg.get(key, _MISSING)
            for attr in (key, "harness_" + key):
                if hasattr(cfg, attr):
                    return getattr(cfg, attr)
            return _MISSING

        def _cfg_bool(key: str, default: bool) -> bool:
            """Config first, then env, then default."""
            v = _get(key)
            if v is not _MISSING:
                if isinstance(v, bool):
                    return v
                return str(v).strip().lower() in ("1", "true", "yes", "on")
            return _env_bool("SYSTEMU_HARNESS_" + key.upper(), default)

        def _cfg_int(key: str, default: int) -> int:
            v = _get(key)
            if v is not _MISSING:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
            return _env_int("SYSTEMU_HARNESS_" + key.upper(), default)

        def _cfg_float(key: str, default: float) -> float:
            v = _get(key)
            if v is not _MISSING:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
            return _env_float("SYSTEMU_HARNESS_" + key.upper(), default)

        def _cfg_bool_env(key: str, env_suffix: str, default: bool) -> bool:
            """Like _cfg_bool but with an explicit env-var suffix.

            Used when the config attr name and the SYSTEMU_HARNESS_* env-var
            suffix differ (e.g. attr ``llm_judge_enabled`` vs env
            ``SYSTEMU_HARNESS_LLM_JUDGE``).
            """
            v = _get(key)
            if v is not _MISSING:
                if isinstance(v, bool):
                    return v
                return str(v).strip().lower() in ("1", "true", "yes", "on")
            return _env_bool("SYSTEMU_HARNESS_" + env_suffix, default)

        def _cfg_set(key: str, default: Set[str]) -> Set[str]:
            v = _get(key)
            if v is not _MISSING:
                if isinstance(v, (set, list, tuple)):
                    return set(v)
                if isinstance(v, str):
                    return {i.strip() for i in v.split(",") if i.strip()}
            return _env_set("SYSTEMU_HARNESS_" + key.upper(), default)

        return cls(
            auto_grant_tool=_cfg_bool("auto_grant_tool", False),
            auto_grant_skill=_cfg_bool("auto_grant_skill", True),
            auto_grant_access=_cfg_bool("auto_grant_access", False),
            auto_grant_compute=_cfg_bool("auto_grant_compute", True),
            auto_grant_subagent=_cfg_bool("auto_grant_subagent", False),
            # Config attr is ``llm_judge_enabled``; env var is the shorter
            # ``SYSTEMU_HARNESS_LLM_JUDGE`` (per design spec §8).
            llm_judge_enabled=_cfg_bool_env("llm_judge_enabled", "LLM_JUDGE", True),
            max_requests_per_run=_cfg_int("max_requests_per_run", 8),
            max_compute_ceiling=_cfg_float("max_compute_ceiling", 1.0),
            max_subagent_depth=_cfg_int("max_subagent_depth", 1),
            max_subagent_budget_fraction=_cfg_float("max_subagent_budget_fraction", 0.5),
            allowed_resources=_cfg_set("allowed_resources", set()),
            allowed_packages=_cfg_set("allowed_packages", set()),
            allowed_hosts=_cfg_set("allowed_hosts", set()),
            auto_grant_mcp=_cfg_bool("auto_grant_mcp", False),
            allowed_mcp_servers=_cfg_set("allowed_mcp_servers", set()),
            allowed_mcp_hosts=_cfg_set("allowed_mcp_hosts", set()),
            mcp_require_tls=_cfg_bool("mcp_require_tls", True),
            mcp_oauth_timeout_s=_cfg_int("mcp_oauth_timeout_s", 1800),
        )
