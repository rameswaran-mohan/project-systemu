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


# --------------------------------------------------------------------------- #
# CAP-6b — NAME + DESCRIPTION TOKEN SIMILARITY (the second near-duplicate lens)
#
# WHY A SECOND LENS. ``slots_from_name`` above answers ONE question: "does this
# name canonicalize to a slot some tool already occupies?" It reads the FIRST
# token as the verb and the LAST token as the target, so it is defeated by a
# version suffix (``fetch_json_v2`` -> read:v2), by a trailing qualifier
# (``fetch_json_from_url`` -> read:url), by token order (``json_fetch`` -> no
# verb, no slot) and by any name whose first token is not a known verb. Against
# the shipped 41-tool seed catalog that left 23 tools with NO slot at all, which
# means no proposal could ever be flagged as their duplicate.
#
# This lens answers a different question: "do these two names DESCRIBE the same
# thing?" It is a set comparison over normalized tokens, so token ORDER, a
# version suffix and a verb synonym all fall out. It does not replace the slot
# lens (which catches ``open_issue`` vs ``create_issue``, where the objects match
# but the surface words do not) — the two are UNIONED at the advisory.
#
# Pure functions over strings — no I/O, no state, replay-stable (CAP-8/IMPL-15),
# and NEVER an admission gate: everything here feeds an advisory line (CAP-6).
#
# The verb classes below are deliberately COARSER than ``_VERB_CANON``'s slot
# vocabulary and deliberately SEPARATE from it. A slot verb is a governance fact
# ("this is a create") that other code keys off; a similarity class is only a
# fuzzy-match aid. Folding ``take``/``capture`` into the same class as ``read``
# is right for "is this the same tool?" and wrong for "what does this tool do to
# the world", so the two maps must be free to disagree.
# --------------------------------------------------------------------------- #

# A camel/snake/digit splitter: "FetchJSON2" -> Fetch, JSON, 2.
_CAMEL_SPLIT = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")

# Tokens that mark a REVISION of a thing rather than part of what it is.
_VERSION_WORDS = frozenset({"new", "copy", "alt"})
_VERSION_RE = re.compile(r"^(?:v\d+|mk\d+|\d+)$")
# The camel/digit splitter cuts "v2" into ("v", "2"), so once a trailing digit
# is dropped the orphaned marker has to go with it — otherwise "fetch_json_v2"
# normalizes to "fetch json v" and matches nothing.
_VERSION_MARKERS = frozenset({"v", "mk", "ver", "rev", "version", "revision"})

# Grammatical glue that carries no capability meaning.
_CONNECTORS = frozenset({"from", "to", "via", "the", "a", "an", "of", "for",
                         "with", "and"})

_SIM_VERB_SYNONYMS = {
    "read":  ("fetch", "get", "read", "download", "retrieve", "load", "pull",
              "take", "capture", "grab", "snap"),
    "write": ("write", "save", "store", "put", "create", "make", "emit"),
    "run":   ("run", "execute", "launch", "open", "start"),
    "parse": ("parse", "extract", "decode"),
    "find":  ("list", "enumerate", "find", "search", "query"),
}
#: The canonical similarity verb classes, in the order a reason string lists them.
SIMILARITY_VERB_CLASSES = ("read", "write", "run", "parse", "find")
_SIM_VERB_CANON = {syn: canon
                   for canon, syns in _SIM_VERB_SYNONYMS.items()
                   for syn in syns}


def _strip_mcp_prefix(raw: str) -> str:
    if raw.startswith("mcp__") and "__" in raw[5:]:
        return raw.rsplit("__", 1)[-1]
    return raw


def raw_similarity_tokens(text: str) -> list:
    """Split ``text`` into lowercase word/number tokens on ``_``, punctuation,
    whitespace AND camelCase boundaries. No folding yet — this is the raw split
    the version-suffix and synonym passes work on."""
    toks: list = []
    for chunk in _NON_WORD.split(_strip_mcp_prefix((text or "").strip())):
        toks.extend(t.lower() for t in _CAMEL_SPLIT.findall(chunk))
    return toks


def strip_version_suffix(tokens: list) -> list:
    """Drop a TRAILING run of version-ish tokens (``fetch_json_v2`` ->
    ``fetch_json``; ``FetchJSON2`` -> ``fetch json``).

    TRAILING-ONLY, and only while at least two tokens survive — because a
    version word is not always a version. The shipped catalog contains
    ``file_copy``, where ``copy`` is the VERB; stripping it positionally-blind
    would collapse that tool to the single token ``file`` and make it a false
    neighbour of every file tool there is. A version marker is a SUFFIX, so
    that is where it is looked for.
    """
    out = list(tokens)
    while len(out) > 2:
        last = out[-1]
        if not (_VERSION_RE.match(last) or last in _VERSION_WORDS):
            break
        out.pop()
        # A bare marker is only a marker when it introduced the digit just
        # dropped: "get_git_rev" keeps its ``rev``, "fetch_json_v2" loses its ``v``.
        if last.isdigit() and len(out) > 2 and out[-1] in _VERSION_MARKERS:
            out.pop()
    return out


def _depluralize(word: str) -> str:
    """Cheap deterministic plural fold, so ``files``/``file`` are one token."""
    w = word
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def canonical_similarity_token(token: str) -> str:
    """Fold ONE token: a verb synonym becomes its similarity class, anything else
    is kept as-is (depluralized). Object words are never invented or renamed."""
    t = _norm_token(token)
    folded = _depluralize(t)
    return _SIM_VERB_CANON.get(t) or _SIM_VERB_CANON.get(folded) or folded


def similarity_tokens(text: str) -> set:
    """The normalized token SET for a tool name or description: camel/snake split,
    trailing version tokens dropped, connectors dropped, verb synonyms folded to a
    class, object tokens depluralized and otherwise kept verbatim."""
    out = set()
    for tok in strip_version_suffix(raw_similarity_tokens(text)):
        if tok in _CONNECTORS:
            continue
        folded = canonical_similarity_token(tok)
        if folded:
            out.add(folded)
    return out


def stripped_name_key(name: str) -> str:
    """The name with only its version suffix removed (``fetch_json_v2`` ->
    ``fetch_json``). Two names sharing this key are the SAME name modulo a
    revision marker — the strongest possible near-duplicate signal, and the one
    that keeps a tool's own re-proposal at the top of its candidate list."""
    return "_".join(strip_version_suffix(raw_similarity_tokens(name)))


def is_object_token(token: str) -> bool:
    """True for a token that names a THING (``json``, ``file``, ``screenshot``)
    rather than a folded verb class. Two tools sharing only a verb class
    (``read``) are not near-duplicates — ``fetch_html`` and ``clipboard_read``
    both 'read' — so an overlap with no object token is no evidence at all."""
    return token not in SIMILARITY_VERB_CLASSES


def token_jaccard(left: set, right: set) -> float:
    """|A n B| / |A u B| — 0.0 when either side is empty (never a divide-by-zero,
    never a vacuous 1.0 for two empty sets)."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def order_shared_tokens(tokens) -> list:
    """The shared tokens in reason-string order: verb classes first (in the fixed
    ``SIMILARITY_VERB_CLASSES`` order), then object tokens alphabetically. Total
    and deterministic, so an advisory line is replay-stable."""
    toks = set(tokens or ())
    verbs = [v for v in SIMILARITY_VERB_CLASSES if v in toks]
    objects = sorted(t for t in toks if is_object_token(t))
    return verbs + objects
