; Math Checker — Windows installer (Inno Setup 6)
; ---------------------------------------------------------------
; Visual identity mirrors the application: dark glass surfaces, the
; #6d8dff → #8a6cff accent, clean typography and a branded wizard.
; Assets are generated from the same palette by make_installer_assets.py.
;
; Build:  iscc /DSourceDir=<release_staging> MathChecker.iss
; ---------------------------------------------------------------

#define AppName "@NAME@"
#define AppVersion "@VERSION@"
#define AppBuild "@BUILD@"
#define AppPublisher "Math Checker"
#define AppExeName "MathChecker.bat"
; Public / LAN access launcher: starts the server AND the tunnel, verifies the
; public address end to end and reports the real reason when it fails.
#define AppPublicExe "start_public.bat"
; Shutdown helper: proves the program on the port is Math Checker, then stops
; it together with its correction workers and tunnel.
#define AppStopExe "Stop.bat"

#ifndef SourceDir
  #define SourceDir "..\release_staging"
#endif

; Location of the generated brand assets. build_release.py passes this as an
; absolute path (/DAssetsDir=...) because the .iss is written into release/.
#ifndef AssetsDir
  #define AssetsDir "assets"
#endif

; Whether build_release.py bundled a Python runtime. Decided at BUILD time
; (the installer cannot look at {app} before files are copied), so the
; recipient warning only appears for a runtime-less build.
#ifndef HasRuntime
  #define HasRuntime "1"
#endif

[Setup]
AppId={{7B2E5C51-4A6D-4F1B-9C3E-8D2A1F6E9B04}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion} (build {#AppBuild})
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Math Checker
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes
PrivilegesRequired=admin
; Default is a normal all-users install into Program Files. The override lets
; an administrator (or a teacher without admin rights) install per-user with
; `MathChecker_Setup.exe /CURRENTUSER`, and lets the release test do a clean,
; non-elevated install/uninstall cycle.
PrivilegesRequiredOverridesAllowed=commandline
WizardStyle=modern
WizardSizePercent=120
WizardImageFile={#AssetsDir}\wizard-large.bmp
WizardSmallImageFile={#AssetsDir}\wizard-small.bmp
SetupIconFile={#AssetsDir}\mathchecker.ico
UninstallDisplayIcon={app}\assets\mathchecker.ico
OutputBaseFilename=MathChecker_Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; Everything staged by build_release.py. Customer data never lives here.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "portable.flag"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; User data is kept OUT of the program folder and is NEVER removed by an
; uninstall or overwritten by an update — this is the whole point of the
; separation between application files and user data.
; These are the folders the application actually creates under its data
; directory (config.toml and secrets.toml live directly in that folder), so a
; fresh install looks the same whether it was installed or extracted.
Name: "{localappdata}\MathChecker"; Flags: uninsneveruninstall
Name: "{localappdata}\MathChecker\secrets"; Flags: uninsneveruninstall
Name: "{localappdata}\MathChecker\logs"; Flags: uninsneveruninstall
Name: "{localappdata}\MathChecker\reports"; Flags: uninsneveruninstall
Name: "{localappdata}\MathChecker\tmp"; Flags: uninsneveruninstall
Name: "{localappdata}\MathChecker\mark_schemes"; Flags: uninsneveruninstall
Name: "{localappdata}\MathChecker\blueprints"; Flags: uninsneveruninstall
Name: "{localappdata}\MathChecker\retained"; Flags: uninsneveruninstall

[UninstallDelete]
; Remove the whole program tree on uninstall. This is safe because ALL user
; data (settings, keys, database, reports) lives under %LOCALAPPDATA%\MathChecker
; and is declared uninsneveruninstall in [Dirs] — it is never touched here.
; It also clears any bytecode or caches the app may have generated.
Type: filesandordirs; Name: "{app}"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\mathchecker.ico"; Comment: "AI-assisted mathematics marking"
Name: "{group}\{#AppName} (Demo mode)"; Filename: "{app}\{#AppExeName}"; Parameters: "--demo"; WorkingDir: "{app}"; IconFilename: "{app}\assets\mathchecker.ico"; Comment: "Explore with synthetic data — no real accounts"
Name: "{group}\{#AppName} (Public / LAN access)"; Filename: "{app}\{#AppPublicExe}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\mathchecker.ico"; Comment: "Also publish a verified public address for phone/tablet access"
Name: "{group}\Stop {#AppName}"; Filename: "{app}\{#AppStopExe}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\mathchecker.ico"; Comment: "Stop the running Math Checker and its background workers"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\mathchecker.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExeName}"; Parameters: "--demo"; Description: "Launch in Demo mode (synthetic data)"; Flags: nowait postinstall skipifsilent unchecked
Filename: "{app}\{#AppPublicExe}"; Description: "Launch with Public / LAN access (verified public address)"; Flags: nowait postinstall skipifsilent unchecked

[Code]

var
  BrandLabel: TNewStaticText;

procedure InitializeWizard();
begin
  { Branded welcome copy instead of the generic Inno wording. }
  WizardForm.WelcomeLabel1.Caption := 'Welcome to {#AppName}';
  WizardForm.WelcomeLabel2.Caption :=
    'AI-assisted mathematics marking for teachers.' + #13#10 + #13#10 +
    'This will install {#AppName} {#AppVersion} on your computer. ' +
    'Your own classes, students and API keys are never included and are ' +
    'created fresh, for you only, the first time you run the app.';
  WizardForm.FinishedHeadingLabel.Caption := '{#AppName} is ready';
  WizardForm.FinishedLabel.Caption :=
    'Math Checker has been installed. Launch it and the first-run setup ' +
    'will walk you through connecting your AI provider and Google Classroom.';

  { A small brand plate echoing the app's header. }
  BrandLabel := TNewStaticText.Create(WizardForm);
  BrandLabel.Parent := WizardForm;
  BrandLabel.Caption := 'MATH CHECKER';
  BrandLabel.Font.Name := 'Segoe UI';
  BrandLabel.Font.Size := 9;
  BrandLabel.Font.Style := [fsBold];
  BrandLabel.Font.Color := $00FF8D6D;   { BGR of accent #6d8dff }
  BrandLabel.Left := WizardForm.ClientWidth - ScaleX(140);
  BrandLabel.Top := ScaleY(12);
  BrandLabel.Width := ScaleX(130);
  BrandLabel.Alignment := taRightJustify;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpReady then
  begin
    { The ready page shows a summary memo; append the data-separation note
      as plain text (no custom layout, so this is version-proof). }
    if Pos('Your data stays separate', WizardForm.ReadyMemo.Text) = 0 then
      WizardForm.ReadyMemo.Text := WizardForm.ReadyMemo.Text + #13#10 + #13#10 +
        'Your data stays separate: application files go to the program ' +
        'folder, while your settings, API keys, database and reports live in ' +
        '%LOCALAPPDATA%\MathChecker and are kept during updates.';
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    { Friendly guard: only a runtime-less build needs system Python. }
    if '{#HasRuntime}' = '0' then
    begin
      if MsgBox('This package does not include a bundled Python runtime. ' +
                'The computer will need Python 3.12 or later installed to run ' +
                '{#AppName}.' + #13#10 + #13#10 + 'Continue anyway?',
                mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
  end;
end;
