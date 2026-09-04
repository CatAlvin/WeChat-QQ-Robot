param(
    [string]$Server = '127.0.0.1',
    [int]$Port = 3306,
    [string]$Database = 'neko_ai',
    [string]$Username = 'neko',
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$EnvPath = Join-Path $ProjectRoot '.env'
$EnvTemplatePath = Join-Path $ProjectRoot '.env.example'

if (-not (Test-Path -LiteralPath $Python)) {
    throw '尚未安装项目依赖，请先运行 .\scripts\setup.ps1'
}
if (-not (Test-Path -LiteralPath $EnvTemplatePath)) {
    throw '缺少 .env.example，无法安全创建本机配置。'
}

$Confirmation = Read-Host "将验证并保存 MySQL 自动启动配置 $Username@$Server`:$Port/$Database；输入 YES 继续"
if ($Confirmation -cne 'YES') {
    Write-Host '已取消，未修改本机配置。'
    exit 0
}

$SecurePassword = Read-Host 'MySQL 密码（会写入本机 .env，不会显示在屏幕或命令历史中）' -AsSecureString
$PasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
$PreviousUrl = [Environment]::GetEnvironmentVariable('NEKO_DATABASE_URL', 'Process')
$PlainPassword = $null
$TemporaryEnvPath = "$EnvPath.tmp"

try {
    $PlainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPointer)
    $EscapedUser = [Uri]::EscapeDataString($Username)
    $EscapedPassword = [Uri]::EscapeDataString($PlainPassword)
    $EscapedDatabase = [Uri]::EscapeDataString($Database)
    $DatabaseUrl = "mysql+pymysql://$EscapedUser`:$EscapedPassword@$Server`:$Port/$EscapedDatabase`?charset=utf8mb4"
    $env:NEKO_DATABASE_URL = $DatabaseUrl

    Push-Location -LiteralPath (Join-Path $ProjectRoot 'backend')
    try {
        & $Python '-m' 'scripts.mysql_acceptance'
        if ($LASTEXITCODE -ne 0) {
            throw "MySQL 验证失败（exit code $LASTEXITCODE）；未修改 .env"
        }
    } finally {
        Pop-Location
    }

    if (Test-Path -LiteralPath $EnvPath) {
        $EnvironmentText = Get-Content -LiteralPath $EnvPath -Raw
    } else {
        $AuthSecret = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
        $BridgeSecret = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
        $EnvironmentText = Get-Content -LiteralPath $EnvTemplatePath -Raw
        $EnvironmentText = $EnvironmentText.Replace('replace-with-at-least-32-random-characters', $AuthSecret)
        $EnvironmentText = $EnvironmentText.Replace('replace-with-a-long-random-bridge-token', $BridgeSecret)
    }

    $DatabaseLine = "NEKO_DATABASE_URL=$DatabaseUrl"
    if ($EnvironmentText -match '(?m)^NEKO_DATABASE_URL=.*$') {
        $DatabaseLinePattern = [Regex]::new('(?m)^NEKO_DATABASE_URL=.*$')
        $EnvironmentText = $DatabaseLinePattern.Replace(
            $EnvironmentText,
            [System.Text.RegularExpressions.MatchEvaluator]{ param($Match) $DatabaseLine },
            1
        )
    } else {
        $EnvironmentText = $EnvironmentText.TrimEnd() + [Environment]::NewLine + $DatabaseLine + [Environment]::NewLine
    }

    [IO.File]::WriteAllText($TemporaryEnvPath, $EnvironmentText, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $TemporaryEnvPath -Destination $EnvPath -Force
    Write-Host "MySQL 自动启动配置已保存：$Username@$Server`:$Port/$Database（密码已隐藏）" -ForegroundColor Green
} finally {
    $PlainPassword = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPointer)
    if ($null -eq $PreviousUrl) {
        Remove-Item Env:NEKO_DATABASE_URL -ErrorAction SilentlyContinue
    } else {
        $env:NEKO_DATABASE_URL = $PreviousUrl
    }
    Remove-Item -LiteralPath $TemporaryEnvPath -ErrorAction SilentlyContinue
}

if (-not $NoRestart) {
    & (Join-Path $PSScriptRoot 'stop.ps1')
    & (Join-Path $PSScriptRoot 'start.ps1') -NoBrowser
    Write-Host 'Neko AI 已使用 MySQL 重新启动。请刷新本机后台并重新创建或登录管理员。' -ForegroundColor Green
}
