"""v0.9.4 Layer 5 — skill_loader.

Parses SKILL.md files (YAML frontmatter + markdown body) into ``SkillManifest``
Pydantic models. Gates by:
- prerequisites.commands (CLI binaries on PATH, via shutil.which)
- requires_toolsets (which toolsets the runtime has registered)

Hermes pattern: skills/<category>/<name>/SKILL.md
Odysseus addition: requires_toolsets + fallback_for_toolsets fields.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from systemu.core.models import SkillManifest

logger = logging.getLogger(__name__)


class SkillManifestError(ValueError):
    """Raised when a SKILL.md file is malformed or missing required fields."""


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)


def _parse_yaml_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter_dict, body_str).

    Uses PyYAML if available; falls back to a tiny hand-rolled parser for
    the simple flat shape we need (no nested dicts beyond ``metadata.systemu``,
    no anchors, no multi-line strings).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillManifestError(
            "SKILL.md must begin with YAML frontmatter delimited by '---' lines"
        )
    yaml_text, body = m.group(1), m.group(2)
    try:
        import yaml  # type: ignore
    except ImportError:
        raise SkillManifestError(
            "PyYAML is required to parse SKILL.md frontmatter — install via 'pip install pyyaml'"
        )
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise SkillManifestError(f"YAML frontmatter parse failed: {exc}")
    if not isinstance(data, dict):
        raise SkillManifestError(
            f"YAML frontmatter must be a dict at top level (got {type(data).__name__})"
        )
    return data, body


def parse_skill_md(path: Union[str, Path]) -> SkillManifest:
    """Read a SKILL.md file and return a parsed SkillManifest.

    Raises SkillManifestError on malformed input.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    frontmatter, body = _parse_yaml_frontmatter(text)

    # Required fields
    for required in ("name", "description", "version"):
        if required not in frontmatter:
            raise SkillManifestError(f"SKILL.md missing required field: {required}")

    # Hermes-style metadata.systemu.{tags, related_skills}
    metadata = frontmatter.get("metadata") or {}
    systemu_meta = (metadata.get("systemu") or {}) if isinstance(metadata, dict) else {}
    tags = systemu_meta.get("tags") or frontmatter.get("tags") or []
    related = systemu_meta.get("related_skills") or frontmatter.get("related_skills") or []

    # Prerequisites
    prereqs = frontmatter.get("prerequisites") or {}
    prereqs_commands = (prereqs.get("commands") or []) if isinstance(prereqs, dict) else []

    try:
        manifest = SkillManifest(
            name=str(frontmatter["name"]),
            description=str(frontmatter["description"]),
            version=str(frontmatter["version"]),
            platforms=[str(x) for x in (frontmatter.get("platforms") or [])],
            tags=[str(x) for x in (tags or [])],
            related_skills=[str(x) for x in (related or [])],
            prerequisites_commands=[str(x) for x in prereqs_commands],
            requires_toolsets=[str(x) for x in (frontmatter.get("requires_toolsets") or [])],
            fallback_for_toolsets=[str(x) for x in (frontmatter.get("fallback_for_toolsets") or [])],
            body=body,
            source_path=str(p),
        )
    except Exception as exc:
        raise SkillManifestError(f"failed to construct SkillManifest: {exc}")
    return manifest


def discover_skills(root: Union[str, Path]) -> List[SkillManifest]:
    """Walk ``root`` and parse every ``SKILL.md`` found one level deep.

    Hermes layout: ``<root>/<skill-name>/SKILL.md``.
    Returns [] if root doesn't exist.
    """
    rp = Path(root)
    if not rp.exists() or not rp.is_dir():
        return []
    manifests: List[SkillManifest] = []
    for entry in sorted(rp.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            manifests.append(parse_skill_md(skill_md))
        except SkillManifestError as exc:
            logger.warning("[SkillLoader] skipping malformed %s: %s", skill_md, exc)
    return manifests


def check_prerequisites(manifest: Union[SkillManifest, Dict[str, Any]]) -> bool:
    """Return True if every command in ``prerequisites_commands`` resolves on PATH."""
    if isinstance(manifest, SkillManifest):
        commands = manifest.prerequisites_commands
    else:
        commands = manifest.get("prerequisites_commands") or []
    for cmd in commands:
        if not shutil.which(str(cmd)):
            return False
    return True


def check_toolsets(
    manifest: Union[SkillManifest, Dict[str, Any]],
    available_toolsets: Set[str],
) -> bool:
    """Return True if every toolset in ``requires_toolsets`` is present in ``available_toolsets``."""
    if isinstance(manifest, SkillManifest):
        required = manifest.requires_toolsets
    else:
        required = manifest.get("requires_toolsets") or []
    return all(t in available_toolsets for t in required)


def load_all_skills(bundled_dir: str, user_dir: str) -> List[SkillManifest]:
    """Combine bundled + user-installed skills into one list.

    User skills with the same name as bundled skills SHADOW the bundled
    version (user dir wins). Empty strings are treated as 'no directory'.
    """
    out: Dict[str, SkillManifest] = {}
    for d in [s for s in (bundled_dir, user_dir) if s]:
        for m in discover_skills(d):
            out[m.name] = m  # later wins (user_dir overrides bundled)
    return list(out.values())
