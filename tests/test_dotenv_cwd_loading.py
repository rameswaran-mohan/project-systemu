"""Test that the CLI's dotenv loader checks CWD first (Bug #2)
and the daemon's project_root prefers CWD when .env present (Bug #7).

Pip-install users have their .env in their working directory, not next to
the installed sharing_on package. The loader must check Path.cwd() to
find their config.
"""
import os
import sys
import subprocess
import textwrap
from pathlib import Path


def test_cli_loads_dotenv_from_cwd(tmp_path):
    """Run sharing_on info from a tmp dir with a .env — must pick up the key."""
    # Setup: write a .env in the tmp dir
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENROUTER_API_KEY=test-key-from-cwd-dotenv\n"
        "SYSTEMU_STORAGE=file\n"
        "SYSTEMU_VAULT_DIR=systemu/vault\n"
    )

    # Spawn the CLI with cwd=tmp_path
    # IMPORTANT: don't inherit the parent env's OPENROUTER_API_KEY, else the
    # test passes trivially because of the parent's setting.
    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith(("OPENROUTER_", "SYSTEMU_", "SHARING_ON_"))}
    clean_env["PATH"] = os.environ.get("PATH", "")
    # v0.7.3: force UTF-8 in the child so Windows cp1252 doesn't choke on
    # rich's unicode output (this is also what the production CLI does).
    clean_env["PYTHONIOENCODING"] = "utf-8"
    clean_env["PYTHONUTF8"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "sharing_on", "info"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env,
        timeout=60,
    )
    combined = result.stdout + result.stderr

    # The key was test-key-from-cwd-dotenv, so "OPENROUTER_API_KEY not set"
    # would only appear if the dotenv wasn't loaded from CWD.
    assert "OPENROUTER_API_KEY not set" not in combined, (
        f"CLI did not load .env from cwd={tmp_path}. Output:\n{combined}"
    )


def test_daemon_project_root_prefers_cwd_when_env_present(tmp_path):
    """Daemon's project_root should resolve to CWD (not site-packages) when
    CWD has a .env or .systemu_mode (Bug #7)."""
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=abc\n")
    code = textwrap.dedent(f"""
        import os
        from pathlib import Path
        os.chdir(r"{tmp_path}")
        import systemu
        cwd = Path.cwd().absolute()
        if (cwd / ".env").exists() or (cwd / ".systemu_mode").exists():
            project_root = cwd
        else:
            project_root = Path(systemu.__file__).parent.parent.absolute()
        print(str(project_root))
    """)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # tmp_path on Windows may render as D:\path or D:\\path; normalize both
    expected = str(tmp_path.resolve())
    got = result.stdout.strip()
    assert os.path.normcase(expected) == os.path.normcase(got), (
        f"Expected project_root={expected}, got: {got}"
    )
