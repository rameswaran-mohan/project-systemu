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
import hmac
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

def _emit_refusal_event(sig: str, *, minted_for, scored_under) -> None:
    """D3: surface a SCOPE-refused resume bridge to the operator.

    Only a real mismatch speaks; "no bridge on record" is the ordinary state of
    every un-approved call and stays quiet. ASCII only. The signature is a sha1
    digest, never operator input, but its concrete type is still pinned in this
    frame before it is formatted (DEC-36) so a stored non-string cannot reach the
    message. Lazy import + best-effort: the approval store must never depend on,
    or be broken by, the notification layer.
    """
    try:
        _sig = sig if type(sig) is str else ""
        _minted = minted_for if type(minted_for) is str else ""
        _under = scored_under if type(scored_under) is str else ""
        from systemu.interface.notifications import log_event
        log_event(
            "WARNING", "tool",
            "Your resume approval did not cover this call, so the gate will "
            "re-ask: it was approved for {}, and this call is scored as {}.".format(
                _minted or "an ordinary gate", _under or "an ordinary gate"),
            {"tool_signature": _sig[:16], "minted_for": _minted,
             "scored_under": _under},
        )
    except Exception:
        logger.debug("[CommandApprovals] could not write bridge-refusal event",
                     exc_info=True)


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


#: The host class of a tool that declares no network effect.
HOST_CLASS_NONE = "none"
#: The host class of a tool that declares a network effect but no hosts.
HOST_CLASS_UNDECLARED = "undeclared"
#: Where a tool spec declares the hosts it may reach. No tool field for hosts
#: existed when the host class was ruled, so these two keys are read from the
#: spec object (attribute) or mapping (key), and their union is the declaration.
#: The vault ``Tool`` model carries neither field today, so every networked vault
#: tool derives ``undeclared`` until it does.
HOST_KEYS = ("hosts", "allowed_hosts")


def _spec_value(spec, key):
    if type(spec) is dict:
        return spec.get(key)
    return getattr(spec, key, None)


def _norm_hostname(raw) -> str:
    """One declared host, canonicalised; "" when it is not a hostname at all."""
    if type(raw) is not str:
        return ""
    h = raw.strip()
    if "://" in h:
        try:
            from urllib.parse import urlsplit
            h = urlsplit(h).hostname or ""
        except Exception:
            return ""
    h = h.strip().lower().rstrip(".")
    if not h or any(c.isspace() for c in h) or "," in h or "/" in h:
        return ""
    if ":" in h and h.count(":") == 1:          # host:port -> host
        h = h.split(":", 1)[0]
    return h


def declared_hosts(spec) -> tuple:
    """The sorted, unique hostnames ``spec`` declares under :data:`HOST_KEYS`.
    Values that are not hostnames are dropped -- which can only push a tool
    toward ``undeclared``, the stricter class, never away from a gate."""
    hosts = set()
    for key in HOST_KEYS:
        raw = _spec_value(spec, key)
        if type(raw) is str:
            raw = [raw]
        if type(raw) not in (list, tuple, set, frozenset):
            continue
        for item in raw:
            norm = _norm_hostname(item)
            if norm:
                hosts.add(norm)
    return tuple(sorted(hosts))


def tool_host_class(tool) -> str:
    """The host class a tool's standing-approval signature binds -- the ONE place
    it is decided, read by the live gate (``tool_sandbox._maybe_gate_tool``), the
    bulk first-gate review and the capability diff, so none of them can key a tool
    on a different value.

    Deterministic, from the tool's DECLARED spec (never from a call's arguments):
    ``none`` when its declared effect tags carry no network effect
    (``action_governance.NET_EFFECTS``); the comma-joined sorted unique declared
    hostnames when they do and it declares hosts; ``undeclared`` when it reaches
    the network and names no host.
    """
    tags = _spec_value(tool, "effect_tags") or ()
    if type(tags) not in (list, tuple, set, frozenset):
        tags = ()
    from systemu.runtime.action_governance import has_network_egress
    if not has_network_egress([t for t in tags if type(t) is str]):
        return HOST_CLASS_NONE
    hosts = declared_hosts(tool)
    return ",".join(hosts) if hosts else HOST_CLASS_UNDECLARED


def signature_host_class(host_class: str) -> str:
    """How a host class is written INTO the signature.

    ``none`` is written as the empty string -- the value every signature carried
    before host classes existed. That is the upgrade ruling made structural: an
    approval minted before host checks stays valid exactly for a tool whose
    derived class is ``none``, and for no other. Every other class is non-empty,
    so the encoding stays one-to-one.
    """
    return "" if host_class == HOST_CLASS_NONE else host_class


def signature_from_parts(parts) -> Optional[str]:
    """The ``tool_signature`` a recorded set of signature parts produces, or None
    when the parts are not well-formed.

    ``parts`` is read back from a decision record in the vault, so every value is
    type-pinned in THIS frame before anything operates on it (DEC-36): a
    non-string name / body hash / host class, or a tag list holding anything but
    strings, is not a set of parts at all. An absent ``host_class`` reads as the
    empty class -- the only value any signature was keyed on before parts were
    recorded.
    """
    if type(parts) is not dict:
        return None
    name = parts.get("name")
    body_hash = parts.get("body_hash")
    tags = parts.get("effect_tags")
    host_class = parts.get("host_class", "")
    if type(name) is not str or type(body_hash) is not str or type(host_class) is not str:
        return None
    if type(tags) is not list and type(tags) is not tuple:
        return None
    for t in tags:
        if type(t) is not str:
            return None
    return tool_signature(name, body_hash, set(tags),
                          host_class=signature_host_class(host_class))


def parts_reproduce_signature(sig, parts) -> bool:
    """True only when ``parts`` recompute to exactly ``sig``.

    The verifier may not consult an input the verified party controls (DEC-34):
    the parts live in an operator-editable decision record, so they are believed
    only when they hash to the approved signature, compared in constant time after
    both sides are pinned to ``str``.
    """
    if type(sig) is not str or not sig:
        return False
    computed = signature_from_parts(parts)
    if computed is None:
        return False
    return hmac.compare_digest(computed.encode("utf-8"), sig.encode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# R1 — the typed-confirm surface for money / irreversible gates (spec section 10)
#
# A gate whose SCORED effects include an irreversible class
# (``action_governance.irreversible_effects``) is approved ONCE, under a typed
# confirmation, and never remembered. These helpers are the one implementation of
# that rule, read by the card (``interface.command.gate``), the Inbox resolve path
# (``interface.command.inbox``), the resume rail (``runtime.resume_on_decision``)
# and the gate itself (``runtime.tool_sandbox``) -- so no two of them can disagree
# about whether a card needs a phrase, or which phrase.
#
# The stamp, on the decision context, is:
#   irreversible          True
#   irreversible_effects  the sorted irreversible classes
#   requires_typed_confirm True (``decision_bridge`` floors the card off the phone)
#   confirm_phrase        what the operator must type
# and the operator's typed text rides back on ``confirm_typed``, patched in by the
# Inbox in the same save that resolves the card.

IRREVERSIBLE_FLAG_KEY = "irreversible"
IRREVERSIBLE_EFFECTS_KEY = "irreversible_effects"
REQUIRES_TYPED_CONFIRM_KEY = "requires_typed_confirm"
CONFIRM_PHRASE_KEY = "confirm_phrase"
CONFIRM_TYPED_KEY = "confirm_typed"
#: Stamped by the rail when it RE-OPENS a card that was resolved without the phrase.
TYPED_CONFIRM_REFUSED_KEY = "typed_confirm_refused"
#: The only affirmative option a typed-confirm card offers.
APPROVE_ONCE = "Approve once"
MAX_CONFIRM_PHRASE_CHARS = 64

# Argument names that carry the amount of a money call. Matched case-insensitively,
# plus any name ending in "_amount". Exactly ONE numeric match makes the amount the
# phrase; none, or more than one (ambiguous), falls back to the tool name.
_AMOUNT_KEYS = frozenset({"amount", "total", "total_amount", "sum", "price", "cost",
                          "fee", "value"})
_AMOUNT_RE = None


def _ascii(text) -> str:
    """DEC-32c: a verdict-carrying string is ASCII. Never raises."""
    if type(text) is not str:
        return ""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _amount_text(value) -> str:
    """The amount as the operator will type it, or "" when ``value`` is not a plain,
    finite number (or a numeric string). Type-pinned: ``bool`` is not an amount."""
    global _AMOUNT_RE
    if type(value) is int:
        text = str(value)
    elif type(value) is float:
        import math
        if not math.isfinite(value):
            return ""
        text = repr(value)
    elif type(value) is str:
        if _AMOUNT_RE is None:
            import re
            _AMOUNT_RE = re.compile(r"-?[0-9]{1,15}(\.[0-9]{1,8})?")
        text = value.strip()
        if not _AMOUNT_RE.fullmatch(text):
            return ""
    else:
        return ""
    return text if 0 < len(text) <= 32 else ""


def typed_confirm_phrase(tool_name, parameters=None, effects=()) -> str:
    """The phrase the operator types to approve ONE irreversible call.

    Names the action: the AMOUNT when ``effects`` include a money class and the
    arguments carry exactly one numeric amount, otherwise the tool name. ASCII,
    at most :data:`MAX_CONFIRM_PHRASE_CHARS`. Deterministic, so the gate that
    stamps it and the gate that later checks a chat-lane approval compute the same
    value from the same call.
    """
    from systemu.runtime.action_governance import MONEY_EFFECTS
    money = any(type(e) is str and e in MONEY_EFFECTS for e in (effects or ()))
    if money and type(parameters) is dict:
        amounts = []
        for key, value in parameters.items():
            if type(key) is not str:
                continue
            k = key.strip().lower()
            if k in _AMOUNT_KEYS or k.endswith("_amount"):
                text = _amount_text(value)
                if text:
                    amounts.append(text)
        if len(amounts) == 1:
            return amounts[0]
    name = _ascii(tool_name).strip()[:MAX_CONFIRM_PHRASE_CHARS].strip()
    return name or "confirm"


def irreversible_card_effects(ctx) -> tuple:
    """The irreversible classes a decision context carries, from POSITIVE evidence
    only: the stamped ``irreversible_effects``, the stamped (scored) ``effect_tags``,
    or the stamped ``irreversible`` flag. ``()`` when none.

    The stamped effect tags are read too so a card posted by an OLDER build -- which
    carries ``effect_tags`` but no R1 stamp -- is still recognised as a money card.
    """
    if type(ctx) is not dict:
        return ()
    from systemu.runtime.action_governance import irreversible_effects
    found = set(irreversible_effects(ctx.get(IRREVERSIBLE_EFFECTS_KEY)))
    found |= set(irreversible_effects(ctx.get("effect_tags")))
    if ctx.get(IRREVERSIBLE_FLAG_KEY) is True and not found:
        found.add("irreversible")
    return tuple(sorted(found))


def expected_confirm_phrase(ctx) -> str:
    """The phrase a resolved typed-confirm card must carry: the stamped one, or --
    for a card an older build posted -- the stamped tool name. "" when neither is a
    usable plain ``str``; then nothing can match and the card cannot be approved."""
    if type(ctx) is not dict:
        return ""
    phrase = ctx.get(CONFIRM_PHRASE_KEY)
    if type(phrase) is str and phrase.strip():
        return _ascii(phrase).strip()[:MAX_CONFIRM_PHRASE_CHARS]
    name = ctx.get("tool_name")
    if type(name) is str and name.strip():
        return typed_confirm_phrase(name)
    return ""


def confirm_phrase_matches(typed, expected) -> bool:
    """True only when the operator's ``typed`` text is exactly ``expected``.

    DEC-34/36: ``typed`` is operator-supplied, so both concrete types are pinned to
    ``str`` in this frame (a str subclass with an agreeable ``__eq__`` is refused),
    and the comparison is ``hmac.compare_digest`` over bytes -- no dispatching
    operator touches the operand. Surrounding whitespace on the typed text is
    forgiven; case and everything inside are not.
    """
    if type(typed) is not str or type(expected) is not str or not expected:
        return False
    return hmac.compare_digest(typed.strip().encode("utf-8", "backslashreplace"),
                               expected.encode("utf-8", "backslashreplace"))


def typed_confirm_refusal(ctx, *, choice, typed) -> str:
    """Why resolving this card with ``choice`` (and ``typed``) must NOT take effect,
    or "" when it may. ASCII. The one rule, read by every resolve path:

      * a card with no irreversible effect -> "" (no typed confirm on this card);
      * a denial needs no confirmation -> "", nor does an IMPL-2 reclassify (it
        runs nothing);
      * any affirmative choice other than "Approve once" is refused -- such a card
        cannot be remembered, so a standing / bulk option is never honoured even
        when a stale card still offers it;
      * "Approve once" needs the typed phrase, exactly.
    """
    effects = irreversible_card_effects(ctx)
    if not effects:
        return ""
    label = choice if type(choice) is str else ""
    norm = label.strip().lower()
    if norm in ("", "deny"):
        return ""
    if norm.startswith("reclassify"):
        # IMPL-2: a reclassification approves nothing (it is typed-confirmed on its
        # own panel); the follow-up card it produces is gated by this rule again.
        return ""
    what = ", ".join(effects)
    if norm != APPROVE_ONCE.lower():
        return _ascii(
            f"'{label.strip()}' is not available on this card. It moves money or "
            f"cannot be undone ({what}), so it cannot be remembered - it can only be "
            f"approved once, with the typed confirmation.")
    expected = expected_confirm_phrase(ctx)
    if not expected:
        return _ascii(
            f"this card ({what}) carries no confirmation phrase, so it cannot be "
            "approved - deny it and run the task again.")
    if confirm_phrase_matches(typed, expected):
        return ""
    if type(typed) is str and typed.strip():
        return _ascii(f"the typed confirmation does not match - type {expected} "
                      f"exactly to approve it once.")
    return _ascii(f"type {expected} exactly to approve it once ({what}).")


# ─────────────────────────────────────────────────────────────────────────────
# R1 (spec section 10 / 5.10.b#5) — loosening the gate-mode POSTURE is a typed
# confirmation naming the new posture; tightening stays one action. ONE rule for
# every surface that writes the dial: the Settings card (``interface.pages.
# settings``), ``systemu decisions mode --set`` and ``systemu settings set
# gate_mode``. NiceGUI-free so the CLI can import it.

# How much each gate mode auto-grants: higher = looser.
_MODE_LOOSENESS = {"approve_only": 0, "risk_tiered": 1, "bypass": 2}
# An explicit per-gate override, on the same scale as the mode default it replaces:
# deny < ask < (mode default) < allow.
_OVERRIDE_LOOSENESS = {"deny": 0, "ask": 1, "allow": 3}
_MODE_DEFAULT_OVERRIDE_LOOSENESS = {"approve_only": 1, "risk_tiered": 2, "bypass": 3}


def _mode_rank(mode, *, unknown: int) -> int:
    return _MODE_LOOSENESS.get(mode, unknown) if type(mode) is str else unknown


def _override_rank(value, mode) -> int:
    """Looseness of one per-gate override under ``mode``. Anything unrecognised
    ranks as the loosest (fail-closed: a value we cannot read is not a tightening)."""
    if type(value) is not str:
        return 3
    v = value.strip().lower()
    if not v:
        return _MODE_DEFAULT_OVERRIDE_LOOSENESS.get(mode, 3)
    return _OVERRIDE_LOOSENESS.get(v, 3)


def posture_loosening(current, proposed) -> list:
    """The changes in ``proposed`` that LOWER approval requirements relative to
    ``current`` (both ``{mode, overrides, no_floor}``), as short ASCII tokens in a
    fixed order: the new mode, then ``no_floor``, then ``<gate_type>=<verdict>``
    per loosened override. ``[]`` when the change only tightens or keeps the
    posture."""
    cur = current if type(current) is dict else {}
    new = proposed if type(proposed) is dict else {}
    cur_mode, new_mode = cur.get("mode"), new.get("mode")
    tokens = []
    # An unreadable CURRENT mode ranks tightest, an unreadable NEW mode loosest:
    # both directions fail closed.
    if _mode_rank(new_mode, unknown=3) > _mode_rank(cur_mode, unknown=0):
        tokens.append(str(new_mode))
    if new.get("no_floor") is True and cur.get("no_floor") is not True:
        tokens.append("no_floor")
    cur_ov = cur.get("overrides") if type(cur.get("overrides")) is dict else {}
    new_ov = new.get("overrides") if type(new.get("overrides")) is dict else {}
    for gate_type in sorted(str(k) for k in set(cur_ov) | set(new_ov)):
        old_v, new_v = cur_ov.get(gate_type, ""), new_ov.get(gate_type, "")
        if old_v == new_v and type(old_v) is type(new_v):
            continue     # an unchanged override: a mode change is its own token
        if _override_rank(new_v, new_mode) > _override_rank(old_v, cur_mode):
            label = new_v.strip() if type(new_v) is str and new_v.strip() else "default"
            tokens.append(f"{gate_type}={label}")
    return [_ascii(t) for t in tokens]


def posture_change_refusal(tokens, typed, *, how: str = "") -> str:
    """"" when the change may be saved, else why not (ASCII). The phrase is the
    tokens joined by single spaces, compared with :func:`confirm_phrase_matches`
    (type-pinned, constant time -- DEC-34/36). ``how`` says where the operator
    supplies it ("in the confirmation box" by default; the CLI passes its flag)."""
    if not tokens:
        return ""
    phrase = " ".join(tokens)
    if confirm_phrase_matches(typed, phrase):
        return ""
    where = how if type(how) is str and how else "in the confirmation box"
    return _ascii(f"This change lowers approval requirements ({phrase}). Type "
                  f"{phrase} exactly {where} to save it. Nothing was saved.")


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
            # R1: a signature the gate classified irreversible is covered by NO
            # standing approval, whatever the file says (an older build, a hand
            # edit). The gate refuses on the consumption side too; this keeps
            # every other reader of the store from believing one exists.
            if sig in (self._data.get("one_shot_only") or {}):
                return False
            return sig in self._data.get("approved", {})

    # ── R1: signatures that may never hold a STANDING approval ───────────────
    #
    # Written by the GATE (``tool_sandbox._maybe_gate_tool``) when it posts a
    # money / irreversible card -- the verifier side, never the caller asking for
    # an approval -- so the store can refuse a standing allow for that signature
    # "even if a caller asks", without trusting the caller to say what it is.

    def mark_one_shot_only(self, sig: str, *, effects=()) -> bool:
        """Record that ``sig`` is a money / irreversible signature: approvable once,
        under a typed confirmation, never standing. Returns True when newly marked.
        Best-effort and idempotent; never raises."""
        if type(sig) is not str or not sig:
            return False
        try:
            with self._lock:
                self._data = self._load()
                reg: Dict[str, Any] = self._data.setdefault("one_shot_only", {})
                if sig in reg:
                    return False
                reg[sig] = {"marked_at": _now_iso(),
                            "effects": sorted(e for e in (effects or ())
                                              if type(e) is str)}
                self._save()
            return True
        except Exception:
            logger.debug("[CommandApprovals] could not mark %s one-shot-only", sig,
                         exc_info=True)
            return False

    def is_one_shot_only(self, sig: str) -> bool:
        with self._lock:
            self._data = self._load()
            return sig in (self._data.get("one_shot_only") or {})

    def _standing_refusal(self, sig, effect_tags) -> str:
        """Why a STANDING approval for ``sig`` must not be minted, or "". Reads the
        gate-written registry AND any effect tags the caller supplies."""
        from systemu.runtime.action_governance import irreversible_effects
        named = irreversible_effects(effect_tags) if effect_tags is not None else ()
        if named:
            return "its effects include " + ", ".join(named)
        if sig in (self._data.get("one_shot_only") or {}):
            return "the gate classified this signature irreversible"
        return ""

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
                             for_reclassification: Optional[str] = None,
                             confirmed_args: Optional[str] = None) -> None:
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

        R1 ``confirmed_args``: the args fingerprint of the money / irreversible call
        the operator TYPED the confirmation against. Such a bridge redeems only that
        exact call, and only a call the gate scores irreversible (see
        :meth:`consume_resume_approved`); an unscoped bridge never redeems one.
        """
        with self._lock:
            self._data = self._load()
            pend: Dict[str, Any] = self._data.setdefault("resume_pending", {})
            entry: Dict[str, Any] = {"marked_at": _now_iso()}
            if for_reclassification:
                entry["for_reclassification"] = str(for_reclassification)
            if type(confirmed_args) is str and confirmed_args:
                entry["confirmed_args"] = confirmed_args
            pend[sig] = entry
            self._save()

    @staticmethod
    def _confirm_scope_matches(entry: Any, *, require_confirmed: bool,
                               confirmed_args: Optional[str]) -> bool:
        """R1: does this bridge's typed-confirm scope cover the call?

        A call the gate scores irreversible (``require_confirmed``) is covered only
        by a bridge minted against ITS args fingerprint after a typed confirmation;
        both sides are pinned to non-empty ``str`` and compared in constant time --
        there is no "both empty, therefore equal". An ordinary call is never
        covered by a typed-confirm bridge either: scopes must agree both ways."""
        stored = entry.get("confirmed_args") if type(entry) is dict else None
        if not require_confirmed:
            return stored is None
        if type(stored) is not str or not stored:
            return False
        if type(confirmed_args) is not str or not confirmed_args:
            return False
        return hmac.compare_digest(stored.encode("utf-8"), confirmed_args.encode("utf-8"))

    def consume_resume_approved(self, sig: str, *,
                                for_reclassification: Optional[str] = None,
                                require_confirmed: bool = False,
                                confirmed_args: Optional[str] = None) -> bool:
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
        # The refusal is NOTIFIED after the lock is dropped, never inside it.
        # ``EventBus.publish`` runs every subscriber synchronously on this thread
        # and ``self._lock`` is a plain ``threading.Lock``, so emitting from within
        # the critical section deadlocks any subscriber that reads this store — and
        # the lock is then never released, wedging every later caller behind it.
        # Carry the decision out in locals and speak on the other side.
        refused = False
        refused_scope = None
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
                refused, refused_scope = True, scope
            elif not self._confirm_scope_matches(
                    entry, require_confirmed=require_confirmed,
                    confirmed_args=confirmed_args):
                # R1: left in place, like a reclassification mismatch -- it may still
                # be the legitimate one-shot for its own call.
                logger.warning(
                    "[CommandApprovals] refusing resume-bridge for %s: it does not "
                    "carry a typed confirmation for THIS call — the gate re-asks", sig)
                refused = True
                if require_confirmed:
                    refused_scope = "a different call, or without a typed confirmation"
                    want = "a money or irreversible call that needs one"
                else:
                    refused_scope = "a typed-confirm call"
                    want = "an ordinary call"
            else:
                del pend[sig]
                self._save()
        if refused:
            # D3 (dogfood 0.10.28): a REFUSAL is not silence. The operator
            # approved something and this says their approval did not cover
            # the call in front of us, so the gate is about to re-ask. Without
            # an operator-readable line that reads, from their seat, exactly
            # like the resume-into-silence bug: a card resolved, then another
            # card. An ABSENT bridge stays quiet deliberately — that is the
            # ordinary state of every un-approved call and would be pure noise.
            _emit_refusal_event(sig, minted_for=refused_scope, scored_under=want)
            return False
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
        effect_tags=None,
    ) -> bool:
        """Record an "Always allow" for an exact command signature.

        Returns True when newly approved.  Idempotent: approving an
        already-approved signature returns False and leaves the existing
        record (and its timestamp) untouched.

        R1: REFUSES -- returns False, logs, writes nothing, never raises -- when
        the signature is one the gate classified irreversible
        (:meth:`mark_one_shot_only`) or ``effect_tags`` name an irreversible
        class. A money / irreversible action is approved once, under a typed
        confirmation, and never remembered, whoever asks.
        """
        newly = False
        refused = ""
        with self._lock:
            # Re-read so we don't clobber a parallel writer.
            self._data = self._load()
            # R1: decided under the same lock as the write, off the same fresh read.
            refused = self._standing_refusal(sig, effect_tags)
            approved: Dict[str, Any] = self._data.setdefault("approved", {})
            pending: Dict[str, Any] = self._data.setdefault("pending", {})
            if not refused and sig not in approved:
                existing_pending = pending.pop(sig, None) or {}
                approved[sig] = {
                    "approved_at": _now_iso(),
                    "approved_by": approved_by,
                    "command":     command or existing_pending.get("command", ""),
                    "cwd":         cwd or existing_pending.get("cwd", ""),
                }
                self._save()
                newly = True
        if refused:
            logger.warning(
                "[CommandApprovals] REFUSED a standing approval for %s: %s. A money "
                "or irreversible action is approved once, under a typed "
                "confirmation, and never remembered.", sig, refused)
            return False
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
                "session_trusted": {}, "reclassified": {}, "one_shot_only": {}}

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
