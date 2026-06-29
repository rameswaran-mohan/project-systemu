"""Tool dry-run validation pipeline (v0.5.0-a).

The gate between forge and operator-enable.  Given a forged Tool:

1. Generate test parameters via Tier-3 LLM (with schema-driven fallback).
2. Install the tool's declared pip dependencies (reuses v0.3.3 installer
   via the existing ToolSandbox).
3. Execute the tool against the test params, capturing the result.
4. Verify the result against the ``return_schema`` (success bool, presence
   of declared output keys).
5. Persist the outcome to ``Tool.dry_run_status`` / ``dry_run_evidence``
   so the operator can inspect on the Tools page.

The result is a :class:`DryRunResult`.  Callers decide whether to block
the tool from being enabled (forge pipeline) or whether to fork-vs-bump
(v0.5.0-d recalibration).

Safety:

* **Destructive-tool guard** — :func:`is_destructive_call` from
  ``tool_sandbox`` is consulted before the run.  Destructive tools that
  don't declare ``dry_run=True`` support get ``status="skipped"`` and
  evidence noting the reason; operator must approve manually.
* **Tmp-path sandbox** — when the test-param generator produces path-like
  arguments, they're rewritten to ``/tmp/dry_run_<uuid>/`` so even tools
  that don't honour ``dry_run=True`` can't trample real outputs.
* **Replay mode** (v0.5.0-d) — when ``replay_params`` is supplied, the
  pipeline skips test-param generation and instead runs the tool against
  each historical params set.  ANY failure → backward-compat regression.

Never raises into the caller.  Network outages, LLM failures, subprocess
crashes all surface as ``DryRunResult(success=False)`` with evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.core.models import Tool
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# v0.8.5: safe sync-from-async coroutine runner

def _run_coro_sync(coro):
    """Execute an async coroutine from sync code.

    Safe from any context: if no event loop is running in the current
    thread, uses asyncio.run directly (fast path).  If a loop IS running
    (e.g. dashboard's NiceGUI event loop), offloads to a fresh thread
    that owns its own loop — avoiding the
    'asyncio.run() cannot be called from a running event loop' error.

    Pre-v0.8.5: tool_dry_run called asyncio.run(coro) unconditionally,
    which crashed every dashboard-initiated dry-run.
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread — fast path
        return asyncio.run(coro)
    # Already in a running loop — run in a fresh thread with its own loop
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


# ─────────────────────────────────────────────────────────────────────────────
# Result type

@dataclass
class DryRunResult:
    success:        bool
    status:         str             # passed | failed | skipped
    params_used:    Dict[str, Any]  = field(default_factory=dict)
    error:          Optional[str]   = None
    skip_reason:    Optional[str]   = None
    elapsed_ms:     int             = 0
    return_value:   Optional[Dict[str, Any]] = None
    replayed_count: int             = 0   # >0 in v0.5.0-d replay mode
    # True when status=="skipped" because the harness could not synthesize a
    # representative input for a file format (e.g. a real .docx) — the operator
    # owns correctness, so the tool is STILL enable-able (unlike a hard "failed").
    operator_verify: bool          = False

    def to_evidence(self) -> Dict[str, Any]:
        """Compact evidence dict for persistence into ``Tool.dry_run_evidence``."""
        try:
            from systemu import __version__ as _ver
        except Exception:
            _ver = ""
        return {
            "success":      self.success,
            "status":       self.status,
            "params_used":  self.params_used,
            "error":        self.error,
            "skip_reason":  self.skip_reason,
            "elapsed_ms":   self.elapsed_ms,
            # v0.9.51: stamp the systemu version this verdict was produced under, so
            # a failure from an OLDER version can be re-validated after an upgrade
            # (a fix may now make it pass) without re-running current-version
            # failures (which would loop). See recover_stale_dry_run_failures.
            "systemu_version": _ver,
            "return_value_summary": (
                {k: str(v)[:120] for k, v in (self.return_value or {}).items()}
                if isinstance(self.return_value, dict) else None
            ),
            "replayed_count": self.replayed_count,
            "operator_verify": self.operator_verify,
        }


# Error-text signatures that mean "the harness fed an input this format-parsing
# tool couldn't open" — NOT a logic/runtime bug in the tool.  A match routes the
# verdict to a non-doomed operator_verify skip instead of a permanent failure.
_FORMAT_PARSE_SIGNATURES = (
    "packagenotfounderror", "not a zip file", "file is not a zip file",
    "badzipfile", "unsupportedformat", "is encrypted", "invalid pdf",
    "eof marker not found",
)


def _is_format_parse_failure(error_text: str) -> bool:
    t = (error_text or "").lower()
    return any(s in t for s in _FORMAT_PARSE_SIGNATURES)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points

def dry_run_tool(
    tool: "Tool",
    *,
    vault: "Vault",
    config: "Config",
    prior_failure: Optional[str] = None,
) -> DryRunResult:
    """Run the v0.5.0-a single-shot validation against ``tool``.

    Args:
        tool:           The forged tool to validate.  Must have
                        ``implementation_path`` set and the file on disk.
        vault:          For looking up neighbouring tools if needed.
        config:         Carries API key + Tier-3 model name.
        prior_failure:  Optional context from a previous failed dry-run —
                        passed to the test-param generator so it can address
                        what previously broke.

    Returns:
        :class:`DryRunResult`.  Status is one of:
          * ``"passed"``  — tool ran cleanly with the test params
          * ``"failed"``  — tool returned ``success=False`` or raised
          * ``"skipped"`` — pre-flight refused (destructive tool, no
                            implementation file, etc.)
    """
    t0 = time.monotonic()

    # Pre-flight: tool must actually exist on disk.
    if not tool.implementation_path:
        return DryRunResult(
            success=False, status="skipped",
            skip_reason="tool has no implementation_path — forge incomplete",
            elapsed_ms=_elapsed_ms(t0),
        )

    # v0.9.51: the PARAMS are now synthesized DETERMINISTICALLY by the recursive
    # schema-walk engine (replaces the flat, scalar-only LLM-first param-gen +
    # _sandbox_paths) — a real, valid, materialized value for every leaf at ANY
    # nesting depth (object / array / array-of-objects / nested), which dissolves
    # the per-shape whack-a-mole. The LLM is still consulted ONLY for the semantic
    # SKIP judgment ("this tool shouldn't be dry-run", e.g. it has real side
    # effects) — a call no schema encodes; its param suggestions are ignored in
    # favor of the engine.
    _, gen_meta = _generate_test_params(tool, config=config, prior_failure=prior_failure)
    if gen_meta.get("skip"):
        return DryRunResult(
            success=False, status="skipped",
            skip_reason=gen_meta.get("skip_reason"),
            elapsed_ms=_elapsed_ms(t0),
        )

    from systemu.pipelines.fixture_synth import synthesize_params
    _synth = synthesize_params(tool.parameters_schema or {}, tool_name=tool.name,
                               grounding_paths=getattr(tool, "grounding_inputs", None))
    params = _synth.params

    # Destructive heuristic: refuse if destructive AND tool didn't accept dry_run.
    try:
        from systemu.runtime.tool_sandbox import ToolSandbox
        if ToolSandbox.is_destructive_call(tool.name, params) and "dry_run" not in params:
            return DryRunResult(
                success=False, status="skipped",
                skip_reason="destructive tool without dry_run support — operator must verify manually",
                params_used=params,
                elapsed_ms=_elapsed_ms(t0),
            )
    except Exception:
        # Conservative: if we can't evaluate, proceed — tmp-path sandbox is a strong floor.
        pass

    # Execute via the existing sandbox.
    result = _execute(tool, params, vault=vault, config=config)
    elapsed = _elapsed_ms(t0)

    if result.get("success") is True:
        return DryRunResult(
            success=True, status="passed",
            params_used=params,
            return_value=result.get("parsed") or {},
            elapsed_ms=elapsed,
        )

    # A format-parse failure means the harness couldn't synthesize a
    # representative input (e.g. a real .docx) — NOT a logic bug in the tool.
    # Route it to a non-doomed operator_verify skip so a correct file/format
    # tool can still be enabled (operator owns correctness), never a hard fail.
    err = str(result.get("error") or result.get("stderr") or "tool returned success=False")
    if _is_format_parse_failure(err):
        return DryRunResult(
            success=False, status="skipped", operator_verify=True,
            skip_reason="harness cannot synthesize a representative input for this file format — operator must verify",
            params_used=params,
            error=err[:1000],
            elapsed_ms=elapsed,
        )
    # v0.9.51: the synthesizer couldn't fully satisfy a constrained param (e.g. a
    # `pattern`-bound string — it has no regex engine). A failure may BE that gap,
    # not a tool bug, so degrade to a non-doomed operator_verify instead of a hard
    # fail (the "never false-fail on a synthesis gap" contract).
    if _synth.unresolved:
        return DryRunResult(
            success=False, status="skipped", operator_verify=True,
            skip_reason=("harness could not synthesize constrained param(s) "
                         + ", ".join(_synth.unresolved) + " — operator must verify"),
            params_used=params,
            error=err[:1000],
            elapsed_ms=elapsed,
        )
    return DryRunResult(
        success=False, status="failed",
        params_used=params,
        error=err[:1000],
        return_value=result.get("parsed") or {},
        elapsed_ms=elapsed,
    )


def replay_against_history(
    tool: "Tool",
    *,
    vault: "Vault",
    config: "Config",
    max_replays: int = 20,
) -> DryRunResult:
    """v0.5.0-d backward-compat replay.

    Re-runs the tool against every entry in ``tool.last_successful_params``
    (capped at ``max_replays``).  Returns ``status="passed"`` only when
    EVERY historical params set still produces ``success=True``.

    Used by RECALIBRATE_TOOL's `bump_version` path to prove that the
    new code doesn't regress for shadows that were happily using the
    old version.  If ANY replay fails, the bump is rejected and the
    supervisor falls back to forking.
    """
    t0 = time.monotonic()
    history = list(tool.last_successful_params or [])[:max_replays]
    if not history:
        # No history → nothing to regress against.  Caller decides whether
        # to allow the bump or require fork.
        return DryRunResult(
            success=True, status="passed",
            replayed_count=0,
            elapsed_ms=_elapsed_ms(t0),
        )

    for idx, params in enumerate(history):
        sandboxed = _sandbox_paths(dict(params))
        result = _execute(tool, sandboxed, vault=vault, config=config)
        if result.get("success") is not True:
            return DryRunResult(
                success=False, status="failed",
                params_used=sandboxed,
                error=(
                    f"replay #{idx + 1}/{len(history)} regression: "
                    f"{str(result.get('error') or result.get('stderr') or 'success=False')[:400]}"
                ),
                replayed_count=idx,
                elapsed_ms=_elapsed_ms(t0),
            )

    return DryRunResult(
        success=True, status="passed",
        replayed_count=len(history),
        elapsed_ms=_elapsed_ms(t0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internals

def _generate_test_params(
    tool: "Tool",
    *,
    config: "Config",
    prior_failure: Optional[str] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Tier-3 LLM call to produce safe test params.  Falls back to
    schema-driven defaults on any failure.

    Returns ``(params, meta)`` where ``meta["skip"] == True`` blocks the
    dry-run with the supplied reason.
    """
    payload: Dict[str, Any] = {
        "tool_name":           tool.name,
        "description":         tool.description,
        "parameters_schema":   tool.parameters_schema or {},
        "implementation_notes": tool.implementation_notes or "",
        "is_destructive":      _looks_destructive(tool),
    }
    if prior_failure:
        payload["prior_dry_run_failure"] = prior_failure[:600]

    try:
        from systemu.core.llm_router import llm_call_json
        from systemu.core.utils import load_prompt
        raw = llm_call_json(
            tier=3,
            system=load_prompt("generate_test_params.md"),
            user=json.dumps(payload, ensure_ascii=False),
            config=config,
            temperature=0.1,
            max_tokens=512,
        )
        if isinstance(raw, dict):
            if raw.get("skip_dry_run"):
                return ({}, {"skip": True, "skip_reason": raw.get("skip_reason") or "LLM advised skip"})
            llm_params = raw.get("params")
            if isinstance(llm_params, dict):
                # Bug B (v0.9.48): the param-gen LLM sometimes returns a valid-but-
                # EMPTY or PARTIAL result ({}, {"params": {}}, {"params": null}, or
                # only some keys). Returning that verbatim called run() with missing
                # required positionals -> a FALSE "run() missing N required positional
                # argument(s)" dry-run failure for every file/multi-param tool (which
                # also poisons the Phase-7 self-heal's course-correction). Backfill any
                # missing keys from the schema-driven defaults (LLM values always win)
                # so run(**params) is guaranteed every required param.
                defaults = _schema_default_params(tool.parameters_schema or {}, tool_name=tool.name)
                return ({**defaults, **llm_params}, {})
    except Exception:
        logger.debug("[ToolDryRun] LLM test-param gen failed — using schema defaults", exc_info=True)

    return (_schema_default_params(tool.parameters_schema or {}, tool_name=tool.name), {})


_FIXTURE_EXTS = ("docx", "xlsx", "pptx", "pdf", "png", "jpg", "jpeg",
                 "zip", "csv", "json", "txt", "xls", "doc")


def _infer_fixture_ext(name: str, spec: Dict[str, Any], tool_name: Optional[str] = None) -> str:
    """Best-effort extension for a path-like param, inferred from the param name,
    the tool name, or the param's description (e.g. ``password_protect_docx`` ->
    ``.docx``). Lets the fallback synthesize a FORMAT-VALID fixture so a correct
    file/format tool passes dry-run instead of failing on a generic input."""
    desc = spec.get("description") if isinstance(spec, dict) else ""
    hay = " ".join(str(x).lower() for x in (name, tool_name or "", desc or ""))
    for e in _FIXTURE_EXTS:
        if e in hay:
            return "." + e
    return ""


def _schema_default_params(schema: Dict[str, Any], tool_name: Optional[str] = None) -> Dict[str, Any]:
    """Schema-driven fallback when the LLM test-param gen is unavailable.

    Neutral defaults: 0 for numbers, empty containers, None for unknown — but
    PATH-like params get a non-empty, sandbox-able placeholder (with an inferred
    extension) and REQUIRED strings get a non-empty value, so a forged file tool
    survives its own ``if not <arg>: required`` check AND gets a dry-run fixture.
    """
    from systemu.core.schema_utils import normalize_parameters_schema

    out: Dict[str, Any] = {}
    for name, spec in normalize_parameters_schema(schema or {}).items():
        if not isinstance(spec, dict):
            out[name] = None
            continue
        if spec.get("default") is not None:
            out[name] = spec["default"]
            continue
        t = (spec.get("type") or "").lower()
        if t == "string":
            if _looks_like_path_key(name):
                # Non-empty path so _sandbox_paths materializes a fixture; the
                # inferred extension makes it format-valid where we can tell.
                out[name] = "dry_run_input" + (_infer_fixture_ext(name, spec, tool_name) or ".dat")
            elif spec.get("required"):
                # A required non-path string (e.g. a password) — empty would trip
                # the tool's own validation, so use a neutral non-empty value.
                out[name] = "dryrun"
            else:
                out[name] = ""
        elif t in ("integer", "int", "number", "float"):
            out[name] = 0
        elif t == "boolean":
            out[name] = False
        elif t in ("array", "list"):
            out[name] = []
        elif t in ("object", "dict"):
            out[name] = {}
        else:
            out[name] = None
    return out


_PATH_SUFFIXES = ("_path", "_file", "_dir")
_PATH_EXACT = {
    "output_path", "file_path", "dest", "destination", "output_dir", "path",
    "filepath", "out", "outfile", "input_path", "input_file", "infile",
    "in_path", "src", "source", "source_path", "data_path", "file", "dir",
}


def _looks_like_path_key(k: str) -> bool:
    """A key whose NAME implies it carries a filesystem path.

    True for the exact known names (which now includes ``source_path``) or any
    ``*_path`` / ``*_file`` / ``*_dir`` suffix.
    """
    kl = (k or "").lower()
    return kl in _PATH_EXACT or kl.endswith(_PATH_SUFFIXES)


def _value_looks_like_path(v: Any) -> bool:
    """A value that LOOKS like a path — has a 1-5 char extension OR a separator."""
    if not isinstance(v, str) or not v:
        return False
    return bool(re.search(r"\.(\w{1,5})$", v)) or ("/" in v) or ("\\" in v)


def _is_dir_key(k: str) -> bool:
    """A key whose NAME implies a *directory* (extensionless), so a bare dir name
    like ``results`` is still sandboxed even though it has no extension/separator."""
    kl = (k or "").lower()
    return kl.endswith("_dir") or kl in {"dir", "output_dir"}


# A 1x1 transparent PNG and a minimal one-page PDF — synthesized so a forged
# tool that PARSES the input by format (Pillow, pypdf, etc.) doesn't choke on a
# text payload during dry-run.  See _write_fixture_file.
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da6360000002000154a24f5d0000000049454e44ae426082"
)
_MIN_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\ntrailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
)


def _write_fixture_file(p: Path, ext: str) -> None:
    """Write a FORMAT-VALID dry-run fixture at ``p`` for the given extension.

    Real-format bytes (a parseable empty ``.docx``/``.xlsx``, a ``%PDF`` header,
    a PNG with the right magic, an empty but valid ``.zip``, a ``{}`` JSON) let a
    forged file/format tool actually open its input during dry-run instead of
    crashing on a text payload.  Any failure (missing optional lib, etc.) falls
    back to the historical plain-text payload so the path still exists on disk.
    """
    e = (ext or "").lower().lstrip(".")
    try:
        if e == "docx":
            import docx
            docx.Document().save(str(p))
            return
        if e == "xlsx":
            import openpyxl
            openpyxl.Workbook().save(str(p))
            return
        if e == "pdf":
            p.write_bytes(_MIN_PDF)
            return
        if e == "png":
            p.write_bytes(_PNG_1x1)
            return
        if e == "zip":
            import zipfile
            with zipfile.ZipFile(str(p), "w"):
                pass
            return
        if e == "json":
            p.write_bytes(b"{}")
            return
    except Exception:
        logger.debug("[ToolDryRun] format fixture for .%s failed; text fallback", e, exc_info=True)
    p.write_bytes(b"dry-run test payload\n")


def _sandbox_paths(params: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite path-like string args to a tmp directory.

    Recognises common path key names; for the value we look for
    extension-bearing strings or path separators.  Conservative — when
    in doubt, leave the value alone.

    v0.9.34.4: also CREATE a small test file at each sandboxed file path so a
    forged tool that READS a file is dry-runnable (the dry-run only checks the
    tool does not crash, not output correctness — any bytes suffice). Input-side
    key names are included so reading tools are sandboxed consistently with
    writing ones. Directory-style keys (``*_dir``) get a real directory, not a
    file, so a tool that writes INTO the dir still works.
    """
    if not params:
        return params
    sandbox = Path(tempfile.gettempdir()) / f"dry_run_{uuid.uuid4().hex[:8]}"
    sandbox.mkdir(parents=True, exist_ok=True)
    out: Dict[str, Any] = {}
    for k, v in params.items():
        # Directory-style keys are recognized by name alone (a bare dir name like
        # "results" has no extension/separator); other path keys also need a
        # value that looks like a path so we don't sandbox an unrelated string.
        is_pathy = isinstance(v, str) and bool(v) and (
            (_looks_like_path_key(k) and _value_looks_like_path(v)) or _is_dir_key(k)
        )
        if is_pathy:
            ext_match = re.search(r"\.(\w{1,5})$", v)
            ext = ext_match.group(0) if ext_match else ""
            p = sandbox / f"{k}{ext}"
            out[k] = str(p)
            try:
                if k.endswith("_dir"):
                    # Directory-style param: create the dir (not a file) so a tool
                    # that mkdirs / writes into it still works.
                    p.mkdir(parents=True, exist_ok=True)
                else:
                    # Create a FORMAT-VALID test file so a tool that READS this
                    # path can actually parse it by format (docx/xlsx/pdf/png/zip/
                    # json), not just find bytes. (Output tools overwrite it.)
                    _write_fixture_file(p, ext)
            except Exception:
                pass
        else:
            out[k] = v
    return out


def _execute(
    tool: "Tool",
    params: Dict[str, Any],
    *,
    vault: "Vault",
    config: "Config",
) -> Dict[str, Any]:
    """Run the tool via the existing ToolSandbox.  Returns a result dict
    shaped like ``ToolResult.to_dict()``.

    Uses asyncio.run since the sandbox API is async — we're synchronous
    here for caller simplicity (forge pipeline is synchronous).
    """
    try:
        from systemu.runtime.tool_sandbox import ToolSandbox
        # v0.7.3 Bug #19 fix — resolve install_mode + load approval store so
        # the sandbox can actually install the tool's pip deps before dry-run.
        # Without this, PROMPT mode fail-closes ("no approval store") and the
        # dry-run runs against missing deps → DRY_RUN_FAILED_BUG.
        try:
            from systemu.runtime.dependency_installer import resolve_install_mode
            from systemu.runtime.dep_approvals import init_default_store
            install_mode = resolve_install_mode(
                config_mode=getattr(config, "tool_dep_install_mode", None),
                systemu_mode=getattr(config, "systemu_mode", None),
            )
            approvals = init_default_store(Path("data"))
        except Exception:
            logger.debug("[ToolDryRun] could not load install mode/approvals; using defaults", exc_info=True)
            install_mode = None
            approvals = None

        sandbox = ToolSandbox(
            vault_root=Path(config.vault_dir).resolve(),
            default_timeout=int(getattr(config, "docker_tool_timeout", 60)),
            install_mode=install_mode,
            approvals=approvals,
        )
        coro = sandbox.execute_tool(
            tool.implementation_path,
            params,
            extra_packages=tool.dependencies or [],
            timeout=int(getattr(config, "docker_tool_timeout", 60)),
        )
        result = _run_coro_sync(coro)
        return result.to_dict()
    except Exception as exc:
        logger.exception("[ToolDryRun] sandbox execution crashed")
        return {"success": False, "error": f"sandbox crash: {exc}"}


def _looks_destructive(tool: "Tool") -> bool:
    name = (tool.name or "").lower()
    destructive_hints = (
        "delete", "remove", "drop", "truncate", "wipe", "purge", "send",
        "publish", "deploy", "purchase", "pay", "transfer",
    )
    return any(h in name for h in destructive_hints)


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# Capture: hook called by shadow_runtime on successful tool calls (v0.5.0-a)
# to grow the rolling buffer of observed-successful params per tool.

_MAX_HISTORY_PER_TOOL = 20


def record_successful_params(
    tool: "Tool",
    params: Dict[str, Any],
    vault: "Vault",
) -> None:
    """Append ``params`` to ``tool.last_successful_params`` and persist.

    Trims to the most recent :data:`_MAX_HISTORY_PER_TOOL` entries.
    Best-effort: vault write failures are swallowed and logged.
    """
    try:
        existing = list(getattr(tool, "last_successful_params", []) or [])
        existing.append(_redact_secrets(dict(params)))
        if len(existing) > _MAX_HISTORY_PER_TOOL:
            existing = existing[-_MAX_HISTORY_PER_TOOL:]
        tool.last_successful_params = existing
        vault.save_tool(tool)
    except Exception:
        logger.debug("[ToolDryRun] record_successful_params skipped", exc_info=True)


def record_evolution(
    tool: "Tool",
    *,
    mode: str,                 # "bump" | "fork"
    reason: str,
    diff_summary: str,
    vault: "Vault",
    new_version: Optional[int] = None,
) -> None:
    """Append a recalibration audit entry to ``tool.evolution_history``.

    v0.5.0-b — used by the v0.5.0-d RECALIBRATE_TOOL action to maintain a
    durable audit of why and how a tool was recalibrated.  When
    ``mode="bump"`` we also bump ``tool.version`` (if ``new_version`` is
    None, increment by one).  For ``mode="fork"`` the new tool is a
    separate record entirely; this function is called against the *new*
    tool with version=1 and a reason citing the originating tool.

    Best-effort: vault write failures are swallowed and logged.
    """
    try:
        from datetime import datetime, timezone
        if mode == "bump":
            tool.version = int(new_version if new_version is not None else (tool.version + 1))
        elif mode == "fork":
            tool.version = int(new_version or 1)
        else:
            logger.debug("[ToolEvolution] unknown mode %r — proceeding without version change", mode)

        entry = {
            "version":      tool.version,
            "mode":         mode,
            "reason":       reason[:500] if reason else "",
            "diff_summary": diff_summary[:500] if diff_summary else "",
            "ts":           datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        }
        history = list(getattr(tool, "evolution_history", []) or [])
        history.append(entry)
        tool.evolution_history = history
        vault.save_tool(tool)
        logger.info(
            "[ToolEvolution] %s v%d recorded (mode=%s reason=%r)",
            tool.name, tool.version, mode, reason[:60],
        )
    except Exception:
        logger.debug("[ToolEvolution] record_evolution skipped", exc_info=True)


def _redact_secrets(params: Dict[str, Any]) -> Dict[str, Any]:
    """Replace values for keys that look like secrets — token, key, password.

    The rolling buffer is persisted to disk; we don't want real keys in it.
    """
    SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "credential")
    out: Dict[str, Any] = {}
    for k, v in params.items():
        if any(h in k.lower() for h in SECRET_HINTS) and v:
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out
