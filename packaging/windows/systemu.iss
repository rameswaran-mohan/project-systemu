; E2 — systemu Windows installer (SPEC §14 E2)
;
; Inno Setup script. Ships the python.org EMBEDDABLE CPython plus an offline
; wheelhouse, and installs the systemu wheel from that wheelhouse so a core
; install needs no network (AC5).
;
; PyInstaller onefile is explicitly REJECTED by the spec: systemu's dynamic
; imports and optional playwright browsers make a frozen single-file build
; fragile. This installer WRAPS the pip artifact; it never forks it. There is
; one build lineage.
;
; Directory names here are mirrored in systemu/winpkg/layout.py and the two are
; pinned against each other by tests/test_e2_windows_packaging.py — if you
; rename a directory in one place, that test fails until you fix the other.
;
; BUILD INPUTS. The [Files] section below consumes three trees, all relative to
; THIS script's directory (Inno's SourceDir defaults to it). They are produced
; by systemu/winpkg/build_step.py — run it from the repo root before ISCC:
;
;   python -m systemu.winpkg.build_step
;
;   build\embeddable\   python.org embeddable CPython, extracted, with the
;                       ._pth rewritten to enable site + Lib\site-packages AND
;                       pip unpacked into it. The embeddable distribution ships
;                       with neither pip nor ensurepip, so without that step the
;                       `-m pip install` in [Run] below dies on the target
;                       machine, and fetching get-pip.py there would need the
;                       network AC5 forbids.
;   build\wheelhouse\   the systemu wheel (built from the tree, not downloaded
;                       from PyPI) plus every transitive dependency, as WHEELS.
;                       Wheels only: `--no-index` will happily accept an sdist
;                       and then try to build it, which needs a build backend
;                       and, in practice, the network.
;   build\launcher\     the launcher exe. It resolves env\pythonw.exe relative
;                       to its OWN module path and runs systemu.winpkg.launcher.
;
; build_step.py VERIFIES all three after building and refuses a tree that would
; compile into an installer that only fails later, on a stranger's machine.
; Compiling with a tree missing fails here at [Files] with a missing-source
; error — loudly missing beats silently shipping an empty environment.
;
; COMPILED: ISCC 6.7.3 (Windows 11 x64) compiles this script, exit code 0, with
; the three trees present — ~150 MB of inputs in, a 137,927,113-byte
; systemu-setup.exe out. That is a COMPILE, not an install: nobody has run the
; produced installer on a clean machine, so AC1 (<15 min on a fresh VM with no
; dev tools) remains UNMEASURED and is not claimed here.

#define AppName        "systemu"
#define AppPublisher   "systemu"
#define AppExeName     "systemu-launcher.exe"

; Directory names — keep in sync with systemu/winpkg/layout.py
#define EnvDirName        "env"
#define VaultDirName      "vault"
#define WheelhouseDirName "wheelhouse"

[Setup]
AppName={#AppName}
AppVerName={#AppName}
AppPublisher={#AppPublisher}
; Per-user install under %LOCALAPPDATA% — no admin rights required, which is
; what makes the <15 min fresh-machine metric (AC1) reachable on a locked-down
; corporate laptop.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
OutputBaseFilename=systemu-setup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
; The vault may survive an uninstall, so the app dir is not always empty.
UninstallDisplayName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
; THE VAULT PIN (AC2/AC3). The vault lives INSIDE {app}, so its survival cannot
; rest on "it is somewhere else". It is declared here and flagged
; uninsneveruninstall: neither an upgrade (re-running this installer over an
; existing install) nor the uninstaller ever removes it. The launcher points
; SYSTEMU_VAULT_DIR at exactly this directory (systemu/winpkg/launcher.py), so
; the directory pinned here is the one the runtime writes.
; tests/test_e2_installer_vault_pin.py parses this script and fails if this
; entry goes, or if any install-delete, uninstall-delete, files or code line
; reaches this directory or an ancestor of it. (Section names are spelled out
; without brackets here on purpose: other tests locate sections by header.)
Name: "{app}\{#VaultDirName}"; Flags: uninsneveruninstall

[Files]
; The embeddable CPython runtime — extracted, never registered system-wide.
Source: "build\embeddable\*"; DestDir: "{app}\{#EnvDirName}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The offline wheelhouse: the systemu wheel and every dependency wheel (AC5).
Source: "build\wheelhouse\*"; DestDir: "{app}\{#WheelhouseDirName}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The launcher: starts the daemon and opens the dashboard.
; It does NOT host the R-P7 global summon hotkey — R-P7 is not built, nothing in
; this repo registers a hotkey, and `systemu-launcher.exe --hotkey` exits
; non-zero saying so rather than quietly opening the dashboard instead.
Source: "build\launcher\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; 1. Install systemu from the wheelhouse into the embedded env. --no-index
;    is what makes this an OFFLINE install; if it needs the network, AC5 has
;    regressed and this step fails loudly rather than silently reaching out.
Filename: "{app}\{#EnvDirName}\python.exe"; \
    Parameters: "-m pip install --no-index --find-links ""{app}\{#WheelhouseDirName}"" systemu"; \
    StatusMsg: "Installing systemu (offline)..."; Flags: runhidden

; 2. Stamp the install time — the start of the time-to-first-completed-task
;    metric. Local file only; there is no phone-home in any mode.
Filename: "{app}\{#EnvDirName}\python.exe"; \
    Parameters: "-m systemu.winpkg.cli stamp-installed --root ""{app}"""; \
    StatusMsg: "Recording install..."; Flags: runhidden

; 3. First run: start the daemon and open the dashboard, which redirects a
;    fresh install to /welcome (systemu.interface.pages.welcome.onboarding_gate).
;    NOTE what that wizard does NOT do: it does not take the provider key in the
;    browser. It tells the operator to put OPENROUTER_API_KEY in .env and click
;    Re-check. systemu.winpkg.first_run — the E2 key-entry/verify/handoff module
;    — has no caller outside its own test, so nothing here collects a key.
Filename: "{app}\{#AppExeName}"; Parameters: "--first-run"; \
    Description: "Set up {#AppName} now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Remove the environment and the wheelhouse. The vault is NOT listed here and
; must never be (AC3) — an uninstaller is the wrong place to decide that an
; operator's data should die.
Type: filesandordirs; Name: "{app}\{#EnvDirName}"
Type: filesandordirs; Name: "{app}\env.new"
Type: filesandordirs; Name: "{app}\env.old"
Type: filesandordirs; Name: "{app}\{#WheelhouseDirName}"

[Code]
function VaultDir(): String;
begin
  Result := ExpandConstant('{app}\{#VaultDirName}');
end;

// AC3: on uninstall, tell the operator plainly what stayed and where.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Notice: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(VaultDir()) then
    begin
      Notice :=
        'systemu has been uninstalled.' + #13#10 + #13#10 +
        'Your vault was NOT deleted. It is still here:' + #13#10 + #13#10 +
        '    ' + VaultDir() + #13#10 + #13#10 +
        'If you want it gone, delete that folder yourself.';
      SaveStringToFile(
        ExpandConstant('{app}\WHAT-WAS-LEFT-BEHIND.txt'), Notice, False);
      MsgBox(Notice, mbInformation, MB_OK);
    end;
  end;
end;
