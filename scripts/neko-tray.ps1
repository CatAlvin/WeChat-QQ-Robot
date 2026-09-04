$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$CreatedNew = $false
$TrayMutex = [Threading.Mutex]::new($true, 'Local\NekoAiTray', [ref]$CreatedNew)
if (-not $CreatedNew) { exit 0 }

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDirectory = Join-Path $ProjectRoot 'data\runtime'
$StatusFile = Join-Path $RuntimeDirectory 'supervisor-status.json'
$CommandFile = Join-Path $RuntimeDirectory 'supervisor-command.json'
$TrayIconFile = Join-Path $ProjectRoot 'assets\neko-ai.ico'
New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null

function Send-NekoCommand {
    param([string]$Action)
    $Command = @{ id = [guid]::NewGuid().ToString(); action = $Action; requested_at = [DateTimeOffset]::UtcNow.ToString('o') }
    $Temporary = "$CommandFile.tray.tmp"
    $Command | ConvertTo-Json | Set-Content -LiteralPath $Temporary -Encoding utf8
    Move-Item -LiteralPath $Temporary -Destination $CommandFile -Force
}

$Menu = [Windows.Forms.ContextMenuStrip]::new()
$OpenItem = $Menu.Items.Add('打开 Neko 后台')
$StartItem = $Menu.Items.Add('启动 / 恢复')
$RestartItem = $Menu.Items.Add('重启 Neko')
$StopItem = $Menu.Items.Add('停止 Neko')
[void]$Menu.Items.Add('-')
$ExitItem = $Menu.Items.Add('退出托盘图标')

$Tray = [Windows.Forms.NotifyIcon]::new()
$CustomTrayIcon = $null
if (Test-Path -LiteralPath $TrayIconFile) {
    try {
        $CustomTrayIcon = [Drawing.Icon]::new($TrayIconFile)
        $Tray.Icon = $CustomTrayIcon
    } catch {
        $Tray.Icon = [Drawing.SystemIcons]::Application
    }
} else {
    $Tray.Icon = [Drawing.SystemIcons]::Application
}
$Tray.Text = 'Neko AI · 正在读取状态'
$Tray.ContextMenuStrip = $Menu
$Tray.Visible = $true

$OpenDashboard = { Start-Process 'http://127.0.0.1:3000' }
$OpenItem.add_Click($OpenDashboard)
$Tray.add_DoubleClick($OpenDashboard)
$StartItem.add_Click({ Send-NekoCommand 'start' })
$RestartItem.add_Click({ Send-NekoCommand 'restart' })
$StopItem.add_Click({ Send-NekoCommand 'stop' })
$ExitItem.add_Click({ $Tray.Visible = $false; [Windows.Forms.Application]::Exit() })

$Timer = [Windows.Forms.Timer]::new()
$Timer.Interval = 2500
$Timer.add_Tick({
    try {
        $State = Get-Content -LiteralPath $StatusFile -Raw | ConvertFrom-Json
        $Label = if ($State.services_running) { '运行中' } elseif ($State.desired_state -eq 'STOPPED') { '已停止' } else { '正在恢复' }
        $Tray.Text = "Neko AI · $Label"
    } catch {
        $Tray.Text = 'Neko AI · 监督进程未响应'
    }
})
$Timer.Start()
[Windows.Forms.Application]::Run()
$Timer.Dispose()
$Tray.Dispose()
if ($null -ne $CustomTrayIcon) { $CustomTrayIcon.Dispose() }
$Menu.Dispose()
$TrayMutex.ReleaseMutex()
$TrayMutex.Dispose()
