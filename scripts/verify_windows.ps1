#requires -Version 5.1
<#
.SYNOPSIS
    Verify a Math Checker V5 installation on Windows.

.DESCRIPTION
    Runs the checks that prove this checkout can actually work: the interpreter
    and native runtime, dependency resolution, the backend suite, the web
    interface build and its tests, the optional desktop client, and a real
    launch of the app with an HTTP health check.

    Every check is reported as PASS, FAIL, SKIP or NOT RUN. The exit code is 1
    when a required check fails, 0 otherwise (a SKIP is never counted as a pass).

.PARAMETER Quick
    Run a small, fast subset of the backend suite instead of all ~2000 tests.

.PARAMETER SkipBackend
    Do not run the backend test suite (imports and pip check still run).

.PARAMETER SkipFrontend
    Do not run the frontend build check or its test suite.

.PARAMETER SkipDesktop
    Do not build or test the .NET desktop client.

.PARAMETER SkipAppSmoke
    Do not start the app and probe its HTTP endpoints.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\verify_windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\verify_windows.ps1 -Quick -SkipDesktop
#>
[CmdletBinding()]
param(
    [switch]$Quick,
    [switch]$SkipBackend,
    [switch]$SkipFrontend,
    [switch]$SkipDesktop,
    [switch]$SkipAppSmoke
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$script:Results = New-Object System.Collections.Generic.List[object]

function Add-Result([string]$Check, [string]$Status, [string]$Detail) {
    $script:Results.Add([pscustomobject]@{ Check = $Check; Status = $Status; Detail = $Detail })
    $colour = switch ($Status) {
        'PASS' { 'Green' }
        'FAIL' { 'Red' }
        'SKIP' { 'Yellow' }
        default { 'Gray' }
    }
    Write-Host ("  {0,-5} {1}" -f $Status, $Check) -ForegroundColor $colour
    if ($Detail) { Write-Host "        $Detail" -ForegroundColor Gray }
}

function Write-Section([string]$Text) {
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Get-CommandPath([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-NodeDir {
    $node = Get-CommandPath 'node'
    if ($node) { return (Split-Path -Parent $node) }
    foreach ($r in @("$env:ProgramFiles\nodejs", "$env:LOCALAPPDATA\Programs\nodejs",
                     "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_*\node-*")) {
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

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

Write-Host ''
Write-Host 'Math Checker V5 - verification' -ForegroundColor White
Write-Host "  project  $Root"
Write-Host "  started  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

# ------------------------------------------------------------------ environment
Write-Section 'Environment'

if ([Environment]::OSVersion.Version.Major -ge 10 -and [Environment]::Is64BitOperatingSystem) {
    Add-Result 'Windows 10+ 64-bit' 'PASS' $([Environment]::OSVersion.VersionString)
} else {
    Add-Result 'Windows 10+ 64-bit' 'FAIL' $([Environment]::OSVersion.VersionString)
}

if (Test-Path "$env:SystemRoot\System32\msvcp140.dll") {
    Add-Result 'Microsoft Visual C++ runtime (msvcp140.dll)' 'PASS' 'present in System32'
} else {
    Add-Result 'Microsoft Visual C++ runtime (msvcp140.dll)' 'FAIL' 'missing - run: winget install --id Microsoft.VCRedist.2015+.x64'
}

$venvPython = Join-Path $Root '.venv\Scripts\python.exe'
$python = $null
$pythonVersion = $null
if (Test-Path $venvPython) {
    $pythonVersion = (& $venvPython -c "import sys;print('%d.%d.%d' % sys.version_info[:3])") 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $venvPython }
}
if (-not $python) {
    Add-Result 'Project virtual environment' 'FAIL' '.venv\Scripts\python.exe is missing or unrunnable - run setup.bat (or scripts\setup_windows.ps1)'
} elseif ([version]$pythonVersion -ge [version]'3.12') {
    Add-Result 'Project virtual environment' 'PASS' "Python $pythonVersion"
} else {
    Add-Result 'Project virtual environment' 'FAIL' "Python $pythonVersion is older than the required 3.12"
}

# ------------------------------------------------------------------ dependencies
if ($python) {
    Write-Section 'Dependencies'

    & $python -m pip check 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Add-Result 'pip check (dependency conflicts)' 'PASS' 'no broken requirements'
    } else {
        Add-Result 'pip check (dependency conflicts)' 'FAIL' 'run: .venv\Scripts\python.exe -m pip check'
    }

    $modules = 'fastapi', 'uvicorn', 'multipart', 'google.genai', 'openpyxl', 'reportlab', 'pymupdf', 'pypdf', 'PIL', 'cryptography'
    $failed = @()
    foreach ($m in $modules) {
        & $python -c "import $m" 2>$null
        if ($LASTEXITCODE -ne 0) { $failed += $m }
    }
    if ($failed.Count -eq 0) {
        Add-Result 'Runtime imports' 'PASS' ($modules -join ', ')
    } else {
        Add-Result 'Runtime imports' 'FAIL' ("could not import: " + ($failed -join ', '))
    }

    # A stale/mismatched lockfile is information, not a failure: requirements.txt
    # allows newer patch/minor versions by design.
    $lock = Join-Path $Root 'backend\requirements-lock.txt'
    if (Test-Path $lock) {
        $pinned = (Get-Content $lock | Where-Object { $_ -match '^[A-Za-z0-9_.-]+==' }).Count
        Add-Result 'Lockfile present' 'PASS' "$pinned pinned packages in backend/requirements-lock.txt"
    } else {
        Add-Result 'Lockfile present' 'SKIP' 'backend/requirements-lock.txt not found'
    }
}

# ------------------------------------------------------------------ backend suite
if ($python -and -not $SkipBackend) {
    Write-Section 'Backend test suite'
    $pytestArgs = @('-m', 'pytest')
    if ($Quick) {
        $pytestArgs += @('backend/tests/test_config_loading.py', 'backend/tests/test_data_location.py',
                         'backend/tests/test_legacy_engine.py', 'backend/tests/test_mark_scheme_validation.py',
                         'backend/tests/test_api_mock.py')
    } else {
        $pytestArgs += @('backend/tests', '-q')
    }
    Push-Location $Root
    try {
        $started = Get-Date
        & $python @pytestArgs 2>&1 | Tee-Object -Variable pytestOutput | Select-Object -Last 25 | Out-Host
        $code = $LASTEXITCODE
        $seconds = [int]((Get-Date) - $started).TotalSeconds
    } finally { Pop-Location }

    $summary = ($pytestOutput | Where-Object { $_ -match '\d+ (passed|failed)' } | Select-Object -Last 1)
    if ($code -eq 0) {
        Add-Result 'Backend tests' 'PASS' "$summary (${seconds}s)"
    } else {
        Add-Result 'Backend tests' 'FAIL' "$summary - rerun: .venv\Scripts\python.exe -m pytest backend\tests -q"
    }
}

# ------------------------------------------------------------------ frontend
if (-not $SkipFrontend) {
    Write-Section 'Web interface'

    $distIndex = Join-Path $Root 'frontend\dist\index.html'
    if (Test-Path $distIndex) {
        Add-Result 'Built interface (frontend/dist)' 'PASS' "$((Get-Item $distIndex).LastWriteTime)"
    } else {
        Add-Result 'Built interface (frontend/dist)' 'FAIL' 'missing - run: cd frontend; npm ci; npm run build'
    }

    $nodeDir = Find-NodeDir
    if (-not $nodeDir) {
        Add-Result 'Frontend tests' 'SKIP' 'Node.js not found'
    } else {
        $node = Join-Path $nodeDir 'node.exe'
        Push-Location (Join-Path $Root 'frontend')
        try {
            $started = Get-Date
            & $node --test 'tests/*.test.ts' 2>&1 | Tee-Object -Variable nodeOutput | Select-Object -Last 15 | Out-Host
            $code = $LASTEXITCODE
            $seconds = [int]((Get-Date) - $started).TotalSeconds
        } finally { Pop-Location }
        $summary = ($nodeOutput | Where-Object { $_ -match 'ℹ (pass|fail|skipped)' } | ForEach-Object { $_.Trim() }) -join ' '
        if ($code -eq 0) {
            Add-Result 'Frontend tests' 'PASS' "$summary (${seconds}s)"
        } else {
            Add-Result 'Frontend tests' 'FAIL' "$summary - rerun: cd frontend; node --test `"tests/*.test.ts`""
        }
    }
}

# ------------------------------------------------------------------ desktop
if (-not $SkipDesktop) {
    Write-Section 'Desktop client (.NET)'
    $dotnet = Find-Dotnet
    if (-not $dotnet) {
        Add-Result 'Desktop client' 'SKIP' '.NET 10 SDK not found (optional; the web app does not need it)'
    } else {
        Push-Location $Root
        try {
            & $dotnet build 'desktop\MathChecker.Desktop.slnx' -v q --nologo 2>&1 | Select-Object -Last 8 | Out-Host
            $buildCode = $LASTEXITCODE
            Add-Result 'Desktop build' $(if ($buildCode -eq 0) { 'PASS' } else { 'FAIL' }) 'dotnet build desktop\MathChecker.Desktop.slnx'

            if ($buildCode -eq 0) {
                & $dotnet test 'desktop\tests\MathChecker.Desktop.Tests\MathChecker.Desktop.Tests.csproj' --no-build -v q --nologo 2>&1 | Select-Object -Last 6 | Out-Host
                $testCode = $LASTEXITCODE
                Add-Result 'Desktop tests' $(if ($testCode -eq 0) { 'PASS' } else { 'FAIL' }) 'MathChecker.Desktop.Tests'
            } else {
                Add-Result 'Desktop tests' 'NOT RUN' 'the build failed'
            }
        } finally { Pop-Location }
    }
}

# ------------------------------------------------------------------ launch smoke test
if ($python -and -not $SkipAppSmoke) {
    Write-Section 'Launch check (real HTTP)'

    $port = Get-FreePort
    $dataDir = Join-Path $env:TEMP ("mc5-verify-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $stdout = Join-Path $dataDir 'stdout.log'
    $stderr = Join-Path $dataDir 'stderr.log'

    $previousData = $env:MC5_DATA_DIR
    $previousBrowser = $env:MC5_NO_BROWSER
    $env:MC5_DATA_DIR = $dataDir
    $env:MC5_NO_BROWSER = '1'

    $proc = $null
    try {
        $proc = Start-Process -FilePath $python -ArgumentList @("`"$Root\main.py`"", "$port") `
            -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr

        # A throw-away MC5_DATA_DIR keeps the real data folder untouched.
        $healthy = $false
        $deadline = (Get-Date).AddSeconds(120)
        while ((Get-Date) -lt $deadline) {
            if ($proc.HasExited) { break }
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 3
                if ($response.StatusCode -eq 200 -and $response.Content -match 'ok') { $healthy = $true; break }
            } catch { Start-Sleep -Milliseconds 500 }
        }

        if ($healthy) {
            Add-Result 'GET /api/health' 'PASS' "HTTP 200 on 127.0.0.1:$port"
            try {
                $page = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 10
                if ($page.StatusCode -eq 200 -and $page.Content -match '<html') {
                    Add-Result 'GET / (built UI served)' 'PASS' "$($page.RawContentLength) bytes"
                } else {
                    Add-Result 'GET / (built UI served)' 'FAIL' "HTTP $($page.StatusCode) without an HTML document"
                }
            } catch {
                Add-Result 'GET / (built UI served)' 'FAIL' $_.Exception.Message
            }
            try {
                $dash = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/dashboard" -UseBasicParsing -TimeoutSec 10
                Add-Result 'GET /api/dashboard' $(if ($dash.StatusCode -eq 200) { 'PASS' } else { 'FAIL' }) "HTTP $($dash.StatusCode)"
            } catch {
                Add-Result 'GET /api/dashboard' 'FAIL' $_.Exception.Message
            }
        } else {
            $tail = ''
            if (Test-Path $stderr) { $tail = (Get-Content $stderr -Tail 8) -join ' | ' }
            if (Test-Path $stdout) { $tail = (Get-Content $stdout -Tail 8) -join ' | ' }
            Add-Result 'GET /api/health' 'FAIL' "the app did not answer on 127.0.0.1:$port within 120s. Last output: $tail"
            Add-Result 'GET / (built UI served)' 'NOT RUN' 'the app did not start'
        }
    } finally {
        if ($proc -and -not $proc.HasExited) {
            & taskkill.exe /PID $proc.Id /T /F 2>&1 | Out-Null
        }
        $env:MC5_DATA_DIR = $previousData
        $env:MC5_NO_BROWSER = $previousBrowser
        Remove-Item -Path $dataDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------ summary
Write-Section 'Summary'

$pass = @($script:Results | Where-Object { $_.Status -eq 'PASS' }).Count
$fail = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
$skip = @($script:Results | Where-Object { $_.Status -in @('SKIP', 'NOT RUN') }).Count

Write-Host ("  {0} passed, {1} failed, {2} skipped/not run" -f $pass, $fail, $skip) -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Red' })

if ($fail -gt 0) {
    Write-Host ''
    foreach ($r in ($script:Results | Where-Object { $_.Status -eq 'FAIL' })) {
        Write-Host "  FAIL  $($r.Check): $($r.Detail)" -ForegroundColor Red
    }
    Write-Host ''
    Write-Host '  See docs/WINDOWS_SETUP.md for the fix for each item.' -ForegroundColor Yellow
    exit 1
}

Write-Host '  All required checks passed.' -ForegroundColor Green
if ($skip -gt 0) {
    Write-Host '  Skipped checks are reported above and are not counted as passes.' -ForegroundColor Yellow
}
exit 0
