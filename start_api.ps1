# One-command local launcher for the Quant Desk API (Windows).
# Note: this repo's venv was built under WSL, so prefer ./start_api.sh in WSL.
# This script uses whatever `python` is on PATH (must have the deps installed).
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not $env:PORT) { $env:PORT = "8000" }

Write-Host "Starting Quant Desk API on http://localhost:$($env:PORT)  (docs: /docs)"
python -m uvicorn api.server:app --host 0.0.0.0 --port $env:PORT --reload
