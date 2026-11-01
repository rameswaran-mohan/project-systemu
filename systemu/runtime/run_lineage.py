"""P28 -- the run a one-shot typed approval is bound to, and for how long.

A typed "Approve once" on an R1 (money / irreversible) MCP card used to be honoured
once by WHICHEVER lane made that exact call next -- with no time bound and no run
bound, so an identical payment much later, from an unrelated run, could cash it.
The ruling binds it to:

  (i)  THE CARD'S OWN EXPIRY. The gate stamps ``expires_at`` on the card when it
       posts it (:data:`TYPED_APPROVAL_TTL_SECONDS` after posting) and shows it on
       the card. Once that time has passed, a typed approval of the card is void.

  (ii) THE RUN LINEAGE. The gate stamps the id of the run that raised the card
       (``run_id``: the per-run id the run carriers hold -- ``ShadowRuntime``'s
       execution id, the quick lane's run id -- never a model-supplied value) and
       that run's run-tree root. The approval is honoured only by:

         * the run that raised the card (the chat lane's re-call after the
           operator approves; a workflow run re-making the call);
         * its direct RESUME -- a run started with ``resume_from_execution_id``
           naming it (the resume rail's re-dispatch), or one in the same run tree
           (the resume restored ``root_execution_id`` from the snapshot; a
           sub-agent of it);
         * its direct RE-RUN -- the NEXT run of the same task after it stopped at a
           card. A fresh re-run carries no lineage id of its own, so when a run
           stops at a card (``PendingOperatorDecision`` unwinds
           ``ShadowRuntime.execute``) it leaves a single-use HANDOFF naming itself
           for its activity, and the next run of that activity claims it at start
           and carries the stopped run's id forward (:func:`record_stopped_run` /
           :func:`claim_stopped_run`). Claimed once; a later run of the same task
           carries nothing.

       Anything else is refused with "This approval was for <run>; ask again."

Every value read off a card is data off disk: types are pinned with ``type(x) is``
before any comparison (DEC-36), and an absent or unreadable coordinate matches
NOTHING -- there is no "both empty, therefore equal".
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: How long a typed "Approve once" of an R1 MCP card stays valid, counted from when
#: the card is posted. The same bound as an IMPL-2 reclassification
#: (``command_approvals.RECLASSIFY_TTL_SECONDS``): long enough for an operator to
#: read the card, type the phrase and re-run the task; short enough that a stale
#: approval cannot wait around for a much later identical call.
TYPED_APPROVAL_TTL_SECONDS = 30 * 60

#: The decision-context key the MCP gate stamps the binding under.
ONE_SHOT_BINDING_KEY = "one_shot_binding"

#: At most this many run ids ride a handoff (a re-run of a re-run that stopped at
#: the same card each time); older links fall off.
MAX_CHAIN = 8

#: Handoff records kept (one per activity; the newest win).
STOPPED_RUNS_RETENTION = 500

_STOPPED_RUNS_FILE = "stopped_runs.json"
_STOPPED_RUNS_LOCK = threading.Lock()


def utcnow() -> datetime:
    """The clock every expiry check reads (a module attribute, so a test can move
    it)."""
    return datetime.now(tz=timezone.utc)


def _str(value: Any) -> str:
    """``value`` when it is exactly a non-blank ``str``, else ``""`` (DEC-36)."""
    return value.strip() if type(value) is str and value.strip() else ""


def _ascii(text: str) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


# ── the current run's lineage (set by ShadowRuntime.execute) ─────────────────

@dataclass(frozen=True)
class RunLineage:
    """What the run in progress descends from. Empty for a run with no lineage
    (the quick lane: its re-call IS the same run id)."""

    root_execution_id: str = ""
    resume_from_execution_id: str = ""
    continues: Tuple[str, ...] = ()     # the stopped run(s) this run directly re-runs

    def with_root(self, root: Any) -> "RunLineage":
        return replace(self, root_execution_id=_str(root))


_EMPTY = RunLineage()
_lineage_var: "contextvars.ContextVar[Optional[RunLineage]]" = contextvars.ContextVar(
    "systemu_run_lineage", default=None)


def set_run_lineage(lineage: Optional[RunLineage]):
    """Set the run's lineage; returns the token :func:`reset_run_lineage` takes."""
    return _lineage_var.set(lineage if type(lineage) is RunLineage else None)


def reset_run_lineage(token) -> None:
    """Restore the lineage from before :func:`set_run_lineage` made ``token``. A
    ``None`` token (never set) changes nothing; a token that cannot be used clears
    the lineage (fail-closed: no lineage honours no other run's approval)."""
    if token is None:
        return
    try:
        _lineage_var.reset(token)
    except (ValueError, RuntimeError):
        _lineage_var.set(None)


def current_run_lineage() -> RunLineage:
    got = _lineage_var.get()
    return got if type(got) is RunLineage else _EMPTY


# ── the stopped-run handoff (a fresh re-run's lineage) ───────────────────────

def _stopped_runs_path(vault: Any) -> Optional[Path]:
    root = getattr(vault, "root", None)
    if type(root) not in (str,) and not isinstance(root, os.PathLike):
        return None
    try:
        return Path(root) / _STOPPED_RUNS_FILE
    except TypeError:
        return None


def _read_stopped(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[RunLineage] %s is unreadable - treating it as empty", path,
                       exc_info=True)
        return {}
    runs = data.get("activities") if type(data) is dict else None
    if type(runs) is not dict:
        return {}
    return {k: v for k, v in runs.items() if type(k) is str and type(v) is dict}


def _write_stopped(path: Path, runs: Dict[str, Dict[str, Any]]) -> None:
    if len(runs) > STOPPED_RUNS_RETENTION:
        keep = sorted(runs.items(), key=lambda kv: _str(kv[1].get("at")))
        runs = dict(keep[-STOPPED_RUNS_RETENTION:])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "activities": runs}, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def record_stopped_run(vault: Any, *, activity_id: Any, execution_id: Any,
                       lineage: Optional[RunLineage] = None) -> bool:
    """A run of ``activity_id`` stopped at an operator card: leave the single-use
    handoff the NEXT run of that activity claims. It names this run and the runs
    this one itself directly continued (a re-run of a re-run, at most
    :data:`MAX_CHAIN`). Best-effort: never raises; False when nothing was written."""
    act, eid = _str(activity_id), _str(execution_id)
    if not act or not eid:
        return False
    lin = lineage if type(lineage) is RunLineage else _EMPTY
    chain = [eid]
    for rid in (lin.resume_from_execution_id, *lin.continues):
        rid = _str(rid)
        if rid and rid not in chain:
            chain.append(rid)
    path = _stopped_runs_path(vault)
    if path is None:
        return False
    try:
        with _STOPPED_RUNS_LOCK:
            runs = _read_stopped(path)
            runs[act] = {"at": utcnow().isoformat(), "runs": chain[:MAX_CHAIN]}
            _write_stopped(path, runs)
        return True
    except Exception:
        logger.warning("[RunLineage] could not record the stopped run", exc_info=True)
        return False


def claim_stopped_run(vault: Any, activity_id: Any) -> Tuple[str, ...]:
    """The run ids the run now starting for ``activity_id`` directly re-runs, taken
    ONCE (the record is removed whether or not it is still fresh). ``()`` when there
    is none, it is older than :data:`TYPED_APPROVAL_TTL_SECONDS` (nothing it could
    unlock is still valid), or it cannot be read. Never raises."""
    act = _str(activity_id)
    path = _stopped_runs_path(vault) if act else None
    if path is None:
        return ()
    try:
        with _STOPPED_RUNS_LOCK:
            runs = _read_stopped(path)
            entry = runs.pop(act, None)
            if entry is None:
                return ()
            _write_stopped(path, runs)
    except Exception:
        logger.warning("[RunLineage] could not claim the stopped run", exc_info=True)
        return ()
    at = _parse_utc(entry.get("at"))
    if at is None or (utcnow() - at).total_seconds() > TYPED_APPROVAL_TTL_SECONDS:
        return ()
    chain = entry.get("runs")
    if type(chain) is not list:
        return ()
    return tuple(r for r in (_str(x) for x in chain[:MAX_CHAIN]) if r)


# ── the binding stamped on a card, and the one check that reads it ────────────

def _parse_utc(value: Any) -> Optional[datetime]:
    text = _str(value)
    if not text:
        return None
    try:
        got = datetime.fromisoformat(text)
    except ValueError:
        return None
    if got.tzinfo is None:
        return None
    return got.astimezone(timezone.utc)


def _when(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def card_binding(run_id: Any, *, now: Optional[datetime] = None) -> Dict[str, str]:
    """The binding the gate stamps on an R1 card it posts: the run that raised it,
    that run's run-tree root, and the card's expiry."""
    at = now if type(now) is datetime else utcnow()
    return {
        "run_id": _str(run_id),
        "root_execution_id": current_run_lineage().root_execution_id,
        "expires_at": (at + timedelta(seconds=TYPED_APPROVAL_TTL_SECONDS)).isoformat(),
    }


def binding_of(ctx: Any) -> Dict[str, str]:
    """The binding on a card context, type-pinned (``{}`` when absent)."""
    raw = ctx.get(ONE_SHOT_BINDING_KEY) if type(ctx) is dict else None
    if type(raw) is not dict:
        return {}
    return {k: _str(raw.get(k)) for k in ("run_id", "root_execution_id", "expires_at")}


def _run_label(binding: Dict[str, str]) -> str:
    rid = binding.get("run_id") or ""
    return _ascii("run " + rid) if rid else "an unrecorded run"


def valid_for_sentence(binding: Dict[str, str]) -> str:
    """What the card says about whom its typed approval serves, and until when."""
    at = _parse_utc(binding.get("expires_at"))
    until = (" until " + _when(at)) if at is not None else ""
    return _ascii("A typed approval of this card is for {} (or its direct re-run or "
                  "resume){}; any other run asks again.".format(_run_label(binding),
                                                                until))


def binding_refusal(ctx: Any, *, run_id: Any, lineage: Optional[RunLineage] = None,
                    now: Optional[datetime] = None) -> str:
    """``""`` when a typed approval on the card ``ctx`` may serve the call made by
    run ``run_id`` (whose lineage is ``lineage``) at ``now``; otherwise the refusal
    the operator reads. Fail-closed on every unreadable value."""
    b = binding_of(ctx)
    label = _run_label(b)
    origin = b.get("run_id") or ""
    if not origin:
        return "This approval was for {}; ask again.".format(label)
    expires = _parse_utc(b.get("expires_at"))
    at = now if type(now) is datetime else utcnow()
    if expires is None:
        return ("This approval was for {} and its expiry cannot be read; "
                "ask again.".format(label))
    if at > expires:
        return ("This approval was for {} and expired at {}; ask again.".format(
            label, _when(expires)))
    lin = lineage if type(lineage) is RunLineage else _EMPTY
    mine = {r for r in (_str(run_id), lin.resume_from_execution_id, *lin.continues)
            if type(r) is str and r}
    if origin in mine:
        return ""
    root = b.get("root_execution_id") or ""
    if root and lin.root_execution_id and root == lin.root_execution_id:
        return ""
    return "This approval was for {}; ask again.".format(label)
