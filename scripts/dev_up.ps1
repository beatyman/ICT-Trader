#requires -Version 7
<#
.SYNOPSIS
  Start the local Redis + ClickHouse stack and prepare the Python environment.

.EXAMPLE
  pwsh -File scripts/dev_up.ps1
  pwsh -File scripts/dev_up.ps1 -Install
#>
param(
  [switch]$Install,
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker Desktop is required. Install/start it, then rerun this script."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running." }

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "[env] created .env from .env.example" -ForegroundColor Cyan
}

docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

Write-Host "[docker] waiting for health checks..." -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  $states = docker compose ps --format json | ConvertFrom-Json
  $healthy = @($states | Where-Object { $_.Name -in @("ict-redis", "ict-clickhouse") -and $_.Health -eq "healthy" }).Count
  if ($healthy -eq 2) { $ok = $true; break }
  Start-Sleep -Seconds 2
}
docker compose ps
if (-not $ok) { throw "middleware did not become healthy; run: docker compose logs --tail 80" }

$venv = Join-Path $root ".venv\Scripts\python.exe"
if ($Install -or -not (Test-Path $venv)) {
  if (-not $Python) {
    $candidate = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -ErrorAction SilentlyContinue
    if ($candidate) { $Python = $candidate.FullName } else { $Python = "python" }
  }
  if (-not (Test-Path $venv)) {
    Write-Host "[venv] creating with $Python" -ForegroundColor Cyan
    & $Python -m venv .venv
  }
  & $venv -m pip install -q --upgrade pip
  & $venv -m pip install -q -e ".[dev,realtime]"
}

Write-Host ""
Write-Host "Middleware ready:" -ForegroundColor Green
Write-Host "  Redis      redis://127.0.0.1:6379/0"
Write-Host "  ClickHouse http://127.0.0.1:8123  (database ict)"
Write-Host ""
Write-Host "Start the paper terminal:"
Write-Host "  .\.venv\Scripts\python.exe -m ict_trader.realtime --mode demo"
Write-Host "  .\.venv\Scripts\python.exe -m ict_trader.realtime --mode live"
Write-Host "  http://127.0.0.1:8080"
