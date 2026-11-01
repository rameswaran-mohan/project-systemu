"""v0.8.16: per-execution LLM transcript (raw completions for lazy UI expand)."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, Optional

_MAX_ENTRY_CHARS = 20000


def _path(vault_root, exec_id: str) -> Path:
    return Path(vault_root) / "executions" / exec_id / "llm_transcript.jsonl"


def _masked(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The entry as it may be persisted. DEC-31: every string is MASKED and only
    then capped to ``_MAX_ENTRY_CHARS`` -- the prompts quote the tool calls'
    parameters and results, and the response IS the model's decision, so a
    raw slice here was a raw parameter on disk. A string that holds a JSON
    document (the user payload, the decision) is masked as the structure it is,
    so a secret-NAMED key inside it is honoured too."""
    from systemu.runtime.durable_redact import mask_text, mask_values
    return {k: (mask_text(v, max_len=_MAX_ENTRY_CHARS, structured=True)
                if isinstance(v, str) else mask_values(v))
            for k, v in entry.items()}


def append_call(vault_root, exec_id: str, entry: Dict[str, Any]) -> int:
    """Append a call entry; return its 0-based index. Best-effort, never raises."""
    p = _path(vault_root, exec_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        idx = 0
        if p.exists():
            with p.open(encoding="utf-8") as f:
                idx = sum(1 for line in f if line.strip())
        trimmed = _masked(entry)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trimmed) + "\n")
        return idx
    except Exception:
        return -1


def read_call(vault_root, exec_id: str, index: int) -> Optional[Dict[str, Any]]:
    """Return the index-th call entry, or None."""
    p = _path(vault_root, exec_id)
    try:
        if not p.exists():
            return None
        with p.open(encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        if 0 <= index < len(lines):
            return json.loads(lines[index])
    except Exception:
        pass
    return None
