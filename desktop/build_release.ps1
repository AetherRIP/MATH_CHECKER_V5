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
#
# The client walks up from its own folder looking for main.py and uses
# <root>\runtime\python.exe, so no machine-wide Python install is needed.

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

Step 'Publishing the WPF client (self-contained, no .NET install needed)'
$stage = Join-Path $Output 'MATH_CHECKER_V5'
$clientDir = Join-Path $stage 'desktop'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $clientDir -Force | Out-Null

& dotnet publish (Join-Path $desktopDir 'src\MathChecker.Desktop\MathChecker.Desktop.csproj') `
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

Step 'Checking the release layout'
$required = @(
    'main.py',
    'backend\app\main.py',
    'runtime\python.exe',
    'desktop\MATH_CHECKER_V5.exe'
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
    Write-Host '  Install Inno Setup 6 (https://jrsoftware.org/isdl.php), then run:'
    Write-Host ("  iscc /DSourceDir=`"{0}`" /DOutputDir=`"{1}`" `"{2}`"" -f `
        $stage, $Output, (Join-Path $desktopDir 'installer\MathCheckerDesktop.iss'))
}

Step 'Done'
$manifest = [ordered]@{
    product = 'MATH_CHECKER_V5'
    version = '1.0.0'
    executable = 'desktop\MATH_CHECKER_V5.exe'
    portableFolder = 'MATH_CHECKER_V5'
    runtime = $Runtime
    selfContained = $true
    builtAt = (Get-Date).ToUniversalTime().ToString('o')
    portableZip = (Split-Path -Leaf $zip)
    installerBuilt = [bool]($iscc -and -not $SkipInstaller)
}
$manifest | ConvertTo-Json | Set-Content (Join-Path $Output 'release_manifest.json')
Get-ChildItem $Output | Select-Object Name, Length | Format-Table -AutoSize
