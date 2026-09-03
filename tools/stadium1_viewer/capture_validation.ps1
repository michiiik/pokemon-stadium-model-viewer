[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8767',
    [string]$OutputDir = (Join-Path ([System.IO.Path]::GetTempPath()) 'stadium1-viewer-validation-auto'),
    [ValidateSet('Regression', 'Representative', 'All')]
    [string]$Set = 'Regression',
    [string[]]$Model,
    [switch]$StaticOnly,
    [switch]$StartServer,
    [string]$Assets,
    [string]$Config,
    [int]$Port = 8767,
    [int]$Width = 1280,
    [int]$Height = 720
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)

function Wait-ForHealth {
    param([string]$Url)
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 3
            if ($health.ok -and $health.modelCount -gt 0) {
                return $health
            }
        } catch {
            # The server may still be starting. Retry below.
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Viewer health check failed: $Url/api/health"
}

function Find-Chrome {
    $found = @()
    $command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($command) { $found += $command.Source }
    $found += @(
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $path = $found | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $path) {
        throw 'Google Chrome was not found. Install Chrome or pass a browser command manually.'
    }
    return $path
}

function Wait-ForScreenshot {
    param([string]$Path)
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if (Test-Path -LiteralPath $Path) {
            $item = Get-Item -LiteralPath $Path
            if ($item.Length -gt 0) { return $item }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Chrome did not produce a screenshot: $Path"
}

if ($StartServer) {
    $serverScript = Join-Path $repoRoot 'tools\stadium1_viewer.py'
    if (-not (Test-Path -LiteralPath $serverScript)) {
        throw "Viewer server script not found: $serverScript"
    }
    if ($Config) {
        $serverArgs = @('-3', $serverScript, '--config', $Config, '--port', "$Port")
    } elseif ($Assets) {
        $serverArgs = @('-3', $serverScript, '--assets', $Assets, '--port', "$Port")
    } else {
        throw 'StartServer requires -Config or -Assets'
    }
    Start-Process -WindowStyle Hidden -FilePath 'py' -ArgumentList $serverArgs | Out-Null
}

$health = Wait-ForHealth -Url $BaseUrl.TrimEnd('/')
$catalog = Invoke-RestMethod -Uri "$($BaseUrl.TrimEnd('/'))/api/catalog" -TimeoutSec 10
$catalogModels = @($catalog.models)

$defaultModels = switch ($Set) {
    'Regression' {
        @('0.bin', '1.bin', '2.bin', '14.bin', '15.bin', '16.bin', '17.bin', '20.bin', '23.bin', '25.bin', '84.bin', '87.bin', '88.bin', '93.bin', '102.bin', '125.bin', '145.bin', '150.bin')
    }
    'Representative' {
        @('0.bin', '1.bin', '2.bin', '14.bin', '23.bin', '25.bin', '108.bin', '119.bin', '131.bin', '145.bin', '150.bin')
    }
    'All' {
        @($catalogModels | ForEach-Object { $_.path })
    }
}
$requestedModels = if ($Model -and $Model.Count -gt 0) { @($Model) } else { @($defaultModels) }
$available = @{}
foreach ($item in $catalogModels) { $available[[string]$item.path] = $item }
$missing = @($requestedModels | Where-Object { -not $available.ContainsKey($_) })
if ($missing.Count -gt 0) {
    throw "Models are not present in the live catalog: $($missing -join ', ')"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$chrome = Find-Chrome
$base = $BaseUrl.TrimEnd('/')
$cases = New-Object System.Collections.Generic.List[object]

foreach ($modelPath in $requestedModels) {
    $resource = $available[$modelPath]
    $cases.Add([pscustomobject]@{ Name = ($modelPath -replace '\.bin$', '') + '-static'; Model = $modelPath; Animation = -1; Frame = 0 })
    if (-not $StaticOnly) {
        $supported = @($resource.animations | Where-Object { $_.supported -eq $true -and $_.frameCount -gt 0 })
        if ($supported.Count -gt 0) {
            $animation = $supported[0]
            $mid = [Math]::Max(0, [Math]::Floor(([int]$animation.frameCount - 1) / 2))
            $cases.Add([pscustomobject]@{ Name = ($modelPath -replace '\.bin$', '') + "-anim$($animation.id)-frame0"; Model = $modelPath; Animation = [int]$animation.id; Frame = 0 })
            if ($mid -gt 0) {
                $cases.Add([pscustomobject]@{ Name = ($modelPath -replace '\.bin$', '') + "-anim$($animation.id)-frame$mid"; Model = $modelPath; Animation = [int]$animation.id; Frame = $mid })
            }
        }
    }
}

$results = New-Object System.Collections.Generic.List[object]
foreach ($case in $cases) {
    $output = Join-Path $OutputDir "$($case.Name).png"
    $query = @(
        "model=$([uri]::EscapeDataString([string]$case.Model))",
        "animation=$($case.Animation)",
        "frame=$($case.Frame)",
        'axes=0', 'bounds=0', 'skeleton=0', 'bonenames=0',
        'textures=1', 'lighting=1', 'wireframe=0',
        'yaw=0', 'pitch=0.05', 'distance=2.4'
    ) -join '&'
    $url = "$base/?$query"
    $profile = Join-Path ([System.IO.Path]::GetTempPath()) ("stadium1-viewer-capture-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $profile | Out-Null
    $chromeArgs = @(
        '--headless=new', '--disable-gpu', '--enable-unsafe-swiftshader', '--hide-scrollbars',
        '--run-all-compositor-stages-before-draw', '--virtual-time-budget=5000',
        "--window-size=$Width,$Height", "--user-data-dir=$profile", "--screenshot=$output", $url
    )
    Write-Host "Capturing $($case.Name) ..." -ForegroundColor Cyan
    & $chrome @chromeArgs 2>$null | Out-Null
    $file = Wait-ForScreenshot -Path $output
    $results.Add([pscustomobject]@{
        name = $case.Name; model = $case.Model; animation = $case.Animation; frame = $case.Frame
        path = $file.FullName; bytes = $file.Length; url = $url
    })
}

$reportPath = Join-Path $OutputDir 'capture-report.json'
$report = [pscustomobject]@{
    generatedAt = (Get-Date).ToString('o')
    health = $health
    set = $Set
    count = $results.Count
    captures = @($results)
}
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportPath -Encoding UTF8

Write-Host "Completed $($results.Count) captures." -ForegroundColor Green
Write-Host "Output: $OutputDir"
Write-Host "Report: $reportPath"
