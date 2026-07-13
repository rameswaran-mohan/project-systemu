"""R-CAP1 · CAP-1 — the capability-slot canonicalizer (spec §5.5.1).

A `CapabilitySlot = (verb, target_class)`. The LLM may propose new slots freely
(open vocabulary — Callout 2); this module NORMALIZES a proposal to a canonical
`(verb, target)` **before** the CAP-5 occupancy check, so synonymous proposals
("create issue" / "open ticket") collapse to ONE slot instead of fragmenting the
index and defeating the pre-forge gate (the 4-lens CAP-1 fragmentation fix).

Pure data + pure functions — NO I/O, NO state — so the whole selection layer is
replay-stable (CAP-8 / IMPL-15). This is a PURPOSE-BUILT slot-synonym map: the
*shape* mirrors ``reference_synonyms.py``'s lookup, NOT its content (that file is
a file-extension hint table — the 4-lens wrong-pattern-citation fix).
"""
from __future__ import annotations

import re
from typing import Tuple

# verb synonym → canonical action verb. Kept small + auditable; an unknown verb
# passes through folded (admitted, never fenced — Callout 2).
_VERB_CANON = {
    "create": "create", "make": "create", "open": "create", "add": "create",
    "new": "create", "forge": "create", "generate": "create", "file": "create",
    "send": "send", "post": "send", "submit": "send", "publish": "send",
    "push": "send", "upload": "send", "email": "send", "notify": "send",
    "read": "read", "get": "read", "fetch": "read", "load": "read",
    "download": "read", "view": "read", "show": "read",
    "update": "update", "edit": "update", "modify": "update", "change": "update",
    "patch": "update", "set": "update", "rename": "update",
    "delete": "delete", "remove": "delete", "drop": "delete", "clear": "delete",
    "list": "list", "search": "list", "find": "list", "query": "list",
    "enumerate": "list", "browse": "list",
    "run": "run", "execute": "run", "invoke": "run", "call": "run",
}

_WORD = re.compile(r"[a-z0-9]+")


def _norm_token(tok: str) -> str:
    return (tok or "").strip().lower()


def canonical_verb(verb: str) -> str:
    """Fold a verb synonym to its canonical action (case-insensitive). An unknown
    verb is admitted as its own lowercased form (open vocabulary, never fenced)."""
    v = _norm_token(verb)
    return _VERB_CANON.get(v, v)


def _singular(word: str) -> str:
    """Cheap deterministic singularizer for target classes (no external deps)."""
    w = word
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"          # policies -> policy
    if len(w) > 2 and w.endswith("ses"):
        return w[:-2]                # addresses -> address
    if len(w) > 1 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]                # issues -> issue ; keep "address"
    return w


def canonical_target(target: str) -> str:
    """Lowercase + singularize a target class (case/plural fold)."""
    return _singular(_norm_token(target))


def canonical_slot(verb: str, target: str) -> Tuple[str, str]:
    """Normalize a proposed (verb, target) to its canonical slot."""
    return (canonical_verb(verb), canonical_target(target))


def slot_str(slot: Tuple[str, str]) -> str:
    """The stable string form ``verb:target`` used as an index key."""
    return f"{slot[0]}:{slot[1]}"


def slots_from_name(name: str) -> list:
    """Derive canonical slots from a tool/connector NAME (the pre-CAP-3 heuristic:
    first token = verb, remaining meaningful tokens = target). ``create_issue`` →
    [("create","issue")]; ``mcp__gh__create_issue`` strips the mcp prefix. A name
    with no recognizable verb yields no slot (rather than a garbage one)."""
    raw = (name or "").lower()
    # strip an mcp prefix generically: mcp__<server>__<toolname> → <toolname>
    # (rsplit on the "__" delimiter, so a server of ANY token count is dropped —
    # not the old toks[3:] heuristic that assumed a 2-token server and ate the verb).
    if raw.startswith("mcp__") and "__" in raw[5:]:
        raw = raw.rsplit("__", 1)[-1]
    toks = _WORD.findall(raw)
    if not toks:
        return []
    verb = canonical_verb(toks[0])
    if verb == toks[0] and toks[0] not in _VERB_CANON:
        # first token isn't a known verb — no confident slot (avoid fragmentation)
        return []
    target_toks = [t for t in toks[1:]
                   if t not in ("a", "an", "the", "to", "my") and not t.isdigit()]
    if not target_toks:
        return [(verb, "")]
    target = canonical_target(target_toks[-1])          # the head noun
    return [(verb, target)]
