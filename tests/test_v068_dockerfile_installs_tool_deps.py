from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _runtime_dockerfile() -> Path:
    """this project's runtime image is the repo-root ``Dockerfile``
    (referenced as ``build: .`` from every service in docker-compose.yml).
    Prefer ``docker/Dockerfile.runtime`` if a future split lands."""
    split = REPO / "docker" / "Dockerfile.runtime"
    if split.exists():
        return split
    return REPO / "Dockerfile"


def test_dockerfile_runtime_installs_tool_deps():
    dockerfile = _runtime_dockerfile().read_text(encoding="utf-8")
    assert "requirements-tools.txt" in dockerfile, \
        "runtime Dockerfile must COPY tools/requirements-tools.txt"
    # Look for a pip install line referencing the file
    lines = dockerfile.splitlines()
    has_install = any(
        "pip install" in line.lower() and "requirements-tools.txt" in line
        for line in lines
    )
    if not has_install:
        # Check for a multi-line RUN that references it
        has_install = "pip install" in dockerfile and "requirements-tools.txt" in dockerfile
    assert has_install, "runtime Dockerfile must `pip install -r ...requirements-tools.txt`"
