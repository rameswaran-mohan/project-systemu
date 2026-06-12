"""W8.2 — the quick lane: a bounded ReAct loop for one-shot asks.

The factory pipeline (refine → approve → extract → decide → persona →
execute) is the right machine for *repeatable workflows*; as the default path
for "find me a spa" it costs ~70 seconds of meta-work and four LLM calls
before the first useful action. `run_quick_task` answers in seconds:

  prompt → [LLM decides: TOOL_CALL | ANSWER | ASK_USER] → tools execute →
  … → answer markdown (+ files produced)

Safety/truth properties (deliberate, tested):
  * Tool surface = v1 vault tools that are BOTH `enabled` (Gate-3 — the quick
    lane can never call a tool the operator hasn't enabled) AND runtime-ready
    (dry-run gates respected). The v2 registry's runtime-internal tools
    (delegate, curator, vault audit, …) are intentionally NOT exposed here.
  * Execution goes through the SAME ToolSandbox contract the full runtime
    uses (Wave-6 subprocess runner, W6.2 truth-in-results, dependency
    install policy) — one execution stack, never a fork of it.
  * Hard caps: iteration budget, wall clock, same-tool failure streak (3),
    consecutive malformed LLM actions (2). Failures are honest, never silent.
  * Every iteration publishes a live event (origin="chat") with the
    reasoning/params/result details, so the existing live panes stream the
    run with expand arrows — zero new pane code.

The LLM and sandbox are injectable for keyless, networkless tests.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 2000     # per-entry transcript clamp (context economy)
_SAME_TOOL_FAIL_CAP = 3
_MALFORMED_CAP = 2


@dataclass
class QuickResult:
    status: str                       # success | failed | needs_input
    answer_md: str = ""
    files_produced: List[str] = field(default_factory=list)
    tool_calls: int = 0
    iterations: int = 0
    error: Optional[str] = None
    question: Optional[str] = None    # set when status == needs_input


def _enabled_tool_records(vault) -> List[Any]:
    """Full Tool records the quick lane may use: Gate-3 enabled AND
    runtime-ready (same predicate the readiness gate uses)."""
    from systemu.runtime.shadow_runtime import tool_is_runtime_ready

    records: List[Any] = []
    try:
        headers = vault.load_index("tools") or []
    except Exception:
        return []
    for header in headers:
        try:
            tool = vault.get_tool(header["id"])
        except Exception:
            continue
        if not getattr(tool, "enabled", False):
            continue
        if not tool_is_runtime_ready(tool.status):
            continue
        if not getattr(tool, "implementation_path", ""):
            continue
        records.append(tool)
    return records


def _tool_index(tools: List[Any]) -> List[Dict[str, Any]]:
    """The LLM-visible index — same shape the full runtime presents."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameter_names": list(getattr(t, "parameter_names", []) or []),
            "parameters_schema": dict(getattr(t, "parameters_schema", {}) or {}),
        }
        for t in tools
    ]


def _mcp_quick_entries(vault, taken_names) -> List[Dict[str, Any]]:
    """W9.3 — operator-ENABLED MCP connector tools for the quick-lane index.

    Vault tools always win name collisions: a remote connector must never
    shadow a local tool (hijack guard) — colliding entries are dropped loud.
    """
    try:
        from systemu.runtime.mcp.connections import enabled_tools
        entries = []
        for entry in enabled_tools(vault):
            if entry.get("name") in taken_names:
                logger.warning(
                    "[QuickTask] MCP tool %r on %s collides with a vault tool — skipped",
                    entry.get("name"), entry.get("server"))
                continue
            entries.append(entry)
        return entries
    except Exception:
        logger.debug("[QuickTask] MCP entries unavailable", exc_info=True)
        return []


def _execute_mcp_tool(entry: Dict[str, Any], params: Dict[str, Any], config):
    """Dispatch one connector call with the truth-in-results envelope:
    an empty payload is NOT success (W6.2 applies to connectors too)."""
    from types import SimpleNamespace
    import systemu.runtime.mcp.client as mcp_client   # module attr → patchable

    out = mcp_client.mcp_call_tool(
        server=entry["server"], name=entry["name"],
        params=params, config=config)
    response = out.get("response")
    if not out.get("success"):
        return SimpleNamespace(success=False, parsed={},
                               error=str(out.get("error") or "MCP call failed"))
    if not response:
        return SimpleNamespace(success=False, parsed={},
                               error="MCP tool returned no payload")
    parsed = response if isinstance(response, dict) else {"response": response}
    return SimpleNamespace(success=True, parsed=parsed, error=None)


def _default_llm_json(*, system: str, user: str, config) -> Dict[str, Any]:
    from systemu.core.llm_router import llm_call_json
    return llm_call_json(tier=1, system=system, user=user, config=config,
                         temperature=0.2, max_tokens=4000)


def _clamp(obj: Any, limit: int = _MAX_RESULT_CHARS) -> str:
    try:
        text = json.dumps(obj, default=str)
    except Exception:
        text = str(obj)
    return text[:limit]


def _publish(level: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort live event — the run must never die on telemetry."""
    try:
        from systemu.interface.notifications import log_event
        log_event(level, "quick_task", message, {"origin": "chat"},
                  details=details or None)
    except Exception:
        logger.debug("[QuickTask] live event publish failed", exc_info=True)


def run_quick_task(
    prompt: str,
    config,
    vault,
    *,
    llm_json: Optional[Callable[..., Dict[str, Any]]] = None,
    sandbox=None,
    max_iters: int = 12,
    wall_clock_s: float = 240.0,
) -> QuickResult:
    """Run one bounded ReAct loop and return the outcome. Never raises."""
    from systemu.core.utils import load_prompt

    llm = llm_json or _default_llm_json
    if sandbox is None:
        from pathlib import Path as _Path

        from systemu.runtime.dep_approvals import init_default_store
        from systemu.runtime.dependency_installer import resolve_install_mode
        from systemu.runtime.tool_sandbox import ToolSandbox

        # W11.7: the SAME installer policy as the full runtime. This sandbox
        # used to be constructed without install_mode/approvals — PROMPT mode
        # with approvals=None fail-closes EVERY dep-declaring tool, so the
        # default chat lane couldn't run most web tools even when the
        # packages were installed and previously approved (field RCA
        # 2026-06-12). The W6 lesson said never fork the execution path;
        # the construction wiring is part of that path.
        try:
            install_mode = resolve_install_mode(
                config_mode=getattr(config, "tool_dep_install_mode", None),
                systemu_mode=getattr(config, "systemu_mode", None),
            )
            approvals = init_default_store(_Path("data"))
        except Exception:
            install_mode, approvals = None, None
        sandbox = ToolSandbox(getattr(vault, "root", None), vault=vault,
                              config=config, install_mode=install_mode,
                              approvals=approvals)

    try:
        system_prompt = load_prompt("quick_task.md")
    except Exception:
        system_prompt = "Reply with one JSON action: TOOL_CALL, ANSWER, or ASK_USER."

    # W9.2: the quick lane must know who it works for — without this the
    # fastest path was the most identity-blind one (runs guessed the
    # operator's location by IP).
    try:
        from systemu.runtime.user_context import profile_context_block
        _ctx_block = profile_context_block(vault)
        if _ctx_block:
            system_prompt = f"{system_prompt}\n\n{_ctx_block}"
    except Exception:
        logger.debug("[QuickTask] profile context skipped", exc_info=True)

    tools = _enabled_tool_records(vault)
    by_name = {t.name: t for t in tools}
    index = _tool_index(tools)

    # W9.3: operator-enabled MCP connector tools join the surface (persisted
    # metadata only — no network at prompt-build); vault names take precedence.
    mcp_by_name: Dict[str, Dict[str, Any]] = {}
    for entry in _mcp_quick_entries(vault, set(by_name)):
        mcp_by_name[entry["name"]] = entry
        index.append({
            "name": entry["name"],
            "description": f"[connector] {entry.get('description', '')}",
            "parameter_names": [],
            "parameters_schema": dict(entry.get("schema") or {}),
        })

    # Deliverables contract: mirror the sandbox's output_dir derivation
    # (config.output_dir or <vault>/output), pre-normalize write paths with
    # the SAME function the sandbox uses (so artifact collection sees the
    # EFFECTIVE path, not the LLM's original), and make sure the directory
    # exists — the redirect target not existing turned writes into failures.
    from pathlib import Path as _Path
    from systemu.runtime.tool_sandbox import _normalize_output_paths
    output_dir = ((getattr(config, "output_dir", "") or "") if config else "") \
        or str(_Path(getattr(vault, "root", ".")) / "output")
    try:
        _Path(output_dir).mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("[QuickTask] could not ensure output_dir", exc_info=True)

    history: List[Dict[str, Any]] = []
    files: List[str] = []
    tool_calls = 0
    malformed_streak = 0
    fail_streaks: Dict[str, int] = {}
    started = time.monotonic()

    def _finish(result: QuickResult) -> QuickResult:
        result.files_produced = files
        result.tool_calls = tool_calls
        level = {"success": "SUCCESS", "needs_input": "WARNING"}.get(result.status, "ERROR")
        head = result.answer_md or result.question or result.error or ""
        _publish(level, f"Quick task {result.status}: {prompt[:80]}",
                 details={"summary": head[:1000],
                          "output_dir": getattr(config, "output_dir", "") or "",
                          })
        return result

    iteration = 0
    for iteration in range(1, max_iters + 1):
        if time.monotonic() - started > wall_clock_s:
            return _finish(QuickResult(
                status="failed", iterations=iteration - 1,
                error=f"wall-clock budget exceeded ({int(wall_clock_s)}s)"))

        payload = json.dumps({
            "task": prompt,
            "iteration": iteration,
            "max_iterations": max_iters,
            "tools": index,
            "history": history[-16:],
        }, default=str)

        try:
            action = llm(system=system_prompt, user=payload, config=config)
        except Exception as exc:
            return _finish(QuickResult(
                status="failed", iterations=iteration,
                error=f"LLM call failed: {exc}"))

        kind = (action or {}).get("action") if isinstance(action, dict) else None

        if kind == "ANSWER":
            answer = str(action.get("answer_md") or "").strip()
            # W13.6 (groundedness, minimal): the model's own honest verdict.
            # A "could not complete" answer must never report success — the
            # field run 'nearest barito shop' admitted failure in the text
            # yet was counted a success. Missing/odd values default to True
            # (back-compat with scripted tests and older transcripts).
            completed = action.get("completed")
            completed = True if completed is None else bool(completed)
            _publish("INFO" if completed else "WARNING",
                     f"[{iteration}/{max_iters}] answer ready"
                     + ("" if completed else " (task NOT completed)"))
            return _finish(QuickResult(
                status="success" if completed else "partial",
                answer_md=answer or "(empty answer)",
                iterations=iteration))

        if kind == "ASK_USER":
            question = str(action.get("question") or "").strip()
            return _finish(QuickResult(
                status="needs_input", iterations=iteration,
                question=question or "(no question given)",
                answer_md=question))

        if kind == "TOOL_CALL":
            malformed_streak = 0
            tool_name = str(action.get("tool") or "")
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            reasoning = str(action.get("reasoning") or "")
            tool = by_name.get(tool_name)
            mcp_entry = mcp_by_name.get(tool_name) if tool is None else None
            if tool is None and mcp_entry is None:
                history.append({
                    "role": "tool_result", "tool": tool_name, "success": False,
                    "error": (f"'{tool_name}' is an unknown or disabled tool — "
                              f"choose from the provided tools list only."),
                })
                _publish("WARNING",
                         f"[{iteration}/{max_iters}] blocked call to unknown/disabled tool '{tool_name}'")
                continue

            params = _normalize_output_paths(tool_name, params, output_dir)
            history.append({"role": "tool_call", "tool": tool_name,
                            "params": params, "reasoning": reasoning})
            denial = _safety_denied(tool_name, params)
            if denial is not None:
                # Counts as a failed call: the same-tool cap + the model's
                # own adaptation (or ASK_USER) take it from here.
                from types import SimpleNamespace as _NS
                result = _NS(success=False, parsed={
                    "success": False, "error": denial,
                    "error_type": "destructive_auto_denied"}, error=denial)
            elif mcp_entry is not None:
                result = _execute_mcp_tool(mcp_entry, params, config)
            else:
                result = _execute_tool(sandbox, tool, params)
            tool_calls += 1

            parsed = getattr(result, "parsed", {}) or {}
            success = bool(getattr(result, "success", False))
            error = getattr(result, "error", None)
            if not success:
                # W11.7: a failure with no message is unactionable — the
                # field report literally read "run_command failed: None".
                error = _failure_error(error, parsed)
            if (not success and parsed.get("error_type")
                    == "dependency_install_pending_approval"):
                # W13.2b: a blocked package must be ONE CLICK away — surface
                # the dep gate in needs-you immediately (idempotent dedup);
                # Approve installs it and the next attempt proceeds.
                try:
                    from systemu.runtime.tool_registry import (
                        _maybe_enqueue_dep_gate)
                    for _pkg in (parsed.get("missing_packages") or []):
                        _maybe_enqueue_dep_gate(
                            vault=vault, tool_id=getattr(tool, "id", "") or "",
                            tool_name=tool_name, package=str(_pkg))
                    error = (str(error) + " An Approve & install button is "
                             "waiting in the dashboard's Needs-you.")
                except Exception:
                    logger.debug("[QuickTask] dep gate enqueue failed",
                                 exc_info=True)
            history.append({
                "role": "tool_result", "tool": tool_name, "success": success,
                "parsed": _clamp(parsed), "error": error,
            })
            _publish("INFO" if success else "WARNING",
                     f"[{iteration}/{max_iters}] {tool_name} → "
                     + ("ok" if success else f"failed: {str(error)[:80]}"),
                     details={"reasoning": reasoning, "tool_params": params,
                              "tool_result": parsed})

            if success:
                fail_streaks[tool_name] = 0
                try:
                    from systemu.runtime.artifacts import collect_artifact_paths
                    for path in collect_artifact_paths(tool_name, params, parsed):
                        if path not in files:
                            files.append(path)
                except Exception:
                    logger.debug("[QuickTask] artifact collection failed", exc_info=True)
            else:
                fail_streaks[tool_name] = fail_streaks.get(tool_name, 0) + 1
                if fail_streaks[tool_name] >= _SAME_TOOL_FAIL_CAP:
                    return _finish(QuickResult(
                        status="failed", iterations=iteration,
                        error=(f"tool '{tool_name}' failed "
                               f"{_SAME_TOOL_FAIL_CAP} times in a row: {error}")))
            continue

        # Malformed action.
        malformed_streak += 1
        history.append({
            "role": "tool_result", "success": False,
            "error": ("Your reply was not one of the three valid JSON actions "
                      "(TOOL_CALL / ANSWER / ASK_USER). Reply with exactly one."),
        })
        if malformed_streak >= _MALFORMED_CAP:
            return _finish(QuickResult(
                status="failed", iterations=iteration,
                error="the model returned malformed actions twice in a row"))

    return _finish(QuickResult(
        status="failed", iterations=iteration,
        error=f"iteration budget exhausted ({max_iters}) without an answer"))


def _safety_denied(tool_name: str, params: Dict[str, Any]) -> "str | None":
    """W12 (audit F5): the quick lane's destructive gate.

    The default chat lane carried NO destructive check at all (the workflow
    runtime gates; this lane didn't — an asymmetry hole). Destructive calls
    are denied with an actionable message the model can adapt to; provably
    read-only shell commands pass (`is_destructive_call` judges commands,
    not tool names). Returns the denial message, or None to allow.
    """
    try:
        from systemu.runtime.tool_sandbox import ToolSandbox
        if not ToolSandbox.is_destructive_call(tool_name, params):
            return None
    except Exception:
        return None  # classifier failure must never block the lane
    return (f"Safety gate: '{tool_name}' with these params is classified "
            "destructive and the quick lane runs unattended. Use a read-only "
            "alternative, or ASK_USER to get the operator's go-ahead.")


def _failure_error(error, parsed) -> str:
    """An always-actionable failure message (W11.7).

    Falls back from the tool's error to its stderr tail to its exit code —
    never the string "None".
    """
    if error:
        return str(error)
    parsed = parsed or {}
    stderr_tail = str(parsed.get("stderr") or "")[-200:].strip()
    if stderr_tail:
        return stderr_tail
    code = parsed.get("returncode", parsed.get("exit_code"))
    if code is not None:
        return f"tool reported failure (exit {code})"
    return "tool reported failure without an error message"


def _execute_tool(sandbox, tool, params: Dict[str, Any]):
    """Execute one tool through the EXACT runtime contract (W6 runner,
    subprocess isolation policy, dependency install). Sync wrapper — the
    quick lane runs in a worker thread."""
    from systemu.core.llm_router import _run_coroutine
    from systemu.runtime.tool_sandbox import requires_subprocess_isolation

    return _run_coroutine(sandbox.execute_tool(
        tool.implementation_path,
        params,
        extra_packages=tool.dependencies or [],
        tool_type=getattr(tool.tool_type, "value", tool.tool_type),
        force_subprocess=requires_subprocess_isolation(tool),
    ))


def promote_to_workflow(prompt: str, config, vault):
    """Promote a one-shot ask into the factory pipeline WITHOUT executing it.

    Creates the refined scroll via the normal Stage-1 entry (it lands in
    /work, where the existing approval → extraction → shadow flow takes
    over). Deliberately does NOT extract, decide, or execute — promotion is
    "make this repeatable", not "run it again". Returns the Scroll.
    """
    from systemu.pipelines.scroll_refiner import refine_from_text
    return refine_from_text(prompt, vault, config)


def submit_quick_task(prompt: str, config, vault, **kwargs) -> QuickResult:
    """Chat-facing wrapper: run the quick lane AND keep the chat-history
    contract (same fields direct_task writes), so the Status dropdown, the
    chat thread, and the fact-extraction hook work unchanged."""
    from datetime import datetime

    ts = datetime.now().isoformat(timespec="seconds")
    try:
        vault.append_chat_history({
            "ts": ts, "prompt": prompt, "status": "running", "lane": "quick",
        })
    except Exception:
        logger.debug("[QuickTask] could not append chat history", exc_info=True)

    result = run_quick_task(prompt, config, vault, **kwargs)

    status = {"needs_input": "pending_decision"}.get(result.status, result.status)
    # needs_input is rendered by chat as the question itself, not a park —
    # keep the plain status for v1 (no decision row exists to resolve).
    status = result.status if result.status != "needs_input" else "needs_input"
    try:
        vault.update_chat_history_entry(ts, {
            "status": status,
            "summary": result.answer_md,
            "error": result.error,
            "files_produced": list(result.files_produced),
            "lane": "quick",
        })
    except Exception:
        logger.debug("[QuickTask] could not update chat history", exc_info=True)

    # W9.4: feed the flywheel — quick runs (the default lane) previously
    # bypassed Stage-5 entirely, so the evolution engine and episodic memory
    # got zero signal from them. Reuse the SAME best-effort capture hook the
    # workflow lane uses (gated by config.summarize_after_run; no-op when
    # config is None).
    try:
        from systemu.runtime.shadow_runtime import _trigger_episodic_capture
        _trigger_episodic_capture(
            vault=vault, config=config, session_id=ts,
            intent=prompt, chat_result=result.answer_md,
            files_produced=list(result.files_produced),
            status=result.status, execution_id=None, raw_chat_id=ts,
        )
    except Exception:
        logger.debug("[QuickTask] episodic capture skipped", exc_info=True)
    return result
