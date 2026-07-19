"""R-B4 — the §5.10.b#4 provenance banner: naming a table item's SOURCE honestly.

§5.10.b#4 requires that the config flow launched from a ``suggested``/
``content_derived`` item show "a provenance banner naming the source" plus a
verify warning. §5.10.b#2's residual says why it is load-bearing rather than
decorative: a proposal reaches ``items.json`` and thence ``declared_intents``,
so the tray is a **cross-run persistence channel for content-derived text**. If
a suggestion can render as though the operator declared it, an injected task's
guess acquires the operator's authority on the way through.

**The honesty rule this module exists to enforce.** Every branch below is driven
by a value the store force-stamps, and each of those stamps has a defined
vocabulary (``ITEM_PROVENANCES``, ``_CANONICAL_ORIGINS``). A row can still carry
something outside them — a future writer, a partial migration, a hand-edited
sidecar, a rollback to a build that knew four provenance values. In that case
this module reports ``determined=False`` and says the source could not be
established. It does NOT fall back to the operator-declared branch.

That direction is deliberate and is the entire point. This project has already
shipped a page that reported a reassuring default where it could not actually
determine the answer, twice. The failing direction for a provenance label is the
flattering one: "you declared this" on a row nobody can vouch for is worse than
no banner at all, because it converts an unknown into a credential. So the
unknown branch is louder than the untrusted branch, not quieter.

Pure data — no nicegui import, no vault read. The caller renders it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from systemu.runtime.table_store import ITEM_PROVENANCES, _CANONICAL_ORIGINS

#: §5.10.b#4 — parameters a table-originated config flow must NEVER pre-fill.
#: An endpoint/URL/hash/root-path supplied by content is the payload of the
#: attack the trust rules exist to stop: pre-filling it means the operator
#: confirms a value they never chose. Matched as substrings against a lowercased
#: param name, so ``base_url``/``server_endpoint``/``root_path`` all catch.
SECURITY_CRITICAL_PARAM_TOKENS = (
    "url", "uri", "endpoint", "host", "origin", "hash", "digest", "sha",
    "root", "path", "dir", "folder", "addr", "port", "server",
)


def must_not_prefill(param_name: str) -> bool:
    """True when §5.10.b#4 forbids pre-filling ``param_name`` from a table item.

    Conservative by construction: an unrecognised name is NOT security-critical
    (pre-filling a plain ``format`` field is fine), but every token above is,
    and the match is a substring so a prefixed/suffixed variant cannot slip past.
    An empty/None name is treated as critical — a field we cannot name is a
    field we cannot clear.
    """
    if not param_name or not isinstance(param_name, str):
        return True
    low = param_name.lower()
    return any(tok in low for tok in SECURITY_CRITICAL_PARAM_TOKENS)


def _get(item: Any, name: str) -> Optional[str]:
    """Read ``name`` off a TableItem or its ``model_dump()`` dict (the §5.10.c
    nicegui boundary rule means the UI often holds the dict form)."""
    if isinstance(item, dict):
        val = item.get(name)
    else:
        val = getattr(item, name, None)
    return val if isinstance(val, str) else None


#: provenance → (source phrase, trusted?). ONLY the five known values appear.
#: There is no ``.get(prov, <operator branch>)`` anywhere in this module — a
#: default argument on that lookup is exactly the flattering fallback the module
#: docstring forbids, so the lookup is guarded by an explicit membership test.
_SOURCE: Dict[str, tuple] = {
    "operator_added": ("you put it on the table", True),
    "consulted": ("you answered it during “Set the table”", True),
    "migrated": ("it was already configured on this install", True),
    "learned": ("systemu learned it from an answer you gave", False),
    "proposed": ("a running task proposed it", False),
}


def provenance_banner(item: Any) -> Dict[str, Any]:
    """The §5.10.b#4 banner for one table item.

    Returns::

        {"determined": bool,   # could the source be established at all?
         "trusted": bool,      # operator-origin AND determined
         "source": str,        # the phrase naming the source
         "headline": str,      # short label for the badge
         "detail": str,        # the sentence shown on the accept→config flow
         "warning": str,       # the verify warning ("" only when trusted)
         "tone": str}          # "ok" | "warn" | "danger"

    ``determined=False`` whenever the provenance or the origin_class is outside
    its known vocabulary. In that case ``trusted`` is False, ``tone`` is
    ``"danger"``, and the text SAYS the source is unknown.
    """
    prov = _get(item, "provenance")
    origin = _get(item, "origin_class")

    # ── the undetermined branch, first and unconditional ──────────────────────
    # Checked BEFORE the known-value lookups so no partially-recognised row (a
    # known provenance carrying an unknown origin, or the reverse) can reach a
    # branch that names a source. Both axes must be known to name anything.
    if prov not in ITEM_PROVENANCES or origin not in _CANONICAL_ORIGINS:
        unknown: List[str] = []
        if prov not in ITEM_PROVENANCES:
            unknown.append("provenance" if prov else "provenance (missing)")
        if origin not in _CANONICAL_ORIGINS:
            unknown.append("origin" if origin else "origin (missing)")
        return {
            "determined": False,
            "trusted": False,
            "source": "unknown",
            "headline": "Source unknown",
            "detail": (
                "systemu cannot establish where this item came from — its "
                + " and ".join(unknown)
                + " is not a value this version recognises. Treat it as "
                "untrusted: it is NOT a record that you declared this."
            ),
            "warning": (
                "Verify every detail against the real service before you "
                "configure anything from it."
            ),
            "tone": "danger",
        }

    source, prov_trusted = _SOURCE[prov]

    # origin_class is the taint axis and it OVERRIDES the provenance's own
    # trust: a `learned` item may legitimately be operator-origin (§5.9 promotes
    # the answer's ORIGINAL origin), and equally a `migrated` row could carry a
    # content_derived origin from whatever seeded it. Trust is the AND of both,
    # so neither axis can vouch for the other.
    trusted = prov_trusted and origin == "operator"

    if origin == "content_derived":
        detail = (
            f"This came from content systemu read — {source}. The text below "
            "originated in a page, file, or tool result, not from you."
        )
        warning = (
            "Check it against the real service before configuring. "
            "Security-critical values (URLs, endpoints, hashes, folder paths) "
            "are never pre-filled from this item — enter them yourself."
        )
        tone = "danger"
    elif origin == "systemu_authored":
        detail = f"systemu generated this itself — {source}."
        warning = "Confirm it matches what you actually use before configuring."
        tone = "warn"
    elif not prov_trusted:
        # operator-origin value, but not an operator DECLARATION — e.g. a
        # `learned` card built from an answer the operator typed. Trusted input,
        # but systemu chose to put it here, so say that rather than implying the
        # operator added it to the table.
        detail = (
            f"systemu put this on your table for you — {source}. The value "
            "itself came from you."
        )
        warning = "Confirm you want it kept before configuring."
        tone = "warn"
    else:
        detail = f"You put this on your table — {source}."
        warning = ""
        tone = "ok"

    return {
        "determined": True,
        "trusted": trusted,
        "source": source,
        "headline": _HEADLINE[prov],
        "detail": detail,
        "warning": warning,
        "tone": tone,
    }


#: Short badge labels. Same closed vocabulary; read only after the membership
#: test above has already passed, so a missing key is impossible by construction.
_HEADLINE = {
    "operator_added": "You added this",
    "consulted": "From your consult",
    "migrated": "Already configured",
    "learned": "Learned from an answer",
    "proposed": "Proposed by a task",
}
