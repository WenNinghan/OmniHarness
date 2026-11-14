# context-v3 offline verification (2026-11-11). Runs only local tests:
# no model, sandbox, network or production instance is touched.
# Usage from the project root:  .\scripts\verify_context_v3.ps1
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\g-drive-env.ps1"
# PowerShell 5.1 turns redirected native stderr into errors under 'Stop';
# keep going and judge each step only by its exit code.
$ErrorActionPreference = 'Continue'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $root ".omniharness\context-v3-verify-$stamp"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$base = Join-Path $env:TEMP "pytest-ctxv3-$stamp"
$results = [ordered]@{}

function Step($name, [scriptblock]$body) {
    Write-Output "=== $name"
    $log = Join-Path $out "$name.log"
    & $body *>&1 | Tee-Object -FilePath $log
    $results[$name] = $LASTEXITCODE
}

Step 'pytest-targeted' { & $py -m pytest -q --basetemp "$base-a" tests/test_context_v3.py tests/test_review_interruption.py tests/test_context_runtime.py tests/test_context_v2.py tests/test_context_policy.py tests/test_chat_context_integration.py tests/test_m3_host.py tests/test_m4_runtime.py }
Step 'ruff' { & $py -m ruff check src tests scripts }
Step 'pytest-full' { & $py -m pytest -q --basetemp "$base-b" }
Step 'web-test' { npm --prefix web test }
Step 'web-build' { npm --prefix web run build }

$results | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $out 'summary.json')
Write-Output "Logs: $out"
$results.GetEnumerator() | ForEach-Object { Write-Output ("{0,-16} exit={1}" -f $_.Key, $_.Value) }
