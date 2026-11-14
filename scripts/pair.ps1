[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'common.ps1')

try {
    Assert-OmniDocker
    Write-Host 'Creating a one-time local pairing code. Enter it in the browser; do not share it or include it in diagnostics.'
    Invoke-OmniCompose -Arguments @('exec', '-T', 'api', 'omniharness', 'auth-code')
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
