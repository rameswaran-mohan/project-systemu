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
; NOTE: this script has NOT been compiled by Inno Setup in this environment
; (ISCC is not installed here). It is structurally linted by the tests only.
;
; BUILD INPUTS — NOT YET PRODUCED BY ANYTHING IN THIS REPO.
; The [Files] section below consumes three trees that a build step must create
; before ISCC is run. That build step is NOT part of this packet, and no code
; here generates them:
;
;   build\embeddable\   python.org embeddable CPython, extracted, with the
;                       ._pth edited to allow site-packages (pip needs it)
;   build\wheelhouse\   `pip download systemu` output — the systemu wheel plus
;                       every transitive dependency, as wheels, for AC5
;   build\launcher\     the launcher exe (daemon + dashboard + R-P7 hotkey host)
;
; Until that build step exists, compiling this script fails at [Files] with a
; missing-source error. That is the intended failure: loudly missing beats an
; installer that silently ships an empty environment.

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

[Files]
; The embeddable CPython runtime — extracted, never registered system-wide.
Source: "build\embeddable\*"; DestDir: "{app}\{#EnvDirName}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The offline wheelhouse: the systemu wheel and every dependency wheel (AC5).
Source: "build\wheelhouse\*"; DestDir: "{app}\{#WheelhouseDirName}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The launcher: starts the daemon, opens the dashboard, hosts the R-P7 hotkey.
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

; 3. The first-run wizard: provider key -> verify -> T3 consult or palette.
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
