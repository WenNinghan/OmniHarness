# Start the reviewed host broker hidden; the profile pins helpers and boundaries.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'g-drive-env.ps1')
$brokerRoot = Join-Path $gRoot '.omniharness\m4-preview'
$brokerArgs = @('-d', 'OmniHarnessCheckAlt', '-u', 'root', '--exec', 'env',
    'OMNI_BROKER_PROFILE=/mnt/g/harness/deploy/m4-preview.broker.json',
    '/var/lib/omniharness-m4-preview/broker-venv/bin/python', '-m', 'uvicorn',
    'omniharness.workspace_broker:create_deployment_app', '--factory',
    '--app-dir', '/mnt/g/harness/src', '--host', '172.17.0.1', '--port', '19092',
    '--no-access-log')
$brokerProcess = Start-Process -FilePath 'wsl.exe' -ArgumentList $brokerArgs -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $brokerRoot 'logs\broker.stdout.log') `
    -RedirectStandardError (Join-Path $brokerRoot 'logs\broker.stderr.log') -PassThru
$brokerProcess.Id | Set-Content -LiteralPath (Join-Path $brokerRoot 'broker-wsl.pid')
Write-Output "M4 preview broker helper launched with PID $($brokerProcess.Id). Health must be checked separately."
