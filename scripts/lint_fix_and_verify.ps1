# Step 1 cleanup (2026-11-11): ruff's safe auto-fix for import order (I001) and
# unused imports (F401) only, then the full offline verification.
# Originals of every file ruff reported are in .omniharness/lint-cleanup-baseline-20261111/.
# No model, sandbox, network or production instance is touched.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\g-drive-env.ps1"
$ErrorActionPreference = 'Continue'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
Write-Output '=== ruff --fix (I001, F401 only)'
& $py -m ruff check --fix --select I001,F401 src tests scripts
Write-Output "ruff-fix exit=$LASTEXITCODE"
& (Join-Path $PSScriptRoot 'verify_context_v3.ps1')
