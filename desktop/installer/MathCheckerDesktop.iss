; Math Checker Desktop — Inno Setup 6 installer.
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

#define AppName "Math Checker Desktop"
#define AppVersion "1.0.0"
#define AppPublisher "Math Checker"
#define AppExeName "MathChecker.Desktop.exe"
#define AppId "{{2C8E1F4A-9B37-4D51-8A6E-6F0D4B71C2E9}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Math Checker Desktop
DefaultGroupName=Math Checker Desktop
UninstallDisplayIcon={app}\desktop\{#AppExeName}
SetupIconFile=app.ico
OutputDir={#OutputDir}
OutputBaseFilename=MathChecker_Desktop_Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
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
; folder is never touched by this installer.
Type: filesandordirs; Name: "{app}\desktop"
