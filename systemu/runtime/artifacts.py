"""W8 — artifact collection at the execution boundary.

`files_produced` was always `[]` because nothing tracked what tools wrote
(the tools themselves don't reliably return paths — write_text_file returns
just `{"success": true}`). Rather than editing 41 tool files, this collector
derives candidate paths from a finished call's *params* and *parsed result*
and keeps ONLY files that exist on disk afterwards — so it can't invent
artifacts, only confirm them.

Consumers: the quick lane (8.2), ShadowRuntime's tool-result site and
direct_task's terminal writes (8.4).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Tool-name prefixes whose PARAMS are trusted as artifact candidates.
_PATHY_TOOL_PREFIXES = (
    "write_", "create_", "download_", "compress_", "extract_", "image_",
)

# Keys (in params or parsed payloads) that plausibly carry a produced path.
_PATH_KEYS = (
    "file_path", "path", "output_path", "output_file", "dest",
    "destination", "save_path", "archive_path", "screenshot_path",
)


def _candidates_from(mapping: Any) -> List[str]:
    if not isinstance(mapping, dict):
        return []
    out: List[str] = []
    for key in _PATH_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value)
    return out


def collect_artifact_paths(
    tool_name: Optional[str],
    params: Optional[Dict[str, Any]],
    parsed: Any,
) -> List[str]:
    """Return resolved paths of files this tool call verifiably produced.

    * For pathy-named tools (write_/create_/download_/…), the call's params
      are candidates.
    * For ANY tool, path keys in the parsed result (top level or under
      ``data``) are candidates — tools that report where they saved count.
    * Only candidates that exist as FILES on disk survive (deduped, order
      preserved). Never raises.
    """
    try:
        candidates: List[str] = []
        name = (tool_name or "").lower()
        if name.startswith(_PATHY_TOOL_PREFIXES):
            candidates += _candidates_from(params)
        candidates += _candidates_from(parsed)
        if isinstance(parsed, dict):
            candidates += _candidates_from(parsed.get("data"))

        out: List[str] = []
        seen: set = set()
        for raw in candidates:
            try:
                p = Path(raw).expanduser()
                if not p.is_file():
                    continue
                resolved = str(p.resolve())
            except Exception:
                continue
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
        return out
    except Exception:
        logger.debug("[Artifacts] collector swallowed error", exc_info=True)
        return []
