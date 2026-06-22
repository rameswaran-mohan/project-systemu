"""Cross-shadow pattern detector (v0.4.0-e).

Promotes failure patterns observed by multiple shadows into
``global_memory.md`` so newly-spawned shadows boot with knowledge of the
army's shared experience.

Mechanism:

1. Walk every shadow's ``memory_buffer.jsonl`` + consolidated memory.
2. For each entry that carries a ``_pattern_signature`` (written by the
   supervisor's live learning path or by ``_analyze_failure`` since
   v0.4.0-c), bucket by signature.
3. Patterns observed by ≥ ``min_shadows`` distinct shadows within
   ``window_days`` are promoted.
4. Promotion appends a single dedupe-keyed entry to
   ``global_memory.md`` so subsequent runs of any shadow see it.

Side effects are explicit + scoped: no external state, no LLM calls.
The detector runs on-demand from the existing memory-consolidation
schedule (so cost grows linearly with shadows × buffer size, not with
runtime work).

The detector is **idempotent**: it tracks already-promoted signatures
in ``data/cross_shadow_promotions.json`` so subsequent runs don't append
the same promotion twice.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


_DEFAULT_PROMOTIONS_FILE = Path("data") / "cross_shadow_promotions.json"


@dataclass
class PromotionCandidate:
    pattern_signature: str
    shadow_ids:        List[str]
    sample_lessons:    List[str]
    first_seen:        Optional[str]
    last_seen:         Optional[str]

    @property
    def shadow_count(self) -> int:
        return len(self.shadow_ids)


@dataclass
class CrossShadowResult:
    scanned_shadows:    int = 0
    scanned_entries:    int = 0
    candidates_found:   List[PromotionCandidate] = field(default_factory=list)
    newly_promoted:     List[PromotionCandidate] = field(default_factory=list)
    already_promoted:   List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Public API

def detect_and_promote(
    vault: "Vault",
    *,
    min_shadows:        int = 3,
    window_days:        int = 7,
    promotions_path:    Optional[Path] = None,
    dry_run:            bool = False,
) -> CrossShadowResult:
    """Scan all shadow buffers; promote qualifying patterns to global memory.

    Args:
        vault:           Vault instance providing shadow access.
        min_shadows:     Distinct shadows that must have observed the pattern.
        window_days:     Only consider entries within the last N days.
        promotions_path: Where the "already promoted" ledger lives.
        dry_run:         When True, compute candidates but don't append to global
                         memory or update the ledger.

    Returns:
        :class:`CrossShadowResult` describing the work done.
    """
    ledger_path = promotions_path or _DEFAULT_PROMOTIONS_FILE
    already = _load_ledger(ledger_path)
    cutoff_ts = (datetime.now(tz=timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")

    # signature → {shadow_id → list of (lesson_text, ts)}
    by_sig: Dict[str, Dict[str, List[Tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    scanned_shadows = 0
    scanned_entries = 0

    shadow_index = vault.list_shadows() or []
    for sh in shadow_index:
        shadow_id = sh.get("id")
        if not shadow_id:
            continue
        scanned_shadows += 1
        try:
            _md, entries = vault.load_shadow_memory(shadow_id)
        except Exception:
            logger.debug("[CrossShadow] load_shadow_memory failed for %s", shadow_id, exc_info=True)
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sig = entry.get("_pattern_signature")
            if not sig:
                continue
            scanned_entries += 1
            ts = entry.get("_ts") or ""
            if ts and ts < cutoff_ts:
                continue
            lesson = (entry.get("lesson") or "").strip()
            if lesson:
                by_sig[sig][shadow_id].append((lesson[:300], ts))

    candidates: List[PromotionCandidate] = []
    for sig, shadows in by_sig.items():
        if len(shadows) < min_shadows:
            continue
        all_lessons = [l for triplets in shadows.values() for l, _t in triplets]
        all_ts = [t for triplets in shadows.values() for _l, t in triplets if t]
        candidates.append(PromotionCandidate(
            pattern_signature=sig,
            shadow_ids=sorted(shadows.keys()),
            sample_lessons=all_lessons[:3],
            first_seen=min(all_ts) if all_ts else None,
            last_seen=max(all_ts) if all_ts else None,
        ))

    newly_promoted: List[PromotionCandidate] = []
    already_promoted: List[str] = []
    for c in candidates:
        if c.pattern_signature in already:
            already_promoted.append(c.pattern_signature)
            continue
        newly_promoted.append(c)

    if newly_promoted and not dry_run:
        _append_to_global_memory(vault, newly_promoted)
        for c in newly_promoted:
            already.add(c.pattern_signature)
        _save_ledger(ledger_path, already)

    logger.info(
        "[CrossShadow] scanned %d shadow(s), %d signature-bearing entries, "
        "candidates=%d, newly_promoted=%d, already=%d (dry_run=%s)",
        scanned_shadows, scanned_entries, len(candidates),
        len(newly_promoted), len(already_promoted), dry_run,
    )
    return CrossShadowResult(
        scanned_shadows=scanned_shadows,
        scanned_entries=scanned_entries,
        candidates_found=candidates,
        newly_promoted=newly_promoted,
        already_promoted=already_promoted,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internals

def _load_ledger(path: Path) -> Set[str]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("promoted"), list):
                return set(data["promoted"])
    except Exception:
        logger.exception("[CrossShadow] could not read ledger %s", path)
    return set()


def _save_ledger(path: Path, promoted: Set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "promoted": sorted(promoted),
            "saved_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
        import os
        os.replace(tmp, path)
    except Exception:
        logger.exception("[CrossShadow] could not write ledger %s", path)


def _append_to_global_memory(vault, candidates: List[PromotionCandidate]) -> None:
    try:
        existing = vault.load_global_memory() or ""
    except Exception:
        existing = ""

    section_lines = ["", "## Cross-Shadow Failure Patterns (auto-promoted)", ""]
    for c in candidates:
        section_lines.append(
            f"- **{c.pattern_signature}** — observed by {c.shadow_count} shadows "
            f"({', '.join(c.shadow_ids[:4])}{'…' if c.shadow_count > 4 else ''}). "
            f"Sample: \"{c.sample_lessons[0]}\""
        )

    new_text = existing.rstrip() + "\n\n" + "\n".join(section_lines) + "\n"
    try:
        vault.save_global_memory(new_text)
    except Exception:
        logger.exception("[CrossShadow] could not save global memory")
