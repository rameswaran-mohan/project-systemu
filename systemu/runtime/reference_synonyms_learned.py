# systemu/runtime/reference_synonyms_learned.py
"""R-A16 G-LEARN slice 4 (§5.9) — the LEARNED half of the phrase-class → extension map.

§5.9's Update clause asks the learning loop to "extend the synonym map
(``reference_synonyms.py``)". That module is a PURE static dict by design — no I/O,
no state, a readable constant — so it is NOT extended in place. The learned half
lives here as a capped, deduped, vault-backed OVERLAY, consulted through the merged
accessor :func:`merged_synonym_exts`. The static map stays the readable constant it
was; nothing here writes to it.

WHY THIS HALF OF SLICE 4 WAS BUILT (and the threshold half was not — see
``docs`` note in ``tests/test_glearn_s4_threshold_evidence.py``).
``reference_resolver._score`` carries a RELEVANCE GATE::

    if not matched and not ext_match:
        return 0.0

so a synonym-ext hint can be the ONLY thing that qualifies a candidate at all. A
learned token therefore flips a ``missing`` verdict — a BLANK "type the path" ask —
into ``resolvable``, a PRE-FILLED one-click confirm. That is precisely the
missing-answered → resolvable conversion the §5.9 metric already reports, so the
payoff is measurable by the report shipped in slice 2.

WHAT IT CAN NEVER DO. It cannot weaken the silent-bind invariant.
``requirement_binder._bind_filehandle`` clamps a resolved file to ``content_derived``
regardless of score, and ``_needs_ask`` surfaces a ``content_derived`` bind at ANY
confidence. So the overlay only ever upgrades a blank ask to a pre-filled confirm; an
untrusted file value still can never bind silently. This is why the DIRECTIONAL
``missing_answered`` sub-case is a legitimate driver HERE while it must not drive a
threshold: widening candidate SCORING is not a security decision, whereas moving the
confirm line is.

TOKENIZATION IS NOT RE-IMPLEMENTED. ``_tokens``/``_STOP`` are imported from
``reference_resolver`` — the consumer — so a learned key is by construction one the
resolver can actually look up. A second tokenizer here would silently produce dead
keys the moment the resolver's split regex changed.

BOUNDED + AUDITABLE (§5.9). Capped per vault (:data:`MAX_TOKENS`) and per token
(:data:`MAX_EXTS_PER_TOKEN`), deduped, and every REFUSAL is logged at INFO — a
fail-closed refusal is an audit signal, not a non-event (the S3 rule). A non-event
(wrong class, no extension, no answer) logs at DEBUG instead: logging those would
drown the real signal on any ordinary card.

NEVER RAISES. This is an observability/tuning path: a hiccup must never affect the
run that made the ask. Every public entry degrades to the static-only answer.

DOCUMENTED RESIDUAL (accepted, not overlooked): the write is read-modify-write, so
two learns racing ACROSS PROCESSES can lose one. It cannot corrupt the store — the
write is atomic (``os.replace``) and the reader re-validates every entry — and the
loss is self-healing, since the next answered ask for the same leaf re-learns it. The
in-process writer is the daemon reconciler tick, which handles paths sequentially, so
the race needs two daemons on one vault. A lock was judged not worth adding to a
never-affects-run tuning path whose worst case is one deferred synonym; the sibling
corpus writer (``replay_metrics._append_line``) does lock, because losing an
observation there corrupts a SHIPPED metric.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Set

from systemu.runtime.reference_resolver import _STOP, _tokens
from systemu.runtime.reference_synonyms import synonym_exts

logger = logging.getLogger(__name__)

#: Total learned tokens per vault. The overlay is consulted per reference token on
#: every file bind, so it stays small enough to read and reason about.
MAX_TOKENS = 64
#: Extensions per learned token. A token that implies five different file types is
#: not a phrase-class hint any more, it is noise.
MAX_EXTS_PER_TOKEN = 4

_MIN_TOKEN_LEN = 3
_MAX_TOKEN_LEN = 24
#: The shape ``os.path.splitext`` yields, lower-cased. ``build_roots`` emits ``ext``
#: with the file's ORIGINAL case and ``reference_resolver`` lower-cases at compare
#: time, so the overlay must store lower-case or a learned ``.XLSX`` never matches.
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,7}$")

#: Structural leaf words that name the SLOT rather than the artifact. Learning one
#: would attach an extension to nearly every path leaf in every schema, flooding the
#: candidate set and destroying the resolver's precision — the opposite of the
#: payoff. ``_tokens`` already drops ``reference_resolver._STOP``; this is the
#: schema-vocabulary complement of it.
_GENERIC: FrozenSet[str] = frozenset({
    "path", "filename", "filepath", "name", "dir", "directory", "folder",
    "output", "input", "source", "src", "dest", "destination", "target",
    "location", "uri", "url", "arg", "param", "value", "key", "id",
})

_STORE_NAME = "learned_synonyms.json"


# ── the store (side file; the table_store atomic-write pattern) ──────────────
def _learned_path(vault) -> Path:
    return Path(vault.root) / "audit" / _STORE_NAME


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def load_learned(vault) -> Dict[str, FrozenSet[str]]:
    """The persisted overlay, ``token → frozenset[ext]``.

    Defensive on EVERY axis: an absent/corrupt file, a non-dict payload, a non-list
    value, a non-str or mis-shaped entry are all skipped rather than raised on. This
    file is plain JSON in the vault and may be hand-edited, so it is treated as
    untrusted input and re-validated on read, not merely on write.
    """
    try:
        path = _learned_path(vault)
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, FrozenSet[str]] = {}
        for token, exts in raw.items():
            if not isinstance(token, str) or not token.strip():
                continue
            if not isinstance(exts, list):
                continue
            good = {e.lower() for e in exts
                    if isinstance(e, str) and _EXT_RE.match(e.lower())}
            if not good:
                continue
            out[token.strip().lower()] = frozenset(sorted(good)[:MAX_EXTS_PER_TOKEN])
            if len(out) >= MAX_TOKENS:
                break
        return out
    except Exception:
        logger.debug("[S4] learned-synonym store unreadable; degrading to static-only",
                     exc_info=True)
        return {}


def _save_learned(vault, data: Dict[str, List[str]]) -> bool:
    try:
        payload = {t: sorted(set(e)) for t, e in data.items()}
        _write_atomic(_learned_path(vault), json.dumps(payload, indent=2, sort_keys=True))
        return True
    except Exception:
        logger.debug("[S4] could not persist the learned-synonym store", exc_info=True)
        return False


# ── the MERGED accessor — the only thing consumers should call ───────────────
def merged_synonym_exts(token, vault=None) -> FrozenSet[str]:
    """Extensions implied by ``token``: the STATIC map unioned with the learned overlay.

    UNION, never override: a learned entry can only ever WIDEN the candidate set, so
    the shipped vocabulary in ``reference_synonyms`` cannot be shadowed or subtracted
    by anything on disk. ``vault=None`` returns the static answer byte-for-byte, so a
    caller that threads no vault is unaffected by this slice.
    """
    static = synonym_exts(token)
    if vault is None or not isinstance(token, str) or not token.strip():
        return static
    try:
        return static | load_learned(vault).get(token.strip().lower(), frozenset())
    except Exception:
        logger.debug("[S4] learned overlay lookup failed; static-only", exc_info=True)
        return static


def merged_exts_for_tokens(tokens, vault=None) -> FrozenSet[str]:
    """The union of :func:`merged_synonym_exts` over MANY tokens, reading the store
    ONCE.

    This is what ``reference_resolver`` calls. Looping ``merged_synonym_exts`` there
    instead would re-read the JSON file per reference TOKEN, and the resolver runs per
    path LEAF — so a multi-leaf schema would do dozens of file reads for a single
    bind. Same answer, one read.
    """
    out: FrozenSet[str] = frozenset()
    toks = [t for t in (tokens or []) if isinstance(t, str) and t.strip()]
    for t in toks:
        out |= synonym_exts(t)
    if vault is None:
        return out
    try:
        learned = load_learned(vault)
    except Exception:
        logger.debug("[S4] learned overlay unavailable; static-only", exc_info=True)
        return out
    for t in toks:
        out |= learned.get(t.strip().lower(), frozenset())
    return out


# ── the learning rule — deterministic, no LLM judge ──────────────────────────
def _learnable_tokens(leaf: str) -> Set[str]:
    """Candidate phrase-class tokens in a schema leaf.

    Uses the RESOLVER's tokenizer, then narrows — so the result is always a subset of
    what the resolver will look up (pinned in the tests). Narrowing drops: structural
    slot words (:data:`_GENERIC`), very short/long tokens, non-alpha-initial tokens,
    and anything the STATIC map already covers (re-learning it would spend the cap on
    a lookup that already succeeds).
    """
    out: Set[str] = set()
    try:
        for t in _tokens(leaf):
            if t in _GENERIC or t in _STOP:
                continue
            if not (_MIN_TOKEN_LEN <= len(t) <= _MAX_TOKEN_LEN):
                continue
            if not t[0].isalpha():
                continue
            if synonym_exts(t):
                continue
            out.add(t)
    except Exception:
        logger.debug("[S4] tokenization failed", exc_info=True)
        return set()
    return out


def learn_from_answer(vault, *, schema_path, klass, answer) -> bool:
    """Learn ``phrase-token → extension`` from ONE answered file-reference ask.

    Returns True only when the store actually changed. Never raises.

    Deterministic and narrow by construction — no LLM judge is involved:
    the extension comes from ``os.path.splitext`` of the operator's own answer, and
    the tokens come from the schema leaf via the resolver's tokenizer.

    THE SECRET FENCE IS BOTH-LEVEL, and reuses the shipped mechanisms rather than
    inventing a third. ``_is_secret`` fences the field NAME; ``_value_is_secret``
    fences the VALUE — S3 established that a secret can hide under a perfectly
    innocuous leaf name, and the value is inspected here (to read its extension), so
    the value-level check is mandatory, not belt-and-braces. Refusals name the PATH
    only, never the value or a hint of its shape.
    """
    refused: List[str] = []
    try:
        path = str(schema_path or "").strip()
        ans = "" if answer is None else str(answer).strip()
        # ── non-events: not a file-reference ask at all. DEBUG, not INFO — logging
        #    these at INFO would drown the real refusals on any ordinary card.
        if not path or str(klass or "").strip().lower() != "input" or not ans:
            logger.debug("[S4] synonym learn skipped (non-event) path=%r class=%r",
                         path, klass)
            return False

        from systemu.runtime.ask_promotion import _is_secret, _leaf_of, _value_is_secret

        # ── the two secret refusals, both LOGGED (audit signal, not a non-event) ──
        if _is_secret(path, klass):
            refused.append(f"{path} (secret-mode field)")
            return False
        if _value_is_secret(ans):
            # the PATH only — never the value, never a hint of its shape
            refused.append(f"{path} (answer looks like a credential)")
            return False

        ext = os.path.splitext(ans)[1].lower()
        if not _EXT_RE.match(ext):
            logger.debug("[S4] synonym learn skipped: answer has no usable extension")
            return False

        leaf = _leaf_of(path)
        stem = os.path.splitext(os.path.basename(ans))[0]
        # A token that ALREADY matches the answer's filename needs no synonym: the
        # resolver's name-overlap term scores it without help, so learning it would
        # spend the cap on a lookup that already succeeds.
        tokens = _learnable_tokens(leaf) - _tokens(stem)
        if not tokens:
            logger.debug("[S4] synonym learn skipped: no learnable token in %r", leaf)
            return False

        current = load_learned(vault)
        data: Dict[str, List[str]] = {t: sorted(e) for t, e in current.items()}
        changed = False
        for tok in sorted(tokens):
            exts = data.get(tok)
            if exts is None:
                if len(data) >= MAX_TOKENS:
                    refused.append(f"{tok} (token cap {MAX_TOKENS})")
                    continue
                data[tok] = [ext]
                changed = True
                continue
            if ext in exts:
                continue                      # dedupe — already known
            if len(exts) >= MAX_EXTS_PER_TOKEN:
                refused.append(f"{tok} (ext cap {MAX_EXTS_PER_TOKEN})")
                continue
            data[tok] = sorted(set(exts) | {ext})
            changed = True

        if not changed:
            return False
        if not _save_learned(vault, data):
            return False
        logger.info("[S4] learned synonym(s) %s -> %s from %s",
                    ",".join(sorted(tokens)), ext, path)
        return True
    except Exception:
        logger.debug("[S4] synonym learning skipped (non-fatal)", exc_info=True)
        return False
    finally:
        if refused:
            # §5.9 "bounded + auditable": what was WITHHELD is logged, never silently
            # dropped — a fail-closed refusal is a signal, not a non-event.
            logger.info("[S4] %d synonym learn(s) capped/refused: %s",
                        len(refused), "; ".join(refused[:12]))


def learned_synonym_report(vault) -> Dict[str, Any]:
    """The overlay, summarised for the §5.9 report. Never raises.

    An invisible learned map is a debugging trap: a resolver verdict that depends on
    accreted state nobody can see is unexplainable. This is what makes it visible.
    """
    try:
        learned = load_learned(vault)
        return {
            "tokens": len(learned),
            "cap": MAX_TOKENS,
            "entries": {t: sorted(e) for t, e in sorted(learned.items())},
        }
    except Exception:
        return {"tokens": 0, "cap": MAX_TOKENS, "entries": {}}
