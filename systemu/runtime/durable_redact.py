"""DEC-31 at the durable writers: a raw value is redacted BEFORE it reaches disk.

Every writer that persists a value the model, a tool or the operator authored --
a tool call's parameters, a prompt, a completion, a tool's output -- runs it
through this module first, so the file only ever holds the masked form. The
writers call it themselves, which is what makes the fence independent of their
callers: a new caller of ``log_event`` or ``append_call`` is covered without
knowing this module exists.

**No new redactor.** This module composes the two the project already ships:

  * ``external_verifier._mask_evidence`` -- the masker the per-call action-audit
    row and the ledger use (key-targeted: a value under a secret-NAMED key is
    dropped; value-shaped: ``Bearer``, ``sk-``, JWT, ``ghp_``, AWS, Slack, long
    hex). It is reached through ``_mask_evidence_strict``, the same traversal
    with the failure left visible (see DEC-27 below).
  * ``messaging.gateway.mask_outbound`` -- the outbound prose chokepoint, the
    span half of ``outbox.redact``. It adds what the evidence mask has no rule
    for: ``password=...`` / ``Authorization: ...`` pairs inside running text,
    the credential after a ``Basic`` / ``Token`` (or other) auth scheme, and
    the password in a URL's userinfo. Those rules live ONLY there, so a
    regression in them reaches this module too -- the auth-scheme spellings
    are pinned through this module's own entry points in
    ``tests/test_outbound_auth_scheme_mask.py``.

``outbox.redact`` itself is NOT used for durable records: it collapses the
WHOLE value to one marker as soon as any span in it looks like a credential,
which is right for a file name and wrong for a 20 000-character prompt that
happens to quote a git sha.

Three shapes, one per kind of record:

  * :func:`mask_params` -- a parameter-like structure (a tool call's
    ``parameters``, a tool result, an LLM decision): secret-named keys dropped,
    every string value shape- and prose-masked, keys kept.
  * :func:`mask_values` -- a structure whose KEYS are the code's own
    (``execution_id``, ``origin``, ...): every string value masked, no key rule,
    so a ``session_id`` the reader needs is not blanked by the ``sess`` hint.
  * :func:`mask_text` -- one string, optionally capped. ``structured=True``
    masks a string holding a JSON document as the structure it is, so a
    secret-named key inside a ``json.dumps``-ed prompt or decision is honoured.

**DEC-31 -- caps apply to the MASKED form.** ``mask_text(v, max_len=n)`` masks
first and caps second; never write ``mask_text(v[:n])`` (the slice can cut a
JWT's third segment off and hide it from the matcher).
``tools/lint_redaction_order.py`` knows these names.

**DEC-27 -- a value that could not be redacted is DROPPED and named.** If the
masker RAISES (or cannot be imported), the value becomes :data:`UNREDACTABLE`
and is never written in clear. The marker is deliberately not ``[REDACTED]``
or ``***``: a reader must be able to tell "masked" from "never looked".
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

#: DEC-27 -- what a value becomes when its redaction failed.
UNREDACTABLE = "[unredactable]"


# --------------------------------------------------------------------------- #
# internals -- every one RAISES on failure; the public functions catch
# --------------------------------------------------------------------------- #

def _jsonable(value: Any) -> Any:
    """``value`` as plain JSON types, with every other object stringified.

    A writer's ``json.dumps(..., default=str)`` would otherwise turn an object
    into text the masker never saw. Doing that conversion HERE, before the mask,
    means the string a writer persists is always the masked one."""
    return json.loads(json.dumps(value, default=str))


def _walk_strings(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, dict):
        return {k: _walk_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_strings(x, fn) for x in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def _prose(s: str) -> str:
    from systemu.messaging.gateway import mask_outbound
    return mask_outbound(s)


def _text_core(s: str) -> str:
    from systemu.runtime.external_verifier import _mask_evidence_strict
    return _prose(_mask_evidence_strict(s))


def _params_core(value: Any) -> Any:
    from systemu.runtime.external_verifier import _mask_evidence_strict
    return _walk_strings(_mask_evidence_strict(_jsonable(value)), _prose)


def _json_text_core(s: str) -> Optional[str]:
    """A string that IS a JSON object/array, masked as that structure and
    re-serialised; ``None`` when it is not one (the caller falls back to the
    text pass)."""
    head = s.lstrip()[:1]
    if head not in ("{", "["):
        return None
    try:
        parsed = json.loads(s)
    except ValueError:
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    return json.dumps(_params_core(parsed))


def _dropped(value: Any, exc: BaseException) -> Any:
    """DEC-27: the value is replaced, never passed through. A mapping keeps its
    keys (they are parameter NAMES, which resume and the live panes read) with
    every value dropped."""
    # The exception's TYPE only: a message may quote the value it choked on.
    logger.warning("[DurableRedact] redaction failed (%s) - value dropped",
                   type(exc).__name__)
    if isinstance(value, Mapping):
        try:
            return {str(k): UNREDACTABLE for k in value}
        except Exception:
            return UNREDACTABLE
    return UNREDACTABLE


def _check_cap(max_len: Optional[int]) -> None:
    if max_len is None:
        return
    if isinstance(max_len, bool) or not isinstance(max_len, int):
        raise TypeError(f"max_len must be an int or None, got {max_len!r}")
    if max_len < 0:
        raise ValueError(f"max_len must be >= 0, got {max_len}")


def _capped(masked: str, max_len: Optional[int]) -> str:
    """Cap an ALREADY-MASKED string. ``outbox._cap`` is the tree's one cap rule
    (ellipsis counted inside the cap); a plain slice of the masked form is the
    fallback -- slicing after the mask discloses nothing."""
    try:
        from systemu.runtime.outbox import _cap
    except Exception:
        return masked if max_len is None else masked[:max_len]
    return _cap(masked, max_len)


# --------------------------------------------------------------------------- #
# public
# --------------------------------------------------------------------------- #

def mask_params(params: Any) -> Any:
    """A parameter-like structure, masked: secret-named keys' values dropped,
    every string value shape- and prose-masked, the keys kept. JSON types out,
    whatever went in. Never raises; DEC-27 on failure."""
    try:
        return _params_core(params)
    except Exception as exc:
        return _dropped(params, exc)


def mask_values(value: Any) -> Any:
    """Every string inside ``value`` masked; keys untouched (no key rule). For
    records keyed by the code itself. Never raises; DEC-27 on failure."""
    try:
        return _walk_strings(_jsonable(value), _text_core)
    except Exception as exc:
        return _dropped(value, exc)


def mask_text(text: Any, *, max_len: Optional[int] = None,
              structured: bool = False) -> str:
    """``text`` (stringified if it is not a string) masked, THEN capped to
    ``max_len`` characters. ``structured=True`` masks a JSON document as a
    structure (see :func:`mask_params`). Never raises on the value; DEC-27 on
    failure. A bad ``max_len`` raises -- that is a caller bug, not a value."""
    _check_cap(max_len)
    try:
        s = text if isinstance(text, str) else str(text)
        out = _json_text_core(s) if structured else None
        if out is None:
            out = _text_core(s)
    except Exception as exc:
        out = _dropped(text, exc)
        if not isinstance(out, str):
            out = UNREDACTABLE
    return _capped(out, max_len)


def mask_display(value: Any, *, max_len: Optional[int] = None) -> str:
    """The ``str()`` a writer would have rendered for ``value``, rendered from
    the MASKED value and capped after (DEC-31: mask, then repr, then cap)."""
    _check_cap(max_len)
    return _capped(str(mask_params(value)), max_len)


def mask_event(entry: Any) -> Any:
    """An event record (``event_log.jsonl``, the execute subprocess's event
    bridge): ``details`` -- reasoning, tool parameters, tool result -- masked as
    parameters, every other field value-masked. Each field fails on its own
    (DEC-27), so one unredactable field does not take the record with it."""
    if not isinstance(entry, Mapping):
        return mask_values(entry)
    out = {}
    for k, v in entry.items():
        out[k] = mask_params(v) if k == "details" else mask_values(v)
    return out
