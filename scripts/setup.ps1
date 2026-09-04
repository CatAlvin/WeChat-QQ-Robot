$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath '.env')) {
    $authSecret = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
    $bridgeSecret = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
    $environmentTemplate = Get-Content -LiteralPath '.env.example' -Raw
    $environmentTemplate = $environmentTemplate.Replace('replace-with-at-least-32-random-characters', $authSecret)
    $environmentTemplate = $environmentTemplate.Replace('replace-with-a-long-random-bridge-token', $bridgeSecret)
    Set-Content -LiteralPath '.env' -Value $environmentTemplate -Encoding utf8NoBOM
}

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
}

& '.venv\Scripts\python.exe' -m pip install -r 'backend\requirements.txt'

Push-Location -LiteralPath 'frontend'
try {
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        pnpm install
        pnpm run build
    } elseif (Get-Command corepack -ErrorAction SilentlyContinue) {
        corepack pnpm install
        corepack pnpm run build
    } else {
        throw '需要 pnpm，或带 Corepack 的 Node.js 22+。'
    }
} finally {
    Pop-Location
}

Write-Host 'Neko AI 安装完成。运行 .\scripts\start.ps1 启动。' -ForegroundColor Green
