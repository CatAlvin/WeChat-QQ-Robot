param([switch]$FromSupervisor)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $ProjectRoot 'data\neko-processes.json'
$SupervisorStatusFile = Join-Path $ProjectRoot 'data\runtime\supervisor-status.json'
$SupervisorCommandFile = Join-Path $ProjectRoot 'data\runtime\supervisor-command.json'
if (-not $FromSupervisor -and (Test-Path -LiteralPath $SupervisorStatusFile)) {
    try {
        $Supervisor = Get-Content -LiteralPath $SupervisorStatusFile -Raw | ConvertFrom-Json
        $Heartbeat = [DateTimeOffset]::Parse([string]$Supervisor.heartbeat_at)
        $SupervisorProcess = Get-Process -Id ([int]$Supervisor.pid) -ErrorAction SilentlyContinue
        if ($SupervisorProcess -and ([DateTimeOffset]::UtcNow - $Heartbeat).TotalSeconds -lt 12) {
            $Command = @{ id = [guid]::NewGuid().ToString(); action = 'stop'; requested_at = [DateTimeOffset]::UtcNow.ToString('o') }
            $TemporaryCommand = "$SupervisorCommandFile.tmp"
            $Command | ConvertTo-Json | Set-Content -LiteralPath $TemporaryCommand -Encoding utf8
            Move-Item -LiteralPath $TemporaryCommand -Destination $SupervisorCommandFile -Force
            Write-Host 'Neko AI stop request was handed to the same-user supervisor.' -ForegroundColor Yellow
            exit 0
        }
    } catch {
        # Fall back to the verified PID-based stop path below.
    }
}
if (-not (Test-Path -LiteralPath $PidFile)) {
    if (-not $FromSupervisor) {
        Write-Host 'No Neko AI processes were recorded by the start script.'
    }
    exit 0
}

$ProcessIds = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
$PidRecord = Get-Item -LiteralPath $PidFile
$Node = Get-Command node.exe -ErrorAction Stop
$Targets = @(
    @{
        Name = 'backend'
        Id = [int]$ProcessIds.backend
        ExpectedPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    },
    @{
        Name = 'frontend'
        Id = [int]$ProcessIds.frontend
        ExpectedPath = $Node.Source
    }
)
$FailedStops = @()
foreach ($Target in $Targets) {
    $ProcessId = $Target.Id
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $Process) { continue }

    $ExpectedPath = [IO.Path]::GetFullPath($Target.ExpectedPath)
    if ($Process.Path) {
        $ActualPath = [IO.Path]::GetFullPath($Process.Path)
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals($ActualPath, $ExpectedPath)) {
            throw "Refusing to stop PID $ProcessId because the stale record does not belong to Neko AI. The process record was preserved."
        }
    } else {
        # Windows may hide Path for a process even when it belongs to the same
        # desktop user. Fall back to both executable name and the PID record's
        # creation window so a recycled stale PID is still rejected.
        $ExpectedName = [IO.Path]::GetFileNameWithoutExtension($ExpectedPath)
        $StartedAt = $Process.StartTime
        $RecordedAt = $PidRecord.LastWriteTime
        $NameMatches = [StringComparer]::OrdinalIgnoreCase.Equals($Process.ProcessName, $ExpectedName)
        $StartMatches = $StartedAt -ge $RecordedAt.AddSeconds(-10) -and $StartedAt -le $RecordedAt.AddSeconds(10)
        if (-not $NameMatches -or -not $StartMatches) {
            throw "Refusing to stop PID $ProcessId because its hidden path cannot be safely matched to the Neko AI PID record. The process record was preserved."
        }
    }

    & taskkill.exe /PID $ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $FailedStops += $ProcessId
        continue
    }
    Wait-Process -Id $ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        $FailedStops += $ProcessId
    }
}
if ($FailedStops.Count -gt 0) {
    throw "Neko AI processes could not be stopped: $($FailedStops -join ', '). The process record was preserved."
}
Remove-Item -LiteralPath $PidFile
Write-Host 'Neko AI local services stopped.' -ForegroundColor Yellow
