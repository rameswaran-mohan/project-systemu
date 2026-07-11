"""IMPL-7 / §5.6 — the committed-effects ledger (pure, deterministic render).

Spec: *"Every precise-handoff card — and any terminal BLOCKED state — MUST
enumerate the external effects already committed this run, rendered
DETERMINISTICALLY from persisted ``ExternalEvidence`` (never from model prose):
'already done: issue #412 created; 3 of 5 invoices imported.' A handoff that is
precise about what it needs but silent about what it already did is not honest."*

This module is the render half. It reads the persisted ``ExternalEvidence`` store
(``context._external_evidence`` — a ``dict[str, dict]`` keyed by objective_id as a
string, value = the evidence dict) and emits a deterministic ledger block. It is:

  * confirmed-only — an entry credits ONLY when ``entry["confirmed"] is True``
    (the same fail-closed bool the S4 read gate uses; a truthy ``1`` / ``"yes"``
    is a MALFORMED entry that must NOT be listed). ``confirmed`` is set ONLY by
    the deterministic S3/R-A7 matcher, NEVER by an LLM — so this ledger is
    ground-truth, not model prose.
  * deterministic — sorted by ``int(objective_id)`` so the same store always
    renders identically.
  * detail-only — emits ONLY the persisted ``detail`` string (contractually a
    short human-readable note, "never a secret" — see ExternalEvidence.detail).
    Never any token, key, or model narration.
  * defensive — a ``None`` / non-dict store, a non-dict entry, a missing/empty
    ``detail``, or a non-int key can NEVER raise; the offending entry is simply
    treated as "no effect".

No LLM. No I/O. Pure function of its input dict.
"""
from __future__ import annotations

from typing import List


def committed_effect_details(external_evidence: dict) -> List[str]:
    """Return the sorted list of confirmed ``detail`` strings.

    Sorted by ``int(objective_id)`` (the store key, falling back to the entry's
    own ``objective_id``). Entries whose key/id is not an int sort deterministically
    AFTER the int-keyed ones (by their detail text) so the output is still stable.

    Filters out: a non-dict store, a non-dict entry, a RECORD-ONLY shadow-meter
    entry (``shadow is True`` — an instrumentation artifact, never a committed
    effect), an entry not ``confirmed is True``, and an entry with a
    missing/blank/non-str ``detail``. Never raises.
    """
    if not isinstance(external_evidence, dict):
        return []

    pairs: List[tuple] = []
    for key, entry in external_evidence.items():
        try:
            if not isinstance(entry, dict):
                continue
            # R-A13b-1: a RECORD-ONLY shadow-meter measurement (shadow=True) is an
            # instrumentation artifact, NEVER an operator-facing committed effect —
            # even when it carries confirmed=True purely as its would-credit
            # measurement. Skip it, symmetric with _read_external_ok (shadow_runtime.py),
            # which also refuses a shadow entry. A shadow-meter would-credit belongs
            # only to the metrics report, not this ledger; a LIVE entry never carries
            # ``shadow`` ⇒ the operator ledger is unchanged for OFF/ENFORCE runs.
            if entry.get("shadow") is True:
                continue
            # fail-closed: ONLY the real bool True credits (mirrors _read_external_ok).
            if entry.get("confirmed") is not True:
                continue
            detail = entry.get("detail")
            if not isinstance(detail, str):
                continue
            detail = detail.strip()
            if not detail:
                continue
            pairs.append((_sort_rank(key, entry), detail))
        except Exception:
            # a malformed entry is a no-effect, never a crash.
            continue

    pairs.sort(key=lambda p: p[0])
    return [detail for _rank, detail in pairs]


def render_committed_effects(external_evidence: dict) -> str:
    """Render the confirmed committed-effects as a deterministic ledger block.

    Returns ``""`` when there are zero confirmed effects (so callers append
    nothing). Otherwise::

        Already committed this run:
          • <detail>
          • <detail>
    """
    details = committed_effect_details(external_evidence)
    if not details:
        return ""
    lines = "\n".join(f"  • {d}" for d in details)
    return f"Already committed this run:\n{lines}"


def _sort_rank(key, entry: dict) -> tuple:
    """A total, deterministic sort rank.

    ``(0, int_id, "")`` for a resolvable int objective_id (sorts first, by id);
    ``(1, 0, detail)`` for an unresolvable key (sorts after, by detail text) so
    the render is stable even for a malformed/non-int key.
    """
    oid = _as_int(key)
    if oid is None:
        oid = _as_int(entry.get("objective_id"))
    if oid is None:
        return (1, 0, str(entry.get("detail") or ""))
    return (0, oid, "")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
