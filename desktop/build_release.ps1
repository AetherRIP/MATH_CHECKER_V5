# Builds the distributable Windows release of Math Checker (MATH_CHECKER_V5).
#
#   powershell -ExecutionPolicy Bypass -File desktop\build_release.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\build_release.ps1 -SkipTests
#
# One command: tests -> self-contained WPF publish -> backend + bundled Python
# runtime -> validation -> portable ZIP -> Windows installer -> checksums.
#
# Output (release/ beside the repository root):
#   MathChecker-Setup-x64-<version>.exe    the installer to hand to a teacher
#   MathChecker-Portable-x64-<version>.zip the same tree, no installation
#   release_manifest.json                  what was built, and from what
#   SHA256SUMS.txt                         hashes for both artefacts
#
# The staged tree (desktop\artifacts\MATH_CHECKER_V5) is laid out so the client
# finds the backend with no developer environment:
#
#   <root>\main.py            existing V5 backend entry point
#   <root>\backend\           backend package (tests and caches excluded)
#   <root>\runtime\python.exe bundled CPython + runtime dependencies
#   <root>\desktop\           the self-contained WPF client (V5.exe)
#   <root>\start_desktop.bat  optional double-click launcher
#   <root>\README.txt         first-run guide
#
# Nothing about the recipient's machine is assumed: no Python, no .NET, no SDK,
# no PATH entry. The client walks up to main.py and runs runtime\python.exe.
# The backend resolves its data folder itself (app\data_location.py): an
# installed copy has no data\ folder and no .git, so it stores its database in
# %LOCALAPPDATA%\MathChecker - never inside the installation directory.

[CmdletBinding()]
param(
    [string]$Configuration = 'Release',
    [string]$Runtime = 'win-x64',
    [string]$BackendRuntime = '',
    [string]$Output = '',
    [string]$Stage = '',
    [string]$Version = '',
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$desktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $desktopDir
if ([string]::IsNullOrWhiteSpace($Output)) { $Output = Join-Path $repoRoot 'release' }
if ([string]::IsNullOrWhiteSpace($Stage)) { $Stage = Join-Path $desktopDir 'artifacts\MATH_CHECKER_V5' }
if ([string]::IsNullOrWhiteSpace($BackendRuntime)) {
    $BackendRuntime = Join-Path $repoRoot 'MathChecker_Distribution\build\runtime'
}

$script:checks = New-Object System.Collections.ArrayList
function Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    [void]$script:checks.Add([pscustomobject]@{ check = $name; result = $status; detail = $detail })
    $colour = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("  [{0}] {1}{2}" -f $status, $name, $(if ($detail) { " - $detail" } else { '' })) -ForegroundColor $colour
    if (-not $ok) { throw "check failed: $name ($detail)" }
}

# --- tool discovery -----------------------------------------------------------
# A "dotnet" on PATH is not proof of an SDK: a runtime-only install answers to
# the same name and then fails "publish" with "No .NET SDKs were found", which
# used to leave an old release in artifacts. Probe each host for an SDK.
function Resolve-Dotnet {
    $candidates = @()
    $onPath = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    if ($env:DOTNET_ROOT) { $candidates += (Join-Path $env:DOTNET_ROOT 'dotnet.exe') }
    $candidates += (Join-Path $env:USERPROFILE '.dotnet\dotnet.exe')
    $candidates += (Join-Path $env:LOCALAPPDATA 'Microsoft\dotnet\dotnet.exe')
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path $candidate)) { continue }
        $sdks = & $candidate --list-sdks 2>$null
        if ($LASTEXITCODE -eq 0 -and $sdks) { return $candidate }
    }
    throw 'No .NET SDK was found (only a runtime, if anything). Install the .NET 10 SDK from https://dotnet.microsoft.com/download and run this script again.'
}

function Resolve-Iscc {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $found = Get-Command iscc -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

function Get-ProjectVersion([string]$project) {
    $text = Get-Content $project -Raw
    $match = [regex]::Match($text, '<Version>([^<]+)</Version>')
    if (-not $match.Success) { throw "No <Version> in $project" }
    return $match.Groups[1].Value.Trim()
}

# Runs a native tool and returns its exit code plus captured output.
#
# PowerShell 5.1 turns a native program's STDERR lines into error records, and
# $ErrorActionPreference='Stop' then aborts the build on a harmless warning (the
# bundled interpreter prints one for `import fitz`, which would have failed the
# runtime check that proves the runtime works). Capturing both streams to files
# keeps the exit code as the only failure signal.
function Invoke-Tool([string]$exe, [string[]]$arguments) {
    $stamp = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $stdout = Join-Path ([System.IO.Path]::GetTempPath()) "mc5_build_$stamp.out"
    $stderr = Join-Path ([System.IO.Path]::GetTempPath()) "mc5_build_$stamp.err"
    $quoted = @($arguments | ForEach-Object {
            if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
        })
    $process = Start-Process -FilePath $exe -ArgumentList $quoted -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $text = ''
    foreach ($file in @($stdout, $stderr)) {
        if (Test-Path $file) {
            $content = Get-Content $file -Raw -ErrorAction SilentlyContinue
            if ($content) { $text += $content }
            Remove-Item $file -Force -ErrorAction SilentlyContinue
        }
    }
    return [pscustomobject]@{ Code = $process.ExitCode; Output = $text.Trim() }
}

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

$dotnet = Resolve-Dotnet
$project = Join-Path $desktopDir 'src\MathChecker.Desktop\MathChecker.Desktop.csproj'
if ([string]::IsNullOrWhiteSpace($Version)) { $Version = Get-ProjectVersion $project }
$iscc = Resolve-Iscc

Write-Host "Math Checker release $Version ($Runtime)" -ForegroundColor Cyan
Write-Host "  repository: $repoRoot"
Write-Host "  output:     $Output"
if (-not $iscc) {
    Write-Host '  WARNING: Inno Setup 6 was not found, so no installer will be produced.' -ForegroundColor Yellow
}

# --- 1. the bundled Python runtime -------------------------------------------
Step 'Checking the bundled Python runtime'
$python = Join-Path $BackendRuntime 'python.exe'
Check 'bundled python.exe exists' (Test-Path $python) $BackendRuntime

# A runtime can contain the whole stdlib and still be unusable: the .pyd
# extension modules are the ones that make socket/sqlite/ssl/crypto work, and
# they are easy to lose (a *.py[cod] ignore rule also matches *.pyd).
$requiredPyd = @('_socket.pyd', '_sqlite3.pyd', '_ssl.pyd', '_ctypes.pyd',
                 '_hashlib.pyd', '_decimal.pyd', '_multiprocessing.pyd')
$missing = @()
foreach ($name in $requiredPyd) {
    if (-not (Test-Path (Join-Path $BackendRuntime $name))) { $missing += $name }
}
Check 'runtime has its native extensions' ($missing.Count -eq 0) ($missing -join ', ')

$sitePackages = Join-Path $BackendRuntime 'Lib\site-packages'
$requiredPackages = @('fastapi', 'uvicorn', 'fitz', 'PIL', 'pydantic_core',
                      'cryptography', 'google', 'pypdf', 'openpyxl', 'reportlab',
                      'multipart')
$missingPackages = @()
foreach ($name in $requiredPackages) {
    if (-not (Test-Path (Join-Path $sitePackages $name))) { $missingPackages += $name }
}
Check 'runtime has the backend dependencies' ($missingPackages.Count -eq 0) ($missingPackages -join ', ')

# The interpreter has to import the stack from a sanitised environment: that is
# exactly what the recipient has.
# Single quotes only: the argument is passed through Start-Process quoting,
# and a nested double quote would silently mangle the code (an earlier version
# printed a NameError yet "matched" the marker in the traceback).
$probe = "import fastapi, uvicorn, fitz, PIL, pydantic_core, cryptography, google.genai, pypdf, openpyxl, reportlab, multipart, sqlite3, ssl; print('runtime-ok')"
$probeResult = Invoke-Tool $python @('-c', $probe)
Check 'bundled runtime imports the backend stack' `
    ($probeResult.Code -eq 0 -and $probeResult.Output -match 'runtime-ok') `
    ($probeResult.Output -replace "`r?`n", ' ')

# --- 2. tests ----------------------------------------------------------------
if ($SkipTests) {
    Step 'Tests skipped (-SkipTests)'
} else {
    Step 'Running the desktop test suite'
    $tests = Join-Path $desktopDir 'tests\MathChecker.Desktop.Tests\MathChecker.Desktop.Tests.csproj'
    $testRun = Invoke-Tool $dotnet @('test', $tests, '--nologo', '-v', 'q')
    Write-Host $testRun.Output
    Check 'desktop tests pass' ($testRun.Code -eq 0)
}

# --- 3. publish the client ---------------------------------------------------
Step 'Publishing the WPF client (self-contained, no .NET install needed)'
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
$clientDir = Join-Path $Stage 'desktop'
New-Item -ItemType Directory -Path $clientDir -Force | Out-Null

$publish = Invoke-Tool $dotnet @('publish', $project,
    '-c', $Configuration, '-r', $Runtime, '--self-contained', 'true',
    '-p:PublishSingleFile=false', '-p:EnableCompressionInSingleFile=false',
    '-p:DebugType=None', '-p:DebugSymbols=false',
    '-p:GenerateDocumentationFile=false', '-o', $clientDir)
Write-Host $publish.Output
if ($publish.Code -ne 0) { throw "dotnet publish failed with exit code $($publish.Code)" }
Check 'client executable published' (Test-Path (Join-Path $clientDir 'V5.exe'))

# --- 4. backend + assets -----------------------------------------------------
Step 'Copying the backend (tests and caches excluded)'
Copy-Item (Join-Path $repoRoot 'main.py') $Stage
Copy-Item (Join-Path $repoRoot 'LICENSE') $Stage -ErrorAction SilentlyContinue
Copy-Item (Join-Path $repoRoot 'backend') (Join-Path $Stage 'backend') -Recurse
foreach ($excluded in @('tests', 'requirements-dev.txt')) {
    $path = Join-Path $Stage "backend\$excluded"
    if (Test-Path $path) { Remove-Item $path -Recurse -Force }
}

Step 'Copying the bundled Python runtime (build tooling stripped)'
Copy-Item $BackendRuntime (Join-Path $Stage 'runtime') -Recurse
# pip/pytest are how the runtime was BUILT; nothing in the app imports them and
# they are large. Everything the backend does import is checked above.
$prune = @('pip', 'pytest', '_pytest', 'pluggy', 'iniconfig', 'pygments', 'distro')
foreach ($name in $prune) {
    $path = Join-Path $Stage "runtime\Lib\site-packages\$name"
    if (Test-Path $path) { Remove-Item $path -Recurse -Force }
    Get-ChildItem (Join-Path $Stage 'runtime\Lib\site-packages') -Directory -Filter "$name-*.dist-info" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
# The console entry points in Scripts\ are how the runtime was BUILT (pip.exe,
# pytest.exe, pygmentize.exe...). The backend is started as
# `python.exe main.py --serve`, so none of them is used - and they would be
# shortcuts into folders this build just removed.
if (Test-Path (Join-Path $Stage 'runtime\Scripts')) {
    Remove-Item (Join-Path $Stage 'runtime\Scripts') -Recurse -Force
}
# __pycache__ / *.pyc are regenerated on first use and would ship the builder's
# absolute paths. PDBs are debug information, not product.
Get-ChildItem $Stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Stage -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @('.pyc', '.pyo', '.pdb') } |
    Remove-Item -Force -ErrorAction SilentlyContinue

Step 'Copying the launcher, guide and version marker'
$launcher = Join-Path $repoRoot 'start_desktop.bat'
if (-not (Test-Path $launcher)) { throw "No launcher at $launcher" }
Copy-Item $launcher $Stage -Force
$guide = Join-Path $repoRoot 'docs\RELEASE_INSTALL.md'
if (-not (Test-Path $guide)) { throw "No first-run guide at $guide" }
Copy-Item $guide (Join-Path $Stage 'README.txt') -Force
Set-Content -Path (Join-Path $Stage 'VERSION') -Value $Version -Encoding ASCII

# --- 5. validation -----------------------------------------------------------
Step 'Validating the staged tree'
$required = @(
    'main.py',
    'backend\app\main.py',
    'runtime\python.exe',
    'desktop\V5.exe',
    'start_desktop.bat',
    'README.txt',
    'VERSION'
)
foreach ($relative in $required) {
    Check $relative (Test-Path (Join-Path $Stage $relative))
}

# Things that must never travel: private configuration, a database, the
# developer's checkout, student papers or build caches.
$forbidden = @('data', 'Data', '.git', '.env', 'config.toml', 'secrets.toml',
               'desktop\artifacts', 'runtime\Lib\site-packages\pip')
foreach ($relative in $forbidden) {
    Check "not shipped: $relative" (-not (Test-Path (Join-Path $Stage $relative)))
}
$stray = @(Get-ChildItem $Stage -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like '*.db' -or $_.Name -like '*.db-wal' -or $_.Name -like '*.db-shm' -or $_.Extension -eq '.pdf' })
Check 'no database or student PDF in the package' ($stray.Count -eq 0) (($stray | Select-Object -First 3 | ForEach-Object FullName) -join '; ')

# Absolute developer paths must not be baked into the product (PDBs and
# __pycache__ are the usual carriers; both are excluded above).
$needles = @($repoRoot, $env:USERPROFILE)
$offenders = New-Object System.Collections.ArrayList
foreach ($relative in @('backend\app\main.py', 'desktop\V5.dll', 'desktop\V5.exe')) {
    $file = Join-Path $Stage $relative
    if (-not (Test-Path $file)) { continue }
    $bytes = [System.IO.File]::ReadAllBytes($file)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    foreach ($needle in $needles) {
        if ($needle -and $text.Contains($needle)) { [void]$offenders.Add("$relative <- $needle") }
    }
}
Check 'no absolute developer path baked into binaries' ($offenders.Count -eq 0) (($offenders) -join '; ')

# --- 6. backend smoke test from the staged tree ------------------------------
Step 'Starting the bundled backend from the staged tree'
$smokeData = Join-Path ([System.IO.Path]::GetTempPath()) ("mc5_release_smoke_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $smokeData -Force | Out-Null
$smokePort = Get-FreePort
$smokeEnv = @{
    MC5_DATA_DIR = $smokeData
    MC5_LOGIN_REQUIRED = '0'
    PATH = "$env:SystemRoot\system32;$env:SystemRoot"
}
$startArgs = @{
    FilePath = (Join-Path $Stage 'runtime\python.exe')
    ArgumentList = @('main.py', '--serve', $smokePort.ToString())
    WorkingDirectory = $Stage
    PassThru = $true
    WindowStyle = 'Hidden'
    RedirectStandardOutput = (Join-Path $smokeData 'stdout.log')
    RedirectStandardError = (Join-Path $smokeData 'stderr.log')
}
if ($PSVersionTable.PSVersion.Major -ge 7) {
    # Prove the backend needs nothing from this machine's PATH: start it with a
    # system-only environment, exactly like the recipient's PC.
    $startArgs.Environment = $smokeEnv
} else {
    # Windows PowerShell 5.1 has no -Environment. The bundled interpreter and
    # data folder are still exercised; PATH is inherited for this check only.
    $env:MC5_DATA_DIR = $smokeData
    $env:MC5_LOGIN_REQUIRED = '0'
}
$smoke = Start-Process @startArgs
$ready = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if ($smoke.HasExited) { break }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$smokePort/api/health" -TimeoutSec 3 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 700 }
}
Check 'bundled backend answers /api/health' $ready
$createdDb = Test-Path (Join-Path $smokeData 'app.db')
Check 'backend initialised its database' $createdDb $smokeData
if (-not $smoke.HasExited) { $smoke.Kill(); $smoke.WaitForExit(10000) | Out-Null }
if (-not $ready) {
    Write-Host '  --- backend stderr ---' -ForegroundColor Yellow
    Get-Content (Join-Path $smokeData 'stderr.log') -ErrorAction SilentlyContinue |
        Select-Object -Last 20 | ForEach-Object { Write-Host "  $_" }
}
Remove-Item $smokeData -Recurse -Force -ErrorAction SilentlyContinue

# The smoke test (and the import probe) regenerate bytecode caches inside the
# staged tree. Strip them LAST, so the package carries no __pycache__ and no
# builder absolute paths that a .pyc embeds.
Step 'Removing bytecode caches regenerated by the checks'
Get-ChildItem $Stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Stage -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @('.pyc', '.pyo') } |
    Remove-Item -Force -ErrorAction SilentlyContinue
$caches = @(Get-ChildItem $Stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
Check 'no bytecode caches in the package' ($caches.Count -eq 0) (($caches | Select-Object -First 3 | ForEach-Object FullName) -join '; ')

# --- 7. portable ZIP ---------------------------------------------------------
Step 'Writing the portable ZIP'
New-Item -ItemType Directory -Path $Output -Force | Out-Null
# Only this build's artefacts: a Setup.exe from an earlier version must never
# sit beside a new one and be handed out as if it matched.
$stalePatterns = @('MathChecker-*.zip', 'MathChecker-*.exe', 'MATH_CHECKER_V5_Setup.exe', 'MATH_CHECKER_V5_Portable.zip')
Get-ChildItem $Output -File -ErrorAction SilentlyContinue |
    Where-Object { $name = $_.Name; $stale = $false; foreach ($pattern in $stalePatterns) { if ($name -like $pattern) { $stale = $true } }; $stale } |
    Remove-Item -Force -ErrorAction SilentlyContinue
$zip = Join-Path $Output "MathChecker-Portable-x64-$Version.zip"
Compress-Archive -Path (Join-Path $Stage '*') -DestinationPath $zip -CompressionLevel Optimal
Write-Host ("  {0:N1} MB  {1}" -f ((Get-Item $zip).Length / 1MB), $zip)

# --- 8. installer ------------------------------------------------------------
$installer = $null
if ($SkipInstaller) {
    Step 'Installer skipped (-SkipInstaller)'
} elseif (-not $iscc) {
    Step 'Inno Setup 6 was not found: the installer was NOT built'
    Write-Host '  Install it from https://jrsoftware.org/isdl.php, then run this script again.' -ForegroundColor Yellow
} else {
    Step "Building the installer with $iscc"
    $iss = Join-Path $desktopDir 'installer\MathCheckerDesktop.iss'
    $compile = Invoke-Tool $iscc @("/DSourceDir=$Stage", "/DOutputDir=$Output",
        "/DAppVersion=$Version", $iss)
    Write-Host $compile.Output
    if ($compile.Code -ne 0) { throw "ISCC failed with exit code $($compile.Code)" }
    $installer = Join-Path $Output "MathChecker-Setup-x64-$Version.exe"
    Check 'installer produced' (Test-Path $installer)
    # A zero-byte or suspiciously small Setup.exe is a packaging failure, not a
    # build to ship: the payload alone is hundreds of MB before compression.
    $size = (Get-Item $installer).Length
    Check 'installer payload is plausible' ($size -gt 40MB) ("{0:N1} MB" -f ($size / 1MB))
}

# --- 9. manifest + checksums -------------------------------------------------
Step 'Writing the release manifest and checksums'
$stageBytes = (Get-ChildItem $Stage -Recurse -File | Measure-Object Length -Sum).Sum
$manifest = [ordered]@{
    product = 'Math Checker'
    productId = 'MATH_CHECKER_V5'
    version = $Version
    architecture = $Runtime
    builtAt = (Get-Date).ToUniversalTime().ToString('o')
    builtOn = [System.Environment]::MachineName
    selfContained = $true
    bundledRuntime = (Split-Path -Leaf $BackendRuntime)
    executable = 'desktop\V5.exe'
    installer = if ($installer) { Split-Path -Leaf $installer } else { $null }
    portableZip = Split-Path -Leaf $zip
    stagedFolder = 'desktop\artifacts\MATH_CHECKER_V5'
    stagedMb = [math]::Round($stageBytes / 1MB, 1)
    installerMb = if ($installer) { [math]::Round((Get-Item $installer).Length / 1MB, 1) } else { $null }
    portableZipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    userDataFolder = '%LOCALAPPDATA%\MathChecker'
    desktopSettingsFolder = '%APPDATA%\MathCheckerDesktop'
    installerKeepsUserData = $true
    externalRequirements = @(
        'Internet access + a Gemini API key (or an AI provider key) for corrections',
        'Google Classroom OAuth client (optional, only for classroom sync)'
    )
    checks = $script:checks
}
# The manifest is a build record, not a map of this developer's disk: report
# what was checked, with the checkout and profile redacted.
foreach ($entry in $script:checks) {
    if ($entry.detail) {
        $entry.detail = $entry.detail.Replace($repoRoot, '<repo>').Replace($env:USERPROFILE, '%USERPROFILE%')
    }
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Output 'release_manifest.json') -Encoding UTF8

$hashLines = @()
foreach ($file in @($zip, $installer)) {
    if ($file -and (Test-Path $file)) {
        $hash = (Get-FileHash $file -Algorithm SHA256).Hash.ToLowerInvariant()
        $hashLines += ("{0}  {1}" -f $hash, (Split-Path -Leaf $file))
    }
}
$hashLines | Set-Content (Join-Path $Output 'SHA256SUMS.txt') -Encoding ASCII

Step 'Done'
$summary = $script:checks | Group-Object result | ForEach-Object { "$($_.Name): $($_.Count)" }
Write-Host ("  checks  " + ($summary -join '  '))
Write-Host ("  version $Version   staged {0:N1} MB" -f ($stageBytes / 1MB))
Get-ChildItem $Output | Select-Object Name, @{ n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } } |
    Format-Table -AutoSize
if ($installer) { Write-Host "`nInstaller: $installer" -ForegroundColor Green }
