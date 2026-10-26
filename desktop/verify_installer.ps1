# Verifies the built installer the way a recipient uses it.
#
#   powershell -ExecutionPolicy Bypass -File desktop\verify_installer.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\verify_installer.ps1 -Installer release\MathChecker-Setup-x64-1.0.0.exe
#
# What it proves (each line becomes PASS/FAIL in the report):
#   * the installer runs silently and completes;
#   * program files, the uninstaller and the Start Menu entry exist;
#   * the registry lists it in Installed apps;
#   * the installed client starts with a sanitised environment (no Python,
#     .NET SDK or Git on PATH) and brings up ITS OWN bundled backend;
#   * the backend's data folder is the per-user one, not the install folder,
#     and a first launch creates a fresh database there;
#   * uninstalling removes the program and start-menu entry but KEEPS user data;
#   * reinstalling works afterwards.
#
# The test uses a fresh install folder under %TEMP% (never the repository) and
# moves any existing %LOCALAPPDATA%\MathChecker aside for the duration, so a
# first launch is genuinely a first launch. Nothing here needs real credentials:
# only /api/health and /api/privacy/info are read.
#
# Exit code 0 = every check passed.

[CmdletBinding()]
param(
    [string]$Installer = '',
    [int]$TimeoutSeconds = 180,
    [switch]$KeepInstalled
)

$ErrorActionPreference = 'Stop'
$desktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $desktopDir
$report = New-Object System.Collections.ArrayList
$script:failed = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if (-not $ok) { $script:failed++ }
    [void]$report.Add([pscustomobject]@{
            check = $name; result = if ($ok) { 'PASS' } else { 'FAIL' }; detail = $detail
        })
    $colour = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("  [{0}] {1}{2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $name,
        $(if ($detail) { " - $detail" } else { '' })) -ForegroundColor $colour
}

function Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }

function Wait-Until([scriptblock]$condition, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-App {
    Get-Process V5 -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.CloseMainWindow() | Out-Null } catch { }
    }
    Wait-Until { -not (Get-Process V5 -ErrorAction SilentlyContinue) } 20 | Out-Null
    Get-Process V5 -ErrorAction SilentlyContinue | ForEach-Object { $_.Kill() }
    # The backend is a child process; it exits when the client stops, but a
    # leftover from this test must never answer the next check.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'main\.py\s+--serve' -and $_.CommandLine -match [regex]::Escape($script:installDir) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
}

function Get-BackendFor([string]$installDir) {
    $processes = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match [regex]::Escape($installDir) -and $_.CommandLine -match 'main\.py\s+--serve' }
    foreach ($process in $processes) {
        $match = [regex]::Match($process.CommandLine, '--serve\s+(\d+)')
        if ($match.Success) {
            return [pscustomobject]@{ ProcessId = $process.ProcessId; Port = [int]$match.Groups[1].Value; CommandLine = $process.CommandLine }
        }
    }
    return $null
}

# --- 0. the artefact ---------------------------------------------------------
Step 'Locating the installer'
if ([string]::IsNullOrWhiteSpace($Installer)) {
    $Installer = Get-ChildItem (Join-Path $repoRoot 'release') -Filter 'MathChecker-Setup-x64-*.exe' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
Check 'installer exists' ([bool]$Installer -and (Test-Path $Installer)) $Installer
$version = if ($Installer -match '(\d+\.\d+\.\d+)') { $Matches[1] } else { '1.0.0' }
$installerSize = (Get-Item $Installer).Length
Write-Host ("  {0}  {1:N1} MB" -f (Split-Path -Leaf $Installer), ($installerSize / 1MB))

$dataDir = Join-Path $env:LOCALAPPDATA 'MathChecker'
$settingsDir = Join-Path $env:APPDATA 'MathCheckerDesktop'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Math Checker'
$script:installDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mc5_verify_install_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$installLog = Join-Path ([System.IO.Path]::GetTempPath()) ("mc5_verify_install_" + [guid]::NewGuid().ToString('N').Substring(0, 8) + ".log")
$uninstallLog = Join-Path ([System.IO.Path]::GetTempPath()) ("mc5_verify_uninstall_" + [guid]::NewGuid().ToString('N').Substring(0, 8) + ".log")

Step 'Preparing a clean first launch'
Get-Process V5 -ErrorAction SilentlyContinue | ForEach-Object { $_.Kill() }
$dataBackup = $null
if (Test-Path $dataDir) {
    $dataBackup = "$dataDir.verify-backup"
    if (Test-Path $dataBackup) { Remove-Item $dataBackup -Recurse -Force }
    Move-Item $dataDir $dataBackup
    Write-Host "  existing data folder moved to $dataBackup (restored at the end)"
}
Check 'no user data folder before first launch' (-not (Test-Path $dataDir))

# --- 1. install --------------------------------------------------------------
Step "Installing to $script:installDir"
$install = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
    "/DIR=$script:installDir", "/LOG=$installLog") -ErrorAction Stop
Check 'installer exit code is 0' ($install.ExitCode -eq 0) "exit $($install.ExitCode)"

$exe = Join-Path $script:installDir 'desktop\V5.exe'
$uninstaller = Join-Path $script:installDir 'unins000.exe'
Check 'client installed' (Test-Path $exe) $exe
Check 'uninstaller installed' (Test-Path $uninstaller)
Check 'bundled backend installed' (Test-Path (Join-Path $script:installDir 'main.py'))
Check 'bundled Python installed' (Test-Path (Join-Path $script:installDir 'runtime\python.exe'))
Check 'start-menu entry created' (Test-Path (Join-Path $startMenu 'Math Checker.lnk'))
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{2C8E1F4A-9B37-4D51-8A6E-6F0D4B71C2E9}_is1'
$entry = Get-ItemProperty -Path $uninstallKey -ErrorAction SilentlyContinue
Check 'listed in Installed apps' ($null -ne $entry -and $entry.DisplayName -match 'Math Checker') ($entry.DisplayName)
if ($entry) {
    Check 'registry version matches the installer' ($entry.DisplayVersion -eq $version) $entry.DisplayVersion
}
Check 'no data folder inside the installation' (-not (Test-Path (Join-Path $script:installDir 'data')))

# The installed product must not carry this developer's checkout paths (a PDB
# or a stale __pycache__ would).
$leaks = @()
foreach ($relative in @('desktop\V5.dll', 'desktop\V5.exe', 'backend\app\main.py')) {
    $file = Join-Path $script:installDir $relative
    if (-not (Test-Path $file)) { continue }
    $text = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($file))
    foreach ($needle in @($repoRoot, $env:USERPROFILE)) {
        if ($needle -and $text.Contains($needle)) { $leaks += "$relative <- $needle" }
    }
}
Check 'no developer path inside installed binaries' ($leaks.Count -eq 0) ($leaks -join '; ')

# --- 2. first launch, sanitised environment ----------------------------------
Step 'Launching the installed client without developer tools on PATH'
# The recipient's machine has no Python/.NET/Git. Launching through cmd with a
# system-only PATH (and no MC5_* overrides) is the closest this machine gets.
$launchCommand = "set `"PATH=$env:SystemRoot\system32;$env:SystemRoot`" && " +
    "set DOTNET_ROOT= && set MC5_DATA_DIR= && set MC5_DEMO= && set MC5_LOGIN_REQUIRED= && " +
    "start `"`" `"$exe`""
Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') -ArgumentList '/c', $launchCommand -WindowStyle Hidden
$windowed = Wait-Until { (Get-Process V5 -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }).Count -gt 0 } $TimeoutSeconds
Check 'application window opened' $windowed "within ${TimeoutSeconds}s"

$backend = $null
$healthy = Wait-Until {
    $script:backend = Get-BackendFor $script:installDir
    if (-not $script:backend) { return $false }
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:backend.Port)/api/health" -TimeoutSec 2
        return $response.status -eq 'ok'
    } catch { return $false }
} $TimeoutSeconds
Check 'installed backend answers /api/health' $healthy $(if ($script:backend) { "port $($script:backend.Port)" } else { 'no backend process' })
Check 'the backend came from the installed folder' ([bool]($script:backend -and $script:backend.CommandLine -match [regex]::Escape($script:installDir))) $script:backend.CommandLine

if ($healthy) {
    $privacy = $null
    try { $privacy = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:backend.Port)/api/privacy/info" -TimeoutSec 5 } catch { }
    $reported = $privacy.data_directory
    Check 'first launch created the per-user data folder' (Test-Path $dataDir) $dataDir
    Check 'backend reports the per-user data folder' ([bool]($reported -and ($reported -replace '\\\\', '\') -like "*MathChecker*")) $reported
    Check 'database created on first launch' (Test-Path (Join-Path $dataDir 'app.db'))
    Check 'demo mode is off (real, empty database)' ($privacy.environment.demo -eq $false)
} else {
    Check 'first launch created the per-user data folder' $false 'backend never became healthy'
}

# --- 3. settings survive a restart ------------------------------------------
Step 'Restarting the client'
Stop-App
Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') -ArgumentList '/c', $launchCommand -WindowStyle Hidden
$reopened = Wait-Until { (Get-Process V5 -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }).Count -gt 0 } $TimeoutSeconds
Check 'application restarts' $reopened
$healthyAgain = Wait-Until {
    $script:backend = Get-BackendFor $script:installDir
    if (-not $script:backend) { return $false }
    try { (Invoke-RestMethod -Uri "http://127.0.0.1:$($script:backend.Port)/api/health" -TimeoutSec 2).status -eq 'ok' } catch { $false }
} $TimeoutSeconds
Check 'backend restarts with the client' $healthyAgain
Check 'data survived the restart' (Test-Path (Join-Path $dataDir 'app.db'))
Stop-App
Check 'closing the client stops its backend' (-not [bool](Get-BackendFor $script:installDir))

# --- 4. uninstall keeps user data -------------------------------------------
Step 'Uninstalling'
$uninstall = Start-Process -FilePath $uninstaller -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$uninstallLog") -ErrorAction Stop
Check 'uninstaller exit code is 0' ($uninstall.ExitCode -eq 0) "exit $($uninstall.ExitCode)"
# The uninstaller relaunches itself from a temp copy and returns immediately,
# so wait for the effect instead of for the process.
Check 'program files removed' (Wait-Until { -not (Test-Path $exe) } 60)
Check 'start-menu entry removed' (Wait-Until { -not (Test-Path $startMenu) } 30)
Check 'removed from Installed apps' (Wait-Until { $null -eq (Get-ItemProperty -Path $uninstallKey -ErrorAction SilentlyContinue) } 30)
Check 'user data kept after uninstall' (Test-Path (Join-Path $dataDir 'app.db')) $dataDir

# --- 5. reinstall ------------------------------------------------------------
Step 'Reinstalling'
$reinstall = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
    "/DIR=$script:installDir") -ErrorAction Stop
Check 'reinstall exit code is 0' ($reinstall.ExitCode -eq 0)
Check 'client reinstalled' (Test-Path $exe)
Check 'user data still present after reinstall' (Test-Path (Join-Path $dataDir 'app.db'))

if ($KeepInstalled) {
    Write-Host "`n  left installed at $script:installDir (-KeepInstalled)"
} else {
    Step 'Removing the test installation'
    if (Test-Path $uninstaller) {
        Start-Process -FilePath $uninstaller -Wait -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') | Out-Null
    }
    Check 'test installation removed' (Wait-Until { -not (Test-Path $exe) } 60)
}

# --- 6. restore the machine --------------------------------------------------
Step 'Restoring this machine'
if (Test-Path $dataDir) { Remove-Item $dataDir -Recurse -Force }
if ($dataBackup -and (Test-Path $dataBackup)) {
    Move-Item $dataBackup $dataDir
    Write-Host "  previous data folder restored"
}
Remove-Item $installLog, $uninstallLog -Force -ErrorAction SilentlyContinue
if (Test-Path $script:installDir) { Remove-Item $script:installDir -Recurse -Force -ErrorAction SilentlyContinue }
if (Test-Path $settingsDir) { Write-Host "  desktop settings left in $settingsDir" }

# --- report ------------------------------------------------------------------
$reportPath = Join-Path $repoRoot ("docs\logs\INSTALLER_VERIFY_{0}.txt" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Path (Split-Path -Parent $reportPath) -Force | Out-Null
$lines = @(
    "Math Checker installer verification",
    "installer: $Installer",
    "version:   $version",
    "when:      $((Get-Date).ToUniversalTime().ToString('o'))",
    "host:      $env:COMPUTERNAME (Windows $([System.Environment]::OSVersion.Version))",
    "note:      install target was a fresh temp folder; PATH during launch was system-only;",
    "           Windows Sandbox / an isolated VM is not available on this machine, so a",
    "           truly pristine OS was NOT tested (see the release report).",
    ""
)
$lines += ($report | ForEach-Object { "  [{0}] {1}{2}" -f $_.result, $_.check, $(if ($_.detail) { " - $($_.detail)" } else { '' }) })
$lines += ""
$lines += ("RESULT: {0}  ({1} checks, {2} failed)" -f $(if ($script:failed -eq 0) { 'PASS' } else { 'FAIL' }), $report.Count, $script:failed)
$lines | Set-Content $reportPath -Encoding UTF8

Write-Host ''
if ($script:failed -eq 0) {
    Write-Host ("ALL {0} CHECKS PASSED" -f $report.Count) -ForegroundColor Green
} else {
    Write-Host ("{0} of {1} checks FAILED" -f $script:failed, $report.Count) -ForegroundColor Red
}
Write-Host "Report: $reportPath"
exit $(if ($script:failed -eq 0) { 0 } else { 1 })
