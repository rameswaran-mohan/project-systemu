"""v0.9.2 session_search + session_recall — LLM-facing episodic memory tools."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _to_lightweight(summary) -> Dict[str, Any]:
    """Compact dict shape used in search results to save tokens."""
    return {
        "session_id": summary.session_id,
        "intent": summary.intent,
        "outcome_summary": summary.outcome_summary,
        "completed_at": summary.completed_at.isoformat(),
        "status": summary.status,
        "tags": list(summary.tags or []),
    }


def _to_full(summary) -> Dict[str, Any]:
    """Full dict — used by recall."""
    d = _to_lightweight(summary)
    d.update({
        "id": summary.id,
        "execution_id": summary.execution_id,
        "user_id": summary.user_id,
        "started_at": summary.started_at.isoformat(),
        "key_facts_learned": list(summary.key_facts_learned or []),
        "files_produced": list(summary.files_produced or []),
        "raw_chat_id": summary.raw_chat_id,
    })
    return d


def session_search(
    *,
    vault,
    query: str,
    limit: int = 5,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search prior session summaries by keyword.

    Backend-aware via vault.search_session_summaries (file scan, sqlite FTS5,
    postgres tsvector). Returns lightweight dicts to save LLM tokens.
    """
    summaries = vault.search_session_summaries(query, user_id=user_id, limit=limit)
    return [_to_lightweight(s) for s in summaries]


def session_recall(*, vault, session_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the full SessionSummary for one prior session as a dict.

    None if not found. Uses query_session_summaries (no LLM call).
    """
    summaries = vault.query_session_summaries(user_id=user_id, limit=None)
    for s in summaries:
        if s.session_id == session_id:
            return _to_full(s)
    return None
