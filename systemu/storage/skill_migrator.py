"""v0.7-c: idempotent skill-layout migrator.

Transforms Systemu's internal ``skill_skill_<hash>/SKILL.md`` layout to the
Anthropic Agent Skills spec:
  - parent dir matches the YAML ``name:`` field
  - ``name`` is lowercase, hyphen-separated (kebab-case)
  - only ``name`` and ``description`` at top level; everything else nested
    under ``metadata:`` block

Idempotent.  Safe to invoke at daemon boot.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)

_SPEC_TOP_LEVEL = {"name", "description"}


@dataclass
class MigrationReport:
    migrated: int = 0
    skipped: int = 0
    collisions: int = 0
    errors: List[str] = field(default_factory=list)


def _kebab(name: str) -> str:
    return name.replace("_", "-").lower()


def _parse(md_text: str) -> tuple[dict, str]:
    parts = md_text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return {}, md_text
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return fm, body


def _render(fm: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body


def _render_skill_md(
    *,
    name: str,
    description: str,
    metadata: dict | None,
    body: str,
) -> str:
    """Render a SKILL.md whose frontmatter is spec-conformant.

    Top-level: only `name` + `description`. Optional `metadata:` block holds
    everything Systemu-internal (category, proficiency_level, required_tools,
    etc.). Body is appended verbatim after the frontmatter terminator.
    """
    fm: dict = {"name": name, "description": description}
    if metadata:
        fm["metadata"] = metadata
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body


def _is_conformant(fm: dict, dir_name: str) -> bool:
    if "name" not in fm or "description" not in fm:
        return False
    if fm["name"] != dir_name:
        return False
    if fm["name"] != _kebab(fm["name"]):
        return False
    extras = set(fm.keys()) - _SPEC_TOP_LEVEL - {"metadata"}
    return not extras


def _migrate_one(skill_dir: Path, skills_root: Path, report: MigrationReport) -> None:
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return
    fm, body = _parse(md.read_text(encoding="utf-8"))
    name = fm.get("name", "")
    if not name:
        report.errors.append(f"{skill_dir}: missing 'name' field")
        return

    if _is_conformant(fm, skill_dir.name):
        report.skipped += 1
        return

    new_name = _kebab(name)
    new_fm = {"name": new_name, "description": fm.get("description", "")}
    metadata = fm.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    for k, v in fm.items():
        if k not in _SPEC_TOP_LEVEL and k != "metadata":
            metadata[k] = v
    if metadata:
        new_fm["metadata"] = metadata

    target_dir = skills_root / new_name
    if target_dir.exists() and target_dir != skill_dir:
        report.collisions += 1
        logger.warning(
            "[skill_migrator] collision: %s -> %s already exists, skipping",
            skill_dir.name, new_name,
        )
        return

    if target_dir == skill_dir:
        md.write_text(_render(new_fm, body), encoding="utf-8")
    else:
        target_dir.mkdir(parents=True, exist_ok=False)
        for f in list(skill_dir.iterdir()):
            f.rename(target_dir / f.name)
        (target_dir / "SKILL.md").write_text(_render(new_fm, body), encoding="utf-8")
        skill_dir.rmdir()

    report.migrated += 1
    logger.info("[skill_migrator] migrated %s -> %s", skill_dir.name, new_name)


def migrate_skill_layout(vault_dir: Path) -> MigrationReport:
    """Idempotent. Returns a report; never raises on per-skill errors."""
    report = MigrationReport()
    skills_root = Path(vault_dir) / "skills"
    if not skills_root.exists():
        return report
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        try:
            _migrate_one(skill_dir, skills_root, report)
        except Exception as exc:
            report.errors.append(f"{skill_dir.name}: {exc}")
            logger.exception("[skill_migrator] failed for %s", skill_dir.name)
    return report
