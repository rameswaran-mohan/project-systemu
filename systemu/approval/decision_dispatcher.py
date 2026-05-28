"""Decision dispatcher (v0.8.5).

Maps OperatorDecision dedup_key namespaces to pipeline-continuation
handlers. When the dashboard's /insights -> Pending Actions handler
resolves a decision, it calls dispatch() to trigger the appropriate
downstream pipeline without waiting for the hourly sweep.

Pre-v0.8.5 the resolved choice sat in the vault until hourly_shadow_sweep
(cron interval=1h) re-invoked decide_shadow. Operator wait: up to 59:59.
"""
from __future__ import annotations

import logging
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.approval.decision_queue import OperatorDecision
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

Handler = Callable[["OperatorDecision", str, "Config", "Vault"], None]

_HANDLERS: dict[str, Handler] = {}

_handlers_bootstrapped = False


def _ensure_handlers_registered() -> None:
    """Force-import the three pipeline modules whose import-time side-effect
    is to register a dispatcher handler.

    Safe to call repeatedly — Python's import cache short-circuits subsequent
    calls.  This guards against the case where a pipeline module hasn't been
    imported elsewhere during daemon boot (e.g. tool_forge, which has no
    import path from the dashboard's startup recovery sweep).
    """
    global _handlers_bootstrapped
    if _handlers_bootstrapped:
        return
    # noqa: F401 — imported for side-effect (handler registration)
    import systemu.pipelines.shadow_decision  # noqa: F401
    import systemu.pipelines.scroll_refiner   # noqa: F401
    import systemu.pipelines.tool_forge       # noqa: F401
    _handlers_bootstrapped = True


def register(namespace: str, handler: Handler) -> None:
    """Register a handler for a dedup_key namespace.

    Namespace is the part before the first ':' in the dedup_key
    (e.g. 'shadow_decision' from 'shadow_decision:activity_x123').

    Must be called at module import time only. Registration is not
    thread-safe and assumes single-threaded population during process
    startup. `dispatch()` may then be called concurrently from any thread.
    Re-registration is idempotent — last call for a given namespace wins.
    """
    _HANDLERS[namespace] = handler


def dispatch(
    decision: "OperatorDecision",
    choice: str,
    config: "Config",
    vault: "Vault",
) -> None:
    """Route a resolved decision to its registered handler.

    Fail-soft: unknown namespaces are no-ops; handler exceptions are
    caught and logged, never propagated. The UI must remain responsive
    even if a pipeline continuation crashes.
    """
    _ensure_handlers_registered()
    ns = (decision.dedup_key or "").split(":", 1)[0]
    handler = _HANDLERS.get(ns)
    if handler is None:
        logger.debug(
            "[Dispatcher] no handler for namespace %r (decision %s) - skipping",
            ns, decision.id,
        )
        return
    try:
        handler(decision, choice, config, vault)
    except Exception:
        logger.exception(
            "[Dispatcher] handler for namespace %r raised on decision %s",
            ns, decision.id,
        )
