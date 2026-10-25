# Verifies a staged release the way a user runs it: copy the folder somewhere
# outside the repository, double-click the exe, confirm the window and the
# bundled backend, upload one correction, then close it and confirm nothing is
# left running.
#
#   powershell -ExecutionPolicy Bypass -File desktop\test_release.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\test_release.ps1 -KeepTemp
#
# The client settings file is backed up and restored; the test always uses its
# own data folder and Demo Mode, so no real student record is touched.

[CmdletBinding()]
param(
    [string]$Stage = '',
    [int]$Port = 8661,
    [int]$TimeoutSeconds = 180,
    [switch]$KeepTemp
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($Stage)) {
    # $PSScriptRoot is empty while parameter defaults are evaluated under -File.
    $Stage = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'artifacts\MATH_CHECKER_V5'
}
$script:checks = New-Object System.Collections.ArrayList

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    [void]$script:checks.Add([pscustomobject]@{ Check = $name; Result = $status; Detail = $detail })
    Write-Host ("  [{0}] {1}{2}" -f $status, $name, $(if ($detail) { " - $detail" } else { '' })) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class ReleaseTestWindow {
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int hh, uint flags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@
# Capture screenshots in physical pixels like the user's display.
[ReleaseTestWindow]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null

function Save-WindowShot([IntPtr]$handle, [string]$path) {
    [ReleaseTestWindow]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 0, 0, 0x0043) | Out-Null
    Start-Sleep -Milliseconds 600
    $rect = New-Object ReleaseTestWindow+RECT
    [ReleaseTestWindow]::GetWindowRect($handle, [ref]$rect) | Out-Null
    $width = $rect.R - $rect.L
    $height = $rect.B - $rect.T
    $inset = 8
    $bitmap = New-Object System.Drawing.Bitmap ($width - 2 * $inset), ($height - $inset)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen(($rect.L + $inset), $rect.T, 0, 0, $bitmap.Size)
    $graphics.Dispose()
    $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    [ReleaseTestWindow]::SetWindowPos($handle, [IntPtr](-2), 0, 0, 0, 0, 0x0043) | Out-Null
    return $path
}

$exeSource = Join-Path $Stage 'desktop\V5.exe'
if (-not (Test-Path $exeSource)) {
    throw "No staged release at $Stage. Run desktop\build_release.ps1 first."
}

# LOCALAPPDATA is the long user path; %TEMP% often holds the 8.3 form and the
# child process reports the long one, which made path comparisons fail.
$tempRoot = Join-Path $env:LOCALAPPDATA ('Temp\mc5-release-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
Write-Host "`n== Copying the release outside the repository" -ForegroundColor Cyan
Write-Host "  $Stage -> $tempRoot"
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
& robocopy $Stage $tempRoot /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
$exe = Join-Path $tempRoot 'desktop\V5.exe'
$dataDir = Join-Path $tempRoot 'data'

$settingsPath = Join-Path $env:APPDATA 'MathCheckerDesktop\settings.json'
$backupPath = $settingsPath + '.release-test-backup'
$hadSettings = Test-Path $settingsPath
if ($hadSettings) { Copy-Item $settingsPath $backupPath -Force }

$settings = [ordered]@{
    backendDirectory        = ''
    pythonExecutable        = ''
    dataDirectory           = $dataDir
    port                    = $Port
    startBackendAutomatically = $true
    attachToRunningBackend  = $true
    startupTimeoutSeconds   = 120
    environmentVariables    = @{ MC5_DEMO = '1' }
    lastPage                = 'dashboard'
    windowWidth             = 1152
    windowHeight            = 700
    windowMaximized         = $false
    railCollapsed           = $false
}
New-Item -ItemType Directory -Path (Split-Path -Parent $settingsPath) -Force | Out-Null
$settings | ConvertTo-Json -Depth 6 | Set-Content -Path $settingsPath -Encoding UTF8

$proc = $null
$closed = $false
try {
    Write-Host "`n== Launching $exe" -ForegroundColor Cyan
    $proc = Start-Process -FilePath $exe -PassThru
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($proc.MainWindowHandle -eq [IntPtr]::Zero -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $proc.Refresh()
        if ($proc.HasExited) { break }
    }
    Check 'client process starts' (-not $proc.HasExited) ("pid {0}" -f $proc.Id)
    Check 'application window opens' ($proc.MainWindowHandle -ne [IntPtr]::Zero) ("title '{0}'" -f $proc.MainWindowTitle)

    Write-Host "`n== Waiting for the bundled backend" -ForegroundColor Cyan
    $health = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 5 -UseBasicParsing
            break
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }
    Check '/api/health answers' ($null -ne $health) ("status={0}" -f $health.status)

    # The client starts the interpreter itself, so the backend is a direct child
    # of the WPF process. Its executable must be the copied runtime, not a
    # machine-wide Python.
    $python = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.ParentProcessId -eq $proc.Id } | Select-Object -First 1
    $expectedPython = Join-Path $tempRoot 'runtime\python.exe'
    $pythonPath = if ($python) { $python.ExecutablePath } else { '' }
    Check 'backend runs on the bundled interpreter' (
        $null -ne $python -and $pythonPath -and
        ($pythonPath -eq $expectedPython -or $pythonPath.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase))
    ) $(if ($python) { $pythonPath } else { 'no child python process' })
    Check 'backend starts the packaged main.py' (
        $null -ne $python -and $python.CommandLine -like '*main.py*--serve*'
    ) $(if ($python) { $python.CommandLine } else { '' })

    $dashboard = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/dashboard" -TimeoutSec 15 -UseBasicParsing
    Check 'dashboard API returns data' ($null -ne $dashboard) ''
    Check 'demo mode is on' ($dashboard.demo_mode -eq $true -or $dashboard.mock_mode -eq $true) (
        "demo_mode={0} mock_mode={1}" -f $dashboard.demo_mode, $dashboard.mock_mode)

    $shotDir = Join-Path $PSScriptRoot '../dev_local/theme'
    if (Test-Path $shotDir) {
        Start-Sleep -Seconds 2
        $shot = Save-WindowShot $proc.MainWindowHandle (Join-Path $shotDir 'release_window.png')
        Check 'packaged window renders' ((Test-Path $shot) -and (Get-Item $shot).Length -gt 20000) $shot
    }

    Write-Host "`n== A second launch must not open a second client" -ForegroundColor Cyan
    $second = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds 6
    $instances = @(Get-Process -Name 'V5' -ErrorAction SilentlyContinue)
    Check 'single instance is enforced' ($instances.Count -eq 1) ("instances={0}" -f $instances.Count)
    if (-not $second.HasExited) { $second.Kill() }

    Write-Host "`n== Uploading one correction through the packaged backend" -ForegroundColor Cyan
    $runtimePython = Join-Path $tempRoot 'runtime\python.exe'
    $pdfScript = Join-Path $tempRoot 'make_test_pdfs.py'
    @'
import sys
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

def make(path, lines):
    sheet = canvas.Canvas(path, pagesize=A4)
    y = 800
    for line in lines:
        sheet.drawString(72, y, line)
        y -= 22
    sheet.save()

make(sys.argv[1], ["Student answer sheet", "Q1  2 + 2 = 4", "Q2  3 x 4 = 12"])
make(sys.argv[2], ["Mark scheme", "Q1  2 marks", "Q2  2 marks"])
'@ | Set-Content -Path $pdfScript -Encoding UTF8
    $studentPdf = Join-Path $tempRoot 'student.pdf'
    $schemePdf = Join-Path $tempRoot 'scheme.pdf'
    & $runtimePython $pdfScript $studentPdf $schemePdf
    Check 'test PDFs generated with the packaged Python' ((Test-Path $studentPdf) -and (Test-Path $schemePdf)) ''

    $uploadJson = & curl.exe -s --max-time 120 -X POST "http://127.0.0.1:$Port/api/corrections/unified" `
        -F "submission=@$studentPdf;type=application/pdf" `
        -F "mark_scheme=@$schemePdf;type=application/pdf" `
        -F "student_name=Packaged Test" `
        -F "pipeline=standard"
    $upload = $null
    try { $upload = $uploadJson | ConvertFrom-Json } catch { }
    Check 'correction upload accepted' ($null -ne $upload -and $null -ne $upload.job) $(if ($upload -and $null -ne $upload.job) { "pipeline={0}" -f $upload.applied_pipeline } else { "response: " + $uploadJson })

    $jobId = $null
    $resultId = $null
    if ($upload -and $upload.job) {
        $jobId = $upload.job.id
        $jobDeadline = (Get-Date).AddSeconds(150)
        while ((Get-Date) -lt $jobDeadline) {
            $job = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/jobs/$jobId" -TimeoutSec 15 -UseBasicParsing
            if ($job.status -in @('COMPLETED', 'FAILED', 'CANCELLED')) { break }
            Start-Sleep -Seconds 2
        }
        Check 'correction job completes' ($job.status -eq 'COMPLETED') ("status={0} stage={1}" -f $job.status, $job.stage_detail)
        $resultId = $job.result_id
    }
    Check 'job produced a stored result' (-not [string]::IsNullOrWhiteSpace($resultId)) ("resultId={0}" -f $resultId)

    if ($resultId) {
        $detail = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/results/$resultId" -TimeoutSec 20 -UseBasicParsing
        $questionCount = @($detail.result_json.questions).Count
        Check 'result detail carries marks' (
            $null -ne $detail.earned -and $null -ne $detail.available -and $questionCount -gt 0
        ) ("{0}/{1}, {2} questions" -f $detail.earned, $detail.available, $questionCount)
        $exportPath = Join-Path $tempRoot 'report.pdf'
        & curl.exe -s --max-time 120 -o $exportPath "http://127.0.0.1:$Port/api/results/$resultId/export?format=pdf&detail=standard"
        $exportSize = if (Test-Path $exportPath) { (Get-Item $exportPath).Length } else { 0 }
        $header = ''
        if ($exportSize -gt 0) {
            $bytes = [System.IO.File]::ReadAllBytes($exportPath)[0..3]
            $header = [System.Text.Encoding]::ASCII.GetString($bytes)
        }
        Check 'PDF export works' ($exportSize -gt 2000 -and $header -eq '%PDF') ("{0} bytes, header {1}" -f $exportSize, $header)
    }

    Check 'backend keeps data in the test folder' (Test-Path $dataDir) $dataDir

    Write-Host "`n== Closing the window" -ForegroundColor Cyan
    [ReleaseTestWindow]::PostMessage($proc.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
    $closeDeadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $closeDeadline -and -not $proc.HasExited) {
        Start-Sleep -Milliseconds 500
        $proc.Refresh()
    }
    Check 'client exits on close' ($proc.HasExited) ''

    # The client stops the interpreter and then waits for it to exit, so give
    # the child a few seconds before calling it a leak.
    $leftover = $null
    $portOpen = $false
    $shutdownDeadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $shutdownDeadline) {
        $leftover = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -like "*$tempRoot*" }
        $portOpen = $false
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 -UseBasicParsing | Out-Null
            $portOpen = $true
        } catch { $portOpen = $false }
        if ($null -eq $leftover -and -not $portOpen) { break }
        Start-Sleep -Milliseconds 700
    }
    Check 'no backend process is left behind' ($null -eq $leftover) ''
    Check 'backend port is closed after exit' (-not $portOpen) ("127.0.0.1:{0}" -f $Port)
} finally {
    if ($proc -and -not $proc.HasExited) { $proc.Kill() }
    Get-Process -Name 'V5' -ErrorAction SilentlyContinue | Stop-Process -Force
    if ($hadSettings) {
        Move-Item $backupPath $settingsPath -Force
        Write-Host '  settings restored' -ForegroundColor DarkGray
    } else {
        Remove-Item $settingsPath -ErrorAction SilentlyContinue
        Write-Host '  test settings removed' -ForegroundColor DarkGray
    }
    if (-not $KeepTemp) {
        Remove-Item $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "  kept: $tempRoot" -ForegroundColor DarkGray
    }
}

Write-Host "`n== Summary" -ForegroundColor Cyan
$script:checks | Format-Table -AutoSize
$failed = @($script:checks | Where-Object { $_.Result -eq 'FAIL' })
Write-Host ("{0}/{1} checks passed" -f ($script:checks.Count - $failed.Count), $script:checks.Count) -ForegroundColor $(if ($failed.Count) { 'Red' } else { 'Green' })
if ($failed.Count) { exit 1 }
