$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$runtimePath = Join-Path (Get-Location) "logs\runtime.json"
if (-not (Test-Path -LiteralPath $runtimePath)) {
    Write-Host "Runtime record not found: $runtimePath" -ForegroundColor Yellow
    Write-Host "The program may already be stopped, or it was not started by start.ps1."
    exit 0
}

$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
$processIds = @(
    [int]$runtime.frontend_pid,
    [int]$runtime.backend_pid,
    [int]$runtime.worker_pid
) | Select-Object -Unique

$stopped = 0
foreach ($processId in $processIds) {
    if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        continue
    }
    try {
        Stop-Process -Id $processId -Force -ErrorAction Stop
        Write-Host "Stopped process: $processId" -ForegroundColor Green
        $stopped++
    } catch {
        Write-Host "Could not stop process: $processId" -ForegroundColor Yellow
    }
}

if ($stopped -eq 0) {
    Write-Host "No running project processes were found." -ForegroundColor Yellow
} else {
    Write-Host "AI Novel Generator stopped." -ForegroundColor Cyan
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    try {
        $composeServices = @(docker compose ps -q 2>$null)
        if ($composeServices.Count -gt 0) {
            docker compose down
            Write-Host "Docker Compose services stopped. Data volumes were preserved." -ForegroundColor Cyan
        }
    } catch {
        Write-Host "Docker Compose was not running or could not be queried." -ForegroundColor Yellow
    }
}
