"""Capability wishlist (P2 v1) - what the operator wishes systemu could do.

A wish is an operator-AUTHORED user FACT (`tags=["wish"]`, `source="table"`) in
the existing append-only fact store.  There is deliberately NO new store:

  * nothing here writes OnTheTable - `table_reconciler.project()` stays its sole
    writer (DEC-10), and the Table page's wishlist section is a fact READER;
  * dismissal goes through the existing supersede API (`user_profile.forget_fact`)
    so a dismissed wish is still in the log, just no longer open.

Matching a wish to a shipped capability is deterministic keyword overlap - at
least ``MATCH_MIN_WORDS`` significant words (len > 3, lowercased, stopword-
stripped) appearing in a tool's name+description.  Honest and dumb beats clever
and wrong: an LLM matcher would need its own ruled criteria, and a wrong "you
wished for this" is worse than no nudge at all.  v1 has no LLM in this path.

THE HONESTY WALL: `ready_tools` is the fence in front of every nudge.  Only a
tool that is DEPLOYED (or UPGRADED) and enabled can be named as a fulfilled
wish.  A `proposed` or `forged` tool is a plan, not a capability, and saying
"the toolbox can now do this" about one would be a promise rather than a fact.

Everything here is defensive: a wishlist that raises would take down the page
it renders on, and no wish is worth that.  The one deliberate exception is that
`add_wish` REPORTS failure (returns None) instead of swallowing it, so the UI
never claims to have saved something it did not.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: the fact tag every wish carries (the read filter)
WISH_TAG = "wish"

#: the fact-text prefix; the operator's own words follow it verbatim
WISH_PREFIX = "wish:"

#: `UserFact.source` for a wish - the surface that captured it
WISH_SOURCE = "table"

#: reason recorded on the supersede when the operator dismisses a wish
DISMISS_REASON = "wish_dismissed"

#: how many significant words must overlap before a tool counts as a match.
#: Stated as a constant so the threshold is testable and cannot drift silently.
MATCH_MIN_WORDS = 2

#: a word shorter than this contributes nothing (len > 3, i.e. 4+ characters)
_MIN_WORD_LEN = 4

#: tool statuses that mean "this exists and can run today"
_READY_STATUSES = frozenset({"deployed", "upgraded"})

#: words that carry no capability signal.  Kept small and literal: this is a
#: noise filter, not linguistics.  Anything here is a word an operator writes
#: while describing ANY wish, so counting it would match every tool equally.
_STOPWORDS = frozenset({
    "able", "about", "also", "anything", "because", "been", "before", "could",
    "does", "done", "each", "else", "even", "ever", "every", "everything",
    "from", "have", "here", "into", "just", "keep", "like", "make", "many",
    "more", "most", "much", "must", "need", "needs", "only", "other", "over",
    "please", "really", "same", "should", "some", "something", "still",
    "stuff", "such", "system", "systemu", "than", "that", "their", "them",
    "then", "there", "these", "they", "thing", "things", "this", "those",
    "very", "want", "wants", "were", "what", "when", "where", "which",
    "while", "will", "wish", "wishes", "with", "would", "your", "yours",
})

_WORD_RE = re.compile(r"[a-z0-9]+")


# --- writing -----------------------------------------------------------------
def add_wish(vault, text: Optional[str]) -> Optional[str]:
    """Record a wish.  Returns the new fact id, or None if nothing was saved.

    The value is the operator's own typed sentence - operator-AUTHORED in the
    strong sense, which is why this caller is on the R-A16 operator-surface
    allowlist rather than stamping an origin.  Blank input is refused outright
    (an empty wish is a mis-click, not a preference).
    """
    body = (text or "").strip()
    if not body:
        return None
    try:
        from systemu.runtime.user_profile import add_fact
        uf = add_fact(vault, f"{WISH_PREFIX} {body}",
                      source=WISH_SOURCE, tags=[WISH_TAG])
        return uf.id
    except Exception:
        logger.warning("[Wishes] could not record a wish", exc_info=True)
        return None


def dismiss_wish(vault, fact_id: Optional[str]) -> bool:
    """Close a wish by SUPERSEDING its fact.  True when one was closed.

    Nothing is deleted - the fact log stays append-only and the wish simply
    stops being open.  The value written is a fixed in-module sentinel on an
    operator click, so this path never carries operator or content text.
    """
    fid = (fact_id or "").strip()
    if not fid:
        return False
    try:
        from systemu.runtime.user_profile import forget_fact
        return bool(forget_fact(vault, fid, reason=DISMISS_REASON))
    except Exception:
        logger.warning("[Wishes] could not dismiss %s", fid, exc_info=True)
        return False


# --- reading -----------------------------------------------------------------
def open_wishes(vault) -> List[Tuple[str, str]]:
    """Every wish still open, NEWEST FIRST, as (fact_id, text) pairs.

    Superseded facts are already excluded by `get_facts`, so a dismissed wish
    never comes back.  An unreadable store reads as "no wishes" - the cost is an
    empty section, never a broken page.
    """
    try:
        from systemu.runtime.user_profile import get_facts
        facts = get_facts(vault, tags=[WISH_TAG])
    except Exception:
        return []
    out: List[Tuple[str, str]] = []
    for f in facts:
        raw = getattr(f, "fact", "") or ""
        if not raw.startswith(WISH_PREFIX):
            continue
        body = raw[len(WISH_PREFIX):].strip()
        if body:
            out.append((getattr(f, "id", "") or "", body))
    out.reverse()                      # get_facts is newest-LAST
    return out


# --- matching ----------------------------------------------------------------
#: how much of a wish a one-line nudge may quote before it stops being one line
QUOTE_LIMIT = 90


def short_wish(text: Optional[str], limit: int = QUOTE_LIMIT) -> str:
    """The wish trimmed to one line, for quoting back in a nudge.

    Whitespace is collapsed (a pasted wish can carry newlines) and truncation is
    VISIBLE - an ASCII ellipsis - so a quoted wish is never silently altered
    into something the operator did not write.
    """
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    return body[:max(0, limit - 3)].rstrip() + "..."


def significant_words(text: Optional[str]) -> List[str]:
    """The words a match may be built from: lowercased, 4+ characters, not a
    stopword, de-duplicated with first-seen order preserved."""
    seen: List[str] = []
    for w in _WORD_RE.findall((text or "").lower()):
        if len(w) < _MIN_WORD_LEN or w in _STOPWORDS or w in seen:
            continue
        seen.append(w)
    return seen


def ready_tools(tools: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """The tools a nudge is ALLOWED to name - deployed/upgraded and enabled.

    This is the honesty wall.  Everything else in the index is a plan, and a
    plan is not something the toolbox "can now do".
    """
    out: List[Dict[str, Any]] = []
    for t in (tools or []):
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "") not in _READY_STATUSES:
            continue
        if t.get("enabled") is False:
            continue
        out.append(t)
    return out


def _overlap(words: List[str], tool: Dict[str, Any]) -> int:
    hay = "{} {}".format(tool.get("name") or "", tool.get("description") or "").lower()
    if not hay.strip():
        return 0
    return sum(1 for w in words if w in hay)


def match_wish(wish_text: Optional[str],
               tools: Optional[List[Any]]) -> Optional[Dict[str, Any]]:
    """The best tool whose name+description carries >= MATCH_MIN_WORDS of the
    wish's significant words, or None.

    Substring containment, not stemming: "invoice" matches "invoices" and does
    not match "invoic".  That bluntness is the point - the threshold, not the
    cleverness, is what keeps a false "you wished for this" out.  Ties resolve
    to the earliest tool in the list, so the result is deterministic for a
    given index order.
    """
    words = significant_words(wish_text)
    if len(words) < MATCH_MIN_WORDS:
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for t in (tools or []):
        if not isinstance(t, dict):
            continue
        score = _overlap(words, t)
        if score >= MATCH_MIN_WORDS and score > best_score:
            best, best_score = t, score
    return best
