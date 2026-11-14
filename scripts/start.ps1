[CmdletBinding()]
param([switch]$NoBuild, [switch]$StartEngine)
. (Join-Path $PSScriptRoot 'common.ps1')

try {
    if ($StartEngine) { Start-OmniWslEngine }
    Assert-OmniDocker
    Initialize-OmniDirectories
    Invoke-OmniCompose -Arguments @('config', '--quiet')
    $context = Get-OmniComposeContext
    if ($context.PinnedRelease) {
        $deployment = Join-Path $script:OmniRuntime 'deployment.env'
        if ((Get-Content -LiteralPath $deployment) -contains 'OMNI_ENABLE_TOOLS=true') {
            $helper = Join-Path $script:OmniRoot '.venv\Scripts\python.exe'
            if (-not (Test-Path -LiteralPath $helper)) { throw 'The reviewed native workspace helper is missing. See docs/m4-deployment.md.' }
            & $helper (Join-Path $PSScriptRoot 'm4_production_deploy.py') 'broker-start'
            if ($LASTEXITCODE -ne 0) { throw 'The workspace broker did not start; application startup stopped.' }
        }
    }
    $startArguments = @('up', '-d', '--wait', '--wait-timeout', '180')
    if ($NoBuild -or $context.PinnedRelease) { $startArguments += '--no-build' } else { $startArguments += '--build' }
    Invoke-OmniCompose -Arguments $startArguments
    $baseArguments = Get-OmniComposeArguments
    $endpoint = Invoke-OmniDocker -Arguments (@('compose') + $baseArguments + @('port', 'api', '8787'))
    if ($LASTEXITCODE -ne 0) { throw 'Services started, but the UI port could not be resolved. Run scripts/doctor.ps1.' }
    Write-Host "Open http://$(($endpoint -join '').Trim())"
    Write-Host 'Pair your browser by running .\scripts\pair.ps1. Existing paired sessions remain usable.'
    Write-Host 'Agent and tool execution require explicit enablement. Pinned releases restart without rebuilding local source.'
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
