param(
    [string]$Server = '127.0.0.1',
    [int]$Port = 3306,
    [string]$Database = 'neko_ai',
    [string]$Username = 'neko'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw '尚未安装项目依赖，请先运行 .\scripts\setup.ps1'
}

$Confirmation = Read-Host "将迁移并验收专用数据库 $Username@$Server`:$Port/$Database；输入 YES 继续"
if ($Confirmation -cne 'YES') {
    Write-Host '已取消，未连接数据库。'
    exit 0
}

$SecurePassword = Read-Host 'MySQL 密码（不会保存到文件或命令行）' -AsSecureString
$PasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
$PreviousUrl = [Environment]::GetEnvironmentVariable('NEKO_DATABASE_URL', 'Process')
try {
    $PlainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPointer)
    $EscapedUser = [Uri]::EscapeDataString($Username)
    $EscapedPassword = [Uri]::EscapeDataString($PlainPassword)
    $EscapedDatabase = [Uri]::EscapeDataString($Database)
    $env:NEKO_DATABASE_URL = "mysql+pymysql://$EscapedUser`:$EscapedPassword@$Server`:$Port/$EscapedDatabase`?charset=utf8mb4"
    Push-Location -LiteralPath (Join-Path $ProjectRoot 'backend')
    try {
        & $Python '-m' 'scripts.mysql_acceptance'
        if ($LASTEXITCODE -ne 0) { throw "MySQL 验收失败（exit code $LASTEXITCODE）" }
    } finally {
        Pop-Location
    }
} finally {
    $PlainPassword = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPointer)
    if ($null -eq $PreviousUrl) {
        Remove-Item Env:NEKO_DATABASE_URL -ErrorAction SilentlyContinue
    } else {
        $env:NEKO_DATABASE_URL = $PreviousUrl
    }
}
