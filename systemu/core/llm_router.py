"""Tiered LLM router — dispatches calls to the correct OpenRouter model.

Tier 1  ->  Deep reasoning  (scroll refinement, shadow decisions, evolution)
Tier 2  ->  Structured/code (tool forge, execution planning)
Tier 3  ->  Fast/cheap      (log->instructions formatting)

All calls go through OpenRouter via the openai-compatible client.

Usage:
  - llm_call_json(...)        -- synchronous, works from CLI or NiceGUI callbacks
  - async_llm_call_json(...)  -- async, for use inside async functions / coroutines
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from sharing_on.config import Config

logger = logging.getLogger(__name__)

# API call timeout in seconds.  120 s covers slow free-tier models while still
# surfacing a clear TimeoutError instead of hanging silently forever.
_API_TIMEOUT_SECONDS = 120.0

# Network retry config (A.1): cap at 2 retries with exponential back-off.
# Only transient failures (timeout, connection reset) are retried; model errors
# (bad JSON, 400 schema rejection) are not — those go straight to the repair path.
_NETWORK_MAX_RETRIES  = 2
_NETWORK_BACKOFF_S    = [5.0, 15.0]

# Google AI Studio OpenAI-compatible endpoint
_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _is_network_retriable(exc: BaseException) -> bool:
    """Return True for transient network / timeout errors that warrant a retry."""
    for candidate in (exc, getattr(exc, "__cause__", None)):
        if candidate is None:
            continue
        if isinstance(candidate, (asyncio.TimeoutError, TimeoutError)):
            return True
        cls_name = type(candidate).__name__
        if cls_name in ("APITimeoutError", "APIConnectionError"):
            return True
        err_lower = str(candidate).lower()
        if any(kw in err_lower for kw in ("timeout", "timed out", "connection", "connect error", "network")):
            return True
    return False


def _get_client(config: Config, tier: int = 0) -> AsyncOpenAI:
    """Return a new AsyncOpenAI client bound to the current event loop.

    v0.7-e: dispatch via the provider registry instead of inline model-name
    checks.  The function still returns an AsyncOpenAI client so existing
    call paths that consume ``resp.choices[0].message.content`` keep
    working; providers with non-OpenAI shape (Anthropic, Ollama) are
    routable via the parallel ``_get_provider()`` entry point — the
    full LLMResponse-shape migration of llm_call/llm_call_json is
    deferred to v0.7.1.  Operators forcing a non-OpenAI-shape provider
    via SYSTEMU_TIER{N}_PROVIDER through this code path will see the
    request degrade to OpenRouter (the catch-all) so live calls do
    not break.

    Historical context (v0.6.8-h): the prior logic routed tier-1/2 to
    Google whenever a google key was set, regardless of model.  After
    v0.6.7 pinned tiers to deepseek/deepseek-v4-flash, that misroute
    sent deepseek requests to Google and 400'd with "API key not
    valid" because the OpenRouter model name isn't a valid Google
    model.  The registry-based dispatch below is now the single source
    of truth for "which provider serves which model."

    We intentionally do NOT cache this globally.  AsyncOpenAI wraps
    httpx.AsyncClient whose connection pool is event-loop-bound.
    _run_coroutine() creates a fresh event loop in a new thread on every call;
    reusing a client that was created in a different (possibly closed) loop
    causes httpx to enqueue I/O on a dead loop and hang indefinitely.
    """
    from systemu.llm.providers import resolve_provider_class
    from systemu.llm.providers.google import GoogleProvider
    from systemu.llm.providers.openai import OpenAIProvider

    model = _model_for_tier(tier, config) if tier in (1, 2, 3) else ""
    override = getattr(config, f"tier{tier}_provider", "") if tier in (1, 2, 3) else ""
    provider_cls = resolve_provider_class(model, override_name=override or None)

    # AsyncOpenAI-compatible providers — return the underlying client as before
    if provider_cls is GoogleProvider:
        return AsyncOpenAI(
            api_key=config.google_api_key,
            base_url=_GOOGLE_BASE_URL,
            timeout=_API_TIMEOUT_SECONDS,
        )
    if provider_cls is OpenAIProvider:
        import os as _os
        return AsyncOpenAI(
            api_key=_os.environ.get("OPENAI_API_KEY", config.openrouter_api_key),
            timeout=_API_TIMEOUT_SECONDS,
        )
    # Default (OpenRouter + any other AsyncOpenAI-compat fallback): use the
    # historical OpenRouter path.  Anthropic / Ollama-shape providers should
    # go through _get_provider() instead — see deferred-migration note above.
    return AsyncOpenAI(
        api_key=config.openrouter_api_key,
        base_url=config.openrouter_base_url,
        timeout=_API_TIMEOUT_SECONDS,
    )


def _get_provider(config: Config, tier: int):
    """v0.7-e: return a BaseLLMProvider instance for LLMResponse-shape callers.

    This is the new entry point that supports every registered provider
    (OpenRouter, Google, Anthropic, OpenAI, Ollama).  ``_get_client`` is
    kept for back-compat with the existing AsyncOpenAI call paths in
    ``llm_call``; the actual migration of ``llm_call`` / ``llm_call_json``
    to consume the returned LLMResponse is deferred to v0.7.1.

    Resolution mirrors ``_get_client``: env override via
    ``SYSTEMU_TIER{N}_PROVIDER`` (read into ``config.tier{N}_provider``)
    beats automatic ``matches()``-based detection from the model name.
    """
    import os as _os
    from systemu.llm.providers import resolve_provider_class

    model = _model_for_tier(tier, config) if tier in (1, 2, 3) else ""
    override = getattr(config, f"tier{tier}_provider", "") if tier in (1, 2, 3) else ""
    provider_cls = resolve_provider_class(model, override_name=override or None)

    cls_name = provider_cls.__name__
    if cls_name == "GoogleProvider":
        return provider_cls(api_key=config.google_api_key)
    if cls_name == "OpenRouterProvider":
        return provider_cls(api_key=config.openrouter_api_key,
                            base_url=config.openrouter_base_url)
    if cls_name == "AnthropicProvider":
        return provider_cls(api_key=_os.environ.get("ANTHROPIC_API_KEY", ""))
    if cls_name == "OpenAIProvider":
        return provider_cls(api_key=_os.environ.get("OPENAI_API_KEY", ""))
    if cls_name == "OllamaProvider":
        return provider_cls(base_url=_os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    raise RuntimeError(f"unhandled provider {cls_name}")


def _model_for_tier(tier: int, config: Config) -> str:
    """Return the configured model name for the given tier."""
    mapping = {
        1: config.tier1_model,
        2: config.tier2_model,
        3: config.tier3_model,
    }
    if tier not in mapping:
        raise ValueError(f"Invalid tier {tier!r}. Must be 1, 2, or 3.")
    return mapping[tier]


def _run_coroutine(coro):
    """Run a coroutine safely regardless of the caller's asyncio context.

    Always executes in a fresh thread with its own event loop to avoid
    conflicts with NiceGUI's loop, APScheduler's loop, or any prior loop
    state in CLI subprocesses (e.g. after generate_instructions ran its loop).
    """
    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            try:
                # Drain any tasks that were scheduled during the call
                # (e.g. httpx AsyncClient.aclose()) before closing the loop.
                # Without this, those tasks raise "RuntimeError: Event loop is closed"
                # as background noise in the logs.
                pending = asyncio.all_tasks(loop)
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


def _extract_json(raw_text: str, tier: int) -> Any:
    """Extract a JSON object from raw LLM text.

    Strategy (order matters — reasoning models put the answer at the END):
      1. Strip markdown fences, try direct parse.
      2. Scan END-first: find the last '}' and walk backward to its matching '{'.
         This is the primary path for chain-of-thought models that emit JSON last.
      3. Scan START-first: find all '{...}' candidates, take the one with the
         most keys (covers models that emit JSON first then explain themselves).
      4. Return raw text so the caller can handle the failure explicitly.
    """
    if not raw_text:
        return raw_text

    # 1. Strip markdown fences
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        inner = stripped.split("\n", 1)[-1]
        if "```" in inner:
            inner = inner.rsplit("```", 1)[0]
        stripped = inner.strip()

    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    text = raw_text

    # 2. End-first scan: reasoning models place the answer JSON last.
    last_brace = text.rfind("}")
    if last_brace != -1:
        depth = 0
        for k in range(last_brace, -1, -1):
            if text[k] == "}":
                depth += 1
            elif text[k] == "{":
                depth -= 1
                if depth == 0:
                    candidate = text[k : last_brace + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict) and parsed:
                            logger.debug(
                                "[LLM] Extracted JSON via end-first scan (tier=%d, keys=%s)",
                                tier, list(parsed.keys()),
                            )
                            return parsed
                    except json.JSONDecodeError:
                        pass
                    break

    # 3. Start-first scan: find all top-level '{...}' blocks, keep richest.
    best: Optional[Dict] = None
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[i : j + 1]
                        try:
                            parsed = json.loads(candidate)
                            if isinstance(parsed, dict) and len(parsed) > len(best or {}):
                                best = parsed
                        except json.JSONDecodeError:
                            pass
                        break
        i += 1

    if best:
        logger.debug(
            "[LLM] Extracted JSON via start-first scan (tier=%d, keys=%s)",
            tier, list(best.keys()),
        )
        return best

    logger.warning(
        "[LLM] Could not extract JSON from response (tier=%d, len=%d), returning raw text",
        tier, len(raw_text),
    )
    return raw_text


# ─────────────────────────────────────────────────────────────────────────────
#  Core async implementation
# ─────────────────────────────────────────────────────────────────────────────

async def llm_call(
    tier: int,
    system: str,
    user: str,
    config: Config,
    *,
    response_format: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> Dict[str, Any]:
    """Make a tiered LLM call and return parsed response (async).

    The AsyncOpenAI client is used as an async context manager so that
    httpx.AsyncClient.aclose() is called *inside* the event loop's lifetime —
    before _run_coroutine() calls loop.close().  Without this, CPython's GC
    schedules aclose() as a finalizer after the loop is already dead, flooding
    the logs with "RuntimeError: Event loop is closed" for every API call.
    """
    model = _model_for_tier(tier, config)

    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]

    kwargs: Dict[str, Any] = {
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format
    if tools:
        kwargs["tools"] = tools

    logger.info("[LLM] tier=%d model=%s max_tokens=%d ...", tier, model, max_tokens)
    t0 = time.monotonic()

    # Use async-with so the httpx connection pool is closed deterministically
    # inside this coroutine — GC finalizers never need to touch it.
    async with _get_client(config, tier) as client:
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Some providers reject response_format=json_object -- retry without it
            exc_str = str(exc)
            if response_format and "json" in response_format.get("type", "") and (
                "400" in exc_str or "json" in exc_str.lower() or "not supported" in exc_str.lower()
            ):
                logger.warning("[LLM] JSON mode rejected by provider (%s), retrying without response_format", exc_str[:80])
                fallback_kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                try:
                    resp = await client.chat.completions.create(**fallback_kwargs)
                except Exception as exc2:
                    logger.error("[LLM] API error (fallback): %s", exc2)
                    raise RuntimeError(f"LLM call failed (tier={tier}, model={model}): {exc2}") from exc2
            else:
                logger.error("[LLM] API error: %s", exc)
                raise RuntimeError(f"LLM call failed (tier={tier}, model={model}): {exc}") from exc

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    choice     = resp.choices[0]
    raw_text   = choice.message.content or ""

    # Some models (GLM, o-series) return content inside reasoning_details
    if not raw_text:
        details = getattr(choice.message, "reasoning_details", None) or []
        for d in details:
            if isinstance(d, dict) and d.get("text"):
                raw_text = d["text"].strip()
                break
            elif hasattr(d, "text") and getattr(d, "text", None):
                raw_text = d.text.strip()
                break

    in_tok  = getattr(resp.usage, "prompt_tokens", 0)
    out_tok = getattr(resp.usage, "completion_tokens", 0)

    logger.info(
        "[LLM] done tier=%d model=%s in=%d out=%d latency=%dms",
        tier, model, in_tok, out_tok, elapsed_ms,
    )

    content: Any = raw_text
    if response_format and response_format.get("type") == "json_object":
        content = _extract_json(raw_text, tier)

    return {
        "content":       content,
        "model":         model,
        "tier":          tier,
        "input_tokens":  in_tok,
        "output_tokens": out_tok,
        "latency_ms":    elapsed_ms,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Public API -- async variant (for callers already inside async context)
# ─────────────────────────────────────────────────────────────────────────────

async def async_llm_call_json(
    tier: int,
    system: str,
    user: str,
    config: Config,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Async convenience wrapper -- always requests JSON.

    Returns the parsed JSON dict from the LLM directly.

    Retry strategy (model-agnostic — works with any decent model):
      1. Network retry: transient timeout / connection errors are retried up to
         _NETWORK_MAX_RETRIES times with exponential back-off (5s, 15s).
      2. First call uses response_format=json_object when supported.
      3. If response is prose, a repair call sends the failed output back to
         the model verbatim and asks it to emit ONLY the JSON object it
         described.  This works regardless of schema, tier, or model family —
         the model already knows the answer; it just needs to re-format it.
         Temperature=0.0 for deterministic extraction.
    """
    # v0.9.1 hotfix (pre-existing): callers like extractor.py pass timeout=
    # but llm_call's signature doesn't accept it. Pop it out of kwargs and
    # apply via asyncio.wait_for() so the caller's intent (cap wall-clock)
    # is preserved without breaking llm_call's strict keyword-only API.
    timeout_s = kwargs.pop("timeout", None)

    result: Dict[str, Any] = {}
    for _attempt in range(_NETWORK_MAX_RETRIES + 1):
        try:
            _coro = llm_call(
                tier=tier,
                system=system,
                user=user,
                config=config,
                response_format={"type": "json_object"},
                **kwargs,
            )
            if timeout_s is not None:
                result = await asyncio.wait_for(_coro, timeout=timeout_s)
            else:
                result = await _coro
            break  # success — exit retry loop
        except Exception as exc:
            if _is_network_retriable(exc) and _attempt < _NETWORK_MAX_RETRIES:
                wait_s = _NETWORK_BACKOFF_S[_attempt]
                logger.warning(
                    "[LLM] tier=%d network/timeout error (attempt %d/%d), retrying in %.0fs: %s",
                    tier, _attempt + 1, _NETWORK_MAX_RETRIES + 1, wait_s, exc,
                )
                await asyncio.sleep(wait_s)
            else:
                raise   # non-retriable, or retries exhausted

    content = result["content"]
    if isinstance(content, dict):
        return content

    # First call produced prose — send the failed output back as context so
    # the model knows exactly what it produced and what needs fixing.
    raw_first = str(content)
    logger.warning(
        "[LLM] tier=%d first response was not JSON (len=%d), sending repair prompt",
        tier, len(raw_first),
    )
    repair_user = (
        f"Your previous response was not valid JSON. "
        f"Your response was:\n<previous_response>\n{raw_first}\n</previous_response>\n\n"
        f"Output ONLY the JSON object from your answer above. "
        f"Start with {{ and end with }}. No prose, no explanation."
    )
    retry_kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}
    _retry_coro = llm_call(
        tier=tier,
        system=system,
        user=repair_user,
        config=config,
        response_format={"type": "json_object"},
        temperature=0.0,
        **retry_kwargs,
    )
    if timeout_s is not None:
        retry_result = await asyncio.wait_for(_retry_coro, timeout=timeout_s)
    else:
        retry_result = await _retry_coro
    content = retry_result["content"]
    if isinstance(content, dict):
        logger.info("[LLM] tier=%d JSON repair succeeded", tier)
        return content

    raise ValueError(
        f"LLM (tier={tier}) did not return valid JSON after repair. "
        f"Raw response (first 500 chars): {str(content)[:500]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Public API -- synchronous variant (safe for CLI subprocesses AND NiceGUI)
# ─────────────────────────────────────────────────────────────────────────────

def llm_call_json(
    tier: int,
    system: str,
    user: str,
    config: Config,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Synchronous wrapper for async_llm_call_json.

    Returns the parsed JSON dict from the LLM directly.
    Safe to call from CLI subprocesses and NiceGUI callbacks.
    """
    return _run_coroutine(async_llm_call_json(
        tier=tier,
        system=system,
        user=user,
        config=config,
        **kwargs,
    ))
