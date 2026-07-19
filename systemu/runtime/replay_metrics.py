"""R-A13.5 — deterministic replay metrics over the accreted corpus (§10 / CAP-10).

The measurement substrate the DEC-7 (ask-cap) decision, R-A16/G-LEARN, and CAP-10
consume. DETERMINISTIC post-hoc replay only — never an LLM judge (IMPL-15 discipline)
— so the numbers are replay-stable and defensible.

Slice-1 (here): the **avoidable-forge** metric (CAP-10). For every forged tool, the
capability-slot query is re-run with hindsight: does an EXISTING tool already occupy
that tool's slot (i.e. would it have bound instead of forging a duplicate)? The rate
is the CAP-10 tripwire that adjudicates the DEC-18 "no embeddings" (CAP-8) question,
reported beside the §10 avoidable-ask rate. Computable over the live vault today
(reuses the shipped R-CAP1 index — `capability_index.slot_collisions`).

The **avoidable-ask** side is two signals, deliberately kept apart:

* the R-A13.5 **no-attempt proxy** (§10, a DEC-7 input) — accreted at the ASK point
  from `record_ask`; DIRECTIONAL, non-definitive by construction.
* the R-A16 / G-LEARN **answer-linked** signal (§5.9) — accreted at the ANSWER point
  from `record_ask_avoidable`, so each event knows what the operator actually chose.
  Its `resolvable_confirmed` sub-case is DEFINITIVE (the binder held the value and
  the operator changed nothing); `missing_answered` is a candidate only, whose
  definitive verdict still needs a resolver-replay (documented refinement).

Both are surfaced together by `avoidable_ask_report` / `sharing-on debug
avoidable-ask`, labelled apart so the definitive number is never blurred into the
proxy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


#: Serializes appenders inside THIS process. The documented writer set for both
#: corpora is two threads of the daemon process, so this alone closes the measured
#: loss; the OS file lock below extends the guarantee across processes.
_APPEND_LOCK = threading.Lock()


def _lock_whole_file(fd) -> bool:
    """Best-effort EXCLUSIVE advisory lock on ``fd``. False if unavailable."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except Exception:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)   # byte 0 used purely as a mutex
        return True
    except Exception:
        return False


def _unlock_whole_file(fd) -> None:
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    except Exception:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass


def _append_line(path: Path, rec: Any) -> None:
    """Append ONE json line to ``path`` without losing it to a concurrent appender.

    A buffered text-mode ``open(p, "a")`` is NOT append-safe across handles: each
    writer flushes at its own buffer boundary and one silently overwrites the other's
    bytes. Measured on this corpus: 8 threads x 150 appends landed ~1155/1200 rows —
    no torn line, no exception (so the blanket ``except`` never saw it), just lost
    rows.

    ``O_APPEND`` + a single ``os.write`` is the POSIX answer (the kernel makes the
    seek-to-end and the write one atomic step for a record well under ``PIPE_BUF``).
    It is NOT sufficient on Windows: the CRT EMULATES ``_O_APPEND`` as seek-then-write,
    so the gap is still racy — measured WORSE than the buffered version (~886/1200).
    So the write is also serialized: a process-wide lock for the in-process writers,
    plus a best-effort OS file lock so concurrent RUNS in separate processes are
    covered too.

    ``O_BINARY`` matters on Windows: without it the fd is text-mode and every ``\\n``
    becomes ``\\r\\n``, corrupting a file the rest of the tree reads as UTF-8 LF."""
    data = (json.dumps(rec) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    with _APPEND_LOCK:
        fd = os.open(str(path), flags, 0o600)
        try:
            locked = _lock_whole_file(fd)
            try:
                os.lseek(fd, 0, os.SEEK_END)   # explicit: O_APPEND is emulated on NT
                os.write(fd, data)
            finally:
                if locked:
                    _unlock_whole_file(fd)
        finally:
            os.close(fd)


# ── avoidable-ASK corpus (§10, decides DEC-7) — slice-2 ────────────────────────
# A deterministic directional signal accreted from real runs. The ask rail records
# each harness ask with its resolution-attempt instrumentation (attempts_before,
# tool_attempts, blocked_signals — the v0.10.0 pull instrumentation); the report
# counts asks made with NO recorded resolution attempt — a §10 lower-bound. The
# corpus is append-only, single-writer (the shadow exec thread) — CONC-MAP registered.

def _ask_corpus_path(vault) -> Path:
    return Path(vault.root) / "audit" / "ask_corpus.jsonl"


def record_ask(vault, *, kind: str = "", attempts_before: int = 0,
               blocked_signals: Any = None, tool_attempts: int = 0,
               confidence: float = 0.5) -> None:
    """Append one ask to the corpus (R-A13.5 / DEC-11 accretion). OBSERVABILITY-ONLY,
    append-only, single-writer — NEVER raises (a recording hiccup must never affect
    the run that made the ask)."""
    try:
        rec = {
            "kind": str(kind or ""),
            "attempts_before": int(attempts_before or 0),
            "tool_attempts": int(tool_attempts or 0),
            "blocked_signals": list(blocked_signals or []),
            "confidence": float(confidence or 0.0),
        }
        _append_line(_ask_corpus_path(vault), rec)
    except Exception:
        pass


def load_ask_corpus(vault) -> List[Dict[str, Any]]:
    """All recorded asks. Defensive: a broken/absent file / malformed line ⇒ skipped."""
    try:
        p = _ask_corpus_path(vault)
        if not p.exists():
            return []
        out: List[Dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out
    except Exception:
        return []


# ── R-A16 / G-LEARN slice-2: the ANSWER-LINKED avoidable-ask signal (§5.9) ────
#
# §5.9: "When the operator answers an input/decision/capability ask with something
# the inventory/discovery/resolver COULD have produced, record an AskWasAvoidable
# event with the class + near-miss score."
#
# SEPARATE FILE, deliberately. This is NOT an extension of ``ask_corpus.jsonl``:
#   * ``avoidable_ask_report`` (above) counts any corpus row whose attempt fields are
#     absent/zero as an avoidable CANDIDATE. Answer-linked rows carry none of those
#     fields, so folding them into that file would silently score every one of them
#     as a no-attempt ask — corrupting BOTH numerator and denominator of a SHIPPED
#     DEC-7 metric.
#   * the writers differ: ``ask_corpus`` is written by the shadow exec loop AT THE
#     ASK POINT (its CONC-MAP row pins that single writer); these events are written
#     at the ANSWER point — the pre-loop elicitation rail and the daemon's
#     harness-grant reconciler. Its own file gets its own CONC-MAP row.
# Both signals are still reported side by side, LABELLED APART (one is a directional
# proxy; the resolvable-confirmed sub-case here is definitive).
#
# ══ SECRETS ══ This is a PLAINTEXT append-only audit artefact. It records REFS ONLY
# — never an answer, never a bound value. Three independent guards, because a leak
# here is a shipped data leak:
#   1. ``requirement_snapshot`` refuses anything outside §5.9's class list (which
#      excludes ``credential``) or whose schema_path reads as secret-mode;
#   2. ``record_ask_avoidable`` re-enforces both on the snapshot it is handed (which
#      may have crossed a suspend inside a card spec, so it is untrusted input);
#   3. ``record_ask_avoidable`` accepts ``candidate_ref`` ONLY in the digest shape
#      ``value_ref`` emits — the one value-derived field carried in from outside.
# (1) and (2) are backed by the codebase's canonical secret marker
# ``elicitation.is_secret_field`` — the same predicate that routes secret fields
# URL-mode instead of into a form — rather than a bespoke rule.
#
# The refs themselves are KEYED (HMAC-SHA256 under a per-vault key), not a bare
# digest. "Refs only" holds literally for an unsalted ``sha256(value)[:16]``, but it
# is not an ANONYMISATION guarantee: a low-entropy answer is recoverable by brute
# force in well under a second (a 6-digit ``login/verification_code`` — a field name
# the secret-name tokens do NOT catch — was recovered from its digest during review).
# Keying makes the corpus unreadable to anyone who does not also hold the vault's
# secret, which is what "safe to share this file" actually requires.

#: §5.9's class list. ``credential`` is EXCLUDED — a credential answer is a secret.
AVOIDABLE_ASK_CLASSES = ("input", "decision", "capability")


def _avoidable_ask_path(vault) -> Path:
    return Path(vault.root) / "audit" / "ask_avoidable.jsonl"


# ── the keyed ref function ────────────────────────────────────────────────────
_REF_SCHEME = "hmac256"
_REF_MAC_HEX = 16
_REF_KEY_ID_HEX = 8
#: Domain separation — this key must never coincide with the session-signing key it
#: is derived from, nor with any future subkey off the same seed.
_REF_KEY_INFO = b"systemu/r-a16/ask-avoidable/ref-key/v1"
_REF_KEY_ID_INFO = b"systemu/r-a16/ask-avoidable/key-id/v1"

_REF_KEY_LOCK = threading.Lock()
_REF_KEY_CACHE: Dict[str, Tuple[bytes, str]] = {}


def _vault_root(vault) -> str:
    """The vault's root path, accepting a vault object OR a bare path."""
    root = getattr(vault, "root", None)
    return str(vault if root is None else root)


def _ref_key(vault) -> Tuple[bytes, str]:
    """``(key, key_id)`` for ``vault`` — the per-vault HMAC key and its public id.

    NOT a new secret scheme: it is DERIVED, by HMAC domain separation, from the
    per-vault secret this codebase already generates and persists —
    ``dashboard_auth.session_secret`` (64 random hex chars, stored through the S5
    at-rest envelope, get-or-create, per vault). Reusing that derivation means there
    is one place that decides how a vault secret is generated, persisted and
    protected, rather than two.

    ``key_id`` is a non-secret 8-hex fingerprint of the derived key, embedded in every
    ref. It makes a key change DETECTABLE: a candidate digest signed by a different
    key is incomparable, and the recorder drops it (→ candidate-only) rather than
    reporting a mismatch as "the operator overrode the binder".

    Cached per vault root under a lock — first use derives the key, and concurrent
    writers must not race into two different generated secrets. Raises if no key can
    be derived; every caller treats that as fail-closed (no ref ⇒ no row)."""
    root = _vault_root(vault)
    hit = _REF_KEY_CACHE.get(root)
    if hit is not None:
        return hit
    with _REF_KEY_LOCK:
        hit = _REF_KEY_CACHE.get(root)
        if hit is not None:
            return hit
        from systemu.runtime.dashboard_auth import session_secret
        seed = str(session_secret(root) or "")
        if len(seed) < 32:
            raise ValueError("no usable per-vault secret for the ask-avoidable ref key")
        key = hmac.new(seed.encode("utf-8"), _REF_KEY_INFO, hashlib.sha256).digest()
        key_id = hmac.new(key, _REF_KEY_ID_INFO,
                          hashlib.sha256).hexdigest()[:_REF_KEY_ID_HEX]
        _REF_KEY_CACHE[root] = (key, key_id)
        return key, key_id


def normalize_value(value: Any) -> str:
    """Canonical string form of a value, applied on BOTH sides of the comparison.

    The binder holds a TYPED schema value; the operator answers through a form that
    hands back a string. Without one shared normalisation a genuine confirm reads as
    an override — the same inverted-signal failure the handle-vs-value bug produced.
    Deliberately minimal: strip surrounding whitespace, fold the two spellings of a
    boolean (Python's ``True`` vs a form's ``true``), and normcase a PATH.

    The path fold matters because paths are the dominant ``content_derived`` source:
    ``out/draft.md``, ``out\\draft.md`` and ``OUT/DRAFT.MD`` are ONE file on Windows,
    but compared raw they digest differently, so confirming the binder's own candidate
    read as an override — and the consumer treats "override" as "the operator typed
    this", i.e. TRUSTED. The failure direction was toward trust, which is the wrong way
    for a value the binder scraped off a page.

    ``os.path.normcase`` is used rather than a hand-rolled lower()+replace() precisely
    because it encodes the PLATFORM's rule: fold case and separators on Windows, and do
    NOTHING on POSIX, where ``a\\b`` is a legitimate filename and folding it would
    conflate two genuinely different files. A URL is excluded — it carries separators
    but its path segment is case-SENSITIVE."""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value).strip()
    low = s.lower()
    if low in ("true", "false"):
        return low
    if ("/" in s or "\\" in s) and "://" not in s:
        return os.path.normcase(s)
    return s


def value_ref(value: Any, vault: Any) -> Optional[str]:
    """A NON-REVERSIBLE, per-vault-KEYED content address for a value — the only form
    in which any value-derived datum enters this corpus. Never the value itself.

    Shape: ``hmac256:<key_id>:<mac>``. ``None`` when there is no value, or when no
    vault key can be derived — callers must treat ``None`` as fail-closed and record
    NOTHING rather than fall back to an unkeyed digest."""
    if value is None:
        return None
    s = normalize_value(value)
    if not s:
        return None
    try:
        key, key_id = _ref_key(vault)
    except Exception:
        return None
    mac = hmac.new(key, s.encode("utf-8"), hashlib.sha256).hexdigest()[:_REF_MAC_HEX]
    return f"{_REF_SCHEME}:{key_id}:{mac}"


_VALUE_REF_LEN = len(_REF_SCHEME) + 1 + _REF_KEY_ID_HEX + 1 + _REF_MAC_HEX
_HEX = "0123456789abcdef"


def _is_value_ref(ref: Any) -> bool:
    """True only for a string in the exact shape :func:`value_ref` emits."""
    if not isinstance(ref, str) or len(ref) != _VALUE_REF_LEN:
        return False
    scheme, _, rest = ref.partition(":")
    if scheme != _REF_SCHEME:
        return False
    key_id, sep, mac = rest.partition(":")
    if sep != ":" or len(key_id) != _REF_KEY_ID_HEX or len(mac) != _REF_MAC_HEX:
        return False
    return all(c in _HEX for c in key_id) and all(c in _HEX for c in mac)


def _ref_key_id(ref: Any) -> Optional[str]:
    """The key-id segment of a ref. PURE PARSING, total, never raises — it deliberately
    does NOT re-validate the shape. Validating here too would silently duplicate guard
    3, leaving the corpus protected but the guard itself unkillable: removing guard 3
    would change nothing observable, so no test could hold it in place."""
    parts = str(ref).split(":")
    return parts[1] if len(parts) >= 3 else None


def _is_secret_path(schema_path: str, kind: str) -> bool:
    """Defence-in-depth secret detection, REUSING the shipped marker rather than a
    bespoke rule: build the same field descriptor the elicitation rail builds and ask
    :func:`elicitation.is_secret_field`. A ``credential`` kind carries the
    ``format="password"`` marker exactly as ``requirement_to_field`` sets it, so a
    mis-kinded secret (``kind="input"``, ``schema_path="auth/api_key"``) is caught by
    the name tokens. Import failure ⇒ treat as secret (fail-closed)."""
    try:
        from systemu.runtime.elicitation import is_secret_field
    except Exception:
        return True
    field: Dict[str, Any] = {"name": str(schema_path or "")}
    if str(kind or "").lower() == "credential":
        field["format"] = "password"
    try:
        return bool(is_secret_field(field))
    except Exception:
        return True


def requirement_snapshot(req: Any) -> Optional[Dict[str, Any]]:
    """The ask-time, SECRET-FREE snapshot of ONE ``Requirement`` — the join key the
    answer-side needs, safe to stamp into a card spec that is persisted in plaintext.

    Returns ``None`` (⇒ nothing recorded, nothing stamped) for anything outside
    §5.9's class list, for any secret-mode ``schema_path``, or for a requirement with
    no ``schema_path`` (no identity ⇒ no signal). Never raises.

    ``candidate_ref`` is the binder's ``bound_value_digest`` — a keyed digest of the
    bind's RESOLVED VALUE, stamped at bind time.

    It is emphatically NOT derived from ``bound_value_ref``. That field is a
    NAMESPACED HANDLE naming the bind SOURCE (``file:C:/work/draft.md``,
    ``profile:email``, ``run_context:out/prior.md``, ``schema_default:out_path``, …) —
    every one of the binder's return sites emits that shape. A handle can never equal
    an operator's answer, so classifying off it made ``resolvable_confirmed``
    structurally unreachable and recorded every bound ask as ``resolvable_overridden``
    ("the binder was WRONG") — the exact inverse of the truth. The handle also embeds
    a real filesystem path, so it must not be carried here at all; only the digest
    crosses the suspend.

    A requirement with no ``bound_value_digest`` (legacy data, or a source that binds
    an IDENTIFIER rather than an extractable value) yields ``candidate_ref=None`` and
    degrades to ``missing_answered`` — candidate-only, the safe direction."""
    try:
        if isinstance(req, dict):
            get = req.get
        elif req is not None and hasattr(req, "kind"):
            get = lambda k, d=None: getattr(req, k, d)   # noqa: E731
        else:
            return None
        kind = str(get("kind", "") or "").lower()
        schema_path = str(get("schema_path", "") or "")
        if not schema_path:
            return None
        if kind not in AVOIDABLE_ASK_CLASSES:            # excludes `credential`
            return None
        if _is_secret_path(schema_path, kind):
            return None
        try:
            conf = float(get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        vo = get("value_origin", None)
        # Guard 1 also covers the digest: only the exact keyed shape is ever stamped,
        # so a hand-built requirement cannot smuggle a raw value in through this field.
        digest = get("bound_value_digest", None)
        return {
            "schema_path": schema_path,
            "class": kind,
            "state": str(get("state", "") or ""),
            "source": str(get("source", "") or ""),
            "value_origin": (str(vo) if vo else None),
            "confidence": conf,
            "candidate_ref": digest if _is_value_ref(digest) else None,
        }
    except Exception:
        return None


def record_ask_avoidable(vault, *, ask_id: Any = "", snapshot: Any = None,
                         answer: Any = None) -> None:
    """Append ONE ``AskWasAvoidable`` event (§5.9). OBSERVABILITY-ONLY, append-only,
    NEVER raises — a recording hiccup must never affect the run that made the ask.

    ONE ANSWER IS ONE OBSERVATION. ``snapshot`` may be a single snapshot dict OR a
    LIST of snapshots for the SAME ``schema_path`` — the shape the bundled scope card
    genuinely produces. ``build_requirement_report`` keeps same-path/different-value
    asks DISTINCT (``bound_value_ref`` is in its dedupe key, deliberately), while
    ``elicitation_schema_from_fields`` collapses same-named fields into ONE form
    property. So the operator sees one slot, the card carries N snapshots, and exactly
    one answer comes back. Writing N rows off it halved ``definitive_rate`` and
    invented ``necessary_overridden`` observations the operator never made. A list
    yields ONE row, holding every valid candidate for the path.

    Three deterministic sub-cases:
      * **resolvable-confirmed** — the binder HELD a candidate value and asked only
        because of the T_high gate or ``content_derived`` taint, and the operator
        confirmed it unchanged (ANY candidate for the path matching is a confirm).
        Avoidable **by construction**; near-miss = that bind's confidence. No replay
        needed — this is the definitive core of the metric.
      * **resolvable-overridden** — candidates existed and the operator answered
        something else. The ask was NECESSARY (the binder's value was wrong); never
        counted as avoidable.
      * **missing-answered** — no comparable candidate. The answer ref + the (empty)
        candidate set are recorded; whether the resolver COULD have produced it is a
        resolver-replay question, OUT OF SCOPE here (documented refinement).

    No answer ⇒ no record: the signal is answer-linked, so a decline/cancel/empty
    answer is not an observation. No vault key ⇒ no record either: without a key there
    is no non-reversible ref, and falling back to an unkeyed digest (or the raw text)
    is precisely what must never happen in a plaintext audit file."""
    try:
        snaps = [s for s in (snapshot if isinstance(snapshot, list) else [snapshot])
                 if isinstance(s, dict) and s]
        if not snaps:
            return
        head = snaps[0]
        cls = str(head.get("class", "") or "").lower()
        schema_path = str(head.get("schema_path", "") or "")
        # Guard 2 (defence-in-depth): re-enforce the class + secret exclusion on the
        # snapshot itself, so a hand-built or mis-stamped snapshot cannot slip a
        # secret-mode ask into a plaintext audit file.
        if cls not in AVOIDABLE_ASK_CLASSES or not schema_path:
            return
        if _is_secret_path(schema_path, cls):
            return
        # Two fail-closed conditions, one branch. NO ANSWER (absent / empty / all
        # whitespace) ⇒ no observation: the signal is answer-linked, so a decline or a
        # blank form is not an event. NO VAULT KEY ⇒ no non-reversible ref, and falling
        # back to an unkeyed digest (or the raw text) is precisely what must never
        # happen in a plaintext audit file. ``value_ref`` returns None for both.
        answer_ref = value_ref(answer, vault)
        if not answer_ref:
            return
        key_id = _ref_key_id(answer_ref)

        candidates: List[Dict[str, Any]] = []
        matched: Optional[str] = None
        seen_refs: Dict[str, int] = {}
        for s in snaps:
            # Every element must agree with the head's identity — guard 2 applies per
            # element, so a grouped snapshot can never become a bypass.
            if str(s.get("class", "") or "").lower() != cls:
                continue
            if str(s.get("schema_path", "") or "") != schema_path:
                continue
            # Guard 3: ``candidate_ref`` is the ONLY value-derived datum carried in
            # from outside (it rides a card spec across a suspend, so it is
            # attacker-shaped input as far as this writer is concerned). Accept it
            # ONLY in the digest form value_ref() produces — anything else is dropped
            # rather than written, so a mis-stamped or hand-built snapshot can never
            # smuggle a raw value into a plaintext audit file. Dropping it degrades
            # the row to missing-answered (under-counting) — the safe direction.
            cand_ref = s.get("candidate_ref") or None
            if not _is_value_ref(cand_ref):
                continue
            # A digest signed by a DIFFERENT vault key is not comparable to this
            # answer. Drop it (→ candidate-only) rather than let the inevitable
            # mismatch be reported as "the operator overrode the binder".
            if _ref_key_id(cand_ref) != key_id:
                continue
            try:
                score = float(s.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if cand_ref in seen_refs:     # same value bound twice ⇒ one candidate
                i = seen_refs[cand_ref]
                if score > candidates[i]["score"]:
                    candidates[i]["score"] = score
                    candidates[i]["value_origin"] = s.get("value_origin")
                continue
            seen_refs[cand_ref] = len(candidates)
            candidates.append({"ref": cand_ref, "score": score,
                               "value_origin": s.get("value_origin")})
            if cand_ref == answer_ref and matched is None:
                matched = cand_ref

        if candidates:
            resolution = "resolvable_confirmed" if matched else "resolvable_overridden"
            near_miss = max(c["score"] for c in candidates
                            if matched is None or c["ref"] == matched)
        else:
            resolution, near_miss = "missing_answered", 0.0
        rec = {
            "ask_id": str(ask_id or ""),
            "class": cls,
            "schema_path": schema_path,
            "state": str(head.get("state", "") or ""),
            "source": str(head.get("source", "") or ""),
            "candidates": candidates,
            "matched_candidate": matched,
            "near_miss_score": near_miss,
            "resolution": resolution,
            "answer_ref": answer_ref,
        }
        _append_line(_avoidable_ask_path(vault), rec)
    except Exception:
        pass


def load_avoidable_ask_corpus(vault) -> List[Dict[str, Any]]:
    """All recorded AskWasAvoidable events, in file order. Defensive: a broken/absent
    file or a malformed line ⇒ skipped."""
    try:
        p = _avoidable_ask_path(vault)
        if not p.exists():
            return []
        out: List[Dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out
    except Exception:
        return []


def _bucket() -> Dict[str, int]:
    return {"total": 0, "definitive_avoidable": 0,
            "necessary_overridden": 0, "missing_answered": 0}


def answer_linked_ask_report(vault) -> Dict[str, Any]:
    """§5.9 — the answer-linked avoidable-ask numbers + the ask→resolve conversion
    trend. Deterministic over the corpus, never an LLM judge. Never raises.

    ``definitive_avoidable`` (the ``resolvable_confirmed`` sub-case) is DEFINITIVE:
    the binder demonstrably held the value and the operator changed nothing, so the
    ask was avoidable by construction. ``missing_answered`` is reported SEPARATELY and
    is NOT folded into that rate — deciding whether the resolver could have produced
    an unbound answer needs the resolver-replay (out of scope; documented refinement).

    ``conversion`` is §5.9's ask→resolve signal: a (class, schema_path) asked more than
    once, first with NOTHING bound and later with a candidate present, means the world
    grew and the ask is converting toward a RESOLVE."""
    try:
        recs = load_avoidable_ask_corpus(vault)
    except Exception:
        recs = []
    total = len(recs)
    by_class: Dict[str, Dict[str, int]] = {}
    counts = _bucket()
    for r in recs:
        cls = str(r.get("class", "") or "?")
        b = by_class.setdefault(cls, _bucket())
        res = str(r.get("resolution", "") or "")
        for tgt in (counts, b):
            tgt["total"] += 1
            if res == "resolvable_confirmed":
                tgt["definitive_avoidable"] += 1
            elif res == "resolvable_overridden":
                tgt["necessary_overridden"] += 1
            elif res == "missing_answered":
                tgt["missing_answered"] += 1

    # ask→resolve conversion, per (class, schema_path) group in file order.
    groups: Dict[tuple, List[str]] = {}
    for r in recs:
        key = (str(r.get("class", "") or ""), str(r.get("schema_path", "") or ""))
        groups.setdefault(key, []).append(str(r.get("resolution", "") or ""))
    eligible = converted = repeat_asks = 0
    for _, seq in groups.items():
        if len(seq) < 2:
            continue
        repeat_asks += len(seq) - 1
        if seq[0] != "missing_answered":
            continue                        # already resolvable at first ask
        eligible += 1
        if any(s.startswith("resolvable") for s in seq[1:]):
            converted += 1

    # positional trend on the definitive rate (first half vs second half, file order).
    def _rate(chunk: List[Dict[str, Any]]) -> float:
        if not chunk:
            return 0.0
        hits = sum(1 for r in chunk
                   if str(r.get("resolution", "")) == "resolvable_confirmed")
        return hits / len(chunk)

    mid = total // 2
    first_rate, second_rate = _rate(recs[:mid]), _rate(recs[mid:])

    return {
        "total": total,
        "definitive_avoidable": counts["definitive_avoidable"],
        "definitive_rate": (counts["definitive_avoidable"] / total) if total else 0.0,
        "necessary_overridden": counts["necessary_overridden"],
        "missing_answered": counts["missing_answered"],
        "by_class": by_class,
        "conversion": {
            "groups": len(groups),
            "eligible": eligible,
            "converted": converted,
            "rate": (converted / eligible) if eligible else 0.0,
            "repeat_asks": repeat_asks,
        },
        "trend": {
            "first_half_definitive_rate": first_rate,
            "second_half_definitive_rate": second_rate,
            "delta": second_rate - first_rate,
        },
    }


def avoidable_ask_report(vault) -> Dict[str, Any]:
    """§10 — a DETERMINISTIC DIRECTIONAL signal for the avoidable-ask rate (a DEC-7
    input). Counts asks made with NO recorded resolution attempt (zero tool-attempts
    AND no blocking signal): by §10 ("try inventory + discovery + resolver + a safe
    default BEFORE asking") those are avoidable-CANDIDATES. This is a NON-DEFINITIVE
    PROXY / leading indicator, not a strict bound — its bias is two-sided (it can miss
    an avoidable ask that logged a *failed* tool attempt, and can count a necessary
    ACCESS/COMPUTE ask that legitimately did no tool attempt). The definitive rate
    needs a resolver-replay over each ask's inventory snapshot (the documented
    refinement). Operator-input (``kind=="input"``) asks are excluded — necessary by
    nature. Reported beside the avoidable-forge rate. Never raises."""
    corpus = [a for a in load_ask_corpus(vault)
              if str(a.get("kind", "")).strip().lower() != "input"]
    total = len(corpus)
    no_attempt = [
        a for a in corpus
        if int(a.get("attempts_before") or 0) == 0
        and int(a.get("tool_attempts") or 0) == 0
        and not (a.get("blocked_signals") or [])
    ]
    return {
        "total_asks": total,
        "no_attempt_count": len(no_attempt),
        "rate": (len(no_attempt) / total) if total else 0.0,
        # R-A16 §5.9 — the ANSWER-LINKED signal, from its own corpus. Reported beside
        # the proxy above but never blended into it: one is directional, the
        # resolvable-confirmed sub-case of the other is definitive.
        "answer_linked": answer_linked_ask_report(vault),
    }


def format_avoidable_ask(report: Dict[str, Any]) -> List[str]:
    r = report or {}
    total = int(r.get("total_asks", 0) or 0)
    n = int(r.get("no_attempt_count", 0) or 0)
    rate = float(r.get("rate", 0.0) or 0.0)
    lines = [
        f"No-prior-attempt asks: {n}/{total} = {rate * 100:.0f}%",
        "  (asks made with no recorded tool-resolution attempt and no blocking signal —",
        "   a deterministic DIRECTIONAL signal (non-definitive proxy) for the §10",
        "   avoidable-ask rate; a DEC-7 input. The definitive rate needs a resolver-replay",
        "   over each ask's inventory snapshot.)",
    ]
    al = r.get("answer_linked")
    if isinstance(al, dict):
        lines.extend(format_answer_linked_ask(al))
    return lines


def format_answer_linked_ask(report: Dict[str, Any]) -> List[str]:
    """R-A16 §5.9 — the answer-linked block. The two sub-cases are labelled APART on
    purpose: resolvable-confirmed is DEFINITIVE, missing-answered is a candidate."""
    r = report or {}
    total = int(r.get("total", 0) or 0)
    dfn = int(r.get("definitive_avoidable", 0) or 0)
    drate = float(r.get("definitive_rate", 0.0) or 0.0)
    ovr = int(r.get("necessary_overridden", 0) or 0)
    mis = int(r.get("missing_answered", 0) or 0)
    conv = r.get("conversion") or {}
    trend = r.get("trend") or {}
    lines = [
        "",
        f"Answer-linked asks (§5.9, R-A16): {total} answered input/decision/capability "
        f"ask(s) recorded  [credential asks are excluded by design]",
        f"  · DEFINITIVE avoidable (resolvable-confirmed): {dfn}/{total} "
        f"= {drate * 100:.0f}%",
        "      (the binder HELD the value and asked only for the T_high / content_derived",
        "       confirm; the operator changed nothing => avoidable BY CONSTRUCTION,",
        "       no replay needed. near-miss score = the bind confidence.)",
        f"  · Necessary (resolvable-overridden): {ovr} — the binder's candidate was WRONG;",
        "      asking was correct. Never counted as avoidable.",
        f"  · Candidate only (missing-answered): {mis} — NOT definitive. Whether the",
        "      resolver could have produced these needs a resolver-replay over each ask's",
        "      inventory snapshot (out of scope here; documented refinement).",
    ]
    if int(conv.get("eligible", 0) or 0):
        lines.append(
            f"  · ask->resolve conversion: {int(conv.get('converted', 0) or 0)}"
            f"/{int(conv.get('eligible', 0) or 0)} repeat-asked requirement(s) became"
            f" resolvable = {float(conv.get('rate', 0.0) or 0.0) * 100:.0f}%"
            f"  ({int(conv.get('repeat_asks', 0) or 0)} re-ask(s) over"
            f" {int(conv.get('groups', 0) or 0)} requirement(s))")
    else:
        lines.append("  · ask->resolve conversion: no repeat-asked requirement yet "
                     "(needs the same requirement asked twice).")
    if total >= 2:
        lines.append(
            f"  · trend (definitive rate, first half -> second half): "
            f"{float(trend.get('first_half_definitive_rate', 0.0) or 0.0) * 100:.0f}% -> "
            f"{float(trend.get('second_half_definitive_rate', 0.0) or 0.0) * 100:.0f}%")
    by_class = r.get("by_class") or {}
    for cls in sorted(by_class):
        b = by_class[cls] or {}
        lines.append(f"      [{cls}] {int(b.get('definitive_avoidable', 0) or 0)}"
                     f"/{int(b.get('total', 0) or 0)} definitive-avoidable")
    return lines


def avoidable_forge_report(vault) -> Dict[str, Any]:
    """CAP-10 — the avoidable-forge rate over the vault's forged tools.

    A forged tool is AVOIDABLE if an existing tool WOULD HAVE BOUND instead of
    forging it. Faithful to "instead of forging" (not a symmetric slot-duplicate
    count — the adversarial-review fix): for a forged tool F occupying slot S,

      • a NON-forged (builtin/MCP) tool in S ⇒ avoidable (it would have bound); else
      • among forged-only occupants of S, the FIRST forge into the (then-empty) slot
        was NOT avoidable — so exactly k-1 of k forged-only occupants count (the
        deterministic 'first' = the min tool_id; the rest are the redundant extras).

    Forged tools with no derivable slot are UNASSESSABLE (reported separately, kept
    out of numerator AND denominator so they don't deflate the rate). Deterministic,
    read-only (live in-memory index derive, never writes), never raises."""
    try:
        from systemu.runtime import capability_index as ci
        from systemu.runtime import capability_slots as cs
    except Exception:
        return _empty()
    try:
        rows = vault.list_tools() or []
    except Exception:
        return _empty()
    forged = [t for t in rows if isinstance(t, dict) and t.get("forged_by_systemu")]

    def _primary_slot(name: str) -> str:
        s = cs.slots_from_name(name or "")
        return cs.slot_str(s[0]) if s else ""

    # occupancy from the live index: which slots hold a NON-forged (builtin/MCP)
    # tool, and the names of those would-be binders per slot.
    try:
        index = list(ci.derive_index(vault) or [])
    except Exception:
        index = []
    slot_nonforged_names: Dict[str, set] = {}
    for r in index:
        origin = str(getattr(r, "origin", "") or "")
        if origin.startswith("forged"):
            continue
        nm = str(getattr(r, "name", "") or "")
        for s in (getattr(r, "slots", []) or []):
            slot_nonforged_names.setdefault(s, set()).add(nm)

    # forged tools grouped by their primary slot (for the k-1 first-forge rule)
    forged_slot: Dict[str, str] = {}
    slot_forged_ids: Dict[str, List[str]] = {}
    for t in forged:
        tid = str(t.get("id", "") or "")
        s = _primary_slot(str(t.get("name", "") or ""))
        forged_slot[tid] = s
        if s:
            slot_forged_ids.setdefault(s, []).append(tid)

    avoidable: List[Dict[str, Any]] = []
    unassessable = 0
    for t in forged:
        tid = str(t.get("id", "") or "")
        name = str(t.get("name", "") or "")
        s = forged_slot.get(tid, "")
        if not s:
            unassessable += 1
            continue
        binders = slot_nonforged_names.get(s)
        if binders:
            avoidable.append({"tool_id": tid, "name": name, "slots": [s],
                              "would_bind": sorted(x for x in binders if x)})
            continue
        siblings = slot_forged_ids.get(s, [])
        if len(siblings) >= 2 and tid != min(siblings):
            first = min(siblings)
            fb = sorted({str(x.get("name", "")) for x in forged
                         if str(x.get("id", "")) == first and x.get("name")})
            avoidable.append({"tool_id": tid, "name": name, "slots": [s],
                              "would_bind": fb})

    assessable = len(forged) - unassessable
    return {
        "total_forged": len(forged),
        "assessable": assessable,
        "unassessable_no_slot": unassessable,
        "avoidable_count": len(avoidable),
        "rate": (len(avoidable) / assessable) if assessable else 0.0,
        "avoidable": avoidable,
    }


def _empty() -> Dict[str, Any]:
    return {"total_forged": 0, "assessable": 0, "unassessable_no_slot": 0,
            "avoidable_count": 0, "rate": 0.0, "avoidable": []}


def format_avoidable_forge(report: Dict[str, Any]) -> List[str]:
    """Plain-string report lines (for a CLI / debug surface)."""
    r = report or {}
    assessable = int(r.get("assessable", r.get("total_forged", 0)) or 0)
    n = int(r.get("avoidable_count", 0) or 0)
    rate = float(r.get("rate", 0.0) or 0.0)
    unassessable = int(r.get("unassessable_no_slot", 0) or 0)
    lines = [
        f"Avoidable-forge rate: {n}/{assessable} = {rate * 100:.0f}%",
        "  (a forged tool an EXISTING tool would have bound instead of forging — CAP-10;",
        "   deterministic replay, never an LLM judge)",
    ]
    if unassessable:
        lines.append(f"  ({unassessable} forged tool(s) have no derivable slot — "
                     f"unassessable, excluded from the rate)")
    for it in (r.get("avoidable") or []):
        wb = ", ".join(it.get("would_bind") or []) or "?"
        slot = ", ".join(it.get("slots") or []) or "-"
        lines.append(f"  · {it.get('name', '')} [{slot}] — would have bound: {wb}")
    if not (r.get("avoidable")):
        lines.append("  · none — no forged tool duplicates an existing slot.")
    return lines
