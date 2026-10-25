#requires -Version 5.1
<#
.SYNOPSIS
    Prepare Math Checker V5 on a fresh Windows installation.

.DESCRIPTION
    Checks the machine and this checkout, creates the project-local Python
    environment, installs the backend and frontend dependencies and builds the
    web interface. It never creates secrets: API keys and Google credentials are
    entered in the app's own setup wizard (Settings -> AI keys / Classroom).

    Nothing here needs administrator rights except the optional
    -InstallPrerequisites switch, which calls winget; the Visual C++ runtime and
    the .NET SDK are machine-wide packages, so Windows may show a UAC prompt.

.PARAMETER InstallPrerequisites
    Install the missing system tools with winget (Python 3.13, Node.js LTS,
    Microsoft Visual C++ 2015-2022 x64, .NET 10 SDK). Safe to combine with a
    normal run; already-installed packages are left alone.

.PARAMETER UseLockedDependencies
    Install the exact versions recorded in backend/requirements-lock.txt instead
    of the version ranges in backend/requirements-dev.txt. Use this to reproduce
    the environment the test suite was verified against.

.PARAMETER Force
    Run setup.bat even when .venv already has a working interpreter, and rebuild
    frontend/dist even when it exists.

.PARAMETER SkipFrontend
    Leave the React UI alone. start.bat then serves an empty page until
    `npm ci` and `npm run build` have been run in frontend/.

.PARAMETER SkipDesktop
    Do not report on the .NET desktop client (it is optional for the web app).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -InstallPrerequisites -UseLockedDependencies
#>
[CmdletBinding()]
param(
    [switch]$InstallPrerequisites,
    [switch]$UseLockedDependencies,
    [switch]$Force,
    [switch]$SkipFrontend,
    [switch]$SkipDesktop
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$script:Problems = New-Object System.Collections.Generic.List[string]

function Write-Section([string]$Text) {
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) { Write-Host "  OK    $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  WARN  $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) {
    Write-Host "  FAIL  $Text" -ForegroundColor Red
    $script:Problems.Add($Text)
}
function Write-Info([string]$Text) { Write-Host "        $Text" -ForegroundColor Gray }

function Get-CommandPath([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-Python {
    # The repository's own interpreter first: once .venv exists it is the one
    # every documented command uses.
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venv) {
        $v = & $venv -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0) { return [pscustomobject]@{ Path = $venv; Args = @(); Version = $v } }
    }
    $candidates = @()
    $py = Get-CommandPath 'py'
    if ($py) { $candidates += [pscustomobject]@{ Path = $py; Args = @('-3') } }
    $python = Get-CommandPath 'python'
    if ($python) { $candidates += [pscustomobject]@{ Path = $python; Args = @() } }
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", 'C:\Python*')) {
        if (Test-Path $dir) {
            foreach ($exe in (Get-ChildItem -Path $dir -Filter 'python.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue)) {
                $candidates += [pscustomobject]@{ Path = $exe.FullName; Args = @() }
            }
        }
    }
    foreach ($c in $candidates) {
        $argList = $c.Args
        $v = & $c.Path @argList -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Path = $c.Path; Args = $c.Args; Version = $v }
        }
    }
    return $null
}

function Find-NodeDir {
    # Get-Command misses a Node.js installed by winget until the shell is
    # restarted, so the winget package folder is checked directly.
    $node = Get-CommandPath 'node'
    if ($node) { return (Split-Path -Parent $node) }
    $roots = @(
        "$env:ProgramFiles\nodejs",
        "$env:LOCALAPPDATA\Programs\nodejs",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_*\node-*"
    )
    foreach ($r in $roots) {
        $found = Get-ChildItem -Path $r -Filter 'node.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.DirectoryName }
    }
    return $null
}

function Find-Dotnet {
    $d = Get-CommandPath 'dotnet'
    if ($d) { return $d }
    foreach ($p in @("$env:USERPROFILE\.dotnet\dotnet.exe", "$env:ProgramFiles\dotnet\dotnet.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Install-Winget([string]$Id, [string[]]$Extra) {
    if (-not (Get-CommandPath 'winget')) {
        Write-Fail "winget is not available, so $Id cannot be installed automatically."
        return $false
    }
    Write-Info "winget install $Id ..."
    $wingetArgs = @('install', '--id', $Id, '--silent', '--accept-package-agreements',
                    '--accept-source-agreements', '--disable-interactivity') + $Extra
    & winget @wingetArgs | Out-Host
    return ($LASTEXITCODE -eq 0)
}

Write-Host ''
Write-Host 'Math Checker V5 - Windows setup' -ForegroundColor White
Write-Host "  project  $Root"
Write-Host "  windows  $([Environment]::OSVersion.VersionString)"

# ----------------------------------------------------------------- prerequisites
Write-Section 'System prerequisites'

if ([Environment]::OSVersion.Version.Major -lt 10) {
    Write-Fail 'Windows 10 or later is required.'
} elseif (-not [Environment]::Is64BitOperatingSystem) {
    Write-Fail 'A 64-bit Windows installation is required.'
} else {
    Write-Ok 'Windows 10 or later, 64-bit.'
}

$needVc = -not (Test-Path "$env:SystemRoot\System32\msvcp140.dll")
if ($needVc) {
    Write-Fail 'Microsoft Visual C++ 2015-2022 x64 runtime is missing (msvcp140.dll). PyMuPDF, cryptography and Pillow cannot import without it.'
} else {
    Write-Ok 'Microsoft Visual C++ 2015-2022 x64 runtime present.'
}

$python = Find-Python
$needPython = $true
if ($python) {
    if ([version]$python.Version -ge [version]'3.12') {
        Write-Ok "Python $($python.Version) at $($python.Path)"
        $needPython = $false
    } else {
        Write-Fail "Python $($python.Version) is too old; 3.12 or later is required."
    }
} else {
    Write-Fail 'Python was not found (3.12 or later, 64-bit).'
}

$nodeDir = Find-NodeDir
$needNode = $true
if ($nodeDir) {
    $nodeVersion = (& (Join-Path $nodeDir 'node.exe') --version) 2>$null
    Write-Ok "Node.js $nodeVersion at $nodeDir"
    $needNode = $false
} else {
    Write-Warn 'Node.js was not found. It is only needed to build the web interface (frontend/dist).'
}

$dotnet = Find-Dotnet
if ($SkipDesktop) {
    Write-Info 'Desktop client check skipped (-SkipDesktop).'
} elseif ($dotnet) {
    $sdk = (& $dotnet --version) 2>$null
    Write-Ok ".NET SDK $sdk at $dotnet"
} else {
    Write-Warn '.NET 10 SDK not found. Only the optional WPF desktop client needs it.'
}

if ($InstallPrerequisites) {
    Write-Section 'Installing missing prerequisites (winget)'
    if ($needVc) {
        # Machine-wide package: Windows shows a UAC prompt.
        if (-not (Install-Winget 'Microsoft.VCRedist.2015+.x64' @())) {
            Write-Warn 'The Visual C++ runtime install did not complete. Approve the UAC prompt and re-run.'
        }
    }
    if ($needPython) {
        if (-not (Install-Winget 'Python.Python.3.13' @('--scope', 'user'))) {
            Write-Warn 'Python install did not complete; install it from https://www.python.org/downloads/ with "Add Python to PATH".'
        }
    }
    if ($needNode) {
        if (-not (Install-Winget 'OpenJS.NodeJS.LTS' @('--scope', 'user'))) {
            Write-Warn 'Node.js install did not complete; install it from https://nodejs.org/.'
        }
    }
    if (-not $SkipDesktop -and -not $dotnet) {
        if (-not (Install-Winget 'Microsoft.DotNet.SDK.10' @())) {
            Write-Warn 'The .NET SDK install did not complete (it is machine-wide and needs administrator approval).'
        }
    }
    Write-Warn 'Tools installed by winget are only on PATH in a NEW terminal. Reopen PowerShell before continuing if a step below cannot find them.'
    $python = Find-Python
    $nodeDir = Find-NodeDir
    $dotnet = Find-Dotnet
}

# ----------------------------------------------------------------- python environment
Write-Section 'Python environment'

$venvPython = Join-Path $Root '.venv\Scripts\python.exe'
if ($Force -or -not (Test-Path $venvPython)) {
    if (-not $python) {
        Write-Fail 'Cannot create .venv without Python. Install Python 3.12+ and re-run.'
    } else {
        Write-Info 'Running setup.bat (creates .venv, installs backend/requirements-dev.txt)...'
        # setup.bat pauses for a keypress at the end; this makes it return.
        $env:MC5_NONINTERACTIVE = '1'
        # The ABSOLUTE path matters: Push-Location changes PowerShell's location
        # but not the process working directory a native cmd.exe starts in.
        $setupBat = Join-Path $Root 'setup.bat'
        & cmd.exe /c "`"$setupBat`"" | Out-Host
        if (-not (Test-Path $venvPython)) {
            Write-Fail 'setup.bat did not produce .venv\Scripts\python.exe. Read its output above.'
        }
    }
} else {
    Write-Ok 'Existing .venv found (use -Force to rebuild it).'
}

if (Test-Path $venvPython) {
    $venvVersion = (& $venvPython -c "import sys;print('%d.%d.%d' % sys.version_info[:3])") 2>$null
    Write-Ok "Virtual environment interpreter: Python $venvVersion"

    if ($UseLockedDependencies) {
        $lock = Join-Path $Root 'backend\requirements-lock.txt'
        if (Test-Path $lock) {
            Write-Info 'Installing the exact versions from backend/requirements-lock.txt ...'
            & $venvPython -m pip install --disable-pip-version-check -r $lock | Out-Host
            if ($LASTEXITCODE -ne 0) { Write-Fail 'Installing backend/requirements-lock.txt failed.' }
        } else {
            Write-Fail 'backend/requirements-lock.txt is missing; cannot use -UseLockedDependencies.'
        }
    }

    Write-Info 'Checking installed packages...'
    & $venvPython -m pip check | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Fail 'pip reports conflicting or missing packages (see above).' }

    $importCheck = & $venvPython -c "import fastapi, uvicorn, multipart, google.genai, openpyxl, reportlab, pymupdf, pypdf, PIL, cryptography; print('ok')" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'All runtime imports resolve (including PyMuPDF).'
    } else {
        Write-Fail "A runtime import failed: $importCheck"
        if ($needVc) { Write-Info 'Install the Visual C++ runtime (see above), then re-run with -Force.' }
    }
}

# ----------------------------------------------------------------- web interface
Write-Section 'Web interface (frontend/dist)'

$distIndex = Join-Path $Root 'frontend\dist\index.html'
if ($SkipFrontend) {
    Write-Info 'Skipped (-SkipFrontend).'
} elseif (-not $nodeDir) {
    Write-Fail 'Node.js is missing, so frontend/dist cannot be built; start.bat would serve an empty page.'
} else {
    $npm = Join-Path $nodeDir 'npm.cmd'
    if (-not (Test-Path $npm)) { $npm = Join-Path $nodeDir 'npm' }
    if ((Test-Path $distIndex) -and -not $Force) {
        Write-Ok 'frontend/dist is already built (use -Force to rebuild).'
    } else {
        Push-Location (Join-Path $Root 'frontend')
        try {
            Write-Info 'npm ci ...'
            & $npm ci | Out-Host
            if ($LASTEXITCODE -ne 0) { Write-Fail 'npm ci failed.' }
            Write-Info 'npm run build ...'
            & $npm run build | Out-Host
            if ($LASTEXITCODE -ne 0) { Write-Fail 'npm run build failed.' }
        } finally { Pop-Location }
        if (Test-Path $distIndex) { Write-Ok 'frontend/dist/index.html built.' }
    }
}

# ----------------------------------------------------------------- configuration
Write-Section 'Configuration and secrets'

$dataDir = Join-Path $Root 'data'
Write-Info "Data folder used by a source checkout: $dataDir"
foreach ($item in @(
    @{ Path = (Join-Path $dataDir 'config.toml'); Label = 'config.toml (teacher, network, retention)' },
    @{ Path = (Join-Path $dataDir 'secrets.toml'); Label = 'secrets.toml (Gemini keys / OAuth tokens)' },
    @{ Path = (Join-Path $dataDir 'secrets\google_oauth_client.json'); Label = 'Google Classroom OAuth client' }
)) {
    if (Test-Path $item.Path) {
        Write-Ok "$($item.Label): present"
    } else {
        Write-Warn "$($item.Label): not present"
    }
}
Write-Info 'Missing credentials are not an installation error: create them in the app (Settings -> AI keys / Classroom).'
Write-Info 'Never put keys in this repository, frontend/.env.local, or a command line.'

# ----------------------------------------------------------------- finish
Write-Section 'Summary'

$desktopNote = '(skipped with -SkipDesktop)'
if (-not $SkipDesktop) {
    if ($dotnet) {
        $desktopNote = 'dotnet build desktop\MathChecker.Desktop.slnx'
    } else {
        $desktopNote = '(install the .NET 10 SDK first)'
    }
}

if ($script:Problems.Count -gt 0) {
    Write-Host ''
    Write-Host "Setup finished with $($script:Problems.Count) problem(s):" -ForegroundColor Red
    foreach ($p in $script:Problems) { Write-Host "  - $p" -ForegroundColor Red }
    Write-Host ''
    Write-Host 'Fix the items above, then re-run this script. See docs/WINDOWS_SETUP.md.' -ForegroundColor Yellow
    exit 1
}

Write-Host '  Everything needed to run the app is in place.' -ForegroundColor Green
Write-Host ''
Write-Host '  Launch:      start.bat            (or: start.bat 9000 for another port)'
Write-Host '  Demo mode:   start.bat --demo     (synthetic data, no API keys)'
Write-Host '  Verify:      powershell -ExecutionPolicy Bypass -File scripts\verify_windows.ps1'
Write-Host "  Desktop:     $desktopNote"
Write-Host ''
Write-Host '  First launch opens the setup wizard for the teacher name, AI keys and'
Write-Host '  Google Classroom. Those are entered in the app, never in this script.'
exit 0
