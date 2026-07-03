# Singularity installer wrapper (Windows). All logic lives in install.py.
$ErrorActionPreference = "Stop"
$engine = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $engine ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "[install] creating venv at $venv"
    python -m venv $venv
}
& (Join-Path $venv "Scripts\python.exe") (Join-Path $PSScriptRoot "install.py") @args
exit $LASTEXITCODE
