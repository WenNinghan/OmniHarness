param(
    [ValidateSet('start', 'stop', 'pair', 'status')][string]$Action = 'start',
    [switch]$NoBrowser,
    [switch]$NoPair
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$python = Join-Path $script:OmniRoot '.venv\Scripts\python.exe'
$statePath = Join-Path $script:OmniRuntime 'dev-processes.json'
$devPort = if ($env:OMNI_DEV_PORT) { [int]$env:OMNI_DEV_PORT } else { 8787 }
$address = "http://127.0.0.1:$devPort"

function Get-OwnedProcesses {
    if (-not (Test-Path -LiteralPath $statePath)) { return @() }
    $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $processes = @(Get-CimInstance Win32_Process)
    $rootIds = @($saved.api, $saved.worker)
    $owned = @($processes | Where-Object {
        ($_.ProcessId -in $rootIds -or $_.ParentProcessId -in $rootIds) -and
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($script:OmniRoot + '\', [System.StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match '-m omniharness\.cli (serve|worker)'
    })
    return $owned
}

if ($Action -eq 'stop') {
    $owned = @(Get-OwnedProcesses)
    foreach ($process in $owned) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Output 'Stopped this checkout''s development processes. Data is preserved.'
    exit 0
}
if ($Action -eq 'status') {
    Get-OwnedProcesses | Select-Object ProcessId, Name
    Write-Output "Development URL: $address (SQLite development mode)"
    exit 0
}
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync --python 3.12 in the project directory first.' }
Set-OmniPrivateDirectory -LiteralPath $script:OmniRuntime
Set-OmniPrivateDirectory -LiteralPath $script:OmniSecretDir
[System.IO.Directory]::CreateDirectory((Join-Path $script:OmniRuntime 'data')) | Out-Null
$env:OMNI_DATABASE_URL = 'sqlite:///' + (Join-Path $script:OmniRuntime 'data\local-dev.db').Replace([char]92, [char]47)
$env:OMNI_ALLOW_SQLITE = 'true'
$env:OMNI_DATA_DIR = Join-Path $script:OmniRuntime 'data'
$env:OMNI_SECRET_DIR = $script:OmniSecretDir
$env:OMNI_STATIC_DIR = Join-Path $script:OmniRoot 'web\dist'
$env:OMNI_HOST = '127.0.0.1'
$env:OMNI_PORT = "$devPort"
$env:OMNI_ALLOWED_ORIGINS = "$address,http://localhost:$devPort"
$env:PYTHONIOENCODING = 'utf-8'

if ($Action -eq 'pair') {
    & $python -m omniharness.cli auth-code
    if ($LASTEXITCODE -ne 0) { throw 'Could not generate pairing code. Start the development service first.' }
    exit 0
}
if (-not (Test-Path -LiteralPath (Join-Path $env:OMNI_STATIC_DIR 'index.html'))) {
    throw 'Build the Web console first: cd web; npm ci; npm run build'
}
if (@(Get-OwnedProcesses).Count -gt 0) { throw 'Development processes already exist. Use dev.ps1 status or dev.ps1 stop first.' }
$listener = Get-NetTCPConnection -LocalPort $devPort -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw "Port $devPort is occupied. Stop the existing service or set OMNI_DEV_PORT." }
Push-Location $script:OmniRoot
try {
    & $python -m omniharness.cli bootstrap
    if ($LASTEXITCODE -ne 0) { throw 'Development database initialization failed.' }
    $api = Start-Process -FilePath $python -ArgumentList @('-m','omniharness.cli','serve') -WorkingDirectory $script:OmniRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $script:OmniRuntime 'api.out.log') -RedirectStandardError (Join-Path $script:OmniRuntime 'api.err.log')
    $worker = Start-Process -FilePath $python -ArgumentList @('-m','omniharness.cli','worker') -WorkingDirectory $script:OmniRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $script:OmniRuntime 'worker.out.log') -RedirectStandardError (Join-Path $script:OmniRuntime 'worker.err.log')
    @{ api=$api.Id; worker=$worker.Id; port=$devPort } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $http = New-Object System.Net.Http.HttpClient($handler)
    $http.Timeout = [TimeSpan]::FromSeconds(2)
    $ready = $false
    try {
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            try { $reply = $http.GetAsync("$address/health").GetAwaiter().GetResult(); $ready = $reply.IsSuccessStatusCode; $reply.Dispose() } catch { }
            if ($ready) { break }
            Start-Sleep -Milliseconds 250
        }
    } finally { $http.Dispose(); $handler.Dispose() }
    if (-not $ready) { throw 'Service did not become healthy. Inspect .omniharness/api.err.log; use dev.ps1 stop before retrying.' }
    Write-Output "OmniHarness development mode: $address"
    Write-Output 'SQLite is used for this local preview. This does not validate the PostgreSQL/Docker deployment.'
    if (-not $NoPair) { & $python -m omniharness.cli auth-code }
    if (-not $NoBrowser) { Start-Process $address }
} finally { Pop-Location }
