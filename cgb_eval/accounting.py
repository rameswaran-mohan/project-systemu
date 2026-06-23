"""Per-trial token/call ledger via a single seam: ``systemu.core.llm_router.llm_call``.

Same mechanism the v0.9.7 release-notes cost comparison used.  All systemu LLM
paths funnel through this module-level async function (``llm_call_json`` calls it
internally), so one attribute patch captures every call's token usage.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, List

import systemu.core.llm_router as llm_router


@dataclass
class TokenLedger:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    per_call: List[dict] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record(self, resp: dict) -> None:
        if not isinstance(resp, dict):
            return
        self.calls += 1
        self.input_tokens += int(resp.get("input_tokens", 0) or 0)
        self.output_tokens += int(resp.get("output_tokens", 0) or 0)
        self.per_call.append({
            "tier": resp.get("tier"), "model": resp.get("model"),
            "in": resp.get("input_tokens"), "out": resp.get("output_tokens"),
            "latency_ms": resp.get("latency_ms"),
        })


@contextmanager
def patched_accounting(ledger: TokenLedger) -> Iterator[None]:
    """Wrap ``llm_router.llm_call`` so every call's usage is recorded on ``ledger``."""
    original = llm_router.llm_call

    async def counting_llm_call(*args, **kwargs):
        resp = await original(*args, **kwargs)
        ledger.record(resp)
        return resp

    llm_router.llm_call = counting_llm_call
    try:
        yield
    finally:
        llm_router.llm_call = original
