#requires -Version 7
<#
.SYNOPSIS
  Run the realtime paper terminal (demo or live) against local Docker middleware.

.EXAMPLE
  pwsh -File scripts/run_realtime.ps1
  pwsh -File scripts/run_realtime.ps1 -Mode live -Symbols BTCUSDT,ETHUSDT
  pwsh -File scripts/run_realtime.ps1 -Mode demo -Speed 15 -Port 8080
#>
param(
  [ValidateSet("demo", "live")][string]$Mode = "demo",
  [string]$Symbols = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT",
  [string]$Intervals = "15m,1h,4h,1d",
  [string]$Stack = "4h_15m",
  [int]$BootstrapBars = 5000,
  [double]$Speed = 15,
  [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run scripts/dev_up.ps1 -Install first." }

docker compose up -d | Out-Null
& $python -m ict_trader.realtime `
  --mode $Mode --symbols $Symbols --intervals $Intervals --stack $Stack `
  --bootstrap-bars $BootstrapBars --speed $Speed --port $Port
