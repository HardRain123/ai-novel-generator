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
