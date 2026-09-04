param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('start', 'stop', 'restart')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Start-Sleep -Seconds 2
if ($Action -eq 'start') {
    $PidFile = Join-Path $ProjectRoot 'data\neko-processes.json'
    if (-not (Test-Path -LiteralPath $PidFile)) { exit 0 }
    $Recorded = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
    $Backend = Get-Process -Id ([int]$Recorded.backend) -ErrorAction SilentlyContinue
    $Frontend = Get-Process -Id ([int]$Recorded.frontend) -ErrorAction SilentlyContinue
    if (-not $Backend -or $Frontend) { exit 0 }
    $Node = Get-Command node.exe -ErrorAction Stop
    $VinextCli = Join-Path $ProjectRoot 'frontend\node_modules\vinext\dist\cli.js'
    $NewFrontend = Start-Process -FilePath $Node.Source -ArgumentList $VinextCli,'start','--hostname','127.0.0.1','--port','3000' -WorkingDirectory (Join-Path $ProjectRoot 'frontend') -WindowStyle Hidden -PassThru
    @{ backend = $Backend.Id; frontend = $NewFrontend.Id } | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding utf8
    exit 0
}
& (Join-Path $PSScriptRoot 'stop.ps1')
if ($Action -eq 'restart') {
    Start-Sleep -Seconds 1
    & (Join-Path $PSScriptRoot 'start.ps1') -NoBrowser
}
