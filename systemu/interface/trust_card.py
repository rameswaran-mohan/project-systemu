"""Phase 2f - the MODEL behind Settings' "What Systemu can touch" card.

WHAT THIS IS
    A read-only summary of the install's reach, assembled for the operator in
    one place. It is a pure function so it can be tested without NiceGUI; the
    Settings renderer (``pages/settings.world_trust_card``) only lays it out.

THE ONE RULE
    EVERY clause consumes an EXISTING mint. Nothing here derives a new fact,
    and nothing approximates one:

      * provider in use  -> ``provider_status.routed_tier_providers`` (the same
        derivation the Settings credentials banner and /welcome step 1 score
        ``unusable_selected`` with);
      * vault root       -> ``vault_root.resolve_vault_root`` (and its fence
        bit, read in the same frame - DEC-32);
      * tools            -> ``vault.list_tools()``, the header both backends
        write, aggregated on the ``enabled`` field only;
      * capture          -> ``platform_profile()["capture_available"]``.

    A clause whose mint cannot answer is OMITTED and recorded in ``omitted``
    with its reason. Silence is the honest output; a guess is not.

WHY THERE IS NO "WHAT LEAVES THIS MACHINE" CLAUSE HERE
    The sentence proposed for it - "prompts and the context you attach go to
    your chosen model provider. Nothing else is sent anywhere." - is FALSE on a
    default install, in both halves, and softening it to make it pass is
    exactly the honesty-wall failure this program exists to stop:

      * ``runtime/web_access.py:326`` - ``read_url`` relays EVERY fetch through
        ``https://r.jina.ai/<url>`` first (``web_reader_backend`` defaults to
        "auto"). r.jina.ai is a third party that receives the target URL and
        returns the page body. It is not a site the tool names, so even the
        proposed qualifier ("tools you approve reach the sites they name") is
        untrue;
      * ``runtime/web_access.py:448`` - ``search_web`` sends the operator's
        QUERY to r.jina.ai before DuckDuckGo ever sees it;
      * ``find_places`` reaches Nominatim plus four Overpass mirrors;
      * ``web_search`` / ``web_read`` / ``web_extract`` / ``find_places`` /
        ``fetch_json`` / ``fetch_html`` / ``download_file`` / ``api_call_get``
        ship SEEDED and ``enabled: true`` (``systemu/vault/tools/index.json``),
        so this is the DEFAULT install, not an opt-in;
      * "your chosen model provider" is also wrong for the common case: a
        ``google/*`` or ``anthropic/*`` tier on an OpenRouter-only box transits
        openrouter.ai (``runtime/privacy.py:60-69`` names the DESTINATION on
        purpose, not the model vendor).

    ``runtime/privacy.privacy_report`` is the shipped mint that already states
    all of this, and ``/privacy`` is the page that renders it. So the card
    carries NO transmission claim and links there instead. Restoring a
    transmission clause needs an operator ruling on the wording, not a patch
    here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Clause ids. Stable, so a test can pin a presence or an absence by name.
CLAUSE_PROVIDER = "provider"
CLAUSE_VAULT_ROOT = "vault_root"
CLAUSE_TOOLS = "tools"
CLAUSE_CAPTURE = "capture"
CLAUSE_TRANSMISSION = "transmission"

TITLE = "What Systemu can touch"

#: The route the operator is sent to for the egress question. It renders
#: ``runtime.privacy.privacy_report()``, which is the mint for that fact.
PRIVACY_ROUTE = "/privacy"

#: Why the transmission clause is not here. Carried in the model (not only in
#: a comment) so the omission is a VALUE a test can assert on.
TRANSMISSION_OMISSION_REASON = (
    "No mint backs a 'nothing else is sent' claim: web_access.read_url and "
    "search_web relay through the third party r.jina.ai, find_places reaches "
    "Nominatim/Overpass, and those tools ship seeded and enabled. See "
    "/privacy, which renders the privacy_report mint."
)


def _ascii(text: Any) -> str:
    """One ASCII spelling of an operator-visible string (DEC-32c)."""
    s = text if type(text) is str else str(text)
    return s.encode("ascii", "backslashreplace").decode("ascii")


def _clause(cid: str, label: str, value: str, detail: str = "",
            **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": cid, "label": _ascii(label),
                           "value": _ascii(value), "detail": _ascii(detail)}
    out.update(extra)
    return out


def _omission(cid: str, reason: str) -> Dict[str, str]:
    return {"id": cid, "reason": _ascii(reason)}


# -- clause: which provider the tiers are really called on -------------------

def provider_clause(config, routed: Optional[List[str]] = None
                    ) -> Optional[Dict[str, Any]]:
    """Which model provider the three tiers will actually be called on.

    ``routed_tier_providers`` is the mint. It is KEY-AWARE and asks the router
    itself, so a ``google/*`` tier on an OpenRouter-only machine reports
    OpenRouter - the party that really receives the text - rather than what the
    model id looks like.

    Deliberately NO reachability witness is spent here. That costs real
    wall-clock on a loopback address and ``settings.provider_credentials_card``
    moved it off the render path on purpose; a second copy on the same page
    would put it straight back. "Selected" and "usable" are different facts and
    the credentials card, four sections up, is the surface for the second one.
    """
    from systemu.runtime import provider_status as _ps

    if routed is None:
        try:
            routed = _ps.routed_tier_providers(config)
        except Exception:
            routed = []

    seen: List[str] = []
    for pid in (routed or ()):
        if type(pid) is not str:
            continue
        key = pid.strip().lower()
        spec = _ps.SPEC_BY_PROVIDER.get(key)
        if spec is None or spec.display in seen:
            continue
        seen.append(spec.display)

    if not seen:
        return None
    return _clause(
        CLAUSE_PROVIDER, "Model provider in use", ", ".join(seen),
        "Your prompts and whatever context you attach are sent here to be "
        "processed. Whether each one is usable right now is the Provider "
        "credentials section above.",
        providers=list(seen),
    )


# -- clause: the operating vault root ----------------------------------------

def vault_root_clause(verdict=None) -> Optional[Dict[str, Any]]:
    """Where this process's vault lives, from the one mint that decides it.

    DEC-32: ``resolve_vault_root`` returns a verdict whose ``refused`` bit
    travels with the path. It is read HERE, in the frame that renders the path,
    so a refused root can never be shown as though it were the working vault.
    """
    from systemu.runtime import vault_root as _vr

    if verdict is None:
        try:
            verdict = _vr.resolve_vault_root()
        except Exception:
            return None

    root = getattr(verdict, "root", None)
    if type(root) is not str or not root:
        return None
    refused = getattr(verdict, "refused", None) is True

    if refused:
        detail = ("REFUSED and NOT IN USE - this path is inside the installed "
                  "systemu package. Run `systemu doctor` for the fix.")
    else:
        detail = ("Everything Systemu keeps - tools, activities, outcomes, "
                  "receipts and credentials - is written under this directory "
                  "on this machine.")
    return _clause(CLAUSE_VAULT_ROOT, "Vault root", root, detail,
                   refused=refused,
                   source=_ascii(getattr(verdict, "source", "")))


# -- clause: how many tools exist, and how many are enabled ------------------

def tools_clause(vault) -> Optional[Dict[str, Any]]:
    """Registered tools, split on the ``enabled`` header field.

    ``list_tools()`` returns the tools index and ``enabled`` is written into
    every header by BOTH backends (``vault._tool_header`` and
    ``storage/sqlite/vault._tool_header``), so this is read off the store, not
    inferred. Only ``True`` counts: a header written before the field existed,
    or one carrying some other value, is UNKNOWN, and unknown is never scored
    as usable (DEC-36 - the concrete type is pinned before the comparison).

    A backend that cannot answer yields no clause at all rather than a zero,
    because "0 tools" and "the store did not answer" are different facts.
    """
    if vault is None:
        return None
    read = getattr(vault, "list_tools", None)
    if not callable(read):
        return None
    try:
        rows = read()
    except Exception:
        return None
    if rows is None:
        rows = []
    try:
        rows = list(rows)
    except Exception:
        return None

    total = len(rows)
    enabled = 0
    for r in rows:
        if type(r) is dict and r.get("enabled") is True:
            enabled += 1
    not_enabled = total - enabled

    return _clause(
        CLAUSE_TOOLS, "Tools registered",
        "{} total - {} enabled, {} not enabled".format(
            total, enabled, not_enabled),
        "An enabled tool can be picked up and run by a Shadow. A tool that is "
        "not enabled cannot run until you enable it on the Tools page.",
        total=total, enabled=enabled, not_enabled=not_enabled,
    )


# -- clause: screen / input capture ------------------------------------------

def capture_clause(profile: Optional[Dict[str, Any]] = None
                   ) -> Optional[Dict[str, Any]]:
    """Can Systemu see the screen and the keyboard on this machine?

    ``platform_profile()["capture_available"]`` is the mint - the same map
    ``doctor`` and /health read. When it is deferred (a container), the
    profile's own ``record_capture`` honesty row supplies the wording, so this
    card cannot word it differently from ``doctor``.

    The default profile is built with ``provider_configured=False`` ONLY to
    decline paying for the keyless-provider witness on a render path; that key
    is never read here, and a False there can withhold a claim, never make one.
    """
    if profile is None:
        try:
            from systemu.runtime.platform_profile import platform_profile
            profile = platform_profile(provider_configured=False)
        except Exception:
            return None
    if type(profile) is not dict:
        return None

    available = profile.get("capture_available")
    if type(available) is not bool:
        return None

    note = ""
    rows = profile.get("host_capabilities")
    if type(rows) is list:
        for row in rows:
            if type(row) is dict and row.get("id") == "record_capture":
                n = row.get("note")
                if type(n) is str:
                    note = n
                break

    if available:
        detail = ("Recording is something you start; nothing is captured until "
                  "you do.")
    else:
        detail = note or "Not available directly on this install."
        if "Host Companion" not in detail:
            detail = detail + " (deferred to the Host Companion)"

    return _clause(CLAUSE_CAPTURE, "Screen / input capture",
                   "Available" if available else "Not available here",
                   detail, available=available)


# -- the model ---------------------------------------------------------------

def trust_card_model(vault, config, *,
                     routed: Optional[List[str]] = None,
                     vault_verdict=None,
                     profile: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    """Assemble the card. Pure, never raises, never approximates a clause.

    Returns ``{"title", "clauses", "omitted", "privacy_route"}``. ``omitted``
    is not decoration: it is how a clause whose mint could not answer stays
    VISIBLY absent instead of quietly becoming a plausible-looking guess.
    """
    clauses: List[Dict[str, Any]] = []
    omitted: List[Dict[str, str]] = []

    builders = (
        (CLAUSE_PROVIDER, lambda: provider_clause(config, routed),
         "no provider could be scored for any tier - the router could not say "
         "which one a tier will be called on"),
        (CLAUSE_VAULT_ROOT, lambda: vault_root_clause(vault_verdict),
         "the vault-root mint did not return a path"),
        (CLAUSE_TOOLS, lambda: tools_clause(vault),
         "the tool store did not answer, so no count would be honest"),
        (CLAUSE_CAPTURE, lambda: capture_clause(profile),
         "the platform profile did not report capture availability"),
    )
    for cid, build, reason in builders:
        try:
            clause = build()
        except Exception:
            clause = None
        if clause is None:
            omitted.append(_omission(cid, reason))
        else:
            clauses.append(clause)

    # Not conditional and not a bug: see the module docstring. The card makes
    # no transmission claim until the wording is ruled against the tree.
    omitted.append(_omission(CLAUSE_TRANSMISSION, TRANSMISSION_OMISSION_REASON))

    return {"title": TITLE, "clauses": clauses, "omitted": omitted,
            "privacy_route": PRIVACY_ROUTE}
