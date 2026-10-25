; MATH_CHECKER_V5 — Inno Setup 6 installer.
;
; Built by desktop\build_release.ps1 (which passes SourceDir and OutputDir):
;   iscc /DSourceDir=<staged tree> /DOutputDir=<output> MathCheckerDesktop.iss
;
; The staged tree contains the existing V5 backend, its bundled Python runtime
; and the published WPF client, so the installed application needs no Python or
; .NET installation on the target machine.

#ifndef SourceDir
  #define SourceDir "..\artifacts\MathCheckerDesktop"
#endif
#ifndef OutputDir
  #define OutputDir "..\artifacts"
#endif

#define AppName "MATH_CHECKER_V5"
#define AppVersion "1.0.0"
#define AppPublisher "Math Checker"
#define AppExeName "V5.exe"
#define AppId "{{2C8E1F4A-9B37-4D51-8A6E-6F0D4B71C2E9}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\MATH_CHECKER_V5
DefaultGroupName=MATH_CHECKER_V5
UninstallDisplayIcon={app}\desktop\{#AppExeName}
SetupIconFile=app.ico
OutputDir={#OutputDir}
OutputBaseFilename=MATH_CHECKER_V5_Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install: a teacher can install it without an administrator,
; and the app's settings and data already live in the user's profile.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\desktop\{#AppExeName}"
Name: "{group}\Desktop logs"; Filename: "{localappdata}\MathCheckerDesktop\logs"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\desktop\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\desktop\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The client's own settings and logs stay in %APPDATA%/%LOCALAPPDATA% so an
; upgrade keeps the user's window state and diagnostics. The backend's data
; folder ({app}\data or one the teacher chose) is never touched: an uninstall
; must not delete classes, marked papers or API keys. Program folders are
; removed wholesale because running the backend writes __pycache__ files the
; installer never placed.
Type: filesandordirs; Name: "{app}\desktop"
Type: filesandordirs; Name: "{app}\backend"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\__pycache__"
