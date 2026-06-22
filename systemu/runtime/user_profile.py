"""v0.9.0 (Layer 1): the single runtime API for user profile + facts.

Storage shape — both files live under the vault root:
  - user_profile.json   (one record, typed; Pydantic-validated)
  - user_facts.jsonl    (append-only, one fact per line; provenance preserved)

All readers/writers go through this module so consumers don't reach into the
vault directly. This is the only writer of these files.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from systemu.core.models import UserFact, UserProfile

if TYPE_CHECKING:
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ── Paths ────────────────────────────────────────────────────────────────────
def _profile_path(vault: "Vault") -> Path:
    return Path(vault.root) / "user_profile.json"


def _facts_path(vault: "Vault") -> Path:
    return Path(vault.root) / "user_facts.jsonl"


# ── Profile API ──────────────────────────────────────────────────────────────
def get_profile(vault: "Vault") -> Optional[UserProfile]:
    """Return the user profile, or None if not set."""
    path = _profile_path(vault)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UserProfile.model_validate(data)
    except Exception:
        logger.exception("[UserProfile] could not load %s", path)
        return None


def save_profile(vault: "Vault", profile: UserProfile) -> None:
    """Write the profile atomically (tempfile + os.replace)."""
    path = _profile_path(vault)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("[UserProfile] saved %s", path)


# ── Facts API ────────────────────────────────────────────────────────────────
def _new_fact_id() -> str:
    return "fact_" + uuid.uuid4().hex[:8]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def add_fact(
    vault: "Vault",
    fact: str,
    *,
    source: str,
    tags: Optional[List[str]] = None,
    source_ref: Optional[str] = None,
    confidence: float = 1.0,
) -> UserFact:
    """Append a new fact to user_facts.jsonl. Returns the created UserFact."""
    uf = UserFact(
        id=_new_fact_id(),
        ts=_now_iso(),
        fact=fact,
        tags=list(tags or []),
        source=source,
        source_ref=source_ref,
        confidence=confidence,
    )
    path = _facts_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(uf.model_dump_json() + "\n")
    return uf


def get_facts(
    vault: "Vault",
    *,
    tags: Optional[List[str]] = None,
    include_superseded: bool = False,
    recent: Optional[int] = None,
) -> List[UserFact]:
    """Return facts, newest-last.

    Filters:
      tags                — return only facts whose tag-set overlaps with this
      include_superseded  — when False (default), exclude facts with superseded_by set
      recent              — when set, return only the most recent N facts (after filtering)
    """
    path = _facts_path(vault)
    if not path.exists():
        return []
    out: List[UserFact] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            uf = UserFact.model_validate_json(line)
        except Exception:
            logger.debug("[UserProfile] malformed fact line skipped: %s", line[:80])
            continue
        if not include_superseded and uf.superseded_by:
            continue
        if tags and not (set(uf.tags) & set(tags)):
            continue
        out.append(uf)
    if recent is not None and recent > 0:
        out = out[-recent:]
    return out


def forget_fact(vault: "Vault", fact_id: str, *, reason: str = "forgotten") -> bool:
    """Mark a fact superseded. Best-effort; in-place rewrite of the JSONL."""
    path = _facts_path(vault)
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    out_lines: List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            uf = UserFact.model_validate_json(line)
        except Exception:
            out_lines.append(line)
            continue
        if uf.id == fact_id and not uf.superseded_by:
            uf = uf.model_copy(update={"superseded_by": reason})
            changed = True
        out_lines.append(uf.model_dump_json())
    if changed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return changed


def wipe(vault: "Vault") -> None:
    """Remove BOTH the profile and the facts log. Idempotent."""
    for p in (_profile_path(vault), _facts_path(vault)):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            logger.warning("[UserProfile] could not delete %s", p, exc_info=True)
