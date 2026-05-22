"""skills exported by Systemu must pass the upstream agentskills.io
spec validator (skills-ref)."""
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

pytest.importorskip("yaml")
skills_ref = shutil.which("skills-ref")
if not skills_ref:
    pytest.skip(
        "skills-ref CLI not installed; in CI it lands via the validator workflow",
        allow_module_level=True,
    )


def test_exported_skill_passes_spec_validator(tmp_path):
    from systemu.pipelines.skill_exporter import export_skill
    skill = MagicMock()
    skill.id = "skill_x"
    skill.name = "csv-cleanup"
    skill.description = "Tidy a messy CSV by stripping bad rows and re-typing columns."
    skill.category = "data"
    skill.required_tools = ["read_csv", "write_csv"]
    skill.instructions_md = "## Steps\n\n1. Load\n2. Clean\n3. Save\n"
    vault = MagicMock()
    vault.get_skill.return_value = skill

    out = export_skill(skill_id="skill_x", target_dir=tmp_path, vault=vault)
    proc = subprocess.run(
        [skills_ref, "validate", str(out)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"skills-ref validate FAILED:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
