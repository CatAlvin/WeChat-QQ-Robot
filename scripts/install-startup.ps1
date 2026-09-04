# Keep this file encoded as UTF-8 with BOM for Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SupervisorScript = Join-Path $PSScriptRoot 'neko-supervisor.ps1'
$TaskName = 'Neko AI Same-User Supervisor'
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source

if (-not (Test-Path -LiteralPath $SupervisorScript)) { throw 'Supervisor script is missing.' }
$ExistingStatus = Join-Path $ProjectRoot 'data\runtime\supervisor-status.json'
if (Test-Path -LiteralPath $ExistingStatus) {
    try {
        $Status = Get-Content -LiteralPath $ExistingStatus -Raw | ConvertFrom-Json
        if (Get-Process -Id ([int]$Status.pid) -ErrorAction SilentlyContinue) {
            Write-Host 'Neko AI same-user supervisor is already running.' -ForegroundColor Green
        }
    } catch {}
}

$Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SupervisorScript`" -WithTray"
$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentIdentity
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentIdentity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Neko AI 已安装为当前用户常驻任务：$CurrentIdentity" -ForegroundColor Green
Write-Host '它会在登录后启动托盘，并在 Neko 崩溃时自动恢复；不要再使用“以管理员身份运行”。'
