"""v0.7-d: export an internal Skill record as a spec-conformant Agent Skills bundle.

Reads a Skill via ``vault.get_skill(skill_id)`` and writes a portable
``SKILL.md`` (Anthropic Agent Skills Standard) into
``target_dir/<kebab-name>/SKILL.md``.

Spec-conformant means:
  - Directory name is kebab-cased (``email-summary`` not ``email_summary``).
  - Top-level YAML frontmatter has ONLY ``name`` and ``description``.
  - Systemu-internal fields (category, proficiency_level, required tools,
    target_outcomes, produces, effectiveness_score) live under
    ``metadata:`` — the upstream validator ignores unknown metadata keys.
  - The instructions body is whatever the operator authored in the vault.
"""
from __future__ import annotations
from pathlib import Path

import yaml


def _kebab(name: str) -> str:
    return name.replace("_", "-").lower()


def export_skill(*, skill_id: str, target_dir: Path, vault) -> Path:
    """Export the named Skill as a spec-conformant directory under target_dir.

    Returns the path to the new skill directory (``target_dir/<kebab-name>/``).
    Raises:
        KeyError: if the skill doesn't exist in the vault.
        FileExistsError: if the target directory already contains a SKILL.md.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    skill = vault.get_skill(skill_id)  # may raise KeyError

    name = _kebab(skill.name)
    out_dir = target_dir / name
    if (out_dir / "SKILL.md").exists():
        raise FileExistsError(f"refuse to clobber existing {out_dir / 'SKILL.md'}")
    out_dir.mkdir(exist_ok=True)

    fm = {"name": name, "description": skill.description}
    metadata = {}
    # ``required_tools`` is the human-facing name used by exported bundles;
    # the internal Skill model spells it ``required_tool_names`` (vault rows
    # carry ``required_tool_ids`` alongside).  Accept either so the exporter
    # works with both Pydantic Skill objects and ad-hoc MagicMock fixtures.
    for attr in (
        "category",
        "proficiency_level",
        "required_tools",
        "required_tool_names",
        "target_outcomes",
        "produces",
        "effectiveness_score",
    ):
        val = getattr(skill, attr, None)
        if val is None or val == "" or val == [] or val == {}:
            continue
        # Skip values that aren't YAML-representable (e.g. MagicMock
        # auto-attrs on test fixtures that didn't pre-set every field).
        if not isinstance(val, (str, int, float, bool, list, dict, tuple)):
            continue
        # Normalise the dual-spelling tool list under the spec-style key.
        key = "required_tools" if attr == "required_tool_names" else attr
        metadata.setdefault(key, val)
    if metadata:
        fm["metadata"] = metadata

    body = (getattr(skill, "instructions_md", None) or "").strip() or "Body."
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body + "\n"
    (out_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return out_dir
