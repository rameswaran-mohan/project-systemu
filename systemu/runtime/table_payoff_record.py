"""T5 / R-B5 / R-A17 -- the DURABLE per-run record of what the operator's table supplied.

WHAT IT IS
----------
One append-only JSONL file per ROOT run, under the audit data root:

    <audit_data_root>/audit/table_payoff/run_<root>.jsonl

keyed by the ROOT execution id, so every resume of the same run (each resume mints a
fresh execution id but inherits the root) and every sub-agent child (which inherits
the root explicitly) appends to the SAME file. Two row kinds:

  * ``consult`` -- written ONCE per root, the first time a requirement-binder pass ran
    with the operator's table on hand (the composed SituationReport carried at least
    one table-backed entry). This is the inventory-hit metric's DENOMINATOR.
  * ``supplied`` -- one per requirement the table supplied, written at the moment the
    binder accepted a table item: the item id and name, the value ORIGIN, how it bound
    (``prefilled_confirm`` vs ``silent``) and a timestamp. NEVER the resolved value,
    its digest, or the bound source handle (a handle can embed an account identity).

WHY IT EXISTS
-------------
The inventory-hit metric used to read the RequirementReport cached in each run's
resume snapshot. ``execution_snapshot.delete_snapshot`` removes that file the moment a
resume consumes it, so the measured population SHRANK over time -- the rate was
computed over whatever happened to survive, which is the DEC-27 defect shape (a
denominator must be witnessed, never inferred from what is left). Nothing in the
snapshot lifecycle touches this record: it survives resume, run end and snapshot
consumption.

WRITER (CONC-MAP)
-----------------
SOLE caller of :func:`record_binder_pass` is ``runtime/shadow_runtime.py``, at the two
requirement-binder call sites (the pre-loop producer and the mid-loop TOOL_CALL
binder), on the shadow execution thread. Concurrent RUNS are concurrent writers to the
DIRECTORY but never to the same run file except through resumes/sub-agents of one run.
Every write runs under a process-wide lock plus a best-effort OS lock on a SIDECAR
``.lock`` file (never on a data file: a mandatory Windows byte lock on a data file would
make every concurrent READER fail). Under that lock the writer reads the run file,
drops rows already recorded (one line per supplied requirement, not one per loop
iteration), and appends the remainder with ONE ``os.write`` on an ``O_APPEND`` fd.

Lookup aliases -- ``alias/exec_<id>.txt`` and ``alias/chat_<id>.txt``, each holding a
root id -- let a surface that only knows the CURRENT execution id (a resumed run) or the
chat submission id find the root's file without scanning. They are DERIVED (the same
value every time), written by the same writer under the same lock, tmp + ``os.replace``.

RETENTION
---------
UNBOUNDED. No existing retention rule covers this tree: ``vault.prune_old_executions``
prunes ``<vault>/executions/`` only, ``delete_snapshot`` removes one resume snapshot
file, and DEC-28's ruled audit-corpus rotation (for ``<vault>/audit/*.jsonl``) is not
built in this tree. Growth is one small file per root run that had the table on hand
(a consult row plus one row per distinct supplied requirement) and one alias file per
resumed execution / chat submission. This docstring and the CONC-MAP row say so; no
silent growth claim is made anywhere.

READERS
-------
Everything here is observability. No reader can change a bind, a taint or an ask.
Readers never raise into a render: an absent record reads as ``None`` ("nothing
recorded"), and :func:`scan_records` COUNTS unreadable files rather than dropping them,
so the metric can say its population is partial instead of silently shrinking it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Row schema version stamped on every row.
RECORD_VERSION = 1

#: The record's directory name under ``<audit_data_root>/audit/``.
RECORD_DIRNAME = "table_payoff"

_ALIAS_DIRNAME = "alias"
_LOCK_NAME = ".lock"
_RUN_PREFIX = "run_"
_RUN_SUFFIX = ".jsonl"

#: Display names are operator-curated labels; cap what an audit row can carry.
_MAX_NAME = 120

#: An id that is already a safe filename component is used verbatim (execution ids
#: are ``exec_<hex>``); anything else -- a chat timestamp with ``:``, a hostile
#: ``../`` -- is hashed. The hashed form carries a ``.``, which the verbatim alphabet
#: cannot, so the two spaces never collide.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")

#: Parsed-record cache bound (the UI re-reads on every refresh tick).
_CACHE_MAX = 512

_RECORD_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[Tuple[int, int], "RunRecord"]] = {}


# -- paths ---------------------------------------------------------------------
def record_dir(data_dir=None) -> Path:
    """``<audit_data_root>/audit/table_payoff`` -- the SAME minted root the snapshot
    writer uses, so the shadow thread and the dashboard / CLI name one tree."""
    from systemu.runtime.execution_snapshot import audit_data_root
    return audit_data_root(data_dir) / "audit" / RECORD_DIRNAME


def _key(raw: Any) -> Optional[str]:
    if type(raw) is not str or not raw:
        return None
    if _SAFE_ID.match(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:40]
    return "x." + digest


def run_record_path(root_execution_id: str, *, data_dir=None) -> Path:
    """The run file for ``root_execution_id``. Always directly inside
    :func:`record_dir`, whatever the id contains."""
    k = _key(root_execution_id)
    if k is None:
        raise ValueError("a run record needs a non-empty root execution id")
    return record_dir(data_dir) / f"{_RUN_PREFIX}{k}{_RUN_SUFFIX}"


def _alias_path(kind: str, raw_id: Any, data_dir=None) -> Optional[Path]:
    k = _key(raw_id)
    if k is None:
        return None
    return record_dir(data_dir) / _ALIAS_DIRNAME / f"{kind}_{k}.txt"


# -- small tolerant readers over model-or-dict ---------------------------------
def _get(obj: Any, field: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def _list(obj: Any, field: str) -> list:
    v = _get(obj, field)
    return v if isinstance(v, list) else []


def _str_or_none(v: Any) -> Optional[str]:
    return v if type(v) is str and v else None


def _norm_oid(oid: Any) -> Any:
    """Objective ids are ints on the live path and strings after a JSON round-trip
    (``per_objective`` keys). Normalise so the dedupe key is the same either way."""
    if type(oid) is int:
        return oid
    if type(oid) is str and oid.isdigit():
        return int(oid)
    return oid if type(oid) is str and oid else None


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def table_entry_count(situation: Any) -> int:
    """How many table-backed entries the composed situation carries.

    Both shapes ``compose_table`` produces: an ANNOTATED live service / capability /
    root (``table_item_id`` set) and a ``declared_intents`` row (a dict that IS a
    table item -- compose_table is its sole producer). Zero means the binder had no
    table on hand, and nothing is recorded. Never raises."""
    try:
        n = 0
        for field in ("services", "capabilities", "roots"):
            for e in _list(situation, field):
                if _str_or_none(_get(e, "table_item_id")):
                    n += 1
        for e in _list(situation, "declared_intents"):
            if isinstance(e, dict) and _str_or_none(e.get("id")):
                n += 1
        return n
    except Exception:
        return 0


def _item_names(situation: Any, vault: Any) -> Dict[str, str]:
    names: Dict[str, str] = {}
    try:
        for e in _list(situation, "declared_intents"):
            if isinstance(e, dict):
                iid, nm = _str_or_none(e.get("id")), _str_or_none(e.get("name"))
                if iid and nm:
                    names[iid] = nm
    except Exception:
        pass
    if vault is not None:
        try:
            from systemu.runtime.table_store import load_items
            for it in load_items(vault):
                iid, nm = _str_or_none(_get(it, "id")), _str_or_none(_get(it, "name"))
                if iid and nm:
                    names.setdefault(iid, nm)
        except Exception:
            logger.debug("[TablePayoffRecord] table item names unavailable", exc_info=True)
    return {k: v[:_MAX_NAME] for k, v in names.items()}


def _supplied_key(row: Dict[str, Any]) -> tuple:
    return (_norm_oid(row.get("objective_id")), row.get("tool"), row.get("schema_path"),
            row.get("table_item_id"))


# -- the OS-level serialisation -------------------------------------------------
@contextmanager
def _locked(dirpath: Path):
    """Process lock + best-effort OS lock on the SIDECAR lock file."""
    from systemu.runtime.replay_metrics import _lock_whole_file, _unlock_whole_file
    with _RECORD_LOCK:
        fd = None
        try:
            fd = os.open(str(dirpath / _LOCK_NAME),
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        except OSError:
            fd = None                     # degrade to the in-process guarantee
        locked = _lock_whole_file(fd) if fd is not None else False
        try:
            yield
        finally:
            if fd is not None:
                if locked:
                    _unlock_whole_file(fd)
                os.close(fd)


def _append(path: Path, rows: List[Dict[str, Any]]) -> None:
    data = "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in rows).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.lseek(fd, 0, os.SEEK_END)      # explicit: O_APPEND is emulated on NT
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)


def _write_alias(path: Optional[Path], root: str) -> None:
    if path is None:
        return
    try:
        if path.is_file() and path.read_text(encoding="utf-8").strip() == root:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(root, encoding="utf-8")
    os.replace(tmp, path)


def _read_rows(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Rows + how many lines were unusable. Missing file -> ([], 0). OSError on an
    unreadable file PROPAGATES (callers decide whether that is 'nothing' or 'unknown')."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], 0
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            skipped += 1
            continue
        if type(obj) is not dict or type(obj.get("event")) is not str:
            skipped += 1
            continue
        rows.append(obj)
    return rows, skipped


# -- THE WRITER -------------------------------------------------------------------
def record_binder_pass(report: Any, situation: Any, *, root_execution_id: Any,
                       execution_id: Any = None, site: str = "",
                       tool: Any = None, scroll_id: Any = None, activity_id: Any = None,
                       chat_submission_id: Any = None, vault: Any = None,
                       data_dir=None) -> Dict[str, Any]:
    """Record one requirement-binder pass against the run's durable record.

    Call it right after the binder built ``report`` over ``situation``. Writes:

      * the run's ``consult`` row, once per root, if the table was on hand;
      * one ``supplied`` row per table-attributed, non-``missing`` requirement in
        ``report`` that this run has not already recorded (same objective, tool, schema
        path, item AND binding -- so a confirm later followed by a silent bind of the
        same leaf is two facts, and the DEC-25 tripwire can never be deduped away);
      * the lookup aliases for a non-root execution id and for a chat submission id.

    Returns ``{"consulted": bool, "written": int, "error": str|None}``. NEVER raises:
    this is observability and must never affect the run that called it. A failure is
    logged at WARNING (not swallowed silently) because a lost row shrinks the metric.
    No ``root_execution_id`` -> nothing is written (no file is ever named after None).
    """
    status: Dict[str, Any] = {"consulted": False, "written": 0, "error": None}
    root = _str_or_none(root_execution_id)
    if root is None:
        return status
    try:
        from systemu.runtime import table_payoff as _tp

        entries = table_entry_count(situation)
        candidates = [(oid, r) for oid, r in _tp.table_backed_keyed(report)
                      if _get(r, "state") != "missing"]
        if entries <= 0 and not candidates:
            return status
        status["consulted"] = True

        eid = _str_or_none(execution_id) or root
        chat = _str_or_none(chat_submission_id)
        base = {"v": RECORD_VERSION, "at": _now(), "root_execution_id": root,
                "execution_id": eid, "site": str(site or "")}
        path = run_record_path(root, data_dir=data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)

        with _locked(path.parent):
            existing, _skipped = _read_rows(path)
            new: List[Dict[str, Any]] = []
            if not any(r.get("event") == "consult" for r in existing):
                new.append({**base, "event": "consult", "table_entries": entries,
                            "scroll_id": _str_or_none(scroll_id),
                            "activity_id": _str_or_none(activity_id),
                            "chat_submission_id": chat})
            seen = {_supplied_key(r) + (r.get("bound_as"),)
                    for r in existing if r.get("event") == "supplied"}
            names: Optional[Dict[str, str]] = None
            for oid, req in candidates:
                row = {**base, "event": "supplied",
                       "objective_id": _norm_oid(oid),
                       "tool": _str_or_none(tool),
                       "schema_path": _str_or_none(_get(req, "schema_path")),
                       "kind": _str_or_none(_get(req, "kind")),
                       "table_item_id": _get(req, "table_item_id"),
                       "value_origin": _str_or_none(_get(req, "value_origin")),
                       "state": _str_or_none(_get(req, "state")),
                       "bound_as": _tp.bound_as(req)}
                k = _supplied_key(row) + (row["bound_as"],)
                if k in seen:
                    continue
                seen.add(k)
                if names is None:
                    names = _item_names(situation, vault)
                row["item_name"] = names.get(row["table_item_id"], "")
                new.append(row)
            if new:
                _append(path, new)
                status["written"] = len(new)
            if eid != root:
                _write_alias(_alias_path("exec", eid, data_dir), root)
            if chat:
                _write_alias(_alias_path("chat", chat, data_dir), root)
        return status
    except Exception as exc:
        logger.warning("[TablePayoffRecord] could not record binder pass for run %s: %s",
                       root, exc)
        status["error"] = type(exc).__name__
        return status


# -- READERS ----------------------------------------------------------------------
@dataclass(frozen=True)
class RunRecord:
    """The projection of one run file.

    ``supplied`` holds ONE entry per requirement (objective, tool, schema path, item);
    an entry is ``silent`` if ANY row for it said so -- a later confirm can never
    launder the DEC-25 alarm. ``rows_skipped`` counts unusable lines (never dropped
    silently)."""

    root_execution_id: str
    consulted: bool
    supplied: Tuple[Dict[str, Any], ...]
    executions: FrozenSet[str]
    rows_skipped: int = 0
    first_at: str = ""

    @property
    def silent(self) -> int:
        return sum(1 for s in self.supplied if s.get("bound_as") == "silent")

    @property
    def prefilled_confirm(self) -> int:
        return len(self.supplied) - self.silent

    @property
    def items(self) -> Tuple[Dict[str, str], ...]:
        """Distinct table items, first-seen order: ``({"id", "name"}, ...)``."""
        out: List[Dict[str, str]] = []
        seen = set()
        for s in self.supplied:
            tid = s.get("table_item_id")
            if tid in seen:
                continue
            seen.add(tid)
            out.append({"id": tid, "name": s.get("item_name") or tid})
        return tuple(out)


def _project(rows: List[Dict[str, Any]], skipped: int, fallback_root: str) -> RunRecord:
    root = None
    consulted = False
    executions = set()
    first_at = ""
    order: List[tuple] = []
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        ev = r.get("event")
        if root is None:
            root = _str_or_none(r.get("root_execution_id"))
        e = _str_or_none(r.get("execution_id"))
        if e:
            executions.add(e)
        if not first_at and type(r.get("at")) is str:
            first_at = r["at"]
        if ev == "consult":
            consulted = True
        elif ev == "supplied":
            if not _str_or_none(r.get("table_item_id")):
                skipped += 1
                continue
            consulted = True
            k = _supplied_key(r)
            cur = by_key.get(k)
            if cur is None:
                by_key[k] = dict(r)
                order.append(k)
            else:
                if r.get("bound_as") == "silent":
                    cur["bound_as"] = "silent"
                if not cur.get("item_name") and _str_or_none(r.get("item_name")):
                    cur["item_name"] = r["item_name"]
    supplied = []
    for k in order:
        s = by_key[k]
        if s.get("bound_as") != "silent":
            s["bound_as"] = "prefilled_confirm"
        supplied.append(s)
    return RunRecord(root_execution_id=root or fallback_root, consulted=consulted,
                     supplied=tuple(supplied), executions=frozenset(executions),
                     rows_skipped=skipped, first_at=first_at)


def _load(path: Path, fallback_root: str) -> Optional[RunRecord]:
    """Parse one run file through the mtime/size cache. None if absent; OSError on an
    unreadable file propagates to the caller."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    cache_key = str(path)
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    rows, skipped = _read_rows(path)
    rec = _project(rows, skipped, fallback_root)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[cache_key] = (stamp, rec)
    return rec


def read_run(root_execution_id: Any, *, data_dir=None) -> Optional[RunRecord]:
    """The run's record, or None when nothing is recorded (or it cannot be read --
    a render must never raise). Cheap: one stat, and a parse only when the file moved."""
    try:
        if _key(root_execution_id) is None:
            return None
        return _load(run_record_path(root_execution_id, data_dir=data_dir),
                     str(root_execution_id))
    except Exception:
        logger.debug("[TablePayoffRecord] read_run failed", exc_info=True)
        return None


def _resolve_alias(kind: str, raw_id: Any, data_dir) -> Optional[str]:
    p = _alias_path(kind, raw_id, data_dir)
    if p is None:
        return None
    try:
        root = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return root or None


def find_run(*, execution_id: Any = None, chat_submission_id: Any = None,
             data_dir=None) -> Optional[RunRecord]:
    """The record of the run a surface is showing, by whatever id that surface holds.

    ``execution_id`` may be the root (a fresh run) or a resumed execution (looked up
    through its alias); ``chat_submission_id`` is what a chat card holds while its run
    is in flight. None when nothing is recorded. Never raises."""
    try:
        if _str_or_none(execution_id):
            rec = read_run(execution_id, data_dir=data_dir)
            if rec is not None:
                return rec
            root = _resolve_alias("exec", execution_id, data_dir)
            if root:
                rec = read_run(root, data_dir=data_dir)
                if rec is not None:
                    return rec
        if _str_or_none(chat_submission_id):
            root = _resolve_alias("chat", chat_submission_id, data_dir)
            if root:
                return read_run(root, data_dir=data_dir)
        return None
    except Exception:
        logger.debug("[TablePayoffRecord] find_run failed", exc_info=True)
        return None


@dataclass(frozen=True)
class RecordScan:
    """Every run record on disk, for the metric.

    ``unreadable`` counts run files that exist but could not be read; ``error`` is set
    when the directory itself could not be listed. Both are WITNESSED gaps -- the
    metric reports them rather than measuring over a silently smaller population."""

    searched: str
    records: Tuple[RunRecord, ...] = ()
    unreadable: int = 0
    error: Optional[str] = None

    @property
    def rows_skipped(self) -> int:
        return sum(r.rows_skipped for r in self.records)


def scan_records(*, data_dir=None) -> RecordScan:
    """Read every ``run_*.jsonl`` under :func:`record_dir`. Never raises."""
    try:
        d = record_dir(data_dir)
    except Exception as exc:
        return RecordScan(searched="(audit data root unresolved)",
                          error=type(exc).__name__)
    searched = str(d / f"{_RUN_PREFIX}*{_RUN_SUFFIX}")
    if not d.is_dir():
        return RecordScan(searched=searched)
    try:
        paths = sorted(d.glob(f"{_RUN_PREFIX}*{_RUN_SUFFIX}"))
    except OSError as exc:
        return RecordScan(searched=searched, error=type(exc).__name__)
    records: List[RunRecord] = []
    unreadable = 0
    for p in paths:
        try:
            rec = _load(p, p.name[len(_RUN_PREFIX):-len(_RUN_SUFFIX)])
        except Exception:
            unreadable += 1
            continue
        if rec is not None:
            records.append(rec)
    return RecordScan(searched=searched, records=tuple(records), unreadable=unreadable)
