param(
    [switch]$WithTray,
    [switch]$StartStopped
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDirectory = Join-Path $ProjectRoot 'data\runtime'
$StatusFile = Join-Path $RuntimeDirectory 'supervisor-status.json'
$CommandFile = Join-Path $RuntimeDirectory 'supervisor-command.json'
$PidFile = Join-Path $ProjectRoot 'data\neko-processes.json'
$StartScript = Join-Path $PSScriptRoot 'start.ps1'
$StopScript = Join-Path $PSScriptRoot 'stop.ps1'
$TrayScript = Join-Path $PSScriptRoot 'neko-tray.ps1'
New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null

$CreatedNew = $false
$Mutex = [Threading.Mutex]::new($true, 'Local\NekoAiSameUserSupervisor', [ref]$CreatedNew)
if (-not $CreatedNew) { exit 0 }

$DesiredState = if ($StartStopped) { 'STOPPED' } else { 'RUNNING' }
$RestartCount = 0
$LastError = $null
$LastAction = 'STARTUP'
$LastActionAt = [DateTimeOffset]::UtcNow
$NextRestartAt = [DateTimeOffset]::MinValue
$TrayProcess = $null
$BackendHealthFailures = 0
$FrontendHealthFailures = 0

function Write-AtomicJson {
    param([string]$Path, [hashtable]$Value)
    $Temporary = "$Path.tmp"
    $Json = $Value | ConvertTo-Json -Depth 6
    try {
        # Windows PowerShell's Move-Item/Replace behavior is not reliably
        # atomic when antivirus or the dashboard briefly holds the status
        # file. A direct UTF-8 write is preferable to terminating supervision.
        $Utf8 = [Text.UTF8Encoding]::new($false)
        [IO.File]::WriteAllText($Path, $Json, $Utf8)
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
        if ([string]$script:LastError -like 'STATUS_*') { $script:LastError = $null }
    } catch {
        # Status publication is diagnostic only and must never terminate the
        # process responsible for crash recovery.
        $script:LastError = "STATUS_WRITE_FAILED: $($_.Exception.Message)"
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
    }
}

function Get-NekoProcessState {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return @{ running = $false; backend = $false; frontend = $false; backend_healthy = $false; frontend_healthy = $false }
    }
    try {
        $Record = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
        $Backend = [bool](Get-Process -Id ([int]$Record.backend) -ErrorAction SilentlyContinue)
        $Frontend = [bool](Get-Process -Id ([int]$Record.frontend) -ErrorAction SilentlyContinue)
        $BackendHealthy = $false
        $FrontendHealthy = $false
        if ($Backend) {
            try {
                $Response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2
                $BackendHealthy = $Response.StatusCode -eq 200
            } catch {}
        }
        if ($Frontend) {
            try {
                $Response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:3000' -TimeoutSec 2
                $FrontendHealthy = $Response.StatusCode -eq 200
            } catch {}
        }
        return @{
            running = $Backend -and $Frontend
            backend = $Backend
            frontend = $Frontend
            backend_healthy = $BackendHealthy
            frontend_healthy = $FrontendHealthy
        }
    } catch {
        return @{ running = $false; backend = $false; frontend = $false; backend_healthy = $false; frontend_healthy = $false }
    }
}

function Stop-NekoManaged {
    try { & $StopScript -FromSupervisor } catch { $script:LastError = $_.Exception.Message }
}

function Start-NekoManaged {
    try {
        & $StartScript -NoBrowser -FromSupervisor
        $script:LastError = $null
        return $true
    } catch {
        $script:LastError = $_.Exception.Message
        return $false
    }
}

function Start-NekoTray {
    if (-not $WithTray -or -not (Test-Path -LiteralPath $TrayScript)) { return }
    if ($null -ne $script:TrayProcess -and -not $script:TrayProcess.HasExited) { return }
    $PowerShellExe = (Get-Process -Id $PID).Path
    $script:TrayProcess = Start-Process -FilePath $PowerShellExe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',$TrayScript -WindowStyle Hidden -PassThru
}

try {
    Start-NekoTray
    while ($true) {
        Start-NekoTray
        if (Test-Path -LiteralPath $CommandFile) {
            try {
                $Command = Get-Content -LiteralPath $CommandFile -Raw | ConvertFrom-Json
                Remove-Item -LiteralPath $CommandFile -Force
                $Action = ([string]$Command.action).ToLowerInvariant()
                $LastAction = $Action.ToUpperInvariant()
                $LastActionAt = [DateTimeOffset]::UtcNow
                if ($Action -eq 'stop') {
                    $DesiredState = 'STOPPED'
                    Stop-NekoManaged
                } elseif ($Action -eq 'restart') {
                    $DesiredState = 'RUNNING'
                    Stop-NekoManaged
                    Start-Sleep -Milliseconds 700
                    [void](Start-NekoManaged)
                } elseif ($Action -eq 'start') {
                    $DesiredState = 'RUNNING'
                    $State = Get-NekoProcessState
                    if (-not $State.running) {
                        Stop-NekoManaged
                        [void](Start-NekoManaged)
                    }
                } elseif ($Action -eq 'shutdown-supervisor') {
                    $DesiredState = 'STOPPED'
                    Stop-NekoManaged
                    break
                }
            } catch {
                $LastError = $_.Exception.Message
                Remove-Item -LiteralPath $CommandFile -Force -ErrorAction SilentlyContinue
            }
        }

        $State = Get-NekoProcessState
        if ($State.backend -and -not $State.backend_healthy) { $BackendHealthFailures++ } else { $BackendHealthFailures = 0 }
        if ($State.frontend -and -not $State.frontend_healthy) { $FrontendHealthFailures++ } else { $FrontendHealthFailures = 0 }
        $HealthRecoveryRequired = $BackendHealthFailures -ge 3 -or $FrontendHealthFailures -ge 3
        if (
            $DesiredState -eq 'RUNNING' -and
            ((-not $State.running) -or $HealthRecoveryRequired) -and
            [DateTimeOffset]::UtcNow -ge $NextRestartAt
        ) {
            $RecoveryAction = if ($HealthRecoveryRequired) { 'HEALTH_RECOVERY' } else { 'CRASH_RECOVERY' }
            Stop-NekoManaged
            if (Start-NekoManaged) {
                $RestartCount++
                $LastAction = $RecoveryAction
                $LastActionAt = [DateTimeOffset]::UtcNow
                $NextRestartAt = [DateTimeOffset]::UtcNow.AddSeconds(5)
                $BackendHealthFailures = 0
                $FrontendHealthFailures = 0
            } else {
                $NextRestartAt = [DateTimeOffset]::UtcNow.AddSeconds([Math]::Min(60, 5 + ($RestartCount * 5)))
            }
            $State = Get-NekoProcessState
        } elseif ($DesiredState -eq 'STOPPED' -and ($State.backend -or $State.frontend)) {
            Stop-NekoManaged
            $State = Get-NekoProcessState
        }

        Write-AtomicJson -Path $StatusFile -Value @{
            pid = $PID
            user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            desired_state = $DesiredState
            services_running = $State.running -and $State.backend_healthy -and $State.frontend_healthy
            backend_running = $State.backend -and $State.backend_healthy
            frontend_running = $State.frontend -and $State.frontend_healthy
            backend_process_running = $State.backend
            frontend_process_running = $State.frontend
            backend_health_failures = $BackendHealthFailures
            frontend_health_failures = $FrontendHealthFailures
            restart_count = $RestartCount
            last_action = $LastAction
            last_action_at = $LastActionAt.ToString('o')
            last_error = $LastError
            tray_running = $null -ne $TrayProcess -and -not $TrayProcess.HasExited
            tray_pid = if ($null -ne $TrayProcess -and -not $TrayProcess.HasExited) { $TrayProcess.Id } else { $null }
            heartbeat_at = [DateTimeOffset]::UtcNow.ToString('o')
        }
        Start-Sleep -Seconds 2
    }
} finally {
    if ($TrayProcess -and -not $TrayProcess.HasExited) { $TrayProcess.Kill() }
    Remove-Item -LiteralPath $StatusFile -Force -ErrorAction SilentlyContinue
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
