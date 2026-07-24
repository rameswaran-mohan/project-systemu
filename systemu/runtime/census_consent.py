"""R-W2 (W-B) — per-category CONSENT for the WM-7 ambient census (spec §5.11.c).

The census reads the OPERATOR'S OWN MACHINE, not systemu's vault. That is a privacy
boundary the rest of the inventory does not cross, so it gets its own consent store —
the ``GrantedRoots`` pattern (durable, revocable, one atomic JSON side-file, defensive
reads), one grant PER CATEGORY.

NO GRANT SURFACE SHIPS — NO OPERATOR CAN TURN THE CENSUS ON (read this first)
----------------------------------------------------------------------------
Nothing in the product calls :meth:`CensusConsentStore.grant`, :func:`consent_card`,
:meth:`set_paused`, or ``ambient_census.revoke_category``. There is no CLI command, no
dashboard control, no registered tool, and no elicitation surface that can create a
grant. So no OPERATOR can consent, and on a fresh install — one where no
``census_consent.json`` has been placed in the vault — ``is_active`` is False for every
category, no probe runs, no census fact is written, and no operator ever sees a card.
This module and :mod:`ambient_census` ship the MACHINERY; the operator surface that
would turn it on is not built.

THAT IS NOT "THE CENSUS NEVER RUNS." :func:`ambient_census.run_census` has a LIVE
production caller — ``shadow_runtime`` invokes it on every survey — and it reads this
consent file DIRECTLY. So the gate is unauthenticated file content, not an unbuilt code
path: any writer that can place a well-formed ``census_consent.json`` in the vault turns
the census on TODAY (see the integrity note on :meth:`_load`). The unbuilt piece is the
OPERATOR-facing grant surface, not the consumer.

What IS pinned — and it is a SOURCE property, not runtime inertness —
``test_no_production_grant_surface_exists`` in ``tests/test_rw2_ambient_census.py`` fails
the moment a shipped file references the grant symbols, and its failure message lists the
disclosures below that must be re-audited first. It cannot pin that no consent file
exists on a given disk; nothing here can. Do not delete that test to make a wiring commit
pass — its whole job is to force the re-audit.

WHAT EACH CATEGORY COLLECTS, EXACTLY
------------------------------------
Every category below declares ``collects`` and ``excludes``. Read those as the
disclosure they are: ``excludes`` names what the probe demonstrably does not reach, and
the ``path_clis`` shape claim is bound to the probe's real output by test
(``test_probe_output_matches_its_card_path_clis``). ``installed_apps`` is the honest
exception — see its note.

  * ``installed_apps``     — the installer-authored DISPLAY NAME, VERBATIM. That string
                             is whatever the vendor wrote, and it very commonly carries
                             a version, a publisher, an edition or a bitness
                             ("Some Runtime 8 Update 241 (64-bit)"). The census does not
                             parse or trim it, so consenting to this category discloses
                             a PATCH-LEVEL SOFTWARE FINGERPRINT of this machine, not a
                             bare product list. It does not read install paths, install
                             dates, sizes or usage counters — only ``DisplayName``.
  * ``path_clis``          — the NAMES of a fixed, published allowlist of developer
                             CLIs that are present on ``PATH`` (e.g. "git", "gh").
                             NAMES ONLY — never the resolved path (which embeds the
                             operator's username), and never an auth state (see the
                             SCOPE note in :mod:`ambient_census`).
  * ``cloud_sync_roots``   — the DIRECTORY PATHS of cloud-sync roots (OneDrive,
                             Dropbox, Google Drive, iCloud). A path is the point of
                             this category, and on Windows it typically embeds the
                             account name — the one place the census records a
                             personally-identifying string, disclosed here.

WHERE IT IS STORED, AND WHERE IT GOES
-------------------------------------
Consent: ``<vault>/census_consent.json`` (this module — sole writer).
Derived facts: the R-W1 durable fact store (see :mod:`ambient_census`, which owns that
side of the boundary — this module holds consent state and nothing else).
Both are local files inside the operator's own vault.

STORAGE IS LOCAL; THE FACTS ARE NOT LOCAL-ONLY. The census itself performs no network
I/O and spawns no subprocess — but that is a statement about the SCANNER, not about the
data. Census facts are the input to ``compose_world_view`` →
``SituationReport.world_facts`` → ``render_situation_for_prompt``, which is the planner
prompt, which is sent to the configured LLM PROVIDER. That path is the entire designed
payoff of the census (§5.11 AC5 clause 3), so it is not incidental: consenting to a
category means the values it finds leave this machine on subsequent runs. They cross
that boundary FENCED, as untrusted data, and clamped to ``content_derived`` — the fence
governs how the model may TREAT them, not whether they are sent.

An earlier revision of this module told the operator "nothing is transmitted". That was
false, and it was false in the permissive direction; the wording above is what the code
actually does.

WHAT BOUNDS IT
--------------
Consent-gated per category (an ungranted category NEVER runs), a per-category entry
cap, a whole-census wall-clock budget, and a minimum re-scan interval — all in
:mod:`ambient_census`. A revoked category stops scanning AND its derived facts are
purged. Zero-census operation is fully functional: the store simply holds fewer facts.

A CREDENTIAL VALUE IS NEVER RECORDED
------------------------------------
Structurally, not by filtering: no probe reads a credential store, a token file, or an
environment variable's VALUE except the cloud-sync location vars whose value IS a
directory path. Pinned by a source-level test.

STANDING SCANS (M3) — the load-bearing consent property
-------------------------------------------------------
A grant is NOT a one-shot snapshot. The census re-runs a consented category on later
runs (and, once WM-13 lands, on the gardener's idle tick), so a single "yes" is an
ONGOING capability. :func:`consent_card` therefore carries ``standing_scan=True`` and
says so in prose, and every category records ``last_ran_at``. The semantics are
DISCLOSED on the original card, never implied.

Of M3's named surface this slice ships the MECHANISM for see / pause / revoke — three
tested library functions — and NO OPERATOR SURFACE for any of them. ``sharing-on world``
renders the "see" half (:func:`ambient_census.census_status`) and is the only census
code an operator can reach; because no operator-created grant can exist, it renders nothing
on a fresh install (a planted consent file would make it render). There is no pause
command and no revoke command. Also not shipped: an explicit "re-run now"
(the min-interval governs re-scans instead) and the PERIODIC "still watching these
categories" notice, which needs a scheduler hook and lands with the WM-13 gardener.

Whoever builds the grant surface MUST build revoke and pause in the same change. A
standing permission to enumerate the operator's machine, with no way to withdraw it, is
not a consent control — and the notice on the card promises those controls to the
operator's face.

This module deliberately does NOT reference the world model: it is pure consent state.
Revoking-and-purging is orchestrated by :func:`ambient_census.revoke_category`, which
is the module the world-model allowlist names.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

#: Serialises the read-modify-write on the consent file (the ``table_store._PROPOSED_LOCK``
#: pattern). Load-bearing, not hygiene — every mutator rewrites the WHOLE file from its own
#: load, so without it ``mark_ran`` (shadow exec thread, after a scan) interleaved with
#: ``revoke`` (operator surface) RESURRECTS the revoked grant: mark_ran loads while the
#: grant exists, revoke removes it, mark_ran then writes its stale snapshot back. That
#: defeats ``ambient_census.run_census``'s pre-write consent re-check, because the re-check
#: would then read a grant that no longer should exist — i.e. it would turn a privacy
#: control into a race. Verified reproducible by hand before the lock was added.
#:
#: Covers concurrent RUNS (threads of one daemon), which is this codebase's stated
#: concurrency model. It does NOT cover a second daemon PROCESS — the same documented
#: exposure every other side-store here carries.
_CONSENT_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value) -> Optional[datetime]:
    """An ISO-8601 timestamp, or None if it is missing/empty/unparseable. Used to decide
    whether a grant row is WELL-FORMED — a row with no parseable ``granted_at`` is not
    evidence of consent."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


class UnknownCensusCategory(ValueError):
    """Raised for a category outside :data:`CATEGORIES`.

    REFUSED, never silently ignored. A typo'd category that "succeeded" and then
    scanned nothing would read to the operator as a granted capability that is quietly
    dead — the exact "accept the input and do something different" failure. A grant
    surface must fail loudly so the typo is visible.
    """


#: The CLOSED category vocabulary. Each entry is the operator-facing disclosure, and
#: :data:`ambient_census.PROBES` must have exactly these keys (pinned by test) — so a
#: probe can never exist without a card, and a card can never exist without a probe.
CATEGORIES: Dict[str, dict] = {
    "installed_apps": {
        "title": "Installed applications",
        # The value stored is the vendor's DisplayName verbatim. Saying "display names"
        # and then excluding "versions"/"publishers" was a real misdisclosure: a
        # DisplayName routinely IS "<product> <version> (<bitness>)", and no filter runs
        # on it. Stating the fingerprint plainly is the correction — stripping versions
        # out of a vendor-authored string would be lossy guesswork that also destroys
        # the planning value ("which Python is installed" is a version question).
        "collects": ["the application's display name exactly as its installer wrote it, "
                     "which usually includes the version, and often the publisher, "
                     "edition or 32/64-bit"],
        "excludes": ["install paths", "install dates", "sizes", "usage data",
                     "anything from inside the application"],
        "why": ("Knowing an application is installed changes planning more than almost "
                "any other fact — it is the difference between planning around a tool "
                "you have and asking you for one."),
        "how": ("Reads the Windows uninstall registry keys (read-only), or lists "
                "/Applications on macOS. No program is launched."),
        # Surfaced as its own field so a renderer that shows only the structured card
        # still conveys it. An itemised software-version list is the classic input to
        # "which known-vulnerable build is this machine running".
        "sensitivity_notice": (
            "Together these amount to a patch-level inventory of the software on this "
            "machine. Combined with the fact that census facts are sent to the model "
            "provider (see `transmission_notice`), treat consenting to this category as "
            "disclosing a software fingerprint, not just a list of app names."),
    },
    "path_clis": {
        "title": "Command-line tools on PATH",
        "collects": ["the names of well-known developer CLIs found on PATH"],
        "excludes": ["the resolved file path (it embeds your username)",
                     "whether the tool is logged in",
                     "any other executable on PATH"],
        "why": ("\"git is available\" lets a plan use it instead of asking you to "
                "install or name it."),
        "how": ("Looks up a fixed, published list of tool names on PATH. Nothing is "
                "executed — presence is decided by the filesystem alone."),
    },
    "cloud_sync_roots": {
        "title": "Cloud-sync folders",
        "collects": ["the folder paths of OneDrive / Dropbox / Google Drive / iCloud"],
        "excludes": ["file names", "file contents", "account identifiers other than "
                     "whatever appears in the folder path itself"],
        "why": ("Knowing where your synced folders are lets a plan find your documents "
                "without you pointing at them."),
        "how": ("Checks a fixed list of location environment variables and well-known "
                "folder names under your home directory. Directory existence only — "
                "nothing inside is read or listed."),
    },
}


def consent_card(category: str) -> dict:
    """The operator-facing consent card for ``category`` (M3).

    NOTHING IN THE PRODUCT CALLS THIS. It is reached only from :meth:`grant`, which has
    no production caller either, so no operator has ever seen a card — see the module
    docstring. That is why correcting the wording here is cheap today and expensive
    later: the wiring commit will be small and will not re-audit prose.

    The STANDING-SCAN and TRANSMISSION disclosures are assembled here, from the same
    :data:`CATEGORIES` entry the probe dispatcher keys on, so a category cannot ship a
    card that omits either. Raises :class:`UnknownCensusCategory` for an unknown
    category — a card that renders for a category with no probe would be a promise
    nothing keeps.
    """
    spec = CATEGORIES.get(str(category or ""))
    if spec is None:
        raise UnknownCensusCategory(
            f"unknown census category {category!r} — known: {', '.join(sorted(CATEGORIES))}")
    return {
        "category": category,
        "title": spec["title"],
        "collects": list(spec["collects"]),
        "excludes": list(spec["excludes"]),
        "why": spec["why"],
        "how": spec["how"],
        # Per-category sensitivity, when the category has one beyond its collects list.
        # Optional by design: an absent notice must render as absent, not as "".
        **({"sensitivity_notice": spec["sensitivity_notice"]}
           if spec.get("sensitivity_notice") else {}),
        # M3: this is an ONGOING capability, stated as a field AND in prose, because a
        # caller that renders only the prose and one that renders only the flag must
        # both convey it.
        "standing_scan": True,
        "standing_scan_notice": (
            "This is a STANDING permission, not a one-time scan: systemu will re-check "
            "this category on later runs, indefinitely, until the grant is revoked. "
            "Revoking also deletes the facts this category produced. Note that this "
            "build ships no command to revoke or pause — see `revocation_notice`."),
        # WHERE IT LIVES vs. WHERE IT GOES — two different questions, and the earlier
        # card answered only the first while implying the second. `stored_at` is now
        # scoped to storage; transmission gets its own flag AND its own prose, the same
        # field+prose pattern `standing_scan` uses and for the same reason.
        "stored_at": "this vault, on this machine",
        "leaves_this_machine": True,
        "transmission_notice": (
            "What this finds is stored on this machine, but NOT ONLY on this machine. "
            "Facts from this category are included in the planning prompt systemu sends "
            "to its model provider on later runs — that is what makes the census useful, "
            "and it means the values it collects leave your computer. They are sent as "
            "clearly-marked untrusted data that the model is told to treat as "
            "description, never as instructions."),
        # The MECHANISM is real, tested, and purges the derived facts. The operator
        # SURFACE is not built (see the module docstring) — so a bare `revocable: True`
        # would be the card's own version of the overclaim this file was held for.
        "revocable": True,
        "revocation_surface_shipped": False,
        "revocation_notice": (
            "Revocation is implemented and it deletes the facts this category produced, "
            "but no command or control in this build calls it yet. Until one ships, this "
            "card must not be shown to an operator as an offer."),
    }


class CensusConsentStore:
    """Durable per-category census consent at ``<base_dir>/census_consent.json``.

    Side-store pattern (atomic write, defensive read): a broken or absent file yields
    NO grants, never an exception. Fail-closed is the safe direction here — the failure
    mode of an unreadable consent file is that the census does not run.
    """

    def __init__(self, base_dir):
        self._base = Path(base_dir)

    @property
    def _file(self) -> Path:
        return self._base / "census_consent.json"

    # ── read ─────────────────────────────────────────────────────────────────
    def _load(self) -> Dict[str, dict]:
        """The grants on disk. NO INTEGRITY CHECK — and the gap is LIVE, not latent.

        This file is plain JSON with no signature, MAC, or provenance. Any writer that
        can place one file in the vault — a hand edit, a restored backup, a granted
        ``local_write`` root that covers the vault, a tool with file-write reach —
        can MANUFACTURE consent for every category, and the census will then treat it as
        the operator's own "yes". Confirmed by construction: the only validation below
        is "is this key a known category and is its row a dict".

        This is exploitable TODAY, not on some future wiring commit. ``run_census`` has a
        live production caller (``shadow_runtime`` runs it every survey) and gates only on
        :meth:`is_active`, whose sole input is this file. So a single forged
        ``census_consent.json`` makes the census scan this machine and route the results
        into the planner prompt (i.e. to the model provider) on the next run — reproduced
        end-to-end during review. The absence of a grant surface constrains only the
        OPERATOR (they have no button to grant); it does not constrain the writer of this
        file at all, and there is nothing to "impersonate" — a forgery MANUFACTURES the
        first yes, it does not mimic an existing one.

        Why an authenticator is still not built here: it is a real design decision — it
        needs a key with somewhere to live and a defined behaviour when the check fails —
        and it belongs with the grant surface, which must decide where operator consent is
        anchored. ``test_no_production_grant_surface_exists`` names this as a precondition
        of that commit. Half-building it now would be the worse outcome: an unkeyed hash
        reads as integrity while providing none. What IS tightened here: :meth:`is_active`
        refuses a row with no parseable ``granted_at`` (see there), so a bare ``{}`` no
        longer reads as consent — that is malformed-input rejection, NOT integrity, and a
        forger who writes a plausible ``granted_at`` still passes.

        What IS true here: reads fail CLOSED. A broken, absent, or unparseable file
        yields NO grants and never an exception, so the failure mode of a damaged
        consent file is that the census does not run."""
        try:
            if not self._file.exists():
                return {}
            data = json.loads(self._file.read_text(encoding="utf-8"))
            grants = data.get("grants") if isinstance(data, dict) else None
            out: Dict[str, dict] = {}
            for cat, row in (grants or {}).items():
                # An unknown category on disk is DROPPED on read, not honoured. A
                # category removed from the build must not keep authorising a scan,
                # and a hand-edited file must not invent one.
                if cat in CATEGORIES and isinstance(row, dict):
                    out[cat] = row
            return out
        except Exception:
            return {}

    def is_granted(self, category: str) -> bool:
        """True iff ``category`` has a durable grant (regardless of pause state)."""
        return str(category or "") in self._load()

    def is_active(self, category: str) -> bool:
        """True iff the category may SCAN right now — a WELL-FORMED grant AND not paused.

        This is the check the census runs. It is deliberately distinct from
        :meth:`is_granted`: pausing must stop scanning without discarding the grant (and
        therefore without purging the facts), which is what makes "pause" different from
        "revoke" on the operator surface.

        "Well-formed" means the row carries a parseable ``granted_at``. A real grant
        always does (:meth:`grant` stamps it), so this changes nothing for genuine
        consent — but it means a bare ``{"grants": {"<cat>": {}}}`` no longer authorises a
        scan. This is malformed-input rejection on the UNMEASURED case: an empty row is
        not evidence of consent, so it must not read as the "scan now" signal. It is NOT
        the integrity check :meth:`_load` documents as absent — a forged row that includes
        a plausible ``granted_at`` still passes, because nothing here authenticates the
        writer.
        """
        row = self._load().get(str(category or ""))
        if row is None or bool(row.get("paused")):
            return False
        return _parse_ts(row.get("granted_at")) is not None

    def last_ran_at(self, category: str) -> Optional[str]:
        """When this category last actually scanned, or None. Powers the "census last
        ran" half of the M3 standing-scan surface."""
        row = self._load().get(str(category or ""))
        return (row or {}).get("last_ran_at") or None

    def list_grants(self) -> List[dict]:
        """Every grant, newest-category-name-sorted, for the operator surface: what is
        granted, when it was granted, when it last ran, and whether it is paused."""
        rows = self._load()
        return [{"category": cat,
                 "title": CATEGORIES[cat]["title"],
                 "granted_at": rows[cat].get("granted_at") or "",
                 "last_ran_at": rows[cat].get("last_ran_at") or "",
                 "paused": bool(rows[cat].get("paused"))}
                for cat in sorted(rows)]

    # ── write ────────────────────────────────────────────────────────────────
    def _write(self, grants: Dict[str, dict]) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "grants": {k: grants[k] for k in sorted(grants)}}
        fd, tmp = tempfile.mkstemp(dir=str(self._base), prefix="census_consent.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, indent=2))
            os.replace(tmp, str(self._file))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def grant(self, category: str) -> dict:
        """Record consent for ``category``. Idempotent (re-granting keeps the original
        ``granted_at`` and clears any pause). Returns the consent card that was agreed
        to, so a caller can log/render exactly what the operator saw.

        Raises :class:`UnknownCensusCategory` for an unknown category — see that class
        for why this refuses rather than no-ops.
        """
        card = consent_card(category)              # raises on unknown — before any write
        with _CONSENT_LOCK:
            grants = self._load()
            row = dict(grants.get(category) or {})
            row.setdefault("granted_at", _now())
            row["paused"] = False
            grants[category] = row
            self._write(grants)
        return card

    def revoke(self, category: str) -> bool:
        """Drop the grant for ``category``. Returns True iff one was present.

        Deliberately does NOT raise on an unknown category: revoke is the SAFE
        direction, and a caller trying to withdraw consent must never be blocked by a
        vocabulary mismatch. (Grant, the direction that authorises a scan, does raise.)

        This removes the CONSENT only. Purging the facts the category produced is
        :func:`ambient_census.revoke_category`'s job — the one entry point that does
        both, and the one the operator surface should call.
        """
        with _CONSENT_LOCK:
            grants = self._load()
            if category not in grants:
                return False
            grants.pop(category, None)
            self._write(grants)
        return True

    def set_paused(self, category: str, paused: bool) -> bool:
        """Pause (or resume) scanning for a granted category — the M3 "pause category"
        surface. Returns True iff the category is granted. Facts are KEPT: a pause is
        not a withdrawal of consent, so purging them would make pause indistinguishable
        from revoke."""
        with _CONSENT_LOCK:
            grants = self._load()
            if category not in grants:
                return False
            row = dict(grants[category])
            row["paused"] = bool(paused)
            grants[category] = row
            self._write(grants)
        return True

    def mark_ran(self, category: str, at: Optional[str] = None) -> bool:
        """Stamp ``last_ran_at`` after a scan. Returns True iff the category is granted.

        Only ever called for a category that just RAN, so a stamp is evidence of a real
        scan rather than of an attempt.

        The membership check and the write MUST stay inside one lock hold: this is the
        mutator that runs on the exec thread while the operator may be revoking, and a
        whole-file write from a stale load is exactly how a revoked grant comes back.
        """
        with _CONSENT_LOCK:
            grants = self._load()
            if category not in grants:
                return False                       # never ADD a row — only stamp one
            row = dict(grants[category])
            row["last_ran_at"] = at or _now()
            grants[category] = row
            self._write(grants)
        return True
