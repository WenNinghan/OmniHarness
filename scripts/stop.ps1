[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'common.ps1')

try {
    Assert-OmniDocker
    Invoke-OmniCompose -Arguments @('stop')
    $deployment = Join-Path $script:OmniRuntime 'deployment.env'
    if ((Test-Path -LiteralPath $deployment) -and ((Get-Content -LiteralPath $deployment) -contains 'OMNI_ENABLE_TOOLS=true')) {
        $helper = Join-Path $script:OmniRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $helper) {
            & $helper (Join-Path $PSScriptRoot 'm4_production_deploy.py') 'broker-stop'
            if ($LASTEXITCODE -ne 0) { throw 'Application containers stopped, but the workspace broker could not be stopped cleanly.' }
        }
    }
    Write-Host 'Services stopped. Database volumes, local data and credentials were preserved.'
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
