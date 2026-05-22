"""regression: docker-compose outputs mount must use
${SYSTEMU_HOST_OUTPUTS_DIR} so install.py can supply an absolute host path.

On Docker Desktop for Windows, a relative path like ``./outputs`` silently
degrades to a named volume — the .docx files end up invisible on the host.
Parameterising via env var lets install.py supply an absolute host path
(with forward slashes) that Docker Desktop reliably honors as a bind mount.
"""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _has_param_outputs_mount(volumes: list) -> bool:
    return any(
        isinstance(v, str)
        and "/app/systemu/outputs" in v
        and "${SYSTEMU_HOST_OUTPUTS_DIR" in v
        for v in volumes
    )


def _has_any_outputs_mount(volumes: list) -> bool:
    return any(
        isinstance(v, str) and "/app/systemu/outputs" in v
        for v in volumes
    )


def test_compose_outputs_mount_uses_env_var():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose.get("services", {})
    inspected = 0
    for svc_name, svc in services.items():
        volumes = (svc.get("volumes") or [])
        if _has_any_outputs_mount(volumes):
            inspected += 1
            assert _has_param_outputs_mount(volumes), (
                f"service {svc_name} mounts outputs but not via "
                f"${{SYSTEMU_HOST_OUTPUTS_DIR}}"
            )
    assert inspected >= 1, "expected at least one service to mount /app/systemu/outputs"
