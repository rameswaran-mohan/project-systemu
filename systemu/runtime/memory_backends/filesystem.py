"""filesystem backend — lifts existing JSONL + SHADOW_MEMORY.md
logic from vault.load_shadow_memory() into the BaseMemoryBackend interface."""
from __future__ import annotations
import json
from pathlib import Path

from .base import BaseMemoryBackend


class FilesystemMemoryBackend(BaseMemoryBackend):
    """JSONL buffer + SHADOW_MEMORY.md consolidated, in the layout that
    matched the pre-vault.  Default backend."""

    def __init__(self, memory_root: Path):
        self._root = Path(memory_root)

    def _shadow_dir(self, shadow_id: str) -> Path:
        p = self._root / shadow_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def load_buffer(self, shadow_id: str) -> list:
        buf = self._shadow_dir(shadow_id) / "memory_buffer.jsonl"
        if not buf.exists():
            return []
        out = []
        for line in buf.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # silently skip malformed lines; the buffer is append-only and a
                # partial write should not break the whole load.
                continue
        return out

    def append_buffer(self, shadow_id: str, entry: dict) -> None:
        buf = self._shadow_dir(shadow_id) / "memory_buffer.jsonl"
        with buf.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def load_consolidated(self, shadow_id: str) -> str:
        md = self._shadow_dir(shadow_id) / "SHADOW_MEMORY.md"
        return md.read_text("utf-8") if md.exists() else ""

    def save_consolidated(self, shadow_id: str, md: str) -> None:
        (self._shadow_dir(shadow_id) / "SHADOW_MEMORY.md").write_text(
            md, encoding="utf-8",
        )
