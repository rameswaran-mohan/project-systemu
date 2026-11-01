"""AST lint: a durable writer that handles tool-call parameters or prompts must
mask them before they reach disk (DEC-31 at the writers).

Usage:
    python -m tools.lint_durable_writers     # exit 1 on drift from the baseline

**What it flags.** A function under ``systemu/`` that

  1. performs a DURABLE WRITE -- ``open``/``Path.open``/``os.fdopen`` in a
     write/append/create mode, ``write_text``, ``write_bytes``, ``json.dump``,
     ``pickle.dump``, ``yaml.dump``/``safe_dump`` to a stream, a SQL
     ``INSERT``/``UPDATE``/``REPLACE`` through ``execute``/``executemany``, or a
     call to one of the persistence entry points in :data:`API_SINKS` (the
     vault methods that write a param/prompt-carrying record as given);
  2. references a PARAMETER/PROMPT name (:data:`VOCAB`) -- as a variable, an
     attribute, a function argument, a keyword, or a string key; and
  3. calls NO masker (:data:`MASKERS`: every redactor ``lint_redaction_order``
     knows, plus the durable writers' own wrappers).

Each nested function is its own scope (``outer.inner``), because a closure that
writes is a writer of its own.

**Baseline, enforced both ways (DEC-32c).** The violations that exist today are
committed in ``tools/durable_writer_allowlist.json`` with a reason each, keyed
by ``(path, qualname)`` and carrying the number of write sites. The test fails
on a NEW violation (an unlisted writer, or a listed one with more write sites
than recorded) and on a STALE entry (a listed writer that no longer violates --
fix it, then delete the line). So the list can only shrink.

**Fenced writers are pinned separately.** A writer that masks through a helper
whose name carries no VOCAB token (``log_event`` masks ``entry``) is invisible
to rule 2, so the writers this slice fenced are listed in :data:`FENCED` with
the masker each must call; removing the call fails the lint.

**What it cannot see** (:data:`SCOPE_NOTE`): data flow. A writer that persists
a parameter under a name outside VOCAB (``row = {"x": p}``), a value that
reaches the write through another module, or a mask call that is present but
not on the path to the write. A clean run means "no writer this matcher can
see handles a parameter/prompt name without calling a masker", not "nothing
raw reaches disk". FAIL LOUD: an unparseable file or an empty scan raises.
"""
from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

#: Names that carry a tool call's raw parameters or a prompt.
VOCAB = frozenset({
    "params", "parameters", "tool_params", "last_successful_params",
    "prompt", "prompts", "system_prompt", "user_prompt", "llm_prompt",
})

_LOCAL_MASKERS = frozenset({
    "_truncate_audit_params",      # the per-call audit row's masker
    "_mask_history_slice", "_persisted_dict",  # execution_snapshot
    "_masked",                     # llm_transcript
})


def _maskers() -> frozenset:
    from tools.lint_redaction_order import _REDACTORS
    return frozenset(_REDACTORS) | _LOCAL_MASKERS


MASKERS = _maskers()

#: (path, qualname) -> the masker that writer must call. Pinned because these
#: mask through names rule 2 cannot see, or because they are the fences this
#: lint exists to keep.
FENCED: Dict[Tuple[str, str], str] = {
    ("systemu/interface/notifications.py", "log_event"): "mask_event",
    ("systemu/interface/event_bridge_writer.py",
     "install_bridge_writer._on_event"): "mask_event",
    ("systemu/runtime/llm_transcript.py", "append_call"): "_masked",
    ("systemu/runtime/llm_transcript.py", "_masked"): "mask_text",
    ("systemu/runtime/execution_snapshot.py", "write_snapshot"): "_persisted_dict",
    ("systemu/runtime/execution_snapshot.py", "_persisted_dict"): "_mask_history_slice",
    ("systemu/runtime/execution_snapshot.py", "_mask_history_slice"): "mask_params",
    ("systemu/pipelines/tool_dry_run.py", "record_successful_params"): "_redact_secrets",
    ("systemu/pipelines/tool_dry_run.py", "_redact_secrets"): "mask_values",
    ("systemu/runtime/shadow_runtime.py", "_build_history_slice"): "_mask_display",
    ("systemu/runtime/shadow_runtime.py", "ShadowRuntime._iteration_event"): "_mask_display",
    ("systemu/pipelines/quick_task.py", "run_quick_task"): "_mask_text",
    ("systemu/pipelines/quick_task.py", "run_quick_task._finish"): "_mask_text",
    ("systemu/runtime/shadow_runtime.py", "_truncate_audit_params"): "_mask_evidence",
    ("systemu/runtime/decision_audit.py", "append_iteration_decision"): "mask_text",
}

ALLOWLIST_PATH = Path(__file__).with_name("durable_writer_allowlist.json")

SCOPE_NOTE = (
    "durable-writer lint scope: a function under systemu/ that performs a durable "
    "write AND references a parameter/prompt name (VOCAB) AND calls no masker.\n"
    "  It CANNOT see data flow: a parameter persisted under a name outside VOCAB, "
    "a value that reaches the write through another module, or a mask call that "
    "is present but not on the path to the write. A clean run means 'no writer "
    "this matcher can see handles a parameter/prompt name without calling a "
    "masker' -- NOT 'nothing raw reaches disk'."
)

#: The vault/runtime persistence entry points that take a param- or
#: prompt-carrying record and write it as given. A call to one counts as a
#: durable write in the CALLER's scope, because the caller is where the record
#: is built. The FENCED writers (``log_event``, ``append_call``,
#: ``write_snapshot``, ``append_iteration_decision``) are deliberately absent:
#: they mask themselves, so their callers need not.
API_SINKS = frozenset({
    "append_action_audit", "append_action", "save_tool", "save_decision",
    "append_chat_history", "update_chat_history_entry",
    "append_session_summary", "append_user_fact",
    "append_memory_buffer", "append_elder_memory_buffer", "_ledger_append",
})

_WRITE_MODE_CHARS = set("wax+")
_SQL_WRITE_PREFIXES = ("INSERT", "UPDATE", "REPLACE")


class DurableWriterLintError(RuntimeError):
    """The lint could not run. Never confuse this with a clean result."""


@dataclass(frozen=True)
class Finding:
    path: str
    qualname: str
    sinks: int

    @property
    def key(self) -> Tuple[str, str]:
        return (self.path, self.qualname)


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #

def _callee(node: ast.Call) -> Optional[str]:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _receiver(node: ast.Call) -> Optional[str]:
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f.value.id
    return None


def _const_str(node: Optional[ast.AST]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def _mode_arg(node: ast.Call, positional_index: int) -> Optional[str]:
    for kw in node.keywords:
        if kw.arg == "mode":
            return _const_str(kw.value)
    if len(node.args) > positional_index:
        return _const_str(node.args[positional_index])
    return None


def _is_sink(node: ast.Call) -> bool:
    name = _callee(node)
    if name is None:
        return False
    if name in API_SINKS:
        return True
    if name in ("write_text", "write_bytes"):
        return True
    if name == "dump" and _receiver(node) in ("json", "pickle", "yaml", "_json"):
        return True
    if name == "safe_dump" and len(node.args) >= 2:
        return True
    if name == "open":
        # builtin/io open(path, mode) vs Path.open(mode)
        idx = 1 if isinstance(node.func, ast.Name) or _receiver(node) in ("io", "codecs") else 0
        mode = _mode_arg(node, idx)
        return bool(mode) and bool(set(mode) & _WRITE_MODE_CHARS)
    if name == "fdopen":
        mode = _mode_arg(node, 1)
        return bool(mode) and bool(set(mode) & _WRITE_MODE_CHARS)
    if name in ("execute", "executemany") and node.args:
        sql = _const_str(node.args[0])
        return bool(sql) and sql.lstrip().upper().startswith(_SQL_WRITE_PREFIXES)
    return False


def _mentions_vocab(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in VOCAB
    if isinstance(node, ast.Attribute):
        return node.attr in VOCAB
    if isinstance(node, ast.arg):
        return node.arg in VOCAB
    if isinstance(node, ast.keyword):
        return node.arg in VOCAB
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in VOCAB
    return False


class _Scope:
    __slots__ = ("qualname", "sinks", "vocab", "calls")

    def __init__(self, qualname: str) -> None:
        self.qualname = qualname
        self.sinks = 0
        self.vocab = False
        self.calls: Set[str] = set()


def _scopes(tree: ast.AST) -> List[_Scope]:
    """Every function scope, nested ones separate, with its sinks/vocab/calls."""
    out: List[_Scope] = []

    def visit_body(nodes, prefix: str, scope: Optional[_Scope]) -> None:
        for n in nodes:
            visit(n, prefix, scope)

    def visit(node: ast.AST, prefix: str, scope: Optional[_Scope]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            q = f"{prefix}{node.name}"
            s = _Scope(q)
            out.append(s)
            for a in ast.walk(node.args):
                if _mentions_vocab(a):
                    s.vocab = True
            for d in node.decorator_list:
                visit(d, prefix, scope)
            visit_body(node.body, q + ".", s)
            return
        if isinstance(node, ast.ClassDef):
            visit_body(node.body, f"{prefix}{node.name}.", scope)
            return
        # a lambda (no new scope here) belongs to the enclosing function
        if scope is not None:
            if isinstance(node, ast.Call):
                if _is_sink(node):
                    scope.sinks += 1
                name = _callee(node)
                if name:
                    scope.calls.add(name)
            if _mentions_vocab(node):
                scope.vocab = True
        for child in ast.iter_child_nodes(node):
            visit(child, prefix, scope)

    visit(tree, "", None)
    return out


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #

def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def iter_scanned_files(root: Optional[Path] = None) -> Iterator[Path]:
    root = root or repo_root()
    pkg = root / "systemu"
    if not pkg.is_dir():
        raise DurableWriterLintError(f"no systemu/ package under {root}")
    files = sorted(pkg.rglob("*.py"))
    if not files:
        raise DurableWriterLintError(f"no .py files under {pkg}")
    yield from files


def _parse(path: Path) -> ast.AST:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        raise DurableWriterLintError(f"cannot parse {path}: {exc}") from exc


def scan_source(source: str, rel: str) -> List[Finding]:
    """Violations in one module's source (for tests)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise DurableWriterLintError(f"cannot parse {rel}: {exc}") from exc
    return _findings(tree, rel)


def _findings(tree: ast.AST, rel: str) -> List[Finding]:
    out = []
    for s in _scopes(tree):
        if s.sinks and s.vocab and not (s.calls & MASKERS):
            out.append(Finding(rel, s.qualname, s.sinks))
    return out


def scan_repo(root: Optional[Path] = None) -> List[Finding]:
    root = root or repo_root()
    out: List[Finding] = []
    for p in iter_scanned_files(root):
        rel = p.relative_to(root).as_posix()
        out.extend(_findings(_parse(p), rel))
    return out


def fence_breaks(root: Optional[Path] = None) -> List[str]:
    """Every :data:`FENCED` writer that no longer exists or no longer calls its
    masker."""
    root = root or repo_root()
    problems = []
    by_file: Dict[str, Dict[str, _Scope]] = {}
    for (rel, qual), masker in sorted(FENCED.items()):
        if rel not in by_file:
            p = root / rel
            if not p.is_file():
                problems.append(f"{rel}: file missing (fenced writer {qual})")
                by_file[rel] = {}
                continue
            by_file[rel] = {s.qualname: s for s in _scopes(_parse(p))}
        scope = by_file[rel].get(qual)
        if scope is None:
            problems.append(f"{rel}::{qual}: fenced writer not found")
        elif masker not in scope.calls:
            problems.append(f"{rel}::{qual}: no longer calls {masker}()")
    return problems


def load_allowlist(path: Path = ALLOWLIST_PATH) -> Dict[Tuple[str, str], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DurableWriterLintError(f"cannot read allowlist {path}: {exc}") from exc
    entries = data.get("writers") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise DurableWriterLintError(f"allowlist {path} has no 'writers' list")
    out: Dict[Tuple[str, str], dict] = {}
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)
                and isinstance(e.get("qualname"), str)
                and type(e.get("sinks")) is int
                and isinstance(e.get("reason"), str) and e["reason"].strip()):
            raise DurableWriterLintError(f"malformed allowlist entry: {e!r}")
        key = (e["path"], e["qualname"])
        if key in out:
            raise DurableWriterLintError(f"duplicate allowlist entry: {key}")
        out[key] = e
    return out


def drift(findings: List[Finding], allow: Dict[Tuple[str, str], dict]) -> Tuple[List[str], List[str]]:
    """(new violations, stale allowlist entries)."""
    found = {f.key: f for f in findings}
    new = []
    for key, f in sorted(found.items()):
        entry = allow.get(key)
        if entry is None:
            new.append(f"{f.path}::{f.qualname} ({f.sinks} write site(s)) "
                       f"handles a parameter/prompt name and calls no masker")
        elif f.sinks > entry["sinks"]:
            new.append(f"{f.path}::{f.qualname}: {f.sinks} write sites, "
                       f"allowlisted with {entry['sinks']}")
    stale = []
    for key, entry in sorted(allow.items()):
        f = found.get(key)
        if f is None:
            stale.append(f"{key[0]}::{key[1]} no longer violates -- delete its "
                         f"allowlist entry (the list only shrinks)")
        elif f.sinks < entry["sinks"]:
            stale.append(f"{key[0]}::{key[1]}: {f.sinks} write sites now, "
                         f"allowlisted with {entry['sinks']} -- lower it")
    return new, stale


def main() -> int:
    try:
        findings = scan_repo()
        allow = load_allowlist()
        breaks = fence_breaks()
    except DurableWriterLintError as exc:
        print(f"durable-writer lint: CANNOT RUN -- {exc}", file=sys.stderr)
        return 2
    new, stale = drift(findings, allow)
    for line in breaks + new + stale:
        print(line)
    if breaks or new or stale:
        print(f"\n{len(breaks)} broken fence(s), {len(new)} new violation(s), "
              f"{len(stale)} stale allowlist entr(y/ies)")
        print(SCOPE_NOTE)
        return 1
    print(f"durable-writer lint: clean -- {len(findings)} allowlisted writer(s), "
          f"{len(FENCED)} fence pin(s)")
    print(SCOPE_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
