"""v0.9.2 Layer 2 — Episodic Memory.

After a chat task / scroll run completes, capture() builds a SessionSummary
via Tier-1 LLM and persists it to the vault. Future sessions can recall via
vault.search_session_summaries.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.core.models import SessionSummary
from systemu.core.llm_router import llm_call_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "summarize_session.md"


def _has_llm_provider(config) -> bool:
    """True iff at least one LLM provider is usable. Consumes THE ONE MINT.

    When NO provider is usable the Tier-1 summarize call below cannot succeed —
    it can only 401 or, keyless/offline, stall through the router's retry ladder
    (``_API_TIMEOUT_SECONDS`` × ``_NETWORK_MAX_RETRIES``, ~380s) before failing.
    ``capture`` already degrades to ``None`` on that failure, so short-circuiting
    here is behavior-equivalent — just fast. Never raises.

    F19 / DEC-43 form (i). This used to ENUMERATE FOUR CONFIG ATTRIBUTE NAMES,
    duplicated verbatim in ``open_world_planner`` — which is exactly why a grep
    for the env var could never find every re-derivation site. It also silently
    excluded the keyless provider, so an Ollama-only install had cross-session
    recall switched off with nothing said.

    ``any_provider_usable`` spends the loopback witness ONLY when a tier
    explicitly selects the keyless provider. That matters here: this runs at the
    end of every capture, and an unconditional probe would put ~1-2 s of network
    I/O on a path that has none today."""
    from systemu.runtime import provider_status as _ps
    return _ps.any_provider_usable(config)


#: A distribution name an operator can be handed inside a `pip install` line.
#: Anything else came from somebody else's exception and is not put on a
#: console as a command.
_PACKAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")

#: Where a plain ``ImportError`` keeps the name that ``ModuleNotFoundError``
#: puts in ``.name``.
_NO_MODULE_NAMED = re.compile(r"No module named ['\"]([^'\"]+)['\"]")


def _missing_package_name(exc) -> str:
    """The top-level distribution an import failure is about, or ``""``.

    ``anthropic.types`` is ``anthropic``: handing the operator
    ``pip install anthropic.types`` would be a remedy that cannot run, which is
    worse than the module name it replaced.
    """
    name = getattr(exc, "name", None)
    if type(name) is not str or not name.strip():
        # A plain ImportError carries the name only in its text.
        text = str(exc)
        match = _NO_MODULE_NAMED.search(text if type(text) is str else "")
        if match is None:
            return ""
        name = match.group(1)
    top = name.split(".")[0].strip()
    return top if _PACKAGE_NAME.match(top) else ""


def missing_dependency_remedy(exc) -> Optional[str]:
    """The remedy sentence for an import failure, or ``None`` to change nothing.

    D13 -- a degradation that names a Python module names nothing the operator
    can act on. ``No module named 'anthropic'`` is true, and it leaves them to
    guess that the fix is a pip install of a package they never asked for.

    Two answers, and never a third:
      * a package :mod:`systemu.runtime.optional_deps` REGISTERS -> that
        group's own remedy, built by the one ``install_command`` builder so the
        shell-safe quoting cannot drift, plus the post-install step the wheel
        alone does not cover;
      * anything else -> the plain ``pip install <package>`` line. Never an
        invented ``systemu[...]`` extra: pip accepts an unknown extra and
        silently installs nothing, which is a remedy that reports success and
        fixes the problem not at all.

    PURE OF THE ENVIRONMENT ON PURPOSE. The import ALREADY failed -- that is
    the witness, and it is the only one consulted. Asking
    ``optional_deps.is_installed`` a second time would be a cheaper source that
    can disagree with it (DEC-34): a present-but-broken distribution answers
    "installed" and would take the remedy away from the operator who most needs
    it.

    ``type(exc) is T`` rather than ``isinstance`` (DEC-36): the two concrete
    classes Python raises for a missing module are pinned in this frame, and
    every other shape -- including an object that merely claims to be an
    ImportError -- falls to ``None``, which is the "leave the line exactly as
    it was" answer.
    """
    kind = type(exc)
    if kind is not ModuleNotFoundError and kind is not ImportError:
        return None
    package = _missing_package_name(exc)
    if not package:
        return None

    from systemu.runtime import optional_deps
    group = optional_deps.group_for_package(package)
    if group is None:
        return "the {p} SDK is not installed (pip install {p})".format(p=package)

    remedy = "{label} is not installed ({pkgs}). Install it with: {cmd}".format(
        label=group.label, pkgs=", ".join(group.packages),
        cmd=optional_deps.install_command(group.packages))
    if group.post_install:
        remedy = "{r}  Then: {p}".format(r=remedy, p=group.post_install)
    return remedy


def _degraded_detail(reason: str, exc) -> str:
    """What goes after ``Reason:`` -- the remedy when there is one.

    Best-effort in the strict sense: the NOTICE is the load-bearing half and
    the remedy is polish on it, so a helper that raises costs the operator the
    remedy and never the warning.
    """
    try:
        remedy = missing_dependency_remedy(exc)
    except Exception:
        logger.debug("[Episodic] remedy lookup failed", exc_info=True)
        return reason
    return remedy if type(remedy) is str and remedy else reason


def _warn_operator_degraded(*, session_id: str, reason: str, exc=None) -> None:
    """F12 / P1 — cross-session recall is an ADVERTISED capability. When a run
    produces no summary, the task still reports SUCCESS, so a ``logger.warning``
    leaves the operator believing a feature ran that did not (DEC-34: a false
    assertion of capability). Name the feature, not just "an LLM call failed" —
    the router's own notice cannot tell the operator WHICH capability went dark.
    Best-effort: never break the end-of-run path.

    D13 — ``exc``, when the caller has it, is what turns "Reason: No module
    named 'anthropic'" into an install command. The operator gets the remedy;
    the event record below keeps the raw reason, because trading one blindness
    for another is not a fix."""
    logger.warning("[Episodic] summarize failed for %s: %s", session_id, reason)
    try:
        from systemu.interface.notifications import log_event
        detail = _degraded_detail(reason, exc)
        # ASCII-only (DEC-32c) — this crosses into event_log.jsonl, the
        # dashboard panes and a Windows console.
        msg = (
            "Cross-session recall did not record this session: no episodic "
            f"summary was stored, so future sessions cannot recall it. Reason: {detail}"
        ).encode("ascii", "backslashreplace").decode("ascii")
        log_event(
            "WARNING", "memory", msg,
            {"kind": "episodic_degraded", "session_id": session_id,
             "reason": reason[:500], "remedy": detail[:500] if detail != reason else ""},
        )
    except Exception:
        logger.error("[Episodic] degraded notice failed to emit for %s", session_id)


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _new_summary_id() -> str:
    return f"session_summary_{secrets.token_hex(4)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def capture(
    *,
    vault,
    session_id: str,
    intent: str,
    chat_result: Optional[str],
    files_produced: List[str],
    status: str,
    config,
    execution_id: Optional[str] = None,
    user_id: Optional[str] = None,
    raw_chat_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
) -> Optional[SessionSummary]:
    """Tier-1 summarize a finished run and persist as SessionSummary.

    Returns the persisted SessionSummary, or None when:
    - config.episodic_memory_enabled is False
    - no LLM provider key is configured (behavior-equivalent to a failed call —
      skips the doomed Tier-1 call instead of stalling on it; see _has_llm_provider)
    - LLM call fails (degraded — logged at WARNING)
    - a SessionSummary already exists for this session_id (idempotent skip)
    """
    if not getattr(config, "episodic_memory_enabled", True):
        return None

    # Idempotency check
    existing = vault.query_session_summaries(limit=None)
    if any(s.session_id == session_id for s in existing):
        logger.debug("[Episodic] session_id %s already summarized; skipping", session_id)
        return None

    # No configured LLM provider → the Tier-1 summarize call cannot succeed (it
    # would 401, or — keyless/offline — stall through the router's retry ladder for
    # ~380s before failing). ``capture`` already returns None on that failure, so
    # short-circuit to the SAME degraded value now: behavior-equivalent, just fast.
    # This keeps keyless/offline production runs (and the hermetic test suite) from
    # a doomed end-of-run LLM call.
    if not _has_llm_provider(config):
        logger.debug("[Episodic] no LLM provider configured; skipping summarize for %s", session_id)
        return None

    user_payload = {
        "intent": intent,
        "status": status,
        "chat_result": chat_result,
        "files_produced": files_produced,
    }
    try:
        result = llm_call_json(
            tier=1,
            system=_load_system_prompt(),
            user=json.dumps(user_payload, separators=(",", ":")),
            config=config,
            max_tokens=400,
            temperature=0.2,
        )
    except Exception as exc:
        _warn_operator_degraded(session_id=session_id, reason=str(exc), exc=exc)
        return None

    if not isinstance(result, dict):
        _warn_operator_degraded(
            session_id=session_id,
            reason=f"the summarizer returned {type(result).__name__}, not a JSON object")
        return None

    max_chars = int(getattr(config, "episodic_summary_max_chars", 800))
    max_tags = int(getattr(config, "episodic_tags_max_count", 8))

    outcome = str(result.get("outcome_summary") or "")[:max_chars]
    facts = result.get("key_facts_learned") or []
    if not isinstance(facts, list):
        facts = []
    facts = [str(f) for f in facts][:20]

    tags = result.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).lower().strip() for t in tags if str(t).strip()][:max_tags]

    summary = SessionSummary(
        id=_new_summary_id(),
        session_id=session_id,
        execution_id=execution_id,
        user_id=user_id,
        started_at=started_at or _now(),
        completed_at=_now(),
        status=status,
        intent=intent or "",
        outcome_summary=outcome,
        key_facts_learned=facts,
        files_produced=files_produced or [],
        tags=tags,
        raw_chat_id=raw_chat_id,
    )

    try:
        vault.append_session_summary(summary)
    except Exception as exc:
        logger.warning("[Episodic] persist failed for %s: %s", session_id, exc)
        return None

    logger.info("[Episodic] captured session %s — tags=%s", session_id, tags)
    return summary
