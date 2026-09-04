$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ModelRoot = Join-Path $ProjectRoot 'backend\data\models\tts'
$ModelName = 'kokoro-multi-lang-v1_0'
$ModelDirectory = Join-Path $ModelRoot $ModelName
$ModelFile = Join-Path $ModelDirectory 'model.onnx'
$TokensFile = Join-Path $ModelDirectory 'tokens.txt'
$Archive = Join-Path $ModelRoot "$ModelName.tar.bz2"
$DownloadUrl = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_0.tar.bz2'

if (-not (Test-Path -LiteralPath $Python)) {
    throw '尚未创建项目 Python 环境。请先运行 .\scripts\setup.ps1。'
}

& $Python -m pip install 'sherpa-onnx==1.13.2'

if ((Test-Path -LiteralPath $ModelFile) -and (Test-Path -LiteralPath $TokensFile)) {
    Write-Host '本地中文男声 TTS 模型已经就绪，无需重复下载。' -ForegroundColor Green
    exit 0
}

New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null
Write-Host '正在下载本地 Kokoro 中文少年男声模型（约 350 MB）……' -ForegroundColor Cyan
Invoke-WebRequest -Uri $DownloadUrl -OutFile $Archive

try {
    tar -xjf $Archive -C $ModelRoot
} finally {
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }
}

if (-not (Test-Path -LiteralPath $ModelFile) -or -not (Test-Path -LiteralPath $TokensFile)) {
    throw '模型下载完成，但文件结构不完整。请重新运行本脚本。'
}

Write-Host '本地中文男声 TTS 已安装。重启 Neko 后，可选择可爱、活泼或温柔少年预设。' -ForegroundColor Green
