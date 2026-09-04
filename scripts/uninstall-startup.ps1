$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDirectory = Join-Path $ProjectRoot 'data\runtime'
$CommandFile = Join-Path $RuntimeDirectory 'supervisor-command.json'
$TaskName = 'Neko AI Same-User Supervisor'

if (Test-Path -LiteralPath (Join-Path $RuntimeDirectory 'supervisor-status.json')) {
    $Command = @{ id = [guid]::NewGuid().ToString(); action = 'shutdown-supervisor'; requested_at = [DateTimeOffset]::UtcNow.ToString('o') }
    $Temporary = "$CommandFile.uninstall.tmp"
    $Command | ConvertTo-Json | Set-Content -LiteralPath $Temporary -Encoding utf8
    Move-Item -LiteralPath $Temporary -Destination $CommandFile -Force
    Start-Sleep -Seconds 3
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host 'Neko AI 当前用户常驻任务已移除。' -ForegroundColor Yellow
