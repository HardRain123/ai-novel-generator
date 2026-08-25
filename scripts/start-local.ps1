$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$projectRoot = (Get-Location).Path
$logRoot = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

function Test-LocalUrl([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

# Repeated double-clicks should only reopen the page, never start another process set.
if ((Test-LocalUrl "http://127.0.0.1:8000/api/health") -and (Test-LocalUrl "http://127.0.0.1:3000")) {
    if ($env:NOVEL_SKIP_BROWSER -ne "1") { Start-Process "http://localhost:3000" }
    exit 0
}

$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) { throw "Python 3.11+ is required. Install Python and run this file again." }
$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) { throw "Node.js 22+ is required for local mode. Install Node.js and run this file again." }
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { throw "Docker is unavailable and uv is not installed. Install uv from https://docs.astral.sh/uv/ and run this file again." }

$projectPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $projectPython)) {
    $uvCache = Join-Path ([IO.Path]::GetTempPath()) "ai-novel-generator-uv-cache"
    New-Item -ItemType Directory -Force -Path $uvCache | Out-Null
    $env:UV_CACHE_DIR = $uvCache
    uv sync --dev
}
if (-not (Test-Path -LiteralPath $projectPython)) { throw "项目虚拟环境创建失败：$projectPython" }

$webRoot = Join-Path $projectRoot "web"
Push-Location $webRoot
if (-not (Test-Path -LiteralPath (Join-Path $webRoot "node_modules"))) { npm ci }
if (-not (Test-Path -LiteralPath (Join-Path $webRoot ".next\BUILD_ID"))) { npm run build }
Pop-Location

$npmCommand = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npmCommand) { $npmCommand = $npm.Source }
$nodeCommand = $node.Source
$nextCli = Join-Path $webRoot "node_modules\next\dist\bin\next"
$runId = Get-Date -Format "yyyyMMdd-HHmmss"
$started = @()

function Start-HiddenService {
    param(
        [string]$Name,
        [string]$FilePath,
        [string]$WorkingDirectory,
        [string[]]$Arguments
    )
    $stdout = Join-Path $logRoot "$Name-$runId.out.log"
    $stderr = Join-Path $logRoot "$Name-$runId.err.log"
    $process = Start-Process -FilePath $FilePath -WorkingDirectory $WorkingDirectory `
        -ArgumentList $Arguments -WindowStyle Hidden -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -PassThru
    $script:started += $process
    return @{ Process = $process; Stdout = $stdout; Stderr = $stderr }
}

try {
    $backend = Start-HiddenService "backend" $projectPython $projectRoot @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000")
    $worker = Start-HiddenService "worker" $projectPython $projectRoot @("-m", "app.worker")
    $frontend = Start-HiddenService "frontend" $nodeCommand $webRoot @($nextCli, "start", "-H", "127.0.0.1", "-p", "3000")

    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        foreach ($service in @($backend, $worker, $frontend)) {
            if ($service.Process.HasExited) {
                $detail = if (Test-Path -LiteralPath $service.Stderr) { (Get-Content -LiteralPath $service.Stderr -Tail 20) -join "`n" } else { "没有错误日志" }
                throw "服务启动失败（退出码 $($service.Process.ExitCode)）：`n$detail`n日志：$($service.Stderr)"
            }
        }
        if ((Test-LocalUrl "http://127.0.0.1:8000/api/health") -and (Test-LocalUrl "http://127.0.0.1:3000")) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        throw "服务在 30 秒内没有就绪。请查看日志目录：$logRoot"
    }

    $runtime = @{
        started_at = (Get-Date).ToString("o")
        backend_pid = $backend.Process.Id
        worker_pid = $worker.Process.Id
        frontend_pid = $frontend.Process.Id
        log_directory = $logRoot
    }
    $runtime | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logRoot "runtime.json") -Encoding UTF8
    if ($env:NOVEL_SKIP_BROWSER -ne "1") { Start-Process "http://localhost:3000" }
    exit 0
} catch {
    foreach ($process in $started) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
