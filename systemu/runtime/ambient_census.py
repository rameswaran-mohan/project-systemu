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

  * **THE OPERATOR GRANT SURFACE COVERS ALL SIX CATEGORIES (P19), ON TWO SURFACES.** The
    history matters for reading the rest of this section: before P4-B2 there was NO
    surface; P4-B2 shipped ``cloud_sync_roots`` alone; P19 widened it to every declared
    category and added the ``/table`` census panel (``interface/pages/table_census.py``,
    its declared region) beside the CLI. Platform availability is a separate question:
    both surfaces refuse to GRANT a category :func:`category_availability` says this
    machine cannot scan, and say why.

    ``cloud_sync_roots`` -- REACHABLE. ``systemu census status | grant | revoke | pause |
    resume`` (``sharing_on/cli.py`` -> :func:`cli_commands.run_census_status` and its four
    siblings) reach :func:`grant_category`, :func:`revoke_category`,
    :func:`pause_category` and ``census_consent.consent_card``. ``grant`` renders the real
    card field by field (including under ``--yes``) and defaults to N. An operator on a
    default install CAN consent, and once they do §5.11 AC5 clause 3 -- "a
    census-discovered capability wins a plan without the operator naming it" -- happens on
    a real install, not only under test. Pinned end to end, through the real CLI and the
    REAL probe, by ``test_the_default_install_path_grants_scans_and_revokes_end_to_end``
    in ``tests/test_rw2_ambient_census.py``: grant -> scan -> fact in the store -> fact in
    ``world_facts`` -> fact in the fenced planner prompt -> revoke -> the next run skips
    AND the fact is gone from all three surfaces.

    Every other category -- ``installed_apps``, ``path_clis``, ``automation_apps``,
    ``file_handlers``, ``installed_printers`` -- is reached the same way since P19, from
    the CLI. GRANT IS CLI-ONLY: the panel offers Withdraw / Pause / Resume (one click,
    the same two verbs through ``run.io_bound``) and shows the exact ``systemu census grant``
    command for a category that is off -- nothing reachable from ``interface/pages/`` may
    call :func:`grant_category` (``tests/test_p19_census_grant_is_cli_only.py``). No registered tool or elicitation surface can create a
    grant for any category; the exact set of references is pinned by
    ``tests/test_p19_census_consent_writer_set.py``. The scope is
    :data:`census_consent.SURFACED_CATEGORIES`, the single source of truth the card, the CLI
    and the panel all derive from.

    On a FRESH install -- one with no ``census_consent.json`` in the vault -- nothing has
    changed, because the surface creates a grant only when the operator runs it:
      - ``is_active`` is False for every category, so no probe is reached;
      - :func:`run_census` runs on each survey and returns
        ``{'scanned': [], 'skipped': {<all six>: 'not_consented'}, 'facts_written': 0}``;
      - ``world_facts`` carries no census row, so AC5 clause 3 does not fire until the
        operator grants;
      - the ``sharing_on world`` standing-scan block renders nothing, because
        :func:`census_status` returns ``[]``.
    THIS IS NOT "THE CENSUS NEVER RUNS." :func:`run_census` is wired into the survey seam
    (``shadow_runtime`` invokes it every survey) and reads ``census_consent.json``
    directly, so every clause above is conditional on that file being absent or ungranted --
    now genuinely a question of what the operator chose, rather than of what was
    unbuildable.
    That file is AUTHENTICATED -- see ``CensusConsentStore._load``: it carries an
    HMAC-SHA256 keyed by a secret derived from this vault, so planting a well-formed
    consent file does not manufacture a grant, and the unsigned ``version: 1`` format is
    never grandfathered.
    ``test_the_census_grant_surface_is_confined_to_its_declared_region`` (which REPLACED
    ``test_no_production_grant_surface_exists``, whose own failure message named the
    narrowing as the procedure) pins the SOURCE property: the grant symbols -- including
    the ``run_census_*`` entry points, which are themselves grant-creating -- appear only
    in the two declared surface files and only below each file's declared region marker.
    A WIDENING to a third category or a third file cannot land without re-reading the
    disclosures this module and :mod:`census_consent` carry, which its failure message
    enumerates. It does not, and cannot, pin what is on a given disk.

WM-7 names six categories. This module ships SIX probes -- ``installed_apps``,
``path_clis``, ``cloud_sync_roots`` and (P19) ``automation_apps``, ``file_handlers``,
``installed_printers`` -- and every one is read-only by CONSTRUCTION rather than by
assumption: registry reads with ``KEY_READ``, directory listings, ``shutil.which``, one
small INI file. Each reads a FIXED published list, never an enumeration. What is NOT
built, stated rather than implied:

  * **CLI AUTH STATE is not probed.** WM-7 names ``gh auth status`` /
    ``aws sts get-caller-identity``. Those SPAWN A SUBPROCESS and MAKE A NETWORK CALL,
    which is a WM-10 probe — and WM-10(a) requires egress to route through the single §7
    resolve-then-connect SSRF guard, which a child process's own sockets cannot be made
    to do. WM-10 is R-W3's ``world_probes.py``, with its own budget and gating. So
    ``path_clis`` reports PRESENCE ONLY, the category is named for presence, and no
    field claims auth. Shipping a subprocess here under a "read-only" class assumption is
    exactly what B1 forbids.
  * **UIA-automatable apps** — not probed. UI Automation needs a live window tree (a
    running program); ``automation_apps`` covers the COM half only.
  * **Devices other than printers** — not probed (no read-only stdlib source).
  * **Platform gaps** — ``automation_apps`` and ``installed_printers`` are Windows-only and
    ``file_handlers`` has no macOS source. :func:`category_availability` says so with a
    reason, and :func:`run_census` never scans an unavailable category, so it is never
    stamped as having run and never shows as "found nothing" (DEC-27).

The three P19 categories are declared, carded, consent-gated and grantable like the
others, wherever this machine has a source for them.

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
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from systemu.runtime.census_consent import (
    CATEGORIES, SURFACED_CATEGORIES, CensusConsentStore, UnknownCensusCategory,
    consent_card, refusal_line,
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

#: The FIXED, published allowlist for ``automation_apps``: COM automation entry points
#: (ProgIDs) of well-known desktop applications. A closed list for the same reason
#: :data:`KNOWN_CLIS` is one: enumerating the class registry would record thousands of
#: names, including private in-house components the operator never agreed to expose.
#: The VALUE recorded is the list entry itself, so nothing installer-authored is stored.
KNOWN_AUTOMATION_PROGIDS: Tuple[str, ...] = (
    "Excel.Application", "Word.Application", "PowerPoint.Application",
    "Outlook.Application", "Access.Application", "OneNote.Application",
    "Visio.Application", "Publisher.Application", "MSProject.Application",
    "AcroExch.App", "Photoshop.Application", "Illustrator.Application",
    "InDesign.Application", "AutoCAD.Application", "QBXMLRP2.RequestProcessor",
    "Lotus.NotesSession",
)

#: The FIXED, published list of file types ``file_handlers`` asks about on Windows.
KNOWN_HANDLER_EXTENSIONS: Tuple[str, ...] = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".txt", ".md",
    ".rtf", ".json", ".xml", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".mp3", ".mp4", ".zip", ".eml", ".msg", ".ics", ".vcf",
)

#: The FIXED, published list of link kinds ``file_handlers`` asks about on Windows.
KNOWN_HANDLER_SCHEMES: Tuple[str, ...] = ("http", "https", "mailto")

#: The same question on a freedesktop system: MIME type -> the label the value carries.
#: Keys are what ``mimeapps.list`` uses; labels match the Windows spelling so one fact
#: means one thing on every platform.
KNOWN_HANDLER_MIME_TYPES: Tuple[Tuple[str, str], ...] = (
    ("application/pdf", ".pdf"),
    ("application/msword", ".doc"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    ("application/vnd.ms-excel", ".xls"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    ("text/csv", ".csv"),
    ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
     ".pptx"),
    ("text/plain", ".txt"),
    ("text/markdown", ".md"),
    ("application/json", ".json"),
    ("text/html", ".html"),
    ("image/png", ".png"),
    ("image/jpeg", ".jpg"),
    ("image/svg+xml", ".svg"),
    ("audio/mpeg", ".mp3"),
    ("video/mp4", ".mp4"),
    ("application/zip", ".zip"),
    ("message/rfc822", ".eml"),
    ("text/calendar", ".ics"),
    ("x-scheme-handler/http", "http:"),
    ("x-scheme-handler/https", "https:"),
    ("x-scheme-handler/mailto", "mailto:"),
)

#: Per-user default-app choices (Windows). Read with ``KEY_READ``.
_FILE_EXTS_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
_URL_ASSOC_SUBKEY = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"

#: The machine-wide print registry (Windows). Its subkeys are printers -- and also
#: per-user SID-named configuration keys, which are NOT printers and carry an account
#: identifier; the probe keeps only subkeys that carry a ``Printer Driver`` value.
_PRINTERS_SUBKEY = r"SYSTEM\CurrentControlSet\Control\Print\Printers"

#: A Windows security identifier (an account SID). The print registry carries per-user
#: printer keys named ``S-1-5-21-...`` and their ``Name`` values embed that SID
#: ("S-1-5-21-...-1001:OneNote") -- witnessed on a real machine while building this probe,
#: and they DO carry a ``Printer Driver`` value, so that filter alone let them through. A
#: SID identifies an account; a value containing one is dropped, never recorded.
_SID_RE = re.compile(r"S-1-\d+(?:-\d+)+")

#: A recorded handler id or printer name longer than this is DROPPED, never truncated:
#: a cut value is a different value, and dropping needs no redaction ordering (DEC-31)
#: because nothing is ever sliced.
_MAX_VALUE_CHARS = 200

#: ``mimeapps.list`` is a small INI file; anything larger is not one and is not read.
_MAX_MIMEAPPS_BYTES = 256 * 1024


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


# ── P19: the rest of WM-7's read-only categories ───────────────────────────────

def _platform_family() -> str:
    """``windows`` / ``macos`` / ``freedesktop`` (Linux and other unix). One seam, so the
    availability rule and the probes cannot disagree about which machine this is."""
    p = str(sys.platform or "")
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "macos"
    return "freedesktop"


def _winreg():
    """The stdlib ``winreg`` module, or ``None`` where it does not exist. A seam so the
    probes below can be driven against a recorded registry on any platform."""
    try:
        import winreg                                    # noqa: PLC0415 (Windows-only)
        return winreg
    except Exception:
        return None


def _usable_id(value) -> str:
    """A handler id / printer name worth recording, or ``""``.

    Dropped, never cut (see :data:`_MAX_VALUE_CHARS`), and never a PATH: these values are
    identifiers, and an absolute path would carry the operator's username into a fact
    the card does not disclose.
    """
    s = _clean(value)
    if not s or len(s) > _MAX_VALUE_CHARS:
        return ""
    if s.startswith(("/", "\\\\")) or ":\\" in s or ":/" in s:
        return ""
    return s


def _reg_value(winreg, hive, subkey: str, name: str) -> str:
    """One string value under ``hive\\subkey``, read with ``KEY_READ``; ``""`` if absent."""
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as k:
            return _clean(winreg.QueryValueEx(k, name)[0])
    except Exception:
        return ""


def probe_automation_apps(limit: int, budget: _Budget) -> List[str]:
    """Which :data:`KNOWN_AUTOMATION_PROGIDS` are registered (``HKCR\\<ProgID>\\CLSID``).

    Existence of a registry key, read with ``KEY_READ`` -- no COM object is created and
    no program is launched, so this is read-only by construction. UI Automation (the
    "UIA-automatable apps" half of WM-7) is NOT probed: it needs a live window tree, i.e.
    a running program, and has no static source.
    """
    if _platform_family() != "windows":
        return []
    winreg = _winreg()
    if winreg is None:
        return []
    out: List[str] = []
    for progid in KNOWN_AUTOMATION_PROGIDS:
        if len(out) >= limit or budget.expired:
            break
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + "\\CLSID", 0,
                                winreg.KEY_READ):
                out.append(progid)
        except Exception:
            continue                                     # not registered
    return out


def _file_handlers_windows(limit: int, budget: _Budget) -> List[str]:
    winreg = _winreg()
    if winreg is None:
        return []
    out: List[str] = []
    for ext in KNOWN_HANDLER_EXTENSIONS:
        if len(out) >= limit or budget.expired:
            return out
        # The operator's own choice first; the class registry's default otherwise.
        handler = _usable_id(_reg_value(winreg, winreg.HKEY_CURRENT_USER,
                                        _FILE_EXTS_SUBKEY + "\\" + ext + "\\UserChoice",
                                        "ProgId"))
        if not handler:
            handler = _usable_id(_reg_value(winreg, winreg.HKEY_CLASSES_ROOT, ext, ""))
        if handler:
            out.append(f"{ext} -> {handler}")
    for scheme in KNOWN_HANDLER_SCHEMES:
        if len(out) >= limit or budget.expired:
            return out
        handler = _usable_id(_reg_value(winreg, winreg.HKEY_CURRENT_USER,
                                        _URL_ASSOC_SUBKEY + "\\" + scheme + "\\UserChoice",
                                        "ProgId"))
        if handler:
            out.append(f"{scheme}: -> {handler}")
    return out


def _file_handlers_freedesktop(limit: int, budget: _Budget) -> List[str]:
    """``~/.config/mimeapps.list`` ``[Default Applications]``, for the fixed MIME list.

    ``XDG_CONFIG_HOME`` is deliberately NOT honoured: the cloud-sync location variables
    are the only environment values the census reads, and that claim stays true. A
    relocated config dir reads as "nothing recorded", a smaller world, never a wrong one.
    """
    import configparser                                  # noqa: PLC0415
    try:
        path = Path.home() / ".config" / "mimeapps.list"
        if not path.is_file() or path.stat().st_size > _MAX_MIMEAPPS_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(text)
        section = parser["Default Applications"]
    except Exception:
        return []
    out: List[str] = []
    for mime, label in KNOWN_HANDLER_MIME_TYPES:
        if len(out) >= limit or budget.expired:
            break
        raw = section.get(mime, fallback="") or ""
        handler = _usable_id(str(raw).split(";")[0])
        if handler:
            out.append(f"{label} -> {handler}")
    return out


def probe_file_handlers(limit: int, budget: _Budget) -> List[str]:
    """``"<type> -> <handler id>"`` for the fixed type list. Never raises."""
    try:
        family = _platform_family()
        if family == "windows":
            return _file_handlers_windows(limit, budget)
        if family == "freedesktop":
            return _file_handlers_freedesktop(limit, budget)
        return []
    except Exception:
        logger.debug("[census] file_handlers probe degraded", exc_info=True)
        return []


def probe_installed_printers(limit: int, budget: _Budget) -> List[str]:
    """Printer names from the Windows print registry, ``KEY_READ`` only.

    Only subkeys carrying a ``Printer Driver`` value are considered, and any subkey or
    name containing an account SID (:data:`_SID_RE`) is skipped -- the per-user keys that
    share the parent pass the driver check, so the SID check is the one that keeps the
    account identifier out. Ports, drivers and jobs are never read. A UNC-shaped name
    (two leading backslashes, then server and printer) is dropped as path-shaped by
    :func:`_usable_id`.
    """
    if _platform_family() != "windows":
        return []
    winreg = _winreg()
    if winreg is None:
        return []
    out: List[str] = []
    seen: set = set()
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PRINTERS_SUBKEY, 0,
                              winreg.KEY_READ)
    except Exception:
        return []
    try:
        i = 0
        while len(out) < limit and not budget.expired:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break                                    # end of enumeration
            i += 1
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                    try:
                        winreg.QueryValueEx(k, "Printer Driver")
                    except OSError:
                        continue                         # not a printer
                    try:
                        name = _usable_id(winreg.QueryValueEx(k, "Name")[0])
                    except OSError:
                        name = _usable_id(sub)
            except Exception:
                continue
            if not name or _SID_RE.search(name) or _SID_RE.search(str(sub)):
                continue
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    finally:
        try:
            root.Close()
        except Exception:
            pass
    return out


#: Which platform families each category has a read-only source on, in THIS build. The
#: key set is pinned to :data:`PROBES` by test, so a category cannot ship without an
#: availability answer.
_CATEGORY_PLATFORMS: Dict[str, Tuple[str, ...]] = {
    "installed_apps": ("windows", "macos", "freedesktop"),
    "path_clis": ("windows", "macos", "freedesktop"),
    "cloud_sync_roots": ("windows", "macos", "freedesktop"),
    "automation_apps": ("windows",),
    "file_handlers": ("windows", "freedesktop"),
    "installed_printers": ("windows",),
}

#: The operator-facing reason a category cannot run here. ASCII, one sentence.
_UNAVAILABLE_REASONS: Dict[str, str] = {
    "automation_apps": ("COM automation exists only on Windows, and this machine is not "
                        "running Windows"),
    "file_handlers": ("this build reads default apps only on Windows and Linux; it has "
                      "no read-only source for them on this platform"),
    "installed_printers": ("this build reads printers only on Windows; on this platform "
                           "every printer source needs root access or a program to run"),
}


def category_availability(category: str, family: Optional[str] = None
                          ) -> Tuple[bool, str]:
    """``(available, reason)`` for ``category`` on this machine. No I/O.

    DEC-27: "unavailable" always carries the reason, and :func:`run_census` never scans
    an unavailable category -- so it is never stamped as having run and never reads as
    "scanned, found nothing", which is a claim about the machine nobody witnessed.
    """
    cat = str(category or "")
    platforms = _CATEGORY_PLATFORMS.get(cat)
    if platforms is None:
        return False, "it is not a census category this build knows"
    fam = family or _platform_family()
    if fam in platforms:
        return True, ""
    return False, _UNAVAILABLE_REASONS.get(
        cat, "this build has no read-only source for it on this platform")


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
    "automation_apps": (probe_automation_apps, "com_automation_server"),
    "file_handlers": (probe_file_handlers, "default_file_handler"),
    "installed_printers": (probe_installed_printers, "printer"),
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
            if not category_availability(category)[0]:
                # Granted, but this machine has no source for it. NOT scanned, so NOT
                # stamped: `last_ran_at` stays evidence of a real scan, and the operator
                # surface shows the reason instead of "found nothing" (DEC-27).
                summary["skipped"][category] = "unavailable"
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


# ── the operator's overview (the /table census panel reads this) ──────────────

#: The four states an operator surface may show for a category. Nothing else: a state
#: outside this set renders as "unknown", never as a reassuring one.
STATE_GRANTED = "granted"
STATE_PAUSED = "paused"
STATE_NOT_GRANTED = "not_granted"
STATE_UNAVAILABLE = "unavailable"

#: Why a declared category cannot be turned on from any operator surface in this build.
#: Derived from ``census_consent.SURFACED_CATEGORIES``, the one source of that scope.
NO_CONTROL_REASON = ("this build ships no control to grant or withdraw it yet, so it "
                     "cannot be turned on")


def _facts_line(state: str, last_ran_at: str, held: Optional[int]) -> str:
    """What the census holds for one category, as the operator reads it. Pure.

    DEC-27, applied to a COUNT: a number is shown only where a scan WITNESSED it. A
    category that never scanned is "not scanned yet", never "0 found"; an unreadable fact
    store is "unknown", never 0; and a category that is not granted says nothing at all
    unless facts are actually held (which happens only when a purge did not run).
    """
    if state in (STATE_GRANTED, STATE_PAUSED):
        if held is None:
            return "What it has found is unknown: the fact store could not be read."
        if not last_ran_at:
            return ("Not scanned yet."
                    + (f" {held} fact(s) gathered earlier are still held." if held else ""))
        lead = "Paused. " if state == STATE_PAUSED else ""
        if held:
            return f"{lead}Last scanned {last_ran_at}; holding {held} fact(s) from it."
        return f"{lead}Last scanned {last_ran_at}; it recorded nothing."
    if held:
        return (f"{held} fact(s) gathered before consent was withdrawn are still held. "
                f"They are deleted when the category is revoked.")
    return ""


def census_overview(vault) -> Optional[dict]:
    """READ-ONLY: every declared category, as an operator surface should show it.

    ``{"refused": str, "facts_readable": bool, "rows": [row, ...]}`` where each row is a
    plain JSON-safe dict (the nicegui boundary rule): ``category``, ``title``, ``state``
    (one of the four ``STATE_*``), ``reason`` (non-empty exactly when the state is
    ``unavailable``), the card's ``collects`` / ``excludes`` / ``how`` / ``why`` /
    ``transmission_notice`` / ``standing_scan_notice`` / ``revocation_notice`` /
    ``sensitivity_notice``, ``granted_at``, ``last_ran_at``, ``facts_held`` (``None``
    unless witnessed), ``facts_line`` and ``controls_shipped``.

    Reads the consent file through the SAME authenticated, fail-closed load the scanner
    gates on (``consent_file_status``), so the panel can never show "granted" for a file
    the census itself refuses -- and ``refused`` carries the operator-readable reason when
    a file is present and not honoured. Writes nothing; on a fresh install it creates no
    file and no secret (the absent-file path short-circuits before key derivation).

    Returns ``None`` when ``vault`` has no root to read: nothing is shown rather than a
    guess. Never raises.
    """
    root = getattr(vault, "root", None)
    if root is None:
        return None
    grants: Dict[str, dict] = {}
    refused = ""
    try:
        verdict = CensusConsentStore(root).consent_file_status()
        if type(verdict.grants) is dict:
            grants = verdict.grants
        refused = refusal_line(verdict)
    except Exception:
        logger.debug("[census] overview consent read degraded", exc_info=True)
    try:
        counts = FactStore(vault).count_by_source_ref(CENSUS_SOURCE_KIND)
    except Exception:
        counts = None
    rows: List[dict] = []
    for category in sorted(CATEGORIES):
        try:
            card = consent_card(category)
        except Exception:
            continue
        available, platform_reason = category_availability(category)
        row = grants.get(category)
        granted = type(row) is dict
        granted_at = str(row.get("granted_at") or "") if granted else ""
        last_ran = str(row.get("last_ran_at") or "") if granted else ""
        controls = category in SURFACED_CATEGORIES
        if not available:
            state = STATE_UNAVAILABLE
            reason = "not available on this machine: " + platform_reason
        elif granted:
            state = STATE_PAUSED if bool(row.get("paused")) else STATE_GRANTED
            reason = ""
        elif not controls:
            state, reason = STATE_UNAVAILABLE, NO_CONTROL_REASON
        else:
            state, reason = STATE_NOT_GRANTED, ""
        held = None if counts is None else int(counts.get(category, 0))
        # A count is a CLAIM only where a scan witnessed it (granted/paused and ran), or
        # where facts are actually held. A never-scanned category's 0 is not a finding.
        witnessed = state in (STATE_GRANTED, STATE_PAUSED) and bool(last_ran)
        shown_count = held if (held is not None and (witnessed or held > 0)) else None
        rows.append({
            "category": category,
            "title": card["title"],
            "state": state,
            "reason": reason,
            "collects": list(card["collects"]),
            "excludes": list(card["excludes"]),
            "how": card["how"],
            "why": card["why"],
            "sensitivity_notice": card.get("sensitivity_notice", ""),
            "transmission_notice": card["transmission_notice"],
            "standing_scan_notice": card["standing_scan_notice"],
            "revocation_notice": card["revocation_notice"],
            "granted_at": granted_at,
            "last_ran_at": last_ran,
            "facts_held": shown_count,
            "facts_line": _facts_line(state, last_ran, held),
            "controls_shipped": controls,
        })
    return {"refused": refused, "facts_readable": counts is not None, "rows": rows}


def grant_category(vault, category: str) -> dict:
    """Consent to ``category`` and return the card the operator agreed to.

    LIVE PRODUCTION CALLERS, FOR EVERY CATEGORY (P19) -- see the SCOPE section.
    ``cli_commands.run_census_grant`` (behind ``systemu census grant``) calls this after
    rendering the card this function returns, and refuses any category outside
    :data:`census_consent.SURFACED_CATEGORIES` before reaching it. So an operator CAN
    create a grant for ``cloud_sync_roots``; for ``installed_apps`` and ``path_clis`` this
    is still reached only from tests.

    The category check lives in the CLI rather than here on purpose: this is the library
    verb, and a future surface (a dashboard control, an elicitation) must be able to widen
    the scope by editing ``SURFACED_CATEGORIES`` and its own refusal, not by editing the
    store. Whoever does that must ship revoke (:func:`revoke_category`) and pause
    (:func:`pause_category`) for the new category in the SAME change, and must re-read the
    disclosures on the card this returns --
    ``test_the_census_grant_surface_is_confined_to_its_declared_region`` enumerates them
    and will fail until the widening is deliberate.

    The consent file is AUTHENTICATED (``CensusConsentStore._load``), so a planted file
    cannot turn the census on; only this path can.

    Raises :class:`UnknownCensusCategory` for an unknown category — grant is the
    direction that AUTHORISES a scan, so it fails loudly.
    """
    return CensusConsentStore(vault.root).grant(category)


def pause_category(vault, category: str, paused: bool) -> bool:
    """Pause (or resume) scanning for a granted category. Returns True iff it is granted.

    The M3 "pause" verb, shipped to the operator alongside grant and revoke
    (``systemu census pause`` / ``systemu census resume``). Exposed HERE rather than
    letting the CLI construct
    a :class:`census_consent.CensusConsentStore` for the same reason
    :func:`census_status` exposes the read: obtaining a MUTABLE HANDLE on consent stays
    confined to this module, which is what keeps the CONC-MAP writer-ownership guard on
    ``CensusConsentStore(`` meaningful. An operator surface needs the VERB, not the
    handle.

    Facts are KEPT. A pause is not a withdrawal of consent, so purging them would make it
    indistinguishable from :func:`revoke_category` — and the difference between the two is
    the entire reason both are offered.
    """
    return CensusConsentStore(vault.root).set_paused(category, bool(paused))


def revoke_category(vault, category: str) -> dict:
    """Revoke ``category`` AND purge the facts it produced (WM-7's revocation clause).

    LIVE PRODUCTION CALLERS, FOR EVERY CATEGORY (P19) -- see the SCOPE section.
    ``cli_commands.run_census_revoke`` (behind ``systemu census revoke``) calls this, so
    for ``cloud_sync_roots`` "revocable" describes something an OPERATOR can do, not only
    a tested mechanism -- which is why ``consent_card`` reports
    ``revocation_surface_shipped: True`` for it and False for the other two, where this
    remains reachable from tests alone.

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
    "KNOWN_AUTOMATION_PROGIDS", "KNOWN_HANDLER_EXTENSIONS", "KNOWN_HANDLER_SCHEMES",
    "KNOWN_HANDLER_MIME_TYPES",
    "MAX_ENTRIES_PER_CATEGORY", "CENSUS_BUDGET_SECONDS", "MIN_RESCAN_INTERVAL_SECONDS",
    "UnknownCensusCategory", "CATEGORIES",
    "probe_installed_apps", "probe_path_clis", "probe_cloud_sync_roots",
    "probe_automation_apps", "probe_file_handlers", "probe_installed_printers",
    "category_availability", "census_overview",
    "STATE_GRANTED", "STATE_PAUSED", "STATE_NOT_GRANTED", "STATE_UNAVAILABLE",
    "run_census", "census_status", "grant_category", "revoke_category",
    "pause_category",
]
