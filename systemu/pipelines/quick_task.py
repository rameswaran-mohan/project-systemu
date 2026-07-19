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
_ASK_USER_CAP = 3            # v0.9.43: cap operator questions per run (anti re-ask)
_SYNTH_MAX_TOKENS = 700
_MAX_PLANS = 3               # anti plan-thrash: cap re-plans per run
# Keys whose non-empty presence in a tool payload counts as "usable content".
_USABLE_CONTENT_KEYS = ("results", "records", "places", "data", "text",
                        "content", "answer", "items", "rows", "response")


@dataclass
class QuickResult:
    status: str                       # success | failed | needs_input
    answer_md: str = ""
    files_produced: List[str] = field(default_factory=list)
    tool_calls: int = 0
    iterations: int = 0
    error: Optional[str] = None
    question: Optional[str] = None    # set when status == needs_input
    # R-P3a (per-run cost visibility): the run's LLM usage rows
    # ({model, tokens_in, tokens_out}) drained from costing._LEDGER at finish so
    # the quick lane (the DEFAULT lane) shows tokens+cost like the workflow lane —
    # AC1 "both lanes". costing.cost_of(result) prices this. NOT a caps mechanism.
    cost: List[Dict[str, Any]] = field(default_factory=list)


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


def _execute_mcp_tool(entry: Dict[str, Any], params: Dict[str, Any], config,
                      *, vault=None, session_id: str = ""):
    """Dispatch one connector call through the v0.9.34 P0 gated chokepoint
    (allowlist + risk-tiered action gate + output guard). Truth-in-results: an
    empty payload is NOT success (W6.2 applies to connectors too).

    An action gate may raise PendingOperatorDecision; the chat lane block-polls
    for the operator's choice (mirrors _execute_tool). Deny/timeout → a clean
    fail-closed denial result so the ReAct loop adapts (never crashes)."""
    from types import SimpleNamespace
    from systemu.runtime.mcp.dispatch import call_mcp_tool
    from systemu.approval.exceptions import PendingOperatorDecision

    def _denied(msg: str):
        return SimpleNamespace(success=False, parsed={
            "success": False, "error": msg, "error_type": "mcp_denied"},
            error=msg)

    def _call(resolved_dedup=None):
        return call_mcp_tool(
            entry["server"], entry["name"], params,
            vault=vault, config=config, session_id=session_id,
            resolved_dedup=resolved_dedup)

    try:
        out = _call()
    except PendingOperatorDecision as pend:
        choice = _poll_command_choice(vault, pend.dedup_key)
        norm = (choice or "").strip().lower()
        if norm in ("approve once", "always allow",
                    "trust this tool for the session"):
            try:
                out = _call(resolved_dedup=pend.dedup_key)
            except PendingOperatorDecision:
                return _denied("MCP call approved once but re-gated; not run.")
            except Exception:
                logger.exception("[QuickTask] MCP gate re-attempt errored "
                                 "— fail-closed denial")
                return _denied("MCP approval re-attempt failed — denied "
                               "(fail-closed).")
        else:
            msg = ("MCP call denied by operator." if norm == "deny"
                   else "MCP approval timed out — denied (fail-closed).")
            return _denied(msg)
    except Exception:
        logger.exception("[QuickTask] MCP dispatch raised — fail-closed denial")
        return _denied("MCP dispatch failed unexpectedly — denied (fail-closed).")

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


def _has_usable_observation(history: List[Dict[str, Any]]) -> bool:
    """True iff history holds >=1 successful tool result carrying non-empty
    content. A success with an empty payload (e.g. a web_search 200 with zero
    hits) is NOT usable — synthesizing from it is the hallucination edge."""
    for h in history:
        if h.get("role") != "tool_result" or not h.get("success"):
            continue
        blob = h.get("parsed")
        if blob in (None, ""):
            continue
        try:
            obj = json.loads(blob) if isinstance(blob, str) else blob
        except Exception:
            obj = None
        if isinstance(obj, dict):
            if any(obj.get(k) not in (None, "", [], {}, 0) for k in _USABLE_CONTENT_KEYS):
                return True
            continue
        if isinstance(blob, str) and len(blob.strip()) > 2:
            return True
    return False


def _default_synthesize(prompt: str, history: List[Dict[str, Any]], config) -> Optional[str]:
    """Grounding-only, cheap-tier final answer from gathered observations.
    Returns None on any failure so the caller keeps an honest 'failed'. Fires
    only after the loop budget is spent — bounded by tier-3 + low max_tokens."""
    try:
        from systemu.core.llm_router import llm_call_json
        obs = [{"tool": h.get("tool"), "result": h.get("parsed")}
               for h in history
               if h.get("role") == "tool_result" and h.get("success")][-12:]
        if not obs:
            return None
        system = (
            "The assistant ran out of tool budget before answering. Using ONLY "
            "the tool observations provided, write the most useful HONEST answer "
            "to the operator's request. Quote concrete data actually present "
            "(names, ratings, addresses, file paths). If the data is partial, say "
            "so and state what is missing. Invent NOTHING not in the observations. "
            'Reply with ONE JSON object: {"answer_md": "<markdown answer>"}.')
        user = json.dumps({"request": prompt, "observations": obs}, default=str)
        out = llm_call_json(tier=3, system=system, user=user, config=config,
                            temperature=0.1, max_tokens=_SYNTH_MAX_TOKENS)
        text = (out.get("answer_md") if isinstance(out, dict) else None) or ""
        return text.strip() or None
    except Exception:
        logger.debug("[QuickTask] synthesis fallback failed", exc_info=True)
        return None


def run_quick_task(
    prompt: str,
    config,
    vault,
    *,
    llm_json: Optional[Callable[..., Dict[str, Any]]] = None,
    sandbox=None,
    synthesize: Optional[Callable[..., Optional[str]]] = None,
    max_iters: int = 12,
    wall_clock_s: float = 240.0,
    cancel_event: Optional["threading.Event"] = None,
    chat_surface: bool = False,
) -> QuickResult:
    """Run one bounded ReAct loop and return the outcome. Never raises.

    v0.9.43: ``chat_surface=True`` (set by the chat wrapper when a live operator
    is present) lets an ASK_USER PARK on an answerable decision and resume with
    the typed answer instead of dead-ending. Default False keeps the historical
    ``needs_input`` terminal for CLI / headless / direct callers.
    """
    import threading  # noqa: F401  (typing/back-compat; Event is duck-checked)
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
            # v0.9.32 D.6: thread the per-command approval store so the chat
            # lane's sandbox gate (block-and-ask) can consult Always-allow.
            from systemu.runtime.command_approvals import (
                init_default_store as _init_cmd_store)
            command_approvals = _init_cmd_store(_Path("data"))
        except Exception:
            install_mode, approvals = None, None
            command_approvals = None
        sandbox = ToolSandbox(getattr(vault, "root", None), vault=vault,
                              config=config, install_mode=install_mode,
                              approvals=approvals,
                              command_approvals=command_approvals)

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

    # R-CAP1 · CAP-4 — order the LLM-visible tool index most-relevant-first for
    # THIS task (deterministic slot+lexical rank). NEVER-SUBTRACT: every tool
    # keeps its full schema and stays in the list; only the order changes, so the
    # model can still pick any tool. Defensive — a hiccup leaves the order as-is.
    try:
        from systemu.runtime import capability_index as _capidx
        index = _capidx.order_records(index, prompt)
    except Exception:
        logger.debug("[QuickTask] capability ordering skipped", exc_info=True)

    # Deliverables contract: mirror the sandbox's output_dir derivation
    # (config.output_dir or <vault>/output), pre-normalize write paths with
    # the SAME function the sandbox uses (so artifact collection sees the
    # EFFECTIVE path, not the LLM's original), and make sure the directory
    # exists — the redirect target not existing turned writes into failures.
    from pathlib import Path as _Path
    from systemu.runtime.tool_sandbox import _normalize_output_paths
    output_dir = ((getattr(config, "output_dir", "") or "") if config else "") \
        or str(_Path(getattr(vault, "root", ".") or ".") / "output")
    try:
        _Path(output_dir).mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("[QuickTask] could not ensure output_dir", exc_info=True)

    # v0.9.34 P0: a per-run session id scopes MCP "Trust for session" so trust
    # cannot leak across runs (mcp_session_key bakes it into the hash). The quick
    # lane is NOT a ShadowRuntime run, so it does NOT read the mcp_run_ctx carrier
    # — it mints its own id and passes it explicitly through _execute_mcp_tool.
    # Stable within one run_quick_task call, different across calls.
    import uuid as _uuid
    execution_id = f"quick_{_uuid.uuid4().hex[:12]}"

    # R-P3a (AC1 "both lanes"): the quick lane is NOT a ShadowRuntime run, so it
    # never set the ambient execution_id — every quick-lane LLM call would ORPHAN
    # its cost (current_execution_id() == None → the router hook no-ops). Stamp it
    # here (mirroring shadow_runtime.execute) so the router accumulator attributes
    # calls to THIS run; _finish resets it on every terminal (all paths route
    # through _finish/_terminate). Best-effort: never break the lane on a carrier
    # import failure.
    _eid_token = None
    try:
        from systemu.runtime.chat_submission_ctx import set_execution_id as _set_eid
        _eid_token = _set_eid(execution_id)
    except Exception:
        logger.debug("[QuickTask] execution_id carrier unavailable", exc_info=True)

    history: List[Dict[str, Any]] = []
    files: List[str] = []
    tool_calls = 0
    malformed_streak = 0
    ask_count = 0                    # v0.9.43: operator questions asked this run
    fail_streaks: Dict[str, int] = {}
    failed_sigs: set = set()         # (tool|params) calls that already failed
    tool_errors: Dict[str, str] = {}  # last error text per tool (honest fail msg)
    plan: List[str] = []             # plan-first: the model's todo for this run
    plan_count = 0
    started = time.monotonic()

    def _finish(result: QuickResult) -> QuickResult:
        result.files_produced = files
        result.tool_calls = tool_calls
        # R-P3a: attach the run's cost rows (drained from the live ledger) and
        # release the ambient execution_id. Every terminal routes through here,
        # so this is the single reset point (no leak into the next same-thread run).
        try:
            from systemu.runtime import costing
            result.cost = costing.usage_rows(execution_id)
        except Exception:
            logger.debug("[QuickTask] cost attach skipped", exc_info=True)
        try:
            from systemu.runtime.chat_submission_ctx import set_execution_id as _reset_eid
            _reset_eid(None, reset_token=_eid_token)
        except Exception:
            logger.debug("[QuickTask] execution_id reset skipped", exc_info=True)
        # v0.9.32 (review FIX 4): an intentional operator interrupt publishes at
        # WARNING, not ERROR — matches the supervisor's cancelled publish level.
        level = {"success": "SUCCESS", "partial": "WARNING",
                 "needs_input": "WARNING", "cancelled": "WARNING"}.get(
                     result.status, "ERROR")
        head = result.answer_md or result.question or result.error or ""
        _publish(level, f"Quick task {result.status}: {prompt[:80]}",
                 details={"summary": head[:1000],
                          "output_dir": getattr(config, "output_dir", "") or "",
                          })
        return result

    synth = synthesize or _default_synthesize

    def _terminate(reason: str, iters: int) -> QuickResult:
        """Machine-owned exit: salvage an honest partial from gathered data,
        else keep an honest failure. Never invents from nothing."""
        if _has_usable_observation(history):
            answer = synth(prompt, history, config)
            if answer:
                return _finish(QuickResult(
                    status="partial", answer_md=answer, error=reason, iterations=iters))
        return _finish(QuickResult(status="failed", error=reason, iterations=iters))

    iteration = 0
    for iteration in range(1, max_iters + 1):
        # v0.9.32 (D3.2): cooperative operator interrupt — checked at the loop
        # boundary, beside the wall-clock budget. Salvage an honest partial from
        # whatever was gathered, else an honest cancelled result. Never invents.
        if cancel_event is not None and cancel_event.is_set():
            if _has_usable_observation(history):
                _answer = synth(prompt, history, config)
                if _answer:
                    return _finish(QuickResult(
                        status="cancelled", answer_md=_answer,
                        error="cancelled by operator", iterations=iteration - 1))
            return _finish(QuickResult(
                status="cancelled", error="cancelled by operator",
                iterations=iteration - 1))
        if time.monotonic() - started > wall_clock_s:
            return _terminate(
                f"wall-clock budget exceeded ({int(wall_clock_s)}s)", iteration - 1)

        payload = json.dumps({
            "task": prompt,
            "iteration": iteration,
            "max_iterations": max_iters,
            "iterations_left": max_iters - iteration,
            "final_turn": iteration >= max_iters,
            "plan": plan,
            "tools": index,
            "history": history[-16:],
        }, default=str)

        try:
            action = llm(system=system_prompt, user=payload, config=config)
        except Exception as exc:
            return _terminate(f"LLM call failed: {exc}", iteration)

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
            # v0.9.43: holistic answer-and-resume. With a live operator chat
            # surface, PARK on an answerable decision and BLOCK-POLL for the
            # answer (the same wait strategy _poll_command_choice uses for
            # command gates — the daemon thread stays alive, so all loop state
            # is intact). Inject the answer as a tool_result the model AND the
            # salvage paths understand, then CONTINUE the same loop. Without a
            # chat surface (CLI / headless / direct callers) keep the historical
            # needs_input terminal.
            if not chat_surface:
                return _finish(QuickResult(
                    status="needs_input", iterations=iteration,
                    question=question or "(no question given)",
                    answer_md=question))
            ask_count += 1
            if ask_count > _ASK_USER_CAP:
                return _terminate(
                    f"asked the operator {ask_count} questions without reaching "
                    f"an answer", iteration)
            _publish("INFO",
                     f"[{iteration}/{max_iters}] waiting for your answer…")
            answer = _ask_operator_inline(
                vault, question or "Input needed",
                dedup_key=f"quick_ask:{execution_id}:{iteration}",
                cancel_event=cancel_event)
            if not answer:
                # declined / cancelled / timed out -> honest terminal
                if cancel_event is not None and cancel_event.is_set():
                    return _finish(QuickResult(
                        status="cancelled", error="cancelled by operator",
                        iterations=iteration))
                return _finish(QuickResult(
                    status="needs_input", iterations=iteration,
                    question=question or "(no question given)",
                    answer_md=question))
            history.append({
                "role": "tool_result", "tool": "ask_user", "success": True,
                "parsed": {
                    "question": question, "answer": answer,
                    "note": ("Operator answered your question — use this "
                             "answer to continue toward the goal; do not ask it "
                             "again."),
                },
            })
            _publish("INFO",
                     f"[{iteration}/{max_iters}] operator answered — continuing")
            continue

        if kind == "PLAN":
            # Plan-first (adaptive): a non-trivial task decomposes into a short
            # todo before acting. The plan rides in every later payload so
            # execution follows it; re-plans are capped to avoid plan-thrash.
            steps = [str(s).strip() for s in (action.get("steps") or [])
                     if str(s).strip()][:8]
            if steps and plan_count < _MAX_PLANS:
                plan = steps
                plan_count += 1
                history.append({"role": "plan", "steps": plan,
                                "reasoning": str(action.get("reasoning") or "")})
                _publish("INFO",
                         f"[{iteration}/{max_iters}] planned {len(plan)} step(s)")
            continue

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
            sig = tool_name + "|" + json.dumps(params, sort_keys=True, default=str)
            if sig in failed_sigs:
                # The live RCA's step-1 == step-12 loop: refuse to re-run a call
                # that already failed. Counts toward the same-tool cap so an
                # unchanged retry can't thrash the loop to its budget.
                history.append({
                    "role": "tool_result", "tool": tool_name, "success": False,
                    "error": ("This exact call already failed earlier in this run — "
                              "do not repeat it. Change the tool or parameters, or "
                              "ANSWER with what you already have."),
                })
                _publish("WARNING",
                         f"[{iteration}/{max_iters}] blocked repeat of a call "
                         f"that already failed: {tool_name}")
                fail_streaks[tool_name] = fail_streaks.get(tool_name, 0) + 1
                if fail_streaks[tool_name] >= _SAME_TOOL_FAIL_CAP:
                    return _terminate(
                        f"tool '{tool_name}' failed {_SAME_TOOL_FAIL_CAP} times "
                        f"in a row: {tool_errors.get(tool_name) or 'repeated a call that already failed'}",
                        iteration)
                continue
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
                result = _execute_mcp_tool(mcp_entry, params, config,
                                           vault=vault, session_id=execution_id)
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
                failed_sigs.add(sig)
                tool_errors[tool_name] = str(error)
                if fail_streaks[tool_name] >= _SAME_TOOL_FAIL_CAP:
                    return _terminate(
                        f"tool '{tool_name}' failed {_SAME_TOOL_FAIL_CAP} "
                        f"times in a row: {error}", iteration)
            continue

        # Malformed action.
        malformed_streak += 1
        history.append({
            "role": "tool_result", "success": False,
            "error": ("Your reply was not one of the three valid JSON actions "
                      "(TOOL_CALL / ANSWER / ASK_USER). Reply with exactly one."),
        })
        if malformed_streak >= _MALFORMED_CAP:
            return _terminate(
                "the model returned malformed actions twice in a row", iteration)

    return _terminate(
        f"iteration budget exhausted ({max_iters}) without an answer", iteration)


def _safety_denied(tool_name: str, params: Dict[str, Any]) -> "str | None":
    """Quick-lane destructive gate (v0.9.32, D-3).

    Shell tools (run_command / run_cli_command) are now gated at the
    ToolSandbox chokepoint with block-and-ask (the operator is present in
    chat), so this returns None for them — the sandbox raises
    PendingOperatorDecision and _execute_tool block-polls the choice.

    NON-shell destructive tools have no inline approval surface in this lane,
    so they keep the actionable auto-deny (the model adapts or ASK_USERs).
    Returns the denial message, or None to allow / defer to the sandbox gate.
    """
    try:
        from systemu.runtime.tool_sandbox import ToolSandbox, _SHELL_TOOL_NAMES
        if tool_name in _SHELL_TOOL_NAMES:
            return None  # gated at the sandbox (block-and-ask)
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


def _poll_command_choice(vault, dedup_key: str, timeout: "float | None" = None):
    """Block-poll the OperatorDecisionQueue for a command gate's resolved
    choice (v0.9.32, D-3). Returns the choice string, or None on timeout
    (caller treats None as Deny — fail-closed)."""
    import time
    from systemu.approval.decision_queue import OperatorDecisionQueue
    deadline = time.monotonic() + (timeout if timeout is not None else 300.0)
    q = OperatorDecisionQueue(vault)
    while time.monotonic() < deadline:
        choice = q.get_resolved_choice(dedup_key)
        if choice is not None:
            return choice
        time.sleep(1.0)
    return None


def _ask_operator_inline(vault, question: str, *, dedup_key: str,
                         cancel_event=None, timeout: float = 600.0):
    """v0.9.43: post the agent's ASK_USER question as an answerable operator
    decision and BLOCK-POLL for the typed answer.

    Mirrors ``_poll_command_choice``'s wait strategy: the chat lane runs on a
    daemon thread that simply blocks, so the ReAct loop's state is preserved on
    the stack (no snapshot/Supervisor needed). The decision is a single
    free-text ``structured_question`` — the exact shape render_decision_card
    needs to draw a free-text box — so it surfaces as an answerable card in the
    "Needs you" rail. No ``chat_submission_id`` is set, so the Supervisor resume
    paths (resume_on_decision / reconcile) stay provably inert.

    Returns the operator's answer string, or None when the operator declines /
    cancels (cancel_event) / the wait times out — the caller then falls back to
    the honest needs_input terminal.
    """
    import json as _json
    import time as _time
    from systemu.approval.decision_queue import OperatorDecisionQueue

    queue = OperatorDecisionQueue(vault)
    if queue.get_resolved_choice(dedup_key) is None:
        try:
            queue.post(
                title=(question or "Input needed")[:80],
                body="Answer to continue.",
                options=["Submit"],   # placeholder; the free-text box is the input
                context={
                    "kind": "structured_question",
                    "questions": [{"id": "answer", "prompt": question,
                                   "options": [], "allow_free_text": True}],
                    "quick_ask": True,
                },
                dedup_key=dedup_key,
            )
        except Exception:
            logger.exception("[QuickTask] could not post ASK_USER decision")
            return None

    def _abandon():
        # timeout / cancel: the decision is still PENDING — expire it so an
        # abandoned question does not linger in the "Needs you" rail/badge.
        # (A declined ask is already RESOLVED by the rail ×, so this no-ops it.)
        try:
            queue.expire_by_dedup_key(dedup_key)
        except Exception:
            logger.debug("[QuickTask] could not expire abandoned ask",
                         exc_info=True)

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            _abandon()
            return None
        choice = queue.get_resolved_choice(dedup_key)
        if choice is not None:
            try:
                data = _json.loads(choice)
                val = data.get("answer") if isinstance(data, dict) else None
            except Exception:
                val = choice
            if val is None:
                return None
            return str(val).strip() or None   # whitespace-only -> decline (None)
        _time.sleep(1.0)
    _abandon()
    return None


def _execute_tool(sandbox, tool, params: Dict[str, Any]):
    """Execute one tool through the EXACT runtime contract (W6 runner,
    subprocess isolation policy, dependency install). Sync wrapper — the
    quick lane runs in a worker thread.

    v0.9.32 (D-3): if the sandbox raises PendingOperatorDecision (a command
    gate), block inline until the operator resolves it in the dashboard Inbox.
    Approve once / Always allow → re-attempt; Deny or timeout → fail-closed
    denial result. ONE gate, two wait strategies (workflow parks; chat blocks).
    """
    from systemu.core.llm_router import _run_coroutine
    from systemu.runtime.tool_sandbox import requires_subprocess_isolation
    from systemu.approval.exceptions import PendingOperatorDecision
    from types import SimpleNamespace as _NS

    def _call(resolved_dedup=None):
        return _run_coroutine(sandbox.execute_tool(
            tool.implementation_path,
            params,
            extra_packages=tool.dependencies or [],
            tool_type=getattr(tool.tool_type, "value", tool.tool_type),
            force_subprocess=requires_subprocess_isolation(tool),
            _command_gate_resolved=resolved_dedup,
            tool=tool,   # S1b: thread the Tool so the sandbox action gate can score it
        ))

    def _denied(msg: str):
        return _NS(success=False, error=msg,
                   parsed={"success": False, "error": msg,
                           "error_type": "command_denied"})

    try:
        return _call()
    except PendingOperatorDecision as pend:
        vault = getattr(sandbox, "_vault", None)
        choice = _poll_command_choice(vault, pend.dedup_key)
        norm = (choice or "").strip().lower()
        if norm in ("approve once", "always allow"):
            # The decision is resolved; "Always allow" persisted the signature
            # (D.3 handler) so the re-attempt's sandbox gate passes. "Approve
            # once" is honored by the one-shot bypass token (resolved_dedup),
            # which lets _maybe_gate_command skip THIS call without persisting.
            try:
                return _call(resolved_dedup=pend.dedup_key)
            except PendingOperatorDecision:
                # Re-gated (the approval on record did not cover THIS call) → fail-closed.
                #
                # IMPL-2: this branch is now the ordinary outcome of the resolved-dedup
                # SCOPE refusal — the poll found a resolved choice belonging to a
                # different card on the same params-independent dedup key, and the gate
                # declined it. The old copy told the operator to "Choose 'Always allow'",
                # which is both unavailable on a reclassified follow-up card and exactly
                # the grant that must never be minted under a one-shot classification.
                # Point them at the card for THIS call instead.
                return _denied("Approved once, but the gate asked again — nothing ran. "
                               "The approval on record did not cover this exact call. "
                               "Open the Inbox and approve the card that names these "
                               "arguments.")
            except Exception:
                # v0.9.32 (D.4 review FIX-5): any unexpected error on the
                # re-attempt must fail-closed too, never crash the lane.
                logger.exception("[QuickTask] command gate re-attempt errored "
                                 "— fail-closed denial")
                return _denied("Command approval re-attempt failed — denied "
                               "(fail-closed).")
        # IMPL-2: a reclassify is neither an approval nor a denial — the operator
        # supplied the effect class and the gate must now re-score on it. It authorizes
        # NOTHING by itself (a fresh card is posted on the new classification), so this
        # stays fail-closed for THIS attempt. It gets its own message because the
        # timeout copy below would be simply untrue: nothing timed out, and the operator
        # did act.
        if norm.startswith("reclassify"):
            # State only what is TRUE of this attempt. The earlier copy promised "the
            # follow-up card" — but this attempt is ABANDONED here, and whether a card
            # ever appears depends on the ReAct loop choosing to retry, which this lane
            # does not control. Telling the operator to go approve a card that may not
            # exist sends them to wait on nothing.
            return _denied("Effect reclassification recorded. This attempt did not "
                           "run; re-running the task re-checks this call on the new "
                           "classification and asks you to approve it.")
        # Deny or timeout → fail-closed.
        msg = ("Command denied by operator." if norm == "deny"
               else "Command approval timed out — denied (fail-closed).")
        return _denied(msg)
    except Exception:
        # v0.9.32 (D.4 review FIX-5): a NON-Pending exception from the dispatch
        # (e.g. _maybe_gate_command's inbox enqueue against an unusable vault)
        # used to propagate and crash run_quick_task. Fail-closed instead: the
        # destructive command did NOT run, and the lane returns a clean denial
        # (same shape as the timeout/Deny branch) so the ReAct loop adapts.
        logger.exception("[QuickTask] tool dispatch raised an unexpected error "
                         "— fail-closed denial")
        return _denied("Tool dispatch failed unexpectedly — denied "
                       "(fail-closed).")


def promote_to_workflow(prompt: str, config, vault):
    """Promote a one-shot ask into the factory pipeline WITHOUT executing it.

    Creates the refined scroll via the normal Stage-1 entry (it lands in
    /work, where the existing approval → extraction → shadow flow takes
    over). Deliberately does NOT extract, decide, or execute — promotion is
    "make this repeatable", not "run it again". Returns the Scroll.
    """
    from systemu.pipelines.scroll_refiner import refine_from_text
    return refine_from_text(prompt, vault, config)


def submit_quick_task(prompt: str, config, vault, *, chat_ts: Optional[str] = None,
                      extra: Optional[Dict[str, Any]] = None,
                      **kwargs) -> QuickResult:
    """Chat-facing wrapper: run the quick lane AND keep the chat-history
    contract (same fields direct_task writes), so the Status dropdown, the
    chat thread, and the fact-extraction hook work unchanged.

    v0.9.32 fix: ``chat_ts`` (when the chat lane supplies it) is the canonical id
    under which the caller registered the cancel Event in ``chat_task_registry``.
    Used VERBATIM as the chat-history entry id so the per-entry Stop button
    (``request_cancel(entry["ts"])``) matches the registry key. When None
    (non-chat callers) the ts is generated internally as before.

    R-UTL1: ``extra`` carries additive provenance (``origin``/``source``/
    ``project_id``) from ``submit_chat_task`` into the row this creates. Merged
    on creation only; the terminal ``update_chat_history_entry`` merges into the
    same row, so the fields survive. Default None = unchanged behaviour.
    """
    from datetime import datetime

    ts = chat_ts or datetime.now().isoformat(timespec="seconds")
    try:
        vault.append_chat_history({
            "ts": ts, "prompt": prompt, "status": "running", "lane": "quick",
            **(extra or {}),
        })
    except Exception:
        logger.debug("[QuickTask] could not append chat history", exc_info=True)

    # v0.9.32: cancel_event (if the caller registered one in chat_task_registry)
    # rides through **kwargs into run_quick_task's loop-top cancel check.
    # v0.9.43: a chat_ts means a live operator chat surface — let an ASK_USER
    # PARK on an answerable decision and resume, instead of the needs_input
    # dead-end. Non-chat callers (no chat_ts) keep the terminal.
    kwargs.setdefault("chat_surface", chat_ts is not None)
    result = run_quick_task(prompt, config, vault, **kwargs)

    # The status written is result.status verbatim. v0.9.43: a chat-surface
    # ASK_USER now PARKS on an answerable decision and resumes inline (see
    # _ask_operator_inline), so a chat task reaches a real terminal
    # (success/partial/cancelled) — a residual "needs_input" only happens when
    # the operator declined or the wait timed out (that decision is already
    # expired/resolved), so it stays a plain terminal status here with no remap.
    status = result.status
    try:
        vault.update_chat_history_entry(ts, {
            "status": status,
            "summary": result.answer_md,
            "error": result.error,
            "files_produced": list(result.files_produced),
            "lane": "quick",
            # R-P3a: persist the run's DURABLE cost rows onto the chat entry so the
            # quick-lane per-run cost chip survives a reload (the live ledger is
            # in-process only) + the run's execution_id for the exec-id meta.
            "execution_id": getattr(result, "execution_id", "") or "",
            "cost": list(getattr(result, "cost", []) or []),
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

    # R-UTL1 / U-12: the no-UI Outbox contract. The quick lane is the DEFAULT
    # lane and never touches ExecutionContext, so hooking only the workflow
    # lane's build_result would silently miss most runs.
    try:
        from systemu.runtime.outbox import write_outbox_for_run
        write_outbox_for_run(
            vault, task_id=ts, prompt=prompt, status=result.status,
            summary=result.answer_md or "",
            files_produced=list(result.files_produced),
            execution_id=getattr(result, "execution_id", None))
    except Exception:
        logger.debug("[QuickTask] outbox hook skipped", exc_info=True)
    return result
