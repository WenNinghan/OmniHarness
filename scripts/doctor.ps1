[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'common.ps1')

Write-Host 'OmniHarness local deployment diagnostic (no environment values or credential contents)'
Write-Host "PowerShell: $($PSVersionTable.PSVersion)"
Write-Host "Compose file present: $(Test-Path -LiteralPath (Join-Path $script:OmniRoot 'compose.yaml'))"
Write-Host "Private deployment configuration present: $(Test-Path -LiteralPath $script:OmniComposeEnv)"
try {
    Assert-OmniDocker
    Write-Host 'Docker Linux engine: ready'
    Invoke-OmniDocker -Arguments @('compose', 'version', '--short')
    Invoke-OmniCompose -Arguments @('config', '--quiet')
    Write-Host 'Compose configuration: valid'
    Invoke-OmniCompose -Arguments @('ps', '--all')
    $baseArguments = Get-OmniComposeArguments
    $endpoint = Invoke-OmniDocker -Arguments (@('compose') + $baseArguments + @('port', 'api', '8787'))
    if ($LASTEXITCODE -ne 0 -or -not $endpoint) { throw 'API is not running or has no published port.' }
    $uri = "http://$(($endpoint -join '').Trim())/health"
    $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 5
    if ($response.StatusCode -ne 200) { throw 'API health endpoint did not return HTTP 200.' }
    Write-Host 'API health: HTTP 200'
    Write-Host 'Provider connectivity is not checked here. Use each model configuration connection test in the UI.'
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
