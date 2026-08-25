[CmdletBinding()]
param(
    [switch]$Publish
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$root = (Get-Location).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "找不到项目虚拟环境：$python。请先运行 uv sync --dev。" }

$env:NEXT_DESKTOP = "1"
$env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:8765/api"
Push-Location (Join-Path $root "web")
try {
    if (-not (Test-Path -LiteralPath "node_modules")) { npm ci }
    npm run build
} finally {
    Pop-Location
}

& $python -c "import PyInstaller; print(PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) { throw "未安装 PyInstaller。请先运行：uv sync --dev" }

$release = Join-Path $root "release"
$build = Join-Path $root ".build\pyinstaller"
New-Item -ItemType Directory -Force -Path $release,$build | Out-Null

& $python -m PyInstaller --noconfirm --clean --onedir --name backend --distpath $release --workpath (Join-Path $build "backend") --specpath (Join-Path $build "backend") (Join-Path $root "packaging\backend_entry.py")
if ($LASTEXITCODE -ne 0) { throw "backend 打包失败" }
& $python -m PyInstaller --noconfirm --clean --onedir --name worker --hidden-import app.worker --distpath $release --workpath (Join-Path $build "worker") --specpath (Join-Path $build "worker") (Join-Path $root "packaging\worker_entry.py")
if ($LASTEXITCODE -ne 0) { throw "worker 打包失败" }

Push-Location (Join-Path $root "desktop")
try {
    if (-not (Test-Path -LiteralPath "node_modules")) { npm install }
    if ($Publish) { npm run dist:publish } else { npm run dist }
} finally {
    Pop-Location
}
