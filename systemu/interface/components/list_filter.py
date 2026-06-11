"""Shared listing-page filter (W4.1).

One pure, unit-testable filter behind every list page's search + select toolbar
(board 5a). The Tools page pioneered the `_filter_tools(rows, query, status)`
shape; this generalises it so scrolls / activities / shadows / skills share ONE
definition instead of four near-identical copies:

  * `query`        — case-insensitive substring match across `search_keys`
                     (scalar string fields) and `list_search_keys` (list fields,
                     space-joined — e.g. a scroll's ``tags``).
  * `select_value` — exact, case-insensitive match on `select_key`. ``""`` or
                     ``"all"`` means "don't filter on this dimension". The same
                     mechanism serves a *status* select (scrolls/activities/
                     shadows) or a *category* select (skills) — only the key
                     differs.

Tolerant of rows (dicts) missing any of the referenced keys.
"""
from __future__ import annotations

from typing import Iterable, Sequence


def filter_rows(
    rows: Iterable[dict],
    query: str = "",
    select_value: str = "all",
    *,
    search_keys: Sequence[str] = ("name",),
    list_search_keys: Sequence[str] = (),
    select_key: str = "status",
) -> list[dict]:
    """Filter `rows` by a free-text query and a single select dimension.

    Returns a new list (input order preserved). See module docstring for the
    matching semantics. Both filters are ANDed.
    """
    q = (query or "").strip().lower()
    sv = (select_value or "all").strip().lower()
    out: list[dict] = []
    for r in rows:
        if sv not in ("", "all") and str(r.get(select_key, "") or "").lower() != sv:
            continue
        if q:
            parts = [str(r.get(k, "") or "") for k in search_keys]
            for lk in list_search_keys:
                parts.append(" ".join(str(x) for x in (r.get(lk) or [])))
            if q not in " ".join(parts).lower():
                continue
        out.append(r)
    return out


def select_options(
    rows: Iterable[dict], key: str = "status", *, prepend_all: bool = True
) -> list[str]:
    """Sorted unique non-empty values of `key` across `rows`.

    With `prepend_all` (default), the list starts with ``"all"`` so it can drop
    straight into ``ui.select(..., value="all")``.
    """
    vals = sorted({str(r.get(key, "") or "") for r in rows if r.get(key)})
    return (["all"] + vals) if prepend_all else vals
