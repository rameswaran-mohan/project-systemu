"""One quiet, declinable growth proposal on Home - derived, never stored.

Rules: at most ONE per render; a declined key NEVER returns (decline is a
user fact tagged proposal_declined); every proposal names an existing surface
(no unwired promises). Deriving at render keeps this module a pure reader -
the OnTheTable store's sole-writer invariant is untouched.
"""
from __future__ import annotations

from typing import Optional, Tuple

_DECLINE_TAG = "proposal_declined"


def _declined(vault) -> set:
    """The set of proposal keys the operator has already turned down.

    An unreadable fact store degrades to "nothing declined": the cost is one
    repeated suggestion, never a broken Home page.
    """
    try:
        from systemu.runtime.user_profile import get_facts
        return {f.fact.split(":", 1)[1].strip()
                for f in get_facts(vault, tags=[_DECLINE_TAG])
                if ":" in (f.fact or "")}
    except Exception:
        return set()


def decline(vault, key: str) -> None:
    """Turn a proposal down for good.

    Deliberately NOT guarded: the caller must be able to tell a persisted
    decline from a failed one, so it never reports a preference it did not
    actually save.
    """
    from systemu.runtime.user_profile import add_fact
    add_fact(vault, f"declined:{key}", source="home", tags=[_DECLINE_TAG])


def derive_proposal(vault) -> Optional[Tuple[str, str, str]]:
    """(key, text, action_route) or None. Pure reads; never raises."""
    try:
        declined = _declined(vault)
        shadows = vault.list_shadows() or []
        tools = vault.list_tools() or []
        if not shadows and "first_shadow" not in declined:
            return ("first_shadow",
                    "You haven't recorded a task yet. Record one chore once - "
                    "it becomes a workflow you can re-run forever.",
                    "/chat")
        if shadows and not tools and "first_forge" not in declined:
            return ("first_forge",
                    "Your workflows so far used only built-in abilities. Ask for "
                    "something I can't do yet and watch me build the tool.",
                    "/chat")
    except Exception:
        pass
    return None
