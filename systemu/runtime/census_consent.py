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

CONSENT IS BOUND TO A GENERATION, NOT ONLY TO A VAULT
-----------------------------------------------------
The per-vault MAC alone answered "was this file signed by THIS vault?" and nothing else,
so one genuinely-signed file stayed valid forever. Witnessed end to end on v0.10.27:
save ``census_consent.json``, run ``census revoke``, copy the saved bytes back, and
``census status`` reports GRANTED again while the next survey re-scans the machine. The
operator's LAST ACT was a withdrawal.

The MAC key is therefore derived per (vault, EPOCH). The epoch is a monotonic integer in
a census-owned sidecar — :func:`consent_epoch_file`, ``<vault>/secrets/census_consent.epoch``
— that :meth:`CensusConsentStore.revoke` ADVANCES before it rewrites the file. Every
signature issued before a withdrawal is stale from the instant the withdrawal lands, so a
restored copy authenticates against a key that no longer exists. ``grant`` / ``set_paused``
/ ``mark_ran`` never advance it: they record or suspend an answer rather than withdrawing
one, and bumping there would invalidate the operator's OTHER live grants.

A consent file with NO epoch sidecar is UNCONSENTED, never grandfathered — the same rule
the unsigned ``version: 1`` format gets, and for the same reason: nothing on disk can
prove which generation an unanchored file belongs to. The absent-sidecar case reads as
"epoch 0" only where there is also no consent file, i.e. a fresh install, and even that
is minted on the SIGNING path alone (reading a fresh vault still creates nothing).

WHAT THAT DOES NOT CLAIM (typed P under DEC-36). Restoring BOTH the consent file and the
epoch sidecar replays, and so does restoring ``dashboard_auth``'s session secret. Those
are writes to the KEY-DOMAIN files themselves: in-process Python is one trust domain, and
a party who can rewrite the key material can mint a genuine signature directly. DELETING
the sidecar is the same class — it fails CLOSED at once (everything reads UNCONSENTED)
but resets the counter, so a LATER operator-typed grant re-creates epoch 0 and a copy
signed at epoch 0 verifies again. Stated rather than papered over. The claim this fence
DOES make is the one the defect broke: a SINGLE restored consent file never revives a
withdrawn consent.

WHERE IT IS STORED, AND WHERE IT GOES
-------------------------------------
Consent: ``<vault>/census_consent.json`` (this module — sole writer).
Consent generation: ``<vault>/secrets/census_consent.epoch`` (this module — sole writer).
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
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

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

#: Binds the derived key to the consent GENERATION. Unambiguous by construction: the
#: base info string contains no NUL and a decimal epoch contains no NUL, so no (info,
#: epoch) pair can spell the same bytes as another.
_CONSENT_KEY_EPOCH_TAG = b"\x00epoch="

#: The consent-generation anchor. It lives under the vault's ``secrets/`` directory — not
#: at the vault root next to the consent file — because that directory is already fenced
#: as runtime state on every road out of the tree: ``.gitignore``'s
#: ``systemu/vault/secrets/`` rule and
#: ``tests/test_packaged_vault_carries_no_runtime_state.py``'s ``RUNTIME_STATE_DIRS``.
#: An anchor that shipped in the wheel would be identical on every install, which is the
#: same collapse of "per-vault" to "global" that a shipped session secret causes.
_CONSENT_EPOCH_DIRNAME = "secrets"
CONSENT_EPOCH_FILENAME = "census_consent.epoch"

#: The anchor's whole vocabulary: one non-negative ASCII decimal integer. Deliberately
#: NOT ``str.isdigit`` / bare ``int()`` — both accept non-ASCII decimal digits (and
#: ``isdigit`` accepts superscripts), so "the file holds a number" and "the file holds
#: the number I will re-serialise" would be different questions.
_ASCII_DIGITS = frozenset("0123456789")

#: Bounds the anchor read. A real epoch counts operator withdrawals; anything needing
#: more than 18 digits is not one.
_MAX_EPOCH_DIGITS = 18


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def consent_epoch_file(base_dir) -> Path:
    """``<base_dir>/secrets/census_consent.epoch`` — the consent-generation anchor."""
    return Path(base_dir) / _CONSENT_EPOCH_DIRNAME / CONSENT_EPOCH_FILENAME


def read_consent_epoch(base_dir) -> Optional[int]:
    """The current consent generation, or ``None``.

    STRICT, and ``None`` is the fence value (DEC-32): absent, unreadable, empty, padded,
    signed, fractional, non-ASCII, over-long or non-decimal all read as "no anchor",
    which :meth:`CensusConsentStore._load` turns into UNCONSENTED whenever a consent file
    is present. There is no "probably meant zero" branch — that branch is precisely the
    grandfathering this anchor exists to refuse.

    Creates NOTHING. ``run_census`` calls into the read path on every survey, so a
    read-only privacy check must never mint the state it is checking (the same reason
    :meth:`CensusConsentStore._load` short-circuits before key derivation).
    """
    try:
        raw = consent_epoch_file(base_dir).read_text(encoding="ascii")
    except Exception:
        return None
    # `type(x) is T` in the same frame before any operation on x (DEC-36): `raw` came
    # off disk, and `.strip`/`in`/`<=` all dispatch on a hostile type.
    if type(raw) is not str:
        return None
    text = raw.strip()
    if not text or len(text) > _MAX_EPOCH_DIGITS:
        return None
    if not set(text) <= _ASCII_DIGITS:
        return None
    value = int(text)
    if type(value) is not int or value < 0:
        return None
    return value


def _write_consent_epoch(base_dir, epoch: int) -> None:
    """Atomically replace the anchor with ``epoch`` (tmp + ``os.replace``).

    ``mkstemp`` creates the temp file 0600 and ``os.replace`` carries that mode over, so
    the anchor lands with the same permissions as everything else under ``secrets/``.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError("the census consent epoch must be a non-negative int")
    directory = Path(base_dir) / _CONSENT_EPOCH_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix="census_consent.epoch.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(str(epoch))
        os.replace(tmp, str(consent_epoch_file(base_dir)))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _current_epoch_or_create(base_dir) -> int:
    """The SIGNING path's view of the generation: get-or-create, starting at 0.

    Only the signing path may create. A vault with no anchor and no consent file is a
    fresh install, and generation 0 is its correct first answer. A vault with no anchor
    and a consent file present is UNCONSENTED on read (see :meth:`CensusConsentStore._load`),
    so the file this creation re-anchors is never the one already on disk — ``grant``
    rewrites from its own (empty) load.
    """
    current = read_consent_epoch(base_dir)
    if type(current) is int:
        return current
    _write_consent_epoch(base_dir, 0)
    return 0


def _bump_consent_epoch(base_dir) -> int:
    """Advance the consent generation. THE WITHDRAWAL PRIMITIVE.

    Sole caller: :meth:`CensusConsentStore.revoke`, under ``_CONSENT_LOCK``. Registered
    in ``docs/CONC-MAP.md`` and pinned by ``tests/test_conc_map_writer_ownership.py`` on
    THIS call — which makes that guard a reachability pin, not only an allowlist: delete
    the bump from ``revoke`` and its "declared writer no longer calls this" half fails.

    A second caller would be a DEC-10 review AND a consent question: advancing the
    generation invalidates every signature the operator currently holds, so anything but
    a withdrawal doing it silently revokes consent the operator never withdrew.
    """
    nxt = _current_epoch_or_create(base_dir) + 1
    _write_consent_epoch(base_dir, nxt)
    return nxt


def _consent_key(base_dir, epoch=None) -> bytes:
    """The per-vault, per-GENERATION HMAC key for ``census_consent.json``.

    ``epoch`` is the consent generation the key belongs to, and the two call sites want
    opposite things from it, so it is explicit rather than implied:

      * ``None`` (the SIGNING path, :meth:`CensusConsentStore._write`) means "the current
        generation, creating the anchor at 0 if this vault has none". A signer must be
        able to anchor a fresh install.
      * an int (the VERIFYING path, :meth:`CensusConsentStore._load`) is the generation
        read STRICTLY off disk. The verifier never falls back to a default: a missing
        anchor is refused BEFORE this function is reached, because a verifier that
        assumed 0 would honour every signature ever issued by a vault whose anchor was
        deleted.

    Making the epoch part of the KEY rather than a field in the signed body is
    deliberate. A plaintext ``epoch`` field would have to be compared separately, and a
    second layer that checks something CHEAPER than the MAC is worse than no second layer
    (DEC-34): it manufactures confidence while the MAC alone still decides. With the key
    bound to the generation there is exactly ONE check, and a stale file fails it the same
    way a tampered file does — whole-file, fail-closed, no partial trust.

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
    # The seed is checked BEFORE the anchor is get-or-created, so a vault that cannot
    # sign does not acquire an anchor as a side effect of trying.
    if epoch is None:
        epoch = _current_epoch_or_create(base_dir)
    if type(epoch) is not int or epoch < 0:
        raise ValueError("no usable consent generation for the census consent MAC")
    info = _CONSENT_KEY_INFO + _CONSENT_KEY_EPOCH_TAG + str(epoch).encode("ascii")
    return hmac.new(seed.encode("utf-8"), info, hashlib.sha256).digest()


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


# ── the refusal, as something the operator can READ ──────────────────────────
#
# THE OBSERVATION (dogfood 28, replaying the v0.10.27 repro). The generation fence
# WORKS: restore a pre-revoke ``census_consent.json`` and it authenticates against a
# key that no longer exists, nothing is granted, no probe runs. What it does not do is
# SAY so. ``census status`` renders that vault identically to one that was never
# granted at all -- "not granted" for every category, and stop. An operator looking at
# a file they just put back, told nothing about it, reasonably concludes the command
# did not see it; the next thing they try is putting it back again, or hand-editing.
# A refusal that is correct and invisible is the one combination that teaches the
# operator to fight it.
#
# The reason is therefore a VALUE (DEC-32), carried beside the grants the load path
# already returns. Nothing raises across this boundary: ``run_census`` still gets the
# same empty dict from a damaged file that it always got.

#: The file was signed by THIS vault, at a generation that has since been withdrawn.
#: WITNESSED, never inferred (DEC-27): claimed only when the MAC actually verifies
#: against a key derived at an earlier generation of this same vault.
REFUSED_STALE_GENERATION = "stale generation"

#: The MAC verified at no generation this store looked at -- a hand edit, a file from
#: another vault, or a signature older than the bounded search below. The LESS specific
#: claim, and therefore the fallback: it says only what was witnessed.
REFUSED_SIGNATURE_MISMATCH = "signature mismatch"

#: ``secrets/census_consent.epoch`` is missing or unreadable, so nothing on disk can say
#: which generation the file belongs to. No signature question is even reached.
REFUSED_NO_GENERATION_ANCHOR = "no generation anchor"

#: Unreadable, unparseable, the wrong shape, or the unsigned ``version: 1`` format. Not
#: one of the three above, and not silent either: the file is THERE and is not being
#: honoured, which is the thing the operator cannot otherwise see.
REFUSED_MALFORMED = "unreadable or unsigned consent file"

#: The per-vault signing secret could not be obtained, so the file could not be checked
#: at all. Stated as the inability it is rather than folded into a claim about the file.
REFUSED_NO_VAULT_SECRET = "this vault's signing secret is unavailable"

#: How many withdrawn generations back a refused file is searched before the diagnosis
#: degrades to the less specific claim. Bounded so a large counter cannot turn a status
#: read into an unbounded key-derivation loop; generous enough that a real operator's
#: withdrawal history is covered.
MAX_DIAGNOSED_GENERATIONS = 64

#: The operator-facing sentence. ASCII only (DEC-32c) -- it reaches the same console
#: every other v0.10.29/v0.10.30 verdict surface was made ASCII for.
_REFUSAL_LINE = ("a consent file exists but was refused ({reason}); nothing is "
                 "granted - re-run census grant if you meant it")


class ConsentFileVerdict:
    """The grants, and WHY there are none, as one value.

    ``grants`` is exactly what :meth:`CensusConsentStore._load` has always returned --
    the authenticated rows, or ``{}``. ``refused`` distinguishes the two states that
    used to be spelled the same way: "there is no consent file" and "there is a consent
    file and it was not honoured".
    """

    __slots__ = ("grants", "refused", "reason")

    def __init__(self, grants=None, refused: bool = False,
                 reason: str = "") -> None:
        self.grants = {} if grants is None else grants
        self.refused = refused
        self.reason = reason

    def __repr__(self) -> str:                       # pragma: no cover - debug
        return "ConsentFileVerdict(refused={r!r}, reason={w!r}, rows={n})".format(
            r=self.refused, w=self.reason, n=len(self.grants or {}))


def refusal_line(verdict) -> str:
    """The one line ``census status`` prints for a refused file, or ``""``.

    Pure: a verdict in, a sentence out, no I/O and no vault. That is what lets the
    CLI's call site be one line and lets this sentence be pinned without a CLI.

    DEC-36: the concrete type is checked with ``type(x) is T`` before anything is read
    off it. A stand-in that merely has a ``.refused`` renders nothing rather than
    having whatever its ``__getattr__`` returns formatted into an operator-facing claim.
    """
    if type(verdict) is not ConsentFileVerdict:
        return ""
    if verdict.refused is not True:
        return ""
    reason = verdict.reason
    if type(reason) is not str or not reason:
        reason = REFUSED_MALFORMED
    return _REFUSAL_LINE.format(reason=reason)


def census_status_refusal_line(base_dir) -> str:
    """``census status``'s refusal line for the vault at ``base_dir``, or ``""``.

    The whole surface in one call, so the CLI's census-status renderer adds exactly one
    line and no consent logic. Never raises: a status display that dies on a damaged
    consent file is a worse version of the silence this closes.
    """
    try:
        return refusal_line(CensusConsentStore(base_dir).consent_file_status())
    except Exception:
        logger.debug("[census] consent refusal read degraded", exc_info=True)
        return ""


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
                "any other fact -- it is the difference between planning around a tool "
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
                "executed -- presence is decided by the filesystem alone."),
    },
    "cloud_sync_roots": {
        "title": "Cloud-sync folders",
        "collects": ["the folder paths of OneDrive / Dropbox / Google Drive / iCloud"],
        "excludes": ["file names", "file contents", "account identifiers other than "
                     "whatever appears in the folder path itself"],
        "why": ("Knowing where your synced folders are lets a plan find your documents "
                "without you pointing at them."),
        "how": ("Checks a fixed list of location environment variables and well-known "
                "folder names under your home directory. Directory existence only -- "
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
            f"unknown census category {category!r} -- known: {', '.join(sorted(CATEGORIES))}")
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
            "to its model provider on later runs -- that is what makes the census useful, "
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
        wrong shape, missing MAC, wrong MAC, MISSING OR MALFORMED GENERATION ANCHOR, a
        MAC signed at an earlier generation, or NO DERIVABLE KEY all yield no grants and
        never an exception. The failure mode of a damaged, unauthenticated or WITHDRAWN
        consent file is that the census does not run.

        The absent-file check comes FIRST and short-circuits before any key derivation.
        ``session_secret`` is get-or-CREATE, so deriving the key on the fresh-install path
        would make a read-only privacy check mint and persist a vault secret on every
        default install — a side effect the census has no business having. Pinned by
        ``test_a_fresh_install_read_does_not_mint_a_vault_secret``.

        This is now a thin read of :meth:`_inspect`, which carries the REFUSAL REASON
        alongside the same grants. Every caller of this method sees exactly what it
        always saw: the value, and its emptiness, are unchanged -- and it does NOT ask
        for the generation diagnosis, which is display work on a path ``run_census``
        takes per category per survey."""
        return self._inspect().grants

    def consent_file_status(self) -> "ConsentFileVerdict":
        """The grants AND why there are none -- the operator-facing read.

        Distinct from :meth:`_load` on purpose, in both directions. ``run_census`` wants
        the fence and nothing else; ``census status`` wants to be able to tell "there is
        no consent file" apart from "there is one and it was refused", which is the whole
        of this finding -- and it is the only caller that pays for the bounded generation
        search. Never raises (DEC-32).
        """
        return self._inspect(diagnose=True)

    def _inspect(self, *, diagnose: bool = False) -> "ConsentFileVerdict":
        """THE VERIFICATION, with its verdict as a value.

        Structurally identical to the fail-closed load it replaces -- every rejection
        yields NO GRANTS -- with one addition: each rejection also names itself. The
        grants are built on the success path ALONE, so the diagnosis below cannot
        return the rows it just refused no matter how it goes wrong.
        """
        try:
            if not self._file.exists():
                # A vault that was never granted. Nothing was refused, so there is
                # nothing to report, and no key derivation happens here.
                return ConsentFileVerdict()
            raw = self._file.read_text(encoding="utf-8")
        except Exception:
            return ConsentFileVerdict(refused=True, reason=REFUSED_MALFORMED)
        try:
            data = json.loads(raw)
        except Exception:
            return ConsentFileVerdict(refused=True, reason=REFUSED_MALFORMED)
        # `type(x) is T` in the same frame before any operation on x (DEC-36): every
        # value below came off disk, and `.get`/`in`/`==` all dispatch on a hostile type.
        if type(data) is not dict:
            return ConsentFileVerdict(refused=True, reason=REFUSED_MALFORMED)
        version = data.get("version")
        if type(version) is not int or version != CONSENT_FORMAT_VERSION:
            # v1 (unsigned) is never honoured
            return ConsentFileVerdict(refused=True, reason=REFUSED_MALFORMED)
        grants = data.get("grants")
        if type(grants) is not dict:
            return ConsentFileVerdict(refused=True, reason=REFUSED_MALFORMED)
        claimed = data.get("mac")
        if type(claimed) is not str:
            return ConsentFileVerdict(refused=True, reason=REFUSED_MALFORMED)
        try:
            candidate = claimed.encode("ascii")
        except Exception:
            # A non-ASCII mac field is the FILE's defect, so it is named as one rather
            # than folded in with "we could not derive a key", which is ours.
            return ConsentFileVerdict(refused=True, reason=REFUSED_MALFORMED)
        # THE GENERATION ANCHOR, read STRICTLY. A consent file with no readable anchor
        # is UNCONSENTED — the same rule the unsigned v1 format gets, and refused for the
        # same reason: nothing on disk can prove which generation an unanchored file
        # belongs to, and "assume 0" would honour every signature a vault ever issued.
        # Like the version above, this is NOT taken from the consent file: the verifier
        # never derives its expectation from an input the verified party controls (DEC-34).
        epoch = read_consent_epoch(self._base)
        if type(epoch) is not int:
            return ConsentFileVerdict(refused=True,
                                      reason=REFUSED_NO_GENERATION_ANCHOR)
        try:
            # The VERSION fed to the MAC is the module constant, not the file's field.
            # They are equal by the check above; using the constant means the verifier
            # never derives its expectation from an input the verified party controls
            # (DEC-34).
            expected = _consent_mac(_consent_key(self._base, epoch),
                                    CONSENT_FORMAT_VERSION, grants)
        except Exception:
            return ConsentFileVerdict(refused=True,
                                      reason=REFUSED_NO_VAULT_SECRET)
        # BYTES on both sides. `hmac.compare_digest` accepts str only when BOTH are
        # ASCII-only and raises otherwise, and the left operand here is file-controlled.
        if not hmac.compare_digest(candidate, expected.encode("ascii")):
            # The unconditional half is the WITNESSED one: this MAC did not verify at
            # this vault's current generation. Whether it verified at an EARLIER one is
            # a further question, asked only where somebody is going to read the answer.
            reason = REFUSED_SIGNATURE_MISMATCH
            if diagnose:
                reason = self._diagnose_mac_refusal(epoch, grants, candidate)
            return ConsentFileVerdict(refused=True, reason=reason)
        out: Dict[str, dict] = {}
        for cat, row in grants.items():
            # An unknown category on disk is DROPPED on read, not honoured. A category
            # removed from the build must not keep authorising a scan. (Post-MAC this can
            # only come from an older build of systemu itself, never from a forger.)
            if type(cat) is str and cat in CATEGORIES and type(row) is dict:
                out[cat] = row
        return ConsentFileVerdict(grants=out)

    def _diagnose_mac_refusal(self, epoch: int, grants, candidate: bytes) -> str:
        """Which of the two MAC refusals this is. A DIAGNOSIS, never a decision.

        The file has already been refused by the frame above; nothing here can change
        that, and nothing here returns a grant. All this does is answer the operator's
        actual question -- "is this the file I saved before I revoked, or is it a file
        that was never mine?" -- because those have completely different remedies and
        the fence spells them the same way.

        ``stale generation`` is WITNESSED (DEC-27): it is claimed only when the file's
        own MAC verifies against a key derived at an earlier generation OF THIS VAULT.
        Everything else -- including a signature older than the bounded search, and a
        search that could not run -- degrades to the less specific
        ``signature mismatch``, which says only what was actually observed.

        Bounded by :data:`MAX_DIAGNOSED_GENERATIONS`: this runs on a display path, and
        an unbounded walk back through a large epoch would turn `census status` into a
        key-derivation loop.
        """
        limit = MAX_DIAGNOSED_GENERATIONS
        if type(limit) is not int or limit < 1:
            return REFUSED_SIGNATURE_MISMATCH
        if type(epoch) is not int or epoch < 1:
            return REFUSED_SIGNATURE_MISMATCH       # generation 0: nothing is older
        lowest = epoch - limit
        if lowest < 0:
            lowest = 0
        for older in range(epoch - 1, lowest - 1, -1):
            try:
                previous = _consent_mac(_consent_key(self._base, older),
                                        CONSENT_FORMAT_VERSION, grants)
            except Exception:
                logger.debug("[census] consent generation diagnosis degraded",
                             exc_info=True)
                return REFUSED_SIGNATURE_MISMATCH
            if hmac.compare_digest(candidate, previous.encode("ascii")):
                return REFUSED_STALE_GENERATION
        return REFUSED_SIGNATURE_MISMATCH

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

        Signs at the CURRENT generation, get-or-creating the anchor at 0 on a vault that
        has none (:func:`_current_epoch_or_create`). Every mutator rewrites the whole file
        from its own load, so this is also what re-anchors the rows a ``revoke`` did not
        withdraw: they are re-signed into the new generation the same call that bumped it.
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

        ADVANCES THE CONSENT GENERATION (:func:`_bump_consent_epoch`) before rewriting
        the file, which is what makes a withdrawal stick against a restored copy. Order
        and placement are both load-bearing:

          * bump BEFORE the rewrite, so :meth:`_write` signs the surviving rows with the
            NEW key. Reversed, the rewrite would be signed into the generation it is about
            to invalidate and the operator's other grants would all die with this one.
          * bump INSIDE ``_CONSENT_LOCK``, alongside the read-modify-write it belongs to.
            Outside it, a ``mark_ran`` that loaded before the bump and wrote after it
            would re-sign the revoked grant into the new generation — a resurrection the
            epoch cannot catch, because the resurrected file would be genuinely current.

        If the rewrite then fails (an unsignable vault), the generation has already moved
        and the file left on disk is stale: everything reads UNCONSENTED. That is the
        fail-closed direction for a withdrawal.

        A no-op revoke does NOT bump. There is no signature to stale out, and a counter
        that moved on every call would turn a privacy file into a liveness signal.
        """
        with _CONSENT_LOCK:
            grants = self._load()
            if category not in grants:
                return False
            grants.pop(category, None)
            _bump_consent_epoch(self._base)
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
