"""E2 — the build step that produces the three trees ``systemu.iss`` consumes.

``packaging/windows/systemu.iss`` has a ``[Files]`` section that reads three
directories which nothing in this repo used to create::

    packaging/windows/build/embeddable/                 python.org embeddable CPython
    packaging/windows/build/wheelhouse/                  the systemu wheel + every dep
    packaging/windows/build/launcher/systemu-launcher.exe the launcher

This module creates them, and — just as importantly — :func:`verify_build_inputs`
refuses a tree that would compile into a broken installer. Three failures are
specifically worth naming, because each one produces an installer that builds
cleanly and then fails on a stranger's machine:

  * **no pip in the embedded runtime.** The embeddable distribution ships no
    pip and no ``ensurepip``. ``[Run]`` step 1 is ``env\\python.exe -m pip
    install ...``; without pip vendored in at build time that step dies, and
    fetching ``get-pip.py`` at install time would need the network — which is
    exactly what AC5 forbids.
  * **an sdist in the wheelhouse.** ``pip install --no-index`` will happily
    take one and then try to build it, which needs a build backend and, in
    practice, the network. The wheelhouse is wheels or it is not offline.
  * **a launcher that finds its interpreter through the CWD.** Measured, not
    assumed — see :mod:`systemu.winpkg.launcher`.

Naming: this is ``build_step``. It is not an "installer" module —
``tests/test_installer.py`` covers the developer-facing ``install.py`` and
``systemu/runtime/dependency_installer.py`` is the pip self-heal for forged
tools. Neither is E2.

Run it from the repo root::

    python -m systemu.winpkg.build_step
    python -m systemu.winpkg.build_step --only launcher
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .layout import ENV_DIRNAME

#: Directory names under ``packaging/windows/``. These are mirrored in the
#: ``[Files]`` section of systemu.iss and pinned against it by
#: tests/test_e2_windows_packaging.py.
BUILD_DIRNAME = "build"
EMBEDDABLE_BUILD_DIRNAME = "embeddable"
WHEELHOUSE_BUILD_DIRNAME = "wheelhouse"
LAUNCHER_BUILD_DIRNAME = "launcher"
LAUNCHER_EXE_NAME = "systemu-launcher.exe"
LAUNCHER_SOURCE_NAME = "SystemuLauncher.cs"

#: The module the launcher stub runs inside the embedded interpreter.
LAUNCHER_MODULE = "systemu.winpkg.launcher"
#: Windowless interpreter — a Start-Menu app must not flash a console.
LAUNCHER_PYTHON_EXE = "pythonw.exe"
#: Written next to the .exe when the stub cannot start the interpreter.
LAUNCHER_ERROR_LOG = "launcher-error.log"
#: Set to "1" for unattended/silent installs: suppresses the modal error box.
#: The exit code and the error log are UNCHANGED by it — it hides a dialog,
#: it does not hide a failure.
LAUNCHER_QUIET_ENV_VAR = "SYSTEMU_LAUNCHER_QUIET"

DEFAULT_PYTHON_VERSION = "3.12.10"
EMBEDDABLE_URL_TEMPLATE = (
    "https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip"
)

#: Pinned digests for the embeddable archives we know. An unknown version is
#: refused unless the caller supplies a digest explicitly — a build step that
#: silently accepted whatever bytes came back would be the softest possible
#: supply-chain seam, and it sits upstream of every installed byte.
EMBEDDABLE_SHA256 = {
    "3.12.10": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
}


class BuildInputsError(RuntimeError):
    """A build input is missing or would produce a broken installer."""


@dataclass(frozen=True)
class BuildInputs:
    """Absolute paths of the three trees, rooted at the .iss's directory."""

    iss_dir: Path
    build_dir: Path
    embeddable_dir: Path
    wheelhouse_dir: Path
    launcher_dir: Path
    launcher_exe: Path


def build_inputs_for(iss_dir: os.PathLike | str) -> BuildInputs:
    iss_dir = Path(iss_dir).resolve()
    build_dir = iss_dir / BUILD_DIRNAME
    launcher_dir = build_dir / LAUNCHER_BUILD_DIRNAME
    return BuildInputs(
        iss_dir=iss_dir,
        build_dir=build_dir,
        embeddable_dir=build_dir / EMBEDDABLE_BUILD_DIRNAME,
        wheelhouse_dir=build_dir / WHEELHOUSE_BUILD_DIRNAME,
        launcher_dir=launcher_dir,
        launcher_exe=launcher_dir / LAUNCHER_EXE_NAME,
    )


def default_iss_dir() -> Path:
    """``packaging/windows`` in the repo this module was imported from."""
    return Path(__file__).resolve().parent.parent.parent / "packaging" / "windows"


# ── 1. the embeddable CPython runtime ────────────────────────────────────────

def embeddable_url(version: str) -> str:
    return EMBEDDABLE_URL_TEMPLATE.format(version=version)


def _pth_tag(version: str) -> str:
    """``3.12.10`` -> ``python312`` — the ``._pth`` basename in the archive."""
    parts = version.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        raise BuildInputsError(f"not a CPython version: {version!r}")
    return f"python{parts[0]}{parts[1]}"


def embeddable_pth_text(version: str) -> str:
    """The ``._pth`` an embedded runtime needs to host pip.

    The shipped file has ``import site`` commented out and no site-packages
    entry, which means ``python -m pip`` cannot work even once pip is present.
    Both lines are required; neither is decoration.
    """
    tag = _pth_tag(version)
    return (
        f"{tag}.zip\n"
        ".\n"
        "Lib\\site-packages\n"
        "\n"
        "# systemu (E2): site + site-packages are ON. The embedded runtime hosts\n"
        "# pip so `[Run]` can install the wheel offline from the wheelhouse.\n"
        "import site\n"
    )


def rewrite_embeddable_pth(env_dir: Path, *, version: str) -> Path:
    """Replace the archive's ``._pth`` with one that can host pip."""
    env_dir = Path(env_dir)
    found = sorted(env_dir.glob("python*._pth"))
    if len(found) != 1:
        raise BuildInputsError(
            f"expected exactly one python*._pth in {env_dir}, found {len(found)}"
        )
    found[0].write_text(embeddable_pth_text(version), encoding="ascii")
    return found[0]


def _default_fetch(url: str) -> bytes:      # pragma: no cover - network
    with urllib.request.urlopen(url) as response:
        return response.read()


def download_embeddable(
    version: str,
    *,
    sha256: Optional[str] = None,
    fetch: Optional[Callable[[str], bytes]] = None,
) -> bytes:
    """Fetch the embeddable archive and check it against a pinned digest.

    Refuses an unpinned version outright rather than trusting the transport.
    """
    expected = sha256 or EMBEDDABLE_SHA256.get(version)
    if not expected:
        raise BuildInputsError(
            f"no pinned sha256 for CPython {version}; pass one explicitly "
            "(--embeddable-sha256) after checking python.org yourself"
        )
    payload = (fetch or _default_fetch)(embeddable_url(version))
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise BuildInputsError(
            f"embeddable CPython {version} digest mismatch: expected "
            f"{expected}, got {actual}"
        )
    return payload


def vendor_pip_into(env_dir: Path, wheels: Sequence[Path]) -> list:
    """Unpack pip (and anything else it needs) into ``env/Lib/site-packages``.

    Extraction rather than ``get-pip.py``: no network, no bootstrap script, and
    the exact wheel that was audited is the one that lands.
    """
    site_packages = Path(env_dir) / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    unpacked = []
    for wheel in wheels:
        wheel = Path(wheel)
        if not wheel.is_file():
            raise BuildInputsError(f"wheel to vendor does not exist: {wheel}")
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site_packages)
        unpacked.append(wheel.name)
    return unpacked


def build_embeddable(
    inputs: BuildInputs,
    *,
    version: str = DEFAULT_PYTHON_VERSION,
    sha256: Optional[str] = None,
    fetch: Optional[Callable[[str], bytes]] = None,
    pip_wheels: Optional[Sequence[Path]] = None,
    runner: Optional[Callable[[Sequence[str]], None]] = None,
) -> Path:
    """Produce ``build/embeddable/`` — extracted, ``._pth`` fixed, pip vendored."""
    dest = inputs.embeddable_dir
    payload = download_embeddable(version, sha256=sha256, fetch=fetch)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive_path = inputs.build_dir / f"python-{version}-embed-amd64.zip"
    archive_path.write_bytes(payload)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(dest)

    rewrite_embeddable_pth(dest, version=version)

    if pip_wheels is None:
        pip_wheels = _download_pip_wheels(inputs.build_dir / "_pip", runner=runner)
    vendor_pip_into(dest, pip_wheels)
    return dest


def _run(command: Sequence[str], *, cwd: Optional[Path] = None) -> None:
    """Run a build command, failing loudly with its output attached."""
    completed = subprocess.run(
        list(command),
        cwd=None if cwd is None else str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        raise BuildInputsError(
            "build command failed (exit {code}): {cmd}\n{output}".format(
                code=completed.returncode,
                cmd=" ".join(command),
                output=(completed.stdout or "")[-4000:],
            )
        )


def _download_pip_wheels(
    dest: Path, *, runner: Optional[Callable[[Sequence[str]], None]] = None
) -> list:
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (runner or _run)([
        sys.executable, "-m", "pip", "download", "pip",
        "--only-binary", ":all:", "--dest", str(dest),
    ])
    wheels = sorted(dest.glob("pip-*.whl"))
    if not wheels:
        raise BuildInputsError(f"pip wheel was not downloaded into {dest}")
    return wheels


# ── 2. the offline wheelhouse ────────────────────────────────────────────────

def build_wheelhouse(
    inputs: BuildInputs,
    *,
    project_root: os.PathLike | str,
    runner: Optional[Callable[[Sequence[str]], None]] = None,
    python: Optional[str] = None,
) -> Path:
    """Produce ``build/wheelhouse/`` — the systemu wheel plus every dependency.

    The systemu wheel is built from THIS tree, never downloaded: the published
    PyPI release trails the working version, and an installer that shipped the
    older one would be a silently different product.
    """
    run = runner or _run
    python = python or sys.executable
    dest = inputs.wheelhouse_dir
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    run([python, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(dest),
         str(project_root)])

    built = sorted(dest.glob("systemu-*.whl"))
    if len(built) != 1:
        raise BuildInputsError(
            f"expected exactly one systemu wheel in {dest}, found {len(built)}"
        )

    run([python, "-m", "pip", "download", str(built[0]),
         "--only-binary", ":all:", "--dest", str(dest)])
    return dest


# ── 3. the launcher ──────────────────────────────────────────────────────────

def render_launcher_source(
    *,
    env_dirname: str = ENV_DIRNAME,
    python_exe: str = LAUNCHER_PYTHON_EXE,
    module: str = LAUNCHER_MODULE,
    install_root_env_var: Optional[str] = None,
    error_log: str = LAUNCHER_ERROR_LOG,
    quiet_env_var: str = LAUNCHER_QUIET_ENV_VAR,
) -> str:
    """The C# source of the launcher stub.

    It resolves the interpreter from its OWN module path
    (``Process.MainModule.FileName``), never from the current directory. That is
    the whole reason this is a compiled stub instead of a pip/distlib ``.exe``
    shim: a distlib stub resolves a relative shebang through ``SearchPath()``,
    which searches the CWD and ``PATH``. Measured on this repo: such a stub ran
    an ``env\\python.exe`` that lived in the CWD instead of the one beside it.
    """
    if install_root_env_var is None:
        from .launcher import INSTALL_ROOT_ENV_VAR

        install_root_env_var = INSTALL_ROOT_ENV_VAR

    return f'''// systemu launcher (SPEC §14 E2) — GENERATED by
// systemu/winpkg/build_step.py:render_launcher_source(). Edit it there.
//
// Resolves {env_dirname}\\{python_exe} relative to THIS FILE, never the working
// directory, and runs {module} inside it.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

static class SystemuLauncher {{
    const string EnvDirName = "{env_dirname}";
    const string PythonExeName = "{python_exe}";
    const string LauncherModule = "{module}";
    const string InstallRootEnvVar = "{install_root_env_var}";
    const string ErrorLogName = "{error_log}";
    const string QuietEnvVar = "{quiet_env_var}";
    const int ExitCannotStart = 3;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    // The install root is the directory holding this .exe. Read from the
    // process's own module path so it is identical no matter what the working
    // directory is.
    static string SelfDir() {{
        using (Process self = Process.GetCurrentProcess()) {{
            return Path.GetDirectoryName(self.MainModule.FileName);
        }}
    }}

    static int Fail(string dir, string message) {{
        try {{
            File.AppendAllText(
                Path.Combine(dir, ErrorLogName),
                DateTime.UtcNow.ToString("o") + "  " + message + Environment.NewLine);
        }} catch {{ }}
        if (Environment.GetEnvironmentVariable(QuietEnvVar) != "1") {{
            MessageBoxW(IntPtr.Zero, message, "systemu", 0x10);
        }}
        return ExitCannotStart;
    }}

    static string Quote(string value) {{
        return "\\"" + value.Replace("\\"", "\\\\\\"") + "\\"";
    }}

    static int Main(string[] argv) {{
        string dir;
        try {{
            dir = SelfDir();
        }} catch (Exception error) {{
            return Fail(Directory.GetCurrentDirectory(),
                        "systemu could not locate its own install directory: " + error.Message);
        }}

        string python = Path.Combine(dir, EnvDirName, PythonExeName);
        if (!File.Exists(python)) {{
            return Fail(dir,
                "systemu is not installed correctly: " + python + " is missing. "
                + "Re-run the systemu installer.");
        }}

        StringBuilder arguments = new StringBuilder();
        arguments.Append("-m ").Append(LauncherModule);
        foreach (string argument in argv) {{
            arguments.Append(' ').Append(Quote(argument));
        }}

        ProcessStartInfo start = new ProcessStartInfo(python, arguments.ToString());
        start.UseShellExecute = false;
        start.WorkingDirectory = dir;
        start.EnvironmentVariables[InstallRootEnvVar] = dir;
        try {{
            using (Process child = Process.Start(start)) {{
                child.WaitForExit();
                return child.ExitCode;
            }}
        }} catch (Exception error) {{
            return Fail(dir, "systemu could not start " + python + ": " + error.Message);
        }}
    }}
}}
'''


def find_csc(environ: Optional[dict] = None) -> Optional[Path]:
    """Locate the .NET Framework C# compiler.

    ``csc.exe`` ships with the .NET Framework that is part of Windows, so the
    build needs no third-party toolchain. It is a BUILD-machine requirement
    only; the produced .exe needs the .NET Framework 4 runtime, which is
    present on every supported Windows release.
    """
    env = os.environ if environ is None else environ
    windir = env.get("WINDIR") or env.get("SystemRoot") or r"C:\Windows"
    roots = [
        Path(windir) / "Microsoft.NET" / "Framework64",
        Path(windir) / "Microsoft.NET" / "Framework",
    ]
    candidates = []
    for root in roots:
        if not root.is_dir():
            continue
        for version_dir in sorted(root.glob("v*"), reverse=True):
            candidate = version_dir / "csc.exe"
            if candidate.is_file():
                candidates.append(candidate)
    if candidates:
        return candidates[0]
    found = shutil.which("csc")
    return Path(found) if found else None


def launcher_compile_command(csc: Path, source: Path, exe: Path) -> list:
    """The compiler invocation, as a list, so a test can read the real flags.

    ``winexe`` is load-bearing: a console subsystem binary flashes a black
    window every time the Start-Menu shortcut is used.
    """
    return [
        str(csc),
        "-nologo",
        "-optimize+",
        "-platform:anycpu",
        "-target:winexe",
        f"-out:{exe}",
        str(source),
    ]


def build_launcher(
    inputs: BuildInputs,
    *,
    csc: Optional[Path] = None,
    runner: Optional[Callable[[Sequence[str]], None]] = None,
    source_text: Optional[str] = None,
) -> Path:
    """Produce ``build/launcher/systemu-launcher.exe``."""
    compiler = csc or find_csc()
    if compiler is None:
        raise BuildInputsError(
            "csc.exe (the .NET Framework C# compiler) was not found. It ships "
            "with Windows under %WINDIR%\\Microsoft.NET\\Framework64\\v*. The "
            "launcher cannot be built without it."
        )

    dest = inputs.launcher_dir
    dest.mkdir(parents=True, exist_ok=True)
    source = dest / LAUNCHER_SOURCE_NAME
    # utf-8-sig: csc.exe only decodes a source file as UTF-8 when it starts with
    # a BOM; without one it falls back to the machine's ANSI codepage and the
    # non-ASCII characters in the header comment come out as mojibake on some
    # locales.
    source.write_text(source_text or render_launcher_source(), encoding="utf-8-sig")

    exe = inputs.launcher_exe
    if exe.exists():
        exe.unlink()
    (runner or _run)(launcher_compile_command(Path(compiler), source, exe))

    if not exe.is_file():
        raise BuildInputsError(f"the compiler reported success but {exe} does not exist")
    return exe


# ── verification ─────────────────────────────────────────────────────────────

def verify_build_inputs(inputs: BuildInputs) -> dict:
    """Refuse a build tree that would compile into a broken installer.

    Returns a small summary on success; raises :class:`BuildInputsError` naming
    the exact problem otherwise. Every check here corresponds to a failure that
    an ISCC compile would NOT catch.
    """
    env_dir = inputs.embeddable_dir
    for required in ("python.exe", LAUNCHER_PYTHON_EXE):
        if not (env_dir / required).is_file():
            raise BuildInputsError(f"{env_dir / required} is missing")

    pth_files = sorted(env_dir.glob("python*._pth"))
    if len(pth_files) != 1:
        raise BuildInputsError(
            f"expected exactly one python*._pth in {env_dir}, found {len(pth_files)}"
        )
    pth_text = pth_files[0].read_text(encoding="ascii", errors="replace")
    pth_lines = [
        line.split("#", 1)[0].strip()
        for line in pth_text.splitlines()
    ]
    if "import site" not in pth_lines:
        raise BuildInputsError(
            f"{pth_files[0]} does not enable site; `python -m pip` cannot run "
            "in the embedded environment"
        )
    if "Lib\\site-packages" not in pth_lines:
        raise BuildInputsError(
            f"{pth_files[0]} does not put Lib\\site-packages on sys.path"
        )

    if not (env_dir / "Lib" / "site-packages" / "pip" / "__init__.py").is_file():
        raise BuildInputsError(
            f"pip is not vendored into {env_dir}; the embeddable distribution "
            "ships without pip and without ensurepip, so [Run] step 1 would "
            "fail on the target machine"
        )

    if not inputs.wheelhouse_dir.is_dir():
        raise BuildInputsError(f"{inputs.wheelhouse_dir} is missing")
    entries = sorted(p for p in inputs.wheelhouse_dir.iterdir())
    non_wheels = [p.name for p in entries if p.is_dir() or p.suffix != ".whl"]
    if non_wheels:
        raise BuildInputsError(
            f"{inputs.wheelhouse_dir} holds non-wheel entries {non_wheels}; "
            "`pip install --no-index` would try to BUILD those, which needs a "
            "build backend and the network (AC5)"
        )
    wheels = [p for p in entries if p.suffix == ".whl"]
    systemu_wheels = [p for p in wheels if p.name.startswith("systemu-")]
    if len(systemu_wheels) != 1:
        raise BuildInputsError(
            f"expected exactly one systemu-*.whl in {inputs.wheelhouse_dir}, "
            f"found {len(systemu_wheels)}"
        )
    if len(wheels) < 2:
        raise BuildInputsError(
            f"{inputs.wheelhouse_dir} holds only the systemu wheel; its "
            "dependencies are missing, so an offline install would fail"
        )

    exe = inputs.launcher_exe
    if not exe.is_file():
        raise BuildInputsError(f"{exe} is missing")
    with exe.open("rb") as handle:
        head = handle.read(2)
    if head != b"MZ":
        raise BuildInputsError(f"{exe} is not a Windows executable (no MZ header)")

    return {
        "embeddable": str(env_dir),
        "wheelhouse": str(inputs.wheelhouse_dir),
        "wheels": len(wheels),
        "launcher": str(exe),
    }


# ── orchestration ────────────────────────────────────────────────────────────

STAGES = ("embeddable", "wheelhouse", "launcher")


def build_all(
    inputs: BuildInputs,
    *,
    project_root: os.PathLike | str,
    only: Optional[Sequence[str]] = None,
    version: str = DEFAULT_PYTHON_VERSION,
    sha256: Optional[str] = None,
    verify: bool = True,
    builders: Optional[dict] = None,
) -> dict:
    """Build the selected stages, then verify ALL THREE.

    ``--only`` narrows what is rebuilt, never what is checked: a partial build
    that leaves one tree missing must not read as a success.

    ``builders`` replaces the per-stage callables, so a test can drive the
    selection/verification logic without a network fetch or a compiler.
    """
    selected = tuple(only) if only else STAGES
    unknown = [stage for stage in selected if stage not in STAGES]
    if unknown:
        raise BuildInputsError(f"unknown build stage(s): {unknown}")

    steps = {
        "embeddable": lambda: build_embeddable(inputs, version=version, sha256=sha256),
        "wheelhouse": lambda: build_wheelhouse(inputs, project_root=project_root),
        "launcher": lambda: build_launcher(inputs),
    }
    if builders:
        steps.update(builders)

    inputs.build_dir.mkdir(parents=True, exist_ok=True)
    for stage in STAGES:
        if stage in selected:
            steps[stage]()

    return verify_build_inputs(inputs) if verify else {"built": list(selected)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m systemu.winpkg.build_step",
        description="Produce the build/ trees that packaging/windows/systemu.iss "
                    "consumes.",
    )
    parser.add_argument("--iss-dir", default=None,
                        help="directory holding systemu.iss (default: the one "
                             "in this checkout)")
    parser.add_argument("--project-root", default=None,
                        help="the tree the systemu wheel is built from "
                             "(default: this checkout)")
    parser.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION)
    parser.add_argument("--embeddable-sha256", default=None)
    parser.add_argument("--only", default=None,
                        help="comma-separated subset of: " + ",".join(STAGES))
    parser.add_argument("--verify-only", action="store_true",
                        help="check an existing build tree and exit")
    args = parser.parse_args(argv)

    iss_dir = Path(args.iss_dir) if args.iss_dir else default_iss_dir()
    project_root = (Path(args.project_root) if args.project_root
                    else iss_dir.parent.parent)
    inputs = build_inputs_for(iss_dir)

    try:
        if args.verify_only:
            summary = verify_build_inputs(inputs)
        else:
            summary = build_all(
                inputs,
                project_root=project_root,
                only=[s.strip() for s in args.only.split(",")] if args.only else None,
                version=args.python_version,
                sha256=args.embeddable_sha256,
            )
    except BuildInputsError as error:
        print(f"build inputs NOT ready: {error}", file=sys.stderr)
        return 1

    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
