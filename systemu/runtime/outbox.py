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
_REDACTED = "[redacted - looked like a credential]"

_OK_STATUSES = frozenset({"success"})
_WARN_STATUSES = frozenset({"partial", "spend_cap_reached", "waiting_on_tools",
                            "pending_decision", "cancelled"})


# --------------------------------------------------------------------------- #
# path safety
# --------------------------------------------------------------------------- #

def safe_component(text: Any, *, fallback: str = "task") -> str:
    """Sanitize ``text`` into ONE filesystem path component.

    ASCII allowlist (``A-Za-z0-9_.-``) + explicit ``..`` neutralization + a
    Windows reserved-device-name guard + a length cap. Follows the shape of the
    shipped ``receipts_store._safe_eid`` and adds the two things it lacks: the
    length bound and the reserved-name guard.

    Never returns ``""``, ``"."``, ``".."``, or a name a path join could escape.
    """
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

def redact(text: Any) -> str:
    """Make ``text`` safe(r) to render into a file that leaves the process.

    Two shipped fences, in order:
      1. ``ask_promotion._value_is_secret`` — if the WHOLE value reads as a
         credential, drop it rather than emit a partly-masked husk.
      2. ``messaging.gateway.mask_outbound`` — span-level redaction of embedded
         secrets inside otherwise-ordinary prose.

    Fail-closed: if either import fails, the value is dropped.
    """
    s = "" if text is None else str(text)
    if not s:
        return ""
    try:
        from systemu.runtime.ask_promotion import _value_is_secret
        if _value_is_secret(s):
            return _REDACTED
    except Exception:
        logger.debug("[Outbox] secret check unavailable - dropping value", exc_info=True)
        return _REDACTED
    try:
        from systemu.messaging.gateway import mask_outbound
        return mask_outbound(s)
    except Exception:
        logger.debug("[Outbox] mask unavailable - dropping value", exc_info=True)
        return _REDACTED


def _esc(text: Any) -> str:
    """Redact THEN HTML-escape. Order matters: escaping first would break the
    redactor's patterns (``sk-...`` survives escaping, ``&quot;`` noise does
    not, and a masked span must not be re-interpretable as markup)."""
    return html.escape(redact(text), quote=True)


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

    Every interpolated value is redacted then HTML-escaped by :func:`_esc`.
    """
    rows: List[str] = []
    for name, original in artifacts or ():
        rows.append(
            f"<li><code>{_esc(name)}</code>"
            f"<br><span class='sub' style='margin:0'>copied from {_esc(original)}</span></li>"
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


def _unique_name(taken: set, name: str) -> str:
    """De-collide a basename WITHIN one folder. ``files_produced`` is a flat list
    of absolute paths from anywhere on disk, so two directories' ``report.md``
    would otherwise silently overwrite each other."""
    if name not in taken:
        taken.add(name)
        return name
    stem, dot, ext = name.partition(".")
    for n in range(2, 1000):
        cand = f"{stem}-{n}{dot}{ext}"
        if cand not in taken:
            taken.add(cand)
            return cand
    cand = f"{stem}-{os.getpid()}{dot}{ext}"
    taken.add(cand)
    return cand


def _copy_artifacts(sources: Iterable[Any], dest_dir: Path,
                    root: Path) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Copy each artifact into ``dest_dir``. Returns (copied, skipped_reasons).

    Every destination is re-checked against ``root`` immediately before the
    write, so a crafted basename can never traverse out of the Outbox.
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
            name = _unique_name(taken, safe_component(src.name, fallback="artifact"))
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
    """
    root = outbox_root(vault)
    stamp = now or datetime.now()
    slug = safe_component(prompt or task_id, fallback="task")
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
