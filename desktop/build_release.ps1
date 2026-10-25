# Builds a self-contained Windows release of the Math Checker desktop client.
#
#   powershell -ExecutionPolicy Bypass -File desktop\build_release.ps1
#
# Output (desktop\artifacts):
#   MATH_CHECKER_V5_Portable.zip   client + backend + Python runtime, no install
#   MATH_CHECKER_V5\                the same layout, unzipped (used by the installer)
#   MATH_CHECKER_V5_Setup.exe      only when Inno Setup 6 (ISCC.exe) is installed
#
# The bundle is arranged so the client finds the existing backend without any
# developer environment:
#
#   <root>\main.py            existing V5 backend entry point
#   <root>\backend\           existing backend package (unchanged)
#   <root>\runtime\python.exe bundled Python with the backend's dependencies
#   <root>\desktop\           the published WPF client
#   <root>\start_desktop.bat  one file for the recipient to double-click
#
# The client walks up from its own folder looking for main.py and uses
# <root>\runtime\python.exe, so no machine-wide Python install is needed. The
# launcher at the root is the repository's own start_desktop.bat: it detects
# this layout (main.py + desktop\V5.exe + runtime\python.exe beside it) and
# starts the client, never a build, so the recipient needs no .NET or Python.

[CmdletBinding()]
param(
    [string]$Configuration = 'Release',
    [string]$Runtime = 'win-x64',
    [string]$BackendRuntime = '',
    [string]$Output = '',
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$desktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $desktopDir
if ([string]::IsNullOrWhiteSpace($Output)) { $Output = Join-Path $desktopDir 'artifacts' }
if ([string]::IsNullOrWhiteSpace($BackendRuntime)) {
    $BackendRuntime = Join-Path $repoRoot 'MathChecker_Distribution\build\runtime'
}

function Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }

# A "dotnet" on PATH is not proof of an SDK: a runtime-only install answers to
# the same name and then fails "publish" with "No .NET SDKs were found", which
# used to leave an old release in artifacts. Probe each host for an SDK and use
# the first one that has it.
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

$dotnet = Resolve-Dotnet

Step 'Publishing the WPF client (self-contained, no .NET install needed)'
$stage = Join-Path $Output 'MATH_CHECKER_V5'
$clientDir = Join-Path $stage 'desktop'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $clientDir -Force | Out-Null

& $dotnet publish (Join-Path $desktopDir 'src\MathChecker.Desktop\MathChecker.Desktop.csproj') `
    -c $Configuration -r $Runtime --self-contained true `
    -p:PublishSingleFile=false -p:EnableCompressionInSingleFile=false `
    -o $clientDir
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed with exit code $LASTEXITCODE" }

Step 'Copying the existing backend (unmodified)'
Copy-Item (Join-Path $repoRoot 'main.py') $stage
foreach ($item in @('backend', 'requirements.txt')) {
    $source = Join-Path $repoRoot $item
    if (Test-Path $source) { Copy-Item $source (Join-Path $stage $item) -Recurse }
}
# Generated caches are never part of a release.
Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Step 'Copying the bundled Python runtime'
if (-not (Test-Path (Join-Path $BackendRuntime 'python.exe'))) {
    throw "No bundled Python runtime at $BackendRuntime. Build it with MathChecker_Distribution\build_release.bat, or pass -BackendRuntime <folder containing python.exe>."
}
Copy-Item $BackendRuntime (Join-Path $stage 'runtime') -Recurse
# The bundled runtime carries caches from the machine that built it; they are
# regenerable and must not travel with the product.
Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Step 'Copying the one-click launcher'
# The same file the checkout uses. Inside a release it takes the packaged
# branch (it starts the client beside it) instead of building from source, so
# one script serves both and there is nothing separate to keep in sync.
$launcher = Join-Path $repoRoot 'start_desktop.bat'
if (-not (Test-Path $launcher)) { throw "No launcher at $launcher" }
Copy-Item $launcher $stage -Force

Step 'Checking the release layout'
$required = @(
    'main.py',
    'backend\app\main.py',
    'runtime\python.exe',
    'desktop\V5.exe',
    'start_desktop.bat'
)
foreach ($relative in $required) {
    $path = Join-Path $stage $relative
    if (-not (Test-Path $path)) { throw "Release is incomplete: missing $relative" }
    Write-Host "  ok  $relative"
}

Step 'Writing the portable ZIP'
$zip = Join-Path $Output 'MATH_CHECKER_V5_Portable.zip'
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal
Write-Host ("  {0:N1} MB  {1}" -f ((Get-Item $zip).Length / 1MB), $zip)

$iscc = $null
foreach ($candidate in @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'))) {
    if (Test-Path $candidate) { $iscc = $candidate; break }
}
if (-not $iscc) { $found = Get-Command iscc -ErrorAction SilentlyContinue; if ($found) { $iscc = $found.Source } }

if ($SkipInstaller) {
    Step 'Installer skipped (-SkipInstaller)'
} elseif ($iscc) {
    Step "Building the installer with $iscc"
    $iss = Join-Path $desktopDir 'installer\MathCheckerDesktop.iss'
    & $iscc "/DSourceDir=$stage" "/DOutputDir=$Output" $iss
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }
    Write-Host ("  {0}" -f (Join-Path $Output 'MATH_CHECKER_V5_Setup.exe'))
} else {
    Step 'Inno Setup 6 was not found: the installer was not built'
    # A Setup.exe left by an earlier build installs THAT client, i.e. an older
    # interface than the stage beside it. Say so loudly instead of letting a
    # stale installer be handed out as if it matched this build.
    $staleSetup = Join-Path $Output 'MATH_CHECKER_V5_Setup.exe'
    if (Test-Path $staleSetup) {
        $stamp = (Get-Item $staleSetup).LastWriteTime
        Write-Host ("  WARNING: {0} was built {1:yyyy-MM-dd HH:mm} and still contains the older client." -f $staleSetup, $stamp) -ForegroundColor Yellow
        Write-Host '           Do not ship it until it is rebuilt with Inno Setup 6.' -ForegroundColor Yellow
    }
    Write-Host '  Install Inno Setup 6 (https://jrsoftware.org/isdl.php), then run:'
    Write-Host ("  iscc /DSourceDir=`"{0}`" /DOutputDir=`"{1}`" `"{2}`"" -f `
        $stage, $Output, (Join-Path $desktopDir 'installer\MathCheckerDesktop.iss'))
}

Step 'Done'
$manifest = [ordered]@{
    product = 'MATH_CHECKER_V5'
    version = '1.0.0'
    executable = 'desktop\V5.exe'
    portableFolder = 'MATH_CHECKER_V5'
    runtime = $Runtime
    selfContained = $true
    builtAt = (Get-Date).ToUniversalTime().ToString('o')
    portableZip = (Split-Path -Leaf $zip)
    installerBuilt = [bool]($iscc -and -not $SkipInstaller)
}
$manifest | ConvertTo-Json | Set-Content (Join-Path $Output 'release_manifest.json')
Get-ChildItem $Output | Select-Object Name, Length | Format-Table -AutoSize
