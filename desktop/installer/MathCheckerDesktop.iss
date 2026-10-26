; Math Checker — Inno Setup 6 installer for the MATH_CHECKER_V5 desktop app.
;
; Built by desktop\build_release.ps1:
;   iscc /DSourceDir=<staged tree> /DOutputDir=<release> /DAppVersion=<version> ...
;
; The staged tree already contains everything the recipient needs: the existing
; V5 backend, its bundled CPython runtime with every dependency, and the
; self-contained WPF client. No Python, .NET, Git or admin rights are required.
;
; Where the app keeps its state (never in {app}):
;   %LOCALAPPDATA%\MathChecker        database, mark schemes, papers, results
;   %APPDATA%\MathCheckerDesktop      desktop settings, window state, logs
; Those folders are deliberately NOT touched by [UninstallDelete]: uninstalling
; removes the program, never the teacher's classes, marks or API keys. The same
; reason keeps them safe across upgrades (same AppId => in-place upgrade).

#ifndef SourceDir
  #define SourceDir "..\artifacts\MATH_CHECKER_V5"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\release"
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "Math Checker"
#define AppId "{{2C8E1F4A-9B37-4D51-8A6E-6F0D4B71C2E9}"
#define AppExeName "V5.exe"
#define AppPublisher "Math Checker"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/AetherRIP/MATH_CHECKER_V5
AppSupportURL=https://github.com/AetherRIP/MATH_CHECKER_V5
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\desktop\{#AppExeName}
DefaultDirName={localappdata}\Programs\MathChecker
DefaultGroupName={#AppName}
; Per-user install: a teacher needs no administrator account, and the app's
; data already lives in that user's profile. An administrator can still choose
; an all-users install from the wizard.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=app.ico
OutputDir={#OutputDir}
OutputBaseFilename=MathChecker-Setup-x64-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
AllowNoIcons=yes
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
; Detecting the running client gives a clear "close Math Checker" prompt
; instead of failing halfway through replacing V5.exe. The name is the mutex
; the app itself takes (Core\AppInstance.cs).
AppMutex=Local\MathChecker.V5.Desktop
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} {#AppVersion} Setup
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoVersion={#AppVersion}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\desktop\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\desktop\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\desktop\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The client and backend write caches into folders the installer never placed
; (__pycache__ under backend\ and runtime\), so remove the program folders
; wholesale. User data lives outside {app} and is intentionally preserved -
; see the header. A reinstall after uninstall finds the same classes, results
; and keys.
Type: filesandordirs; Name: "{app}\desktop"
Type: filesandordirs; Name: "{app}\backend"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\.pytest_cache"

[Messages]
; Say exactly where the data is, so "uninstall" is not assumed to mean
; "delete my grades".
WelcomeLabel2=This will install [name/ver] on your computer.%n%nMath Checker keeps its classes, papers and marks in your own Windows profile, not in the program folder, so uninstalling later removes the application and leaves your data untouched.%n%nInternet access and a Gemini API key are needed for correction; both can be set up after installation.
