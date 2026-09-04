$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Push-Location -LiteralPath (Join-Path $ProjectRoot 'backend')
try {
    & '..\.venv\Scripts\python.exe' -m pytest
    if ($LASTEXITCODE -ne 0) { throw "后端验收失败（exit code $LASTEXITCODE）" }
} finally {
    Pop-Location
}

Push-Location -LiteralPath (Join-Path $ProjectRoot 'frontend')
try {
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        pnpm run lint
        if ($LASTEXITCODE -ne 0) { throw "前端代码检查失败（exit code $LASTEXITCODE）" }
        pnpm run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败（exit code $LASTEXITCODE）" }
    } else {
        corepack pnpm run lint
        if ($LASTEXITCODE -ne 0) { throw "前端代码检查失败（exit code $LASTEXITCODE）" }
        corepack pnpm run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败（exit code $LASTEXITCODE）" }
    }
} finally {
    Pop-Location
}
