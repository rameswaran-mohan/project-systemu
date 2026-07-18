"""Operator-managed allow-list for destructive shell commands (v0.9.32, D-2).

Mirrors DepApprovalStore (systemu/runtime/dep_approvals.py): a small JSON file
the operator can inspect/hand-edit, re-read on every check so an out-of-process
"Always allow" is seen without a daemon restart. Keyed on an EXACT normalized
command signature = sha1(whitespace-collapsed command + "\x00" + cwd).

In a per-command approval gate the operator confirms a harmful shell command
before it runs.  "Always allow" persists that exact (command, cwd) so future
identical runs skip the gate.  Persisted here so it survives daemon restarts and
is operator-readable/editable as plain JSON.

File schema (versioned for forward-compat):

    {
        "version": 1,
        "approved": {
            "<sha1-sig>": {
                "approved_at": "2026-06-15T12:34:56+00:00",
                "approved_by": "operator",
                "command":     "rm -rf build",
                "cwd":         "/proj"
            },
            ...
        },
        "pending": {
            "<sha1-sig>": {
                "first_seen_at": "2026-06-15T12:35:01+00:00",
                "command":       "rm -rf build",
                "cwd":           "/proj",
                "request_count": 3
            },
            ...
        },
        "reclassified": {
            "<sha1-tool-sig>": {
                "effect_class": "local_write",
                "recorded_at":  "2026-07-18T09:12:00+00:00"
            },
            ...
        }
    }

Concurrency: a single ``threading.Lock`` serialises mutations.  The JSON file is
small (operator-scale) and writes are infrequent, so this is dramatically
simpler than file locking — same trade-off DepApprovalStore makes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# effect_tags is foundational and imports nothing from systemu at module scope
# (its only intra-package import is lazy, inside classify_source), so this is
# cycle-free. IMPL-2 validates every reclassification through it.
from systemu.runtime.effect_tags import EffectTag, coerce as _coerce_effect_class

logger = logging.getLogger(__name__)

#: IMPL-2 — how long an operator effect-class assignment stays redeemable.
#:
#: It has to outlive the round trip it exists for: the operator reclassifies, the run
#: resumes and posts a follow-up card, and the record must still be there when they
#: approve THAT card. So it cannot be seconds. But it must not be open-ended either —
#: an ABANDONED follow-up card would otherwise leave an assignment that lives forever
#: and is spent by whatever call reaches that signature next. Thirty minutes is the
#: compromise: long enough for a real operator to come back to a card, short enough
#: that the dangling window is bounded. Expiry is fail-closed — an expired record
#: reads as absent, so the call simply re-DENYs and the operator re-assigns.
RECLASSIFY_TTL_SECONDS = 30 * 60


# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


_DEFAULT_FILENAME = "command_approvals.json"


def command_signature(command: str, *, cwd: str = "") -> str:
    """Exact normalized signature: sha1 of whitespace-collapsed command + cwd.

    Whitespace runs collapse to a single space and ends are stripped so
    cosmetic reformatting does not invalidate an "Always allow" (``ls  -la`` ==
    ``ls -la``). cwd is part of the key — the same command in a different
    directory is a NEW decision. This is EXACT-match, not fuzzy: a different
    command never collides.
    """
    norm_cmd = " ".join((command or "").split())
    norm_cwd = (cwd or "").strip()
    raw = f"{norm_cmd}\x00{norm_cwd}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def mcp_signature(server: str, tool: str) -> str:
    """Always-allow signature for an MCP (server, tool) pair.

    The server is whitespace-stripped and trailing-slash-normalized (matching
    connections._path / is_tool_enabled normalization) so a cosmetic trailing
    "/" does not invalidate a persisted Always-allow. EXACT-match: a different
    server or tool never collides.
    """
    norm_server = (server or "").strip().rstrip("/")
    norm_tool = (tool or "").strip()
    raw = f"mcp\x00{norm_server}\x00{norm_tool}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def mcp_session_key(server: str, tool: str, session_id: str) -> str:
    """Session-scoped trust key for an MCP (server, tool) within ONE run.

    session_id is part of the hash, so a trust grant CANNOT leak across runs:
    a new run has a new session_id → a fresh, untrusted key.
    """
    norm_server = (server or "").strip().rstrip("/")
    norm_tool = (tool or "").strip()
    norm_sess = (session_id or "").strip()
    raw = f"mcp-session\x00{norm_server}\x00{norm_tool}\x00{norm_sess}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def tool_signature(name: str, body_hash: str, effect_tags, *, host_class: str = "") -> str:
    """IMPL-1 approval key for a forged/registry tool (S1b live action gate).

    Order-insensitive over effect_tags (sorted before hashing); a changed
    body_hash, tag set, or host_class invalidates the blessing. host_class
    defaults to "" so non-network tools get a stable 3-tuple signature.
    Disjoint from mcp_signature via the "tool" domain prefix. body_hash and
    host_class are computed elsewhere and passed in as strings — this
    function does no filesystem/host work.
    """
    norm_name = (name or "").strip()
    tags = "\x00".join(sorted(str(t).strip() for t in (effect_tags or [])))
    raw = f"tool\x00{norm_name}\x00{body_hash}\x00{tags}\x00{host_class}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


class CommandApprovalStore:
    """Persistent allow-list of operator-approved command signatures.

    Args:
        path: Path to the JSON file.  Parent directories are created on
              first write.  When the file does not exist (or is unreadable)
              the store starts empty — never raises in the constructor so
              the daemon can boot in a fresh checkout.
    """

    def __init__(self, path: Path):
        self.path: Path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def is_approved(self, sig: str) -> bool:
        # Re-read on every check so out-of-process mutations (the dashboard /
        # CLI run in a separate Python interpreter from the daemon) are picked
        # up WITHOUT a daemon restart. Cost: one tiny JSON read per check —
        # cheaper than file-watching plumbing. (Mirrors DepApprovalStore.)
        with self._lock:
            self._data = self._load()
            return sig in self._data.get("approved", {})

    def is_session_trusted(self, session_key: str) -> bool:
        """Return True iff this exact session-trust key is on record.

        Re-reads the file on every check (same out-of-process freshness
        contract as is_approved) so a dashboard-side "Trust for session"
        click is seen by the daemon without a restart.
        """
        with self._lock:
            self._data = self._load()
            return session_key in self._data.get("session_trusted", {})

    def trust_session(
        self,
        session_key: str,
        *,
        server: str = "",
        tool: str = "",
        session_id: str = "",
        approved_by: str = "operator",
    ) -> bool:
        """Record session-scoped trust for an MCP (server, tool, session).

        Returns True when newly trusted. Idempotent: re-trusting an existing
        key returns False and leaves the record untouched.
        """
        newly = False
        with self._lock:
            self._data = self._load()
            trusted: Dict[str, Any] = self._data.setdefault("session_trusted", {})
            if session_key not in trusted:
                trusted[session_key] = {
                    "trusted_at": _now_iso(),
                    "trusted_by": approved_by,
                    "server":     server,
                    "tool":       tool,
                    "session_id": session_id,
                }
                self._save()
                newly = True
        if newly:
            logger.info("[CommandApprovals] session-trusted %s (%s:%s, run %s)",
                        session_key, server, tool, session_id)
        return newly

    def mark_resume_approved(self, sig: str, *,
                             for_reclassification: Optional[str] = None) -> None:
        """v0.9.52: record a SINGLE-USE approval for a command signature.

        Set by the command-gate RESUME path so the resumed run honors the
        operator's "Approve once" exactly once, across the park→resume boundary.
        Unlike :meth:`approve` this is NOT a standing allow-list entry — it's a
        one-shot bridge, consumed on the first :meth:`consume_resume_approved`.

        IMPL-2 ``for_reclassification``: the effect class of the card that minted
        this bridge, when it was an operator-RECLASSIFIED follow-up card. The bridge
        is the operator's decision about ONE specific card; stamping which card makes
        it identifiable, so it cannot be redeemed against a call scored under a
        different classification (see :meth:`consume_resume_approved`). ``None`` for
        an ordinary gate — the historical, unscoped bridge.
        """
        with self._lock:
            self._data = self._load()
            pend: Dict[str, Any] = self._data.setdefault("resume_pending", {})
            entry: Dict[str, Any] = {"marked_at": _now_iso()}
            if for_reclassification:
                entry["for_reclassification"] = str(for_reclassification)
            pend[sig] = entry
            self._save()

    def consume_resume_approved(self, sig: str, *,
                                for_reclassification: Optional[str] = None) -> bool:
        """Return True (and REMOVE the entry) iff a one-shot resume-approval is on
        record for ``sig`` AND its reclassification scope matches. Single-use — a
        second check returns False.

        IMPL-2 SCOPE MATCH. ``for_reclassification`` is the class the CURRENT call is
        being scored under (``None`` for an ordinary call). The bridge is honoured
        only when the record's scope is exactly that. Both directions matter and both
        are pinned:

        * an UNSCOPED bridge (or one scoped to another class) does not satisfy a call
          under a pending reclassification — it is not the operator's decision about
          the classification they just assigned. This is what stopped the stale
          "Approve once" from a DENY card being cashed the instant IMPL-2 lifted the
          verdict: the DENY card's bridge (which is no longer minted at all — see
          ``_record_gate_approval`` — but could survive from an older build or another
          surface) carries no scope, so it cannot redeem a reclassified call;
        * a SCOPED bridge does not satisfy an ordinary call either. It was granted for
          a call scored under a class the operator assigned, not for this one.

        A mismatch LEAVES the record in place rather than silently revoking it: it is
        still a legitimate one-shot for its own card. It is cleared explicitly when a
        reclassification is recorded, and by the DENY branch of the resume dispatcher.
        """
        want = for_reclassification or None
        with self._lock:
            self._data = self._load()
            pend = self._data.get("resume_pending") or {}
            if sig not in pend:
                return False
            entry = pend[sig]
            scope = (entry.get("for_reclassification") or None
                     if isinstance(entry, dict) else None)
            if scope != want:
                logger.warning(
                    "[CommandApprovals] refusing resume-bridge for %s: minted for "
                    "%s, call is scored under %s — the gate re-asks",
                    sig, scope or "an ordinary gate",
                    want or "an ordinary gate")
                return False
            del pend[sig]
            self._save()
            return True

    def clear_resume_approved(self, sig: str) -> bool:
        """Drop any one-shot resume bridge for ``sig``, WHATEVER its scope. Returns
        True when one was present.

        Distinct from :meth:`consume_resume_approved`, which is a scoped redemption
        and refuses on a mismatch. This is the unconditional sweep used when the
        gate's arbitration is about to change out from under an outstanding bridge
        (an operator reclassification): no bridge minted before that change can be a
        decision about the call the change produces.
        """
        if not sig:
            return False
        with self._lock:
            self._data = self._load()
            pend = self._data.get("resume_pending") or {}
            if sig not in pend:
                return False
            del pend[sig]
            self._save()
        logger.info("[CommandApprovals] cleared resume bridge %s", sig)
        return True

    # ── IMPL-2: operator effect-class reclassification (SINGLE-USE) ───────────
    #
    # A DENY verdict is the gate's refusal band (unclassifiable effect ∩ a
    # high-severity signal). IMPL-2 makes it operator-REMEDIABLE rather than a dead
    # end: under typed confirmation the operator assigns the real effect class, and
    # the gate re-arbitrates on it (action_governance.ActionContext
    # .operator_assigned_class). These records are where that assignment lives
    # between the operator's click and the re-run that re-scores the call.
    #
    # SINGLE-USE, deliberately. ``tool_signature`` is params-INDEPENDENT (name +
    # body hash + effect tags + host class) while the DENY verdict is
    # params-DEPENDENT (is_destructive_param), so a STANDING reclassification would
    # silently re-arbitrate every future destructive call to the same tool body —
    # the very shape of the hole the DENY-band consumption fix closed. One
    # reclassification buys exactly one re-scored call; the next identical call
    # DENYs again.

    @staticmethod
    def _valid_effect_class(value) -> Optional[str]:
        """The normalized effect class, or None when it classifies NOTHING.

        ``coerce`` maps anything unrecognized to ``unknown`` — and ``unknown`` is
        precisely the conjunct the DENY band keys on. Storing (or reading back) it
        would be a "reclassification" that strips UNKNOWN and puts nothing in its
        place, so it is refused on BOTH the write and the read side: this file is
        documented as operator-inspectable and hand-editable, so validating only on
        write would leave the door open.
        """
        raw = str(value or "").strip()
        if not raw:
            return None
        cls = _coerce_effect_class(raw)
        return None if cls == EffectTag.UNKNOWN.value else cls

    @staticmethod
    def _reclassification_applies(entry: Any, args_fingerprint: str) -> Optional[str]:
        """The effect class this record assigns to THIS call, or None.

        Three independent conditions, all fail-closed:

        1. the class must still coerce to a real tag (the file is hand-editable);
        2. the record's args fingerprint must EXACTLY match the call's. The signature
           is params-INDEPENDENT while the DENY verdict is params-DEPENDENT, so
           without this an assignment made for ``{"path": "/data/report"}`` would
           re-arbitrate ``{"path": "/etc/production_secrets"}`` — and the card the
           gate then posts is byte-identical, because ``GateDescriptor.from_tool``
           never receives parameters. An ABSENT fingerprint on either side matches
           NOTHING: there is deliberately no "both empty, therefore equal" case;
        3. the record must be inside :data:`RECLASSIFY_TTL_SECONDS`. An unparseable
           or absent timestamp reads as expired — if the store cannot tell how old
           an assignment is, it does not get to act on it.
        """
        if not isinstance(entry, dict):
            return None
        cls = CommandApprovalStore._valid_effect_class(entry.get("effect_class"))
        if cls is None:
            return None
        stored_fp = str(entry.get("args_fingerprint") or "")
        want_fp = str(args_fingerprint or "")
        if not stored_fp or not want_fp or stored_fp != want_fp:
            return None
        raw_ts = entry.get("recorded_at")
        try:
            recorded = datetime.fromisoformat(str(raw_ts))
        except Exception:
            return None
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - recorded).total_seconds()
        if age < 0 or age > RECLASSIFY_TTL_SECONDS:
            return None
        return cls

    def mark_reclassified(self, sig: str, effect_class: str, *,
                          args_fingerprint: str = "") -> bool:
        """Record a SINGLE-USE operator effect-class assignment for a tool signature,
        scoped to the exact call (``args_fingerprint``) the operator was looking at.

        Returns True when a record was written. A garbage / ``unknown`` / empty class
        records NOTHING and returns False (an existing record is left untouched) —
        the caller has assigned no classification, so the gate must keep refusing.

        An EMPTY ``args_fingerprint`` likewise records nothing. Such a record could
        never be applied (see :meth:`_reclassification_applies`), and a permanently
        inert row in an operator-inspectable security store reads as an authorization
        that exists when it does not.
        """
        cls = self._valid_effect_class(effect_class)
        fp = str(args_fingerprint or "").strip()
        if not sig or cls is None or not fp:
            logger.warning(
                "[CommandApprovals] refusing reclassification for %r: %s — recording "
                "nothing",
                sig,
                ("no tool signature" if not sig else
                 "no args fingerprint (the record could never be applied)" if not fp
                 else f"{effect_class!r} does not coerce to a real effect class"),
            )
            return False
        with self._lock:
            self._data = self._load()
            rec: Dict[str, Any] = self._data.setdefault("reclassified", {})
            rec[sig] = {"effect_class": cls, "recorded_at": _now_iso(),
                        "args_fingerprint": fp}
            self._save()
        logger.info("[CommandApprovals] reclassified %s as %s (single-use, "
                    "args %s…, TTL %ss)", sig, cls, fp[:8], RECLASSIFY_TTL_SECONDS)
        return True

    def peek_reclassified(self, sig: str, *,
                          args_fingerprint: str = "") -> Optional[str]:
        """The pending effect class for ``sig`` AND this call, WITHOUT consuming it —
        or None.

        Read by the gate on every call so the operator's assignment is scored;
        non-consuming because the gate peeks once to score and the bypass path
        consumes only when the call actually runs.
        """
        if not sig:
            return None
        with self._lock:
            self._data = self._load()
            entry = (self._data.get("reclassified") or {}).get(sig)
        return self._reclassification_applies(entry, args_fingerprint)

    def consume_reclassified(self, sig: str, *,
                             args_fingerprint: str = "") -> Optional[str]:
        """Return the pending effect class and REMOVE the record. Single-use — a
        second call returns None.

        A record that does not apply to THIS call (wrong params, expired, or a class
        that no longer validates) reports None. It is removed only when it could
        never apply to any call again — i.e. when it is EXPIRED or malformed. A
        params MISMATCH leaves it alone: it is still the operator's live assignment
        for a different call, and spending it here would let any unrelated call
        silently revoke it.
        """
        if not sig:
            return None
        cls = None
        with self._lock:
            self._data = self._load()
            rec = self._data.get("reclassified") or {}
            entry = rec.get(sig)
            if entry is None:
                return None
            cls = self._reclassification_applies(entry, args_fingerprint)
            if cls is not None:
                del rec[sig]
                self._save()
            elif self._is_stale_record(entry):
                del rec[sig]
                self._save()
                logger.info("[CommandApprovals] purged stale reclassification %s", sig)
                return None
            else:
                return None
        logger.info("[CommandApprovals] consumed reclassification %s (%s)", sig, cls)
        return cls

    @staticmethod
    def _is_stale_record(entry: Any) -> bool:
        """True when this record can never apply to ANY call — expired, malformed, or
        classifying nothing — so it is safe (and tidy) to drop. A record that is
        merely scoped to DIFFERENT parameters is NOT stale."""
        if not isinstance(entry, dict):
            return True
        if CommandApprovalStore._valid_effect_class(entry.get("effect_class")) is None:
            return True
        if not str(entry.get("args_fingerprint") or ""):
            return True
        try:
            recorded = datetime.fromisoformat(str(entry.get("recorded_at")))
        except Exception:
            return True
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - recorded).total_seconds()
        return age < 0 or age > RECLASSIFY_TTL_SECONDS

    def clear_reclassified(self, sig: str) -> bool:
        """Drop any pending reclassification for ``sig``. Returns True when one was
        present. Used when the operator DENIES the follow-up card, so a refused
        remedy leaves no dangling record behind."""
        if not sig:
            return False
        cleared = False
        with self._lock:
            self._data = self._load()
            rec = self._data.get("reclassified") or {}
            if sig in rec:
                del rec[sig]
                self._save()
                cleared = True
        if cleared:
            logger.info("[CommandApprovals] cleared reclassification %s", sig)
        return cleared

    def approve(
        self,
        sig: str,
        *,
        command: str = "",
        cwd: str = "",
        approved_by: str = "operator",
    ) -> bool:
        """Record an "Always allow" for an exact command signature.

        Returns True when newly approved.  Idempotent: approving an
        already-approved signature returns False and leaves the existing
        record (and its timestamp) untouched.
        """
        newly = False
        with self._lock:
            # Re-read so we don't clobber a parallel writer.
            self._data = self._load()
            approved: Dict[str, Any] = self._data.setdefault("approved", {})
            pending: Dict[str, Any] = self._data.setdefault("pending", {})
            if sig not in approved:
                existing_pending = pending.pop(sig, None) or {}
                approved[sig] = {
                    "approved_at": _now_iso(),
                    "approved_by": approved_by,
                    "command":     command or existing_pending.get("command", ""),
                    "cwd":         cwd or existing_pending.get("cwd", ""),
                }
                self._save()
                newly = True
        if newly:
            logger.info("[CommandApprovals] approved %s (cmd=%r, by %s)",
                        sig, command, approved_by)
        return newly

    def revoke(self, sig: str) -> bool:
        """Remove a signature from the allow-list.  Returns True when present."""
        revoked = False
        with self._lock:
            self._data = self._load()
            approved: Dict[str, Any] = self._data.setdefault("approved", {})
            if sig in approved:
                del approved[sig]
                self._save()
                revoked = True
        if revoked:
            logger.info("[CommandApprovals] revoked %s", sig)
        return revoked

    def record_pending(
        self,
        sig: str,
        *,
        command: str = "",
        cwd: str = "",
    ) -> None:
        """Note that a command was requested but is not yet approved.

        Increments ``request_count`` so the operator can prioritise
        frequently-requested commands.  No-op when the signature is already
        approved.  (Mirrors DepApprovalStore.record_pending.)
        """
        with self._lock:
            if sig in self._data.get("approved", {}):
                return
            pending: Dict[str, Any] = self._data.setdefault("pending", {})
            entry = pending.get(sig)
            if entry is None:
                pending[sig] = {
                    "first_seen_at": _now_iso(),
                    "command":       command,
                    "cwd":           cwd,
                    "request_count": 1,
                }
            else:
                entry["request_count"] = int(entry.get("request_count", 0)) + 1
                if not entry.get("command") and command:
                    entry["command"] = command
                if not entry.get("cwd") and cwd:
                    entry["cwd"] = cwd
            self._save()

    def list_approved(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"signature": s, **dict(meta)}
                for s, meta in sorted(self._data.get("approved", {}).items())
            ]

    def list_pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"signature": s, **dict(meta)}
                for s, meta in sorted(
                    self._data.get("pending", {}).items(),
                    key=lambda kv: -int(kv[1].get("request_count", 0)),
                )
            ]

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _empty() -> Dict[str, Any]:
        """A fresh, fully-keyed store. One definition so a new section (IMPL-2's
        ``reclassified``) can never be present on one empty-path and absent on
        another — a section missing here would KeyError-degrade to "no record",
        which for a security store is a silent behaviour change."""
        return {"version": 1, "approved": {}, "pending": {},
                "session_trusted": {}, "reclassified": {}}

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return self._empty()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("approval file is not a JSON object")
            for key, default in self._empty().items():
                data.setdefault(key, default)
            return data
        except Exception:
            logger.exception(
                "[CommandApprovals] Could not parse %s — starting with empty store. "
                "The bad file is left in place for operator inspection.",
                self.path,
            )
            return self._empty()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[CommandApprovals] Failed to persist store at %s",
                             self.path)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton helpers — convenient for runtime code that doesn't
# want to thread the store through every call site. (Mirrors dep_approvals.)

_singleton: Optional[CommandApprovalStore] = None
_singleton_lock = threading.Lock()


def init_default_store(data_dir: Path) -> CommandApprovalStore:
    """Initialise the process-wide singleton at ``<data_dir>/command_approvals.json``.

    Idempotent: calling twice with the same ``data_dir`` returns the existing
    instance.  Calling with a different ``data_dir`` rebuilds (used in tests).
    """
    global _singleton
    target = Path(data_dir) / _DEFAULT_FILENAME
    with _singleton_lock:
        if _singleton is None or _singleton.path != target:
            _singleton = CommandApprovalStore(target)
        return _singleton


def get_default_store() -> Optional[CommandApprovalStore]:
    """Return the process-wide singleton, or None if not initialised yet."""
    with _singleton_lock:
        return _singleton


def reset_default_store_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
