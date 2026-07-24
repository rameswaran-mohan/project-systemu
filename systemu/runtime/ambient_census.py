"""R-W2 (W-B) — the WM-7 Tier-0 AMBIENT CENSUS (spec §5.11.c).

The §5.1 inventory is systemu-CENTRIC: it sees what is already plugged into systemu
(connections, granted roots, credential names, profile). The census is machine-CENTRIC:
it sees what is on the operator's computer at all. "Excel is installed", "git is on
PATH", "your OneDrive is at X" — facts no live inventory source can produce, and which
change planning more than almost any other class.

WHERE THESE FACTS GO, AND WHY THAT IS SAFE
------------------------------------------
Into the R-W1 ``FactStore`` (``<vault>/world_model/facts.json``), from which
``compose_world_view`` attaches a goal-conditioned ranked view to
``SituationReport.world_facts`` and ``render_situation_for_prompt`` renders it as FENCED
data. That is the whole payoff path (§5.11 AC5's third clause: a census-discovered
capability wins a plan without the operator naming it), and it is a DESCRIBE path, not
an AUTHORIZE path:

  * ``world_facts`` is NOT a §5.3 bind source. ``requirement_binder._bind_inventory_entry``
    enumerates its sources BY FIELD NAME (credentials / services / capabilities /
    declared_intents) and ``world_facts`` is deliberately absent. A census fact can be
    READ by the planner; it can never silently fill a requirement.
  * Every row crossing the store→prompt boundary is clamped to ``content_derived`` by
    ``world_query.bind_taint_of``, regardless of what is stored.
  * WM-15: the world model DESCRIBES, it never AUTHORIZES.

"SAFE" ABOVE MEANS TRUST-SAFE, NOT PRIVATE — they are different questions
------------------------------------------------------------------------
Everything above is about what a census fact can DO once read (nothing: it cannot bind,
it cannot authorize). It says nothing about who SEES it, and the answer to that is not
"only this machine": the planner prompt is an LLM call, so consenting to a category
means the values it finds are sent to the configured model PROVIDER on later runs. The
census performs no network I/O itself — that is a true statement about the scanner and a
misleading one about the data, and conflating the two is how ``census_consent`` came to
tell the operator "nothing is transmitted". The consent card now discloses the
transmission as its own field (``leaves_this_machine`` / ``transmission_notice``).

PROVENANCE — census facts are ``content_derived``, deliberately
--------------------------------------------------------------
Not ``systemu_authored``. systemu chose to LOOK, but every value it finds is a string
some third party wrote: a ``DisplayName`` is authored by whoever built the installer, a
CLI name by whoever put the file on PATH, a sync-root path by whoever named the folder.
WM-15 is explicit that "a filename, an app name, a server description IS content", so
the honest taint is the untrusted one. This costs nothing (the read path clamps to
``content_derived`` anyway) and it means the STORED provenance never overstates.

SCOPE — WHAT THIS SLICE DOES NOT DO (stated, not implied)
---------------------------------------------------------

  * **NO OPERATOR-FACING GRANT SURFACE IS BUILT.** This is the gap that matters most, and
    an earlier revision of this section omitted it while enumerating printers. Nothing in
    the product calls :func:`grant_category`, :func:`revoke_category`,
    ``CensusConsentStore.set_paused`` or ``census_consent.consent_card``: no CLI command,
    no dashboard control, no registered tool, no elicitation surface. So no OPERATOR can
    consent. On a FRESH install — one with no ``census_consent.json`` in the vault:
      - ``is_active`` is False for every category, so no probe is reached;
      - :func:`run_census` runs on each survey and returns
        ``{'scanned': [], 'skipped': {<all three>: 'not_consented'}, 'facts_written': 0}``;
      - ``world_facts`` never carries a census row, so §5.11 AC5 clause 3 ("a
        census-discovered capability wins a plan without the operator naming it") is
        DEMONSTRATED BY TEST ONLY, never on a default install;
      - the ``sharing-on world`` standing-scan block renders nothing, because
        :func:`census_status` returns ``[]``.
    THIS IS NOT "THE CENSUS NEVER RUNS." :func:`run_census` is wired into the survey seam
    (``shadow_runtime`` invokes it every survey) and reads ``census_consent.json``
    directly, so every clause above is conditional on that file being absent or ungranted.
    A writer that plants a well-formed consent file makes all four false on the next run —
    the census scans this machine and the facts reach the planner prompt (see
    ``CensusConsentStore._load`` for the integrity gap, which is LIVE, not latent). What
    ships here is the machinery, its live consumer, and its consent model, reviewed ahead
    of the OPERATOR surface. ``test_no_production_grant_surface_exists`` pins the SOURCE
    property (no shipped file references the grant symbols) so the wiring commit cannot
    land without re-reading the disclosures this module and :mod:`census_consent` carry;
    it does not, and cannot, pin that no consent file exists on disk.

WM-7 names six categories; this slice ships THREE probes, and every probe it ships is
read-only by CONSTRUCTION rather than by assumption. The other three, and one half of a
shipped category, are NOT built:

  * **CLI AUTH STATE is not probed.** WM-7 names ``gh auth status`` /
    ``aws sts get-caller-identity``. Those SPAWN A SUBPROCESS and MAKE A NETWORK CALL,
    which is a WM-10 probe — and WM-10(a) requires egress to route through the single §7
    resolve-then-connect SSRF guard, which a child process's own sockets cannot be made
    to do. WM-10 is R-W3's ``world_probes.py``, with its own budget and gating. So
    ``path_clis`` reports PRESENCE ONLY, the category is named for presence, and no
    field claims auth. Shipping a subprocess here under a "read-only" class assumption is
    exactly what B1 forbids.
  * **COM ProgIDs / UIA-automatable apps** — not built (a bounded registry probe, but it
    only refines what ``installed_apps`` already says).
  * **Default file-type handlers** — not built.
  * **Devices / printers** — not built (no stdlib enumeration; needs pywin32/CUPS).

Adding a category is mechanical: one :data:`PROBES` entry + one
:data:`census_consent.CATEGORIES` card. The two dicts are pinned to the same key set,
so neither can ship without the other.

BUDGETS (an unbounded scan of a real machine is a cost AND a privacy problem)
----------------------------------------------------------------------------
Per-category entry cap, whole-census wall-clock deadline, and a minimum re-scan
interval so a consented category does not re-walk the registry on every run.

WRITE-ONLY W.R.T. DECISIONS
---------------------------
This module never READS a fact to decide anything — it has no call to the WM-4 read
family. It writes facts, and (on revocation) deletes them; both are writes. That is why
it can join the world-model allowlist alongside ``world_model_populator`` without
widening the read side.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from systemu.runtime.census_consent import (
    CATEGORIES, CensusConsentStore, UnknownCensusCategory,
)
from systemu.runtime.world_model import Fact, FactStore, ProvStep, fact_id_for

logger = logging.getLogger(__name__)

#: The ``ProvStep.source_kind`` every census fact carries. The ``ref`` is the CATEGORY,
#: which is what makes revocation able to find exactly the facts a category produced.
CENSUS_SOURCE_KIND = "census"

#: Per-category cap on entries from ONE scan. A real machine can list several hundred
#: uninstall keys; the store is not an inventory dump and the planner view is 12 rows.
MAX_ENTRIES_PER_CATEGORY = 200

#: Whole-census wall-clock budget. Checked BETWEEN categories and inside the registry
#: walk, so a pathological hive cannot stall a run.
CENSUS_BUDGET_SECONDS = 6.0

#: A consented category re-scans at most this often. The census runs at the same
#: post-survey seam as the inventory populator, i.e. potentially every run; without this
#: it would re-walk the registry each time for a machine that changes weekly at most.
MIN_RESCAN_INTERVAL_SECONDS = 6 * 60 * 60          # 6 hours

#: The FIXED, published allowlist for ``path_clis``. A closed list, not an enumeration
#: of PATH: enumerating every executable would be unbounded, noisy, and would record
#: names the operator never consented to expose (including private build tooling).
KNOWN_CLIS: Tuple[str, ...] = (
    "git", "gh", "docker", "kubectl", "helm", "terraform", "aws", "az", "gcloud",
    "node", "npm", "yarn", "pnpm", "python", "pip", "poetry", "uv",
    "psql", "mysql", "sqlite3", "redis-cli",
    "ffmpeg", "pandoc", "curl", "rsync", "jq", "make", "cargo", "go", "java", "dotnet",
)

#: Location environment variables whose VALUE IS a directory path. These are the only
#: environment values the census reads; no other env var's value is ever recorded.
_CLOUD_ENV_VARS: Tuple[str, ...] = (
    "OneDrive", "OneDriveCommercial", "OneDriveConsumer",
)

#: Well-known cloud-sync folder names, checked directly under the home directory.
_CLOUD_HOME_DIRS: Tuple[str, ...] = (
    "OneDrive", "Dropbox", "Google Drive", "GoogleDrive", "My Drive",
    "iCloudDrive", "iCloud Drive",
)

#: Windows uninstall registry locations. Read with ``KEY_READ`` — an access mask that
#: does not include any write right, so "read-only" here is enforced by the OS rather
#: than assumed from the call's name.
_UNINSTALL_SUBKEYS: Tuple[str, ...] = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Budget:
    """A monotonic wall-clock deadline for one census run."""

    def __init__(self, seconds: float):
        self._deadline = time.monotonic() + max(0.0, float(seconds))

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._deadline


def _clean(value) -> str:
    """A probe value, or ``""`` if it is not a usable one.

    Uses an explicit ``.strip()`` emptiness test rather than a truthiness/``or``
    default: a whitespace-only ``DisplayName`` is TRUTHY and would otherwise sail
    through as a fact whose value renders as nothing.
    """
    try:
        s = str(value)
    except Exception:
        return ""
    s = s.strip()
    return s if s else ""


# ── the probes: read-only by construction, each bounded by ``limit`` ──────────

def _probe_installed_apps_windows(limit: int, budget: _Budget) -> List[str]:
    """Application display names from the Windows uninstall keys.

    ``KEY_READ`` is the neutralization (WM-10(b) applied to a local read): the handle
    carries no write right, so this cannot mutate the hive even if a key is malformed.
    Skips ``SystemComponent`` entries and update/patch rows (``ParentKeyName``), which
    are not applications an operator would recognise.
    """
    try:
        import winreg                                    # noqa: PLC0415 (Windows-only)
    except Exception:
        return []
    out: List[str] = []
    seen: set = set()
    hives = [(winreg.HKEY_LOCAL_MACHINE, sub) for sub in _UNINSTALL_SUBKEYS]
    hives.append((winreg.HKEY_CURRENT_USER, _UNINSTALL_SUBKEYS[0]))
    for hive, sub in hives:
        if len(out) >= limit or budget.expired:
            break
        try:
            root = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ)
        except Exception:
            continue                                     # absent hive/view — not an error
        try:
            i = 0
            while len(out) < limit and not budget.expired:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break                                # end of enumeration
                i += 1
                try:
                    with winreg.OpenKey(root, name, 0, winreg.KEY_READ) as k:
                        try:
                            if winreg.QueryValueEx(k, "SystemComponent")[0]:
                                continue
                        except OSError:
                            pass                         # absent value ⇒ not a component
                        try:
                            winreg.QueryValueEx(k, "ParentKeyName")
                            continue                     # an update/patch, not an app
                        except OSError:
                            pass
                        display = _clean(winreg.QueryValueEx(k, "DisplayName")[0])
                except Exception:
                    continue                             # one unreadable key never stops the walk
                if display and display.lower() not in seen:
                    seen.add(display.lower())
                    out.append(display)
        finally:
            try:
                root.Close()
            except Exception:
                pass
    return out


def _probe_installed_apps_darwin(limit: int, budget: _Budget) -> List[str]:
    """App bundle names under ``/Applications``. Directory listing only."""
    out: List[str] = []
    for base in ("/Applications", str(Path.home() / "Applications")):
        if len(out) >= limit or budget.expired:
            break
        try:
            entries = sorted(os.listdir(base))
        except Exception:
            continue
        for entry in entries:
            if len(out) >= limit or budget.expired:
                break
            if entry.endswith(".app"):
                name = _clean(entry[: -len(".app")])
                if name and name not in out:
                    out.append(name)
    return out


def _probe_installed_apps_linux(limit: int, budget: _Budget) -> List[str]:
    """Desktop-entry names from the freedesktop application directories.

    Uses the ``.desktop`` FILE STEM rather than parsing the ``Name=`` field: the stem is
    a stable id, and parsing the file would mean reading third-party content for no
    planning benefit.
    """
    out: List[str] = []
    bases = ["/usr/share/applications", "/usr/local/share/applications",
             str(Path.home() / ".local/share/applications")]
    for base in bases:
        if len(out) >= limit or budget.expired:
            break
        try:
            entries = sorted(os.listdir(base))
        except Exception:
            continue
        for entry in entries:
            if len(out) >= limit or budget.expired:
                break
            if entry.endswith(".desktop"):
                name = _clean(entry[: -len(".desktop")])
                if name and name not in out:
                    out.append(name)
    return out


def probe_installed_apps(limit: int, budget: _Budget) -> List[str]:
    """Installed application names for this platform. Never raises; an unsupported
    platform yields ``[]``, which is indistinguishable downstream from the category
    being ungranted (a smaller world, never a broken one)."""
    try:
        if sys.platform.startswith("win"):
            return _probe_installed_apps_windows(limit, budget)
        if sys.platform == "darwin":
            return _probe_installed_apps_darwin(limit, budget)
        return _probe_installed_apps_linux(limit, budget)
    except Exception:
        logger.debug("[census] installed_apps probe degraded", exc_info=True)
        return []


def probe_path_clis(limit: int, budget: _Budget) -> List[str]:
    """The NAMES of :data:`KNOWN_CLIS` present on PATH.

    ``shutil.which`` is a PATH + filesystem lookup: nothing is executed, so this is
    read-only by construction rather than by a class assumption about the tool.

    Returns the NAME, never ``which``'s resolved path — a resolved path on Windows
    routinely embeds the operator's username, and the planning-relevant fact is
    presence, not location.
    """
    out: List[str] = []
    for name in KNOWN_CLIS:
        if len(out) >= limit or budget.expired:
            break
        try:
            if shutil.which(name):
                out.append(name)
        except Exception:
            continue
    return out


def probe_cloud_sync_roots(limit: int, budget: _Budget) -> List[str]:
    """Cloud-sync root DIRECTORIES that exist.

    Two sources, both existence-only — nothing inside any directory is read or listed:
      * the location environment variables in :data:`_CLOUD_ENV_VARS` (whose value IS a
        path — the only env values the census ever records);
      * the well-known folder names in :data:`_CLOUD_HOME_DIRS`, directly under home.
    """
    out: List[str] = []
    seen: set = set()

    def _add(raw) -> None:
        if len(out) >= limit or budget.expired:
            return
        path = _clean(raw)
        if not path:
            return
        try:
            if not os.path.isdir(path):
                return
            key = os.path.normcase(os.path.abspath(path))
        except Exception:
            return
        if key not in seen:
            seen.add(key)
            out.append(path)

    for var in _CLOUD_ENV_VARS:
        _add(os.environ.get(var))
    try:
        home = Path.home()
    except Exception:
        return out
    for name in _CLOUD_HOME_DIRS:
        _add(str(home / name))
    return out


#: category -> (probe, the ``Fact.kind`` its values become).
#:
#: Distinct kinds per category ON PURPOSE. ``staleness_of`` derives freshness from what
#: the INVENTORY survey watermark says it covered, and its ``data_location`` branch also
#: checks the covered roots — so reusing an inventory kind would entangle census facts
#: with a coverage record that never described them. With their own kinds, census facts
#: read ``not_surveyed`` ("absence of coverage is not evidence"), which is honest and is
#: the class ``goal_view`` keeps.
PROBES: Dict[str, Tuple[Callable[[int, _Budget], List[str]], str]] = {
    "installed_apps": (probe_installed_apps, "installed_application"),
    "path_clis": (probe_path_clis, "cli_on_path"),
    "cloud_sync_roots": (probe_cloud_sync_roots, "cloud_sync_root"),
}


def _needs_rescan(last_ran_at: Optional[str], min_interval: float,
                  now_dt: Optional[datetime] = None) -> bool:
    """True iff a category is due for a re-scan.

    A never-run category is due. An UNPARSEABLE stamp is also treated as due: a corrupt
    timestamp must not permanently freeze a consented category, and the cost of the
    wrong answer here is one extra consented read-only scan.
    """
    if not last_ran_at:
        return True
    try:
        last = datetime.fromisoformat(str(last_ran_at))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        cur = now_dt or datetime.now(timezone.utc)
        if cur.tzinfo is None:
            cur = cur.replace(tzinfo=timezone.utc)
        return (cur - last).total_seconds() >= max(0.0, float(min_interval))
    except Exception:
        return True


def _facts_for(category: str, kind: str, values: List[str], now: str) -> List[Fact]:
    """Values → Facts. Pure. ``content_derived`` (see the module docstring), confidence
    1.0 (presence was directly observed), and ONE provenance step whose ``ref`` is the
    CATEGORY — the handle revocation purges on."""
    out: List[Fact] = []
    for v in values:
        value = _clean(v)
        if not value:
            continue
        try:
            out.append(Fact(
                fact_id=fact_id_for(kind, value),
                kind=kind,
                value=value,
                origin_class="content_derived",
                confidence=1.0,
                last_confirmed=now,
                source_chain=[ProvStep(source_kind=CENSUS_SOURCE_KIND,
                                       ref=category, at=now)],
            ))
        except Exception:
            logger.debug("[census] skipped a malformed %s fact", kind, exc_info=True)
    return out


def run_census(vault, *, now: Optional[str] = None,
               budget_seconds: float = CENSUS_BUDGET_SECONDS,
               min_interval_seconds: float = MIN_RESCAN_INTERVAL_SECONDS,
               max_entries: int = MAX_ENTRIES_PER_CATEGORY) -> dict:
    """Run every CONSENTED, due census category and write its facts to the store.

    Returns ``{"scanned": [...], "skipped": {cat: reason}, "discarded": [...],
    "facts_written": N}`` — a summary for the operator surface and the tests, not a
    value any decision reads. ``discarded`` names categories that scanned but whose
    consent was withdrawn before the write (see the re-check below).

    FAIL-SAFE: never raises. A failure anywhere yields a summary with fewer scanned
    categories, which is byte-identical downstream to the feature being absent.

    The consent check is THE gate: :meth:`CensusConsentStore.is_active` is consulted for
    every category before its probe is looked up, so an ungranted category's probe is
    never even reached (§5.11 AC5's first clause).
    """
    summary: dict = {"scanned": [], "skipped": {}, "discarded": [], "facts_written": 0}
    try:
        stamp = now or _now()
        consent = CensusConsentStore(vault.root)
        budget = _Budget(budget_seconds)
        per_category: Dict[str, List[Fact]] = {}
        for category in sorted(PROBES):
            if not consent.is_active(category):
                # Ungranted, or granted-then-paused. Either way: do not scan.
                summary["skipped"][category] = "not_consented"
                continue
            if not _needs_rescan(consent.last_ran_at(category), min_interval_seconds):
                summary["skipped"][category] = "recently_scanned"
                continue
            if budget.expired:
                summary["skipped"][category] = "budget_exhausted"
                continue
            probe, kind = PROBES[category]
            cap = max(0, int(max_entries))
            try:
                values = probe(cap, budget)
            except Exception:
                logger.debug("[census] probe %s degraded", category, exc_info=True)
                summary["skipped"][category] = "probe_failed"
                continue
            # The cap is handed to the probe AND re-applied here. Passing it alone would
            # make it advisory: a probe that ignores its limit — a bug, or a category
            # added later whose source enumerates lazily — would flood the store, and the
            # budget would be a comment rather than a bound. This is the enforcing site.
            per_category[category] = _facts_for(category, kind,
                                                list(values or [])[:cap], stamp)
            # Stamped only after the probe actually RAN, so `last_ran_at` is evidence of
            # a scan rather than of an attempt.
            consent.mark_ran(category, stamp)
            summary["scanned"].append(category)

        # RE-CHECK consent immediately before writing (DEC-10). `revoke_category`
        # withdraws consent and then purges, but a census already past its `is_active`
        # check and still probing would otherwise `put_facts` AFTER that purge and
        # RESURRECT exactly the facts the operator just revoked.
        #
        # Ordering alone cannot close this: the FACT store is an unlocked
        # read-modify-write. (The CONSENT store is lock-serialised — see
        # `census_consent._CONSENT_LOCK` — which is what makes this re-read trustworthy;
        # without that lock a concurrent `mark_ran` could write a revoked grant back and
        # the re-check would read consent that no longer exists.)
        #
        # So this shrinks the window from "the whole probe" (seconds — the registry walk)
        # to "one file read plus one save". It does not eliminate it; a revoke landing
        # between this check and `put_facts` still loses. It is also the correct semantic
        # regardless: never write facts for a category that is no longer consented.
        facts: List[Fact] = []
        for cat, rows in per_category.items():
            if consent.is_active(cat):
                facts.extend(rows)
            else:
                summary["discarded"].append(cat)
        if facts:
            summary["facts_written"] = FactStore(vault).put_facts(facts)
        return summary
    except Exception:
        logger.debug("[census] run_census skipped (non-fatal)", exc_info=True)
        return summary


def census_status(vault) -> List[dict]:
    """READ-ONLY: the categories the operator is currently letting the census watch, each
    with ``granted_at`` / ``last_ran_at`` / ``paused``.

    This is the M3 standing-scan surface's data half — "census last ran", "still watching
    these categories". Exposed HERE rather than by letting callers construct a
    :class:`CensusConsentStore` directly, so that constructing one (i.e. obtaining a
    MUTABLE handle on consent) stays confined to this module and keeps its CONC-MAP
    writer-ownership guard meaningful. A display surface needs the data, not the handle.

    Never raises; an unreadable store reads as "watching nothing". That empty value is the
    reassuring one, so — per the rule that no failure path may emit the healthy signal — it
    is called out rather than left implicit: it is safe ONLY because it cannot hide an
    active scan. :func:`run_census` gates on :meth:`CensusConsentStore.is_active`, which
    derives from the SAME fail-closed :meth:`CensusConsentStore._load`; so whenever this
    display cannot read the file, the scanner cannot either, and both do nothing. A
    file-content problem can therefore never show "watching nothing" while the census
    scans. The residual is a transient per-call I/O error that hits this read but not the
    scanner's — which under-reports the DISPLAY for one call, never the SCAN. Pinned by
    ``test_an_unreadable_consent_file_shows_nothing_and_scans_nothing``."""
    try:
        return CensusConsentStore(vault.root).list_grants()
    except Exception:
        logger.debug("[census] status read degraded", exc_info=True)
        return []


def grant_category(vault, category: str) -> dict:
    """Consent to ``category`` and return the card the operator agreed to.

    NO PRODUCTION CALLER — see the SCOPE section. This is the function an operator grant
    surface would call; until one exists it is reached only from tests, so no OPERATOR can
    create a grant. That is NOT the same as the census being inert: :func:`run_census` is
    wired live and reads the consent file directly, so a planted ``census_consent.json``
    already turns the census on. Whoever wires the operator surface must ship a revoke
    surface in the same change (:func:`revoke_category`), must re-read the disclosures on
    the card it returns, and must resolve the consent-file integrity gap documented on
    ``CensusConsentStore._load`` — that gap is LIVE now (a forged file MANUFACTURES a
    "yes"), and a legitimate grant surface only adds a second thing to forge.

    Raises :class:`UnknownCensusCategory` for an unknown category — grant is the
    direction that AUTHORISES a scan, so it fails loudly.
    """
    return CensusConsentStore(vault.root).grant(category)


def revoke_category(vault, category: str) -> dict:
    """Revoke ``category`` AND purge the facts it produced (WM-7's revocation clause).

    NO PRODUCTION CALLER — see the SCOPE section. This is the entry point a revoke
    surface would call; there is no CLI command, dashboard control or tool behind it
    today, so "revocable" describes a tested mechanism, not something an operator can
    currently do.

    It is THE revoke entry point in the sense that it is the only one that does BOTH
    halves: doing one alone would leave either a scanner with no consent or a store
    still asserting what the operator withdrew.

    The purge runs even when no grant was present, so a leftover fact from an earlier
    revocation is still cleaned up (idempotent). Returns
    ``{"category", "revoked", "facts_removed", "facts_detached"}``.
    """
    out = {"category": category, "revoked": False,
           "facts_removed": 0, "facts_detached": 0}
    try:
        out["revoked"] = CensusConsentStore(vault.root).revoke(category)
    except Exception:
        logger.debug("[census] revoke of %s degraded", category, exc_info=True)
    try:
        purged = FactStore(vault).purge_source_ref(CENSUS_SOURCE_KIND, category)
        out["facts_removed"] = int(purged.get("removed", 0))
        out["facts_detached"] = int(purged.get("detached", 0))
    except Exception:
        logger.debug("[census] purge of %s degraded", category, exc_info=True)
    return out


__all__ = [
    "CENSUS_SOURCE_KIND", "KNOWN_CLIS", "PROBES",
    "MAX_ENTRIES_PER_CATEGORY", "CENSUS_BUDGET_SECONDS", "MIN_RESCAN_INTERVAL_SECONDS",
    "UnknownCensusCategory", "CATEGORIES",
    "probe_installed_apps", "probe_path_clis", "probe_cloud_sync_roots",
    "run_census", "census_status", "grant_category", "revoke_category",
]
