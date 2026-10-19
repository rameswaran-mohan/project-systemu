"""Capability diff: what each standing tool approval APPROVED vs. what is on disk now.

WHY THIS EXISTS
    A standing tool approval ("Always allow") is stored as ONE opaque sha1 --
    ``command_approvals.tool_signature`` over the tool name, the sha1 of its
    implementation bytes, its declared effect tags and its host class. That is the
    right shape for the gate: change any part and the signature no longer matches,
    so the tool asks again. But it is unreadable after the fact. An operator who
    asks "is the thing I approved still the thing that runs?" deserves an answer
    that names WHAT changed, row by row:

      body         approved implementation hash vs the file on disk now
      effect tags  approved declared tags vs the tags declared now
      host class   approved vs current -- ``unknown`` when either side cannot be
                   determined, and an unknown is NEVER reported as a match
      dependency   each approved dependency spec vs the installed distribution

    plus one verdict per tool, ``as approved`` only when every row is ``match``.

WHERE THE APPROVED PARTS COME FROM
    Not the approval store: it holds the sha1 and nothing else. The parts come
    from the DECISION LEDGER -- the live gate stamps ``tool_parts`` into the card's
    decision context at the moment it computes the signature, and the bulk
    first-gate card carries each entry's body hash. Those are vault files, so a
    ledger row is believed ONLY when its parts recompute to the approved sha1
    (``command_approvals.parts_reproduce_signature``). A row that does not is
    ignored, never half-trusted.

    An approval with no believable ledger row is still attributed when it IS the
    signature of a tool as that tool is now -- the sha1 then binds body and tags,
    so both genuinely match. Anything else is listed as unattributable, and keeps
    the report from claiming everything is as approved.

READING DISCIPLINE
    Every value read from disk is type-pinned in the frame that reads it
    (``type(x) is T``) before anything operates on it. The approval stores are
    read strictly: a file that exists but cannot be parsed raises
    :class:`ApprovalStoreUnreadable` (the CLI exits 2 and names it) -- the runtime
    stores' own lenient "start empty" fallback would make a corrupt store read as
    "nothing approved, nothing drifted".

ALL VERDICT STRINGS ARE ASCII.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Row statuses. ASCII, lower-case, stable: `--json` consumers key on them.
MATCH = "match"
DRIFTED = "drifted"
MISSING = "missing"
UNKNOWN = "unknown"
DIFFERENT = "different"
NOT_INSTALLED = "not installed"

COMMAND_APPROVALS_FILE = "command_approvals.json"
DEP_APPROVALS_FILE = "dep_approvals.json"

_NOT_BOUND = "(not bound)"
_NOT_DETERMINED = "(not determined)"


class ApprovalStoreUnreadable(Exception):
    """An approval store exists but cannot be read as one. Never confused with
    an empty store: a missing file is empty, a broken one is THIS."""

    def __init__(self, path, reason: str):
        self.path = str(path)
        self.reason = reason
        super().__init__(f"cannot read the approval store at {self.path} ({reason})")


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DiffRow:
    kind: str          # "body" | "effects" | "host" | "dep"
    label: str         # what the operator reads: "body", "effect tags", ...
    status: str
    approved: str
    current: str
    detail: str = ""


@dataclass(frozen=True)
class ApprovedParts:
    """What one standing approval bound. ``host_class`` None = it bound none."""
    name: str
    tool_id: str
    body_hash: str
    effect_tags: Tuple[str, ...]
    host_class: Optional[str]


@dataclass(frozen=True)
class CurrentState:
    """A tool as it is now. ``host_class`` None = it cannot be determined."""
    name: str
    tool_id: str
    body_hash: str
    body_missing: bool
    effect_tags: Tuple[str, ...]
    host_class: Optional[str]
    dependencies: Tuple[str, ...]


@dataclass(frozen=True)
class ToolDiff:
    name: str
    tool_id: str
    signature: str
    rows: Tuple[DiffRow, ...]
    note: str = ""

    @property
    def as_approved(self) -> bool:
        return bool(self.rows) and all(r.status == MATCH for r in self.rows)

    @property
    def verdict(self) -> str:
        if self.as_approved:
            return "as approved"
        for r in self.rows:
            if r.status != MATCH:
                return f"{r.label} {r.status}"
        return "nothing to compare"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "tool_id": self.tool_id,
                "signature": self.signature, "verdict": self.verdict,
                "as_approved": self.as_approved, "note": self.note,
                "rows": [asdict(r) for r in self.rows]}


@dataclass(frozen=True)
class DiffReport:
    tools: Tuple[ToolDiff, ...]
    unattributed: Tuple[str, ...]
    only: Optional[str] = None
    message: str = ""          # set when a named tool has nothing to report

    @property
    def exit_code(self) -> int:
        if self.only is not None:
            if not self.tools:
                return 1
            return 0 if all(t.as_approved for t in self.tools) else 1
        if self.unattributed:
            return 1
        return 0 if all(t.as_approved for t in self.tools) else 1

    def to_dict(self) -> Dict[str, Any]:
        return {"tools": [t.to_dict() for t in self.tools],
                "unattributed": list(self.unattributed),
                "message": self.message, "exit_code": self.exit_code}


# --------------------------------------------------------------------------- #
# The pure builder
# --------------------------------------------------------------------------- #

def _short(h: str) -> str:
    return h[:12] if len(h) == 40 else h


def _tags_text(tags: Sequence[str]) -> str:
    return ", ".join(tags) if tags else "(none declared)"


def _norm_host(value) -> Optional[str]:
    """A host class, or None when there is none to compare: not a string, or the
    empty class -- which is "not bound", never a value two sides can share."""
    if type(value) is not str or not value:
        return None
    return value


def host_row(approved, current) -> DiffRow:
    """The host-class row. ``unknown`` whenever EITHER side is undetermined: two
    absences are not evidence of agreement, so they never read as ``match``."""
    a = _norm_host(approved)
    c = _norm_host(current)
    a_text = a if a is not None else _NOT_BOUND
    c_text = c if c is not None else _NOT_DETERMINED
    if a is None:
        return DiffRow("host", "host class", UNKNOWN, a_text, c_text,
                       "approved before host checks existed; the approval bound "
                       "no host class")
    if c is None:
        return DiffRow("host", "host class", UNKNOWN, a_text, c_text,
                       "the current host class cannot be determined")
    if a == c:
        return DiffRow("host", "host class", MATCH, a_text, c_text)
    return DiffRow("host", "host class", DRIFTED, a_text, c_text)


def _body_row(approved: Optional[ApprovedParts],
              current: Optional[CurrentState]) -> DiffRow:
    a_text = _short(approved.body_hash) if approved is not None else "(not recorded)"
    if current is None:
        return DiffRow("body", "body", MISSING, a_text, "(no such tool)",
                       "the tool is no longer in the vault")
    if current.body_missing:
        return DiffRow("body", "body", MISSING, a_text, "(file missing)",
                       "the implementation file cannot be read")
    c_text = _short(current.body_hash)
    if approved is None:
        return DiffRow("body", "body", UNKNOWN, a_text, c_text,
                       "approved before approvals recorded what they bind; the "
                       "approval no longer matches this tool, so something changed")
    if approved.body_hash == current.body_hash:
        return DiffRow("body", "body", MATCH, a_text, c_text)
    return DiffRow("body", "body", DRIFTED, a_text, c_text,
                   "the implementation changed since it was approved")


def _effects_row(approved: Optional[ApprovedParts],
                 current: Optional[CurrentState]) -> DiffRow:
    a_text = _tags_text(approved.effect_tags) if approved is not None else "(not recorded)"
    if current is None:
        return DiffRow("effects", "effect tags", MISSING, a_text, "(no such tool)")
    c_text = _tags_text(current.effect_tags)
    if approved is None:
        return DiffRow("effects", "effect tags", UNKNOWN, a_text, c_text)
    if tuple(sorted(set(approved.effect_tags))) == tuple(sorted(set(current.effect_tags))):
        return DiffRow("effects", "effect tags", MATCH, a_text, c_text)
    return DiffRow("effects", "effect tags", DRIFTED, a_text, c_text)


def diff_tool(*, signature: str, approved: Optional[ApprovedParts],
              current: Optional[CurrentState], dep_rows: Sequence[DiffRow] = (),
              name: str = "", tool_id: str = "", note: str = "") -> ToolDiff:
    """Compare one approval with one tool. Pure: no disk, no clock.

    ``approved`` None = the approval is attributed to this tool but what it bound
    is not known (rows read ``unknown``). ``current`` None = the tool is gone.
    """
    rows: List[DiffRow] = [
        _body_row(approved, current),
        _effects_row(approved, current),
        host_row(approved.host_class if approved is not None else None,
                 current.host_class if current is not None else None),
    ]
    rows.extend(dep_rows)
    _name = (approved.name if approved is not None and approved.name else
             current.name if current is not None and current.name else name)
    _tid = (approved.tool_id if approved is not None and approved.tool_id else
            current.tool_id if current is not None and current.tool_id else tool_id)
    return ToolDiff(name=_name, tool_id=_tid, signature=signature,
                    rows=tuple(rows), note=note)


# --------------------------------------------------------------------------- #
# Dependency rows
# --------------------------------------------------------------------------- #

# The installer's spec grammar, split into name + specifier (extras ignored).
_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_\-\.]*)"
    r"(?:\[[A-Za-z0-9_,\-]+\])?"
    r"(?P<spec>(?:\s*(?:==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9_\-\.\*\+]+\s*,?)*)\s*$")


def installed_version(dist: str) -> Optional[str]:
    """The installed version of distribution ``dist``; ``""`` when it is
    importable under that name but has no readable distribution version (the
    installer's own import-name fallback, e.g. ``PIL``); None when absent."""
    try:
        from importlib import metadata as _md
        v = _md.version(dist)
        return v if type(v) is str else ""
    except Exception:
        pass
    if "." in dist:          # never import a parent package just to ask
        return None
    try:
        import importlib.util as _ilu
        return "" if _ilu.find_spec(dist) is not None else None
    except Exception:
        return None


def _dep_row(spec: str, installed_fn: Callable[[str], Optional[str]]) -> DiffRow:
    label = f"dependency {spec}"
    m = _SPEC_RE.match(spec)
    if m is None:
        return DiffRow("dep", label, UNKNOWN, spec, "(unparseable)",
                       "the approved spec does not parse")
    dist = m.group("name")
    specifier = (m.group("spec") or "").strip().rstrip(",").strip()
    try:
        have = installed_fn(dist)
    except Exception:
        have = None
    if have is None:
        return DiffRow("dep", label, NOT_INSTALLED, spec, "(not installed)")
    if type(have) is not str:
        return DiffRow("dep", label, UNKNOWN, spec, "(unreadable)")
    if not specifier:
        return DiffRow("dep", label, MATCH, spec, have or "(installed, version unknown)")
    if not have:
        return DiffRow("dep", label, UNKNOWN, spec, "(installed, version unknown)",
                       "installed, but its version cannot be read to check the spec")
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        ok = SpecifierSet(specifier.replace(" ", "")).contains(Version(have),
                                                               prereleases=True)
    except Exception:
        return DiffRow("dep", label, UNKNOWN, spec, have,
                       "the installed version cannot be checked against the spec")
    return DiffRow("dep", label, MATCH if ok else DIFFERENT, spec, have)


def dep_rows_for(*, tool_id: str, name: str, declared: Sequence[str],
                 dep_approvals: Dict[str, dict],
                 installed_fn: Callable[[str], Optional[str]]) -> Tuple[DiffRow, ...]:
    """One row per dependency approval that covers this tool: approvals first
    requested by this tool, plus approvals for the specs it declares now."""
    covered = set()
    for spec, entry in dep_approvals.items():
        if type(spec) is not str or type(entry) is not dict:
            continue
        first_id = entry.get("first_seen_tool_id")
        first_name = entry.get("first_seen_tool")
        if type(first_id) is str and first_id:
            if tool_id and first_id == tool_id:
                covered.add(spec)
        elif type(first_name) is str and first_name and first_name == name:
            covered.add(spec)
    for spec in declared:
        if type(spec) is str and spec in dep_approvals:
            covered.add(spec)
    return tuple(_dep_row(s, installed_fn) for s in sorted(covered))


# --------------------------------------------------------------------------- #
# Strict readers
# --------------------------------------------------------------------------- #

def _read_json_object(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ApprovalStoreUnreadable(path, type(exc).__name__) from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ApprovalStoreUnreadable(path, "not valid JSON") from exc
    if type(data) is not dict:
        raise ApprovalStoreUnreadable(path, "not a JSON object")
    return data


def _approved_section(path: Path) -> Dict[str, Any]:
    data = _read_json_object(path)
    approved = data.get("approved", {})
    if type(approved) is not dict:
        raise ApprovalStoreUnreadable(path, "its 'approved' section is not an object")
    return approved


def load_tool_approvals(path: Path) -> Tuple[Dict[str, dict], Tuple[str, ...]]:
    """(standing TOOL approvals keyed by signature, malformed entry keys).

    The command-approval store also holds shell-command and MCP allows; those
    carry their command text (``"mcp:..."`` for MCP) and are skipped. A tool allow
    is recorded with an empty command. An entry that is not an object cannot be
    classified at all and is returned as malformed -- never dropped silently.
    """
    approved = _approved_section(path)
    tools: Dict[str, dict] = {}
    malformed: List[str] = []
    for sig, entry in approved.items():
        if type(entry) is not dict:
            malformed.append(sig)
            continue
        command = entry.get("command", "")
        if type(command) is not str:
            malformed.append(sig)
            continue
        if command:
            continue
        tools[sig] = entry
    return tools, tuple(sorted(malformed))


def load_dep_approvals(path: Path) -> Dict[str, dict]:
    approved = _approved_section(path)
    return {k: v for k, v in approved.items() if type(v) is dict}


def _parts_from(raw) -> Optional[ApprovedParts]:
    """ApprovedParts from a verified parts dict (already type-checked by the
    verifier, re-pinned here because this frame operates on them)."""
    if type(raw) is not dict:
        return None
    name, tid, body = raw.get("name"), raw.get("tool_id", ""), raw.get("body_hash")
    tags, host = raw.get("effect_tags"), raw.get("host_class", "")
    if (type(name) is not str or type(body) is not str or type(host) is not str
            or (type(tags) is not list and type(tags) is not tuple)):
        return None
    if type(tid) is not str:
        tid = ""
    clean_tags = tuple(sorted({t for t in tags if type(t) is str}))
    return ApprovedParts(name=name, tool_id=tid, body_hash=body,
                         effect_tags=clean_tags,
                         host_class=approved_host_class(host, clean_tags))


def approved_host_class(host, tags) -> Optional[str]:
    """What host class a recorded approval bound, from its recorded parts.

    An empty class is how every approval minted BEFORE host checks recorded it.
    For a tool whose tags carry no network effect that is exactly the class
    ``none`` (the upgrade ruling: such approvals stay valid, and ``none`` signs as
    the empty string). For a networked tool it means the approval bound NO host --
    returned as None, which the host row reports as unknown, never as a match.
    """
    if type(host) is not str:
        return None
    if host:
        return host
    from systemu.runtime.action_governance import has_network_egress
    from systemu.runtime.command_approvals import HOST_CLASS_NONE
    return None if has_network_egress(list(tags)) else HOST_CLASS_NONE


def load_ledger(vault) -> Tuple[Dict[str, ApprovedParts], Dict[str, Tuple[str, str]]]:
    """Read the decision ledger for tool-approval cards.

    Returns ``(verified, named)``: ``verified`` maps a signature to the parts that
    recompute to it; ``named`` maps a signature to the (tool name, tool id) a card
    stamped beside it when no believable parts exist -- enough to say WHICH tool,
    not what it bound. Best-effort: an unreadable decision is skipped.
    """
    from systemu.runtime.command_approvals import parts_reproduce_signature
    from systemu.runtime.first_gate_review import BULK_DEDUP_PREFIX

    verified: Dict[str, ApprovedParts] = {}
    named: Dict[str, Tuple[str, str]] = {}

    def _name_it(sig, name, tid):
        if sig in verified:
            return
        n = name if type(name) is str else ""
        t = tid if type(tid) is str else ""
        if n or t:
            named.setdefault(sig, (n, t))

    def _take(sig, parts, name, tid):
        if type(sig) is not str or not sig:
            return
        if parts_reproduce_signature(sig, parts):
            p = _parts_from(parts)
            if p is not None:
                verified[sig] = p
                named.pop(sig, None)
                return
        _name_it(sig, name, tid)

    try:
        headers = vault.load_index("decisions") or []
    except Exception:
        logger.debug("[CapabilityDiff] decision index unreadable", exc_info=True)
        headers = []
    if type(headers) is not list:
        headers = []
    for h in headers:
        if type(h) is not dict:
            continue
        did, dedup = h.get("id"), h.get("dedup_key")
        if type(did) is not str:
            continue
        if type(dedup) is str and not (dedup.startswith("tool:")
                                       or dedup.startswith(BULK_DEDUP_PREFIX)):
            continue
        try:
            ctx = getattr(vault.get_decision(did), "context", None)
        except Exception:
            continue
        if type(ctx) is not dict:
            continue
        sig = ctx.get("tool_signature")
        if type(sig) is str and sig:
            _take(sig, ctx.get("tool_parts"), ctx.get("tool_name"), ctx.get("tool_id"))
        entries = ctx.get("bulk_entries")
        if type(entries) is list:
            for e in entries:
                if type(e) is not dict:
                    continue
                parts = {"name": e.get("name"), "tool_id": e.get("tool_id", ""),
                         "body_hash": e.get("body_hash"),
                         "effect_tags": e.get("effect_tags"),
                         "host_class": e.get("host_class", "")}
                _take(e.get("signature"), parts, e.get("name"), e.get("tool_id"))
    return verified, named


def load_current_tools(vault) -> List[Tuple[CurrentState, str]]:
    """Every vault tool as it is now, with the signature the live gate would key
    it on (same body-hash function, same host-class function, same tag set)."""
    from systemu.runtime.command_approvals import (signature_host_class,
                                                   tool_host_class, tool_signature)
    from systemu.runtime.tool_sandbox import tool_body_hash

    root = Path(getattr(vault, "root", None) or ".").resolve()
    out: List[Tuple[CurrentState, str]] = []
    try:
        headers = vault.load_index("tools") or []
    except Exception:
        logger.debug("[CapabilityDiff] tool index unreadable", exc_info=True)
        return out
    if type(headers) is not list:
        return out
    for h in headers:
        if type(h) is not dict or type(h.get("id")) is not str:
            continue
        try:
            tool = vault.get_tool(h["id"])
        except Exception:
            continue
        name = getattr(tool, "name", "")
        tid = getattr(tool, "id", "")
        if type(name) is not str or type(tid) is not str:
            continue
        raw_tags = getattr(tool, "effect_tags", None) or []
        if type(raw_tags) is not list and type(raw_tags) is not tuple:
            raw_tags = []
        tags = tuple(sorted({str(t) for t in raw_tags}))
        raw_deps = getattr(tool, "dependencies", None) or []
        deps = tuple(d for d in raw_deps if type(d) is str) if (
            type(raw_deps) is list or type(raw_deps) is tuple) else ()
        body_hash, missing = tool_body_hash(tool, root)
        host_class = tool_host_class(tool)
        sig = tool_signature(name, body_hash, set(tags),
                             host_class=signature_host_class(host_class))
        out.append((CurrentState(name=name, tool_id=tid, body_hash=body_hash,
                                 body_missing=missing, effect_tags=tags,
                                 host_class=_norm_host(host_class),
                                 dependencies=deps), sig))
    return out


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def build_report(*, tool_approvals: Dict[str, dict], malformed: Sequence[str] = (),
                 dep_approvals: Dict[str, dict],
                 current: Sequence[Tuple[CurrentState, str]],
                 verified: Dict[str, ApprovedParts],
                 named: Dict[str, Tuple[str, str]],
                 installed_fn: Callable[[str], Optional[str]] = installed_version,
                 only: Optional[str] = None) -> DiffReport:
    by_sig = {sig: cs for cs, sig in current}
    sig_of = {id(cs): sig for cs, sig in current}
    by_id = {cs.tool_id: cs for cs, _ in current if cs.tool_id}
    by_name = {cs.name: cs for cs, _ in current if cs.name}

    def _find(tid: str, name: str) -> Optional[CurrentState]:
        return by_id.get(tid) or by_name.get(name)

    # key -> list of (sig, approved parts or None, current or None, name, tid, at)
    groups: Dict[str, List[tuple]] = {}
    unattributed: List[str] = list(malformed)
    for sig, entry in tool_approvals.items():
        at = entry.get("approved_at")
        at = at if type(at) is str else ""
        if sig in by_sig:
            cs = by_sig[sig]
            parts = verified.get(sig) or ApprovedParts(
                name=cs.name, tool_id=cs.tool_id, body_hash=cs.body_hash,
                effect_tags=cs.effect_tags, host_class=cs.host_class)
            groups.setdefault(cs.tool_id or cs.name, []).append(
                (sig, parts, cs, cs.name, cs.tool_id, at))
        elif sig in verified:
            p = verified[sig]
            cs = _find(p.tool_id, p.name)
            key = (cs.tool_id or cs.name) if cs is not None else (p.tool_id or p.name)
            groups.setdefault(key, []).append((sig, p, cs, p.name, p.tool_id, at))
        elif sig in named:
            n, t = named[sig]
            cs = _find(t, n)
            key = (cs.tool_id or cs.name) if cs is not None else (t or n)
            groups.setdefault(key, []).append((sig, None, cs, n, t, at))
        else:
            unattributed.append(sig)

    diffs: List[ToolDiff] = []
    for _key, cands in groups.items():
        cs = cands[0][2]
        cur_sig = sig_of.get(id(cs)) if cs is not None else None
        # The approval to report: the one that IS the tool as it is now; else the
        # newest one that knows what it bound; else the newest.
        cands = sorted(cands, key=lambda c: (c[0] == cur_sig, c[1] is not None, c[5]),
                       reverse=True)
        sig, parts, cs, n, t, _at = cands[0]
        note = (f"{len(cands) - 1} older approval(s) for this tool are also on record"
                if len(cands) > 1 else "")
        tid = (cs.tool_id if cs is not None else "") or (parts.tool_id if parts else t)
        nm = (cs.name if cs is not None else "") or (parts.name if parts else n)
        deps = dep_rows_for(tool_id=tid, name=nm,
                            declared=cs.dependencies if cs is not None else (),
                            dep_approvals=dep_approvals, installed_fn=installed_fn)
        diffs.append(diff_tool(signature=sig, approved=parts, current=cs,
                               dep_rows=deps, name=nm, tool_id=tid, note=note))
    diffs.sort(key=lambda d: (d.name, d.tool_id))

    message = ""
    if only is not None:
        diffs = [d for d in diffs if only in (d.name, d.tool_id)]
        if not diffs:
            exists = only in by_name or only in by_id
            message = (f"{only}: no standing approval on record for this tool."
                       if exists else
                       f"{only}: no tool by that name or id, and no standing approval "
                       "names one.")
            if unattributed:
                message += (f" {len(unattributed)} approval(s) could not be attributed "
                            "to any tool and may include it.")
    return DiffReport(tools=tuple(diffs), unattributed=tuple(sorted(set(unattributed))),
                      only=only, message=message)


def build_report_from_disk(*, vault, data_dir: Path, only: Optional[str] = None,
                           installed_version: Optional[Callable[[str], Optional[str]]] = None
                           ) -> DiffReport:
    """Read both approval stores (strictly), the ledger and the vault, and build
    the report. Raises :class:`ApprovalStoreUnreadable` for a broken store."""
    data_dir = Path(data_dir)
    tool_approvals, malformed = load_tool_approvals(data_dir / COMMAND_APPROVALS_FILE)
    dep_approvals = load_dep_approvals(data_dir / DEP_APPROVALS_FILE)
    verified, named = load_ledger(vault) if tool_approvals else ({}, {})
    current = load_current_tools(vault) if (tool_approvals or only) else []
    return build_report(tool_approvals=tool_approvals, malformed=malformed,
                        dep_approvals=dep_approvals, current=current,
                        verified=verified, named=named,
                        installed_fn=installed_version or globals()["installed_version"],
                        only=only)


# --------------------------------------------------------------------------- #
# Rendering (ASCII)
# --------------------------------------------------------------------------- #

def _ascii(text: str) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def render_text(report: DiffReport) -> str:
    lines: List[str] = []
    if report.only is not None and not report.tools:
        return _ascii(report.message) + "\n"
    if not report.tools and not report.unattributed:
        return "No standing tool approvals on record -- nothing to compare.\n"
    lines.append("Capability diff: what each standing tool approval approved, "
                 "compared with what is on disk now.")
    for td in report.tools:
        lines.append("")
        head = td.name + (f"  ({td.tool_id})" if td.tool_id and td.tool_id != td.name
                          else "")
        lines.append(head)
        for r in td.rows:
            lines.append(f"  {r.label:<24} {r.status:<14} approved {r.approved}"
                         f"   now {r.current}")
            if r.detail and r.status != MATCH:
                lines.append(f"  {'':<24} {'':<14} ({r.detail})")
        if td.note:
            lines.append(f"  note: {td.note}")
        lines.append(f"  verdict: {td.verdict}")
    if report.tools:
        ok = sum(1 for t in report.tools if t.as_approved)
        lines.append("")
        lines.append(f"{len(report.tools)} tool(s) with a standing approval: "
                     f"{ok} as approved, {len(report.tools) - ok} not.")
    if report.message:
        lines.append(report.message)
    if report.unattributed and report.only is None:
        lines.append("")
        lines.append(f"{len(report.unattributed)} standing tool approval(s) cannot be "
                     "attributed to a tool: they record no parts and match no tool as "
                     "it is now. Approving the tool again records what it binds.")
        for sig in report.unattributed:
            lines.append(f"  {sig[:16]}")
    return _ascii("\n".join(lines)) + "\n"


def render_json(report: DiffReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=True)


# =========================================================================== #
# What approving a dependency would pull in (the TRANSITIVE install set)
# =========================================================================== #
#
# Approving "install X" runs `pip install X`, which installs X and every package
# X needs that is not already present. The dependency card used to name X alone.
# This resolves the real set with pip's own resolver in dry-run mode, BEFORE the
# approval, and renders it -- or says plainly that it could not. It never
# renders a guessed list: a consent surface that invents what it consents to is
# worse than one that says nothing.
#
# BLOCKING by nature (a subprocess, up to PIP_DRY_RUN_TIMEOUT_S). It lives here,
# in the runtime layer, and is reached from the interface layer only through
# `prefetch_transitive` (a worker thread) or the CLI process -- never from a
# UI handler's own thread.

PIP_DRY_RUN_TIMEOUT_S = 60

#: The section header on the card and in the CLI. One constant, because the
#: card rebuild finds and REPLACES the section by it -- two spellings would mean
#: two sections.
DEP_TREE_HEADER = "What installing it would add"

#: Lines of packages a CARD shows before it discloses the remainder. The CLI
#: shows them all.
CARD_PACKAGE_LIMIT = 25


@dataclass(frozen=True)
class TransitivePackage:
    name: str
    version: str
    installed: Optional[str]     # None = not installed; "" = present, version unknown

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version, "installed": self.installed}


@dataclass(frozen=True)
class TransitivePlan:
    spec: str
    ok: bool
    packages: Tuple[TransitivePackage, ...] = ()
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"spec": self.spec, "ok": self.ok, "reason": self.reason,
                "packages": [p.to_dict() for p in self.packages]}


def _run_pip(argv: List[str], timeout: float) -> Tuple[int, str]:
    """The subprocess seam: run pip, return (exit code, stderr). Raises
    ``subprocess.TimeoutExpired`` past ``timeout``. ``errors="backslashreplace"``
    so a non-UTF-8 byte in pip's output can never raise in place of a verdict
    (DEC-32c)."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="backslashreplace", timeout=timeout,
                          stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stderr or ""


#: Replaced in tests -- never the network from a test.
_pip_runner: Callable[[List[str], float], Tuple[int, str]] = _run_pip

_CACHE: Dict[str, "TransitivePlan"] = {}
_IN_FLIGHT: Dict[str, threading.Event] = {}
_CACHE_LOCK = threading.Lock()

_OFFLINE_MARKERS = ("NewConnectionError", "Failed to establish a new connection",
                    "getaddrinfo failed", "Temporary failure in name resolution",
                    "Network is unreachable", "No route to host",
                    "Connection refused", "ConnectTimeoutError", "ReadTimeoutError",
                    "ProxyError")


def _failure_reason(rc: int, stderr) -> str:
    err = stderr if type(stderr) is str else ""
    if any(m in err for m in _OFFLINE_MARKERS):
        return "the package index could not be reached"
    if "No matching distribution found" in err:
        return "no matching distribution was found"
    return f"pip exited with code {rc}"


def _valid_spec(spec) -> bool:
    """The installer's own spec grammar -- the same one a tool manifest must pass.
    It cannot start with '-', so a hand-edited pending entry cannot pass pip an
    OPTION (an index URL, a requirements file) in place of a package."""
    if type(spec) is not str or not spec.strip() or spec.lstrip().startswith("-"):
        return False
    try:
        from systemu.runtime.dependency_installer import _normalise_and_validate
        return _normalise_and_validate([spec]) == [spec.strip()]
    except Exception:
        return False


def _parse_report(path: Path, installed_fn) -> Optional[Tuple[TransitivePackage, ...]]:
    """Packages from pip's ``--report`` JSON, or None when the report is not the
    shape pip documents. Every value is type-pinned before use."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if type(data) is not dict:
        return None
    install = data.get("install", [])
    if type(install) is not list:
        return None
    out: List[TransitivePackage] = []
    for item in install:
        if type(item) is not dict:
            return None
        meta = item.get("metadata")
        if type(meta) is not dict:
            return None
        name, version = meta.get("name"), meta.get("version")
        if type(name) is not str or type(version) is not str or not name:
            return None
        try:
            have = installed_fn(name)
        except Exception:
            have = None
        out.append(TransitivePackage(name=name, version=version,
                                     installed=have if type(have) is str else None))
    return tuple(out)


def resolve_transitive(spec: str, *, runner=None, timeout: float = PIP_DRY_RUN_TIMEOUT_S,
                       installed_fn=None, use_cache: bool = True) -> TransitivePlan:
    """Resolve what ``pip install <spec>`` would install. BLOCKING (a subprocess,
    bounded by ``timeout``) -- call it from a worker thread or a CLI process.

    Cached per spec for the session, failures included: a resolution that failed
    because the machine is offline is not retried on every card render.
    """
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(spec)
        if hit is not None:
            return hit
    plan = _resolve_uncached(spec, runner=runner or _pip_runner, timeout=timeout,
                             installed_fn=installed_fn or installed_version)
    with _CACHE_LOCK:
        _CACHE[spec] = plan
    return plan


def _resolve_uncached(spec, *, runner, timeout, installed_fn) -> TransitivePlan:
    if not _valid_spec(spec):
        return TransitivePlan(spec=str(spec), ok=False,
                              reason="not a valid package spec")
    tmp = tempfile.mkdtemp(prefix="systemu-dep-tree-")
    report = Path(tmp) / "report.json"
    argv = [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
            "--no-input", "--disable-pip-version-check", "--report", str(report),
            spec.strip()]
    try:
        try:
            rc, stderr = runner(argv, timeout)
        except subprocess.TimeoutExpired:
            return TransitivePlan(spec=spec, ok=False,
                                  reason=f"pip did not finish within {int(timeout)}s")
        except Exception as exc:
            return TransitivePlan(spec=spec, ok=False,
                                  reason=f"pip could not be started ({type(exc).__name__})")
        if type(rc) is not int or rc != 0:
            return TransitivePlan(spec=spec, ok=False,
                                  reason=_failure_reason(rc if type(rc) is int else -1,
                                                         stderr))
        packages = _parse_report(report, installed_fn)
        if packages is None:
            return TransitivePlan(spec=spec, ok=False, reason="pip's report was malformed")
        return TransitivePlan(spec=spec, ok=True, packages=packages)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cached_transitive(spec: str) -> Optional[TransitivePlan]:
    with _CACHE_LOCK:
        return _CACHE.get(spec)


def transitive_in_flight(spec: str) -> bool:
    with _CACHE_LOCK:
        return spec in _IN_FLIGHT


def reset_transitive_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _IN_FLIGHT.clear()


def prefetch_transitive(spec: str) -> bool:
    """Start resolving ``spec`` on a WORKER thread and return at once. False when
    it is already resolved or already resolving. Never blocks the caller."""
    with _CACHE_LOCK:
        if spec in _CACHE or spec in _IN_FLIGHT:
            return False
        done = threading.Event()
        _IN_FLIGHT[spec] = done

    def _work():
        try:
            resolve_transitive(spec)
        except Exception:
            logger.debug("[CapabilityDiff] dependency-tree prefetch failed for %r",
                         spec, exc_info=True)
        finally:
            with _CACHE_LOCK:
                _IN_FLIGHT.pop(spec, None)
            done.set()

    threading.Thread(target=_work, name="dep-tree-prefetch", daemon=True).start()
    return True


def wait_for_transitive(spec: str, timeout: float) -> bool:
    """Block until a prefetch of ``spec`` finishes (tests and the CLI only)."""
    with _CACHE_LOCK:
        ev = _IN_FLIGHT.get(spec)
        if ev is None:
            return spec in _CACHE
    return ev.wait(timeout) and cached_transitive(spec) is not None


def prefetch_armed() -> bool:
    """May building a dependency card start pip on its own?

    Only when the card is posted BY A LIVE RUN in a process that SERVES OPERATOR
    DECISIONS (the daemon sets ``SYSTEMU_DECISION_QUEUE=true``; its dashboard
    thread renders the Inbox from this same process and therefore this same
    cache). Building a card anywhere else -- a test, the CLI, a page render --
    never spawns a network-touching subprocess as a side effect.
    """
    if (os.environ.get("SYSTEMU_DECISION_QUEUE") or "").strip().lower() != "true":
        return False
    try:
        from systemu.runtime.chat_submission_ctx import (current_chat_submission_id,
                                                         current_execution_id)
        return bool(current_execution_id() or current_chat_submission_id())
    except Exception:
        return False


def transitive_lines(spec: str, plan: Optional[TransitivePlan], *,
                     resolving: bool = False,
                     limit: Optional[int] = None) -> List[str]:
    """The ASCII lines that say what installing ``spec`` would add."""
    if plan is None:
        if resolving:
            return [f"{DEP_TREE_HEADER}: resolving now -- reload the Inbox in a "
                    "moment to see it."]
        return [f"{DEP_TREE_HEADER}: not resolved yet -- run "
                "`systemu tools diff --pending` to see it."]
    if not plan.ok:
        return [_ascii(f"{DEP_TREE_HEADER}: could not resolve the dependency tree "
                       f"({plan.reason})")]
    if not plan.packages:
        return [f"{DEP_TREE_HEADER}: nothing -- everything it needs is already "
                "installed."]
    out = [f"{DEP_TREE_HEADER} ({len(plan.packages)} package(s)):"]
    shown = plan.packages if limit is None else plan.packages[:limit]
    for p in shown:
        if p.installed is None:
            mark = "(new)"
        elif p.installed == p.version:
            mark = "(already installed)"
        elif p.installed == "":
            mark = "(already present, version unknown)"
        else:
            mark = f"(replaces installed {p.installed})"
        out.append(_ascii(f"  {p.name} {p.version}  {mark}"))
    rest = len(plan.packages) - len(shown)
    if rest > 0:
        out.append(f"  ...and {rest} more (`systemu tools diff --pending` lists all)")
    return out


def dep_card_section(spec: str, *, transitive: Optional[TransitivePlan] = None,
                     may_prefetch: bool = False) -> str:
    """The dependency card's transitive section. Uses ``transitive`` when given,
    else the session cache; with ``may_prefetch`` (the card is being POSTED) and
    nothing cached, starts a worker-thread resolution when :func:`prefetch_armed`.
    Never blocks."""
    plan = transitive if transitive is not None else cached_transitive(spec)
    resolving = False
    if plan is None:
        if may_prefetch and prefetch_armed():
            prefetch_transitive(spec)
        resolving = transitive_in_flight(spec)
        plan = cached_transitive(spec)      # a very fast resolve may have landed
    return "\n".join(transitive_lines(spec, plan, resolving=resolving and plan is None,
                                      limit=CARD_PACKAGE_LIMIT))


def with_dep_card_section(inspect, spec: str, **kw) -> str:
    """``inspect`` with its transitive section replaced by a fresh one -- so a
    card rebuilt from its stored context shows ONE section, the current one."""
    base = inspect if type(inspect) is str else ""
    cut = base.find("\n\n" + DEP_TREE_HEADER)
    if cut == -1 and base.startswith(DEP_TREE_HEADER):
        cut = 0
    if cut != -1:
        base = base[:cut]
    section = dep_card_section(spec, **kw)
    return f"{base}\n\n{section}" if base else section


def load_pending_deps(path: Path) -> List[Tuple[str, dict]]:
    """Pending dependency requests, read strictly (a broken store raises)."""
    data = _read_json_object(path)
    pending = data.get("pending", {})
    if type(pending) is not dict:
        raise ApprovalStoreUnreadable(path, "its 'pending' section is not an object")
    rows = [(k, v) for k, v in pending.items() if type(v) is dict]
    return sorted(rows, key=lambda kv: kv[0].lower())


def resolve_pending(data_dir: Path, *, runner=None
                    ) -> List[Tuple[str, dict, TransitivePlan]]:
    """Every pending dependency with what installing it would add. BLOCKING:
    for the CLI process only."""
    return [(spec, entry, resolve_transitive(spec, runner=runner))
            for spec, entry in load_pending_deps(Path(data_dir) / DEP_APPROVALS_FILE)]


def render_pending_text(rows) -> str:
    if not rows:
        return "No pending dependency approvals.\n"
    lines = ["Pending dependency approvals -- what approving each would install:"]
    for spec, entry, plan in rows:
        tool = entry.get("first_seen_tool") or entry.get("first_seen_tool_id") or "?"
        tool = tool if type(tool) is str else "?"
        count = entry.get("request_count", 1)
        count = count if type(count) is int else 1
        lines.append("")
        lines.append(f"{spec}  (requested by {tool} x{count})")
        lines.extend(f"  {ln}" for ln in transitive_lines(spec, plan))
    return _ascii("\n".join(lines)) + "\n"
