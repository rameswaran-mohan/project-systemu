"""PACKET F / F12 — structured-output requests must not fail silently.

Live evidence that motivated this file (real OpenRouter call, default tier-1
model ``deepseek/deepseek-v4-flash``, the exact prompt + budget
``episodic_memory.capture`` ships with, max_tokens=400)::

    ORIGINAL  finish_reason='length'  completion_tokens=401  reasoning_tokens=357
              content_len=146 -> '{\\n  "outcome_summary": "Successfully wrote ...
              parses as JSON: NO (Unterminated string ...)
    REPAIR    finish_reason='length'  completion_tokens=400  reasoning_tokens=400
              content_len=0
              parses as JSON: NO

i.e. the model was NOT failing to follow instructions — the completion budget
was consumed by reasoning tokens and the JSON was cut off mid-string.  The
router never looked at ``finish_reason`` (zero occurrences in the package
before this change), so a hard truncation was indistinguishable from prose, and
the whole thing surfaced only as a ``logger.warning`` no operator ever reads.

PROPERTIES pinned here
----------------------
P1  Every structured-output request (any tier, any provider) either returns a
    parsed dict or produces an operator-visible WARNING event.  Never
    "developer-only log line + silent degrade".

P2  The JSON repair/retry path is granted AT LEAST the completion budget the
    original request had, so a repair can never fail for a budget reason the
    original would not have.

These are runtime pins on the real production call path (they assert on the
actual kwargs handed to the provider client and on the real
``notifications.log_event`` seam), so deleting the fence turns them red.
"""
from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, patch

import pytest

import systemu.core.llm_router as R
from sharing_on.config import Config


# ── helpers ──────────────────────────────────────────────────────────────────

def _resp(content: str, finish_reason: str = "stop"):
    """A minimal OpenAI-shape chat completion (SimpleNamespace, not AsyncMock —
    AsyncMock auto-creates truthy attributes and would mask missing fields)."""
    msg = types.SimpleNamespace(content=content, reasoning_details=[])
    choice = types.SimpleNamespace(message=msg, finish_reason=finish_reason)
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    return types.SimpleNamespace(choices=[choice], usage=usage)


class _Recorder:
    """Serves a scripted list of responses and records every call's kwargs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]

    @property
    def budgets(self):
        return [c.get("max_tokens") for c in self.calls]


def _wire(mock_async_openai, responses):
    rec = _Recorder(responses)
    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=rec.create))
    )
    ctx = AsyncMock()
    ctx.__aenter__.return_value = client
    ctx.__aexit__.return_value = False
    mock_async_openai.return_value = ctx
    return rec


@pytest.fixture
def cfg():
    c = Config.from_env()
    c.openrouter_api_key = "dummy_key"
    c.tier1_model = "test/tier1"
    c.tier2_model = "test/tier2"
    c.tier3_model = "test/tier3"
    return c


@pytest.fixture(autouse=True)
def _reset_client():
    R._client = None
    yield
    R._client = None


# The exact shape the live run produced: valid JSON prefix, cut off mid-string.
_TRUNCATED = '{\n  "outcome_summary": "Successfully wrote a three-line haiku about ra'
_GOOD = '{"outcome_summary": "ok", "key_facts_learned": [], "tags": ["x"]}'


# ── F1: finish_reason is plumbed out of BOTH provider paths ──────────────────

@pytest.mark.asyncio
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_openai_shape_path_reports_finish_reason(mock_openai, cfg):
    """A truncated completion must be REPORTABLE as truncated, not just short."""
    _wire(mock_openai, [_resp(_TRUNCATED, finish_reason="length")])
    out = await R.llm_call(tier=1, system="s", user="u", config=cfg)
    assert out["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_native_provider_path_reports_finish_reason(cfg):
    """Anthropic ``stop_reason`` / Ollama ``done_reason`` normalize to the same
    field, so the truncation fence covers native providers too (GATE-7a
    corollary: do not widen only one gate of a multi-surface fact)."""
    from systemu.llm.providers.base import LLMResponse

    class _Prov:
        async def call(self, **_kw):
            return LLMResponse(
                content=_TRUNCATED, model="claude-x", usage={"input": 1, "output": 2},
                raw=types.SimpleNamespace(stop_reason="max_tokens"),
            )

    with patch.object(R, "_get_provider", lambda *_a, **_k: _Prov()):
        out = await R._llm_call_via_provider(
            cfg, 1, "claude-x", [], response_format={"type": "json_object"},
            temperature=0.2, max_tokens=400, t0=0.0)
    assert out["finish_reason"] == "length", out


# ── P2: the repair is never granted less budget than the original ────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [64, 400, 1024, 2048, 8192, 20000])
@pytest.mark.parametrize("first_finish", ["length", "stop"])
@patch("systemu.interface.notifications.log_event")
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_repair_budget_is_never_below_the_original(
    mock_openai, _log_event, cfg, requested, first_finish
):
    """P2 quantified over the whole class: for EVERY structured-output request
    and EVERY reason the first attempt failed, every follow-up call the router
    makes carries a completion budget >= the first call's."""
    rec = _wire(mock_openai, [_resp("not json at all", finish_reason=first_finish)] * 6)
    with pytest.raises(ValueError):
        await R.async_llm_call_json(
            tier=1, system="s", user="u", config=cfg, max_tokens=requested)

    assert len(rec.calls) >= 2, f"expected a repair attempt, got {rec.budgets}"
    first = rec.budgets[0]
    assert first is not None
    for i, b in enumerate(rec.budgets[1:], start=1):
        assert b >= first, (
            f"call #{i} was granted max_tokens={b}, BELOW the original {first} "
            f"— a repair must never fail for a budget reason the original "
            f"would not have. budgets={rec.budgets}")


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [64, 400, 1024])
@patch("systemu.interface.notifications.log_event")
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_after_a_budget_truncation_every_retry_gets_MORE_budget(
    mock_openai, _log_event, cfg, requested
):
    """The sharp edge of P2.  ``finish_reason='length'`` means the budget WAS
    the failure; re-running at the same budget is re-running a configuration
    already proven insufficient.  Live: the shipped repair reused max_tokens=400
    and burned all 400 on reasoning tokens, returning 0 characters."""
    rec = _wire(mock_openai, [_resp(_TRUNCATED, finish_reason="length")] * 6)
    with pytest.raises(ValueError):
        await R.async_llm_call_json(
            tier=1, system="s", user="u", config=cfg, max_tokens=requested)

    first = rec.budgets[0]
    assert len(rec.calls) >= 2, f"expected a retry, got {rec.budgets}"
    assert all(b > first for b in rec.budgets[1:]), (
        f"after a finish_reason='length' truncation the router retried at "
        f"budgets={rec.budgets} — the first budget {first} is the one that "
        f"already failed")


@pytest.mark.asyncio
@patch("systemu.interface.notifications.log_event")
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_truncated_first_attempt_is_retried_with_a_larger_budget(
    mock_openai, _log_event, cfg
):
    """The live failure exactly: budget too small for a reasoning model, JSON cut
    off mid-string.  The router must widen the budget and recover instead of
    re-running the identical under-budgeted call."""
    rec = _wire(mock_openai, [
        _resp(_TRUNCATED, finish_reason="length"),   # as observed live
        _resp(_GOOD, finish_reason="stop"),          # succeeds once given room
    ])
    out = await R.async_llm_call_json(
        tier=1, system="s", user="u", config=cfg, max_tokens=400)

    assert out["outcome_summary"] == "ok"
    assert rec.budgets[1] > rec.budgets[0], (
        f"retry after a finish_reason='length' truncation reused budget "
        f"{rec.budgets}; the budget WAS the failure")


@pytest.mark.asyncio
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_json_requests_get_a_floor_budget(mock_openai, cfg):
    """A structured-output request may not be dispatched under a budget a
    reasoning model burns entirely on reasoning tokens (357 of 400 observed
    live).  max_tokens is a CAP, not a spend — raising it costs nothing unused."""
    rec = _wire(mock_openai, [_resp(_GOOD)])
    await R.async_llm_call_json(tier=1, system="s", user="u", config=cfg, max_tokens=400)
    assert rec.budgets[0] >= 2048, (
        f"structured-output request dispatched at max_tokens={rec.budgets[0]}")


# ── P1: failure is operator-visible, not developer-log-only ──────────────────

@pytest.mark.asyncio
@patch("systemu.interface.notifications.log_event")
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_structured_output_failure_reaches_the_operator(mock_openai, log_event, cfg):
    """P1 witness at the router — the single choke point all 42 structured-output
    call sites pass through."""
    _wire(mock_openai, [_resp(_TRUNCATED, finish_reason="length")] * 6)
    with pytest.raises(ValueError):
        await R.async_llm_call_json(
            tier=1, system="s", user="u", config=cfg, max_tokens=400)

    assert log_event.called, (
        "a structured-output request produced NOTHING usable and the operator "
        "was never told — only a logger.warning a developer would have to read")
    lvl, cat, msg = log_event.call_args.args[:3]
    ctx = log_event.call_args.args[3] if len(log_event.call_args.args) > 3 else {}
    assert lvl == "WARNING"
    assert cat == "llm"
    assert ctx.get("kind") == "structured_output_failed"
    assert ctx.get("tier") == 1
    assert "test/tier1" in msg
    # the diagnosis must name the real cause, not blame the model
    assert "truncat" in msg.lower() or "budget" in msg.lower() or ctx.get("truncated") is True
    # DEC-32c: a verdict that mojibakes on the operator's console is a broken
    # verdict. The message crosses into event_log.jsonl and the dashboard.
    assert msg.isascii(), repr(msg)


@pytest.mark.asyncio
@patch("systemu.interface.notifications.log_event")
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_raised_error_names_truncation_not_model_stupidity(mock_openai, _le, cfg):
    _wire(mock_openai, [_resp(_TRUNCATED, finish_reason="length")] * 6)
    with pytest.raises(ValueError) as ei:
        await R.async_llm_call_json(
            tier=1, system="s", user="u", config=cfg, max_tokens=400)
    assert "truncat" in str(ei.value).lower(), str(ei.value)
    assert str(ei.value).isascii(), repr(str(ei.value))


def test_episodic_degradation_reaches_the_operator(tmp_path):
    """P1 witness at the FEATURE surface.  'A tier-1 JSON call failed' does not
    tell an operator that cross-session recall silently recorded nothing — the
    advertised capability has to name itself (DEC-34: no false assertion of
    capability)."""
    from systemu.runtime import episodic_memory as EM

    class _Vault:
        def query_session_summaries(self, limit=None):
            return []

    cfg = types.SimpleNamespace(
        episodic_memory_enabled=True, openrouter_api_key="k",
        episodic_summary_max_chars=800, episodic_tags_max_count=8)

    with patch.object(EM, "llm_call_json", side_effect=ValueError("boom")), \
         patch("systemu.interface.notifications.log_event") as log_event:
        out = EM.capture(
            vault=_Vault(), session_id="s1", intent="i", chat_result="r",
            files_produced=[], status="success", config=cfg)

    assert out is None
    assert log_event.called, (
        "episodic memory produced no summary and the operator was never told — "
        "the task still reports SUCCESS")
    lvl, cat, msg = log_event.call_args.args[:3]
    ctx = log_event.call_args.args[3] if len(log_event.call_args.args) > 3 else {}
    assert lvl == "WARNING"
    assert ctx.get("kind") == "episodic_degraded"
    assert "recall" in msg.lower() or "episodic" in msg.lower() or "memory" in msg.lower()
    assert msg.isascii(), repr(msg)


def test_no_new_structured_output_caller_bypasses_the_router():
    """The fences above all live in ``async_llm_call_json``. They protect a call
    site ONLY if that call site goes through the router — a module that builds
    ``response_format={"type": "json_object"}`` against its own client inherits
    none of them.

    Committed violation-identity baseline (DEC-32c): both mismatch directions
    are fatal. A NEW bypass fails here and forces an explicit decision instead
    of quietly shipping a 43rd call site with the F12 defect; REMOVING the known
    bypass also fails here, so the ledger cannot go stale.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pat = re.compile(r"""response_format\s*=\s*\{\s*["']type["']""")
    found = set()
    for pkg in ("systemu", "sharing_on"):
        for py in (root / pkg).rglob("*.py"):
            if pat.search(py.read_text(encoding="utf-8", errors="replace")):
                found.add(py.relative_to(root).as_posix())

    KNOWN = {
        # THE fence itself — this is the router.
        "systemu/core/llm_router.py",
        # Pre-existing bypass, filed not fixed: the recorder-side analyzer owns
        # its own OpenAI client (max_tokens=1024, no finish_reason check), so a
        # truncation there is misreported as `parse_error` in intent.json.
        # It degrades VISIBLY (confidence="low" + error), which is why it is not
        # in this packet — but it is the same blindness.
        "sharing_on/analyzer/intent_extractor.py",
    }
    assert found == KNOWN, (
        f"structured-output callers changed.\n"
        f"  new bypasses (route them through llm_call_json): {sorted(found - KNOWN)}\n"
        f"  gone (update this baseline):                     {sorted(KNOWN - found)}")


def test_episodic_capture_raising_also_reaches_the_operator():
    """GATE-7a corollary — the fact 'cross-session recall did not record this
    session' has TWO gates: capture() returning None, and capture() RAISING.
    ``shadow_runtime._trigger_episodic_capture`` swallowed the second into a
    logger.warning, which would leave a half-fixed contradiction: one failure
    mode visible to the operator, an adjacent one not."""
    from systemu.runtime import shadow_runtime as SR

    cfg = types.SimpleNamespace(summarize_after_run=True, episodic_memory_enabled=True)

    with patch("systemu.runtime.episodic_memory.capture", side_effect=RuntimeError("vault down")), \
         patch("systemu.interface.notifications.log_event") as log_event:
        SR._trigger_episodic_capture(
            vault=object(), config=cfg, session_id="s3", intent="i",
            chat_result="r", files_produced=[], status="success")

    kinds = [
        (c.args[3] or {}).get("kind")
        for c in log_event.call_args_list if len(c.args) > 3
    ]
    assert "episodic_degraded" in kinds, (
        f"capture() raised and the operator was never told; events={kinds}")


def test_episodic_success_does_not_warn(tmp_path):
    """The degraded-notice must not fire on the happy path (a warning that
    always fires teaches the operator to ignore it)."""
    from systemu.runtime import episodic_memory as EM

    stored = []

    class _Vault:
        def query_session_summaries(self, limit=None):
            return []

        def store_session_summary(self, s):
            stored.append(s)

    cfg = types.SimpleNamespace(
        episodic_memory_enabled=True, openrouter_api_key="k",
        episodic_summary_max_chars=800, episodic_tags_max_count=8)

    good = json.loads(_GOOD)
    with patch.object(EM, "llm_call_json", return_value=good), \
         patch("systemu.interface.notifications.log_event") as log_event:
        EM.capture(vault=_Vault(), session_id="s2", intent="i", chat_result="r",
                   files_produced=[], status="success", config=cfg)

    kinds = [
        (c.args[3] or {}).get("kind")
        for c in log_event.call_args_list if len(c.args) > 3
    ]
    assert "episodic_degraded" not in kinds, kinds
