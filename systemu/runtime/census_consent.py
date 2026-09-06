"""R-W2 (W-B) — per-category CONSENT for the WM-7 ambient census (spec §5.11.c).

The census reads the OPERATOR'S OWN MACHINE, not systemu's vault. That is a privacy
boundary the rest of the inventory does not cross, so it gets its own consent store —
the ``GrantedRoots`` pattern (durable, revocable, one atomic JSON side-file, defensive
reads), one grant PER CATEGORY.

WHAT AN OPERATOR CAN ACTUALLY REACH (read this first)
-----------------------------------------------------
ONE category is reachable from this build's operator surface: ``cloud_sync_roots``, via
``systemu census status | grant | revoke | pause | resume`` (see
:data:`SURFACED_CATEGORIES`). All three verbs shipped together, deliberately — a standing
permission to enumerate the operator's machine with no way to withdraw it is not a
consent control, and the card promises those controls to the operator's face.

``installed_apps`` and ``path_clis`` are NOT reachable: the CLI refuses every verb for
them and says so, :func:`consent_card` reports ``revocation_surface_shipped: False`` for
them, and no dashboard control, registered tool or elicitation surface can create a grant
for any category. On a fresh install — no ``census_consent.json`` in the vault —
``is_active`` is False for every category, no probe runs, no census fact is written, and
the operator sees a card only if they run ``census grant`` themselves.

:func:`ambient_census.run_census` has a LIVE production caller — ``shadow_runtime``
invokes it on every survey — and it reads this consent file DIRECTLY. The consent file IS
AUTHENTICATED (:meth:`_load`): it carries an HMAC-SHA256 keyed by a secret derived from
this vault, so placing a well-formed ``census_consent.json`` in the vault does not
manufacture a grant — forging one needs the vault's own secret. An unsigned file (the
``version: 1`` format that shipped before the authenticator) reads as UNCONSENTED and is
never grandfathered.

What IS pinned — a SOURCE property, not runtime inertness —
``test_the_census_grant_surface_is_confined_to_its_declared_region`` in
``tests/test_rw2_ambient_census.py`` fails the moment a shipped file OUTSIDE the two
declared surface files (or outside their declared region within those files) references
the grant symbols, and its failure message lists the disclosures a widening must re-audit
first. It cannot pin what is on a given disk; nothing here can. Do not delete that test to
make a widening commit pass — its whole job is to force the re-audit.

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

M3's named surface — see / pause / revoke — is now OPERATOR-REACHABLE for
:data:`SURFACED_CATEGORIES`: ``systemu census status`` (see, all categories),
``census pause`` / ``census resume`` and ``census revoke``. ``systemu world`` renders the
"see" half too (:func:`ambient_census.census_status`) and shows nothing on a fresh
install, because no grant exists until the operator creates one. Still NOT shipped: an
explicit "re-run now" (the min-interval governs re-scans instead) and the PERIODIC "still
watching these categories" notice, which needs a scheduler hook and lands with the WM-13
gardener — so the standing-scan reminder remains pull-only (the operator must run
``census status``), which is why the card states the standing semantics so plainly.

Whoever adds a category to the grant surface MUST add revoke and pause for it in the same
change. A standing permission to enumerate the operator's machine, with no way to withdraw
it, is not a consent control — and the notice on the card promises those controls to the
operator's face.

This module deliberately does NOT reference the world model: it is pure consent state.
Revoking-and-purging is orchestrated by :func:`ambient_census.revoke_category`, which
is the module the world-model allowlist names.
"""
from __future__ import annotations

import hashlib
import hmac
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

#: The ONLY on-disk format that can carry consent. Version 1 is the UNSIGNED format that
#: shipped before the authenticator; it reads as UNCONSENTED and is never upgraded in
#: place (see :meth:`CensusConsentStore._load`).
CONSENT_FORMAT_VERSION = 2

#: HKDF-style domain separation. The KEY info string keeps this MAC key distinct from
#: every other key derived from the same per-vault secret (dashboard sessions, the
#: ask-corpus refs), and the MAC prefix keeps a consent signature from ever being
#: replayable as some other message signed with this same key.
_CONSENT_KEY_INFO = b"systemu/census-consent/key/v1"
_CONSENT_MAC_PREFIX = b"systemu/census-consent/mac/v1\x00"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _consent_key(base_dir) -> bytes:
    """The per-vault HMAC key for ``census_consent.json``.

    NOT a new secret scheme. It is DERIVED, by HMAC domain separation, from the per-vault
    secret this codebase already generates and persists — ``dashboard_auth.session_secret``
    (64 random hex chars, stored through the S5 at-rest envelope, get-or-create, per
    vault) — exactly as ``replay_metrics._ref_key`` derives the ask-corpus key. Reusing
    that derivation means ONE place decides how a vault secret is generated, persisted and
    protected, rather than three.

    PER-VAULT is load-bearing, not incidental: a consent file signed for one vault must
    not authorise a scan in another, because copying a genuinely-signed file is the
    forgery a global key would not stop.

    RAISES when no usable secret can be obtained. Every caller treats that as fail-closed:
    :meth:`CensusConsentStore._load` turns it into "no grants" (nothing scans) and
    :meth:`CensusConsentStore._write` refuses to write a file it cannot sign, because an
    unsignable file would read back as UNCONSENTED and silently discard the operator's
    answer.

    Deliberately NOT cached. The cache in ``replay_metrics`` exists because that key is
    derived on a hot per-ask path; this one is derived at most a handful of times per
    census run, and a cache would keep honouring a key after the vault secret it came
    from was rotated or removed — the wrong direction for a privacy gate.
    """
    from systemu.runtime import dashboard_auth
    seed = dashboard_auth.session_secret(str(base_dir))
    # `type(x) is str`, not isinstance/truthiness: the terminating type check (DEC-36).
    # A stand-in object with a __len__ and an __eq__ must not reach `encode`.
    if type(seed) is not str or len(seed) < 32:
        raise ValueError("no usable per-vault secret for the census consent MAC")
    return hmac.new(seed.encode("utf-8"), _CONSENT_KEY_INFO, hashlib.sha256).digest()


def _canonical_body(version: int, grants) -> bytes:
    """The exact bytes the MAC covers: ``{"version": N, "grants": {...}}``, sorted keys,
    no whitespace, ASCII-escaped.

    Canonical rather than "the file's bytes" so that indentation, key order and unicode
    escaping are not part of the signature — the SIGNED THING is the decoded meaning, so
    a re-serialisation with the same meaning verifies and a changed meaning does not.
    ``ensure_ascii=True`` makes the output pure ASCII, so the encode below cannot raise on
    a non-BMP display name that reached a grant row.
    """
    return json.dumps({"version": int(version), "grants": grants},
                      sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _consent_mac(key: bytes, version: int, grants) -> str:
    """HMAC-SHA256 over the prefixed canonical body, as lowercase hex."""
    return hmac.new(key, _CONSENT_MAC_PREFIX + _canonical_body(version, grants),
                    hashlib.sha256).hexdigest()


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


#: The categories an OPERATOR can actually reach from this build's surface
#: (``systemu census grant|revoke|pause|resume``). A subset of :data:`CATEGORIES` on
#: purpose: P4-B2 ships ONE category end to end — ``cloud_sync_roots``, the lowest
#: privacy surface of the three — so the whole path (card wording, confirm, signed
#: grant, real probe, real revocation) can be reviewed at once instead of three times
#: badly.
#:
#: THIS IS THE SINGLE SOURCE OF TRUTH for that scope. :func:`consent_card` derives
#: ``revocation_surface_shipped`` and its revocation prose from it, and the CLI refuses
#: every verb for a category outside it, so the card can never claim a control the
#: command does not offer — nor deny one it does.
#:
#: All four verbs are restricted to this set, not just ``grant``. There is nothing
#: legitimate to revoke for a category no surface can grant: the ONLY writer of a valid
#: grant row is this store, reached from the CLI, and since the consent file is
#: authenticated a planted file cannot create one either. Adding a category means
#: shipping its grant AND its revoke AND its pause together, and re-reading the
#: disclosures the surface guard in ``tests/test_rw2_ambient_census.py`` enumerates.
SURFACED_CATEGORIES: tuple = ("cloud_sync_roots",)


def consent_card(category: str) -> dict:
    """The operator-facing consent card for ``category`` (M3).

    RENDERED BY ``systemu census grant`` for a category in :data:`SURFACED_CATEGORIES`;
    for the others it is still reachable only from tests, since the CLI refuses to grant
    them. The disclosures below are therefore operator-visible prose for the shipped
    category — change them the way you would change a dialog an operator has to agree to,
    not the way you would change a comment.

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
    surfaced = str(category) in SURFACED_CATEGORIES
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
            "Revoking also deletes the facts this category produced."
            + (" You can stop it at any time with `systemu census revoke "
               f"{category}`, or pause it without losing what it found with "
               f"`systemu census pause {category}`."
               if surfaced else
               " Note that this build ships no command to revoke or pause this "
               "category - see `revocation_notice`.")),
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
        # The MECHANISM is real, tested, and purges the derived facts. Whether an
        # OPERATOR can reach it is a separate question, answered per category — a bare
        # `revocable: True` for a category with no surface would be the card's own
        # version of the overclaim this file was held for, and a bare False for the
        # shipped one would deny a control the operator was just handed.
        "revocable": True,
        "revocation_surface_shipped": surfaced,
        "revocation_notice": (
            ("Revoking deletes the facts this category produced, and stops future scans: "
             f"`systemu census revoke {category}`. `systemu census pause {category}` "
             "stops scanning but keeps what was already found. `systemu census status` "
             "shows what is being watched and when it last ran.")
            if surfaced else
            ("Revocation is implemented and it deletes the facts this category produced, "
             "but no command or control in this build calls it for this category yet. "
             "Until one ships, this card must not be shown to an operator as an offer.")),
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
        """The AUTHENTICATED grants on disk, or ``{}``.

        THE FENCE IS THE RETURN VALUE, NOT A RAISE (DEC-32). Every rejection path below
        returns the empty dict, which is the same value an absent file returns, and
        :meth:`is_active` — the check ``run_census`` gates on — derives from nothing else.
        So "this file did not authenticate" and "there is no consent" are the same state
        all the way to the scanner: no frame between here and the decision can catch a
        rejection and carry on.

        WHAT THIS CLOSES. Until the consent surface shipped, this file was plain JSON with
        no signature. Any writer that could place ONE file in the vault — a hand edit, a
        restored backup, a granted ``local_write`` root covering the vault, a tool with
        file-write reach — MANUFACTURED consent for every category. That was live, not
        latent: ``run_census`` has a production caller (``shadow_runtime``, every survey)
        and gates only on :meth:`is_active`, so a forged file made the census scan this
        machine and route the results into the planner prompt — i.e. to the model
        provider — on the next run. It was reproduced end-to-end during review.

        Now the file carries an HMAC-SHA256 over its canonical contents, keyed by a secret
        DERIVED FROM THIS VAULT (:func:`_consent_key`). Forging consent therefore requires
        the vault's own secret, not merely the ability to write a file into the vault
        directory. The MAC covers the WHOLE grants object, so an attacker cannot bolt a
        category onto a file the operator legitimately signed, un-pause one, or backdate
        one: any edit invalidates every row. "Partly valid" is not a state this store
        produces.

        MIGRATION — an unsigned (``version: 1``) file is UNCONSENTED, never grandfathered.
        It is not upgraded in place and not re-signed on read: nothing on disk can prove a
        v1 file ever carried the operator's answer, and re-signing whatever is there would
        be an authenticator that authenticates the attacker. Re-granting is the only way
        back, which is the correct cost — a grant records an answer, so it has to be asked
        for again. Reading a v1 file leaves it exactly as it was.

        FAIL-CLOSED IN EVERY DIRECTION: absent, unreadable, unparseable, wrong version,
        wrong shape, missing MAC, wrong MAC, or NO DERIVABLE KEY all yield no grants and
        never an exception. The failure mode of a damaged or unauthenticated consent file
        is that the census does not run.

        The absent-file check comes FIRST and short-circuits before any key derivation.
        ``session_secret`` is get-or-CREATE, so deriving the key on the fresh-install path
        would make a read-only privacy check mint and persist a vault secret on every
        default install — a side effect the census has no business having. Pinned by
        ``test_a_fresh_install_read_does_not_mint_a_vault_secret``."""
        try:
            if not self._file.exists():
                return {}
            raw = self._file.read_text(encoding="utf-8")
        except Exception:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        # `type(x) is T` in the same frame before any operation on x (DEC-36): every
        # value below came off disk, and `.get`/`in`/`==` all dispatch on a hostile type.
        if type(data) is not dict:
            return {}
        version = data.get("version")
        if type(version) is not int or version != CONSENT_FORMAT_VERSION:
            return {}                                  # v1 (unsigned) is never honoured
        grants = data.get("grants")
        if type(grants) is not dict:
            return {}
        claimed = data.get("mac")
        if type(claimed) is not str:
            return {}
        try:
            # The VERSION fed to the MAC is the module constant, not the file's field.
            # They are equal by the check above; using the constant means the verifier
            # never derives its expectation from an input the verified party controls
            # (DEC-34).
            expected = _consent_mac(_consent_key(self._base),
                                    CONSENT_FORMAT_VERSION, grants)
            candidate = claimed.encode("ascii")
        except Exception:
            return {}                                  # no key, or a non-ASCII mac field
        # BYTES on both sides. `hmac.compare_digest` accepts str only when BOTH are
        # ASCII-only and raises otherwise, and the left operand here is file-controlled.
        if not hmac.compare_digest(candidate, expected.encode("ascii")):
            return {}
        out: Dict[str, dict] = {}
        for cat, row in grants.items():
            # An unknown category on disk is DROPPED on read, not honoured. A category
            # removed from the build must not keep authorising a scan. (Post-MAC this can
            # only come from an older build of systemu itself, never from a forger.)
            if type(cat) is str and cat in CATEGORIES and type(row) is dict:
                out[cat] = row
        return out

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
        """Sign and atomically replace the consent file.

        The key is derived BEFORE anything is created on disk. A consent file this store
        cannot sign must never be written: it would read back as UNCONSENTED on the next
        load, so the operator would have answered "yes" to a control that silently threw
        the answer away. Raising instead makes the grant surface report the failure.
        """
        key = _consent_key(self._base)
        self._base.mkdir(parents=True, exist_ok=True)
        ordered = {k: grants[k] for k in sorted(grants)}
        payload = {"version": CONSENT_FORMAT_VERSION, "grants": ordered,
                   "mac": _consent_mac(key, CONSENT_FORMAT_VERSION, ordered)}
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
