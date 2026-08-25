$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$envFile = Join-Path (Get-Location) ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath ".env.example" -Destination $envFile
}
$envText = Get-Content -LiteralPath $envFile -Raw
if ($envText -notmatch "(?m)^APP_SECRET_KEY=.+") {
    $secretBytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($secretBytes)
    $generatedSecret = [Convert]::ToBase64String($secretBytes)
    if ($envText -match "(?m)^APP_SECRET_KEY=$") {
        $envText = [Regex]::Replace($envText, "(?m)^APP_SECRET_KEY=$", "APP_SECRET_KEY=$generatedSecret")
    } else {
        $envText += "`nAPP_SECRET_KEY=$generatedSecret`n"
    }
    Set-Content -LiteralPath $envFile -Value $envText -Encoding UTF8
}

$codexMode = "auto"
if ($envText -match "(?m)^CODEX_AUTH_MODE=(.+)$") {
    $codexMode = $Matches[1].Trim().ToLowerInvariant()
}
$codex = Get-Command codex -ErrorAction SilentlyContinue
$localRuntime = (Get-Command uv -ErrorAction SilentlyContinue) -and (Get-Command node -ErrorAction SilentlyContinue) -and (Get-Command npm -ErrorAction SilentlyContinue)
if ($codex -and $codexMode -ne "docker" -and $localRuntime) {
    Write-Host "Codex CLI detected; starting in local mode so the backend and worker can access Codex Auth." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "start-local.ps1")
    exit $LASTEXITCODE
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    try {
        docker compose version | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Starting AI Novel Generator with Docker Compose..." -ForegroundColor Cyan
            docker compose up --build
            exit $LASTEXITCODE
        }
    } catch {
        Write-Host "Docker Compose is not ready; falling back to local mode." -ForegroundColor Yellow
    }
}

& (Join-Path $PSScriptRoot "start-local.ps1")
