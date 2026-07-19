"""Vault CRUD — file-based persistent storage for all Systemu entities.

Every entity type has:
  • An index.json  — flat list of lightweight header dicts for fast scanning.
  • Per-entity .json files — full entity data.

Skills additionally follow the Agent Skills Standard:
  vault/skills/skill_<id>/
    SKILL.md           — markdown metadata
    scripts/           — optional executable scripts

All index writes use an atomic temp-file-rename pattern to prevent corruption.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar

from systemu.core.utils import utcnow
from systemu.core.schema_utils import normalize_parameters_schema, schema_param_names
from systemu.core.models import (
    Activity, ActivityStatus,
    Evolution, EvolutionStatus,
    Notification, NotificationStatus,
    Scroll, ScrollStatus,
    Shadow, ShadowStatus,
    Skill,
    Tool, ToolStatus,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ─── index header extractors ─────────────────────────────────────────────────
# Each function returns the lightweight dict saved to the entity's index.json.

def _scroll_header(s: Scroll) -> Dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "status": s.status,
        "source_session_id": s.source_session_id,
        "created_at": s.created_at.isoformat(), "tags": s.tags,
        # v0.6.5-a: fast badge rendering — derive from pipeline_trace
        "has_warnings": s.has_warnings,
    }

def _skill_header(s: Skill) -> Dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "description": s.description,
        "category": s.category,
        "proficiency_level": s.proficiency_level,
        "required_tool_names": s.required_tool_names,
        "required_tool_ids": s.required_tool_ids,
        "evidence_scroll_ids": s.evidence_scroll_ids,
        "created_at": s.created_at.isoformat(),
    }

def _summarise_schema(schema: Dict[str, Any]) -> Dict[str, str]:
    """v0.6.1-d: strip a JSON Schema to {field: type} pairs (max 20 fields).

    Lives in vault.py so the canonical implementation is colocated with
    `_tool_header` (which embeds the summary).  Both pipelines
    (scroll_validator + activity_extractor) read these summaries directly
    from the index header — no per-tool `get_tool()` fetch.
    """
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") if "properties" in schema else schema
    if not isinstance(props, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in list(props.items())[:20]:
        if isinstance(v, dict):
            out[str(k)] = str(v.get("type") or v.get("$type") or "any")[:30]
        else:
            out[str(k)] = "any"
    return out


def _tool_header(t: Tool) -> Dict[str, Any]:
    return {
        "id": t.id, "name": t.name, "description": t.description,
        "tool_type": t.tool_type,
        "parameter_names": schema_param_names(t.parameters_schema),
        "dependencies": t.dependencies or [],
        "status": t.status, "enabled": t.enabled,
        "forged_by_systemu": t.forged_by_systemu,
        # v0.5.0-a: dry-run status visible on the Tools page list
        "dry_run_status": getattr(t, "dry_run_status", "not_run") or "not_run",
        "version": getattr(t, "version", 1),
        # v0.6.1-d: schema summaries inline in the header so catalog builders
        # (scroll_validator, activity_extractor) don't N+1 `vault.get_tool()`
        # for every tool just to read schemas.
        "parameters_schema_summary": _summarise_schema(t.parameters_schema),
        "return_schema_summary":    _summarise_schema(t.return_schema),
        "created_at": t.created_at.isoformat(),
    }

def _activity_header(a: Activity) -> Dict[str, Any]:
    return {
        "id": a.id, "name": a.name, "scroll_id": a.scroll_id,
        "required_tool_ids": a.required_tool_ids,
        "required_skill_ids": a.required_skill_ids,
        "missing_tools": a.missing_tools,
        "assigned_shadow_id": a.assigned_shadow_id,
        "status": a.status, "created_at": a.created_at.isoformat(),
    }

def _shadow_header(s: Shadow) -> Dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "description": s.description,
        "status": s.status,
        "skill_ids": s.skill_ids, "tool_ids": s.available_tool_ids,
        "activity_count": len(s.assigned_activity_ids),
        "memory_md_path": s.memory_md_path,
        "created_at": s.created_at.isoformat(),
    }

def _evolution_header(e: Evolution) -> Dict[str, Any]:
    return {
        "id": e.id, "evolution_type": e.evolution_type,
        "target_entity_type": e.target_entity_type,
        "description": e.description, "status": e.status,
        "proposed_at": e.proposed_at.isoformat(),
    }

def _notification_header(n: Notification) -> Dict[str, Any]:
    return {
        "id": n.id, "title": n.title, "status": n.status,
        "created_at": n.created_at.isoformat(),
    }


def _empty_elder_memory_md() -> str:
    """Initial scaffold for ELDER_MEMORY.md — global personalisation across all shadows."""
    from datetime import datetime
    return (
        f"---\n"
        f"last_consolidated: {utcnow().isoformat()}\n"
        f"entry_count: 0\n"
        f"buffer_pending: 0\n"
        f"---\n\n"
        f"# Elder Memory — Global Personalisation\n\n"
        f"## User Preferences\n\n"
        f"_No user preferences observed yet._\n\n"
        f"## Workflow Patterns\n\n"
        f"_No workflow patterns observed yet._\n\n"
        f"## Tool Affinities\n\n"
        f"_No tool affinities recorded yet._\n\n"
        f"## Recurring Variables\n\n"
        f"_No recurring variables observed yet._\n\n"
        f"## Personalisation Notes\n\n"
        f"_No personalisation notes yet._\n"
    )


def _empty_shadow_memory_md(shadow: Shadow) -> str:
    """Initial scaffold for a shadow's SHADOW_MEMORY.md — typed sections, all empty.

    Consolidation always rewrites this file in full; the scaffold just gives the
    UI and any cold-start reads a coherent shape to render.
    """
    from datetime import datetime
    return (
        f"---\n"
        f"shadow_id: {shadow.id}\n"
        f"last_consolidated: {utcnow().isoformat()}\n"
        f"entry_count: 0\n"
        f"buffer_pending: 0\n"
        f"---\n\n"
        f"# Memory: {shadow.name}\n\n"
        f"## Self-Assessment\n\n"
        f"_No self-assessment yet — this shadow has not produced any executions._\n\n"
        f"## Heuristics\n\n"
        f"_No heuristics yet._\n\n"
        f"## Failure Patterns\n\n"
        f"_No failure patterns observed yet._\n\n"
        f"## Tool Quirks\n\n"
        f"_No tool quirks recorded yet._\n\n"
        f"## Domain Glossary\n\n"
        f"_No domain terms learned yet._\n"
    )


# ─────────────────────────────────────────────────────────────────────────────

class Vault:
    """File-based vault with atomic index writes and full CRUD for all entities."""

    def __init__(
        self,
        vault_dir: str | Path | None = None,
        *,
        root: str | Path | None = None,
        strict_tier_types: bool = True,
    ):
        """Construct a file-backed Vault rooted at ``vault_dir``.

        Args:
            vault_dir: Directory containing scrolls/, activities/, …
                       Also accepts ``root=`` as a keyword alias.
            root: Keyword alias for vault_dir.
            strict_tier_types: When True (default), the buffer
                gate-keepers reject Shadow writes whose category is not
                in ``SHADOW_CLAIM_TYPES``.  Flip to False to replay
                pre-audit data with ad-hoc categories.  Elder tier is
                always permissive — strictness applies to Shadow only.
        """
        if vault_dir is None and root is None:
            raise TypeError("Vault() requires vault_dir or root")
        resolved = vault_dir if vault_dir is not None else root
        self.root = Path(resolved)
        self._strict_tier_types = bool(strict_tier_types)
        self._ensure_structure()

    # ── initialisation ───────────────────────────────────────────────────────

    def _ensure_structure(self) -> None:
        """Create vault directory structure and initialise empty index files."""
        subdirs = [
            "scrolls", "activities", "skills",
            "tools/implementations", "shadow_army",
            "evolutions", "notifications", "elder",
        ]
        for sub in subdirs:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

        # Initialise empty index files if absent
        index_paths = [
            "scrolls/index.json", "activities/index.json",
            "skills/index.json", "tools/index.json",
            "shadow_army/index.json", "evolutions/index.json",
            "notifications/pending.json",
        ]
        for rel in index_paths:
            p = self.root / rel
            if not p.exists():
                self._write_json(p, [])

        # Initialise Elder memory scaffold if absent
        elder_memory = self.root / "elder" / "ELDER_MEMORY.md"
        if not elder_memory.exists():
            elder_memory.write_text(_empty_elder_memory_md(), encoding="utf-8")

        # Backfill 'enabled' into any tool index entries written before this field was added.
        self._backfill_tool_index_enabled()

        # Migrate any flat shadow_<id>.json files into per-shadow directories.
        self._migrate_shadow_directories()

    # ── low-level helpers ────────────────────────────────────────────────────

    def _migrate_shadow_directories(self) -> None:
        """Move legacy flat shadow_<id>.json files into shadow_<id>/shadow.json.

        Idempotent — does nothing if every shadow already lives in its directory.
        Runs once on startup; the move is atomic via os.replace.
        """
        shadow_dir = self.root / "shadow_army"
        for entry in self._read_json(shadow_dir / "index.json"):
            sid = entry.get("id")
            if not sid:
                continue
            flat   = shadow_dir / f"shadow_{sid}.json"
            nested = shadow_dir / f"shadow_{sid}" / "shadow.json"
            if flat.exists() and not nested.exists():
                nested.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(flat, nested)
                    logger.info("[Vault] Migrated shadow %s into per-shadow directory", sid)
                except OSError as exc:
                    logger.warning("[Vault] Could not migrate shadow %s: %s", sid, exc)

    def _backfill_tool_index_enabled(self) -> None:
        """Backfill the 'enabled' field in tool index entries that predate it."""
        index_path = self.root / "tools/index.json"
        index = self._read_json(index_path)
        changed = False
        for entry in index:
            if "enabled" not in entry:
                tool_json = self.root / f"tools/tool_{entry['id']}.json"
                data = self._read_json(tool_json)
                entry["enabled"] = data.get("enabled", False) if data else False
                changed = True
        if changed:
            self._write_json(index_path, index)

    def _write_json(self, path: Path, data: Any) -> None:
        """Atomically write JSON to path (temp-file-rename pattern)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then rename
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read_json(self, path: Path) -> Any:
        """Read and parse JSON from path. Returns [] for empty / missing."""
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return []

    def load_index(self, entity: str) -> List[Dict[str, Any]]:
        """Load the flat index list for a given entity type.

        entity: "scrolls" | "activities" | "skills" | "tools" |
                "shadow_army" | "evolutions" | "notifications"
        """
        index_file = {
            "scrolls":       "scrolls/index.json",
            "activities":    "activities/index.json",
            "skills":        "skills/index.json",
            "tools":         "tools/index.json",
            "shadow_army":   "shadow_army/index.json",
            "evolutions":    "evolutions/index.json",
            "notifications": "notifications/pending.json",
            "decisions":     "decisions/index.json",
        }.get(entity)
        if index_file is None:
            raise ValueError(f"Unknown entity type: {entity!r}")
        return self._read_json(self.root / index_file)

    def _update_index(
        self,
        index_rel: str,
        header: Dict[str, Any],
    ) -> None:
        """Upsert a header dict into the named index file (keyed on 'id')."""
        path   = self.root / index_rel
        index  = self._read_json(path)
        idx_map = {item["id"]: i for i, item in enumerate(index)}
        if header["id"] in idx_map:
            index[idx_map[header["id"]]] = header
        else:
            index.append(header)
        self._write_json(path, index)

    def _remove_from_index(self, index_rel: str, entity_id: str) -> None:
        path  = self.root / index_rel
        index = self._read_json(path)
        index = [item for item in index if item.get("id") != entity_id]
        self._write_json(path, index)

    # ── Scroll ────────────────────────────────────────────────────────────────

    def save_scroll(self, scroll: Scroll) -> None:
        path = self.root / f"scrolls/scroll_{scroll.id}.json"
        self._write_json(path, scroll.model_dump(mode="json"))
        self._update_index("scrolls/index.json", _scroll_header(scroll))
        logger.debug("Saved scroll %s (%s)", scroll.id, scroll.status)

    def get_scroll(self, scroll_id: str) -> Scroll:
        path = self.root / f"scrolls/scroll_{scroll_id}.json"
        data = self._read_json(path)
        if not data:
            raise KeyError(f"Scroll not found: {scroll_id}")
        return Scroll.model_validate(data)

    def list_scrolls(
        self, status: Optional[ScrollStatus] = None
    ) -> List[Dict[str, Any]]:
        index = self.load_index("scrolls")
        if status:
            index = [s for s in index if s.get("status") == status.value]
        return index

    # ── Skill ─────────────────────────────────────────────────────────────────

    def save_skill(self, skill: Skill) -> None:
        """Save skill JSON and write the Agent Skills Standard SKILL.md.

        This is the single authoritative writer for SKILL.md — no other code
        path should generate skill markdown files.

        SKILL.md format follows the Anthropic Agent Skills open standard:
          YAML frontmatter:  name, description, category, proficiency_level,
                             required_tools (names)
          Procedural body:   Description + step-by-step Procedural Instructions
        """
        # v0.6.1-e: batch resolution — single index read + dict lookup instead
        # of N find_tool_by_name() calls (each of which previously opened a
        # session / read the full record).  Unknown names are silently dropped.
        if skill.required_tool_names:
            name_to_id: Dict[str, str] = {}
            for t in (self.load_index("tools") or []):
                tname = t.get("name")
                if tname:
                    name_to_id[tname] = t["id"]
            resolved_ids: List[str] = []
            for tname in skill.required_tool_names:
                tid = name_to_id.get(tname)
                if tid:
                    resolved_ids.append(tid)
                else:
                    logger.debug(
                        "[Vault] save_skill: tool %r not found in vault yet", tname,
                    )
            skill.required_tool_ids = resolved_ids

        # ── Resolve the on-disk dir name (kebab-cased, spec-conformant) ──────
        from systemu.storage.skill_migrator import _kebab, _render_skill_md

        base_name = _kebab(skill.name)
        skills_root = self.root / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)

        # Find any existing dir for this skill by reading the JSON record's
        # stored skill_md_path. If the parent of that path is still a child
        # of skills_root, we treat it as the current dir.
        existing_dir: Optional[Path] = None
        try:
            old_record = self._read_json(self.root / f"skills/skill_{skill.id}.json")
            old_path = (old_record or {}).get("skill_md_path")
            if old_path:
                p = Path(old_path).parent
                if p.exists() and p.parent == skills_root:
                    existing_dir = p
        except Exception:
            existing_dir = None

        # Pick the target dir: reuse existing, or claim base_name, or suffix.
        target_dir = skills_root / base_name
        if existing_dir is not None and existing_dir.name == base_name:
            # Same-name rewrite — fall through.
            pass
        elif existing_dir is not None and existing_dir.name != base_name:
            # Renamed skill — move the dir.
            if target_dir.exists():
                target_dir = skills_root / f"{base_name}-{skill.id[:8]}"
            existing_dir.rename(target_dir)
        elif target_dir.exists():
            # Brand-new skill that collides with an unrelated existing dir.
            target_dir = skills_root / f"{base_name}-{skill.id[:8]}"
            target_dir.mkdir(parents=True, exist_ok=False)
        else:
            target_dir.mkdir(parents=True, exist_ok=False)

        (target_dir / "scripts").mkdir(exist_ok=True)

        # ── Body: instructions + required-tools section + evidence section ───
        category    = skill.category or "general"
        proficiency = skill.proficiency_level or "intermediate"
        tool_names  = skill.required_tool_names or skill.required_tool_ids  # fallback to IDs if names missing

        instructions_body = (
            skill.instructions_md.strip()
            if skill.instructions_md.strip()
            else "_No procedural instructions captured yet._"
        )
        if skill.evidence_scroll_ids:
            evidence_section = "\n".join(f"- {s}" for s in skill.evidence_scroll_ids)
        else:
            evidence_section = "_No evidence scrolls._"

        body = (
            f"# {_kebab(skill.name)}\n\n"
            f"## Description\n\n{skill.description}\n\n"
            f"## Procedural Instructions\n\n{instructions_body}\n\n"
            f"## Required Tools\n\n"
            f"{chr(10).join('- ' + t for t in tool_names) if tool_names else '_No tools linked yet._'}\n\n"
            f"## Evidence Scrolls\n\n{evidence_section}\n"
        )

        # ── Metadata block: everything Systemu-internal ──────────────────────
        metadata: Dict[str, Any] = {
            "category": category,
            "proficiency_level": proficiency,
        }
        if tool_names:
            metadata["required_tools"] = list(tool_names)

        skill_md = target_dir / "SKILL.md"
        skill_md.write_text(
            _render_skill_md(
                name=_kebab(skill.name),
                description=skill.description,
                metadata=metadata,
                body=body,
            ),
            encoding="utf-8",
        )
        skill.skill_md_path = str(skill_md)

        # ── Full JSON ────────────────────────────────────────────────────────
        json_path = self.root / f"skills/skill_{skill.id}.json"
        self._write_json(json_path, skill.model_dump(mode="json"))
        self._update_index("skills/index.json", _skill_header(skill))
        logger.debug("Saved skill %s (%s)", skill.id, skill.name)

    def get_skill(self, skill_id: str) -> Skill:
        path = self.root / f"skills/skill_{skill_id}.json"
        data = self._read_json(path)
        if not data:
            raise KeyError(f"Skill not found: {skill_id}")
        return Skill.model_validate(data)

    def find_skill_by_name(self, name: str) -> Optional[Skill]:
        """Case-insensitive name lookup via index — returns first match or None."""
        name_lower = name.lower()
        for header in self.load_index("skills"):
            if header.get("name", "").lower() == name_lower:
                return self.get_skill(header["id"])
        return None

    def list_skills(self) -> List[Dict[str, Any]]:
        return self.load_index("skills")

    # ── Tool ──────────────────────────────────────────────────────────────────

    def save_tool(self, tool: Tool) -> None:
        """Save tool JSON and write the TOOL.md manifest.

        TOOL.md mirrors the SKILL.md format: YAML frontmatter (name, type, status,
        enabled, dependencies) + structured body (description, parameters, returns,
        implementation notes). Vault is the single authoritative writer for TOOL.md.
        """
        # ── TOOL.md manifest ──────────────────────────────────────────────────
        tool_dir = self.root / f"tools/tool_{tool.id}"
        tool_dir.mkdir(parents=True, exist_ok=True)

        deps = tool.dependencies or []
        if deps:
            deps_yaml = "dependencies:\n" + "\n".join(f"  - {d}" for d in deps)
        else:
            deps_yaml = "dependencies: []"

        # Render parameters from schema
        param_lines: List[str] = []
        for pname, pdef in normalize_parameters_schema(tool.parameters_schema or {}).items():
            ptype    = pdef.get("type", "any") if isinstance(pdef, dict) else "any"
            pdesc    = pdef.get("description", "") if isinstance(pdef, dict) else ""
            preq     = pdef.get("required", False) if isinstance(pdef, dict) else False
            pdefault = pdef.get("default") if isinstance(pdef, dict) else None
            suffix   = "required" if preq else (f"default: {pdefault}" if pdefault is not None else "optional")
            param_lines.append(f"- {pname} ({ptype}, {suffix}): {pdesc}".rstrip(": "))
        params_body = "\n".join(param_lines) if param_lines else "_No parameters defined._"

        # Render returns from schema
        return_lines: List[str] = []
        for rname, rdef in (tool.return_schema or {}).items():
            rtype = rdef.get("type", "any") if isinstance(rdef, dict) else "any"
            rdesc = rdef.get("description", "") if isinstance(rdef, dict) else ""
            return_lines.append(f"- {rname} ({rtype}): {rdesc}".rstrip(": "))
        returns_body = "\n".join(return_lines) if return_lines else "_No return schema defined._"

        impl_notes = tool.implementation_notes.strip() or "_No implementation notes yet._"

        tool_type_val = tool.tool_type.value if hasattr(tool.tool_type, "value") else str(tool.tool_type)
        status_val    = tool.status.value if hasattr(tool.status, "value") else str(tool.status)

        tool_md_content = (
            f"---\n"
            f"name: {tool.name}\n"
            f"tool_type: {tool_type_val}\n"
            f"status: {status_val}\n"
            f"enabled: {str(tool.enabled).lower()}\n"
            f"{deps_yaml}\n"
            f"---\n\n"
            f"# {tool.name}\n\n"
            f"## Description\n\n"
            f"{tool.description}\n\n"
            f"## Parameters\n\n"
            f"{params_body}\n\n"
            f"## Returns\n\n"
            f"{returns_body}\n\n"
            f"## Implementation Notes\n\n"
            f"{impl_notes}\n"
        )

        tool_md = tool_dir / "TOOL.md"
        tool_md.write_text(tool_md_content, encoding="utf-8")
        tool.tool_md_path = str(tool_md)

        # ── Full JSON ─────────────────────────────────────────────────────────
        json_path = self.root / f"tools/tool_{tool.id}.json"
        self._write_json(json_path, tool.model_dump(mode="json"))
        self._update_index("tools/index.json", _tool_header(tool))
        logger.debug("Saved tool %s (%s)", tool.id, tool.name)

    def get_tool(self, tool_id: str) -> Tool:
        path = self.root / f"tools/tool_{tool_id}.json"
        data = self._read_json(path)
        if not data:
            raise KeyError(f"Tool not found: {tool_id}")
        return Tool.model_validate(data)

    def find_tool_by_name(self, name: str) -> Optional[Tool]:
        name_lower = name.lower()
        for header in self.load_index("tools"):
            if header.get("name", "").lower() == name_lower:
                return self.get_tool(header["id"])
        return None

    def list_tools(self, status: Optional[ToolStatus] = None) -> List[Dict[str, Any]]:
        index = self.load_index("tools")
        if status:
            index = [t for t in index if t.get("status") == status.value]
        return index

    # ── Activity ──────────────────────────────────────────────────────────────

    def save_activity(self, activity: Activity) -> None:
        path = self.root / f"activities/activity_{activity.id}.json"
        self._write_json(path, activity.model_dump(mode="json"))
        self._update_index("activities/index.json", _activity_header(activity))
        logger.debug("Saved activity %s (%s)", activity.id, activity.name)

    def get_activity(self, activity_id: str) -> Activity:
        path = self.root / f"activities/activity_{activity_id}.json"
        data = self._read_json(path)
        if not data:
            raise KeyError(f"Activity not found: {activity_id}")
        return Activity.model_validate(data)

    def list_activities(
        self, status: Optional[ActivityStatus] = None
    ) -> List[Dict[str, Any]]:
        index = self.load_index("activities")
        if status:
            index = [a for a in index if a.get("status") == status.value]
        return index

    # ── Shadow ────────────────────────────────────────────────────────────────

    def _shadow_dir(self, shadow_id: str) -> Path:
        return self.root / f"shadow_army/shadow_{shadow_id}"

    def prune_old_executions(self, max_keep: int = 50) -> int:
        """Delete the oldest execution directories beyond max_keep.

        Walks vault/executions/ by modification time (newest first), keeps the
        newest `max_keep` directories, deletes the rest. Safe to call on every
        save_shadow — O(N) in execution count, amortised O(1) when below threshold.

        Returns the number of directories deleted.
        """
        import shutil
        executions_dir = self.root / "executions"
        if not executions_dir.exists():
            return 0

        entries = [
            p for p in executions_dir.iterdir() if p.is_dir()
        ]
        if len(entries) <= max_keep:
            return 0

        # Sort newest-first by mtime
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        to_delete = entries[max_keep:]
        deleted = 0
        for path in to_delete:
            try:
                shutil.rmtree(path)
                deleted += 1
            except OSError as exc:
                logger.warning("[Vault] Could not prune execution dir %s: %s", path, exc)

        if deleted:
            logger.info("[Vault] Pruned %d old execution dir(s) (kept newest %d)", deleted, max_keep)
        return deleted

    def save_shadow(self, shadow: Shadow) -> None:
        sdir = self._shadow_dir(shadow.id)
        sdir.mkdir(parents=True, exist_ok=True)

        # Set memory paths if not already set
        memory_md  = sdir / "SHADOW_MEMORY.md"
        memory_buf = sdir / "memory_buffer.jsonl"
        shadow.memory_md_path     = str(memory_md)
        shadow.memory_buffer_path = str(memory_buf)

        # Initialise an empty MEMORY.md scaffold if missing — gives the shadow a
        # canonical store from day one and lets the UI render something coherent.
        if not memory_md.exists():
            memory_md.write_text(_empty_shadow_memory_md(shadow), encoding="utf-8")

        self._write_json(sdir / "shadow.json", shadow.model_dump(mode="json"))
        self._update_index("shadow_army/index.json", _shadow_header(shadow))
        logger.debug("Saved shadow %s (%s)", shadow.id, shadow.name)

    def get_shadow(self, shadow_id: str) -> Shadow:
        # Prefer per-shadow directory; fall back to legacy flat path.
        nested = self._shadow_dir(shadow_id) / "shadow.json"
        flat   = self.root / f"shadow_army/shadow_{shadow_id}.json"
        data = self._read_json(nested) or self._read_json(flat)
        if not data:
            raise KeyError(f"Shadow not found: {shadow_id}")
        shadow = Shadow.model_validate(data)
        # Derive memory paths on the fly so legacy/migrated shadows have working
        # paths without needing an explicit re-save first.
        sdir = self._shadow_dir(shadow_id)
        if not shadow.memory_md_path:
            shadow.memory_md_path = str(sdir / "SHADOW_MEMORY.md")
        if not shadow.memory_buffer_path:
            shadow.memory_buffer_path = str(sdir / "memory_buffer.jsonl")
        return shadow

    # ── Shadow memory ─────────────────────────────────────────────────────────

    def save_shadow_memory(self, shadow_id: str, memory_md: str) -> None:
        """Atomically write the consolidated SHADOW_MEMORY.md for a shadow."""
        sdir = self._shadow_dir(shadow_id)
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / "SHADOW_MEMORY.md"
        # Atomic write — temp file in the same directory, then replace.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(memory_md)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def append_memory_buffer(self, shadow_id: str, entry: Dict[str, Any]) -> None:
        """Append a lesson candidate to the shadow's memory buffer (JSONL).

        Low-level writer.  Prefer :meth:`append_shadow_memory_buffer` for new
        code — it enforces the memory-model write contract by stamping tier
        provenance and rejecting cross-tier writes.  See
        ``docs/memory-model.md`` for the contract.
        """
        sdir = self._shadow_dir(shadow_id)
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / "memory_buffer.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    # ── Memory tier helpers (write-contract gate-keepers) ────────────────────
    #
    # These two methods are the *only sanctioned writers* to buffer files for
    # new code.  They stamp tier provenance on every entry and validate that
    # the claim category belongs to the tier being written.  See
    # ``docs/memory-model.md`` for the full contract.
    #
    # Single source of truth for claim categories lives in
    # ``systemu/core/memory_types.py``.  The vault re-exports those names
    # below; do NOT redefine them here.
    #
    # Tier strictness model:
    #
    #   * Shadow tier — CLOSED.  Categories must be in SHADOW_CLAIM_TYPES.
    #     Strict mode (default) rejects unknown categories with a clear
    #     error pointing at the allowlist.
    #
    #   * Elder tier — OPEN.  LLM-driven categories are unbounded, so any
    #     non-empty string passes.  The cross-tier wall still applies:
    #     anything in SHADOW_CLAIM_TYPES is rejected from Elder (and vice
    #     versa), so the two tiers' claim spaces don't collide.

    def append_shadow_memory_buffer(
        self,
        shadow_id: str,
        entry: Dict[str, Any],
        *,
        source: str,
    ) -> Dict[str, Any]:
        """Append an entry to a Shadow's memory buffer with tier metadata.

        See ``systemu.core.memory_types.augment_buffer_entry`` for the
        full validation contract.  This method enforces it for Shadow
        writes (allowlist = ``SHADOW_CLAIM_TYPES``, strict by default).
        """
        from systemu.core.memory_types import (
            augment_buffer_entry,
            SHADOW_CLAIM_TYPES,
            ELDER_RECOMMENDED_TYPES,
        )
        augmented = augment_buffer_entry(
            entry,
            tier="shadow",
            source=source,
            allowed=SHADOW_CLAIM_TYPES,
            forbidden=ELDER_RECOMMENDED_TYPES,
            strict=self._strict_tier_types,
        )
        self.append_memory_buffer(shadow_id, augmented)
        return augmented

    def append_elder_buffer(
        self,
        entry: Dict[str, Any],
        *,
        source: str,
    ) -> Dict[str, Any]:
        """Append an entry to the Elder memory buffer with tier metadata.

        Elder categories are LLM-driven and open-ended.  Only the
        cross-tier wall is enforced — a Shadow-tier category is
        rejected.  Strictness does not apply to Elder.
        """
        from systemu.core.memory_types import (
            augment_buffer_entry,
            SHADOW_CLAIM_TYPES,
        )
        augmented = augment_buffer_entry(
            entry,
            tier="elder",
            source=source,
            allowed=frozenset(),    # Elder is open-ended
            forbidden=SHADOW_CLAIM_TYPES,
            strict=False,
        )
        self.append_elder_memory_buffer(augmented)
        return augmented

    def load_shadow_memory(self, shadow_id: str) -> tuple[str, List[Dict[str, Any]]]:
        """Return (MEMORY.md text, list of buffer entries) for a shadow.

        Missing files are treated as empty — never raises for absent state.
        """
        sdir = self._shadow_dir(shadow_id)
        md_path  = sdir / "SHADOW_MEMORY.md"
        buf_path = sdir / "memory_buffer.jsonl"

        md_text  = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        entries: List[Dict[str, Any]] = []
        if buf_path.exists():
            for line in buf_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("[Vault] Skipping malformed buffer line for shadow %s", shadow_id)
        return md_text, entries

    def clear_memory_buffer(self, shadow_id: str) -> None:
        """Truncate the memory buffer — called after successful consolidation."""
        path = self._shadow_dir(shadow_id) / "memory_buffer.jsonl"
        if path.exists():
            path.write_text("", encoding="utf-8")

    def expunge_memory_entry(
        self,
        shadow_id: str,
        predicate,
        *,
        audit_path: Optional[Path] = None,
        reason: str = "operator_request",
    ) -> int:
        """Remove buffer entries that match ``predicate``.  Returns the count.

        v0.4.0-a — needed because the v0.4.0 Intelligent Supervisor writes
        lessons live to ``memory_buffer.jsonl``.  When a confidently wrong
        lesson is detected (or an operator wants to retract one), today's
        only recourse would be hand-editing the file.  This API replaces
        that workflow with a structured, audited removal.

        Args:
            shadow_id:  Owning shadow.
            predicate:  Callable ``(entry: dict) -> bool``.  Entries for which
                        this returns truthy are removed.
            audit_path: Optional path for the audit log (defaults to
                        ``data/audit/expunged_lessons.jsonl``).
            reason:     String stored on each audit row so multiple expunge
                        sources can be distinguished post-hoc.

        Returns:
            Number of entries removed.  When zero, the buffer file is
            unchanged.
        """
        path = self._shadow_dir(shadow_id) / "memory_buffer.jsonl"
        if not path.exists():
            return 0

        kept: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                # Preserve malformed lines verbatim — they're operator data we
                # don't understand; never silently lose them on an expunge call.
                kept.append({"__raw__": raw_line})
                continue
            try:
                hit = bool(predicate(entry))
            except Exception:
                logger.exception(
                    "[Vault] expunge predicate raised on entry; keeping it for safety",
                )
                hit = False
            if hit:
                removed.append(entry)
            else:
                kept.append(entry)

        if not removed:
            return 0

        # Re-serialise.  ``__raw__`` lines round-trip as their original text.
        new_lines = []
        for k in kept:
            if isinstance(k, dict) and "__raw__" in k and len(k) == 1:
                new_lines.append(k["__raw__"])
            else:
                new_lines.append(json.dumps(k, ensure_ascii=False))
        path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")

        # Audit trail so operators can see what was removed and why.
        try:
            audit_target = audit_path or (
                self.root.parent / "data" / "audit" / "expunged_lessons.jsonl"
            )
            audit_target.parent.mkdir(parents=True, exist_ok=True)
            with audit_target.open("a", encoding="utf-8") as f:
                ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
                for r in removed:
                    f.write(json.dumps({
                        "ts":         ts,
                        "shadow_id":  shadow_id,
                        "reason":     reason,
                        "entry":      r,
                    }, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("[Vault] could not write expunge audit log")

        logger.info(
            "[Vault] expunged %d memory entries for shadow %s (reason=%s)",
            len(removed), shadow_id, reason,
        )
        return len(removed)

    def list_shadows(
        self, status: Optional[ShadowStatus] = None
    ) -> List[Dict[str, Any]]:
        index = self.load_index("shadow_army")
        if status:
            index = [s for s in index if s.get("status") == status.value]
        return index

    # ── Elder ─────────────────────────────────────────────────────────────────

    def save_elder_memory(self, md_text: str) -> None:
        """Atomically overwrite ELDER_MEMORY.md with consolidated content."""
        path = self.root / "elder" / "ELDER_MEMORY.md"
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(md_text)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_elder_memory(self) -> str:
        """Read ELDER_MEMORY.md — returns empty string if not yet initialised."""
        path = self.root / "elder" / "ELDER_MEMORY.md"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("[Vault] Could not read ELDER_MEMORY.md: %s", exc)
            return ""

    def append_elder_memory_buffer(self, entry: Dict[str, Any]) -> None:
        """Append a lesson candidate to the Elder memory buffer (JSONL)."""
        path = self.root / "elder" / "memory_buffer.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def load_elder_memory_buffer(self) -> List[Dict[str, Any]]:
        """Load all pending Elder memory buffer entries."""
        path = self.root / "elder" / "memory_buffer.jsonl"
        entries: List[Dict[str, Any]] = []
        if not path.exists():
            return entries
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("[Vault] Skipping malformed Elder memory buffer line")
        return entries

    def clear_elder_memory_buffer(self) -> None:
        """Truncate the Elder memory buffer after successful consolidation."""
        path = self.root / "elder" / "memory_buffer.jsonl"
        if path.exists():
            path.write_text("", encoding="utf-8")

    # ── Global Memory aliases (canonical public API) ─────────────────────────

    def load_global_memory(self) -> str:
        """Read GLOBAL_MEMORY (stored as elder/ELDER_MEMORY.md)."""
        return self.load_elder_memory()

    def save_global_memory(self, md_text: str) -> None:
        """Atomically overwrite GLOBAL_MEMORY with consolidated content."""
        self.save_elder_memory(md_text)

    def append_global_memory_buffer(self, entry: Dict[str, Any]) -> None:
        """Append a lesson candidate to the global memory buffer."""
        self.append_elder_memory_buffer(entry)

    def clear_global_memory_buffer(self) -> None:
        """Truncate the global memory buffer after successful consolidation."""
        self.clear_elder_memory_buffer()

    def append_chat_history(self, entry: Dict[str, Any]) -> None:
        """Append a chat submission record to vault/elder/chat_history.jsonl."""
        path = self.root / "elder" / "chat_history.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def load_chat_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Load the most recent `limit` chat submissions (newest last)."""
        path = self.root / "elder" / "chat_history.jsonl"
        if not path.exists():
            return []
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        recent = lines[-limit:] if len(lines) > limit else lines
        entries: List[Dict[str, Any]] = []
        for line in recent:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries

    def update_chat_history_entry(self, ts: str, updates: Dict[str, Any]) -> None:
        """Update a chat_history.jsonl entry by timestamp key (in-place rewrite)."""
        path = self.root / "elder" / "chat_history.jsonl"
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        new_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("ts") == ts:
                    entry.update(updates)
                new_lines.append(json.dumps(entry, ensure_ascii=False, default=str))
            except json.JSONDecodeError:
                new_lines.append(line)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + ("\n" if new_lines else ""))
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear_chat_history(self) -> None:
        """Delete all chat-history entries (operator-initiated 'Clear history')."""
        path = self.root / "elder" / "chat_history.jsonl"
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("[Vault] clear_chat_history failed: %s", exc)

    # ── User profile + facts (v0.9.0 Layer 1) ───────────────────────────────
    def get_user_profile(self):
        """v0.9.0: return the user profile, or None if not set."""
        from systemu.runtime.user_profile import get_profile
        return get_profile(self)

    def save_user_profile(self, profile) -> None:
        """v0.9.0: write the user profile (atomic)."""
        from systemu.runtime.user_profile import save_profile
        save_profile(self, profile)

    def load_user_facts(self, *, tags=None, include_superseded: bool = False,
                          recent=None):
        """v0.9.0: return facts (newest-last). Filters: tags, include_superseded, recent."""
        from systemu.runtime.user_profile import get_facts
        return get_facts(self, tags=tags, include_superseded=include_superseded, recent=recent)

    def append_user_fact(self, *, fact: str, source: str, tags=None,
                           source_ref=None, confidence: float = 1.0,
                           origin_class: Optional[str] = None):
        """v0.9.0: append a new fact, return the created UserFact.

        ``origin_class`` (R-A16 slice-1, IMPL-5) is pure TRANSPORT here — the
        canonical taint axis of the fact's VALUE, validated by ``UserFact`` and
        clamped fail-untrusted by ``requirement_binder._fact_origin``. This wrapper
        is the SANCTIONED write path (``user_profile`` docstring), so dropping the
        parameter here made the whole taint mechanism unreachable in production.
        Defaults to ABSENT, which grandfathers to ``operator`` at bind.
        """
        from systemu.runtime.user_profile import add_fact
        return add_fact(self, fact, source=source, tags=tags,
                         source_ref=source_ref, confidence=confidence,
                         origin_class=origin_class)

    # Plan 0 Build 3 (Task 3.2 — paper fleet): per-child execution namespace.
    def create_child_execution_namespace(self, parent_id: str, child_id: str) -> Path:
        """Create and return an isolated namespace dir for a child subagent.

        Layout::

            vault/execution_<parent_id>/child_<child_id>/

        Used by the subagent fleet so each child's audit/artifacts land in a
        sandboxed subtree under its parent execution rather than the global
        vault root. Idempotent — ``mkdir(parents=True, exist_ok=True)``.
        """
        ns = self.root / f"execution_{parent_id}" / f"child_{child_id}"
        ns.mkdir(parents=True, exist_ok=True)
        return ns

    # v0.9.1 (Layer 4) — action-audit log writer + reader.
    def append_action_audit(
        self,
        entry: Dict[str, Any],
        namespace_path: Optional[Path] = None,
    ) -> None:
        """Append one audit entry to the action audit log.

        File backend: appends a JSON line to vault/audit/actions.jsonl.
        Non-file backends route to the storage layer (sqlite/postgres).

        ``entry`` MUST contain: ts (ISO), execution_id, objective_id,
        action, params (dict), success (bool), error (Optional[str]).
        ``user_id`` is optional (set in multi-user docker-enterprise mode).

        Plan 0 Build 3 (Task 3.2): when ``namespace_path`` is given (a child
        execution namespace from :meth:`create_child_execution_namespace`), the
        entry is written under ``namespace_path/audit/actions.jsonl`` instead of
        the global vault audit log. When None, behaviour is unchanged.
        """
        if namespace_path is None:
            backend = getattr(self, "_storage_backend", "file")
            if backend != "file":
                from systemu.vault.backend import dispatch_append_action_audit
                return dispatch_append_action_audit(self, entry)
            audit_dir = self.root / "audit"
        else:
            # Namespaced audit always uses the file layout under the child dir,
            # regardless of the global storage backend — the namespace is a
            # filesystem sandbox for one subagent's run.
            audit_dir = Path(namespace_path) / "audit"

        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / "actions.jsonl"
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def query_action_audit(
        self,
        *,
        execution_id: str,
        since_ts: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return audit entries matching the given filters, in append order.

        File backend: reads vault/audit/actions.jsonl line by line and filters in Python.
        Non-file backends route to the storage layer.

        Filters are AND-combined. ``since_ts`` is an ISO timestamp inclusive
        from. Returns [] if no audit log exists yet.
        """
        backend = getattr(self, "_storage_backend", "file")
        if backend != "file":
            from systemu.vault.backend import dispatch_query_action_audit
            return dispatch_query_action_audit(
                self, execution_id=execution_id,
                since_ts=since_ts, user_id=user_id,
            )

        audit_path = self.root / "audit" / "actions.jsonl"
        if not audit_path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("[Vault] skipping malformed audit line: %s", line[:100])
                continue
            if entry.get("execution_id") != execution_id:
                continue
            if user_id is not None and entry.get("user_id") != user_id:
                continue
            if since_ts is not None and entry.get("ts", "") < since_ts:
                continue
            rows.append(entry)
        return rows

    # v0.9.2 (Layer 2 Episodic Memory) — session summary writer/reader/search.
    def append_session_summary(self, summary) -> None:
        """Append one session summary to vault/episodic/sessions.jsonl (file
        backend) or the session_summaries table (sqlite/postgres dispatch).

        ``summary`` is a SessionSummary Pydantic model. Serialized via model_dump_json.
        """
        backend = getattr(self, "_storage_backend", "file")
        if backend != "file":
            from systemu.vault.backend import dispatch_append_session_summary
            return dispatch_append_session_summary(self, summary)

        ep_dir = self.root / "episodic"
        ep_dir.mkdir(parents=True, exist_ok=True)
        path = ep_dir / "sessions.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(summary.model_dump_json() + "\n")

    def query_session_summaries(
        self,
        *,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        since_ts: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[Any]:
        """Return session summaries in append order, AND-filtered.

        File backend reads + filters in Python. sqlite/postgres dispatch.
        """
        backend = getattr(self, "_storage_backend", "file")
        if backend != "file":
            from systemu.vault.backend import dispatch_query_session_summaries
            return dispatch_query_session_summaries(
                self, user_id=user_id, status=status,
                since_ts=since_ts, limit=limit,
            )

        from systemu.core.models import SessionSummary
        path = self.root / "episodic" / "sessions.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                s = SessionSummary.model_validate_json(line)
            except Exception:
                logger.warning("[Vault] skipping malformed session summary: %s", line[:100])
                continue
            if user_id is not None and s.user_id != user_id:
                continue
            if status is not None and s.status != status:
                continue
            if since_ts is not None and s.completed_at < since_ts:
                continue
            out.append(s)
            if limit is not None and len(out) >= limit:
                break
        return out

    def search_session_summaries(
        self,
        query: str,
        *,
        user_id: Optional[str] = None,
        limit: int = 5,
    ) -> List[Any]:
        """Keyword search over intent + outcome_summary + tags.

        File backend: case-insensitive substring scan. sqlite uses FTS5,
        postgres uses tsvector — both via dispatch.
        """
        backend = getattr(self, "_storage_backend", "file")
        if backend != "file":
            from systemu.vault.backend import dispatch_search_session_summaries
            return dispatch_search_session_summaries(
                self, query=query, user_id=user_id, limit=limit,
            )

        q = (query or "").lower().strip()
        if not q:
            return []
        all_sessions = self.query_session_summaries(user_id=user_id, limit=None)
        out = []
        for s in all_sessions:
            haystack = " ".join([
                s.intent or "",
                s.outcome_summary or "",
                " ".join(s.tags or []),
            ]).lower()
            if q in haystack:
                out.append(s)
                if len(out) >= limit:
                    break
        return out

    def get_latest_chat_scroll(self) -> Optional["Scroll"]:
        """Return the most recent Scroll created from a direct chat task, or None."""
        history = self.load_chat_history(limit=1)
        if not history:
            return None
        scroll_id = history[-1].get("scroll_id")
        if not scroll_id:
            return None
        try:
            return self.get_scroll(scroll_id)
        except KeyError:
            return None

    # ── Evolution ─────────────────────────────────────────────────────────────

    def save_evolution(self, evolution: Evolution) -> None:
        path = self.root / f"evolutions/evolution_{evolution.id}.json"
        self._write_json(path, evolution.model_dump(mode="json"))
        self._update_index("evolutions/index.json", _evolution_header(evolution))

    def get_evolution(self, evolution_id: str) -> Evolution:
        path = self.root / f"evolutions/evolution_{evolution_id}.json"
        data = self._read_json(path)
        if not data:
            raise KeyError(f"Evolution not found: {evolution_id}")
        return Evolution.model_validate(data)

    def list_evolutions(
        self, status: Optional[EvolutionStatus] = None
    ) -> List[Dict[str, Any]]:
        index = self.load_index("evolutions")
        if status:
            index = [e for e in index if e.get("status") == status.value]
        return index

    # ── Notification ──────────────────────────────────────────────────────────

    def queue_notification(self, notification: Notification) -> None:
        """Add a notification to the pending queue for the UI to pick up."""
        pending = self._read_json(self.root / "notifications/pending.json")
        pending.append(notification.model_dump(mode="json"))
        self._write_json(self.root / "notifications/pending.json", pending)

    def resolve_notification(self, notification_id: str, resolution: str) -> None:
        """Mark a queued notification as resolved with the chosen action."""
        path    = self.root / "notifications/pending.json"
        pending = self._read_json(path)
        for item in pending:
            if item.get("id") == notification_id:
                item["status"]     = NotificationStatus.RESOLVED.value
                item["resolution"] = resolution
        self._write_json(path, pending)

    def list_pending_notifications(self) -> List[Dict[str, Any]]:
        pending = self._read_json(self.root / "notifications/pending.json")
        return [n for n in pending if n.get("status") == NotificationStatus.PENDING.value]

    # ── Decisions (v0.8.0 Pattern 1: OperatorDecisionQueue backing) ───────

    def save_decision(self, decision) -> None:
        """Persist an OperatorDecision (upsert). Updates decisions/index.json."""
        from systemu.approval.decision_queue import OperatorDecision
        if not isinstance(decision, OperatorDecision):
            raise TypeError(f"expected OperatorDecision, got {type(decision).__name__}")
        decisions_dir = self.root / "decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        # Write the per-decision JSON
        body_path = decisions_dir / f"{decision.id}.json"
        self._write_json(body_path, decision.to_dict())
        # Upsert the header in the index
        index_path = decisions_dir / "index.json"
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        except Exception:
            idx = []
        header = {
            "id":         decision.id,
            "title":      decision.title,
            "dedup_key":  decision.dedup_key,
            "status":     decision.status,
            "options":    decision.options,
            "created_at": decision.created_at.isoformat() if decision.created_at else None,
        }
        # Remove any existing entry with the same id, then append
        idx = [h for h in idx if h.get("id") != decision.id]
        idx.append(header)
        self._write_json(index_path, idx)

    def get_decision(self, decision_id: str):
        """Load a single OperatorDecision by id."""
        from systemu.approval.decision_queue import OperatorDecision
        path = self.root / "decisions" / f"{decision_id}.json"
        if not path.exists():
            raise KeyError(f"decision {decision_id} not found at {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return OperatorDecision.from_dict(raw)
