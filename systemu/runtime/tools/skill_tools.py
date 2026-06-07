"""v0.9.4 Layer 5 — skill_list_skills + skill_view_skill LLM tools.

Registered via the v0.9.3 v2 tool registry. Read-only — gives the LLM
visibility into what bundled and user-installed recipes are available.

Pattern mirrors v0.9.2 session_tools and v0.9.3 capability_tools, but
these are actually registered (the prior modules ship infrastructure only;
this is the first batch of code-registered tools beyond file_tools).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from systemu.runtime.skill_loader import load_all_skills
from systemu.runtime.tool_registry_v2 import registry


def _to_lightweight(manifest) -> Dict[str, Any]:
    """Compact dict for search/list results — token-efficient."""
    return {
        "name": manifest.name,
        "description": manifest.description,
        "version": manifest.version,
        "tags": list(manifest.tags or []),
        "requires_toolsets": list(manifest.requires_toolsets or []),
    }


def _to_full(manifest) -> Dict[str, Any]:
    """Full dict including the markdown body — used by view."""
    d = _to_lightweight(manifest)
    d.update({
        "platforms": list(manifest.platforms or []),
        "related_skills": list(manifest.related_skills or []),
        "prerequisites_commands": list(manifest.prerequisites_commands or []),
        "fallback_for_toolsets": list(manifest.fallback_for_toolsets or []),
        "body": manifest.body,
        "source_path": manifest.source_path,
    })
    return d


def skill_list_skills(*, config) -> List[Dict[str, Any]]:
    """List all SKILL.md recipes from bundled + user directories.

    Gated by ``config.skill_loader_enabled``. Returns lightweight dicts
    (no body) so the LLM can scan without burning tokens.
    """
    if not getattr(config, "skill_loader_enabled", True):
        return []
    bundled = getattr(config, "skills_bundled_dir", "") or ""
    user = getattr(config, "skills_user_dir", "") or ""
    manifests = load_all_skills(bundled, user)
    return [_to_lightweight(m) for m in manifests]


def skill_view_skill(*, name: str, config) -> Optional[Dict[str, Any]]:
    """Return the full manifest (including body) for one named skill.

    None if not found. Gated by ``config.skill_loader_enabled``.
    """
    if not getattr(config, "skill_loader_enabled", True):
        return None
    bundled = getattr(config, "skills_bundled_dir", "") or ""
    user = getattr(config, "skills_user_dir", "") or ""
    manifests = load_all_skills(bundled, user)
    for m in manifests:
        if m.name == name:
            return _to_full(m)
    return None


# ── Schemas for v2 registry ──────────────────────────────────────────

_LIST_SKILLS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
}

_VIEW_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The skill name (e.g. 'burrito-delivery')."},
    },
    "required": ["name"],
}


def _list_handler(**kwargs) -> Dict[str, Any]:
    """v2 registry handler — wraps skill_list_skills with a Config built from env."""
    from sharing_on.config import Config
    cfg = Config.from_env()
    return {"success": True, "skills": skill_list_skills(config=cfg)}


def _view_handler(**kwargs) -> Dict[str, Any]:
    """v2 registry handler — wraps skill_view_skill with a Config built from env."""
    from sharing_on.config import Config
    cfg = Config.from_env()
    name = kwargs.get("name", "")
    result = skill_view_skill(name=name, config=cfg)
    if result is None:
        return {"success": False, "error": f"skill not found: {name!r}"}
    return {"success": True, "skill": result}


# ── Module-level registrations (AST-scan discovery picks these up) ──

registry.register(
    name="skill_list_skills", toolset="skill",
    schema=_LIST_SKILLS_SCHEMA, handler=_list_handler,
    description="List all loadable SKILL.md recipes from bundled + user directories.",
    is_action_tool=False,
    max_result_size_chars=50_000,
)

registry.register(
    name="skill_view_skill", toolset="skill",
    schema=_VIEW_SKILL_SCHEMA, handler=_view_handler,
    description="Return the full body + metadata for a named SKILL.md recipe.",
    is_action_tool=False,
    max_result_size_chars=100_000,
)
