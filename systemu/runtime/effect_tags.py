"""G0 — the shared EffectTag vocabulary (spec UNIFIED-v2 §5.7).

This is the effect language every later gate/verifier consumes. Two design
properties are load-bearing:

  * **Open vocabulary (Callout 2).** A tier or the planner may PROPOSE an effect
    class the type system has never seen. ``coerce`` maps any unrecognized value
    to :data:`EffectTag.UNKNOWN` (which the action gate treats as
    dangerous-until-proven ⇒ REQUIRE_APPROVAL), and ``register_effect_tag`` lets
    a new modality add its own tag at runtime. Determinism *classifies and gates*
    a proposal; it never *refuses* the plan.

  * **Advisory, escalate-only classifier.** ``classify_source`` is a deterministic
    AST scan of a tool's source for effectful sinks. It is a SIGNAL, not a proof:
    the absence of a tag is NEVER "no effect" (unparseable source ⇒ UNKNOWN), and
    the real network boundary is the OS-kernel egress jail (S2), not this scan.

    The scan RESOLVES IMPORT ALIASES. It once matched receivers by literal name,
    which ordinary idiomatic Python defeated — ``import subprocess as sp``,
    ``from os import system``, ``import requests as r`` all scanned as
    "purely local", which silently ungated BOTH consumers of this classifier
    (``tool_dry_run._gate_skip_reason``, which decides whether a freshly-forged
    body executes unattended, and ``action_governance.forged_network_denied``,
    the live network gate). ``_EffectVisitor`` now builds a module-alias and a
    symbol-alias map while walking and resolves attribute receivers AND bare call
    names through them. Resolution is ADDITIVE — the literal receiver is scored
    too — so it can only ever ADD a tag, never remove one the old scan produced.

    KNOWN LIMITS, by construction: this is import-BINDING analysis, not dataflow.
    A sink reached through a local variable (``sp = subprocess``,
    ``cx = requests.Session()``, ``f = subprocess.run``) or a star-import is not
    seen. Genuinely dynamic access (``__import__``/``exec``/``eval``/
    ``importlib.import_module``) forces UNKNOWN rather than guessing; ``getattr``
    is DELIBERATELY excluded from that list on measured evidence (see
    ``_DYNAMIC_UNKNOWN_NAMES``).

Nothing here imports :mod:`systemu.core.models` — the vocabulary is foundational
and must stay import-cycle-free (``core.models`` stores ``effect_tags`` as plain
strings and never imports this module).

**Cycle note (R-A13b-2ii-a).** :func:`classify_source` consults the curated
:mod:`systemu.runtime.effect_signals` map to emit the SEMANTIC classes
(``money_move``/``send_message``) the structural scan cannot reach. That module
imports ``EffectTag`` from HERE at its top, so this module must import it **LAZILY
inside** :func:`classify_source` (never at module top) — whichever module loads
first fully initializes ``effect_tags`` (no top-level ``effect_signals`` import)
before ``effect_signals`` pulls ``EffectTag`` back, so there is no import cycle.
"""
from __future__ import annotations

import ast
from enum import Enum
from typing import Set


class EffectTag(str, Enum):
    """The canonical effect classes (str-valued so they serialize to their value
    in ``model_dump(mode="json")`` and round-trip through plain-string storage)."""

    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    LOCAL_DELETE = "local_delete"
    SHELL_EXEC = "shell_exec"
    NET_READ = "net_read"
    NET_MUTATE = "net_mutate"
    SEND_MESSAGE = "send_message"
    MONEY_MOVE = "money_move"
    OAUTH_CALL = "oauth_call"

    # ── F14: capture / actuation classes ─────────────────────────────────────
    # These name capabilities the shipped catalog ALREADY HAS and the vocabulary
    # had no honest word for, so six seed tools (`take_screenshot`,
    # `clipboard_read`, `clipboard_write`, `type_text`, `keyboard_shortcut`,
    # `web_act`) classified to NOTHING and every consumer read that as "no
    # effects" or "unknown" depending on which one you asked.
    #
    # They are DELIBERATELY not folded into LOCAL_READ / LOCAL_WRITE. Typing
    # keystrokes into whatever window happens to be focused is not a local file
    # write, and a screenshot is not a local file read — its CONTENT is placed
    # into an LLM prompt and therefore LEAVES THE MACHINE. Mis-tagging them would
    # push them straight through the low-authority allowlist, which is precisely
    # the conflation F9 existed to eliminate.

    # Reads pixel content off the screen / a window. Captures whatever is on
    # display — including applications the agent was never granted — and the
    # captured image is normally handed to the model, so it egresses by use.
    SCREEN_CAPTURE = "screen_capture"
    # Reads the system clipboard: a cross-application channel that routinely
    # holds a password, a token or a one-time code the human just copied.
    CLIPBOARD_READ = "clipboard_read"
    # Replaces the system clipboard: decides what the human pastes NEXT, into an
    # application of their choosing. A tampering channel, not an exfil one — a
    # different risk from CLIPBOARD_READ, hence a different class.
    CLIPBOARD_WRITE = "clipboard_write"
    # Synthesises OS-level keyboard/mouse events. They land in whatever window
    # has focus at that instant, so the blast radius is every running
    # application, and it is bounded by nothing this process controls.
    INPUT_SYNTHESIS = "input_synthesis"
    # Drives a real (headless or attached) browser session: navigates, clicks,
    # fills and submits, carrying whatever ambient cookies/session that browser
    # holds. It necessarily reaches the network — see NET_EFFECTS, which
    # includes it — but "fetched a URL" understates operating a page's controls.
    BROWSER_ACTUATE = "browser_actuate"
    # Raises a desktop notification / toast. Small, but real: it draws UI the
    # human may mistake for a system message. No existing class fits, and
    # `local_write` would be a lie.
    DESKTOP_NOTIFY = "desktop_notify"

    # ── the two NON-effects, which are NOT the same fact (F14 problem 2) ──────
    # POSITIVE witness: the body was examined and provably has no effects. Only
    # `classify_source`'s purity proof mints it (all imports pure, no effect
    # sink, no dynamic access), and it is EXCLUSIVE — carrying it alongside a
    # real class is a self-contradicting record. An operator may NOT hand-assign
    # it (see `interface.pages.inbox_page._reclassify_choices`): it asserts a
    # machine-checked property, not an opinion.
    NO_EFFECT = "no_effect"
    # sentinel: an effect the classifier could not resolve — gated, never refused
    UNKNOWN = "unknown"


# The §5.7 two-band DENY floor keys on these: an UNKNOWN effect that ALSO carries
# a high-severity signal fails closed to DENY (not a rubber-stampable card).
HIGH_SEVERITY: "frozenset[EffectTag]" = frozenset({
    EffectTag.LOCAL_DELETE,
    EffectTag.NET_MUTATE,
    EffectTag.SEND_MESSAGE,
    EffectTag.MONEY_MOVE,
})


# ── F9: which effect classes may receive a STANDING, UNATTENDED, BLANKET allow ──
#
# A different question from every other set in this module, and the distinction is
# what F9 was: `HIGH_SEVERITY` answers "does an UNCLASSIFIABLE effect fail closed to
# DENY?" — `shell_exec` is deliberately absent from it because a shell tool is not
# unclassifiable, it is classified, and it gates as REQUIRE_APPROVAL. That is correct
# for the per-call gate and CATASTROPHIC for a batch: the first-run review card offered
# the whole REQUIRE_APPROVAL band for one-click standing approval, so `run_command`,
# `run_cli_command`, `launch_application`, `close_application` (shell_exec) and
# `file_delete` (local_delete) were all in the batch, and the card truthfully reported
# "0 excluded" because nothing had reached the DENY floor.
#
# BATCH_APPROVABLE answers the batch question directly: "may the operator grant this
# effect class blanket permission WITHOUT ever seeing a single call?"
#
# It is an ALLOWLIST, and that is the load-bearing property. The requirement is that a
# shell tool added tomorrow is excluded WITHOUT anyone remembering to update a list —
# only default-exclude gives that. Everything outside this set is refused, including:
#   * every canonical tag not named here (shell_exec, local_delete, net_*, money_move,
#     send_message, oauth_call);
#   * UNKNOWN — an effect we could not classify is the last thing that may receive a
#     standing allow, and it is also the commonest backfill outcome;
#   * every open-vocabulary extension registered via `register_effect_tag` — a novel
#     actuation class is unreviewed by construction;
#   * any brand-new member added to `EffectTag` later.
#
# The purely-local, non-egressing classes are in. `net_read` is OUT even though it
# scores ALLOW at the gate: it EGRESSES (it is in `action_governance.NET_EFFECTS`, and
# the forged-network DENY exists precisely because a read exfiltrates).
# `local_write` is IN on the same footing as `local_read` — both are already frictionless
# at the gate, so admitting them here grants no authority the gate withholds. `no_effect`
# is IN because it is the SAFEST value in the vocabulary: a machine-checked proof that
# the body has no effects at all (and it can only be minted by that proof, never
# declared by the tool or assigned by hand).
#
# `tests/test_f9_batch_approval_effects.py` pins this set against HIGH_SEVERITY,
# `_APPROVAL_TAGS`, `NET_EFFECTS` and `MUST_ISOLATE`, so the only way it can go wrong —
# somebody widening it — goes red.


# ── OPERATOR RULING, 2026-08-07 — DO NOT "FIX" THIS BACK ─────────────────────────
#
# The operator was shown the risks IN FULL — that a screenshot and a clipboard read
# travel to the LLM provider, and that synthetic keyboard/mouse input lands in whatever
# window happens to have focus — and ruled that `take_screenshot`, `clipboard_read`,
# `type_text`, `keyboard_shortcut` and `web_act` ARE to be batch-approvable once
# correctly tagged. These are the effect classes those five carry, admitted here BY
# THAT DECISION and named as such so a later reader can see it was a decision and not
# a drift.
#
# The ruling did NOT extend to anything else. It is a set of deliberate MEMBERS, not a
# weakening of the rule: `shell_exec`, `local_delete`, `send_message`, `money_move`,
# `oauth_call`, `net_read`/`net_mutate`, UNKNOWN, `clipboard_write`, `desktop_notify`
# and every open-vocabulary extension all remain refused BY DEFAULT, with nobody
# updating a denylist. `is_batch_approvable_tag`'s promise — "a novel actuation class
# invented tomorrow is refused" — is intact and is pinned by
# `test_an_unruled_new_actuation_class_is_still_refused`.
#
# `browser_actuate` is the one member that EGRESSES, and it is deliberately left in
# `action_governance.NET_EFFECTS` rather than hidden from it: the pre-S2 forged-network
# hard-DENY must still fire on a forged tool that drives a browser. So the record says
# BOTH true things — it egresses, AND the operator granted it batch approval — instead
# of falsifying one to keep a set-disjointness assertion green.
OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07: "frozenset[EffectTag]" = frozenset({
    EffectTag.SCREEN_CAPTURE,     # take_screenshot
    EffectTag.CLIPBOARD_READ,     # clipboard_read
    EffectTag.INPUT_SYNTHESIS,    # type_text, keyboard_shortcut
    EffectTag.BROWSER_ACTUATE,    # web_act
})

# F16 / operator ruling 2026-08-09 — RESOLVES AN INCOHERENCE, it does not relax a
# guard. `net_read` was absent from `action_governance._APPROVAL_TAGS`, so seven
# shipped tools carrying it alone (web_search, fetch_html, fetch_json, api_call_get,
# extract_records, find_places, web_extract) already ran UNGATED — no approval, ever.
# The same class was absent here, so it BLOCKED a standing allow: `web_read` is gated
# by `browser_actuate`, which the 2026-08-07 ruling made batch-approvable, yet could
# never be batched because it also reads the network. A class cannot be both too safe
# to stop the agent for and too dangerous to grant standing. This picks the side the
# runtime already behaves as.
#
# Scope is exactly READING. `net_mutate`, `send_message`, `money_move` and `oauth_call`
# stay gated and unbatchable — pinned by test_the_ruling_did_not_leak_into_network_MUTATION.
# Like its 08-07 sibling this stays a SEPARATE dated constant so the disjointness
# assertions can subtract the ruled members and still bound everything else.
OPERATOR_RULED_BATCH_APPROVABLE_2026_08_09: "frozenset[EffectTag]" = frozenset({
    EffectTag.NET_READ,           # web_search, fetch_html, fetch_json, api_call_get, …
})

# ── F17: THE REGISTRY OF DATED RULINGS — one row per operator decision ───────
#
# The first-gate review card is the operator-facing RECORD of what they agreed to, and
# it used to generate its attribution sentence from the 08-07 constant ALONE. When the
# 08-09 ruling landed, `net_read` appeared in the card's allowlist with no attribution
# at all — reading as though it had always been allowed rather than decided on a day,
# with risks shown. Not false; incomplete, which on a consent record is the same defect.
#
# So the dated constants are no longer summed ad hoc at the point of use. THIS is the
# one table: date, the classes that ruling admitted, and the risks the operator was
# shown when they took it. `OPERATOR_RULED_BATCH_APPROVABLE` is DERIVED from it, which
# makes registration FAIL-CLOSED rather than merely audited: a future dated constant
# that nobody adds here is not batch-approvable at all, so it can never reach the card
# unattributed. `first_gate_review.batch_rule_sentence` emits one clause per row, so a
# third ruling tomorrow is attributed with nobody editing a sentence.
#
# `risks` is operator-facing prose and is rendered verbatim onto the card: ASCII only
# (DEC-32c — it crosses a Windows console and the event log), and it must state what
# the operator was actually told, not a reassurance.
from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class OperatorRuling:
    """One dated operator decision admitting effect classes to the batch."""

    date: str                      # ISO 8601, the day the operator ruled
    tags: "frozenset[EffectTag]"   # exactly what that decision admitted
    risks: str                     # the risks stated when it was taken


OPERATOR_RULINGS: "tuple[OperatorRuling, ...]" = (
    OperatorRuling(
        date="2026-08-07",
        tags=OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07,
        risks=("a screenshot and a clipboard read are put into the model's prompt "
               "and therefore leave this machine, synthetic keystrokes land in "
               "whatever window has focus, and a browser action runs against a live "
               "site with that browser's session"),
    ),
    OperatorRuling(
        date="2026-08-09",
        tags=OPERATOR_RULED_BATCH_APPROVABLE_2026_08_09,
        risks=("a fetched URL carries whatever the agent puts in it to a third-party "
               "host, so reading the network is also an exfiltration channel, and the "
               "response is untrusted text that then steers the agent; the decision "
               "covers READING only, and writing to the network stays gated"),
    ),
)

OPERATOR_RULED_BATCH_APPROVABLE: "frozenset[EffectTag]" = frozenset().union(
    *(r.tags for r in OPERATOR_RULINGS)) if OPERATOR_RULINGS else frozenset()

BATCH_APPROVABLE: "frozenset[EffectTag]" = frozenset({
    EffectTag.LOCAL_READ,
    EffectTag.LOCAL_WRITE,
    EffectTag.NO_EFFECT,
}) | OPERATOR_RULED_BATCH_APPROVABLE

_CANONICAL = {t.value: t for t in EffectTag}
# Runtime-registered extension tags: value -> is_high_severity
_EXTENSIONS: "dict[str, bool]" = {}


def register_effect_tag(value: str, *, high_severity: bool = False) -> str:
    """Register a new effect class proposed by a tier/modality at runtime.

    Idempotent; returns the normalized value. Registering a canonical value is a
    no-op. This is the open-vocabulary hook — a novel actuation can declare its
    own effect class, and the gate then treats it by its declared severity."""
    v = str(value or "").strip().lower()
    if not v:
        raise ValueError("effect tag value must be a non-empty string")
    if v not in _CANONICAL:
        _EXTENSIONS[v] = bool(high_severity)
    return v


def coerce(value) -> str:
    """Normalize any value to a known/registered tag value, else ``UNKNOWN``.

    Open-world rule: an unrecognized effect is UNKNOWN (gated), never an error."""
    if isinstance(value, EffectTag):
        return value.value
    v = str(value or "").strip().lower()
    if v in _CANONICAL or v in _EXTENSIONS:
        return v
    return EffectTag.UNKNOWN.value


_HIGH_SEVERITY_VALUES = {t.value for t in HIGH_SEVERITY}


def is_high_severity(value) -> bool:
    """True iff the (known) tag is a high-severity effect. UNKNOWN is NOT
    high-severity on its own — the DENY floor is UNKNOWN *plus* a separately
    detected high-severity signal (see the action gate, S1)."""
    v = coerce(value)
    if v == EffectTag.UNKNOWN.value:
        return False
    if v in _HIGH_SEVERITY_VALUES:
        return True
    return _EXTENSIONS.get(v, False)


_BATCH_APPROVABLE_VALUES = frozenset(t.value for t in BATCH_APPROVABLE)


def is_batch_approvable_tag(value) -> bool:
    """True iff this effect class may receive a STANDING, UNATTENDED, BLANKET allow.

    ALLOWLIST semantics — anything not explicitly in :data:`BATCH_APPROVABLE` is False,
    including UNKNOWN, every runtime-registered extension class, and any value that does
    not name a tag at all. That default is the whole point: a shell tool or a novel
    actuation class invented tomorrow is refused without anyone updating a denylist.

    The membership test is on the COERCED value, and the coercion is what makes junk
    safe: `coerce` maps anything unrecognised to UNKNOWN, which is not in the allowlist.
    """
    return coerce(value) in _BATCH_APPROVABLE_VALUES


def batch_approvable_tags() -> "list[str]":
    """The allowlist as sorted values — the vocabulary the operator-facing copy names.

    Card text is GENERATED from this, never written alongside it, so the sentence the
    operator reads cannot drift from the set the code enforces (F9 shipped exactly that
    drift: a promise about "unclassifiable high-severity" effects on a card whose batch
    was full of `shell_exec`)."""
    return sorted(_BATCH_APPROVABLE_VALUES)


def all_known() -> "list[str]":
    return sorted(set(_CANONICAL) | set(_EXTENSIONS))


# --------------------------------------------------------------------------- #
# deterministic AST source classifier (advisory signal, escalate-only)
# --------------------------------------------------------------------------- #

_NET_CLIENTS = {"requests", "httpx", "aiohttp", "session", "client", "http", "urllib3"}
_NET_READ_METHODS = {"get", "head", "options"}
_NET_WRITE_METHODS = {"post", "put", "patch", "delete", "request", "send"}
# attr-only network mutators (receiver is not a bare module name, e.g. self.session.post)
_ATTR_ONLY_NET_MUTATE = {"post", "put", "patch"}

# R-A14a §15.1 hardening: stdlib egress modules whose IMPORT signals network egress
# even when the CALL-site scan misses it (e.g. `http.client.HTTPSConnection(...)` has
# an Attribute receiver, and `conn.request(...)` a bare `conn` receiver, so neither
# trips _NET_CLIENTS). Keyed on the FULL dotted module so `http.client` is net but
# `http.server` (inbound) is not. Tightens the forged-network hard-DENY: a forged
# tool reaching the net via one of these is DENIED, not merely REQUIRE_APPROVAL.
_NET_EGRESS_MODULES = {
    "http.client": EffectTag.NET_MUTATE,   # HTTP client egress (request/send)
    "ftplib": EffectTag.NET_MUTATE,        # FTP transfer egress
    "telnetlib": EffectTag.NET_MUTATE,     # telnet session egress
    "poplib": EffectTag.NET_READ,          # POP3 mail read
    "imaplib": EffectTag.NET_READ,         # IMAP mail read
    "nntplib": EffectTag.NET_READ,         # NNTP read
    # `from urllib.request import build_opener` / `Request` / `urlretrieve`: the
    # opener's `.open()` has no receiver the call-site scan can key on, so the
    # IMPORT is the only reliable signal. NET_READ is the conservative floor —
    # the call-site `_urlopen_effect` still upgrades a data= urlopen to NET_MUTATE.
    # Keyed on the FULL dotted module, so the very common (and inert)
    # `urllib.parse` is NOT matched.
    "urllib.request": EffectTag.NET_READ,

    # F14 — FIRST-PARTY egress modules. Six seed tools reach the network through
    # systemu's own helper layer and NOTHING else (`web_search`, `find_places`,
    # `web_read`, `web_extract`, `extract_records`): the call site is
    # `web_access.search_web(...)`, whose receiver is a local module object, so no
    # call-site rule can see it. The IMPORT is the reliable signal — the same
    # reasoning that put `urllib.request` in this table. Keyed on the FULL dotted
    # name so `systemu.runtime` (imported by half the catalog for inert helpers
    # like `capture_exclusion`) is NOT matched.
    #   web_access      keyless search/read stack: DDG, Jina Reader, Overpass, OSM
    #   web.fetch_core  the raw httpx/requests fetch + readability extraction
    #   web.search_providers  the keyed provider chain (Tavily/Exa/Brave/Serper)
    #   core.llm_router the model call itself — it ships the payload off-machine
    #   runtime.extractor  Tier-3 LLM extraction; imports llm_router at its top
    "systemu.runtime.web_access": EffectTag.NET_READ,
    "systemu.runtime.web.fetch_core": EffectTag.NET_READ,
    "systemu.runtime.web.search_providers": EffectTag.NET_READ,
    "systemu.core.llm_router": EffectTag.NET_READ,
    "systemu.runtime.extractor": EffectTag.NET_READ,
}


# F14 — modules whose IMPORT is itself the capability. Same mechanism and same
# justification as `_NET_EGRESS_MODULES`: for a screen-grab / input-synthesis /
# browser-driving library there is no receiver a call-site rule can key on
# (`sct.grab(...)`, `keyboard.press(...)`, `page.goto(...)` all have local-variable
# receivers), so the import binding is the only reliable structural signal.
# Prefix-matched on the FULL dotted name, so `mss.tools` matches `mss` while
# `PIL.Image` (ordinary image I/O) does NOT match `PIL.ImageGrab` (a screen grab).
_CAPABILITY_MODULES: "dict[str, frozenset]" = {
    # screen capture
    "mss": frozenset({EffectTag.SCREEN_CAPTURE}),
    "PIL.ImageGrab": frozenset({EffectTag.SCREEN_CAPTURE}),
    "pyscreeze": frozenset({EffectTag.SCREEN_CAPTURE}),
    "d3dshot": frozenset({EffectTag.SCREEN_CAPTURE}),
    # synthetic input events
    "pynput": frozenset({EffectTag.INPUT_SYNTHESIS}),
    "pydirectinput": frozenset({EffectTag.INPUT_SYNTHESIS}),
    "pywinauto": frozenset({EffectTag.INPUT_SYNTHESIS}),
    # pyautogui is BOTH: it grabs the screen and it synthesises input.
    "pyautogui": frozenset({EffectTag.SCREEN_CAPTURE, EffectTag.INPUT_SYNTHESIS}),
    # browser actuation. `browser_pool` LAUNCHES and drives chromium; `act_loop`
    # is the click/type loop itself. BROWSER_ACTUATE is in NET_EFFECTS, so this
    # also keeps the pre-S2 forged-network DENY firing on a forged browser tool.
    "playwright": frozenset({EffectTag.BROWSER_ACTUATE}),
    "selenium": frozenset({EffectTag.BROWSER_ACTUATE}),
    "systemu.runtime.web.browser_pool": frozenset({EffectTag.BROWSER_ACTUATE}),
    "systemu.runtime.web.act_loop": frozenset({EffectTag.BROWSER_ACTUATE}),
    # desktop notification / toast
    "plyer": frozenset({EffectTag.DESKTOP_NOTIFY}),
    "win10toast": frozenset({EffectTag.DESKTOP_NOTIFY}),
    "notifypy": frozenset({EffectTag.DESKTOP_NOTIFY}),
}


def _prefix_lookup(table, module_name):
    """Exact-or-dotted-prefix match of *module_name* against *table*.

    Dotted-PREFIX, never ``startswith``: ``http.client`` must match
    ``http.clientfoo`` never, and ``PIL.ImageGrab`` must not be reached by
    ``PIL.Image``."""
    if not isinstance(module_name, str) or not module_name:
        return None
    mod = module_name.strip()
    for m, val in table.items():
        if mod == m or mod.startswith(m + "."):
            return val
    return None


def _net_egress_for_module(module_name) -> "Optional[EffectTag]":
    """The net-egress EffectTag for an imported module (exact or dotted-prefix
    match on the FULL module name), else None. ``http.client`` → net;
    ``http.server`` → None."""
    return _prefix_lookup(_NET_EGRESS_MODULES, module_name)


def _effects_for_module(module_name) -> "Set[EffectTag]":
    """Every effect class an IMPORT of *module_name* signals (net egress +
    capability). Empty set when the module is not in either table."""
    out: Set[EffectTag] = set()
    net = _net_egress_for_module(module_name)
    if net is not None:
        out.add(net)
    cap = _prefix_lookup(_CAPABILITY_MODULES, module_name)
    if cap:
        out |= set(cap)
    return out

_SHELL_ATTRS = {("os", "system"), ("os", "popen"), ("os", "execv"),
                ("os", "execve"), ("os", "execvp"), ("os", "execvpe")}
_SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}

_DELETE_ATTRS = {("os", "remove"), ("os", "unlink"), ("shutil", "rmtree")}
_WRITE_ATTRS = {("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile"),
                ("shutil", "copytree"), ("shutil", "move"), ("os", "rename"),
                ("os", "replace"), ("os", "mkdir"), ("os", "makedirs")}

_WRITE_METHODS = {"write_text", "write_bytes"}
_READ_METHODS = {"read_text", "read_bytes"}
_WRITE_MODE_CHARS = ("w", "a", "x", "+")

# ── F14 additions to the structural scan ─────────────────────────────────────
#
# Every entry below was added because a SHIPPED seed tool's only real sink was one
# of these and it therefore scanned to nothing. Each is an ordinary filesystem or
# capability sink; none of them is a name heuristic.

# Directory ENUMERATION and file-content reads. Deliberately does NOT include
# `exists`/`is_file`/`is_dir`/`suffix`: a metadata probe is not an effect, and
# treating it as one would tag almost every tool `local_read` and make the class
# meaningless. Enumerating a directory, by contrast, discloses its contents.
_READ_ATTRS_ANY_RECEIVER = {"glob", "rglob", "iterdir", "scandir", "listdir",
                            "walk", "read", "readlines", "readline"}
# Filesystem MUTATION whose receiver is a Path/handle the scan cannot bind.
# `save` covers openpyxl `Workbook.save`, PIL `Image.save`, python-docx
# `Document.save` — the write sink of five seed tools, all of which build the
# object in memory and only touch the disk here.
_WRITE_ATTRS_ANY_RECEIVER = {"mkdir", "makedirs", "touch", "save", "extractall",
                             "writelines", "to_png", "writestr", "rmdir"}

# Callables whose FILE-vs-not behaviour depends on a mode/arg, handled like `open`.
#   zipfile.ZipFile(p, "w") / tarfile.open(p, "r:*")  → write vs read
_ARCHIVE_OPENERS = {("zipfile", "ZipFile"), ("tarfile", "open"),
                    ("gzip", "open"), ("bz2", "open"), ("lzma", "open"),
                    ("zipfile", "PyZipFile"), ("tarfile", "TarFile")}
# Library entry points that OPEN AN EXISTING FILE for reading.
_FILE_READ_CALLS = {("openpyxl", "load_workbook"), ("PIL.Image", "open"),
                    ("Image", "open"), ("pandas", "read_csv"),
                    ("pandas", "read_excel"), ("json", "load"),
                    ("pickle", "load"), ("csv", "reader"),
                    ("msoffcrypto", "OfficeFile")}
# `docx.Document(path)` READS that path; `docx.Document()` creates an empty one.
# Arg-sensitive, exactly like `open`'s mode.
_FILE_READ_CALLS_IF_ARGS = {("docx", "Document"), ("Document", "Document")}

# Clipboard: direction is decided by the FUNCTION, not the module — a tool that
# only pastes must not inherit the write class, and vice versa (the operator
# ruling admits `clipboard_read` to the batch and not `clipboard_write`).
_CLIPBOARD_CALLS = {
    ("pyperclip", "paste"): EffectTag.CLIPBOARD_READ,
    ("pyperclip", "copy"): EffectTag.CLIPBOARD_WRITE,
    ("clipboard", "paste"): EffectTag.CLIPBOARD_READ,
    ("clipboard", "copy"): EffectTag.CLIPBOARD_WRITE,
}

# Page-actuation verbs. Attr-only (the receiver is a Playwright `page`/`locator`
# local, which the scan cannot bind). Kept DELIBERATELY NARROW to names that do
# not occur on stdlib objects: a false positive here adds BROWSER_ACTUATE, which
# is in NET_EFFECTS, which would hard-DENY a forged tool pre-S2. `click`/`fill`
# are excluded for exactly that reason — too generic to assert.
_BROWSER_ACTUATE_ATTRS = {"goto", "set_input_files", "select_option",
                          "aria_snapshot"}

# Genuinely DYNAMIC module/attribute access — the target is a runtime string, so
# no static resolution is possible and the honest answer is UNKNOWN (gated, never
# refused). Kept DELIBERATELY SMALL.
#
# ``getattr`` is EXCLUDED on measured evidence: it appears in ~15% of this repo's
# own tool bodies used entirely benignly (``getattr(obj, "name", None)``), so
# forcing UNKNOWN on it would skip that whole population and collapse the forge
# dry-run into a no-op — a large validation loss for a small gating gain. The
# advisory ``code_risk`` scan already surfaces getattr-based indirection to the
# operator at Gate-2, which is the right surface for it.
_DYNAMIC_UNKNOWN_NAMES = {"__import__", "exec", "eval"}
_DYNAMIC_UNKNOWN_ATTRS = {("importlib", "import_module")}


class _EffectVisitor(ast.NodeVisitor):
    def __init__(self, sig=None) -> None:
        self.tags: Set[EffectTag] = set()
        # the curated effect_signals module (lazily injected by classify_source);
        # None ⇒ semantic classification degrades to the structural scan (defensive).
        self._sig = sig
        # ── import-alias resolution ──────────────────────────────────────────
        # Without these the scan is NAME-MATCHING: it sees a sink only when the
        # receiver is spelled literally, so ordinary idiomatic Python
        # (`import subprocess as sp`, `from os import system`) reads as
        # purely-local. That defeated BOTH controls this classifier feeds — the
        # unattended forge dry-run and the live `forged_network_denied` gate.
        #
        # local name -> module (`import subprocess as sp`  ⇒ sp -> "subprocess")
        self._module_alias: "dict[str, str]" = {}
        # local name -> (module, symbol)
        # (`from subprocess import check_output as co` ⇒ co -> ("subprocess",
        #  "check_output")), so a BARE call resolves to a qualified pair.
        self._symbol_alias: "dict[str, tuple[str, str]]" = {}

    def _add_class(self, cls) -> None:
        """Add the EffectTag for a curated class VALUE (e.g. "money_move"); no-op on
        None / an unknown value. Keeps the never-raises contract."""
        if not cls:
            return
        try:
            self.tags.add(EffectTag(cls))
        except ValueError:
            pass

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 (ast API)
        for alias in node.names:
            if self._sig is not None:
                self._add_class(self._sig.class_for_import(alias.name))
            self.tags |= _effects_for_module(alias.name)
            # `import os.path as p` binds `p` to the FULL dotted module, whereas a
            # plain `import os.path` binds only the ROOT name `os`. Recording the
            # full dotted name for the aliased form is what keeps `p.remove(...)`
            # from being mis-resolved to `os.remove` (a false positive) while
            # `import os.path; os.remove(...)` still resolves correctly.
            if alias.asname:
                self._module_alias[alias.asname] = alias.name
            else:
                root = alias.name.split(".")[0]
                self._module_alias[root] = root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module:
            if self._sig is not None:
                self._add_class(self._sig.class_for_import(node.module))
            self.tags |= _effects_for_module(node.module)
            # RELATIVE imports (`from .os import system`, level > 0) name a LOCAL
            # module, not the stdlib one — resolving those against the stdlib
            # tables would invent effects that are not there.
            if not node.level:
                for alias in node.names:
                    if alias.name == "*":
                        continue   # star-import: nothing to bind a name to
                    # `from systemu.runtime import web_access` imports a SUBMODULE,
                    # and the dotted name that the module tables are keyed on is
                    # `<module>.<name>`, not `<module>`. Without this join, six seed
                    # tools whose ONLY egress is `from systemu.runtime import
                    # web_access` scanned as purely local. Harmless when the name is
                    # a symbol rather than a submodule: `<module>.<symbol>` is simply
                    # absent from the tables.
                    self.tags |= _effects_for_module(f"{node.module}.{alias.name}")
                    self._symbol_alias[alias.asname or alias.name] = (
                        node.module, alias.name)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        # a URL / host string literal names a curated endpoint (host-suffix match).
        if self._sig is not None and isinstance(node.value, str):
            self._add_class(self._sig.class_for_host(node.value))
        self.generic_visit(node)

    def _classify_module_attr(self, mod: str, attr: str, node: ast.Call) -> None:
        """Score a resolved ``module.attr`` sink. Shared by the attribute-receiver
        path and the BARE-name path (a from-imported symbol is the same sink, just
        spelled without its module)."""
        pair = (mod, attr)
        # F14 — the arg/mode-sensitive and curated-callable sinks first; they are
        # exact pairs, so they can never shadow the broader rules below.
        if pair in _CLIPBOARD_CALLS:
            self.tags.add(_CLIPBOARD_CALLS[pair])
        if pair in _ARCHIVE_OPENERS:
            # `zipfile.ZipFile(p, "w")` writes; `"r"` (and the default) reads.
            self.tags.add(self._open_effect(node, default=EffectTag.LOCAL_READ))
        if pair in _FILE_READ_CALLS:
            self.tags.add(EffectTag.LOCAL_READ)
        if pair in _FILE_READ_CALLS_IF_ARGS and (node.args or node.keywords):
            # `docx.Document(path)` reads that file; `docx.Document()` does not.
            self.tags.add(EffectTag.LOCAL_READ)

        if pair in _DELETE_ATTRS:
            self.tags.add(EffectTag.LOCAL_DELETE)
        elif pair in _WRITE_ATTRS:
            self.tags.add(EffectTag.LOCAL_WRITE)
        elif pair in _SHELL_ATTRS:
            self.tags.add(EffectTag.SHELL_EXEC)
        elif pair in _DYNAMIC_UNKNOWN_ATTRS:
            self.tags.add(EffectTag.UNKNOWN)
        elif mod == "subprocess" and attr in _SUBPROCESS_FUNCS:
            self.tags.add(EffectTag.SHELL_EXEC)
        elif mod == "socket" and attr in {"socket", "create_connection"}:
            self.tags.add(EffectTag.NET_MUTATE)
        elif mod in _NET_CLIENTS:
            if attr in _NET_READ_METHODS:
                self.tags.add(EffectTag.NET_READ)
            elif attr in _NET_WRITE_METHODS:
                self.tags.add(EffectTag.NET_MUTATE)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API)
        func = node.func

        if isinstance(func, ast.Name):
            if func.id == "open":
                self.tags.add(self._open_effect(node))
            elif func.id == "urlopen":
                self.tags.add(self._urlopen_effect(node))
            elif func.id in _DYNAMIC_UNKNOWN_NAMES:
                # a runtime-string target — no static resolution is possible
                self.tags.add(EffectTag.UNKNOWN)
            # A BARE call of a from-imported symbol is the SAME sink as the
            # qualified form: `from subprocess import check_output` +
            # `check_output(...)` ⇒ ("subprocess", "check_output").
            _sym = self._symbol_alias.get(func.id)
            if _sym is not None:
                self._classify_module_attr(_sym[0], _sym[1], node)

        elif isinstance(func, ast.Attribute):
            attr = func.attr
            # module.attr where module is a bare Name
            if isinstance(func.value, ast.Name):
                # Score the LITERAL receiver AND its import-alias resolution.
                # The union is deliberate: it is strictly ADDITIVE, so no
                # classification the pre-alias scan produced can be lost (a bare
                # `session.post(...)` on an un-imported local keeps working), while
                # `import subprocess as sp; sp.run(...)` now also resolves.
                literal = func.value.id
                resolved = self._module_alias.get(literal)
                self._classify_module_attr(literal, attr, node)
                if resolved is not None and resolved != literal:
                    self._classify_module_attr(resolved, attr, node)
            # attr-only signals (any receiver) — conservative, high-value only
            if attr == "urlopen":
                self.tags.add(self._urlopen_effect(node))
            elif attr == "unlink":
                self.tags.add(EffectTag.LOCAL_DELETE)
            elif attr == "open":
                # `p.open("a")` — a Path handle. Same mode rule as builtin `open`,
                # but the path is the RECEIVER so mode is positional slot 0.
                self.tags.add(self._open_effect(node, pos=0))
            elif attr in _WRITE_METHODS:
                self.tags.add(EffectTag.LOCAL_WRITE)
            elif attr in _READ_METHODS:
                self.tags.add(EffectTag.LOCAL_READ)
            elif attr in _ATTR_ONLY_NET_MUTATE:
                self.tags.add(EffectTag.NET_MUTATE)

            # F14 attr-only signals. Separate `if`s, not `elif`s off the chain
            # above: these are a different family and must not be shadowed by an
            # earlier match on an unrelated name.
            if attr in _WRITE_ATTRS_ANY_RECEIVER:
                self.tags.add(EffectTag.LOCAL_WRITE)
            if attr in _READ_ATTRS_ANY_RECEIVER:
                self.tags.add(EffectTag.LOCAL_READ)
            if attr in _BROWSER_ACTUATE_ATTRS:
                self.tags.add(EffectTag.BROWSER_ACTUATE)

            # R-A13b-2ii-a — curated SEMANTIC classes (money_move/send_message) by
            # attr/method chain. Test the QUALIFIED 2-component chain
            # ("PaymentIntent.create"/"messages.create") AND the bare attr
            # ("sendmail"/"chat_postMessage"); the table omits generic verbs so a
            # bare ".create"/".get" never over-hits.
            if self._sig is not None:
                self._add_class(self._sig.class_for_attrchain(attr))
                if isinstance(func.value, ast.Attribute):
                    self._add_class(self._sig.class_for_attrchain(
                        f"{func.value.attr}.{attr}"))

        self.generic_visit(node)

    @staticmethod
    def _mode_arg(node: ast.Call, pos: int = 1):
        """The literal mode string, from positional slot *pos* or a ``mode=`` kwarg.

        *pos* differs by call shape and getting it wrong inverts the answer:
        ``open(path, "a")`` puts mode at index 1, but ``Path.open("a")`` — the path
        is the RECEIVER — puts it at index 0. Reading index 1 there found nothing,
        fell through to the read default, and stamped `file_append` ``local_read``.
        """
        if len(node.args) > pos and isinstance(node.args[pos], ast.Constant):
            return node.args[pos].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                return kw.value.value
        return None

    def _open_effect(self, node: ast.Call, default: EffectTag = EffectTag.LOCAL_READ,
                     *, pos: int = 1) -> EffectTag:
        """LOCAL_WRITE iff the mode argument requests writing, else *default*.

        Shared by builtin ``open``, ``Path.open`` and the archive openers
        (``zipfile.ZipFile(p, "w")``, ``tarfile.open(p, "r:*")``) — one mode rule,
        so the three cannot disagree about what ``"a"`` means."""
        mode = self._mode_arg(node, pos)
        if isinstance(mode, str) and any(c in mode for c in _WRITE_MODE_CHARS):
            return EffectTag.LOCAL_WRITE
        return default

    @staticmethod
    def _urlopen_effect(node: ast.Call) -> EffectTag:
        # urlopen(url, data=...) is a POST; a bare urlopen(url) is a GET
        has_data = len(node.args) >= 2 or any(kw.arg == "data" for kw in node.keywords)
        return EffectTag.NET_MUTATE if has_data else EffectTag.NET_READ


# --------------------------------------------------------------------------- #
# F14 — the PURITY PROOF: the only thing that may mint EffectTag.NO_EFFECT
# --------------------------------------------------------------------------- #
#
# WHY A PROOF AND NOT AN ABSENCE. Before F14, "no effects" and "nobody classified
# this" were the SAME value (`[]`), so the gate could not tell `format_date` (a
# strptime/strftime wrapper) from `web_search` (which reaches the network through a
# helper the scan could not follow) and correctly refused both — which is why the
# first-run card offered 0 of 30 tools. The fix is NOT to read the empty scan as
# "pure": the empty scan is exactly the unclassified case, and the module's own
# docstring says so ("the absence of a tag is NEVER 'no effect'").
#
# So NO_EFFECT is minted only by a POSITIVE, machine-checkable witness: every import
# resolves to a module that cannot do anything, no effect sink was found, no impure
# builtin is called, and nothing is reached dynamically. Anything the check cannot
# account for leaves the tool UNCLASSIFIED, which is the safe side.

# Modules that cannot touch the filesystem, the network, the screen, the clipboard,
# a subprocess or the clock-independent outside world. `pathlib` is DELIBERATELY
# ABSENT: constructing a Path is inert but every interesting method on it is not, so
# a Path-using body must never be auto-declared pure.
_PURE_MODULES = frozenset({
    "__future__", "typing", "typing_extensions", "abc", "enum", "dataclasses",
    "collections", "itertools", "functools", "operator", "copy", "types",
    "numbers", "decimal", "fractions", "math", "cmath", "statistics",
    "string", "textwrap", "re", "unicodedata", "html", "html.parser",
    "json", "base64", "binascii", "struct", "codecs",
    "datetime", "calendar", "zoneinfo", "time",
    "urllib.parse", "hashlib", "hmac", "uuid", "secrets", "random",
    "dataclasses_json", "warnings", "logging",
})

# Modules that are pure ONLY through a restricted set of attributes. `os` is the one
# that matters: `os.path.splitext` is a string operation, `os.remove` is not. Every
# attribute reached on the module must be in the set or the body is not provably pure.
_PURE_MODULE_ATTRS = {
    "os": frozenset({"path", "sep", "altsep", "extsep", "pathsep", "linesep",
                     "curdir", "pardir", "name", "devnull", "fspath"}),
}

# Builtins that reach outside the process. `open` is already an effect sink; the rest
# would not be caught by any sink rule but defeat the purity claim.
_IMPURE_BUILTINS = frozenset({"open", "input", "exec", "eval", "compile",
                              "__import__", "breakpoint", "help", "print",
                              "globals", "locals", "vars", "memoryview"})


def _purity_failure(tree: ast.AST) -> "Optional[str]":
    """``None`` iff the module body is PROVABLY effect-free; else the reason it is
    not. Never raises — an unexpected node shape yields a refusal, not an exception.

    This is a WITNESS, not a heuristic: it enumerates what the body is allowed to
    contain and refuses everything else, so a construct nobody thought about lands
    on the UNCLASSIFIED side rather than being blessed as pure.
    """
    try:
        module_names: "dict[str, str]" = {}   # local name -> dotted module
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name in _PURE_MODULES:
                        pass
                    elif name in _PURE_MODULE_ATTRS:
                        pass
                    else:
                        return f"imports {name}"
                    module_names[alias.asname or name.split(".")[0]] = name
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    return "uses a relative import"
                mod = node.module or ""
                if mod not in _PURE_MODULES:
                    return f"imports from {mod}"
                for alias in node.names:
                    if alias.name == "*":
                        return f"star-imports {mod}"
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in _IMPURE_BUILTINS:
                    return f"calls {node.id}()"

        # Every attribute reached on a conditionally-pure module must be allowed.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if not isinstance(base, ast.Name):
                continue
            mod = module_names.get(base.id)
            allowed = _PURE_MODULE_ATTRS.get(mod) if mod else None
            if allowed is None:
                continue
            # the FIRST attribute off the module name is the one that decides
            first = node
            while isinstance(first.value, ast.Attribute):
                first = first.value
            if first.attr not in allowed:
                return f"uses {mod}.{first.attr}"
        return None
    except Exception:  # noqa: BLE001 — a purity proof that raises proves nothing
        return "purity check failed"


def is_provably_effect_free(code: str) -> bool:
    """PUBLIC: does *code* carry the machine-checkable witness of having no effects?

    True only for a body that both scans to no effect sink AND passes
    :func:`_purity_failure`. This is the sole minting authority for
    :data:`EffectTag.NO_EFFECT`."""
    return EffectTag.NO_EFFECT in classify_source(code)


def normalize_tagset(values) -> "list[str]":
    """Coerce an iterable of tags to sorted canonical values, enforcing the one
    invariant the vocabulary has: **NO_EFFECT is EXCLUSIVE**.

    ``["no_effect", "net_mutate"]`` is a self-contradicting record — a reader that
    stops at the first tag draws the opposite conclusion from one that stops at the
    second. Whenever any real class is present the verified-none witness is dropped,
    never the other way round, so the escalate-only direction is preserved.
    """
    out = {coerce(v) for v in (values or ())}
    if len(out) > 1:
        out.discard(EffectTag.NO_EFFECT.value)
    return sorted(out)


def classify_source(code: str) -> Set[EffectTag]:
    """Deterministically classify a tool's source into effect tags.

    A SIGNAL (advisory, escalate-only). Unparseable source ⇒ ``{UNKNOWN}``.

    Three OUTCOMES, and F14 exists because two of them used to be the same value:

      * a non-empty set of real classes — classified, with effects;
      * ``{NO_EFFECT}`` — classified, and PROVED to have none (see
        :func:`_purity_failure`). A positive witness, never an inference from
        silence;
      * ``set()`` — UNCLASSIFIED. The scan found nothing AND could not prove the
        body inert, e.g. because it reaches its sink through a helper module the
        import-binding analysis cannot follow. The absence of a tag is still never
        treated as "no effect".
    """
    if not code or not code.strip():
        return set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {EffectTag.UNKNOWN}
    # LAZY import (see the module docstring's cycle note): effect_signals imports the
    # EffectTag enum from THIS module, so importing it here — after effect_tags is
    # fully loaded — is cycle-free. A failure degrades to the structural scan.
    try:
        from systemu.runtime import effect_signals as _sig
    except Exception:
        _sig = None
    visitor = _EffectVisitor(_sig)
    visitor.visit(tree)
    if visitor.tags:
        return visitor.tags
    # Nothing found. That is UNCLASSIFIED unless the body can be PROVED inert.
    return set() if _purity_failure(tree) else {EffectTag.NO_EFFECT}
