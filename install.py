"""Systemu unified installer.

Single entry point for fresh installs and reconfigures.  Asks the operator
which deployment mode they want, then sets up everything needed:

  local             — native venv, SQLite vault, Huey-SQLite queue, daemon +
                      worker run as detached subprocesses on the host.
  docker-local      — docker-compose, Postgres vault, Huey-SQLite queue.
  docker-enterprise — docker-compose, Postgres vault, Huey-Redis queue,
                      scaled workers.

stdlib-only by design — must run before any venv is created.

Usage:
  python install.py                    # interactive
  python install.py --mode local
  python install.py --mode docker-enterprise --non-interactive \\
      --pg-password=hunter2 --redis-password=hunter3 --worker-replicas=3
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent
ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
MODE_MARKER = REPO_ROOT / ".systemu_mode"
DATA_DIR = REPO_ROOT / "data"

VALID_MODES = ("local", "docker-local", "docker-enterprise")

# Force stdout/stderr to UTF-8 on Windows so box-drawing chars and check marks
# don't trip the legacy cp1252 codec when invoked from a non-Windows-Terminal
# parent process (e.g. CI subprocess, old cmd.exe, pytest capture).  Python
# 3.7+ supports reconfigure(); older interpreters fall through silently.
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ─── ANSI colours ───────────────────────────────────────────────────────────

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        # Modern Windows terminals support ANSI; legacy cmd.exe doesn't.
        return os.environ.get("WT_SESSION") is not None or os.environ.get("ANSICON") is not None
    return sys.stdout.isatty()


_COLOR = _supports_color()
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s
def bold(s: str) -> str:    return _c("1", s)
def dim(s: str) -> str:     return _c("2", s)
def green(s: str) -> str:   return _c("32", s)
def yellow(s: str) -> str:  return _c("33", s)
def red(s: str) -> str:     return _c("31", s)
def cyan(s: str) -> str:    return _c("36", s)


def header(title: str) -> None:
    print()
    print(bold(cyan(f" Systemu — {title}")))
    print(cyan(" " + "─" * (len(title) + 11)))
    print()


def info(msg: str) -> None:    print(f" {dim('•')} {msg}")
def success(msg: str) -> None: print(f" {green('✓')} {msg}")
def warn(msg: str) -> None:    print(f" {yellow('!')} {msg}")
def error(msg: str) -> None:   print(f" {red('✗')} {msg}", file=sys.stderr)


# ─── Prompts ────────────────────────────────────────────────────────────────

def prompt(question: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            ans = input(f" {question}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if ans:
            return ans
        if default is not None:
            return default


def prompt_choice(question: str, choices: list[tuple[str, str]], default_idx: int = 0) -> str:
    print(f" {question}")
    for i, (key, descr) in enumerate(choices, 1):
        marker = green("→") if (i - 1) == default_idx else " "
        print(f"   {marker} {bold(str(i))}. {bold(key)}  {dim('—')} {descr}")
    while True:
        ans = prompt("Choice", default=str(default_idx + 1))
        if ans.isdigit() and 1 <= int(ans) <= len(choices):
            return choices[int(ans) - 1][0]
        if ans in [k for k, _ in choices]:
            return ans
        warn(f"Invalid choice — pick 1..{len(choices)}")


def prompt_password(question: str, default_random: bool = True) -> str:
    if default_random:
        suggested = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
        ans = prompt(f"{question} (blank = generate)", default="")
        return ans if ans else suggested
    return prompt(question)


# ─── Tooling checks ─────────────────────────────────────────────────────────

def check_python_version() -> None:
    if sys.version_info < (3, 10):
        error(f"Python 3.10+ required (you have {sys.version_info.major}.{sys.version_info.minor})")
        # OS-specific upgrade hint.  Debian 11 (still common on
        # bare-metal servers) ships 3.9 by default — users hit the version
        # check and stall without knowing how to upgrade.
        print()
        if sys.platform.startswith("linux"):
            print("  Install Python 3.11 with your package manager:")
            print("    " + bold("sudo apt install python3.11 python3.11-venv") + dim("   # Debian / Ubuntu"))
            print("    " + bold("sudo dnf install python3.11") + dim("                       # Fedora / RHEL"))
            print("  Then re-run:  " + bold("python3.11 install.py"))
        elif sys.platform == "darwin":
            print("  Install Python 3.11 with Homebrew:")
            print("    " + bold("brew install python@3.11"))
            print("  Then re-run:  " + bold("python3.11 install.py"))
        elif sys.platform == "win32":
            print("  Install Python 3.11 from python.org:")
            print("    " + bold("https://www.python.org/downloads/"))
            print("  Or via winget:  " + bold("winget install Python.Python.3.11"))
        sys.exit(1)
    success(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


def check_command(cmd: str, friendly: Optional[str] = None) -> bool:
    found = shutil.which(cmd) is not None
    label = friendly or cmd
    if found:
        success(f"{label} on PATH")
    else:
        error(f"{label} not found on PATH")
    return found


def check_linux_capture_deps() -> list[str]:
    """return list of missing Linux capture deps (xdotool, xclip).

    Returns empty list on non-Linux platforms or when all deps are present.
    Used by setup_local() to print apt-install hint to the operator.

    These tools are required for pynput-based keyboard/clipboard capture
    on Linux.  Without them, sharing_on records empty event streams from
    the desktop session (daemon + worker themselves are unaffected).
    """
    if not sys.platform.startswith("linux"):
        return []
    missing = []
    for tool in ("xdotool", "xclip"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def playwright_install_args() -> list[str]:
    """return platform-appropriate args for `python -m playwright ...`.

    Linux gets --with-deps so playwright pulls Chromium's OS libraries
    (libnss3, libatk1.0-0, etc.) via apt.  Without --with-deps the browser
    binary downloads but fails to launch when sharing_on or forged tools
    invoke it.

    macOS + Windows: --with-deps is a no-op (or errors) — Chromium is
    self-contained on those platforms.
    """
    if sys.platform.startswith("linux"):
        return ["install", "--with-deps", "chromium"]
    return ["install", "chromium"]


def detect_proxy_config() -> Dict[str, str]:
    """read HTTP_PROXY / HTTPS_PROXY env vars (lowercase variants too).

    Returns a dict like ``{"http": "...", "https": "..."}`` suitable for
    passing to urllib.request via ProxyHandler.  Empty dict when no proxy
    is set.  Uppercase env vars take precedence over lowercase, matching
    curl + pip behaviour.

    pip and Playwright auto-read these env vars themselves — we don't need
    to forward them to subprocess.  We detect + echo them so the operator
    sees that their corporate proxy was honored.
    """
    proxies: Dict[str, str] = {}
    for env_key, dest_key in (
        ("HTTPS_PROXY", "https"),
        ("HTTP_PROXY",  "http"),
        ("https_proxy", "https"),
        ("http_proxy",  "http"),
    ):
        v = os.environ.get(env_key)
        if v and dest_key not in proxies:
            proxies[dest_key] = v
    return proxies


def _mask_proxy_url(url: str) -> str:
    """Mask the password segment of a proxy URL for safe stdout printing.

    ``http://user:secret@host:3128``  ->  ``http://user:****@host:3128``
    URLs with no credentials are returned unchanged.
    """
    return re.sub(
        r"^(\w+://[^:/@]+:)([^@]+)(@)",
        r"\1****\3",
        url,
    )


def validate_openrouter_key(key: str, *, proxies: Dict[str, str]) -> tuple[bool, str]:
    """probe OpenRouter to confirm the API key works.

    Returns ``(True, "")`` on success; ``(False, "<reason>")`` otherwise.

    Uses ``GET /api/v1/models`` — cheapest documented endpoint, no tokens
    burned.  10s timeout so air-gapped users fail fast.  Honors HTTP_PROXY
    / HTTPS_PROXY via the passed-in proxies dict.
    """
    if not key:
        return False, "Key is empty"

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "systemu-installer/0.6.3",
        },
    )
    handler = urllib.request.ProxyHandler(proxies if proxies else {})
    opener = urllib.request.build_opener(handler)

    try:
        with opener.open(req, timeout=10) as resp:
            if resp.status == 200:
                return True, ""
            return False, f"Unexpected HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Invalid key (HTTP 401 from OpenRouter)"
        return False, f"HTTP error {e.code}"
    except urllib.error.URLError as e:
        return False, f"Connection failed: {e.reason}"
    except Exception as e:  # noqa: BLE001 — install-time best-effort
        return False, f"Network error: {type(e).__name__}: {e}"


def _resolve_outputs_host_dir() -> Path:
    """Pick an absolute host path for the outputs volume.

    on Docker Desktop Windows, default to ~/SystemuOutputs.  The user
    home is auto-shared by Docker Desktop on Windows, so the bind propagates
    without additional File-sharing config.  On Linux/macOS, project-relative
    ./outputs is fine because the Docker daemon shares the project dir by
    default.
    """
    import platform
    if platform.system() == "Windows":
        p = Path.home() / "SystemuOutputs"
        p.mkdir(exist_ok=True)
        # Docker Desktop on Windows accepts forward slashes for absolute paths.
        return Path(str(p).replace("\\", "/"))
    p = Path("outputs").resolve()
    p.mkdir(exist_ok=True)
    return p


def _run_bind_smoke_test(host_outputs_dir: Path) -> tuple[bool, str]:
    """After docker compose up, write a probe file from inside a
    throwaway container and check it appears on the host filesystem.

    Returns (success, message). When success is False, the message contains
    actionable repair instructions for the operator (Docker Desktop file
    sharing config).
    """
    import subprocess
    import time

    probe_name = f".v069_bind_probe_{int(time.time())}.txt"

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{host_outputs_dir}:/app/systemu/outputs",
        "alpine:latest", "sh", "-c",
        f"echo bindprobe > /app/systemu/outputs/{probe_name}",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=30, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, f"smoke test container failed to run: {exc}"
    except FileNotFoundError:
        # docker binary missing — can't smoke-test, treat as inconclusive
        return True, "docker binary not found; skipped smoke test"

    # Give Docker Desktop a moment to propagate.
    time.sleep(2)

    host_file = host_outputs_dir / probe_name
    if not host_file.exists():
        msg = (
            f"\n[v0.6.9] Bind mount smoke test FAILED.\n"
            f"  Container wrote to /app/systemu/outputs/{probe_name}\n"
            f"  Host path: {host_file}\n"
            f"  Status: probe file did NOT appear on host.\n\n"
            f"  This is a Docker Desktop file-sharing issue.  Fix:\n"
            f"  1. Open Docker Desktop -> Settings -> Resources -> File sharing\n"
            f"  2. Add this path: {host_outputs_dir.parent}\n"
            f"  3. Apply & restart Docker Desktop\n"
            f"  4. Re-run: docker compose --profile <local|enterprise> up -d\n"
        )
        return False, msg

    try:
        host_file.unlink()
    except Exception:
        pass
    return True, f"bind mount verified: {host_outputs_dir} <-> /app/systemu/outputs"


def is_apple_silicon() -> bool:
    """True when running on Apple Silicon (M1/M2/M3/M4 Mac).

    Used to surface a "less-tested codepath" banner during install — most
    of systemu has been validated on Intel Mac + Linux x86_64 + Windows
    x86_64.  Apple Silicon mostly works but has known edge cases around
    Playwright (Rosetta) and some PyObjC-using deps.
    """
    import platform
    return sys.platform == "darwin" and platform.machine() == "arm64"


def print_apple_silicon_banner() -> None:
    """info banner when running install on Apple Silicon.

    Non-blocking — just lets the operator know they're on a less-tested
    codepath and where to look if something breaks.
    """
    if not is_apple_silicon():
        return
    print()
    print(bold(cyan(" Apple Silicon (ARM64) detected")))
    print(cyan(" " + "─" * 32))
    print()
    print("  systemu's install path is validated on Intel Mac + Linux x86_64 +")
    print("  Windows x86_64.  ARM64 mostly works but has known edge cases:")
    print()
    print("    * Playwright sometimes needs Rosetta if Chromium binaries lag")
    print("    * Some PyObjC-using deps require recompilation on M-series")
    print()
    print(dim("  If install fails, set ARCHFLAGS to force x86_64 emulation:"))
    print(dim("    arch -x86_64 python install.py"))
    print()


def print_macos_permissions_guide() -> None:
    """macOS — print the System Settings paths needed for capture.

    pynput's keyboard/clipboard hooks require Accessibility.  Screen capture
    in sharing_on requires Screen Recording.  Both are silent no-ops without
    the grant — install completes "successfully" but capture records empty
    event streams.

    No programmatic detection — tccutil lacks a read API, and PyObjC would
    be a hefty new dep just to check two booleans.  Print the paths and let
    the user verify by running a capture session after install.
    """
    if sys.platform != "darwin":
        return
    print()
    print(bold(yellow(" macOS capture permissions — required for sharing_on")))
    print(yellow(" " + "─" * 56))
    print()
    print("  sharing_on records desktop activity (keyboard, clipboard, screen)")
    print("  via pynput + screen capture.  Both need explicit macOS grants:")
    print()
    print("    1. " + bold("Accessibility") + " (keyboard + clipboard capture)")
    print("       System Settings -> Privacy & Security -> Accessibility")
    print("       Click + and add: " + bold("Terminal") + " (or whichever app runs start.sh)")
    print()
    print("    2. " + bold("Screen Recording") + " (screenshot capture)")
    print("       System Settings -> Privacy & Security -> Screen Recording")
    print("       Click + and add: " + bold("Terminal"))
    print()
    print(dim("  Without these, the daemon will still run, but sharing_on"))
    print(dim("  captures will record empty event streams.  Grant the perms,"))
    print(dim("  then restart the daemon:  ./stop.sh && ./start.sh"))
    print()


def check_linux_pyatspi() -> bool:
    """probe whether AT-SPI Python bindings are importable.

    Returns True on non-Linux platforms (not applicable) or when the
    ``pyatspi`` module can be located via importlib.  Returns False with
    an apt/dnf install hint printed to stdout when on Linux and missing.

    AT-SPI bindings are required for UI introspection — without them,
    certain accessibility-driven tools silently degrade to no-ops.  We
    use ``importlib.util.find_spec`` instead of ``import pyatspi`` so we
    don't accidentally trigger heavy GObject initialisation just to check
    presence.
    """
    if not sys.platform.startswith("linux"):
        return True

    import importlib.util
    if importlib.util.find_spec("pyatspi") is not None:
        return True

    warn(
        "AT-SPI Python bindings (pyatspi) not found.  Tools that introspect "
        "running applications via accessibility APIs will silently degrade.  "
        "Install with:\n"
        "   sudo apt install python3-pyatspi          # Debian / Ubuntu\n"
        "   sudo dnf install pyatspi                  # Fedora / RHEL"
    )
    return False


def detect_wayland_session() -> bool:
    """return True when running on Linux Wayland (capture broken).

    pynput requires X11.  Ubuntu 22.04+ and Fedora Workstation default to
    Wayland — on those sessions, sharing_on capture records empty event
    streams.  The daemon, dashboard, and tool execution are unaffected;
    only the capture pipeline is impacted.

    Detection: $XDG_SESSION_TYPE env var (set by every modern Linux DE).
    """
    if not sys.platform.startswith("linux"):
        return False
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland"


def check_docker() -> bool:
    if not check_command("docker"):
        return False
    # `docker compose` (V2) is preferred; `docker-compose` (V1) is legacy.
    try:
        r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if r.returncode == 0:
            success(f"docker compose: {r.stdout.strip().splitlines()[0]}")
            return True
    except FileNotFoundError:
        pass
    error("`docker compose` (V2 plugin) not available — install it before continuing.")
    return False


# ─── .env writing ───────────────────────────────────────────────────────────

def render_env(values: Dict[str, str]) -> str:
    """Render an .env file from a flat key→value dict.

    Values are quoted when they contain whitespace, '#', or any character that
    a naive shell might mis-parse.  Keys are emitted in insertion order so the
    file stays human-diffable.
    """
    out: list[str] = []
    for k, v in values.items():
        s = str(v)
        if any(c in s for c in (" ", "\t", "#", "$", "\"", "'", "\\")):
            s = '"' + s.replace("\\", "\\\\").replace("\"", "\\\"") + '"'
        out.append(f"{k}={s}")
    return "\n".join(out) + "\n"


# registry of env vars that were renamed in prior releases.
# Keep entries here for at least one minor release after the rename so
# operators upgrading via `git pull` see a clear migration message.
_RENAMED_ENV_VARS: Dict[str, str] = {
    # SYSTEMU_AUTO_APPROVE_SCROLLS lied about scope (cascaded to
    # every notify_user prompt).  Renamed to SYSTEMU_NON_INTERACTIVE.
    "SYSTEMU_AUTO_APPROVE_SCROLLS": "SYSTEMU_NON_INTERACTIVE",
}


def detect_stale_env_vars() -> Dict[str, str]:
    """return {old_name: new_name} for any renamed env vars
    present in the operator's .env but for which the new name is NOT
    also present.

    Used by setup_local/setup_docker_* to print a one-time migration
    prompt during reconfigure.  Empty dict when:
      * .env doesn't exist (fresh install)
      * No renamed vars are in the file
      * Both old and new names are present (operator is mid-migration —
        the new name already takes effect; don't nag).
    """
    if not ENV_PATH.exists():
        return {}
    present: set[str] = set()
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", s)
        if m:
            present.add(m.group(1))
    stale: Dict[str, str] = {}
    for old, new in _RENAMED_ENV_VARS.items():
        if old in present and new not in present:
            stale[old] = new
    return stale


def merge_existing_env(values_or_path, new_vars: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Carry over any existing .env keys that the new values don't supply.

    Lets the operator preserve custom overrides (proxy URLs, API keys, etc.)
    across reconfigures.

    Two call forms are supported:
      * ``merge_existing_env(values)`` — legacy: read from module-level ``ENV_PATH``.
      * ``merge_existing_env(path, new_vars=...)`` — explicit env file path.
    """
    if isinstance(values_or_path, (str, Path)):
        env_path = Path(values_or_path)
        values: Dict[str, str] = dict(new_vars or {})
    else:
        env_path = ENV_PATH
        values = values_or_path  # type: ignore[assignment]

    if not env_path.exists():
        return values
    existing: Dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", s)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1]
        existing[key] = raw

    # auto-migrate deprecated env keys so the operator's .env
    # converges on the canonical names.  Don't overwrite explicit values
    # of the new key.
    DEPRECATED_RENAMES = {
        "SYSTEMU_AUTO_APPROVE_SCROLLS": "SYSTEMU_NON_INTERACTIVE",
    }
    for _old, _new in DEPRECATED_RENAMES.items():
        if _old in existing:
            _old_val = existing.pop(_old)
            if _new not in existing:
                existing[_new] = _old_val

    merged = dict(existing)
    merged.update(values)
    return merged


def write_env(values: Dict[str, str]) -> None:
    final = merge_existing_env(values)
    ENV_PATH.write_text(render_env(final), encoding="utf-8")
    success(f"Wrote {ENV_PATH.relative_to(REPO_ROOT)}")


def write_mode_marker(mode: str) -> None:
    MODE_MARKER.write_text(mode + "\n", encoding="utf-8")
    success(f"Wrote {MODE_MARKER.relative_to(REPO_ROOT)} ({mode})")


def read_existing_mode() -> Optional[str]:
    if MODE_MARKER.exists():
        return MODE_MARKER.read_text(encoding="utf-8").strip() or None
    return None


# ─── Mode-specific setup ────────────────────────────────────────────────────

def venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def run(cmd: list[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> None:
    info(f"$ {' '.join(str(c) for c in cmd)}")
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    r = subprocess.run(cmd, cwd=str(cwd or REPO_ROOT), env=full_env)
    if r.returncode != 0:
        error(f"Command failed (exit {r.returncode}): {' '.join(cmd)}")
        sys.exit(r.returncode)


def scan_tool_deps(implementations_dir):
    """Scan every .py under implementations_dir for `# deps: <spec>` comments
    and return the deduped sorted union of dep specs."""
    implementations_dir = Path(implementations_dir)
    if not implementations_dir.exists():
        return []
    pat = re.compile(r"^\s*#\s*deps?:\s*(.+?)\s*$", re.MULTILINE)
    seen = set()
    for path in sorted(implementations_dir.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in pat.finditer(text):
            for chunk in m.group(1).split(","):
                spec = chunk.strip()
                if spec:
                    seen.add(spec)
    return sorted(seen)


def bake_tool_deps(args: argparse.Namespace) -> None:
    """scan tools/implementations for `# deps:` comments, prompt the
    operator (or auto-approve via --approve-tool-deps), and write the resulting
    union to tools/requirements-tools.txt.  Docker builds COPY this file and
    pip-install it into the runtime image.  No-op if no deps are declared."""
    impl_dir = Path("systemu/vault/tools/implementations")
    if not impl_dir.exists():
        return
    deps = scan_tool_deps(impl_dir)
    if not deps:
        return
    should_approve = getattr(args, "approve_tool_deps", False)
    if not args.non_interactive and not should_approve:
        print()
        info("Tool dependencies detected:")
        for d in deps:
            print(f"    - {d}")
        try:
            resp = input(" Install these into the container image? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = ""
        should_approve = resp in ("", "y", "yes")
    if should_approve:
        tools_dir = Path("tools")
        tools_dir.mkdir(exist_ok=True)
        (tools_dir / "requirements-tools.txt").write_text(
            "\n".join(deps) + "\n", encoding="utf-8"
        )
        success(f"Wrote {len(deps)} dep(s) to tools/requirements-tools.txt")


def setup_local(args: argparse.Namespace) -> None:
    header("Local install — native venv + SQLite + Huey-SQLite broker")
    check_python_version()

    # echo detected proxy so user sees their corp firewall config
    # was honored.  pip + Playwright auto-read the env vars themselves;
    # this is purely informational.
    for scheme, url in detect_proxy_config().items():
        info(f"Detected {scheme.upper()}_PROXY: {_mask_proxy_url(url)}")

    # macOS-only — print Accessibility + Screen Recording guidance.
    # No-op on Linux / Windows.  Docker modes skip this (host perms don't
    # apply to containers).
    print_macos_permissions_guide()

    # macOS-only — info banner when running on ARM64 (M-series).
    print_apple_silicon_banner()

    # Linux capture-deps check — non-blocking warning.
    missing = check_linux_capture_deps()
    if missing:
        warn(
            f"Linux capture tools missing: {', '.join(missing)}.  "
            f"Daemon will still run, but keyboard/clipboard capture "
            f"will produce empty event streams in sharing_on sessions.  "
            f"Install with:\n"
            f"   sudo apt install {' '.join(missing)}      # Debian / Ubuntu\n"
            f"   sudo dnf install {' '.join(missing)}      # Fedora"
        )

    # Linux UI introspection bindings check — non-blocking warning.
    check_linux_pyatspi()

    if detect_wayland_session():
        warn(
            "Wayland session detected.  pynput-based keyboard/clipboard "
            "capture in sharing_on requires X11 — captures will record empty "
            "event streams in this session.  Daemon + dashboard + tool "
            "execution are unaffected.  To use capture features, log out and "
            "log back in selecting an X11/Xorg session at the login screen."
        )

    # warn on stale env vars (renamed in a prior release).
    stale = detect_stale_env_vars()
    for old, new in stale.items():
        warn(
            f"Your .env contains the deprecated env var {old}.  "
            f"It was renamed to {new} in v0.6.1 and is now silently ignored.  "
            f"This reconfigure will preserve your other settings but you "
            f"should rename {old} -> {new} in .env manually after install "
            f"completes, then restart the daemon."
        )

    DATA_DIR.mkdir(exist_ok=True)
    db_path = DATA_DIR / "systemu.db"
    db_url = f"sqlite:///{db_path.as_posix()}"

    if args.skip_deps:
        info("--skip-deps: skipping venv / pip / playwright / alembic.")
    else:
        venv = REPO_ROOT / ".venv"
        py = venv_python(venv)
        if not venv.exists():
            info(f"Creating virtualenv at {venv.relative_to(REPO_ROOT)} …")
            run([sys.executable, "-m", "venv", str(venv)])
        else:
            info("Reusing existing .venv")

        info("Upgrading pip …")
        run([str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])

        info("Installing dependencies (this may take a minute) …")
        run([str(py), "-m", "pip", "install", "-r", "requirements.txt", "--quiet"])
        info("Installing systemu in editable mode with [local] extras …")
        run([str(py), "-m", "pip", "install", "-e", ".[local]", "--quiet"])

        if not args.skip_playwright:
            info("Installing Playwright Chromium (browser tools) …")
            try:
                run([str(py), "-m", "playwright"] + playwright_install_args())
            except SystemExit:
                pw_cmd = " ".join(playwright_install_args())
                warn(f"Playwright install failed — you can re-run it later: "
                     f".venv/bin/python -m playwright {pw_cmd}")

        info("Running alembic migrations …")
        env = {"SYSTEMU_DATABASE_URL": db_url}
        try:
            run([str(py), "-m", "alembic", "upgrade", "head"], env=env)
        except SystemExit:
            warn("Alembic upgrade failed — DB will be created on first launch "
                 "instead.  Crash safety degrades until this is resolved.")

    api_keys = collect_api_keys(args)
    write_env({
        "SYSTEMU_MODE": "local",
        **api_keys,
        "SYSTEMU_STORAGE": "sqlite",
        "SYSTEMU_DATABASE_URL": db_url,
        "SYSTEMU_QUEUE": "huey",
        "SYSTEMU_QUEUE_BROKER": "sqlite",
        "SYSTEMU_VAULT_DIR": "systemu/vault",
        "HUEY_WORKERS": "4",
        # local mode keeps the v0.6.7 auto-install behaviour
        # (PROMPT-mode gated by the operator allow-list).
        "SYSTEMU_TOOL_DEP_INSTALL_MODE": "auto",
    })
    write_mode_marker("local")

    print()
    success("Local install complete.")
    print(f"   Start with:  {bold('./start.sh' if sys.platform != 'win32' else 'start.bat')}")
    print(f"   Stop with:   {bold('./stop.sh'  if sys.platform != 'win32' else 'stop.bat')}")
    print(f"   Dashboard:   http://localhost:8765/")


def setup_docker_local(args: argparse.Namespace) -> None:
    header("docker-local install — Postgres vault + Huey-SQLite broker")

    # echo proxy detection for docker builds too.
    for scheme, url in detect_proxy_config().items():
        info(f"Detected {scheme.upper()}_PROXY: {_mask_proxy_url(url)}")

    # warn on stale env vars (renamed in a prior release).
    stale = detect_stale_env_vars()
    for old, new in stale.items():
        warn(
            f"Your .env contains the deprecated env var {old}.  "
            f"It was renamed to {new} in v0.6.1 and is now silently ignored.  "
            f"This reconfigure will preserve your other settings but you "
            f"should rename {old} -> {new} in .env manually after install "
            f"completes, then restart the daemon."
        )

    if not check_docker():
        error("Docker is required for docker-local mode. Install Docker Desktop and re-run.")
        sys.exit(1)

    pg_password = args.pg_password or prompt_password("Postgres password")
    api_keys = collect_api_keys(args)

    write_env({
        "SYSTEMU_MODE": "docker-local",
        **api_keys,
        "POSTGRES_USER": "systemu",
        "POSTGRES_PASSWORD": pg_password,
        "POSTGRES_DB": "systemu",
        "SYSTEMU_STORAGE": "postgres",
        "SYSTEMU_DATABASE_URL": f"postgresql://systemu:{pg_password}@postgres-local:5432/systemu",
        "SYSTEMU_QUEUE": "huey",
        "SYSTEMU_QUEUE_BROKER": "sqlite",
        "SYSTEMU_VAULT_DIR": "/app/systemu/vault",
        "HUEY_WORKERS": str(args.huey_workers),
        # expose Postgres on the host so `sharing_on record`
        # running on the host can reach the container's vault.  Loopback-only
        # by default — matches the dashboard's existing 127.0.0.1:8765
        # security boundary.  Operators on shared hosts can opt out by
        # editing this line in .env (set to empty + add an override file).
        "SYSTEMU_DB_BIND": "127.0.0.1:5432",
        # absolute host path for the outputs bind mount.  Docker
        # Desktop on Windows silently degrades a relative ``./outputs`` to a
        # named volume — the .docx files end up invisible on the host.
        "SYSTEMU_HOST_OUTPUTS_DIR": str(_resolve_outputs_host_dir()),
        # docker-* modes use allow-list — runtime installs go
        # through approve_and_install() which writes to tool_dep_approvals
        # and rebuilds the image baked-deps requirements.
        "SYSTEMU_TOOL_DEP_INSTALL_MODE": "allow-list",
    })
    write_mode_marker("docker-local")

    bake_tool_deps(args)

    if not args.skip_pull:
        info("Building images (docker compose --profile local build) …")
        run(["docker", "compose", "--profile", "local", "build"])

    print()
    success("docker-local install complete.")
    print(f"   Start with:  {bold('./start.sh' if sys.platform != 'win32' else 'start.bat')}")
    print(f"   Stop with:   {bold('./stop.sh'  if sys.platform != 'win32' else 'stop.bat')}")

    # probe the bind mount before declaring install complete.
    if not getattr(args, "skip_bind_check", False) and not getattr(args, "skip_pull", False):
        host_outputs = _resolve_outputs_host_dir()
        ok, msg = _run_bind_smoke_test(host_outputs)
        if ok:
            print(f"  [OK] {msg}")
        else:
            print(msg)


def setup_docker_enterprise(args: argparse.Namespace) -> None:
    header("docker-enterprise install — Postgres + Redis + scaled workers")

    # echo proxy detection for docker builds too.
    for scheme, url in detect_proxy_config().items():
        info(f"Detected {scheme.upper()}_PROXY: {_mask_proxy_url(url)}")

    # warn on stale env vars (renamed in a prior release).
    stale = detect_stale_env_vars()
    for old, new in stale.items():
        warn(
            f"Your .env contains the deprecated env var {old}.  "
            f"It was renamed to {new} in v0.6.1 and is now silently ignored.  "
            f"This reconfigure will preserve your other settings but you "
            f"should rename {old} -> {new} in .env manually after install "
            f"completes, then restart the daemon."
        )

    if not check_docker():
        error("Docker is required for docker-enterprise mode. Install Docker Desktop and re-run.")
        sys.exit(1)

    pg_password = args.pg_password or prompt_password("Postgres password")
    redis_password = args.redis_password or prompt_password("Redis password (blank = no auth)")
    if args.worker_replicas:
        replicas = args.worker_replicas
    else:
        ans = prompt("Worker replicas", default="2")
        try:
            replicas = max(1, int(ans))
        except ValueError:
            replicas = 2
    api_keys = collect_api_keys(args)

    if redis_password:
        redis_url = f"redis://:{redis_password}@redis:6379/0"
        redis_auth = f":{redis_password}@"
    else:
        redis_url = "redis://redis:6379/0"
        redis_auth = ""

    write_env({
        "SYSTEMU_MODE": "docker-enterprise",
        **api_keys,
        "POSTGRES_USER": "systemu",
        "POSTGRES_PASSWORD": pg_password,
        "POSTGRES_DB": "systemu",
        "REDIS_PASSWORD": redis_password,
        "REDIS_AUTH": redis_auth,
        "SYSTEMU_STORAGE": "postgres",
        "SYSTEMU_DATABASE_URL": f"postgresql://systemu:{pg_password}@postgres:5432/systemu",
        "SYSTEMU_QUEUE": "huey",
        "SYSTEMU_QUEUE_BROKER": "redis",
        "SYSTEMU_REDIS_URL": redis_url,
        "SYSTEMU_VAULT_DIR": "/app/systemu/vault",
        "HUEY_WORKERS": str(args.huey_workers),
        "WORKER_REPLICAS": str(replicas),
        # enterprise mode does NOT publish Postgres by default —
        # production deployments should keep the DB on the docker-internal
        # network only.  The enterprise `postgres` service has no `ports:`
        # section, so this variable has no effect in enterprise.  Set in
        # .env for parity / documentation purposes.
        "SYSTEMU_DB_BIND": "",
        # see docker-local above — same rationale.
        "SYSTEMU_HOST_OUTPUTS_DIR": str(_resolve_outputs_host_dir()),
        # docker-* modes use allow-list (see docker-local above).
        "SYSTEMU_TOOL_DEP_INSTALL_MODE": "allow-list",
    })
    write_mode_marker("docker-enterprise")

    bake_tool_deps(args)

    if not args.skip_pull:
        info("Building images (docker compose --profile enterprise build) …")
        run(["docker", "compose", "--profile", "enterprise", "build"])

    print()
    success("docker-enterprise install complete.")
    print(f"   Start with:  {bold('./start.sh' if sys.platform != 'win32' else 'start.bat')}")
    print(f"   Stop with:   {bold('./stop.sh'  if sys.platform != 'win32' else 'stop.bat')}")
    print(f"   Workers:     {replicas} replica(s) × {args.huey_workers} threads each")

    # probe the bind mount before declaring install complete.
    if not getattr(args, "skip_bind_check", False) and not getattr(args, "skip_pull", False):
        host_outputs = _resolve_outputs_host_dir()
        ok, msg = _run_bind_smoke_test(host_outputs)
        if ok:
            print(f"  [OK] {msg}")
        else:
            print(msg)


def collect_api_keys(args: argparse.Namespace) -> Dict[str, str]:
    """Prompt for API keys (skipped when --non-interactive and keys are unset).

    in interactive mode, the OpenRouter key is probe-validated
    against /api/v1/models.  On 401 the operator gets re-prompted (up to 3
    attempts).  On connection error we warn and proceed — user may be
    air-gapped or behind a strict proxy that blocks outbound during install.
    """
    out: Dict[str, str] = {}
    if args.non_interactive:
        if args.openrouter_key:
            out["OPENROUTER_API_KEY"] = args.openrouter_key
        if args.google_key:
            out["GOOGLE_API_KEY"] = args.google_key
        return out
    print()
    info("API keys — leave blank to fill in later by editing .env")

    proxies = detect_proxy_config()
    attempts_left = 3
    while attempts_left > 0:
        key = prompt("OpenRouter API key", default="")
        if not key:
            info("Skipping OpenRouter validation (blank key).")
            out["OPENROUTER_API_KEY"] = ""
            break
        info("Validating OpenRouter key …")
        ok, msg = validate_openrouter_key(key, proxies=proxies)
        if ok:
            success("OpenRouter key is valid.")
            out["OPENROUTER_API_KEY"] = key
            break
        attempts_left -= 1
        if "401" in msg:
            if attempts_left > 0:
                warn(f"{msg}.  {attempts_left} attempt(s) left.")
                continue
            warn(f"{msg}.  Storing the rejected key anyway — fix it in .env later.")
            out["OPENROUTER_API_KEY"] = key
        else:
            # Network / proxy issue — don't loop; let install continue.
            warn(f"{msg}.  Storing the key as-is (validation skipped).")
            out["OPENROUTER_API_KEY"] = key
            break

    out["GOOGLE_API_KEY"] = prompt("Google AI Studio key", default="")
    return out


# ─── Argument parsing & main ────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Systemu unified installer — pick local, docker-local, or docker-enterprise.",
    )
    p.add_argument("--mode", choices=VALID_MODES, help="Skip the interactive prompt.")
    p.add_argument("--non-interactive", action="store_true",
                   help="Fail rather than prompt — for CI / automation.")
    p.add_argument("--pg-password", help="Postgres password (docker modes only).")
    p.add_argument("--redis-password", default="",
                   help="Redis password (docker-enterprise only; blank = no auth).")
    p.add_argument("--worker-replicas", type=int,
                   help="Number of worker containers (docker-enterprise only).")
    p.add_argument("--huey-workers", type=int, default=4,
                   help="Threads per worker process / container (default 4).")
    p.add_argument("--openrouter-key", help="Pass OPENROUTER_API_KEY non-interactively.")
    p.add_argument("--google-key", help="Pass GOOGLE_API_KEY non-interactively.")
    p.add_argument("--skip-playwright", action="store_true",
                   help="Skip Playwright Chromium install (local only).")
    p.add_argument("--skip-pull", action="store_true",
                   help="Skip docker compose build (docker modes only).")
    p.add_argument("--skip-bind-check", action="store_true",
                   help="Skip the post-up bind-mount propagation smoke test (CI environments).")
    p.add_argument("--skip-deps", action="store_true",
                   help="Skip venv creation, pip install, and alembic "
                        "(local only).  Used by tests / CI to exercise just "
                        "the .env / mode-marker layer.")
    p.add_argument("--approve-tool-deps", action="store_true",
                   help="Non-interactive: pre-approve scanned tool deps for the image.")
    return p


def pick_mode(args: argparse.Namespace, existing: Optional[str]) -> str:
    if args.mode:
        return args.mode
    if args.non_interactive:
        error("--non-interactive requires --mode")
        sys.exit(2)

    header("Installation mode")
    if existing:
        info(f"Detected existing install: {bold(existing)}")
        action = prompt_choice(
            "Action",
            [
                ("reconfigure", "Wipe .env + .systemu_mode and pick a mode again"),
                ("upgrade", "Re-run setup for the same mode (refresh deps, rebuild images)"),
                ("quit", "Exit without changes"),
            ],
            default_idx=1,
        )
        if action == "quit":
            sys.exit(0)
        if action == "upgrade":
            return existing
        # reconfigure → fall through to mode picker

    return prompt_choice(
        "Pick a deployment mode",
        [
            ("local",
             "Native venv on this host. SQLite vault. Daemon + worker as subprocesses."),
            ("docker-local",
             "docker-compose. Postgres vault. Huey-SQLite broker. One worker."),
            ("docker-enterprise",
             "docker-compose. Postgres vault. Redis broker. Scaled workers."),
        ],
        default_idx=0,
    )


def main() -> None:
    args = build_parser().parse_args()
    existing = read_existing_mode()
    mode = pick_mode(args, existing)

    if mode == "local":
        setup_local(args)
    elif mode == "docker-local":
        setup_docker_local(args)
    elif mode == "docker-enterprise":
        setup_docker_enterprise(args)
    else:
        error(f"Unknown mode: {mode}")
        sys.exit(2)


if __name__ == "__main__":
    main()
