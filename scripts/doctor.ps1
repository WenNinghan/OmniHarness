[CmdletBinding()]
param([switch]$Fix)
$env:NO_PROXY = '127.0.0.1,localhost,172.17.0.1'
. (Join-Path $PSScriptRoot 'common.ps1')

Write-Host 'OmniHarness local deployment diagnostic (no environment values or credential contents)'
Write-Host "PowerShell: $($PSVersionTable.PSVersion)"
Write-Host "Compose file present: $(Test-Path -LiteralPath (Join-Path $script:OmniRoot 'compose.yaml'))"
Write-Host "Private deployment configuration present: $(Test-Path -LiteralPath $script:OmniComposeEnv)"
try {
    if ($Fix) {
        try {
            Assert-OmniDocker
        } catch {
            if ((Get-OmniDockerBackend) -eq 'wsl') {
                Write-Host 'WSL Docker engine not ready; attempting self-heal start...'
                Start-OmniWslEngine
            } else {
                throw
            }
        }
    }
    Assert-OmniDocker
    Write-Host 'Docker Linux engine: ready'
    Invoke-OmniDocker -Arguments @('compose', 'version', '--short')
    Invoke-OmniCompose -Arguments @('config', '--quiet')
    Write-Host 'Compose configuration: valid'
    $deployment = Join-Path $script:OmniRuntime 'deployment.env'
    $toolsEnabled = (Test-Path -LiteralPath $deployment) -and ((Get-Content -LiteralPath $deployment) -contains 'OMNI_ENABLE_TOOLS=true')
    $baseArguments = Get-OmniComposeArguments
    if ($Fix) {
        $needsStart = $false
        $probeEndpoint = Invoke-OmniDocker -Arguments (@('compose') + $baseArguments + @('port', 'api', '8787'))
        if ($LASTEXITCODE -ne 0 -or -not $probeEndpoint) {
            $needsStart = $true
        } else {
            $probeUri = "http://$(($probeEndpoint -join '').Trim())"
            try {
                $probeResp = Invoke-WebRequest -Uri "$probeUri/health" -UseBasicParsing -TimeoutSec 5
                if ($probeResp.StatusCode -ne 200) { $needsStart = $true }
            } catch {
                $needsStart = $true
            }
        }
        if (-not $needsStart -and $toolsEnabled) {
            try {
                $ctrlProbe = Invoke-WebRequest -Uri 'http://127.0.0.1:19096/health' -UseBasicParsing -TimeoutSec 5
                if ($ctrlProbe.StatusCode -ne 200) { $needsStart = $true }
            } catch {
                $needsStart = $true
            }
        }
        if ($needsStart) {
            Write-Host 'Services not healthy; attempting self-heal via scripts/start.ps1 -NoBuild...'
            $startParams = @{ NoBuild = $true }
            if ((Get-OmniDockerBackend) -eq 'wsl') { $startParams['StartEngine'] = $true }
            & (Join-Path $PSScriptRoot 'start.ps1') @startParams
            if ($LASTEXITCODE -ne 0) { throw 'Self-heal startup via scripts/start.ps1 failed.' }
        }
    }
    Invoke-OmniCompose -Arguments @('ps', '--all')
    $endpoint = Invoke-OmniDocker -Arguments (@('compose') + $baseArguments + @('port', 'api', '8787'))
    if ($LASTEXITCODE -ne 0 -or -not $endpoint) { throw 'API is not running or has no published port.' }
    $baseUri = "http://$(($endpoint -join '').Trim())"
    $response = Invoke-WebRequest -Uri "$baseUri/health" -UseBasicParsing -TimeoutSec 5
    if ($response.StatusCode -ne 200) { throw 'API health endpoint did not return HTTP 200.' }
    Write-Host 'API health: HTTP 200'
    $bootstrap = Invoke-RestMethod -Uri "$baseUri/api/v1/bootstrap" -TimeoutSec 5
    Write-Host "Bootstrap stage: $($bootstrap.stage) (demo_enabled=$($bootstrap.demo_enabled))"
    if ($toolsEnabled) {
        $ctrl = Invoke-WebRequest -Uri 'http://127.0.0.1:19096/health' -UseBasicParsing -TimeoutSec 5
        if ($ctrl.StatusCode -ne 200) { throw 'Sandbox controller health endpoint did not return HTTP 200.' }
        Write-Host 'Sandbox controller (127.0.0.1:19096): HTTP 200'
        $helper = Join-Path $script:OmniRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $helper) {
            & $helper (Join-Path $PSScriptRoot 'm4_production_deploy.py') 'broker-start'
            if ($LASTEXITCODE -ne 0) { throw 'Workspace broker health check failed.' }
        }
    }
    Write-Host 'Provider connectivity is not checked here. Use each model configuration connection test in the UI.'
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
