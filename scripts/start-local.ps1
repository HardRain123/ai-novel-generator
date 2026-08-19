$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) { throw "Python 3.11+ is required. Install Python and run this file again." }
$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) { throw "Node.js 22+ is required for local mode. Install Node.js and run this file again." }
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { throw "Docker is unavailable and uv is not installed. Install uv from https://docs.astral.sh/uv/ and run this file again." }

uv sync --dev
Push-Location (Join-Path (Get-Location) "web")
if (-not (Test-Path "node_modules")) { npm ci }
Pop-Location

$backend = Start-Process uv -ArgumentList @("run", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000") -PassThru
$worker = Start-Process uv -ArgumentList @("run", "python", "-m", "app.worker") -PassThru
$frontend = Start-Process npm -WorkingDirectory (Join-Path (Get-Location) "web") -ArgumentList @("run", "dev") -PassThru
Start-Sleep -Seconds 4
Start-Process "http://localhost:3000"

try {
    Wait-Process -Id $frontend.Id
} finally {
    foreach ($process in @($backend, $worker, $frontend)) {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
}
