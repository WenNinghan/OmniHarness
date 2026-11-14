[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'common.ps1')

try {
    Assert-OmniDocker
    Invoke-OmniCompose -Arguments @('stop')
    Write-Host 'Services stopped. Database volumes, local data and credentials were preserved.'
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
