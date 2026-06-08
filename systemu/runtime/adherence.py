"""Execution-Adherence Resolver — Phase 3.2 of the Intent Engine.

Defines the adherence spectrum:
  strict   — Strict-Replay: the agent follows the SOP step-by-step; no
             autonomous deviation is permitted.
  guided   — Guided-Autonomy: the agent follows intent and key steps but
             may adapt details to the current context.
  free     — Free-Agent: the agent reasons from first principles; the SOP
             (if any) is advisory only.

The public API is:
  resolve_adherence(config, *, request_kind, sop_adherence) -> str
  ADHERENCE_PRESETS: dict[str, dict]
  apply_preset(name) -> dict
"""

from __future__ import annotations

from typing import Any

# ── valid adherence levels ─────────────────────────────────────────────────────
_VALID = frozenset({"free", "guided", "strict"})

# ── request kinds that should default to something stricter than "free" ────────
_SOP_KINDS = frozenset({"record", "sop"})


def resolve_adherence(
    config: Any,
    *,
    request_kind: str = "chat",
    sop_adherence: str | None = None,
) -> str:
    """Return the effective adherence level for a single request.

    Priority rules
    --------------
    1. If ``config.execution_adherence`` is explicit (free/guided/strict) it
       wins unconditionally — the operator pinned a deployment-wide policy.
    2. If ``config.execution_adherence`` is "auto", the runtime default kicks
       in:
       - ``request_kind == "chat"``         → "free"
       - ``request_kind in {"record","sop"}`` → use ``sop_adherence`` if it is
         a valid level, otherwise "guided".

    Parameters
    ----------
    config:
        Any object with an ``execution_adherence`` attribute, or a plain dict
        with key ``"execution_adherence"``.  Typically a ``sharing_on.config.Config``
        instance.
    request_kind:
        One of "chat", "record", "sop" (or any future kind).  Unknown kinds
        are treated like "chat".
    sop_adherence:
        The per-SOP adherence saved in Phase 3.3.  Only consulted when
        ``config.execution_adherence == "auto"`` and ``request_kind`` is a
        SOP kind.

    Returns
    -------
    str
        One of "free", "guided", "strict".
    """
    # --- read the config value ---
    if isinstance(config, dict):
        raw = config.get("execution_adherence", "auto")
    else:
        raw = getattr(config, "execution_adherence", "auto")

    level = (raw or "auto").strip().lower()

    # --- explicit operator pin wins ---
    if level in _VALID:
        return level

    # --- auto resolution ---
    if request_kind in _SOP_KINDS:
        if sop_adherence and sop_adherence.strip().lower() in _VALID:
            return sop_adherence.strip().lower()
        return "guided"

    # chat (and all other unknown kinds) → free
    return "free"


# ── presets ────────────────────────────────────────────────────────────────────

#: Coherent named presets pairing an adherence level with harness auto-grant
#: flags and a forge-mode hint.  Keys of each preset value are environment
#: variable names (or Config field names where noted) that ``apply_preset``
#: returns verbatim so callers can write them to .env / os.environ.
#:
#: Preset semantics
#: ----------------
#: locked_sop   — Maximum fidelity to recorded SOPs.  No auto-grant of any
#:                kind; all harness requests require operator review.  Use for
#:                compliance-critical or sensitive workflows.
#: assisted      — Balanced: guided adherence + conservative auto-grants for
#:                low-risk operations (skill reuse, compute within ceiling).
#:                Good for daily team automation.
#: autonomous    — Maximum throughput.  Free-Agent mode; broad auto-grants
#:                for low/medium risk ops.  Use in trusted dev environments.
ADHERENCE_PRESETS: dict[str, dict] = {
    "locked_sop": {
        # adherence
        "SYSTEMU_EXECUTION_ADHERENCE": "strict",
        # harness: no auto-grants at all — everything escalates for review
        "SYSTEMU_HARNESS_AUTO_GRANT_TOOL":     "false",
        "SYSTEMU_HARNESS_AUTO_GRANT_SKILL":    "false",
        "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS":   "false",
        "SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE":  "false",
        "SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT": "false",
        # forge mode: review-only (no new code without operator sign-off)
        "SYSTEMU_AUTO_FORGE_TOOLS": "false",
    },
    "assisted": {
        # adherence
        "SYSTEMU_EXECUTION_ADHERENCE": "guided",
        # harness: allow low-risk ops; block forge + subagent
        "SYSTEMU_HARNESS_AUTO_GRANT_TOOL":     "false",
        "SYSTEMU_HARNESS_AUTO_GRANT_SKILL":    "true",
        "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS":   "false",
        "SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE":  "true",
        "SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT": "false",
        # forge mode: off
        "SYSTEMU_AUTO_FORGE_TOOLS": "false",
    },
    "autonomous": {
        # adherence
        "SYSTEMU_EXECUTION_ADHERENCE": "free",
        # harness: broad auto-grant for low/medium risk
        "SYSTEMU_HARNESS_AUTO_GRANT_TOOL":     "false",   # forge is always high-risk
        "SYSTEMU_HARNESS_AUTO_GRANT_SKILL":    "true",
        "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS":   "true",
        "SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE":  "true",
        "SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT": "true",
        # forge mode: on (dev/testing only — callers must warn)
        "SYSTEMU_AUTO_FORGE_TOOLS": "false",   # still off by default; caller may override
    },
}


def apply_preset(name: str) -> dict:
    """Return the env/config key-value mapping for a named preset.

    Parameters
    ----------
    name:
        One of "locked_sop", "assisted", "autonomous".

    Returns
    -------
    dict
        A copy of the preset dict (env-var name → string value) ready to
        write to ``.env`` or merge into ``os.environ``.

    Raises
    ------
    KeyError
        If ``name`` is not a known preset.
    """
    try:
        return dict(ADHERENCE_PRESETS[name])
    except KeyError:
        valid = ", ".join(sorted(ADHERENCE_PRESETS))
        raise KeyError(f"Unknown preset {name!r}. Valid presets: {valid}") from None
