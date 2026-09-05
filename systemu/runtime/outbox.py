"""U-12 — the no-UI Outbox contract (R-UTL1).

A completed run hands its work back to the filesystem so an operator (or a
script) never has to open the dashboard to collect it:

    <vault>/Outbox/<yyyy-mm-dd>-<task-slug>/
        receipt.html          # standalone, no network, redacted
        <artifacts...>        # COPIES — the run's own files stay where they are
        FAILED-<slug>.txt     # ONLY when the run did not succeed
        .done                 # written LAST: "this folder is complete"

``.done`` is the consumer contract. It is written after every other file in the
folder, so a watcher that waits for ``.done`` can never read a half-copied
artifact. It is written on success AND failure — it means "systemu has finished
writing this folder", not "the task succeeded". The run's verdict is
``receipt.html`` (and the presence of ``FAILED-*.txt``).

Trust surface: everything here LEAVES the process and lands where other tools
read it, so text rendered into ``receipt.html`` goes through the shipped
outbound redactor (``messaging.gateway.mask_outbound``) and the shipped
value-level secret check (``ask_promotion._value_is_secret``) — REUSED, not
reinvented. Known gap, stated plainly rather than papered over: both fences are
lexical/shape-based, so a SHAPELESS secret (``hunter2``, a bare 32-char hex)
passes both, and NEITHER reads the CONTENT of a copied artifact. A receipt that
passed these checks is not a guarantee that the folder holds no secrets.

Confinement: every path written is re-checked to be inside the Outbox root
(``commonpath`` on resolved paths) immediately before the write, so a crafted
task slug or artifact basename can never traverse out.

Identity vs. content (DEC-34c): every PATH COMPONENT this module writes (the
run folder's slug, and each copied artifact's STEM) is derived ONLY from
:class:`TrustedIdentity` sources — ``task_id`` and a per-artifact ordinal —
never from ``prompt`` or from a copied file's original name. Those stay
CONTENT, rendered only into ``receipt.html``/``FAILED-*.txt`` through
:func:`redact`. A credential pasted into either one is therefore
UNREPRESENTABLE in a path: not because a detector recognises its shape, but
because prompt text and other untrusted free-text fields are never a path
input in the first place.

Type vs. identity (DEC-34c round 4): a copied artifact's basename is
``artifact-<ordinal><ext>`` — the ``<ext>`` is real, and matches the SOURCE
file's own suffix, but only when that suffix is a member of the fixed,
code-defined :data:`_ARTIFACT_EXTENSIONS` table (:func:`_artifact_extension`).
This is not a narrowing of AC-1b: a suffix drawn from a compile-time enum and
matched case-insensitively (the emitted spelling is always the table's own
lowercase form, never the source's casing) carries no attacker-chosen byte —
:class:`TrustedIdentity`'s own docstring names "a value drawn from a fixed,
code-defined enum" as exactly this kind of safe, non-prompt identity. An
unrecognised or absent source suffix yields NO destination suffix — never a
passthrough of whatever the source happened to be named.

The receipt's "copied from" line redacts the parent directory and the
basename of the ORIGINAL path as two INDEPENDENT values (:func:`_esc_path`),
not one compound string handed to :func:`redact` whole. A single-string
redaction made two unrelated artifacts collapse to the identical rendered
line the instant the run's OWN directory happened to contain anything
shape-matching (a 40+ hex-character run id is enough, and carries no
credential) — splitting first means a hex-shaped directory segment no longer
drags an ordinary filename down with it, while a genuinely credential-shaped
basename still collapses on its own, independently, exactly as before.

Suffix vs. survivability (DEC-34c round 5): round 4's allowlist both
under- and over-reached. It admitted three suffixes (``.html``/``.htm``/
``.svg``) whose stock Windows file association EXECUTES the file's own
embedded script on double-click — removed; see :data:`_ARTIFACT_EXTENSIONS`'s
own docstring for precisely what the table claims now. It also excluded
several genuinely inert data types, stranding legitimate deliverables
unopenable — widened, deliberately and per-type, not by unioning the whole
"untyped" survey (same docstring). A compound archive suffix
(``archive.tar.gz``) no longer collapses to its misleading final component
alone (:func:`_display_suffix`), and every artifact row — admitted or
excluded — now states its ORIGINAL suffix in the receipt, so an excluded
type is never stranded with no way to learn what it was
(:func:`render_receipt`, AC-4).

Claim vs. leak (DEC-34c round 6): two corrections, one in the code, one in
the comments only. Code: the round 5 sentence just above — "every artifact
row ... now states its ORIGINAL suffix" — was accurate about WHAT the
receipt's type line showed but not about how it got there. It was built by
SLICING the untrusted original name (:func:`_display_suffix` returns
whatever follows the source's last dot) and redacting the SLICE, not the
whole value — and DEC-31 exists precisely because a slice can defeat a
shape-based redactor. Measured: a source basename equal to a JWT (three
dot-separated segments) has ITS OWN SIGNATURE segment taken as "the
suffix" by that slice; the signature alone does not match the three-part
shape either shipped fence looks for, so it rendered VERBATIM, while the
identical ``redact()`` call against the INTACT original — the basename
half of :func:`_esc_path`'s "copied from" line, two rows up in the same
receipt — correctly recognised and masked the very same value. The type
line no longer shows the original suffix, sliced or otherwise: it shows
either the matching :data:`_ARTIFACT_EXTENSIONS`/
:data:`_COMPOUND_ARTIFACT_EXTENSIONS` member (a fixed, code-defined enum —
never a transformation of the untrusted name) or one fixed "not preserved"
literal (:func:`_receipt_type_label`). Round 5's stated goal for this line
— "an excluded type is never stranded with no way to learn what it was" —
was retired rather than re-attempted, on the reasoning that every way tried
to keep it depends on redacting something DERIVED from untrusted text,
which is the mechanism that just leaked. **Phase 1d narrows that.** A
SECOND closed, code-defined table (:data:`_WITHHELD_EXTENSION_LABELS`)
names the excluded suffixes it knows, and states the rename that opens the
landed file deliberately - the untrusted suffix is used only as a
membership key, so the rendered bytes are the table's own literals and the
round 5 mechanism is not re-introduced. A suffix neither table knows still
renders the fixed literal, unnamed. Nothing about what LANDS changed: a
labelled ``.pdf`` is still copied to a bare ``artifact-<n>``.
Comments only: a stock Windows install also runs
``.xml`` through a script-capable browser exactly like the three round 5
removed — removed here too — and the per-extension table's own comment
block had, by round 5's last commit, started asserting properties ("no
member is ever a browser"; "every member opens in a passive viewer") that
were measurably false for members round 5 never individually checked; see
:data:`_ARTIFACT_EXTENSIONS`'s own docstring for the corrected, narrower,
per-member-measured claims.

Measured vs. inferred (DEC-34c round 7): two corrections, neither reversing
a wrong fact so much as closing an UNMEASURED one. First,
:data:`_ARTIFACT_EXTENSIONS`'s round 6 comment left ``.pdf`` admitted as a
named residual on the reasoning that it reaches the same ``msedge.exe`` the
four removed suffixes did but "through a distinct, PDF-specific viewing
surface" — a claim the same paragraph admitted stopped at the association
and was never tested past it. Measured this round: a landed artifact's own
``/OpenAction`` ``/JS`` script DOES run automatically when opened by that
association, confirmed via DevTools Protocol against the real handler
binary and isolated from generic browser/extension chrome by a negative
control; see :data:`_ARTIFACT_EXTENSIONS`'s own docstring for the full
method and what a companion network-egress probe did NOT show. ``.pdf`` is
removed, pinned like ``.xml`` before it, and the untested rationale is
retracted rather than kept as a plausible-sounding maybe. Second,
:func:`_receipt_type_label`'s "accepted trade" paragraph told the operator
they could use the "copied from" line as a fallback hint for an excluded
type's real extension — true only when that line's OWN basename escapes
redaction. When the copied artifact's original name is ALSO
credential-shaped, independent of carrying an excluded suffix, both lines
collapse to their own fixed constants and the row carries no recoverable
hint at all; :func:`_receipt_type_label`'s docstring now states this as the
actual limit rather than leaving the fallback implied to always work.
"""
from __future__ import annotations

import html
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# tunables (no magic numbers)
# --------------------------------------------------------------------------- #
OUTBOX_DIRNAME = "Outbox"
DONE_MARKER = ".done"
#: Slug budget. The folder name is "<10-char date>-<slug>" and artifacts nest one
#: level deeper, so a 60-char slug leaves room for a long artifact basename
#: inside a deep vault path (Windows legacy MAX_PATH is 260).
MAX_SLUG_CHARS = 60
#: A run can legitimately produce many files; a runaway loop can produce
#: thousands. Cap the copy so the Outbox can never become the disk-filler.
MAX_ARTIFACTS = 50
#: Per-file copy ceiling. Larger artifacts are NAMED in the receipt with their
#: real path but not copied — an honest pointer beats a stalled completion hook.
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]")
#: Windows reserved device names — a folder called ``CON`` is not creatable.
_WIN_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})
#: DEC-34c round 4 (AC-1a). A FIXED, code-defined allowlist of artifact
#: suffixes considered known-safe to carry onto a copied artifact's basename.
#: :func:`_artifact_extension` matches a source's suffix against this table
#: CASE-INSENSITIVELY and always emits the table's OWN lowercase spelling —
#: never the source's original casing — so not even a one-bit (upper/lower)
#: channel of the source's own bytes reaches the destination name. A suffix
#: absent from this table (or absent entirely) yields NO destination suffix;
#: it is never passed through raw.
#:
#: round 5 correction (DEC-34c, AC-1) — the claim below was FALSE for three
#: members. Round 4's comment here said this table "deliberately excludes
#: anything a stock Windows install will EXECUTE on double-click" while
#: still containing ``.html``/``.htm``/``.svg``: a stock Windows install
#: ``ftype``'s all three to a web browser. Measured directly on this
#: machine (``assoc``/``ftype``, not taken on faith): ``.html``/``.htm`` ->
#: ProgId ``htmlfile``, ``.svg`` -> ProgId ``svgfile``, both ``ftype``'d to
#: ``iexplore.exe``; many other Windows 11 installs instead resolve the
#: same three to Edge's ``MSEdgeHTM`` — the SPECIFIC browser is a matter of
#: that machine's file-association history, but the property that matters
#: is not: a stock double-click opens SOME script-capable browser either
#: way, and it runs the file's own ``<script>``. Removed — pinned
#: end-to-end, with a fixture whose content is a real ``<script>`` tag, by
#: ``test_no_admitted_suffix_lets_a_script_tag_survive_to_the_outbox`` in
#: ``tests/test_rutl1_intake_and_outbox.py``.
#:
#: round 6 correction (DEC-34c, AC-2) — the claim below was ALSO false, for
#: a fourth member this table still admitted: ``.xml``. Measured directly
#: on THIS machine, by registry rather than by ``assoc``/``ftype``
#: (``assoc``/``ftype`` report the machine-wide CLASS default, which a
#: per-user ``UserChoice`` override — set here — takes priority over):
#: ``HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.xml\
#: UserChoice`` -> ProgId ``MSEdgeHTM`` -> ``C:\Program Files
#: (x86)\Microsoft\Edge\Application\msedge.exe``. Confirmed past the
#: association lookup, not stopped at it: a landed ``artifact-1.xml``
#: carrying a standards-legal same-document stylesheet reference
#: (``<?xml-stylesheet type="text/xsl" href="#s"?>`` naming an XSLT
#: template, inside the SAME file, whose output includes a ``<script>``
#: tag) run through ``msedge.exe --headless --dump-dom`` — the
#: association's OWN binary, not a substitute — came back with the
#: ``<script>``'s side effect already applied: the element it targeted read
#: ``SCRIPT-EXECUTED-FROM-OUTBOX`` in the dumped DOM, not the fixture's
#: original placeholder text. Same property as round 5's three, same fix:
#: removed. Pinned end-to-end, content untouched but the dangerous suffix
#: gone, by
#: ``test_no_admitted_suffix_lets_an_xml_stylesheet_script_survive_to_the_outbox``
#: in ``tests/test_rutl1_intake_and_outbox.py``.
#:
#: round 7 correction (DEC-34c, AC-1) — round 6 left ``.pdf`` admitted as a
#: NAMED residual, reasoning that ``UserChoice`` resolves it (ProgId
#: ``MSEdgePDF``) to the SAME ``msedge.exe`` the four removed suffixes used,
#: but "through a distinct, PDF-specific viewing surface" — a claim its own
#: text admitted stopped at the association and was never tested past it.
#: An untested inference stated next to three measured facts reads as a
#: fourth measured fact; it was not one. Measured this round, not inferred:
#: a PDF whose ``/OpenAction`` runs a document-level ``/JS`` script (four
#: independently try/caught probes — ``Doc.submitForm`` to a
#: ``127.0.0.1`` listener, ``app.alert(...)``, a deliberately-undefined
#: function call, and a ``this.info.Title`` write) was landed through the
#: REAL production chain (``collect_artifact_paths`` -> ``write_outbox``,
#: module identity of ``systemu.runtime.outbox`` asserted first) and the
#: resulting ``artifact-1.pdf`` opened with ``msedge.exe --headless=new``
#: — the association's own binary, not a substitute. DevTools Protocol,
#: attached to every target Edge created for the file (the top page, the
#: built-in PDF-viewer extension's "webview" frame at
#: ``chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/edge_pdf/
#: index.html`` — Edge's own "Chrome PDF Viewer" — and its nested content
#: "iframe"), recorded a real ``Page.javascriptDialogClosed`` event on the
#: UNFORCED, natural first load, before any reload or other interaction:
#: ``app.alert`` asked the browser to show a dialog, which headless
#: Chrome's default policy auto-dismissed (``result: false``) — reproduced
#: on two independent fresh launches. A negative control — byte-identical
#: fixture and procedure, ``/JS`` body replaced with an inert comment —
#: produced ZERO dialog events in the same observation window, isolating
#: the effect to the script rather than to generic Edge/extension chrome.
#: The ``Doc.submitForm`` network probe, by contrast, never reached its
#: listener in 15+ seconds across three launches. Measured this round via
#: CDP, no try/catch around the call, output captured in call order and
#: reproduced across two independent launches: ``TYPES submitForm=function
#: launchURL=undefined viewerType=msdcpdf viewerVersion=8`` ->
#: ``PRE_SUBMIT`` -> ``POST_SUBMIT_NO_THROW``. So ``Doc.submitForm`` IS
#: implemented, IS callable, and does NOT throw in this build
#: (PDFium/``msdcpdf``, viewer version 8) — it simply produced no request
#: the listener observed, and WHY was not determined: blocked by policy,
#: unimplemented past the point this probe reaches, or some other reason
#: are all still open, and this probe does not distinguish among them.
#: (``app.launchURL``, separately, genuinely IS ``undefined`` in this
#: build — narrower, and also not previously stated here.) The network-
#: exfiltration question this residual originally raised therefore stays
#: unanswered in the affirmative. Deciding this table never needed that
#: answer: the property that removed the other four suffixes was never
#: "and it can reach the network" — the ``.xml`` pin above proves
#: execution via an in-document DOM mutation, not exfiltration — it was
#: "a plain double-click runs the file's own script, no further gesture."
#: ``.pdf`` meets that bar. Removed — leaving 51 simple admitted suffixes
#: — and pinned end-to-end, content untouched but the dangerous suffix
#: gone, by
#: ``test_no_admitted_suffix_lets_a_pdf_openaction_script_survive_to_the_outbox``
#: in ``tests/test_rutl1_intake_and_outbox.py``. The "distinct viewing
#: surface" rationale is RETRACTED, not narrowed: it was never measured,
#: and what was measured this round contradicts the safety it implied.
#:
#: What this table now actually claims (round 7 restatement, DEC-34c AC-3 —
#: the round 5 version of this very paragraph was ITSELF an instance of the
#: defect it exists to name: it asserted a property for every member while
#: only three had been individually checked; re-swept from a fresh
#: registry enumeration of every CURRENT member this round rather than
#: carried forward from round 6's text):
#:
#: No member's stock association is an interpreter, shell, or macro engine
#: reachable by a plain double-click with no further click in between. Nor
#: does any member's stock association run the file's own script on that same
#: plain double-click, no further gesture — the property that made
#: ``.html``/``.htm``/``.svg``/``.xml``/``.pdf`` above dangerous and got them
#: removed. Four of those five ran it through a browser's GENERAL HTML/script
#: engine; ``.pdf`` did not — what was measured for it was a distinct,
#: PDF-specific Acrobat-JS surface (see the round 7 correction above), not
#: the general engine. Both are the same "a plain double-click runs the
#: file's own script, no further gesture" property, not the same engine, and
#: both are gone. With ``.pdf`` gone, this is now true of every remaining
#: member WITHOUT exception. Round 6's text carried a one-member carve-out
#: here (``.pdf``, admitted as an unresolved residual); a fresh sweep this
#: round — every current member's real registry association re-enumerated
#: directly, not read off round 6's prose — resolved exactly ONE member to a
#: browser executable (``.pdf`` -> ``msedge.exe``, see the round 7 correction
#: above), and that member is now removed, leaving zero.
#:
#: It does NOT claim anything about macro-CAPABLE office formats
#: (``.doc``/``.xls``/``.ppt`` remain members; a document's auto-run macro
#: needs an explicit "Enable Content" click beyond the double-click that
#: opens the file — a different, Office-mitigated risk this table does not
#: attempt to re-solve), and it says nothing about a copied file's
#: CONTENTS, which are never scanned — this table gates the SUFFIX only.
#:
#: Nor did "every member opens in a passive viewer, player, or text
#: editor" hold for all of them. Measured by the same registry lookup —
#: re-run this round (AC-3) over every member the table CURRENTLY holds,
#: not assumed to still read the same as round 6's text — 12 of this
#: table's members do not reach a working viewer AT ALL. 11 have NO
#: stock association whatsoever — neither a per-user ``UserChoice`` nor an
#: ``HKEY_CLASSES_ROOT`` class default names a ProgId, so a double-click
#: opens Windows' "How do you want to open this file?" picker rather than
#: launching anything — ``.md`` ``.tex`` ``.tsv`` ``.parquet`` ``.psd``
#: ``.json`` ``.yaml`` ``.yml`` ``.toml`` ``.sql`` ``.ipynb``. A twelfth,
#: ``.epub``, DOES have a ``UserChoice`` ProgId
#: (``AppXvepbp3z66accmsd0x877zbbxjctkpr6t``) but that ProgId has NO
#: registration anywhere under ``HKEY_CLASSES_ROOT`` on this machine
#: (confirmed by direct lookup — the key itself is absent, not merely
#: unresolvable): the app that once claimed it is no longer installed, and
#: Windows left the per-user preference pointing at nothing — in practice
#: this also falls back to the picker (or an error), not a working viewer.
#: None of these 12 is a script-execution risk — the picker/error path runs
#: nothing — so they stay admitted (the copy is still a legitimate,
#: correctly-typed deliverable; the operator just cannot open it with one
#: click), but "opens in a passive viewer" was false for them and is
#: corrected here rather than left standing.
#:
#: round 5 widening (AC-2): 14 additional inert-data types admitted below
#: (marked "round 5" in their category). Admission test applied to each
#: candidate: (a) its stock Windows file association, if any, is a passive
#: viewer/player/reader/text-editor — never an interpreter or a
#: macro-capable engine reachable by opening the file with no further
#: click; and (b) the format itself has no standardised embedded-script or
#: macro auto-run capability. ``.xlsm``/``.docm`` (macro-ENABLED Office —
#: fails (a): a document macro's auto-run behaviour is the same
#: Office-mediated risk noted above, and these two invite it BY THEIR
#: EXTENSION) were examined and deliberately left OUT, along with the rest
#: of round 5's "also untyped" survey (``.drawio`` ``.pem`` ``.bin``
#: ``.m4a`` ``.dwg`` ``.numbers`` ``.key`` ``.rar`` ``.bz2``), which were
#: not independently vetted against test (a)/(b) this round. Widen
#: deliberately, per-type — never by unioning a whole survey.
_ARTIFACT_EXTENSIONS = frozenset({
    # documents — ".pdf" removed round 7, see the docstring above this table
    ".doc", ".docx", ".rtf", ".odt", ".txt", ".md",
    ".tex", ".epub",                                            # round 5
    # spreadsheets
    ".xls", ".xlsx", ".csv", ".tsv", ".ods",
    ".parquet",                                                 # round 5
    # presentations
    ".ppt", ".pptx", ".odp",
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".ico",
    ".heic", ".psd",                                            # round 5
    # structured data / text
    ".json", ".yaml", ".yml", ".toml", ".ini", ".log",
    ".sql", ".jsonl",                                           # round 5
    # archives — see _COMPOUND_ARTIFACT_EXTENSIONS for ".tar.gz"-shaped names
    ".zip", ".tar", ".gz", ".7z",
    # audio / video
    ".mp3", ".mp4", ".wav", ".mov", ".avi",
    ".webm", ".flac", ".mkv",                                   # round 5
    # calendar / contacts / mail — round 5
    ".eml", ".ics", ".vcf",
    # notebooks — round 5
    ".ipynb",
})

#: DEC-34c round 5 (AC-3). A compound archive suffix's FINAL component alone
#: misrepresents the archive's real type: ``archive.tar.gz`` is a
#: gzip-compressed TAR, not a bare gzip file, and the two are opened with
#: different tools and different expectations — collapsing to ``.gz`` lost
#: the ``.tar`` and left the artifact effectively mistyped.
#: :func:`_artifact_extension` (via :func:`_display_suffix`) checks a
#: source's last TWO dot-separated components against this table BEFORE
#: falling back to its single final suffix, so exactly these three
#: gzip/bzip2/xz-over-tar shapes survive whole. Anything else that happens
#: to have two dots (``data.pkl.gz``, which is a compressed pickle, not a
#: tar archive) still collapses to its single final suffix, unchanged from
#: round 4 — this table intentionally special-cases only the three tar
#: compression pairings, not every possible double suffix.
_COMPOUND_ARTIFACT_EXTENSIONS = frozenset({
    ".tar.gz", ".tar.bz2", ".tar.xz",
})

#: The actual "is this suffix admitted" membership test — round 4's simple
#: table plus round 5's compound shapes, precomputed once.
_ALL_ADMITTED_EXTENSIONS = _ARTIFACT_EXTENSIONS | _COMPOUND_ARTIFACT_EXTENSIONS
_REDACTED = "[redacted - looked like a credential]"
#: Appended when a ``max_len`` cap fires, so a shortened value is not read as a
#: complete one. Counted INSIDE the cap — ``len(result) <= max_len`` always.
_TRUNCATED = "…"

_OK_STATUSES = frozenset({"success"})
_WARN_STATUSES = frozenset({"partial", "spend_cap_reached", "waiting_on_tools",
                            "pending_decision", "cancelled"})


# --------------------------------------------------------------------------- #
# path safety
# --------------------------------------------------------------------------- #

class TrustedIdentity(str):
    """A path-component source KNOWN to be a non-prompt identity: a
    ``task_id``, a per-artifact ordinal, or a value drawn from a fixed,
    code-defined enum — never raw prompt text or any other untrusted
    free-text field (an artifact's original, caller-chosen basename included).

    DEC-34c. A filename cannot be made safe by FILTERING untrusted text:
    ``safe_component``'s allowlist strips punctuation but preserves the
    entire alphanumeric alphabet a credential is made of, and routing the
    text through :func:`redact` first does not save it either — measured on
    this tree (``tests/test_dec33_gate5_outbox_slug_redaction.py``), that
    still collapsed several ordinary prompts to one constant while missing
    every real credential shape tried against it (a pasted PEM key
    included). The fix is to never hand a path-component builder untrusted
    text in the first place.

    Subclassing ``str`` — rather than a bare ``typing.NewType``, which is
    erased at runtime and would let a plain ``str`` slip through unnoticed —
    makes the distinction a REAL runtime type: :func:`safe_component` rejects
    anything that is not a ``TrustedIdentity`` with a ``TypeError``, so
    wrapping a value is a decision a caller has to make explicitly, not a
    hope that they remembered to sanitize it. Wrapping is NOT sanitizing —
    ``safe_component`` still does that — it only asserts "this string did
    not come from the prompt, or from any other untrusted free-text field",
    which is true of ``task_id`` and an ordinal, and is NOT true of ``prompt``
    or a copied file's original name.
    """
    __slots__ = ()


def safe_component(text: TrustedIdentity, *, fallback: str = "task") -> str:
    """Sanitize a TRUSTED identity string into ONE filesystem path component.

    ASCII allowlist (``A-Za-z0-9_.-``) + explicit ``..`` neutralization + a
    Windows reserved-device-name guard + a length cap. Follows the shape of the
    shipped ``receipts_store._safe_eid`` and adds the two things it lacks: the
    length bound and the reserved-name guard.

    **DEC-34c — ``text`` must be a** :class:`TrustedIdentity`, **never a bare**
    ``str``/``Any``. Passing an UNWRAPPED value — a bare ``str`` (prompt text,
    a copied artifact's original name, or anything else untrusted) — is an
    INTERFACE ERROR raised HERE, rather than a runtime hope that a
    shape-detector upstream caught it.

    **round 4 correction — precisely scoped, not overclaimed (DEC-34/DEC-34c):**
    that ``TypeError`` catches exactly one mistake: forgetting to wrap a
    value at all. ``TrustedIdentity(...)`` performs NO content check on
    construction — it is a plain ``str`` subclass — so wrapping untrusted
    text (``TrustedIdentity(prompt)``) satisfies this function's ``isinstance``
    check and passes straight through; nothing here can tell a properly-wrapped
    ``task_id`` from a carelessly-wrapped ``prompt``. The actual guarantee is
    NOT enforced by this function — it lives entirely at the two call sites in
    this module (:func:`write_outbox`, :func:`_copy_artifacts`), which wrap
    only ``task_id`` and the per-artifact ordinal, never ``prompt``/
    ``src.name`` (pinned by
    ``test_both_safe_component_call_sites_pass_a_TrustedIdentity_by_source``
    in ``tests/test_dec33_gate5_outbox_slug_redaction.py`` — itself an
    honestly-scoped source-shape tripwire, not a content proof; see that
    test's own docstring). Wrap the value explicitly: ``TrustedIdentity(task_id)``
    or ``TrustedIdentity(f"artifact-{i}")`` — never ``TrustedIdentity(prompt)``
    or ``TrustedIdentity(src.name)``, both of which this function CANNOT catch.

    Never returns ``""``, ``"."``, ``".."``, or a name a path join could escape.
    """
    if not isinstance(text, TrustedIdentity):
        raise TypeError(
            f"safe_component() requires a TrustedIdentity, not "
            f"{type(text).__name__}. Wrap a task_id / ordinal / fixed-enum "
            f"value explicitly with TrustedIdentity(...) — raw prompt text "
            f"or any other untrusted free-text field must never reach a "
            f"filename (DEC-34c AC-1/AC-3).")
    s = _UNSAFE_COMPONENT.sub("_", str(text or "").strip())
    # Neutralize traversal AFTER the allowlist pass (which keeps "."), and LOOP:
    # a single replace turns "...." into "..", which is still traversal.
    while ".." in s:
        s = s.replace("..", "_")
    s = s.strip("._")
    if not s:
        return fallback
    s = s[:MAX_SLUG_CHARS].strip("._")
    if not s:
        return fallback
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s


def _resolved(p: Any) -> Path:
    try:
        return Path(p).resolve()
    except Exception:
        return Path(os.path.abspath(str(p)))


def is_within(candidate: Any, root: Any) -> bool:
    """True iff ``candidate`` resolves inside ``root``.

    ``commonpath`` on resolved paths — never a string prefix, which would treat
    ``/Outbox-evil`` as inside ``/Outbox``. Fail-closed: any error -> False.
    """
    try:
        c = _resolved(candidate)
        r = _resolved(root)
        if c == r:
            return True
        return os.path.commonpath([str(c), str(r)]) == str(r)
    except Exception:
        return False


def vault_dir(vault: Any) -> Path:
    """The vault DIRECTORY for ``vault``, which may be a Vault object (the
    runtime callers) or a plain path (the CLI and tests).

    Do NOT collapse this to ``getattr(vault, "root", None) or vault``:
    ``pathlib.Path`` HAS a ``.root`` attribute — it is the FILESYSTEM root
    (``"\\"`` / ``"/"``), not a vault. That idiom silently resolves a Path
    argument to the drive root, which would put the Outbox at ``C:\\Outbox``
    and every confinement check would pass while pointing at the wrong disk.
    Check the path types FIRST.
    """
    if isinstance(vault, (str, Path)):
        return Path(str(vault))
    for attr in ("root", "vault_dir"):
        value = getattr(vault, attr, None)
        if value is not None:
            return Path(str(value))
    return Path(str(vault))


def outbox_root(vault: Any) -> Path:
    return vault_dir(vault) / OUTBOX_DIRNAME


# --------------------------------------------------------------------------- #
# atomic write (tmp + os.replace, same filesystem by construction)
# --------------------------------------------------------------------------- #

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
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# redaction — REUSE the shipped fences, never a third mechanism
# --------------------------------------------------------------------------- #

def _cap(out: str, max_len: Optional[int]) -> str:
    """Apply a length cap to ALREADY-REDACTED text.

    Private and taking only the masked form, so there is no way to reach it with
    a raw value — the cap cannot be applied in the wrong order by construction.
    The result is always at most ``max_len`` characters, ellipsis included, so
    callers sizing a fixed-width field get what they asked for.

    A cap too small to hold ``_REDACTED`` truncates the MARKER. That is a caller
    mistake, but it resolves by emitting a prefix of a constant, which discloses
    nothing — never by falling back to the value.
    """
    if max_len is None or len(out) <= max_len:
        return out
    if max_len <= 0:
        return ""
    return out[:max_len - 1] + _TRUNCATED


def redact(text: Any, *, max_len: Optional[int] = None) -> str:
    """Make ``text`` safe(r) to render into a file that leaves the process.

    Two shipped fences, in order:
      1. ``ask_promotion._value_is_secret`` — if the WHOLE value reads as a
         credential, drop it rather than emit a partly-masked husk.
      2. ``messaging.gateway.mask_outbound`` — span-level redaction of embedded
         secrets inside otherwise-ordinary prose.

    Fail-closed: if either import fails, the value is dropped.

    **DEC-31 — ``max_len`` caps the MASKED form, never the input.** Both fences
    above are shape rules, and every shape they know spans more characters than
    a typical log-line cap: a 230-char JWT here returns the marker, while the
    same token sliced to 64 characters first returns 64 CLEAR characters,
    because the pattern needs all three segments and the slice removes two. So
    a caller that needs a bounded string must ask for it HERE — ``redact(v,
    max_len=64)`` — instead of writing ``redact(v[:64])``, which is the leak
    this parameter exists to make unnecessary. ``tools/lint_redaction_order.py``
    is the gate that keeps the second form out of the tree.

    ``max_len=None`` (the default) means no cap and is byte-identical to the
    signature every existing caller was written against. The sentinel is
    ``None``, tested with ``is None`` rather than falsiness, so an explicit
    ``max_len=0`` means "emit nothing" instead of silently becoming "no cap".
    A negative cap raises: ``out[:-5]`` is valid Python that chops the TAIL,
    i.e. a cap that quietly does the opposite of what it says.
    """
    if max_len is not None:
        if isinstance(max_len, bool) or not isinstance(max_len, int):
            raise TypeError(f"max_len must be an int or None, got {max_len!r}")
        if max_len < 0:
            raise ValueError(
                f"max_len must be >= 0, got {max_len} — a negative slice would "
                f"truncate from the END rather than capping")
    s = "" if text is None else str(text)
    if not s:
        return ""
    try:
        from systemu.runtime.ask_promotion import _value_is_secret
        if _value_is_secret(s):
            return _cap(_REDACTED, max_len)
    except Exception:
        logger.debug("[Outbox] secret check unavailable - dropping value", exc_info=True)
        return _cap(_REDACTED, max_len)
    try:
        from systemu.messaging.gateway import mask_outbound
        return _cap(mask_outbound(s), max_len)
    except Exception:
        logger.debug("[Outbox] mask unavailable - dropping value", exc_info=True)
        return _cap(_REDACTED, max_len)


def _esc(text: Any) -> str:
    """Redact THEN HTML-escape. Order matters: escaping first would break the
    redactor's patterns (``sk-...`` survives escaping, ``&quot;`` noise does
    not, and a masked span must not be re-interpretable as markup).

    That "order matters" was written here first and the tree shipped the
    inverted order anyway, elsewhere, twice — which is why the rule is now
    DEC-31 and is enforced by ``tools/lint_redaction_order.py`` rather than by
    this paragraph. Truncation is the same hazard as escaping and has the same
    answer: ask :func:`redact` for a bounded result via ``max_len``, never slice
    the value on the way in.
    """
    return html.escape(redact(text), quote=True)


def _esc_path(path: Any) -> str:
    """Redact THEN HTML-escape an on-disk PATH — the parent directory and the
    basename are redacted as two INDEPENDENT values, never one compound
    string handed to :func:`redact` whole.

    DEC-34c round 4 (AC-2). ``_esc`` alone, applied to a whole absolute path
    (the shape ``render_receipt`` used through round 3), made two DIFFERENT
    artifacts' "copied from" lines identical whenever the run's OWN directory
    happened to contain anything shape-matching. Measured on this tree: a
    path through an ordinary, credential-free run directory named with a 40+
    hex-character id collapses via ``_value_is_secret`` -> ``mask_outbound``
    treating the WHOLE string as one credential the instant ANY substring
    matches — so ``.../<40-hex-run-id>/Q3-report.pdf`` and
    ``.../<same-id>/revenue-chart.png`` both render the identical constant,
    with no credential anywhere in the scenario, and become mutually
    indistinguishable AND individually unidentifiable.

    Splitting before redacting fixes this without a new detector or a
    weaker one: the directory segment and the filename are still each fully
    redacted (and, if triggered, still each collapse to the marker) — a
    hex-shaped run directory just no longer drags an ORDINARY filename down
    with it. A genuinely credential-shaped BASENAME (an artifact saved as
    ``sk-live....txt``) still collapses on its own, exactly as before this
    change — this does not weaken that case, only decouples it from the
    directory it happens to sit in.

    DEC-34c round 5 (AC-5). The join used to be a raw f-string —
    ``f"{parent}{os.sep}{base}"`` — which is only correct when ``parent``
    does NOT already end in a separator. It always does at a filesystem
    root (``Path("C:\\x.pdf").parent`` is already ``"C:\\"``; a
    POSIX-style ``"/x.pdf"`` parses the same way under ``WindowsPath``) and
    is ``"."`` for a bare relative filename with no real directory — so the
    raw f-string DOUBLED the root's own separator (``C:\\x.pdf`` rendered
    as ``C:\\\\x.pdf``, and a POSIX-style root rendered with two leading
    backslashes — a UNC path to a browser) and, for a bare relative name,
    SYNTHESISED a ``.\\`` prefix that was never in the original path.
    Fixed with :func:`os.path.join`, which only inserts a separator when
    ``parent`` does not already end in one, plus an explicit check for
    ``parent in (".", "")`` — the one shape where joining AT ALL would
    synthesise a separator with nothing real to attach to (a bare relative
    filename, or the empty-path degenerate case where there is also no
    basename). Ordinary paths — a relative file inside a real
    subdirectory, an absolute path with a real directory under the drive —
    already rendered correctly and are BYTE-IDENTICAL before and after this
    change; only the root/bare-name edge cases move.
    """
    p = Path(str(path))
    parent = redact(str(p.parent))
    base = redact(p.name)
    joined = base if parent in (".", "") else os.path.join(parent, base)
    return html.escape(joined, quote=True)


# --------------------------------------------------------------------------- #
# receipt
# --------------------------------------------------------------------------- #

_RECEIPT_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 2rem; background: #fbfbfd; color: #14151a; }
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; word-wrap: break-word; }
.sub { color: #6b7280; font-size: .85rem; margin: 0 0 1.5rem; }
.badge { display: inline-block; padding: .18rem .6rem; border-radius: 999px;
         font-size: .78rem; font-weight: 600; }
.ok { background: #dcfce7; color: #14532d; }
.bad { background: #fee2e2; color: #7f1d1d; }
.warn { background: #fef3c7; color: #78350f; }
section { background: #fff; border: 1px solid #e6e7ea; border-radius: 10px;
          padding: 1rem 1.15rem; margin: 0 0 1rem; }
h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .07em;
     color: #6b7280; margin: 0 0 .6rem; }
pre { white-space: pre-wrap; word-wrap: break-word; margin: 0; font: inherit; }
ul { margin: 0; padding-left: 1.1rem; }
li { margin: .35rem 0; }
code { background: #f3f4f6; padding: .08rem .3rem; border-radius: 4px;
       font-size: .88em; word-wrap: break-word; }
footer { color: #9ca3af; font-size: .78rem; margin-top: 1.5rem; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e7ea; }
  section { background: #171a21; border-color: #262b36; }
  .ok { background: #14532d; color: #dcfce7; }
  .bad { background: #7f1d1d; color: #fee2e2; }
  .warn { background: #78350f; color: #fef3c7; }
  code { background: #262b36; }
}
"""


def _badge_class(status: Any) -> str:
    s = str(status or "").lower()
    if s in _OK_STATUSES:
        return "ok"
    if s in _WARN_STATUSES or s.startswith("suspended"):
        return "warn"
    return "bad"


def render_receipt(*, task_id: str, prompt: str, status: str, summary: str,
                   artifacts: Sequence[Tuple[str, str]],
                   execution_id: Optional[str] = None,
                   produced_at: Optional[str] = None,
                   note: str = "") -> str:
    """A standalone, network-free receipt for ONE run.

    ``artifacts`` is a sequence of ``(name_in_folder, original_absolute_path)``.

    Client-mode by construction: no costs, no model names, no internal notes.
    U-4's renderer — which owns the full worklog frame including the cost and
    external-receipt sections and the client-mode FLAG — does NOT exist at this
    HEAD (it folds into R-P3b, unshipped), so this is a deliberately minimal
    receipt written here, NOT a call into it. When U-4 lands this should be
    REPLACED by a call into it rather than grown into a second renderer.

    Every interpolated value is redacted then HTML-escaped by :func:`_esc` —
    except ``original``, which goes through :func:`_esc_path` (DEC-34c round
    4, AC-2): the parent directory and the basename are redacted as two
    independent values so one artifact's directory cannot drag an unrelated
    artifact's filename down into the same collapsed constant. See
    :func:`_esc_path`'s docstring for the measured failure this replaces.

    DEC-34c round 5 (AC-4), corrected round 6 (AC-1). Every row also states
    a type on its own line ("original type: …"). Round 5 built that text by
    redacting :func:`_display_suffix` — a SLICE of the untrusted original
    name — which let a slice-shaped fragment (a JWT's own signature
    segment, taken alone) defeat the shape-based redactor that correctly
    caught the intact original two lines above it in the same receipt; see
    :func:`_receipt_type_label`'s docstring for the measured leak and the
    fix. The line now renders :func:`_receipt_type_label`'s result, which is
    always drawn from one of three code-defined sources and never from a
    transformation of ``original``: the matching
    :data:`_ARTIFACT_EXTENSIONS`/:data:`_COMPOUND_ARTIFACT_EXTENSIONS`
    member, or - Phase 1d - the matching :data:`_WITHHELD_EXTENSION_LABELS`
    label plus its fixed rename remedy, or the fixed literal
    :data:`_TYPE_NOT_PRESERVED` when neither table knows the suffix.

    Phase 1d correction: round 6 wrote here that the "not preserved" literal
    renders "unconditionally, for every row, admitted or excluded alike",
    and retired round 5's goal for an EXCLUDED type ("state its real
    extension so the operator can rename it") as unreachable. Both
    statements are now narrower. The goal is met for the suffixes a second
    CLOSED table names, without re-introducing round 5's mechanism: the
    untrusted suffix is a membership key, the rendered bytes are the table's
    own values, and a suffix neither table knows still renders the fixed
    literal and nothing else. See :func:`_receipt_type_label`.

    A withheld row's line therefore contains TWO ``<code>`` spans (the type,
    and the name to rename the landed file to) rather than one - a reader
    matching this markup with a regex that assumes a single span will see
    only the type half.
    """
    rows: List[str] = []
    for name, original in artifacts or ():
        type_html = _receipt_type_label(original, name)
        rows.append(
            f"<li><code>{_esc(name)}</code>"
            f"<br><span class='sub' style='margin:0'>copied from {_esc_path(original)}</span>"
            f"<br><span class='sub' style='margin:0'>original type: "
            f"{type_html}</span></li>"
        )
    art_html = ("<ul>" + "".join(rows) + "</ul>") if rows else \
        "<p class='sub' style='margin:0'>No files were produced.</p>"

    note_html = (f"<section><h2>Not copied</h2><pre>{_esc(note)}</pre></section>"
                 if note else "")
    exec_html = (f"<section><h2>Execution</h2><pre>{_esc(execution_id)}</pre></section>"
                 if execution_id else "")

    return (
        "<!DOCTYPE html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>systemu receipt - {_esc(task_id)}</title>"
        f"<style>{_RECEIPT_CSS}</style></head><body><main>"
        f"<h1>{_esc(prompt)[:400] or 'Task'}</h1>"
        f"<p class='sub'>{_esc(produced_at or '')} &middot; "
        f"<span class='badge {_badge_class(status)}'>{_esc(status)}</span></p>"
        f"<section><h2>Outcome</h2><pre>{_esc(summary) or '-'}</pre></section>"
        f"{note_html}"
        f"<section><h2>Files</h2>{art_html}</section>"
        f"{exec_html}"
        "<footer>Generated by systemu. This file is self-contained - it loads "
        "nothing from the network. Secret-shaped values are redacted; a "
        "shapeless credential can still survive, and the CONTENTS of the copied "
        "files above are not scanned.</footer>"
        "</main></body></html>\n"
    )


def _failure_note(*, prompt: str, status: str, summary: str,
                  committed: Sequence[str] = ()) -> str:
    """The honest handoff text: what happened, what already took effect in the
    world, and what the operator must do. Never a cheerful non-answer."""
    lines = [
        "This task did not complete.",
        "",
        f"Task    : {redact(prompt)}",
        f"Status  : {redact(status)}",
        "",
        "What happened",
        "-------------",
        redact(summary) or "(no outcome text was recorded)",
        "",
        "Effects already committed",
        "-------------------------",
    ]
    if committed:
        lines += [f"  - {redact(c)}" for c in committed]
        lines += ["", "These already took effect and were NOT rolled back."]
    else:
        lines.append("  (none recorded)")
    lines += [
        "",
        "What is needed from you",
        "-----------------------",
        "Open systemu and check this task for a parked question or a gate",
        "awaiting approval. If nothing is waiting, the run ended without",
        "finishing its objective and needs to be re-run or re-scoped.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the writer
# --------------------------------------------------------------------------- #

def _unique_dir(root: Path, base: str) -> Path:
    """``root/base``, or ``root/base-2``, ``-3``... if taken. Collide-safe: two
    tasks with the same slug on the same day never share a folder (which would
    let one run's ``.done`` seal another run's half-written artifacts)."""
    candidate = root / base
    if not candidate.exists():
        return candidate
    for n in range(2, 1000):
        candidate = root / f"{base}-{n}"
        if not candidate.exists():
            return candidate
    return root / f"{base}-{os.getpid()}"


def _display_suffix(path: Any) -> str:
    """The source's REAL suffix — compound-aware, but NOT filtered by
    :data:`_ARTIFACT_EXTENSIONS`.

    **DEC-34c round 6 correction.** Through round 5 this had a second
    caller: :func:`render_receipt` used this UNFILTERED result directly, so
    the operator could learn an excluded artifact's real type. That was the
    bug — this function's return value is a SLICE of untrusted text (a
    source suffix is exactly as untrusted as the prompt; see this module's
    own docstring), and round 5's receipt line redacted the slice instead
    of the whole original value, which let a slice-shaped fragment (a JWT's
    own signature segment, taken alone) defeat the shape-based redactor
    that correctly caught the intact original two lines above it in the
    same receipt. :func:`render_receipt` no longer calls this directly —
    only :func:`_artifact_extension` does, and its result is filtered
    through the allowlist before anything downstream sees it (see
    :func:`_receipt_type_label`). This function itself is unchanged and
    still correct for that one remaining, filtered use.

    Checks the source's last two dot-separated components against
    :data:`_COMPOUND_ARTIFACT_EXTENSIONS` first (so ``archive.tar.gz``
    reports ``.tar.gz``, not the misleading ``.gz``), then falls back to the
    plain final suffix. Returns ``""`` when the source has no suffix at all.
    Never raises — an unparseable ``path`` yields ``""``, the same as "no
    suffix", rather than propagating into a receipt render.
    """
    try:
        p = Path(str(path))
    except Exception:
        return ""
    suffixes = p.suffixes
    if len(suffixes) >= 2:
        compound = "".join(suffixes[-2:]).lower()
        if compound in _COMPOUND_ARTIFACT_EXTENSIONS:
            return compound
    return p.suffix.lower()


def _artifact_extension(src: Path) -> str:
    """The DESTINATION suffix for a copied artifact (DEC-34c round 4, AC-1a;
    compound handling added round 5, AC-3).

    :func:`_display_suffix` computes the source's real suffix — checking the
    three recognised tar-compression compounds first, see
    :data:`_COMPOUND_ARTIFACT_EXTENSIONS` — and this function matches that
    CASE-INSENSITIVELY against :data:`_ALL_ADMITTED_EXTENSIONS` (the union
    of the simple and compound tables), a fixed, code-defined enum — never
    the source's own suffix passed through as-is. A match returns the
    TABLE's own canonical lowercase spelling (so a source named ``.DOCX``
    and one named ``.docx`` produce the identical destination suffix — not
    even a casing bit of the source's own bytes survives). No match — an
    unrecognised suffix, or none at all — returns ``""``: dropped, never
    passed through raw. ``.pdf`` is one such no-match since DEC-34c round 7
    (see :data:`_ARTIFACT_EXTENSIONS`'s own docstring) — a source named
    ``.pdf``/``.PDF`` now returns ``""`` like any other excluded suffix,
    not a canonicalised ``.pdf``.
    """
    ext = _display_suffix(src)
    return ext if ext in _ALL_ADMITTED_EXTENSIONS else ""


#: DEC-34c round 6 (AC-1). The ONLY text :func:`_receipt_type_label` may
#: render for a source whose suffix is not admitted. A single literal —
#: never a value built from, or containing any byte of, the untrusted
#: source name. See :func:`_receipt_type_label` for why round 5's
#: alternative (show the real, excluded suffix, redacted) was refuted.
_TYPE_NOT_PRESERVED = "(type not preserved)"

#: Phase 1d. A SECOND closed, code-defined table - the ONLY suffixes for
#: which the receipt may name a type the on-disk copy does not carry.
#:
#: **What this does NOT do.** It does not admit anything. The landing fence
#: is :data:`_ARTIFACT_EXTENSIONS` alone, via :func:`_artifact_extension`, and
#: nothing here is consulted on the copy path (:func:`_copy_artifacts`) - a
#: member of this table still lands with NO suffix, byte-identical to before
#: this table existed, and ``test_each_known_excluded_suffix_resolves_to_its_
#: own_label`` re-asserts both halves of that for every member. The two tables
#: are disjoint by construction and pinned disjoint by
#: ``test_the_label_table_is_closed_disjoint_and_ascii``.
#:
#: **Why it is safe to render (DEC-31).** The round 5 defect this module's
#: docstring narrates was rendering a SLICE of the untrusted source name
#: (:func:`_display_suffix`'s result, redacted after the slice - the exact
#: ordering DEC-31 forbids), which let a JWT's own signature segment through
#: verbatim. This table never renders a slice. The source's suffix is
#: lower-cased by :func:`_display_suffix` and then used ONLY as a dict
#: MEMBERSHIP KEY over this closed key set; the bytes that reach the receipt
#: are the matched entry's VALUE - a hardcoded ASCII literal in this file -
#: never a transformation of ``original``. A miss renders
#: :data:`_TYPE_NOT_PRESERVED`, unchanged from round 6: an unknown suffix
#: stays unnamed, which is the whole point of the enum being CLOSED.
#:
#: **Membership, and how to widen it.** The five suffixes each MEASURED to
#: run their own script on a plain Windows double-click and removed from the
#: landing table for it (rounds 5/6/7 - see :data:`_ARTIFACT_EXTENSIONS`'s
#: docstring for each measurement), plus the four ``b7500a14``'s own commit
#: message names as landing bare. ``.xlsm``/``.docm`` were considered and
#: deliberately left OUT this pass: a shipped round 6 pin
#: (``test_receipt_hides_the_extension_for_an_EXCLUDED_type_rather_than_risk_
#: a_leak``) names ``.xlsm`` as THE worked example of the honest
#: "not preserved" line, and moving it is a separate decision, not a
#: side effect of this one. Widen deliberately, per-type - never by
#: unioning a survey (the same discipline :data:`_ARTIFACT_EXTENSIONS`
#: states for itself).
_WITHHELD_EXTENSION_LABELS = {
    # measured script-executing suffixes, removed from the landing table
    ".pdf": ".pdf",
    ".html": ".html",
    ".htm": ".htm",
    ".svg": ".svg",
    ".xml": ".xml",
    # named in b7500a14's commit message as landing with no suffix at all
    ".exe": ".exe",
    ".hta": ".hta",
    ".chm": ".chm",
    ".reg": ".reg",
}

#: The fixed prose either side of a withheld type's own label. ASCII only -
#: DEC-32c: this text reaches a verdict-carrying, subprocess-capturable
#: surface, so no em dash, no typographic quote. Split out as constants so
#: the rendered sentence is assembled from literals plus exactly two
#: substitutions, both of which are themselves code-derived (the table's
#: value, and the ``artifact-<n>`` name this module built from a loop
#: ordinal via :class:`TrustedIdentity`).
_WITHHELD_REASON = ("suffix withheld: a double-click could run this file "
                    "type's own scripts")
_WITHHELD_REMEDY_TAIL = " to open it deliberately"


def _receipt_type_label(original: Any, landed_name: Any = "") -> str:
    """The receipt's "original type" text for ONE artifact row — DEC-34c
    round 6, AC-1.

    **The defect this replaces.** Round 5's version of this line computed
    :func:`_display_suffix` — the source's REAL suffix, a SLICE of the
    untrusted original name — and ran that slice through :func:`_esc`
    (redact then escape). DEC-31 requires redaction to be the FIRST
    transformation applied to untrusted text, before any slice; this line
    sliced first. Measured consequence: a source basename equal to a JWT
    (``header.payload.signature``, three dot-separated segments) has ONLY
    its SIGNATURE segment taken as "the suffix" — ``Path.suffix`` returns
    whatever follows the LAST dot. That fragment, alone, does not match the
    shape either shipped fence looks for (a JWT is recognised by its
    three-part ``eyJ…\\.…\\.…`` structure; the slice keeps one part and
    discards two), so :func:`_esc` returned it VERBATIM. The SAME
    ``redact()`` call, given the intact original two lines up in the same
    receipt (:func:`_esc_path`'s basename half), correctly recognised and
    masked the identical text — only the pre-sliced fragment escaped.

    **The fix is not a better redaction of the fragment.** DEC-37's shape:
    a rendered value must be UNCONSTRUCTIBLE from the failure/unknown
    path, not merely redacted along it. This function never hands ANY byte
    derived from ``original`` to its return value. :func:`_artifact_extension`
    already computes, from the identical source, either a member of
    :data:`_ALL_ADMITTED_EXTENSIONS` (a fixed, code-defined enum — the SAME
    function and the SAME result that decided the copy's own destination
    suffix, so this line can never claim a type the on-disk copy does not
    also carry) or ``""``. A match renders that enum member — html-escaped
    defensively, though every member is a hardcoded ASCII literal with no
    metacharacters to escape — not the observed text: the two are
    string-equal only because :func:`_artifact_extension` lower-cased and
    then matched against the table, so the rendered byte comes from the
    table, never from a transformation of ``original``. No match —
    unrecognised suffix, or none at all - falls to the second closed table,
    :data:`_WITHHELD_EXTENSION_LABELS` (Phase 1d, below), and on a miss
    THERE renders :data:`_TYPE_NOT_PRESERVED`, one fixed literal, regardless
    of what the untrusted suffix actually was. Both tables are consulted by
    MEMBERSHIP and render only their own VALUES, so the DEC-31 property is
    identical for both: no rendered byte is a slice or a transformation of
    ``original``. Deliberately NOT routed through :func:`_esc`/:func:`redact`: those
    defend untrusted CONTENT, and by this point ``ext`` is never that — it
    is a lookup result over a closed set, not a transformation of
    ``original``, so there is nothing left for a content-based fence to
    usefully check.

    **Round 6's accepted trade, and how Phase 1d narrows it.** Round 6 wrote
    here that an EXCLUDED type "is no longer individually named on this
    line", because "every way tried to keep that property still means
    redacting something DERIVED from untrusted text, which is the exact
    mechanism that just leaked." The premise was too strong, and this
    paragraph is corrected rather than left standing: naming the type does
    not require rendering anything derived from the name IF the rendered
    bytes come from a CLOSED, code-defined table and the untrusted suffix is
    used only as a membership key into it - structurally the same move
    :func:`_artifact_extension` already makes for the admitted case, one
    line up. :data:`_WITHHELD_EXTENSION_LABELS` is that table. For a suffix
    it knows, this line now renders the table's own literal plus a fixed
    remedy sentence naming the row's LANDED file - the operator is no
    longer left with an unopenable ``artifact-1`` and no stated way to open
    it. For a suffix it does NOT know, round 6's behaviour is unchanged,
    byte for byte: :data:`_TYPE_NOT_PRESERVED`, and nothing else. Unknown
    stays unnamed; that is what makes the enum closed rather than a filter.

    **What did NOT change: the landing fence.** This function is a RENDERING
    decision only. :func:`_artifact_extension` still decides the copy's own
    suffix from :data:`_ARTIFACT_EXTENSIONS` alone, and every member of the
    label table is still absent from it - a labelled ``.pdf`` lands as bare
    ``artifact-1``, exactly as it did before this table existed. Naming a
    type on the receipt and admitting it to disk are different acts; only
    the first moved.

    **The limit, stated exactly (DEC-34c round 7, AC-2).** Earlier text
    here suggested the operator could fall back on the "copied from"
    line's path as a hint for the real, excluded type — eyeballing the
    original BASENAME (seeing ``notes.xlsm`` hints "an Office macro
    file"). That only works when :func:`_esc_path`'s basename half itself
    escapes redaction. Measured against this tree: when the original
    basename is ALSO credential-shaped — independent of, and in addition
    to, carrying an excluded suffix — :func:`_esc_path` redacts that half
    to :data:`_REDACTED`; the parent-directory half may still render in
    the clear, but a directory alone does not hint at a FILE's type. The
    row is then ``artifact-<n>`` / "copied from ``<dir>\\[redacted -
    looked like a credential]``" / "original type:
    ``(type not preserved)``" — two DIFFERENT fixed constants
    (:data:`_REDACTED` and :data:`_TYPE_NOT_PRESERVED`), neither derived
    from the original name, and no surface left in the row that still
    names what the file was. This is not a new leak — if anything the
    opposite, since nothing sensitive is exposed either way — it is the
    retired round-5 goal in its starker form: say plainly that the
    fallback hint is unavailable in exactly the case an operator would
    have wanted it, rather than leave a reader assuming one always
    exists. Phase 1d narrows this limit to exactly the UNKNOWN-suffix case:
    a credential-shaped basename carrying a suffix the label table DOES
    know still collapses its "copied from" half to :data:`_REDACTED`, but
    the type line now names the type and the remedy from the table, so the
    row is no longer hint-free. For a suffix the table does not know -
    which is what ``.secretfmt`` in that pin is - the paragraph above
    still holds verbatim.

    ``landed_name`` is the row's own name in the Outbox folder. It is
    ``artifact-<ordinal>`` plus an admitted suffix, built by
    :func:`_copy_artifacts` from a loop position through
    :class:`TrustedIdentity` - no byte of it comes from the source - and it
    is only interpolated on the withheld branch, where that suffix is
    empty by construction. It is still routed through :func:`_esc`
    (redact THEN escape, never a slice) rather than trusted on that
    reasoning alone.

    Returns the receipt's type-line MARKUP (the text after
    ``"original type: "``), not a bare label: the withheld branch needs two
    ``<code>`` spans with prose between them.
    """
    ext = _artifact_extension(Path(str(original)))
    if ext:
        return f"<code>{html.escape(ext, quote=True)}</code>"

    # DEC-31: the source's suffix is used ONLY as a membership key into the
    # closed table; every rendered byte below is that table's own VALUE (or
    # a literal defined in this file), never a slice of ``original``.
    # DEC-36's terminating rule - pin the concrete type in this frame before
    # operating on the value - so a non-``str`` can never reach the lookup
    # and dispatch its own ``__hash__``/``__eq__`` into a spurious match.
    raw = _display_suffix(original)
    label = _WITHHELD_EXTENSION_LABELS.get(raw) if type(raw) is str else None
    if type(label) is not str:
        return f"<code>{_TYPE_NOT_PRESERVED}</code>"

    label_html = html.escape(label, quote=True)
    return (f"<code>{label_html}</code> - {_WITHHELD_REASON}; rename to "
            f"<code>{_esc(landed_name)}{label_html}</code>"
            f"{_WITHHELD_REMEDY_TAIL}")


def _claim_name(taken: set, name: str) -> str:
    """Register ``name`` as used within one Outbox folder and return it.

    DEC-34c round 4 (AC-4). This function used to be ``_unique_name``: a
    numeric-suffix RENAME loop justified by "``files_produced`` is a flat
    list of absolute paths from anywhere on disk, so two directories'
    ``report.md`` would otherwise silently overwrite each other." That
    justification described a naming scheme this module no longer has — a
    copied artifact's basename is ``artifact-<ordinal><ext>``
    (:func:`_copy_artifacts`), and the ordinal is the loop position, so two
    entries produced by ONE call can never collide: the ordinal alone
    already distinguishes them, extension included. The rename loop could
    therefore never execute — it was dead code whose own comment kept
    claiming a live protection it no longer provided (DEC-34/DEC-33: a false
    assertion of enforcement is itself a defect).

    What replaces it is a genuine, cheap invariant check rather than
    decoration: if a future change ever DOES produce a duplicate (a bug in
    the ordinal, the extension table, or ``safe_component``'s cap), this
    raises here — loudly, and caught by :func:`_copy_artifacts`'s own
    per-artifact ``try/except`` as a skip reason — instead of
    ``shutil.copy2`` silently letting one artifact overwrite another on
    disk. If this naming scheme is ever generalised back to something that
    CAN legitimately collide, restore a real de-collision strategy rather
    than reintroducing dead code around this assertion.
    """
    if name in taken:
        raise ValueError(
            f"artifact basename collision on {name!r} — unreachable under "
            f"ordinal naming; treat this as an upstream bug, not a "
            f"collision to rename around")
    taken.add(name)
    return name


def _copy_artifacts(sources: Iterable[Any], dest_dir: Path,
                    root: Path) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Copy each artifact into ``dest_dir``. Returns (copied, skipped_reasons).

    Every destination is re-checked against ``root`` immediately before the
    write, so a crafted basename can never traverse out of the Outbox.

    **DEC-34c (AC-1b, the call site round 2 self-disclosed and did not fix).**
    The on-disk STEM is ``artifact-<position>`` — the loop position, a
    :class:`TrustedIdentity` — never ``src.name``. ``files_produced`` is a
    flat list of paths the RUN chose, so a source basename is exactly as
    untrusted as the prompt: a task that saves its output to a file named
    after a pasted credential would otherwise write that name straight into
    the Outbox folder, allowlist-sanitized but perfectly legible.

    **round 4 (AC-1a) — the destination keeps a real, but ALLOWLISTED,
    extension.** Round 3 dropped the suffix entirely along with the stem,
    which over-applied AC-1: the suffix is TYPE information, not identity,
    and destroying it broke every downstream consumer that keys off a
    file's kind (:func:`_artifact_extension` — a fixed, code-defined enum,
    matched case-insensitively, canonical spelling only; an unrecognised or
    absent source suffix yields no destination suffix, never a passthrough).

    **round 4 (AC-2) — the receipt's identity claim, corrected.** The
    original name is shown to the operator — redacted, not filtered — in
    ``receipt.html``'s "copied from" line, but as of round 4 that line
    redacts the parent directory and the basename INDEPENDENTLY
    (:func:`_esc_path`), not as one compound string: feeding the whole
    absolute path to :func:`redact` in one call made two unrelated artifacts
    collapse to the identical rendered line the instant the run's own
    directory happened to be shape-matching (a 40+ hex run id, which carries
    no credential) — see :func:`_esc_path`'s docstring for the measurement.
    This is a content surface DEC-31 already covers, not a filename.
    """
    items = list(sources or [])
    copied: List[Tuple[str, str]] = []
    skipped: List[str] = []
    taken: set = set()
    for i, raw in enumerate(items):
        if i >= MAX_ARTIFACTS:
            skipped.append(
                f"{len(items) - MAX_ARTIFACTS} more file(s) not copied "
                f"(cap {MAX_ARTIFACTS})")
            break
        try:
            src = Path(str(raw)).expanduser()
            if not src.is_file():
                skipped.append(f"{raw} (not a file at write time)")
                continue
            if src.stat().st_size > MAX_ARTIFACT_BYTES:
                skipped.append(
                    f"{raw} (over the {MAX_ARTIFACT_BYTES // (1024 * 1024)}MB "
                    f"copy cap - left in place)")
                continue
            stem = safe_component(
                TrustedIdentity(f"artifact-{i + 1}"), fallback="artifact")
            name = _claim_name(taken, stem + _artifact_extension(src))
            dest = dest_dir / name
            if not is_within(dest, root):
                skipped.append(f"{raw} (destination escaped the Outbox root - refused)")
                continue
            shutil.copy2(str(src), str(dest))
            copied.append((name, str(src)))
        except Exception as exc:
            skipped.append(f"{raw} ({exc})")
            logger.debug("[Outbox] artifact copy failed", exc_info=True)
    return copied, skipped


def write_outbox(vault: Any, *, task_id: str, prompt: str, status: str,
                 summary: str = "", files_produced: Sequence[Any] = (),
                 execution_id: Optional[str] = None,
                 committed_effects: Sequence[str] = (),
                 now: Optional[datetime] = None) -> str:
    """Write ONE run's Outbox folder and return its path. Raises on failure —
    the best-effort wrapper is :func:`write_outbox_for_run`.

    Ordering IS the contract: artifacts, then ``receipt.html``, then (on
    failure) ``FAILED-<slug>.txt``, and ``.done`` LAST. A consumer that waits
    for ``.done`` can never observe a partially-written folder.

    **DEC-34c (supersedes the DEC-33 GATE-5 fix attempted here).** GATE-5
    named the real gap: the slug is a real ON-DISK DIRECTORY NAME — a trust
    boundary crossing exactly like the receipt body — and it never went
    through :func:`redact`. The first attempt at closing it routed the
    prompt through ``redact(prompt, max_len=MAX_SLUG_CHARS)`` before
    :func:`safe_component`. REFUTED, measured against this tree (see
    ``tests/test_dec33_gate5_outbox_slug_redaction.py``, which reconstructs
    and pins the measurement rather than merely narrating it): several
    realistic CREDENTIAL-FREE prompts collapsed to the SAME constant slug
    (``_value_is_secret`` treats the WHOLE value as a credential the instant
    ``mask_outbound`` changes anything ANYWHERE in it — an ordinary sentence
    like "Compare Basic and Bearer authentication for the API design doc" is
    enough), while shapes ``mask_outbound`` does not know — a pasted PEM/SSH
    key, an underscore-style Stripe secret, a Google API key, a raw AWS
    secret access key — sailed through UNCHANGED and landed on disk. You
    cannot make attacker-controlled free text safe FOR A FILENAME by
    filtering it; redact-then-sanitize is the wrong operation for an
    identifier, whether or not ``redact()`` happens to recognise the shape.

    The fix is architectural, not a better filter: the slug is a PURE
    FUNCTION of ``task_id`` (the date is already carried by ``stamp``) via
    :class:`TrustedIdentity` — ``prompt`` is not consulted AT ALL, so a
    credential pasted into it cannot reach the slug regardless of its shape.
    ``task_id`` is trusted BY CONTRACT rather than by shape-detection: every
    call site in this codebase (``direct_task.py`` / ``quick_task.py``) hands
    it an internally-generated chat timestamp or session id, never raw user
    text. Nothing here re-derives that trust — if it is ever violated the
    fix is to keep ``task_id`` non-attacker-influenced upstream, not to
    filter it here, for the same reason filtering never worked for the
    prompt (see :class:`TrustedIdentity`'s docstring). The artifact basename
    has the same fix — see ``_copy_artifacts``.
    """
    root = outbox_root(vault)
    stamp = now or datetime.now()
    slug = safe_component(TrustedIdentity(str(task_id or "")), fallback="task")
    base = f"{stamp.strftime('%Y-%m-%d')}-{slug}"

    root.mkdir(parents=True, exist_ok=True)
    run_dir = _unique_dir(root, base)
    if not is_within(run_dir, root):
        raise ValueError(f"Outbox run dir escaped its root: {run_dir!r}")
    run_dir.mkdir(parents=True, exist_ok=True)

    copied, skipped = _copy_artifacts(files_produced, run_dir, root)

    failed = str(status or "").lower() not in _OK_STATUSES

    receipt_path = run_dir / "receipt.html"
    if not is_within(receipt_path, root):
        raise ValueError("receipt path escaped the Outbox root")
    _write_atomic(receipt_path, render_receipt(
        task_id=task_id, prompt=prompt, status=status, summary=summary,
        artifacts=copied, execution_id=execution_id,
        produced_at=stamp.isoformat(timespec="seconds"),
        note=("\n".join(skipped) if skipped else "")))

    if failed:
        failed_path = run_dir / f"FAILED-{slug}.txt"
        if not is_within(failed_path, root):
            raise ValueError("failure-note path escaped the Outbox root")
        _write_atomic(failed_path, _failure_note(
            prompt=prompt, status=status, summary=summary,
            committed=committed_effects))

    # LAST, always: the folder is now complete and safe to consume.
    _write_atomic(run_dir / DONE_MARKER, stamp.isoformat(timespec="seconds") + "\n")
    return str(run_dir)


def write_outbox_for_run(vault: Any, *, task_id: str, prompt: str, status: str,
                         summary: str = "", files_produced: Sequence[Any] = (),
                         execution_id: Optional[str] = None) -> Optional[str]:
    """Best-effort completion hook — NEVER raises, never blocks a run's terminal.

    Skips silently when a SUCCESSFUL run produced no files: an empty folder per
    chat message is noise, not a handoff. A non-successful run ALWAYS writes,
    because the failure note is itself the deliverable.
    """
    try:
        ok = str(status or "").lower() in _OK_STATUSES
        if ok and not list(files_produced or []):
            return None
        return write_outbox(
            vault, task_id=task_id, prompt=prompt, status=status,
            summary=summary, files_produced=files_produced,
            execution_id=execution_id)
    except Exception:
        logger.debug("[Outbox] write skipped", exc_info=True)
        return None
